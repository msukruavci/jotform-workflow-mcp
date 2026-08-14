"""
Layer 2: reading.

Everything here is read-only and safe to call freely. The shapes returned
are trimmed — Jotform's raw responses carry UI bookkeeping (uuids, canvas
coordinates) that would add noise to a conversation.

Two corrections worth recording (2026-08-07):

1. This module originally dropped everything about a link except its two
   endpoints, which lost the distinction between an if/else step's TRUE and
   FALSE branches — that is meaning, not plumbing.

2. The obvious place to look for that label was the link. It isn't there:
   `labels` is empty on every link, and `fromPortName` ("RIGHT_MIDDLE_Out")
   describes where an edge leaves the box on the canvas, which happens to
   correlate with the branch and would have been a plausible, wrong answer.
   The label lives on the *deciding element*, as
   `outcomes[] = {conditionValue, linkID}`. Verified via
   probes/inspect_outcomes.py. See docs/decision-log.md.
"""
from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from mcp_server import graph, revision_log, schema_registry, workflow_inspector
from mcp_server import tree_builder as tb
from mcp_server.schema_registry import BRANCHING_TYPES
from mcp_server.jotform_client import JotformClient, JotformAPIError
from mcp_server.models import (
    Connection, FormField, FormFieldList, FormList, FormSummary, Step,
    StepDetail, WorkflowDetail, WorkflowHealth, WorkflowList, WorkflowSummary,
    WorkflowGap, WorkflowGapReport, WorkflowRevisionList, WorkflowRevisionSummary,
)


def _workflow_url(workflow_id: str | None) -> str | None:
    return f"https://www.jotform.com/workflow/{workflow_id}/build" if workflow_id else None


def _form_url(form_id: str | None) -> str | None:
    return f"https://www.jotform.com/build/{form_id}" if form_id else None


def _sign_url_from_config(config: dict) -> str | None:
    for key in ("signDocumentID", "sign_document_id", "documentID", "document_id", "sign_id", "signID"):
        value = config.get(key)
        if value:
            return f"https://www.jotform.com/sign/{value}"
    return None


def _field_options(question: dict) -> list[str]:
    options = question.get("options")
    if isinstance(options, str):
        return [item.strip() for item in options.split("|") if item.strip()]
    if isinstance(options, list):
        return [str(item).strip() for item in options if str(item).strip()]
    return []


def _hydrate_elements_for_inspection(client: JotformClient, workflow_id: str, elements: list[dict]) -> list[dict]:
    hydrated = []
    for element in elements:
        element_id = element.get("element_id")
        if element_id is None:
            hydrated.append(element)
            continue
        try:
            full = client.get_element(workflow_id, element_id)
        except JotformAPIError:
            hydrated.append(element)
            continue
        hydrated.append({**element, **full} if isinstance(full, dict) else element)
    return hydrated


def _outcome_map(elements: list[dict]) -> tuple[dict[str, str], list[str]]:
    """
    Build link_id -> branch label from the deciding elements.

    Also returns branches that are defined but wired to nothing: an if/else
    with a FALSE outcome and no linkID has a path the user drew in their head
    but not on the canvas. Jotform's builder flags this; the API does not.

    Labelling goes through tree_builder.outcome_label, not a raw
    `conditionValue` read here — the same field priority connect_steps
    uses to resolve an outcome by name, kept in one place so the two can't
    drift apart. This matters concretely for workflow_conditional_branch:
    every *named* custom branch shares the literal conditionValue
    "CUSTOM" — the real, human label lives in `branchName` instead.
    Reading conditionValue directly here would have labelled three
    different branches all "CUSTOM" in get_workflow's connection list,
    same bug as connect_steps had before outcome_label existed. Confirmed
    2026-08-12 against a real conditional-branch element
    (probes/inspect_conditional_branch_outcomes.py).
    """
    mapping: dict[str, str] = {}
    unconnected: list[str] = []

    for el in elements:
        if el.get("type") not in BRANCHING_TYPES:
            continue
        step_id = el.get("element_id")
        for outcome in el.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            label = tb.outcome_label(outcome)
            link_id = outcome.get("linkID")
            if link_id in (None, 0, "0", ""):
                unconnected.append(f"step {step_id} {label or outcome.get('outcomeID')}")
            elif label:
                mapping[str(link_id)] = str(label)

    return mapping, unconnected


