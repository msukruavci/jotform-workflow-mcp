"""
Layer 2: reading.

Everything here is read-only and safe to call freely. Normal MCP reading
tools return trimmed shapes because Jotform's raw UI bookkeeping would add
noise to a conversation. The dedicated workflow-preview reader is the one
exception: it intentionally preserves the native canvas properties required
by Jotform's own read-only renderer.

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

from copy import deepcopy
from html import unescape
import re
from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from mcp_server import audit_log, graph, revision_log, schema_registry, sync_state, workflow_inspector
from mcp_server import tree_builder as tb
from mcp_server.schema_registry import BRANCHING_TYPES
from mcp_server.jotform_client import (
    JotformClient,
    JotformAPIError,
    workflow_revision_id,
    workflow_updated_at,
)
from mcp_server.models import (
    Connection, EmailStepIncoming, EmailStepSummary, FormField, FormList, FormSummary, Step,
    StepDetail, StepEdgeSummary, StepStateSummary, WorkflowDetail, WorkflowHealth, WorkflowList, WorkflowSummary,
    WorkflowGap, WorkflowGapReport, WorkflowRevisionList, WorkflowRevisionSummary,
    WorkflowPreviewData,
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


def form_fields_from_questions(questions: dict | None) -> list[FormField]:
    return [
        FormField(
            field_id=str(qid),
            name=q.get("name") or str(qid),
            label=q.get("text"),
            type=q.get("type"),
            required=q.get("required"),
            options=_field_options(q),
        )
        for qid, q in (questions or {}).items()
        if isinstance(q, dict)
    ]


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


def _plain_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _collect_question_context(
    elements: list[dict],
    explicit_questions: dict | None = None,
) -> dict[str, dict]:
    questions: dict[str, dict] = {}
    for qid, question in (explicit_questions or {}).items():
        if isinstance(question, dict):
            questions[str(qid)] = question

    for element in elements:
        resource = element.get("resourceObject")
        if not isinstance(resource, dict):
            continue
        resource_questions = resource.get("questions")
        if not isinstance(resource_questions, dict):
            continue
        for qid, question in resource_questions.items():
            if isinstance(question, dict):
                questions.setdefault(str(qid), question)
    return questions


def _question_label_by_token(questions: dict | None, token: str) -> str | None:
    cleaned = token.strip().strip("{}").strip()
    if not cleaned:
        return None
    for qid, question in (questions or {}).items():
        if not isinstance(question, dict):
            continue
        candidates = {
            str(qid),
            str(question.get("qid") or ""),
            str(question.get("name") or ""),
        }
        if cleaned in candidates:
            label = _plain_text(question.get("text") or question.get("label"))
            return label or cleaned
    return None


def _display_field_tokens(value: object, questions: dict | None) -> str:
    text = _plain_text(value)
    if not text or not questions:
        return text

    def replace(match: re.Match) -> str:
        token = match.group(1)
        if token in {"id", "form_title"}:
            return match.group(0)
        label = _question_label_by_token(questions, token)
        return "{" + label + "}" if label else match.group(0)

    return re.sub(r"\{([^{}]+)\}", replace, text)


def _recipient_values(value: object) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        recipients: list[str] = []
        for item in value:
            recipients.extend(_recipient_values(item))
        return recipients
    if isinstance(value, dict):
        for key in ("value", "text", "email", "label", "name"):
            if value.get(key):
                return [_plain_text(value.get(key))]
        return []
    return [_plain_text(value)]


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _field_present(element: dict, *field_names: str) -> bool:
    return any(_has_value(element.get(field_name)) for field_name in field_names)


def _first_plain_value(element: dict, *field_names: str) -> str | None:
    for field_name in field_names:
        value = _plain_text(element.get(field_name))
        if value:
            return value
    return None


def _outcome_summaries(element: dict) -> list[dict]:
    summaries: list[dict] = []
    for outcome in element.get("outcomes") or []:
        if not isinstance(outcome, dict):
            continue
        summaries.append({
            "label": tb.outcome_label(outcome) or outcome.get("outcomeID"),
            "link_id": str(outcome.get("linkID")) if outcome.get("linkID") else None,
            "has_condition_terms": _has_value(outcome.get("conditionTerms")),
        })
    return summaries


def _step_key_config_and_missing(
    element: dict,
    questions: dict | None = None,
) -> tuple[dict, list[str]]:
    step_type = element.get("type")
    config: dict = {}
    missing: list[str] = []

    def require(field: str, present: bool) -> None:
        if not present:
            missing.append(field)

    if step_type in ("workflow_send_email", "workflow_reminder_email"):
        recipients = [
            _display_field_tokens(recipient, questions)
            for recipient in _recipient_values(element.get("to"))
            if recipient
        ]
        subject = _display_field_tokens(element.get("subject"), questions) or None
        body = _display_field_tokens(
            element.get("content") or element.get("body") or element.get("message"),
            questions,
        )
        config.update({
            "to": recipients,
            "subject": subject,
            "content_present": bool(body),
            "content_excerpt": body[:180] if body else None,
        })
        require("to", bool(recipients))
        if step_type == "workflow_send_email":
            require("subject", bool(subject))
            require("content", bool(body))
        else:
            timing_present = _field_present(
                element,
                "timing",
                "executeWhen",
                "afterAmount",
                "remindAfter__afterAmount",
                "reminder__remindAfter__afterAmount",
            )
            config["timing_present"] = timing_present
            require("timing", timing_present)
        return config, missing

    if step_type == "workflow_approval":
        approvers = _recipient_values(element.get("approver"))
        description = _plain_text(element.get("taskDescription"))
        config.update({
            "approver": approvers,
            "taskDescription_present": bool(description),
            "taskDescription_excerpt": description[:180] if description else None,
            "outcomes": _outcome_summaries(element),
        })
        require("approver", bool(approvers))
        require("taskDescription", bool(description))
        return config, missing

    if step_type == "workflow_assign_task":
        assignees = _recipient_values(element.get("assignee"))
        description = _plain_text(element.get("taskDescription"))
        config.update({
            "assignee": assignees,
            "taskDescription_present": bool(description),
            "taskDescription_excerpt": description[:180] if description else None,
            "outcomes": _outcome_summaries(element),
        })
        require("assignee", bool(assignees))
        require("taskDescription", bool(description))
        return config, missing

    if step_type in ("workflow_assign", "workflow_assign_form"):
        assignees = _recipient_values(element.get("assignee"))
        config["assignee"] = assignees
        require("assignee", bool(assignees))
        if step_type == "workflow_assign_form":
            form_id = _first_plain_value(element, "formID", "resourceID")
            config["form_id"] = form_id
            require("formID", bool(form_id))
        return config, missing

    if step_type == "workflow_sign_document":
        document_id = _first_plain_value(element, "signDocumentID", "sign_document_id", "documentID", "document_id")
        signer_mapping_present = _field_present(element, "signerMapping", "signers", "recipients")
        config.update({
            "document_id": document_id,
            "signer_mapping_present": signer_mapping_present,
        })
        require("documentID", bool(document_id))
        require("signerMapping", signer_mapping_present)
        return config, missing

    if step_type == "workflow_binary_decision":
        condition_present = _field_present(element, "conditionTerms")
        config["condition_terms_present"] = condition_present
        require("conditionTerms", condition_present)
        return config, missing

    if step_type == "workflow_conditional_branch":
        outcomes = _outcome_summaries(element)
        config["outcomes"] = outcomes
        require("outcomes", bool(outcomes))
        for idx, outcome in enumerate(element.get("outcomes") or [], start=1):
            if not isinstance(outcome, dict):
                continue
            if outcome.get("conditionValue") != "OTHER" and not _has_value(outcome.get("conditionTerms")):
                missing.append(f"outcomes[{idx}].conditionTerms")
        return config, missing

    if step_type == "workflow_webhook":
        url = _first_plain_value(element, "url", "webhookURL", "webhookUrl")
        config["url"] = url
        require("url", bool(url))
        return config, missing

    return config, missing


def _step_state_summaries(
    elements: list[dict],
    links: list[dict],
    outcome_by_link: dict[str, str],
    questions: dict | None = None,
) -> list[StepStateSummary]:
    incoming_by_step: dict[str, list[StepEdgeSummary]] = {}
    outgoing_by_step: dict[str, list[StepEdgeSummary]] = {}
    for link in links:
        link_id = str(link.get("link_id")) if link.get("link_id") is not None else None
        from_step = str(link.get("fromElement")) if link.get("fromElement") is not None else None
        to_step = str(link.get("toElement")) if link.get("toElement") is not None else None
        outcome = outcome_by_link.get(link_id or "")
        if to_step:
            incoming_by_step.setdefault(to_step, []).append(StepEdgeSummary(
                link_id=link_id,
                step_id=from_step,
                outcome=outcome,
            ))
        if from_step:
            outgoing_by_step.setdefault(from_step, []).append(StepEdgeSummary(
                link_id=link_id,
                step_id=to_step,
                outcome=outcome,
            ))

    summaries: list[StepStateSummary] = []
    for element in elements:
        step_id = str(element.get("element_id")) if element.get("element_id") is not None else None
        key_config, missing_fields = _step_key_config_and_missing(element, questions)
        summaries.append(StepStateSummary(
            step_id=step_id,
            type=element.get("type"),
            label=element.get("name") or schema_registry.default_label(element.get("type")),
            incoming=incoming_by_step.get(step_id or "", []),
            outgoing=outgoing_by_step.get(step_id or "", []),
            key_config=key_config,
            missing_fields=missing_fields,
            config_complete=not missing_fields,
        ))
    return summaries


def _email_step_summaries(
    elements: list[dict],
    links: list[dict],
    outcome_by_link: dict[str, str],
    questions: dict | None = None,
) -> list[EmailStepSummary]:
    incoming_by_step: dict[str, list[EmailStepIncoming]] = {}
    for link in links:
        to_step = str(link.get("toElement")) if link.get("toElement") is not None else ""
        if not to_step:
            continue
        link_id = str(link.get("link_id")) if link.get("link_id") is not None else None
        incoming_by_step.setdefault(to_step, []).append(EmailStepIncoming(
            link_id=link_id,
            from_step=str(link.get("fromElement")) if link.get("fromElement") is not None else None,
            outcome=outcome_by_link.get(link_id or ""),
        ))

    summaries: list[EmailStepSummary] = []
    for element in elements:
        if element.get("type") not in ("workflow_send_email", "workflow_reminder_email"):
            continue
        step_id = str(element.get("element_id")) if element.get("element_id") is not None else None
        recipients = [
            _display_field_tokens(recipient, questions)
            for recipient in _recipient_values(element.get("to"))
            if recipient
        ]
        subject = _display_field_tokens(element.get("subject"), questions) or None
        body = _display_field_tokens(
            element.get("content") or element.get("body") or element.get("message"),
            questions,
        )
        missing_fields: list[str] = []
        if not recipients:
            missing_fields.append("to")
        if not subject:
            missing_fields.append("subject")
        if not body:
            missing_fields.append("content")
        summaries.append(EmailStepSummary(
            step_id=step_id,
            label=element.get("name") or schema_registry.default_label(element.get("type")),
            to=recipients,
            subject=subject,
            content_present=bool(body),
            content_excerpt=body[:180] if body else None,
            missing_fields=missing_fields,
            incoming=incoming_by_step.get(step_id or "", []),
        ))
    return summaries


def _incomplete_email_step_warnings(email_steps: list[EmailStepSummary]) -> list[str]:
    warnings: list[str] = []
    for email in email_steps:
        if not email.missing_fields:
            continue
        label = email.label or f"step {email.step_id}"
        warnings.append(
            f"Email step {email.step_id} ({label}) is incomplete: missing "
            f"{', '.join(email.missing_fields)}. Do not treat it as satisfying "
            "a requested email/survey/notification."
        )
    return warnings


def _incomplete_step_warnings(step_states: list[StepStateSummary]) -> list[str]:
    warnings: list[str] = []
    for step in step_states:
        if not step.missing_fields:
            continue
        label = step.label or f"step {step.step_id}"
        warnings.append(
            f"Step {step.step_id} ({label}, {step.type}) is incomplete: missing "
            f"{', '.join(step.missing_fields)}. Do not treat it as satisfying "
            "a requested workflow action."
        )
    return warnings


def read_workflow_list(client: JotformClient, *, limit: int = 50, offset: int = 0) -> WorkflowList:
    """Return the authoritative workflow list used by tools and the MCP UI."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    try:
        workflows = client.list_workflows(limit=limit, offset=offset)
    except TypeError:
        workflows = client.list_workflows()
    except JotformAPIError as e:
        return WorkflowList(limit=limit, offset=offset, error=str(e))

    summaries = [
        WorkflowSummary(
            workflow_id=w.get("id"),
            workflow_url=_workflow_url(w.get("id")),
            title=w.get("title"),
            status=w.get("status"),
            updated_at=w.get("updated_at"),
            run_count=(w.get("instance_summary") or {}).get("total"),
        )
        for w in workflows
    ]
    has_more = len(summaries) == limit
    return WorkflowList(
        workflows=summaries,
        limit=limit,
        offset=offset,
        count=len(summaries),
        has_more=has_more,
        next_offset=offset + len(summaries) if has_more else None,
    )


