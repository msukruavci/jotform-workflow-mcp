"""
Layer 3: building.

Every tool here follows the same shape: fetch current state (never trust a
cached id or position from earlier in the conversation), ask tree_builder
what to send, write it, report what happened in terms the model can act on.

None of these tools accept x/y, port names, or link `type` from the model —
those are either server-computed (ports), constant (link type — see
tree_builder for why a typo there is dangerous), or not yet solved
(layout; see docs/gap-report.md item 5). What a step *is* is the model's
job; where it sits on the canvas is ours.
"""
from __future__ import annotations

import html
import re
from typing import Annotated
from uuid import uuid4

from mcp.server import MCPServer
from pydantic import Field

from mcp_server import revision_log, schema_registry, tree_builder as tb, workflow_inspector
from mcp_server.jotform_client import JotformAPIError, JotformClient
from mcp_server.models import (
    AddStepResult, BuildWorkflowBulkResult, ConnectStepsResult, ConnectionSpec,
    CreateAIFormResult, CreateWorkflowResult,
    CreateWorkflowWithAIFormResult,
    DisconnectStepsResult, StepSpec, UpdateStepResult,
)


def _workflow_url(workflow_id: str | None) -> str | None:
    return f"https://www.jotform.com/workflow/{workflow_id}/build" if workflow_id else None


def _form_url(form_id: str | None) -> str | None:
    return f"https://www.jotform.com/build/{form_id}" if form_id else None


INTENT_FIELD = Field(
    description=(
        "Optional short, privacy-conscious summary of the user's intent for "
        "audit/debug logs. Do not copy the full user message; keep one phrase."
    )
)
REASON_FIELD = Field(
    description=(
        "Optional short explanation of why this tool call is the right next "
        "step. Used for audit/debug logs and revision history."
    )
)


def _revision_reason(default: str, intent: str = "", reason: str = "") -> str:
    details = []
    if intent:
        details.append(f"intent={intent}")
    if reason:
        details.append(f"reason={reason}")
    return f"{default} ({'; '.join(details)})" if details else default


def _norm_text(value) -> str:
    return str(value or "").strip().lower()


def _find_duplicate_step(elements: list[dict], step_type: str, config: dict) -> dict | None:
    """
    Conservative duplicate check.

    We only block when the model supplies a human-facing name that already
    exists on the same step type, or for emails when subject/content clearly
    match. Legitimate duplicate steps can still be created with
    allow_duplicate=true.
    """
    wanted_name = _norm_text(config.get("name"))
    wanted_subject = _norm_text(config.get("subject"))
    wanted_content = _norm_text(config.get("content"))

    for element in elements:
        if not isinstance(element, dict) or element.get("type") != step_type:
            continue
        if wanted_name and _norm_text(element.get("name")) == wanted_name:
            return element
        if (
            step_type in ("workflow_send_email", "workflow_reminder_email")
            and wanted_subject
            and wanted_content
            and _norm_text(element.get("subject")) == wanted_subject
            and _norm_text(element.get("content")) == wanted_content
        ):
            return element
    return None


REQUIRED_STEP_DETAILS: dict[str, dict[str, str]] = {
    "workflow_assign_task": {
        "assignee": "who the task should be assigned to",
        "taskDescription": "what the assignee should do",
    },
    "workflow_approval": {
        "approver": "who should approve or deny it",
        "taskDescription": "what the approver is deciding",
    },
    "workflow_assign": {
        "assignee": "who the submission should be assigned to",
    },
    "workflow_assign_form": {
        "assignee": "who should receive the assigned form",
        "formID": "which form should be assigned",
    },
    "workflow_send_email": {
        "to": "who should receive the email",
        "subject": "the email subject",
        "content": "the email body",
    },
    "workflow_reminder_email": {
        "to": "who should receive the reminder",
        "timing": "when the reminder should be sent",
    },
    "workflow_sign_document": {
        "documentID": "which Sign document should be used",
        "signerMapping": "who should sign the document",
    },
    "workflow_binary_decision": {
        "conditionTerms": "the condition to test",
    },
    "workflow_conditional_branch": {
        "outcomes": "branch names and their conditions",
    },
}


ASSIGNEE_FIELDS_BY_STEP_TYPE: dict[str, tuple[str, ...]] = {
    "workflow_assign_task": ("assignee",),
    "workflow_assign": ("assignee",),
    "workflow_assign_form": ("assignee",),
    "workflow_approval": ("approver",),
}


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _missing_required_step_details(step_type: str, config: dict) -> list[str]:
    required = REQUIRED_STEP_DETAILS.get(step_type, {})
    return [
        f"{field} ({reason})"
        for field, reason in required.items()
        if not _has_value(config.get(field))
    ]


def _invalid_condition_field_message(
    client: JotformClient,
    workflow_id: str,
    step_type: str,
    config: dict,
) -> tuple[str | None, str | None]:
    if not workflow_inspector.extract_condition_terms(step_type, config):
        return None, None

    try:
        combined = client.get_workflow_combined(workflow_id)
    except JotformAPIError as e:
        return f"Could not verify condition form fields before writing: {e}", None

    elements = [e for e in (combined.get("elements") or []) if isinstance(e, dict)]
    trigger_form_id = workflow_inspector.trigger_form_id(elements)
    if not trigger_form_id:
        return (
            "This workflow has no trigger form, so condition fields cannot be verified.",
            "Bind a trigger form first, then call get_form_fields and use a real field_id.",
        )

    try:
        questions = client.get_form_questions(trigger_form_id)
    except JotformAPIError as e:
        return f"Could not read trigger form fields before writing: {e}", None

    invalid = workflow_inspector.invalid_field_references(
        config, step_type, {str(qid) for qid in questions}
    )
    if not invalid:
        return None, None

    available = [
        f"{qid}: {q.get('text')}"
        for qid, q in questions.items()
        if isinstance(q, dict)
    ]
    return (
        "Condition fields must be real field_id values from the trigger form; "
        f"invalid references: {', '.join(invalid)}.",
        (
            "Call get_form_fields or inspect_workflow_gaps, ask the user which "
            f"field to use, then retry. Available fields: {available}"
        ),
    )


def _trigger_form_questions(client: JotformClient, workflow_id: str) -> tuple[str | None, dict, str | None]:
    try:
        combined = client.get_workflow_combined(workflow_id)
    except JotformAPIError as e:
        return None, {}, f"Could not read workflow trigger form: {e}"

    elements = [e for e in (combined.get("elements") or []) if isinstance(e, dict)]
    trigger_form_id = workflow_inspector.trigger_form_id(elements)
    if not trigger_form_id:
        return None, {}, "Workflow has no trigger form."

    try:
        return trigger_form_id, client.get_form_questions(trigger_form_id), None
    except JotformAPIError as e:
        return trigger_form_id, {}, f"Could not read trigger form fields: {e}"


def _email_field_reference(question_id: str, question: dict, form_title: str | None = None) -> dict:
    return {
        "id": str(uuid4()),
        "value": "{" + str(question.get("name") or question_id) + "}",
        "text": question.get("text") or question_id,
        "isValid": True,
        "isQuestion": True,
        "style": {"backgroundColor": "#007862", "--pillColor": "#007862"},
        "isBright": False,
        "formTitle": form_title or "Form",
    }


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _fixed_email_reference(email: str) -> dict:
    return {
        "id": str(uuid4()),
        "value": email,
        "text": email,
        "isValid": True,
        "isQuestion": False,
    }