def register(mcp: MCPServer, client: JotformClient) -> None:
    @mcp.tool()
    def list_workflows() -> WorkflowList:
        """
        List the user's workflows.

        Returns each workflow's id, title, status, when it was last updated,
        and run_count — how many times it has actually run, which is the
        quickest way to tell a live workflow from an abandoned draft.

        Use the id with get_workflow to see the steps.
        """
        try:
            workflows = client.list_workflows()
        except JotformAPIError as e:
            return WorkflowList(error=str(e))

        return WorkflowList(workflows=[
            WorkflowSummary(
                workflow_id=w.get("id"),
                workflow_url=_workflow_url(w.get("id")),
                title=w.get("title"),
                status=w.get("status"),
                updated_at=w.get("updated_at"),
                run_count=(w.get("instance_summary") or {}).get("total"),
            )
            for w in workflows
        ])

    @mcp.tool()
    def get_workflow(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
    ) -> WorkflowDetail:
        """
        Get a workflow's structure: its steps, how they connect, and whether
        the structure is sound.

        Each connection carries an `outcome` when it leaves a branching step:
        TRUE or FALSE on an if/else, the branch name on a conditional
        branch, or the button/outcome text on an approval/task. That is how
        you tell two paths apart. A split's paths are equivalent and carry
        no outcome.

        `health` reports steps that can never run (unreachable from the start
        point), steps that lead nowhere, branches defined but wired to nothing,
        and step types this server has no schema for. A workflow can look fine
        as a list and still be broken.

        Steps are summaries only — use get_step_details for one step's full
        configuration.
        """
        try:
            combined = client.get_workflow_combined(workflow_id)
        except JotformAPIError as e:
            return WorkflowDetail(workflow_id=workflow_id, workflow_url=_workflow_url(workflow_id), error=str(e))

        wf = combined.get("workflow", {}) or {}
        elements = [el for el in (combined.get("elements") or []) if isinstance(el, dict)]
        links = [ln for ln in (combined.get("links") or []) if isinstance(ln, dict)]

        outcome_by_link, unconnected_branches = _outcome_map(elements)

        steps: list[Step] = []
        unknown_types: list[str] = []
        for el in elements:
            step_type = el.get("type")
            known = bool(step_type) and schema_registry.is_known_type(step_type)
            if step_type and not known and step_type not in unknown_types:
                unknown_types.append(step_type)
            steps.append(Step(
                step_id=str(el.get("element_id")) if el.get("element_id") is not None else None,
                type=step_type,
                label=el.get("name") or schema_registry.default_label(step_type),
                trigger_form_id=el.get("resourceID"),
                trigger_form_url=_form_url(el.get("resourceID")),
                sign_url=_sign_url_from_config(el) if step_type == "workflow_sign_document" else None,
                known_type=known,
            ))

        connections = []
        for ln in links:
            link_id = str(ln.get("link_id")) if ln.get("link_id") is not None else None
            connections.append(Connection(
                link_id=link_id,
                from_step=str(ln.get("fromElement")) if ln.get("fromElement") is not None else None,
                to_step=str(ln.get("toElement")) if ln.get("toElement") is not None else None,
                outcome=outcome_by_link.get(link_id or ""),
                from_port=ln.get("fromPortName"),
            ))

        health_raw = graph.analyse(
            [s.model_dump() for s in steps],
            [c.model_dump() for c in connections],
        )

        diagnostics: dict = workflow_inspector.branch_diagnostics(elements, links)
        if (
            diagnostics["unlabelled_branching_steps"]
            or diagnostics["invalid_branch_links"]
        ):
            diagnostics["note"] = (
                "Branch links must be represented both in links[] and in the "
                "source element's outcomes[].linkID. These entries do not match."
            )

        return WorkflowDetail(
            workflow_id=str(wf.get("id")) if wf.get("id") is not None else workflow_id,
            workflow_url=_workflow_url(str(wf.get("id")) if wf.get("id") is not None else workflow_id),
            title=wf.get("title"),
            status=wf.get("status"),
            publish_status=wf.get("publishStatus"),
            steps=steps,
            connections=connections,
            health=WorkflowHealth(
                **health_raw,
                unknown_types=unknown_types,
                unconnected_branches=unconnected_branches,
                invalid_branch_links=diagnostics["invalid_branch_links"],
                unlabelled_branching_steps=diagnostics["unlabelled_branching_steps"],
            ),
            diagnostics=diagnostics,
        )

    @mcp.tool()
    def get_step_details(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        step_id: Annotated[str, Field(description="From get_workflow's steps list.")],
    ) -> StepDetail:
        """
        Get the full configuration of a single step — subject line, recipients,
        condition terms, and so on.

        get_workflow only summarizes steps; use this when you need to know
        exactly how one step is set up, for example before changing it.
        """
        try:
            config = client.get_element(workflow_id, step_id)
        except JotformAPIError as e:
            return StepDetail(step_id=step_id, error=str(e))

        if not isinstance(config, dict):
            return StepDetail(step_id=step_id, error=f"Unexpected response: {type(config).__name__}")

        return StepDetail(
            step_id=step_id,
            type=config.get("type"),
            sign_url=_sign_url_from_config(config) if config.get("type") == "workflow_sign_document" else None,
            config=config,
        )

    @mcp.tool()
    def list_workflow_revisions(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        limit: Annotated[int, Field(
            description="Maximum revisions to return, newest first. Default 10."
        )] = 10,
    ) -> WorkflowRevisionList:
        """
        List saved workflow revisions for this MCP server session/history.

        Revisions are full snapshots captured automatically before mutating
        tools write to Jotform. Use restore_workflow_revision to preview and
        restore one of them.
        """
        summaries = revision_log.list_workflow_revisions(workflow_id, limit=limit)
        return WorkflowRevisionList(
            workflow_id=workflow_id,
            workflow_url=_workflow_url(workflow_id),
            revisions=[WorkflowRevisionSummary(**summary) for summary in summaries],
        )

    @mcp.tool()
    def inspect_workflow_gaps(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
    ) -> WorkflowGapReport:
        """
        Check for incomplete workflow setup before continuing or publishing.

        This catches empty links, unwired branch outcomes, missing assignees,
        missing email/task content, and condition fields that are not real
        fields on the trigger form. Call this before presenting a workflow as
        ready, and when the user asks what still needs to be completed.
        """
        try:
            combined = client.get_workflow_combined(workflow_id)
        except JotformAPIError as e:
            return WorkflowGapReport(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                error=str(e),
            )

        elements = [el for el in (combined.get("elements") or []) if isinstance(el, dict)]
        trigger_form_id = workflow_inspector.trigger_form_id(elements)
        questions = {}
        if trigger_form_id:
            try:
                questions = client.get_form_questions(trigger_form_id)
            except JotformAPIError as e:
                return WorkflowGapReport(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    trigger_form_id=trigger_form_id,
                    trigger_form_url=_form_url(trigger_form_id),
                    error=f"Could not inspect trigger form fields: {e}",
                )

        combined = {
            **combined,
            "elements": _hydrate_elements_for_inspection(client, workflow_id, elements),
        }
        report = workflow_inspector.inspect_workflow(combined, questions)
        return WorkflowGapReport(
            workflow_id=report.get("workflow_id") or workflow_id,
            workflow_url=report.get("workflow_url") or _workflow_url(workflow_id),
            trigger_form_id=report.get("trigger_form_id"),
            trigger_form_url=report.get("trigger_form_url"),
            ok_to_publish=bool(report.get("ok_to_publish")),
            issues=[WorkflowGap(**issue) for issue in report.get("issues", [])],
            available_form_fields=[
                FormField(
                    field_id=qid,
                    label=q.get("text"),
                    type=q.get("type"),
                    required=q.get("required"),
                    options=_field_options(q),
                )
                for qid, q in (questions or {}).items()
                if isinstance(q, dict)
            ],
        )

    @mcp.tool()
    def list_forms() -> FormList:
        """
        List the user's forms, with each form's id, title, status and
        submission count.

        A workflow is triggered by one of these, so this is usually the first
        call when working out which form a workflow is about.
        """
        try:
            forms = client.list_forms()
        except JotformAPIError as e:
            return FormList(error=str(e))

        return FormList(forms=[
            FormSummary(
                form_id=f.get("id"),
                form_url=_form_url(f.get("id")),
                title=f.get("title"),
                status=f.get("status"),
                submission_count=f.get("count"),
            )
            for f in forms
        ])

    @mcp.tool()
    def get_form_fields(
        form_id: Annotated[str, Field(description="From list_forms.")],
    ) -> FormFieldList:
        """
        List a form's fields (questions), with each field's id, label, type
        and whether it is required.

        Needed for two things: picking which field a condition should test,
        and picking which field holds the email address when sending mail to
        the person who submitted the form.
        """
        try:
            questions = client.get_form_questions(form_id)
        except JotformAPIError as e:
            return FormFieldList(form_id=form_id, form_url=_form_url(form_id), error=str(e))

        return FormFieldList(form_id=form_id, form_url=_form_url(form_id), fields=[
            FormField(
                field_id=qid,
                label=q.get("text"),
                type=q.get("type"),
                required=q.get("required"),
                options=_field_options(q),
            )
            for qid, q in (questions or {}).items()
            if isinstance(q, dict)
        ])