def _workflow_detail_from_combined(
    workflow_id: str,
    combined: dict,
    *,
    trigger_form_fields: list[FormField] | None = None,
    trigger_form_questions: dict | None = None,
) -> WorkflowDetail:
    """Build the compact tool view from an already-read Workflow snapshot."""
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
    if diagnostics["unlabelled_branching_steps"] or diagnostics["invalid_branch_links"]:
        diagnostics["note"] = (
            "Branch links must be represented both in links[] and in the "
            "source element's outcomes[].linkID. These entries do not match."
        )
    question_context = _collect_question_context(elements, trigger_form_questions)
    email_steps = _email_step_summaries(elements, links, outcome_by_link, question_context)
    step_states = _step_state_summaries(elements, links, outcome_by_link, question_context)
    incomplete_email_warnings = _incomplete_email_step_warnings(email_steps)
    if incomplete_email_warnings:
        diagnostics["incomplete_email_steps"] = incomplete_email_warnings
    incomplete_step_warnings = _incomplete_step_warnings(step_states)
    if incomplete_step_warnings:
        diagnostics["incomplete_steps"] = incomplete_step_warnings

    resolved_workflow_id = str(wf.get("id")) if wf.get("id") is not None else workflow_id
    return WorkflowDetail(
        workflow_id=resolved_workflow_id,
        workflow_url=_workflow_url(resolved_workflow_id),
        title=wf.get("title"),
        status=wf.get("status"),
        publish_status=wf.get("publishStatus"),
        revision_id=workflow_revision_id(combined),
        updated_at=workflow_updated_at(combined),
        steps=steps,
        connections=connections,
        step_states=step_states,
        email_steps=email_steps,
        trigger_form_fields=trigger_form_fields or [],
        health=WorkflowHealth(
            **health_raw,
            unknown_types=unknown_types,
            unconnected_branches=unconnected_branches,
            invalid_branch_links=diagnostics["invalid_branch_links"],
            unlabelled_branching_steps=diagnostics["unlabelled_branching_steps"],
        ),
        diagnostics=diagnostics,
    )