def _question_name_by_token(questions: dict, token: str) -> str | None:
    wanted = token.strip().lower()
    for qid, question in questions.items():
        if not isinstance(question, dict):
            continue
        candidates = {
            str(qid).strip().lower(),
            str(question.get("qid") or "").strip().lower(),
            str(question.get("name") or "").strip().lower(),
            str(question.get("text") or "").strip().lower(),
        }
        if wanted in candidates:
            return str(question.get("name") or qid)
    return None


def _normalize_content_field_tokens(content: str, questions: dict) -> tuple[str, bool]:
    changed = False

    def replace(match: re.Match) -> str:
        nonlocal changed
        token = match.group(1)
        if token in {"id", "form_title"} or token.startswith("q"):
            return match.group(0)
        question_name = _question_name_by_token(questions, token)
        if not question_name:
            return match.group(0)
        changed = True
        return "{" + question_name + "}"

    return re.sub(r"\{([^{}]+)\}", replace, content), changed


def _normalize_assignee_fields(
    client: JotformClient,
    workflow_id: str,
    config: dict,
    fields: tuple[str, ...],
) -> tuple[dict, str | None, str | None]:
    if not any(field in config for field in fields):
        return config, None, None

    trigger_form_id, questions, trigger_error = _trigger_form_questions(client, workflow_id)
    email_questions = {
        str(qid): q for qid, q in questions.items()
        if isinstance(q, dict) and q.get("type") == "control_email"
    }
    by_label = {
        str(q.get("text", "")).strip().lower(): (qid, q)
        for qid, q in email_questions.items()
        if q.get("text")
    }
    form_title = None
    for question in questions.values():
        if isinstance(question, dict) and question.get("type") == "control_head":
            form_title = question.get("text")
            break

    normalized = dict(config)
    changed = False
    for field in fields:
        value = normalized.get(field)
        items = value if isinstance(value, list) else [value]
        next_items = []
        for item in items:
            if isinstance(item, dict) and item.get("isQuestion") is True:
                next_items.append(item)
                continue

            raw = ""
            if isinstance(item, dict):
                raw = str(item.get("value") or item.get("text") or item.get("id") or "").strip()
            elif isinstance(item, str):
                raw = item.strip()

            if not raw:
                next_items.append(item)
                continue

            qid = str((item or {}).get("id") if isinstance(item, dict) else raw)
            question = email_questions.get(qid)
            if question is None and isinstance(item, dict):
                label = str(item.get("text") or item.get("value") or "").strip().lower()
                match = by_label.get(label)
                if match:
                    qid, question = match
            if question is not None:
                next_items.append(_email_field_reference(qid, question, form_title))
                changed = True
                continue

            if EMAIL_RE.match(raw):
                next_items.append(_fixed_email_reference(raw))
                changed = True
                continue

            if trigger_error and not questions:
                return normalized, None, trigger_error
            return normalized, None, (
                f"{field} must be a valid email address or a real email field "
                f"from the trigger form; got {raw!r}."
            )
        normalized[field] = next_items

    hint = (
        f"Normalized assignee/approver fields using builder recipient shape"
        f"{' and trigger form ' + trigger_form_id if trigger_form_id else ''}."
        if changed else None
    )
    return normalized, hint, None


EMAIL_MODAL_DEFAULTS = {
    "attachment": {"name": "", "url": "", "type": ""},
    "senderEmail": "noreply@jotform.com",
    "hideEmptyFields": "1",
    "uploadAttachment": "0",
    "recipientLimit": 10,
    "cc": [],
    "bcc": [],
    "replyTo": [],
    "showCcField": False,
    "showBccField": False,
    "pdfattachment": "0",
    "passwordEnabled": "0",
    "pdfId": "",
    "pdfPassword": "",
    "isRecipientExpanded": True,
    "isDirty": "Yes",
}


def _html_email_content(content: str) -> str:
    stripped = content.strip()
    if "<html" in stripped.lower() or "<body" in stripped.lower():
        return content
    paragraphs = [
        f"<p>{html.escape(part).replace(chr(10), '<br />')}</p>"
        for part in stripped.split("\n\n")
        if part.strip()
    ]
    body = "\n".join(paragraphs) or "<p></p>"
    return "<!DOCTYPE html>\n<html>\n<head>\n</head>\n<body>\n" + body + "\n</body>\n</html>"


def _normalize_email_config(
    client: JotformClient,
    workflow_id: str,
    config: dict,
) -> tuple[dict, str | None, str | None]:
    normalized = {**EMAIL_MODAL_DEFAULTS, **config}
    hint_parts = []
    if isinstance(normalized.get("content"), str) and normalized["content"].strip():
        html_content = _html_email_content(normalized["content"])
        if html_content != normalized["content"]:
            normalized["content"] = html_content
            hint_parts.append("wrapped plain text email content as HTML")
        trigger_form_id, questions, _ = _trigger_form_questions(client, workflow_id)
        if questions:
            normalized_content, changed = _normalize_content_field_tokens(
                normalized["content"], questions
            )
            if changed:
                normalized["content"] = normalized_content
                hint_parts.append(
                    f"normalized email content field tokens from trigger form {trigger_form_id}"
                )

    normalized, recipient_hint, recipient_error = _normalize_email_recipients(
        client, workflow_id, normalized
    )
    if recipient_error:
        return normalized, None, recipient_error
    if recipient_hint:
        hint_parts.append(recipient_hint)

    return normalized, "; ".join(hint_parts) if hint_parts else None, None


def _normalize_email_recipients(
    client: JotformClient,
    workflow_id: str,
    config: dict,
) -> tuple[dict, str | None, str | None]:
    recipient_fields = ("to", "replyTo", "cc", "bcc")
    if not any(field in config for field in recipient_fields):
        return config, None, None

    trigger_form_id, questions, error = _trigger_form_questions(client, workflow_id)
    if error:
        return config, None, error

    email_questions = {
        str(qid): q for qid, q in questions.items()
        if isinstance(q, dict) and q.get("type") == "control_email"
    }
    by_label = {
        str(q.get("text", "")).strip().lower(): (qid, q)
        for qid, q in email_questions.items()
        if q.get("text")
    }
    form_title = None
    for question in questions.values():
        if isinstance(question, dict) and question.get("type") == "control_head":
            form_title = question.get("text")
            break

    normalized = dict(config)
    changed = False
    for field in recipient_fields:
        recipients = normalized.get(field)
        if not isinstance(recipients, list):
            continue
        next_recipients = []
        for item in recipients:
            if not isinstance(item, dict) or item.get("isQuestion") is True:
                next_recipients.append(item)
                continue
            question = None
            qid = str(item.get("id") or "")
            if qid in email_questions:
                question = email_questions[qid]
            else:
                label = str(item.get("text") or item.get("value") or "").strip().lower()
                match = by_label.get(label)
                if match:
                    qid, question = match
            if question is None:
                next_recipients.append(item)
                continue
            next_recipients.append(_email_field_reference(qid, question, form_title))
            changed = True
        normalized[field] = next_recipients

    hint = (
        f"Normalized recipient field references from trigger form {trigger_form_id}."
        if changed else None
    )
    return normalized, hint, None


