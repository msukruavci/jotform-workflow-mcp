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

from mcp_server import graph, schema_registry
from mcp_server import tree_builder as tb
from mcp_server.schema_registry import BRANCHING_TYPES
from mcp_server.jotform_client import JotformClient, JotformAPIError
from mcp_server.models import (
    Connection, FormField, FormFieldList, FormList, FormSummary, Step,
    StepDetail, WorkflowDetail, WorkflowHealth, WorkflowList, WorkflowSummary,
)

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
        TRUE or FALSE on an if/else, or the branch name on a conditional
        branch. That is how you tell two paths apart. A split's paths are
        equivalent and carry no outcome.

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
            return WorkflowDetail(workflow_id=workflow_id, error=str(e))

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

        # A branching step whose exits came back unlabelled means the outcome
        # data moved or is missing. Say so rather than letting the model treat
        # the two paths as interchangeable.
        diagnostics: dict = {}
        branching_ids = {
            str(el.get("element_id")) for el in elements
            if el.get("type") in BRANCHING_TYPES
        }
        unlabelled = sorted(
            sid for sid in branching_ids
            if any(c.from_step == sid for c in connections)
            and not any(c.from_step == sid and c.outcome for c in connections)
        )
        if unlabelled:
            diagnostics["unlabelled_branching_steps"] = unlabelled
            diagnostics["note"] = (
                "These steps branch, but their outcomes carried no link mapping, "
                "so which path is which cannot be determined. Run "
                "probes/inspect_outcomes.py against this workflow."
            )

        return WorkflowDetail(
            workflow_id=str(wf.get("id")) if wf.get("id") is not None else workflow_id,
            title=wf.get("title"),
            status=wf.get("status"),
            publish_status=wf.get("publishStatus"),
            steps=steps,
            connections=connections,
            health=WorkflowHealth(
                **health_raw,
                unknown_types=unknown_types,
                unconnected_branches=unconnected_branches,
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

        return StepDetail(step_id=step_id, type=config.get("type"), config=config)

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
            return FormFieldList(form_id=form_id, error=str(e))

        return FormFieldList(form_id=form_id, fields=[
            FormField(
                field_id=qid,
                label=q.get("text"),
                type=q.get("type"),
                required=q.get("required"),
            )
            for qid, q in (questions or {}).items()
            if isinstance(q, dict)
        ])