def read_workflow_detail(client: JotformClient, workflow_id: str) -> WorkflowDetail:
    """Read and analyse one workflow without trusting model-provided state."""
    try:
        combined = client.get_workflow_combined(workflow_id)
    except JotformAPIError as e:
        return WorkflowDetail(
            workflow_id=workflow_id,
            workflow_url=_workflow_url(workflow_id),
            error=str(e),
        )

    sync_state.remember_workflow_snapshot(workflow_id, combined)

    elements = [el for el in (combined.get("elements") or []) if isinstance(el, dict)]
    trigger_form_fields: list[FormField] = []
    trigger_form_questions: dict = {}
    warnings: list[str] = []
    trigger_form_id = workflow_inspector.trigger_form_id(elements)
    if trigger_form_id:
        try:
            trigger_form_questions = client.get_form_questions(trigger_form_id)
            trigger_form_fields = form_fields_from_questions(trigger_form_questions)
        except JotformAPIError as error:
            trigger_form_fields = []
            trigger_form_questions = {}
            warnings.append(
                f"Could not read trigger form {trigger_form_id} fields: {error}. "
                "Do not assume the form has no fields."
            )

    detail = _workflow_detail_from_combined(
        workflow_id,
        combined,
        trigger_form_fields=trigger_form_fields,
        trigger_form_questions=trigger_form_questions,
    )
    detail.warnings.extend(warnings)
    return detail