def _merge_outcome_updates(current: dict, config: dict) -> dict:
    if "outcomes" not in config or not isinstance(config.get("outcomes"), list):
        return config

    current_outcomes = [
        outcome for outcome in (current.get("outcomes") or [])
        if isinstance(outcome, dict)
    ]

    def keys(outcome: dict) -> list[tuple[str, str]]:
        result = []
        for key in ("outcomeID", "id"):
            if outcome.get(key) is not None:
                result.append((key, str(outcome.get(key))))
        label = tb.outcome_label(outcome)
        if label:
            result.append(("label", label.strip().lower()))
        return result

    by_key = {
        key: outcome
        for outcome in current_outcomes
        for key in keys(outcome)
    }

    merged = []
    for outcome in config["outcomes"]:
        if not isinstance(outcome, dict):
            merged.append(outcome)
            continue
        current_match = next((by_key.get(key) for key in keys(outcome) if by_key.get(key)), None)
        if current_match is None:
            merged.append(outcome)
            continue
        preserved = {**current_match, **outcome}
        if "linkID" not in outcome and current_match.get("linkID"):
            preserved["linkID"] = current_match.get("linkID")
        merged.append(preserved)

    return {**config, "outcomes": merged}


def _extract_ai_form_id(content: dict) -> str | None:
    for key in ("resource_id", "form_id", "formID", "id"):
        value = content.get(key)
        if value is not None:
            return str(value)
    messages = content.get("messages") or []
    for message in messages:
        if isinstance(message, dict):
            value = message.get("form_id") or message.get("resource_id")
            if value is not None:
                return str(value)
    return None


def _bind_and_verify_trigger(
    client: JotformClient,
    workflow_id: str,
    trigger_form_id: str,
    title: str,
) -> CreateWorkflowResult | None:
    try:
        client.set_trigger_form(workflow_id, trigger_form_id)
    except JotformAPIError as e:
        return CreateWorkflowResult(
            workflow_id=str(workflow_id), title=title,
            workflow_url=_workflow_url(str(workflow_id)),
            trigger_form_id=trigger_form_id,
            trigger_form_url=_form_url(trigger_form_id),
            error=f"Workflow created, but setting trigger form failed: {e}",
        )

    try:
        start = client.get_element(workflow_id, 1)
    except JotformAPIError as e:
        return CreateWorkflowResult(
            workflow_id=str(workflow_id), title=title,
            workflow_url=_workflow_url(str(workflow_id)),
            trigger_form_id=trigger_form_id,
            trigger_form_url=_form_url(trigger_form_id),
            error=f"Workflow created, trigger form set, but could not verify: {e}",
        )

    if str(start.get("resourceID")) != str(trigger_form_id):
        return CreateWorkflowResult(
            workflow_id=str(workflow_id), title=title,
            workflow_url=_workflow_url(str(workflow_id)),
            trigger_form_id=trigger_form_id,
            trigger_form_url=_form_url(trigger_form_id),
            error=(
                "Workflow created, but the trigger form binding could "
                "not be verified — the start point doesn't show this "
                "form id after the write. Tell the user to check the "
                "trigger form in the Jotform builder (Settings -> "
                "trigger form) and set it manually if it's missing."
            ),
        )

    return None


def register(mcp: MCPServer, client: JotformClient) -> None:
    @mcp.tool()
    def create_form_with_ai(
        prompt: Annotated[str, Field(
            description=(
                "Natural-language description of the form to create. Ask the "
                "user what fields, labels, language, and purpose they want "
                "before calling this. The created form can be passed to "
                "create_workflow as trigger_form_id."
            )
        )],
        form_type: Annotated[str, Field(
            description='Form type preference for Jotform AI. Default "classic".'
        )] = "classic",
        language: Annotated[str, Field(
            description='Form language preference. Default "en"; use "tr" when the user wants Turkish.'
        )] = "en",
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
    ) -> CreateAIFormResult:
        """
        Create a new Jotform form from an AI prompt.

        Use this only after the user chooses "create a new form" for a
        workflow trigger and describes what the form should collect.
        """
        try:
            content = client.create_form_with_ai(prompt, form_type=form_type, language=language)
        except JotformAPIError as e:
            return CreateAIFormResult(error=str(e))

        form_id = _extract_ai_form_id(content)
        if not form_id:
            return CreateAIFormResult(error=f"No form id in AI form response: {content!r}")

        questions = content.get("questions") if isinstance(content.get("questions"), dict) else {}
        title = (questions.get("1") or {}).get("text") if isinstance(questions.get("1"), dict) else None
        return CreateAIFormResult(
            form_id=form_id,
            form_url=_form_url(form_id),
            title=title,
            summary=content.get("summary"),
            questions=questions,
        )

    @mcp.tool()
    def create_workflow(
        title: Annotated[str, Field(description="Workflow name.")],
        trigger_form_id: Annotated[str, Field(
            description=(
                "Required unless allow_without_trigger=true — the form (from "
                "list_forms) whose submissions should trigger this workflow. "
                "Before calling create_workflow, decide the form strategy "
                "with the user: existing form or new AI-created form. For an "
                "existing form, call list_forms first and use the selected id "
                "here. For a complete new workflow, prefer build_workflow_bulk "
                "with form_prompt/title instead of this low-level tool. "
                "Binding takes two API calls under the "
                "hood and is verified by reading the start point back."
            )
        )] = "",
        allow_without_trigger: Annotated[bool, Field(
            description=(
                "Default false. Set true only if the user explicitly asks for "
                "a draft workflow with no trigger form. Normal workflows must "
                "have a trigger form."
            )
        )] = False,
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
    ) -> CreateWorkflowResult:
        """
        Low-level helper to create a new workflow with a trigger form.

        For ordinary new-workflow requests, prefer build_workflow_bulk with
        title/form_prompt or title/trigger_form_id so creation and step wiring
        happen in one tool call. Use this tool only for manual/partial setup.

        1. Use an existing form — then call list_forms and pass the chosen
           form id as trigger_form_id.
        2. Create a new form first — prefer build_workflow_bulk with form_prompt.
        3. No trigger form yet — only proceed with allow_without_trigger=true
           if the user explicitly asks for a draft workflow without one.

        Returns the new workflow_id. ALWAYS use build_workflow_bulk to add
        all steps and connections in one atomic shot. DO NOT call add_step in a loop.
        """
        if not trigger_form_id and not allow_without_trigger:
            return CreateWorkflowResult(
                error=(
                    "A workflow needs a trigger form. Before creating it, ask "
                    "the user whether to use an existing form or create a new "
                    "AI form. Existing form: call list_forms, then pass the "
                    "chosen form id as trigger_form_id. New form: use "
                    "create_workflow_with_ai_form. Only use "
                    "allow_without_trigger=true if the user explicitly asks "
                    "for a formsuz/no-trigger draft."
                ),
            )

        try:
            created = client.create_workflow(title)
        except JotformAPIError as e:
            return CreateWorkflowResult(error=str(e))

        workflow_id = created.get("id") or created.get("workflowID")
        if not workflow_id:
            return CreateWorkflowResult(error=f"No workflow id in response: {created!r}")

        if trigger_form_id:
            error = _bind_and_verify_trigger(client, str(workflow_id), trigger_form_id, title)
            if error is not None:
                return error

        return CreateWorkflowResult(
            workflow_id=str(workflow_id), title=title,
            workflow_url=_workflow_url(str(workflow_id)),
            trigger_form_id=trigger_form_id or None,
            trigger_form_url=_form_url(trigger_form_id or None),
        )

    @mcp.tool()
    def create_workflow_with_ai_form(
        title: Annotated[str, Field(description="Workflow name.")],
        form_prompt: Annotated[str, Field(
            description=(
                "Natural-language description of the new trigger form to "
                "create before creating the workflow. Include the form's "
                "purpose and desired fields. Default language is English; "
                "mention Turkish only if the user asks for it."
            )
        )],
        form_type: Annotated[str, Field(
            description='Form type preference for Jotform AI. Default "classic".'
        )] = "classic",
        language: Annotated[str, Field(
            description='Form language preference. Default "en"; use "tr" when the user wants Turkish.'
        )] = "en",
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
    ) -> CreateWorkflowWithAIFormResult:
        """
        Low-level helper to create a new AI-generated form, then a workflow triggered by it.

        For complete new workflows, prefer a single build_workflow_bulk call
        with title, form_prompt, steps, and connections. Use this helper only
        when the user explicitly wants form/workflow creation without steps yet.
        """
        try:
            form_content = client.create_form_with_ai(
                form_prompt, form_type=form_type, language=language
            )
        except JotformAPIError as e:
            return CreateWorkflowWithAIFormResult(error=f"Creating AI form failed: {e}")

        form_id = _extract_ai_form_id(form_content)
        if not form_id:
            return CreateWorkflowWithAIFormResult(
                error=f"No form id in AI form response: {form_content!r}"
            )

        try:
            created = client.create_workflow(title)
        except JotformAPIError as e:
            return CreateWorkflowWithAIFormResult(
                trigger_form_id=form_id,
                trigger_form_url=_form_url(form_id),
                error=f"AI form created ({form_id}), but workflow creation failed: {e}",
            )

        workflow_id = created.get("id") or created.get("workflowID")
        if not workflow_id:
            return CreateWorkflowWithAIFormResult(
                trigger_form_id=form_id,
                trigger_form_url=_form_url(form_id),
                error=f"AI form created ({form_id}), but no workflow id in response: {created!r}",
            )

        error = _bind_and_verify_trigger(client, str(workflow_id), form_id, title)
        if error is not None:
            return CreateWorkflowWithAIFormResult(
                workflow_id=error.workflow_id,
                workflow_url=error.workflow_url,
                title=error.title,
                trigger_form_id=form_id,
                trigger_form_url=_form_url(form_id),
                error=error.error,
            )

        questions = form_content.get("questions") if isinstance(form_content.get("questions"), dict) else {}
        form_title = (questions.get("1") or {}).get("text") if isinstance(questions.get("1"), dict) else None
        return CreateWorkflowWithAIFormResult(
            workflow_id=str(workflow_id),
            workflow_url=_workflow_url(str(workflow_id)),
            title=title,
            trigger_form_id=form_id,
            trigger_form_url=_form_url(form_id),
            form_title=form_title,
            form_summary=form_content.get("summary"),
            questions=questions,
        )

    @mcp.tool()
    def build_workflow_bulk(
        workflow_id: Annotated[str, Field(
            description=(
                "Optional. ID of an existing workflow to add steps to. If omitted, "
                "a new workflow is created automatically using title/form_prompt."
            )
        )] = "",
        *,
        steps: Annotated[list[StepSpec], Field(
            description=(
                "List of steps to create. Each step has a unique 'ref' name (e.g. 'approval_1', 'notify_mgr', 'reject_email'), "
                "'type' (e.g. 'workflow_approval', 'workflow_send_email', 'workflow_conditional_branch'), and 'config' dict."
            )
        )],
        connections: Annotated[list[ConnectionSpec], Field(
            default=[],
            description=(
                "List of connections between steps. 'from_ref' can be 'start' (or '1') for the trigger form, "
                "or any step 'ref'. 'to_ref' is the target step's 'ref'. 'outcome' is required for branching steps "
                "(e.g. 'Approve', 'Deny', 'TRUE', 'FALSE', or branch name)."
            )
        )] = [],
        title: Annotated[str, Field(
            description="Optional. Name of the workflow when creating a new one."
        )] = "",
        trigger_form_id: Annotated[str, Field(
            description="Optional. Existing form ID to bind as trigger form when creating a new workflow."
        )] = "",
        form_prompt: Annotated[str, Field(
            description="Optional. Natural language description to automatically generate an AI trigger form for this workflow."
        )] = "",
        form_language: Annotated[str, Field(
            description='Form language for AI trigger form generation. Use "tr" for Turkish, otherwise "en".'
        )] = "en",
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
    ) -> BuildWorkflowBulkResult:
        """
        Primary tool for building workflows in one bulk operation.

        Use this tool for both:
        - Creating a new workflow from scratch by passing title/form_prompt or title/trigger_form_id.
        - Adding a complete graph of steps and connections to an existing workflow_id.

        Prefer this over create_workflow_with_ai_form followed by another bulk call for new workflows.
        All step references ('ref') in steps can be wired using 'from_ref' and 'to_ref' in connections.
        Use 'start' or '1' as from_ref to connect from the trigger form.
        """
        workflow_id = str(workflow_id or "").strip()
        title = str(title or "").strip()
        trigger_form_id = str(trigger_form_id or "").strip()
        form_prompt = str(form_prompt or "").strip()
        form_language = str(form_language or "en").strip() or "en"

        if not steps:
            return BuildWorkflowBulkResult(error="No steps provided to build_workflow_bulk.")

        # 1. Check uniqueness of step refs
        step_items: list[tuple[str, str, dict]] = []
        seen_refs: set[str] = set()
        for s in steps:
            s_ref = str(getattr(s, "ref", None) or (s.get("ref") if isinstance(s, dict) else "") or "").strip()
            s_type = str(getattr(s, "type", None) or (s.get("type") if isinstance(s, dict) else "") or "").strip()
            s_config = getattr(s, "config", None) if not isinstance(s, dict) else s.get("config")
            s_config = dict(s_config or {})
            if not s_ref:
                return BuildWorkflowBulkResult(error="Every step in steps must have a non-empty 'ref'.")
            if s_ref in seen_refs:
                return BuildWorkflowBulkResult(error=f"Duplicate step ref '{s_ref}' found in steps list.")
            if s_ref.lower() in ("start", "1"):
                return BuildWorkflowBulkResult(error=f"Step ref '{s_ref}' is reserved for the trigger form start point.")
            seen_refs.add(s_ref)
            step_items.append((s_ref, s_type, s_config))

        # 2. Check connections validity
        conn_items: list[tuple[str, str, str]] = []
        for c in connections or []:
            c_from = str(getattr(c, "from_ref", None) or (c.get("from_ref") if isinstance(c, dict) else "") or "").strip()
            c_to = str(getattr(c, "to_ref", None) or (c.get("to_ref") if isinstance(c, dict) else "") or "").strip()
            c_outcome = str(getattr(c, "outcome", None) or (c.get("outcome") if isinstance(c, dict) else "") or "").strip()
            if c_from not in seen_refs and c_from.lower() not in ("start", "1"):
                return BuildWorkflowBulkResult(
                    error=f"Connection from_ref '{c_from}' is invalid. Must be 'start', '1', or one of: {list(seen_refs)}."
                )
            if c_to not in seen_refs:
                return BuildWorkflowBulkResult(
                    error=f"Connection to_ref '{c_to}' is invalid. Must be one of: {list(seen_refs)}."
                )
            conn_items.append((c_from, c_to, c_outcome))

        warnings: list[str] = []
        clean_configs: dict[str, dict] = {}

        # 3. Validate each step config
        for s_ref, s_type, s_config in step_items:
            try:
                clean_cfg, step_warnings = tb.validate_config(s_type, s_config)
            except tb.ValidationError as e:
                return BuildWorkflowBulkResult(
                    error=f"Step '{s_ref}' ({s_type}) config error: {e}",
                    hint="Call list_step_types to see valid values.",
                )
            for w in step_warnings:
                warnings.append(f"[{s_ref}] {w}")

            missing = _missing_required_step_details(s_type, clean_cfg)
            if missing:
                return BuildWorkflowBulkResult(
                    warnings=warnings,
                    error=f"Step '{s_ref}' ({s_type}) needs more detail before it can be added. Missing: {', '.join(missing)}.",
                    hint="Ask for or provide the essentials (assignee/approver/subject/body/outcomes) before bulk creation."
                )

            clean_configs[s_ref] = clean_cfg

        created_trigger_form_id: str | None = None
        if not workflow_id:
            if not (title or trigger_form_id or form_prompt):
                return BuildWorkflowBulkResult(
                    warnings=warnings,
                    error=(
                        "workflow_id is required for existing workflows. To create "
                        "a workflow from scratch, provide title and either form_prompt "
                        "or trigger_form_id."
                    ),
                    hint="Pass workflow_id, or call build_workflow_bulk with title + form_prompt.",
                )

            form_content: dict = {}
            if form_prompt:
                try:
                    form_content = client.create_form_with_ai(
                        form_prompt, language=form_language
                    )
                except JotformAPIError as e:
                    return BuildWorkflowBulkResult(
                        warnings=warnings,
                        error=f"Creating AI form failed: {e}",
                    )

                created_trigger_form_id = _extract_ai_form_id(form_content)
                if not created_trigger_form_id:
                    return BuildWorkflowBulkResult(
                        warnings=warnings,
                        error=f"No form id in AI form response: {form_content!r}",
                    )
                trigger_form_id = created_trigger_form_id

                questions = form_content.get("questions") if isinstance(form_content.get("questions"), dict) else {}
                form_title = (questions.get("1") or {}).get("text") if isinstance(questions.get("1"), dict) else None
                if not title:
                    title = form_title or "Untitled Workflow"
            elif trigger_form_id:
                created_trigger_form_id = trigger_form_id

            if not title:
                title = "Untitled Workflow"

            try:
                created = client.create_workflow(title)
            except JotformAPIError as e:
                return BuildWorkflowBulkResult(
                    trigger_form_id=trigger_form_id or None,
                    trigger_form_url=_form_url(trigger_form_id or None),
                    warnings=warnings,
                    error=f"Workflow creation failed: {e}",
                )

            workflow_id = str(created.get("id") or created.get("workflowID") or "")
            if not workflow_id:
                return BuildWorkflowBulkResult(
                    trigger_form_id=trigger_form_id or None,
                    trigger_form_url=_form_url(trigger_form_id or None),
                    warnings=warnings,
                    error=f"No workflow id in response: {created!r}",
                )

            if trigger_form_id:
                bind_error = _bind_and_verify_trigger(client, workflow_id, trigger_form_id, title)
                if bind_error is not None:
                    return BuildWorkflowBulkResult(
                        workflow_id=bind_error.workflow_id,
                        workflow_url=bind_error.workflow_url,
                        trigger_form_id=bind_error.trigger_form_id,
                        trigger_form_url=bind_error.trigger_form_url,
                        warnings=warnings,
                        error=bind_error.error,
                    )
        elif trigger_form_id:
            created_trigger_form_id = trigger_form_id
            bind_error = _bind_and_verify_trigger(client, workflow_id, trigger_form_id, title or workflow_id)
            if bind_error is not None:
                return BuildWorkflowBulkResult(
                    workflow_id=bind_error.workflow_id,
                    workflow_url=bind_error.workflow_url,
                    trigger_form_id=bind_error.trigger_form_id,
                    trigger_form_url=bind_error.trigger_form_url,
                    warnings=warnings,
                    error=bind_error.error,
                )

        for s_ref, s_type, _ in step_items:
            clean_cfg = clean_configs[s_ref]

            field_error, field_hint = _invalid_condition_field_message(client, workflow_id, s_type, clean_cfg)
            if field_error:
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    trigger_form_id=created_trigger_form_id,
                    trigger_form_url=_form_url(created_trigger_form_id),
                    warnings=warnings,
                    error=f"Step '{s_ref}': {field_error}",
                    hint=field_hint,
                )

            assignee_fields = ASSIGNEE_FIELDS_BY_STEP_TYPE.get(s_type, ())
            if assignee_fields:
                clean_cfg, assignee_hint, assignee_error = _normalize_assignee_fields(client, workflow_id, clean_cfg, assignee_fields)
                if assignee_error:
                    return BuildWorkflowBulkResult(
                        workflow_id=workflow_id,
                        workflow_url=_workflow_url(workflow_id),
                        trigger_form_id=created_trigger_form_id,
                        trigger_form_url=_form_url(created_trigger_form_id),
                        warnings=warnings,
                        error=f"Step '{s_ref}': {assignee_error}",
                    )
                if assignee_hint:
                    warnings.append(f"[{s_ref}] {assignee_hint}")

            if s_type in ("workflow_send_email", "workflow_reminder_email"):
                clean_cfg, recipient_hint, recipient_error = _normalize_email_config(client, workflow_id, clean_cfg)
                if recipient_error:
                    return BuildWorkflowBulkResult(
                        workflow_id=workflow_id,
                        workflow_url=_workflow_url(workflow_id),
                        trigger_form_id=created_trigger_form_id,
                        trigger_form_url=_form_url(created_trigger_form_id),
                        warnings=warnings,
                        error=f"Step '{s_ref}': {recipient_error}",
                    )
                if recipient_hint:
                    warnings.append(f"[{s_ref}] {recipient_hint}")

            clean_configs[s_ref] = clean_cfg

        # 4. Fetch current workflow elements & links
        try:
            existing_elements = client.get_elements(workflow_id)
            existing_links = client.get_links(workflow_id)
        except JotformAPIError as e:
            return BuildWorkflowBulkResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                trigger_form_id=created_trigger_form_id,
                trigger_form_url=_form_url(created_trigger_form_id),
                error=str(e),
                warnings=warnings,
            )

        start_elem = next((e for e in existing_elements if e.get("type") == "workflow_start_point"), None)
        start_id = start_elem.get("element_id") if start_elem else 1

        existing_elem_ids = [
            int(e.get("element_id"))
            for e in existing_elements
            if str(e.get("element_id", "")).isdigit()
        ]
        curr_elem_id = max(existing_elem_ids, default=1)

        ref_to_id: dict[str, int | str] = {"start": start_id, "1": start_id}
        for s_ref, _, _ in step_items:
            curr_elem_id += 1
            ref_to_id[s_ref] = curr_elem_id

        all_elements = list(existing_elements)
        element_creates: list[dict] = []
        created_data_by_id: dict[int | str, dict] = {}

        layout_positions = tb.compute_layered_dag_positions(
            all_elements,
            [s_ref for s_ref, _, _ in step_items],
            conn_items,
            start_step_id=start_id,
        )

        for s_ref, s_type, _ in step_items:
            eid = ref_to_id[s_ref]
            cfg = clean_configs[s_ref]

            pos = layout_positions.get(s_ref)
            if pos is None:
                incoming_parents = [c[0] for c in conn_items if c[1] == s_ref]
                if len(incoming_parents) > 1:
                    parent_ids = [ref_to_id.get(p) for p in incoming_parents if ref_to_id.get(p) is not None]
                    pos = tb.compute_position(all_elements, parent_ids, branch_offset=0.0)
                elif len(incoming_parents) == 1:
                    pos = tb.compute_position(all_elements, ref_to_id.get(incoming_parents[0]))
                else:
                    pos = tb.compute_position(all_elements, None)

            elem_create = tb.build_element_create(s_type, eid, cfg, pos)
            element_creates.append(elem_create)
            created_data_by_id[eid] = elem_create["data"]
            all_elements.append(elem_create["data"])

        # 5. Build links and wire branching outcomes
        existing_link_ids = [
            int(l.get("link_id"))
            for l in existing_links
            if str(l.get("link_id", "")).isdigit()
        ]
        curr_link_id = max(existing_link_ids, default=0)

        link_creates: list[dict] = []

        for c_from, c_to, c_outcome in conn_items:
            curr_link_id += 1
            lid = curr_link_id
            from_id = ref_to_id[c_from]
            to_id = ref_to_id[c_to]

            from_elem_data = created_data_by_id.get(from_id)
            if from_elem_data is None:
                from_elem_data = next((e for e in existing_elements if str(e.get("element_id")) == str(from_id)), {})

            source_type = from_elem_data.get("type")
            is_branching = source_type in schema_registry.BRANCHING_TYPES

            if is_branching and not c_outcome:
                outcomes_list = from_elem_data.get("outcomes") or []
                if len(outcomes_list) == 1:
                    c_outcome = tb.outcome_label(outcomes_list[0])
                else:
                    available = [tb.outcome_label(o) for o in outcomes_list]
                    return BuildWorkflowBulkResult(
                        warnings=warnings,
                        error=f"Step '{c_from}' ({source_type}) is a branching step and requires an outcome.",
                        hint=f"Available outcomes: {available}",
                    )
            if not is_branching and c_outcome:
                return BuildWorkflowBulkResult(
                    warnings=warnings,
                    error=f"Step '{c_from}' ({source_type}) does not branch — it takes no outcome.",
                )

            link_payload = tb.build_link_create(lid, from_id, to_id)

            if is_branching:
                try:
                    matched_outcome = tb.resolve_outcome(from_elem_data, c_outcome)
                except tb.ValidationError as e:
                    return BuildWorkflowBulkResult(warnings=warnings, error=str(e))

                outcome_label = tb.outcome_label(matched_outcome) or c_outcome
                link_payload["data"]["labels"] = [{"justCreated": True, "label": outcome_label}]

                outcome_id = matched_outcome.get("outcomeID") or matched_outcome.get("id") if isinstance(matched_outcome, dict) else 1
                outcomes = from_elem_data.get("outcomes") or []
                updated_outcomes = []
                for idx, o in enumerate(outcomes, start=1):
                    if isinstance(o, str):
                        o = tb._task_outcome_object(o, idx)
                    curr_id = o.get("outcomeID") or o.get("id") or idx
                    try:
                        curr_id = int(curr_id)
                        target_oid = int(outcome_id)
                    except (TypeError, ValueError):
                        curr_id = str(curr_id)
                        target_oid = str(outcome_id)
                    if curr_id == target_oid:
                        updated_outcomes.append({**o, "linkID": lid})
                    else:
                        updated_outcomes.append(o)
                from_elem_data["outcomes"] = updated_outcomes

            link_creates.append(link_payload)

        # 6. Atomic write via update_tree
        try:
            revision_log.capture_workflow_revision(
                client,
                workflow_id,
                _revision_reason(f"before build_workflow_bulk ({len(steps)} steps)", intent, reason),
                tool_name="build_workflow_bulk",
            )
            client.update_tree(
                workflow_id,
                elements=element_creates,
                links=link_creates,
            )
        except JotformAPIError as e:
            return BuildWorkflowBulkResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                trigger_form_id=created_trigger_form_id,
                trigger_form_url=_form_url(created_trigger_form_id),
                error=str(e),
                warnings=warnings,
            )

        return BuildWorkflowBulkResult(
            workflow_id=workflow_id,
            workflow_url=_workflow_url(workflow_id),
            trigger_form_id=created_trigger_form_id,
            trigger_form_url=_form_url(created_trigger_form_id),
            created_steps={s_ref: str(ref_to_id[s_ref]) for s_ref, _, _ in step_items},
            created_links_count=len(link_creates),
            warnings=warnings,
        )

    @mcp.tool()
    def add_step(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        step_type: Annotated[str, Field(
            description='From list_step_types, e.g. "workflow_send_email".'
        )],
        config: Annotated[dict, Field(
            description=(
                "Fields for this step type — call get_step_schema first to "
                "see what it accepts. Unknown fields are dropped, not "
                "rejected; check `warnings` in the result. Before adding "
                "steps whose behavior depends on user intent — tasks, emails, "
                "approvals, conditions, conditional branches, or any step with "
                "outcomes/description/message fields — ask one short question "
                "for the missing essentials instead of creating an empty "
                "placeholder. Examples: for a task ask who it is assigned to "
                "and what should be done; for a branch ask the branch names "
                "and conditions; for an approval ask the approver and outcomes. "
                "The server refuses empty task/approval/assign/email/sign/"
                "condition steps."
            )
        )],
        after_step_id: Annotated[str, Field(
            description=(
                "Optional — if given, connects this step directly after "
                "that one. Only works when after_step_id doesn't already "
                "have an outgoing connection (a step with more than one "
                "exit needs deliberate wiring — use connect_steps for "
                "that, with an outcome if the source branches)."
            )
        )] = "",
        allow_duplicate: Annotated[bool, Field(
            description=(
                "Default false. Set true only after the user explicitly wants "
                "another similar step instead of reusing/updating/connecting "
                "the existing one."
            )
        )] = False,
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
    ) -> AddStepResult:
        """
        Add a single step to an EXISTING workflow for minor manual edits.

        CRITICAL: DO NOT call add_step in a loop when creating or generating a workflow.
        ALWAYS use build_workflow_bulk instead to build all steps and wiring in one atomic call.

        Returns the new step_id. Position on the canvas is chosen automatically.
        """
        try:
            clean_config, warnings = tb.validate_config(step_type, config)
        except tb.ValidationError as e:
            return AddStepResult(
                error=str(e), hint="Call list_step_types to see valid values.",
            )

        missing = _missing_required_step_details(step_type, clean_config)
        if missing:
            return AddStepResult(
                type=step_type,
                warnings=warnings,
                error=(
                    f"{step_type} needs more detail before it can be added. "
                    f"Missing: {', '.join(missing)}."
                ),
                hint=(
                    "Ask the user one short question for these essentials, "
                    "then call add_step again with those fields. Do not create "
                    "an empty placeholder unless a separate draft-specific "
                    "tool/flow explicitly allows it."
                ),
            )

        field_error, field_hint = _invalid_condition_field_message(
            client, workflow_id, step_type, clean_config
        )
        if field_error:
            return AddStepResult(
                type=step_type,
                warnings=warnings,
                error=field_error,
                hint=field_hint,
            )

        assignee_fields = ASSIGNEE_FIELDS_BY_STEP_TYPE.get(step_type, ())
        if assignee_fields:
            clean_config, assignee_hint, assignee_error = _normalize_assignee_fields(
                client, workflow_id, clean_config, assignee_fields
            )
            if assignee_error:
                return AddStepResult(
                    type=step_type,
                    warnings=warnings,
                    error=assignee_error,
                    hint="Use a valid fixed email address or call get_form_fields and choose a real email field.",
                )
            if assignee_hint:
                warnings.append(assignee_hint)

        if step_type in ("workflow_send_email", "workflow_reminder_email"):
            clean_config, recipient_hint, recipient_error = _normalize_email_config(
                client, workflow_id, clean_config
            )
            if recipient_error:
                return AddStepResult(
                    type=step_type,
                    warnings=warnings,
                    error=recipient_error,
                    hint="Bind a trigger form first, then use a real email field or fixed email address.",
                )
            if recipient_hint:
                warnings.append(recipient_hint)

        try:
            elements = client.get_elements(workflow_id)
        except JotformAPIError as e:
            return AddStepResult(error=str(e))

        if not allow_duplicate:
            duplicate = _find_duplicate_step(elements, step_type, clean_config)
            if duplicate is not None:
                existing_step_id = str(duplicate.get("element_id"))
                return AddStepResult(
                    type=step_type,
                    existing_step_id=existing_step_id,
                    warnings=warnings,
                    error=(
                        f"A similar {step_type} step already exists as step "
                        f"{existing_step_id}. I did not create a duplicate."
                    ),
                    hint=(
                        "Use connect_steps to wire the existing step, "
                        "update_step to change it, or call add_step again with "
                        "allow_duplicate=true only if the user explicitly wants "
                        "a second similar step."
                    ),
                )

        after_id = after_step_id or None
        if after_id is not None:
            try:
                links = client.get_links(workflow_id)
            except JotformAPIError as e:
                return AddStepResult(error=str(e))
            existing_exit = next(
                (l for l in links if str(l.get("fromElement")) == str(after_id)), None
            )
            if existing_exit is not None:
                return AddStepResult(
                    error=(
                        f"Step {after_id} already has an outgoing connection "
                        f"(to step {existing_exit.get('toElement')})."
                    ),
                    hint=(
                        "Add this step without after_step_id, then use "
                        "connect_steps to wire it in explicitly — pass an "
                        "outcome if step {after_id} is an if/else, "
                        "conditional branch, approval, or task with outcomes."
                    ).format(after_id=after_id),
                )

        element_id = tb.next_id([e.get("element_id") for e in elements])
        position = tb.compute_position(elements, after_id)
        create_entry = tb.build_element_create(step_type, element_id, clean_config, position)

        try:
            revision_log.capture_workflow_revision(
                client,
                workflow_id,
                _revision_reason(f"before add_step {step_type}", intent, reason),
                tool_name="add_step",
            )
            client.update_tree(workflow_id, elements=[create_entry])
        except JotformAPIError as e:
            return AddStepResult(error=str(e))

        linked_from = None
        if after_id is not None:
            try:
                links = client.get_links(workflow_id)
                link_id = tb.next_id([l.get("link_id") for l in links])
                client.update_tree(
                    workflow_id,
                    links=[tb.build_link_create(link_id, after_id, element_id)],
                )
                linked_from = str(after_id)
            except JotformAPIError as e:
                return AddStepResult(
                    step_id=str(element_id), type=step_type, warnings=warnings,
                    error=f"Step created, but linking from {after_id} failed: {e}",
                )

        return AddStepResult(
            step_id=str(element_id), type=step_type,
            linked_from=linked_from, warnings=warnings,
        )

    @mcp.tool()
    def connect_steps(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        from_step_id: Annotated[str, Field(description="From get_workflow's steps list.")],
        to_step_id: Annotated[str, Field(description="From get_workflow's steps list.")],
        outcome: Annotated[str, Field(
            description=(
                'Required if from_step_id is a branching step: if/else, '
                'conditional branch, approval, or task with outcomes. '
                'Examples: "TRUE", "FALSE", "Approve", "Reject", '
                'or a custom task/branch outcome name. Check get_workflow '
                "or get_step_details on from_step_id to see what outcomes "
                "exist and which are already used. Leave empty only for "
                "steps without outcomes."
            )
        )] = "",
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
    ) -> ConnectStepsResult:
        """
        Connect two existing steps in an EXISTING workflow for minor manual edits.

        CRITICAL: DO NOT call connect_steps in a loop when creating or generating a workflow.
        ALWAYS use build_workflow_bulk instead to build all steps and connections in one atomic call.

        Fails without changing anything if the outcome doesn't exist, is
        already connected elsewhere, or is missing when required.
        """
        try:
            source = client.get_element(workflow_id, from_step_id)
        except JotformAPIError as e:
            return ConnectStepsResult(error=str(e))

        source_type = source.get("type")
        is_branching = source_type in schema_registry.BRANCHING_TYPES

        if is_branching and not outcome:
            # tb.outcome_label, not raw conditionValue — a conditional
            # branch's named outcomes all share conditionValue "CUSTOM";
            # the real per-branch name lives in branchName, and this hint
            # is the model's only way to discover it without a separate
            # get_step_details call.
            available = [tb.outcome_label(o) for o in (source.get("outcomes") or [])]
            return ConnectStepsResult(
                error=f"{from_step_id} is a {source_type} and requires an outcome.",
                hint=f"Available outcomes: {available}",
            )
        if not is_branching and outcome:
            return ConnectStepsResult(
                error=f"{from_step_id} ({source_type}) does not branch — it takes no outcome.",
            )

        matched_outcome = None
        if is_branching:
            try:
                matched_outcome = tb.resolve_outcome(source, outcome)
            except tb.ValidationError as e:
                return ConnectStepsResult(error=str(e))

        try:
            links = client.get_links(workflow_id)
        except JotformAPIError as e:
            return ConnectStepsResult(error=str(e))

        link_id = tb.next_id([l.get("link_id") for l in links])
        try:
            revision_log.capture_workflow_revision(
                client,
                workflow_id,
                _revision_reason(
                    f"before connect_steps {from_step_id}->{to_step_id}",
                    intent,
                    reason,
                ),
                tool_name="connect_steps",
            )
            client.update_tree(
                workflow_id,
                links=[tb.build_link_create(link_id, from_step_id, to_step_id)],
            )
        except JotformAPIError as e:
            return ConnectStepsResult(error=str(e))

        if is_branching:
            try:
                outcome_label = tb.outcome_label(matched_outcome) or outcome
                client.update_tree(
                    workflow_id,
                    links=[tb.build_link_label_update(link_id, outcome_label)],
                    elements=[tb.build_outcome_update(
                        source, matched_outcome["outcomeID"], link_id
                    )],
                )
            except JotformAPIError as e:
                return ConnectStepsResult(
                    link_id=str(link_id), from_step=from_step_id, to_step=to_step_id,
                    error=(
                        f"Link created, but labelling the outcome failed: {e}. "
                        f"The steps are connected but the branch is unlabelled."
                    ),
                )

        return ConnectStepsResult(
            link_id=str(link_id), from_step=from_step_id, to_step=to_step_id,
            outcome=outcome or None,
        )

    @mcp.tool()
    def disconnect_steps(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        link_id: Annotated[str, Field(
            description="From get_workflow's connections list."
        )],
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
    ) -> DisconnectStepsResult:
        """
        Remove a single connection between two steps, without deleting
        either step.

        If the connection leaves a branching step (if/else, conditional
        branch, approval, or task with outcomes), that outcome's link is cleared first — so it
        shows up as unconnected again and can be wired to something else
        with connect_steps. Without this, the outcome would still point at
        a link_id that no longer exists: resolve_outcome would wrongly
        report it as already connected, and get_workflow's health check
        would separately start flagging it as a dangling link.

        Use this instead of add_step + connect_steps when the goal is
        rewiring an existing structure — for example, replacing
        On Submission -> Review -> Approval with a direct
        On Submission -> Approval by removing the old link first.
        """
        try:
            links = client.get_links(workflow_id)
        except JotformAPIError as e:
            return DisconnectStepsResult(error=str(e))

        link = next((l for l in links if str(l.get("link_id")) == str(link_id)), None)
        if link is None:
            return DisconnectStepsResult(
                error=f"No link {link_id} in this workflow.",
                hint="Call get_workflow and check the connections list for valid link ids.",
            )

        from_step_id = link.get("fromElement")

        try:
            source = client.get_element(workflow_id, from_step_id)
        except JotformAPIError as e:
            return DisconnectStepsResult(error=str(e))

        outcome_cleared = None
        try:
            revision_log.capture_workflow_revision(
                client,
                workflow_id,
                _revision_reason(f"before disconnect_steps {link_id}", intent, reason),
                tool_name="disconnect_steps",
            )
        except JotformAPIError as e:
            return DisconnectStepsResult(
                from_step=str(from_step_id),
                error=f"Could not save a revision before disconnecting: {e}",
            )

        if source.get("type") in schema_registry.BRANCHING_TYPES:
            outcome = tb.find_outcome_by_link(source, link_id)
            if outcome is not None:
                try:
                    client.update_tree(
                        workflow_id,
                        elements=[tb.build_outcome_update(
                            source, outcome["outcomeID"], None
                        )],
                    )
                except JotformAPIError as e:
                    return DisconnectStepsResult(
                        from_step=str(from_step_id),
                        error=f"Could not clear the outcome before disconnecting: {e}",
                    )
                outcome_cleared = tb.outcome_label(outcome)

        try:
            client.update_tree(workflow_id, links=[tb.build_link_delete(link_id)])
        except JotformAPIError as e:
            return DisconnectStepsResult(
                from_step=str(from_step_id), outcome_cleared=outcome_cleared,
                error=(
                    f"Outcome cleared but link deletion failed: {e}. "
                    f"The branch is now unwired but the old link may still exist "
                    f"— check get_workflow before retrying."
                ),
            )

        return DisconnectStepsResult(
            link_id=str(link_id), from_step=str(from_step_id),
            outcome_cleared=outcome_cleared, disconnected=True,
        )

    @mcp.tool()
    def update_step(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        step_id: Annotated[str, Field(description="From get_workflow's steps list.")],
        config: Annotated[dict, Field(
            description=(
                "Only the fields to change — call get_step_details first "
                "to see current values, get_step_schema for valid fields."
            )
        )],
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
    ) -> UpdateStepResult:
        """
        Change an existing step's configuration.

        Does not move the step or change its connections — use connect_steps
        for wiring.
        """
        try:
            current = client.get_element(workflow_id, step_id)
        except JotformAPIError as e:
            return UpdateStepResult(step_id=step_id, error=str(e))

        step_type = current.get("type")
        if not step_type:
            return UpdateStepResult(step_id=step_id, error="Could not determine this step's type.")

        try:
            clean_config, warnings = tb.validate_config(step_type, config)
        except tb.ValidationError as e:
            return UpdateStepResult(step_id=step_id, error=str(e))

        if not clean_config:
            return UpdateStepResult(
                step_id=step_id, warnings=warnings,
                error="Nothing to update — no valid fields in config.",
            )

        clean_config = _merge_outcome_updates(current, clean_config)

        field_error, field_hint = _invalid_condition_field_message(
            client, workflow_id, step_type, clean_config
        )
        if field_error:
            return UpdateStepResult(
                step_id=step_id,
                warnings=warnings,
                error=field_error,
                hint=field_hint,
            )

        assignee_fields = ASSIGNEE_FIELDS_BY_STEP_TYPE.get(step_type, ())
        if assignee_fields:
            clean_config, assignee_hint, assignee_error = _normalize_assignee_fields(
                client, workflow_id, clean_config, assignee_fields
            )
            if assignee_error:
                return UpdateStepResult(
                    step_id=step_id,
                    warnings=warnings,
                    error=assignee_error,
                    hint="Use a valid fixed email address or call get_form_fields and choose a real email field.",
                )
            if assignee_hint:
                warnings.append(assignee_hint)

        if step_type in ("workflow_send_email", "workflow_reminder_email"):
            clean_config, recipient_hint, recipient_error = _normalize_email_config(
                client, workflow_id, clean_config
            )
            if recipient_error:
                return UpdateStepResult(
                    step_id=step_id,
                    warnings=warnings,
                    error=recipient_error,
                    hint="Bind a trigger form first, then use a real email field or fixed email address.",
                )
            if recipient_hint:
                warnings.append(recipient_hint)

        try:
            revision_log.capture_workflow_revision(
                client,
                workflow_id,
                _revision_reason(f"before update_step {step_id}", intent, reason),
                tool_name="update_step",
            )
            client.update_tree(
                workflow_id, elements=[tb.build_element_update(step_id, clean_config)]
            )
        except JotformAPIError as e:
            return UpdateStepResult(step_id=step_id, warnings=warnings, error=str(e))

        return UpdateStepResult(step_id=step_id, warnings=warnings)