def _form_resource_ids(elements: list[dict]) -> set[str]:
    """Find form ids referenced by nodes without treating other resources as forms."""
    form_ids: set[str] = set()
    for element in elements:
        resource_type = str(element.get("resourceType") or "").upper()
        element_type = element.get("type")
        if element_type == "workflow_start_point" and workflow_inspector.is_schedule_start_point(element):
            continue
        candidate = element.get("formID") or element.get("resourceID")
        if candidate and (
            resource_type == "FORM"
            or element_type == "workflow_assign_form"
            or element.get("formID")
        ):
            form_ids.add(str(candidate))
    return form_ids


def _enrich_form_resources(
    client: JotformClient,
    elements: list[dict],
) -> tuple[list[dict], list[str]]:
    """Attach the same form metadata used by Workflow's standalone preview."""
    resources: dict[str, dict] = {}
    warnings: list[str] = []

    for form_id in sorted(_form_resource_ids(elements)):
        form: dict = {}
        questions: dict = {}
        try:
            raw_form = client.get_form(form_id)
            form = raw_form if isinstance(raw_form, dict) else {}
        except JotformAPIError as error:
            warnings.append(f"Could not load form {form_id} metadata: {error}")
        try:
            raw_questions = client.get_form_questions(form_id)
            questions = raw_questions if isinstance(raw_questions, dict) else {}
        except JotformAPIError as error:
            warnings.append(f"Could not load form {form_id} fields: {error}")

        resources[form_id] = {
            **form,
            "id": str(form.get("id") or form_id),
            "questions": questions,
        }

    enriched: list[dict] = []
    for raw_element in elements:
        element = deepcopy(raw_element)
        candidate = element.get("formID") or element.get("resourceID")
        form_id = str(candidate) if candidate is not None else None
        if form_id in resources:
            element["resourceObject"] = resources[form_id]
        enriched.append(element)
    return enriched, warnings


def read_workflow_preview(client: JotformClient, workflow_id: str) -> WorkflowPreviewData:
    """Read the complete, UI-only snapshot consumed by Workflow's native canvas."""
    try:
        combined = client.get_workflow_combined(workflow_id, fetch_essential=False)
    except JotformAPIError as error:
        return WorkflowPreviewData(
            workflow_id=workflow_id,
            workflow_url=_workflow_url(workflow_id),
            error=str(error),
        )

    sync_state.remember_workflow_snapshot(workflow_id, combined)

    detail = _workflow_detail_from_combined(workflow_id, combined)
    raw_elements = [
        element for element in (combined.get("elements") or [])
        if isinstance(element, dict)
    ]
    raw_links = [
        deepcopy(link) for link in (combined.get("links") or [])
        if isinstance(link, dict)
    ]
    elements, warnings = _enrich_form_resources(client, raw_elements)
    outcome_by_link, _ = _outcome_map(elements)
    question_context = _collect_question_context(elements)
    email_steps = _email_step_summaries(elements, raw_links, outcome_by_link, question_context)
    step_states = _step_state_summaries(elements, raw_links, outcome_by_link, question_context)
    # Missing config remains available deterministically in step_states and
    # email_steps. Do not promote draft/incomplete nodes to proactive warning
    # text: users may intentionally leave them unfinished on the canvas.

    # Native Workflow normally resolves edge labels from outcomes[].linkID.
    # Keeping the verified label on the link as well gives the read-only UI a
    # safe fallback for older or uncommon node renderers.
    for link in raw_links:
        link_id = str(link.get("link_id")) if link.get("link_id") is not None else ""
        if not link.get("labels") and outcome_by_link.get(link_id):
            link["labels"] = [{"label": outcome_by_link[link_id]}]

    known_element_ids = [
        str(element["element_id"])
        for element in elements
        if element.get("element_id") is not None
        and schema_registry.is_known_type(element.get("type"))
    ]

    return WorkflowPreviewData(
        workflow_id=detail.workflow_id,
        workflow_url=detail.workflow_url,
        title=detail.title,
        status=detail.status,
        publish_status=detail.publish_status,
        revision_id=detail.revision_id,
        updated_at=detail.updated_at,
        elements=elements,
        links=raw_links,
        step_states=step_states,
        email_steps=email_steps,
        known_element_ids=known_element_ids,
        health=detail.health,
        diagnostics=detail.diagnostics,
        warnings=warnings,
    )


def read_step_detail(client: JotformClient, workflow_id: str, step_id: str) -> StepDetail:
    """Read one step's complete persisted configuration."""
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


for _traced_helper_name in (
    "form_fields_from_questions",
    "_hydrate_elements_for_inspection",
    "_outcome_map",
    "_collect_question_context",
    "_display_field_tokens",
    "_step_key_config_and_missing",
    "_step_state_summaries",
    "_email_step_summaries",
    "_incomplete_email_step_warnings",
    "_incomplete_step_warnings",
    "read_workflow_list",
    "_workflow_detail_from_combined",
    "read_workflow_detail",
    "_form_resource_ids",
    "_enrich_form_resources",
    "read_workflow_preview",
    "read_step_detail",
):
    globals()[_traced_helper_name] = audit_log.trace_function(globals()[_traced_helper_name])


def register(mcp: MCPServer, client: JotformClient) -> None:
    @mcp.tool()
    def list_workflows(
        limit: Annotated[int, Field(description="Page size, 1-100. Default 50.")] = 50,
        offset: Annotated[int, Field(description="Zero-based page offset. Default 0.")] = 0,
    ) -> WorkflowList:
        """
        List one page of the user's workflows.

        Returns each workflow's id, title, status, when it was last updated,
        and run_count — how many times it has actually run, which is the
        quickest way to tell a live workflow from an abandoned draft.

        Use the id with get_workflow to see the steps.
        """
        return read_workflow_list(client, limit=limit, offset=offset)

    @mcp.tool()
    def get_workflow(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
    ) -> WorkflowDetail:
        """
        Get an existing workflow's structure: its steps, connections, and generic step state summaries.

        Use this before every mutation of an existing workflow to obtain fresh
        numeric step/link IDs and revision_id. It is also the compact read tool
        when the user asks to inspect a workflow. After a successful bulk write,
        call show_workflow directly instead of reading again.

        Each connection carries an `outcome` when it leaves a branching step:
        TRUE or FALSE on an if/else, the branch name on a conditional
        branch, or the button/outcome text on an approval/task. That is how
        you tell two paths apart. A split's paths are equivalent and carry
        no outcome.

        `step_states` is the generic exact-match source for deciding whether a
        requested workflow action already exists. If a step's config_complete is
        false or missing_fields is not empty, do not treat it as completed. This
        is not permission to repair, connect, or delete that step unless the user
        explicitly requested that exact change.
        `email_steps` is an additional compact view for email/survey/notification
        requests.

        Steps are summaries only — use get_step_details for one non-email step's
        full configuration.
        """
        return read_workflow_detail(client, workflow_id)

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
        return read_step_detail(client, workflow_id, step_id)

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
        Diagnostic gap analysis tool for existing workflows.

        This catches empty links, unwired branch outcomes, missing assignees,
        missing email/task content, and condition fields that are not real
        fields on the trigger form.
        Use ONLY when the user explicitly asks for gap analysis, validation,
        or diagnostics. Do NOT call this during normal workflow creation or
        before show_workflow.
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
                    name=q.get("name") or str(qid),
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
    def list_forms(
        limit: Annotated[int, Field(description="Page size, 1-100. Default 50.")] = 50,
        offset: Annotated[int, Field(description="Zero-based page offset. Default 0.")] = 0,
        status: Annotated[str, Field(description="Optional exact form status filter.")] = "",
    ) -> FormList:
        """
        List one page of the user's forms, with each form's id, title, status and
        submission count.

        Use this when the user wants to browse forms or explicitly wants to
        build a workflow from an existing form. For new AI-generated workflow
        drafts, use search_workflow_templates first, then create_form_with_ai
        when a form is needed.
        """
        try:
            limit = max(1, min(limit, 100))
            offset = max(0, offset)
            forms = client.list_forms(status=status or None, limit=limit, offset=offset)
        except TypeError:
            forms = client.list_forms(status=status or None)
        except JotformAPIError as e:
            return FormList(limit=limit, offset=offset, error=str(e))

        summaries = [
            FormSummary(
                form_id=f.get("id"),
                form_url=_form_url(f.get("id")),
                title=f.get("title"),
                status=f.get("status"),
                submission_count=f.get("count"),
            )
            for f in forms
        ]
        has_more = len(summaries) == limit
        return FormList(
            forms=summaries,
            limit=limit,
            offset=offset,
            count=len(summaries),
            has_more=has_more,
            next_offset=offset + len(summaries) if has_more else None,
        )
