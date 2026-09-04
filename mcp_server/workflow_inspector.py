"""
Workflow preflight checks beyond graph health.

graph.py answers "can this workflow run through the drawn links?" This module
answers the messier product questions: is a task still missing an assignee,
does a condition point at a real form field, and are branches defined but not
wired yet?
"""
from __future__ import annotations

from mcp_server import condition_validation, graph, schema_registry
from mcp_server import tree_builder as tb


VALUELESS_OPERATORS = {"isEmpty", "isFilled"}


def has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def workflow_url(workflow_id: str | None) -> str | None:
    return f"https://www.jotform.com/workflow/{workflow_id}/build" if workflow_id else None


def form_url(form_id: str | None) -> str | None:
    return f"https://www.jotform.com/build/{form_id}" if form_id else None


def trigger_form_id(elements: list[dict]) -> str | None:
    start = next(
        (e for e in elements if str(e.get("element_id")) == "1"
         or e.get("type") == "workflow_start_point"),
        None,
    )
    if not start or is_schedule_start_point(start):
        return None
    value = (start or {}).get("resourceID")
    return str(value) if value else None


def is_schedule_start_point(element: dict | None) -> bool:
    if not isinstance(element, dict):
        return False
    subtype = str(element.get("subType") or "").lower()
    return subtype == "workflow_start_point_schedule"


def extract_condition_terms(step_type: str, config: dict) -> list[tuple[str, dict]]:
    terms = []
    if step_type == "workflow_binary_decision":
        for idx, term in enumerate(config.get("conditionTerms") or [], start=1):
            if isinstance(term, dict):
                terms.append((f"conditionTerms[{idx}]", term))
    if step_type == "workflow_conditional_branch":
        for outcome_idx, outcome in enumerate(config.get("outcomes") or [], start=1):
            if not isinstance(outcome, dict):
                continue
            label = tb.outcome_label(outcome) or f"outcome {outcome_idx}"
            for term_idx, term in enumerate(outcome.get("conditionTerms") or [], start=1):
                if isinstance(term, dict):
                    terms.append((f"outcomes[{label}].conditionTerms[{term_idx}]", term))
    return terms


def invalid_field_references(config: dict, step_type: str, valid_field_ids: set[str]) -> list[str]:
    invalid = []
    for path, term in extract_condition_terms(step_type, config):
        field = term.get("field")
        if field and str(field) not in valid_field_ids:
            invalid.append(f"{path}.field={field!r}")
    return invalid


def branch_diagnostics(elements: list[dict], links: list[dict]) -> dict[str, list[str]]:
    """
    Check that a branch link exists both as a canvas link and as outcome metadata.

    Jotform stores the drawn line in links[] and the selected branch label in
    outcomes[].linkID. A workflow can look connected while the builder still
    cannot tell which outcome the line represents.
    """
    actual_links = {
        str(link.get("link_id")): link
        for link in links
        if link.get("link_id") is not None
    }
    invalid: list[str] = []
    unlabelled: list[str] = []

    for element in elements:
        if element.get("type") not in schema_registry.BRANCHING_TYPES:
            continue
        step_id = str(element.get("element_id"))
        mapped_link_ids: set[str] = set()

        for outcome in element.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            link_id = outcome.get("linkID")
            if link_id in (None, 0, "0", ""):
                continue
            normalized_id = str(link_id)
            mapped_link_ids.add(normalized_id)
            link = actual_links.get(normalized_id)
            label = tb.outcome_label(outcome) or str(outcome.get("outcomeID"))
            if link is None:
                invalid.append(f"step {step_id} {label}: link {normalized_id} does not exist")
            elif str(link.get("fromElement")) != step_id:
                invalid.append(
                    f"step {step_id} {label}: link {normalized_id} leaves step {link.get('fromElement')}"
                )

        for link_id, link in actual_links.items():
            if str(link.get("fromElement")) == step_id and link_id not in mapped_link_ids:
                unlabelled.append(f"step {step_id}: outgoing link {link_id} has no outcome")

    return {
        "invalid_branch_links": invalid,
        "unlabelled_branching_steps": unlabelled,
    }


def _issue(
    severity: str,
    category: str,
    message: str,
    *,
    step_id: str | None = None,
    step_type: str | None = None,
    field: str | None = None,
    suggested_question: str | None = None,
) -> dict:
    return {
        "severity": severity,
        "category": category,
        "step_id": step_id,
        "step_type": step_type,
        "field": field,
        "message": message,
        "suggested_question": suggested_question,
    }


def _recipient_is_static(item) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("isQuestion") is True:
        return False
    text = item.get("text") or item.get("value")
    return isinstance(text, str) and bool(text.strip()) and not text.strip().startswith("{")


def _add_recipient_issues(issues: list[dict], element: dict, field_name: str) -> None:
    recipients = element.get(field_name)
    if recipients in (None, "", []):
        issues.append(_issue(
            "error",
            "missing_recipient",
            f"{field_name} is empty.",
            step_id=str(element.get("element_id")),
            step_type=element.get("type"),
            field=field_name,
            suggested_question=(
                "Which form email field or fixed email address should receive this?"
            ),
        ))
        return

    if not isinstance(recipients, list):
        return
    if any(_recipient_is_static(item) for item in recipients):
        issues.append(_issue(
            "warning",
            "static_text_recipient",
            (
                f"{field_name} uses static text/email. If this should come "
                "from the submitted form, use a form field reference instead."
            ),
            step_id=str(element.get("element_id")),
            step_type=element.get("type"),
            field=field_name,
            suggested_question=(
                "Should this recipient be a fixed email, or should I use one "
                "of the form's email fields?"
            ),
        ))


def inspect_workflow(combined: dict, form_questions: dict | None = None) -> dict:
    workflow = combined.get("workflow") if isinstance(combined.get("workflow"), dict) else {}
    workflow_id = str(workflow.get("id")) if workflow.get("id") is not None else None
    elements = [e for e in (combined.get("elements") or []) if isinstance(e, dict)]
    links = [l for l in (combined.get("links") or []) if isinstance(l, dict)]
    form_questions = form_questions or {}
    valid_field_ids = {str(qid) for qid in form_questions}

    issues: list[dict] = []
    start = next(
        (e for e in elements if str(e.get("element_id")) == "1"
         or e.get("type") == "workflow_start_point"),
        None,
    )
    form_id = trigger_form_id(elements)
    if not form_id and not is_schedule_start_point(start):
        issues.append(_issue(
            "error",
            "missing_trigger_form",
            "Workflow start point has no trigger form.",
            step_id="1",
            step_type="workflow_start_point",
            field="resourceID",
            suggested_question=(
                "Should I use an existing form as the trigger, or create a new AI form?"
            ),
        ))

    element_ids = {
        str(e.get("element_id")) for e in elements if e.get("element_id") is not None
    }
    for link in links:
        link_id = str(link.get("link_id")) if link.get("link_id") is not None else None
        from_id = link.get("fromElement")
        to_id = link.get("toElement")
        if from_id in (None, "") or to_id in (None, ""):
            issues.append(_issue(
                "error",
                "empty_link",
                f"Link {link_id or '(no id)'} is missing fromElement or toElement.",
                field="links",
                suggested_question="Which two steps should this connection link?",
            ))
            continue
        if str(from_id) not in element_ids or str(to_id) not in element_ids:
            issues.append(_issue(
                "error",
                "dangling_link",
                f"Link {link_id} points at a step that does not exist.",
                field="links",
                suggested_question="Should I delete this broken link or reconnect it?",
            ))

    steps = [{"step_id": e.get("element_id"), "type": e.get("type")} for e in elements]
    conns = [
        {"link_id": l.get("link_id"), "from_step": l.get("fromElement"), "to_step": l.get("toElement")}
        for l in links
    ]
    health = graph.analyse(steps, conns)
    for step_id in health["unreachable_steps"]:
        issues.append(_issue(
            "warning",
            "unreachable_step",
            f"Step {step_id} is not reachable from the start point.",
            step_id=str(step_id),
            suggested_question="Should I connect this step into the flow or remove it?",
        ))
    for step_id in health["dead_end_steps"]:
        issues.append(_issue(
            "warning",
            "dead_end_step",
            f"Step {step_id} is reached but has no outgoing path.",
            step_id=str(step_id),
            suggested_question="What should happen after this step?",
        ))

    branch_health = branch_diagnostics(elements, links)
    for item in branch_health["invalid_branch_links"]:
        issues.append(_issue(
            "error",
            "invalid_branch_link",
            item,
            field="outcomes.linkID",
            suggested_question=(
                "Should I disconnect this broken branch mapping and reconnect it?"
            ),
        ))
    for item in branch_health["unlabelled_branching_steps"]:
        issues.append(_issue(
            "warning",
            "unlabelled_branch_link",
            item,
            field="outcomes.linkID",
            suggested_question=(
                "Which outcome should this outgoing branch link represent?"
            ),
        ))

    for element in elements:
        step_id = str(element.get("element_id")) if element.get("element_id") is not None else None
        step_type = element.get("type")

        if step_type == "workflow_assign_task":
            if not has_value(element.get("assignee")):
                issues.append(_issue(
                    "error", "missing_assignee", "Task has no assignee.",
                    step_id=step_id, step_type=step_type, field="assignee",
                    suggested_question="Who should this task be assigned to?"
                ))
            if not has_value(element.get("taskDescription")):
                issues.append(_issue(
                    "warning", "missing_description", "Task has no taskDescription.",
                    step_id=step_id, step_type=step_type, field="taskDescription",
                    suggested_question="What should the assignee do in this task?"
                ))

        if step_type == "workflow_approval":
            if not has_value(element.get("approver")):
                issues.append(_issue(
                    "error", "missing_approver", "Approval has no approver.",
                    step_id=step_id, step_type=step_type, field="approver",
                    suggested_question="Who should approve or deny this?"
                ))
            if not has_value(element.get("taskDescription")):
                issues.append(_issue(
                    "warning", "missing_description", "Approval has no decision description.",
                    step_id=step_id, step_type=step_type, field="taskDescription",
                    suggested_question="What is the approver deciding?"
                ))

        if step_type in ("workflow_assign", "workflow_assign_form"):
            if not has_value(element.get("assignee")):
                issues.append(_issue(
                    "error", "missing_assignee", "Assignment has no assignee.",
                    step_id=step_id, step_type=step_type, field="assignee",
                    suggested_question="Who should this be assigned to?"
                ))

        if step_type == "workflow_send_email":
            _add_recipient_issues(issues, element, "to")
            for field_name in ("subject", "content"):
                if not has_value(element.get(field_name)):
                    issues.append(_issue(
                        "error", f"missing_{field_name}", f"Email has no {field_name}.",
                        step_id=step_id, step_type=step_type, field=field_name,
                        suggested_question=(
                            "What should the email subject and short message be?"
                        ),
                    ))

        if step_type == "workflow_reminder_email":
            _add_recipient_issues(issues, element, "to")

        if step_type in schema_registry.BRANCHING_TYPES:
            for outcome in element.get("outcomes") or []:
                if not isinstance(outcome, dict):
                    continue
                label = tb.outcome_label(outcome) or outcome.get("outcomeID")
                if not outcome.get("linkID"):
                    issues.append(_issue(
                        "warning", "unconnected_outcome",
                        f"Outcome {label!r} is not connected to any next step.",
                        step_id=step_id, step_type=step_type, field="outcomes",
                        suggested_question=f"What should happen on outcome {label!r}?",
                    ))
                if step_type == "workflow_conditional_branch":
                    is_other = outcome.get("conditionValue") == "OTHER"
                    if not is_other and not has_value(outcome.get("conditionTerms")):
                        issues.append(_issue(
                            "error", "missing_condition_terms",
                            f"Conditional branch {label!r} has no condition terms.",
                            step_id=step_id, step_type=step_type, field="outcomes.conditionTerms",
                            suggested_question=(
                                "Which form field and value should this branch check?"
                            ),
                        ))

            if step_type == "workflow_binary_decision" and not has_value(element.get("conditionTerms")):
                issues.append(_issue(
                    "error", "missing_condition_terms",
                    "If/Else condition has no conditionTerms.",
                    step_id=step_id, step_type=step_type, field="conditionTerms",
                    suggested_question="Which form field and value should this condition check?",
                ))

            for path, term in extract_condition_terms(step_type, element):
                field_id = term.get("field")
                operator = term.get("operator")
                if not field_id:
                    issues.append(_issue(
                        "error", "missing_condition_field",
                        f"{path} has no field.",
                        step_id=step_id, step_type=step_type, field=path,
                        suggested_question="Which form field should this condition use?",
                    ))
                elif valid_field_ids and str(field_id) not in valid_field_ids:
                    issues.append(_issue(
                        "error", "invalid_condition_field",
                        f"{path} uses {field_id!r}, which is not a field on the trigger form.",
                        step_id=step_id, step_type=step_type, field=path,
                        suggested_question=(
                            "Which trigger form field should replace this invalid condition field?"
                        ),
                    ))
                if not operator:
                    issues.append(_issue(
                        "error", "missing_condition_operator",
                        f"{path} has no operator.",
                        step_id=step_id, step_type=step_type, field=path,
                        suggested_question="Which operator should this condition use?",
                    ))
                if operator not in VALUELESS_OPERATORS and "value" not in term:
                    issues.append(_issue(
                        "warning", "missing_condition_value",
                        f"{path} has no comparison value.",
                        step_id=step_id, step_type=step_type, field=path,
                        suggested_question="What value should this condition compare against?",
                    ))
                if form_questions and field_id and str(field_id) in valid_field_ids and operator:
                    try:
                        condition_validation.validate_terms(
                            form_questions,
                            [term],
                            form_id=form_id or "",
                            context=path,
                        )
                    except condition_validation.ConditionValidationError as e:
                        issues.append(_issue(
                            "error",
                            (
                                "invalid_condition_operator"
                                if operator not in condition_validation.ALLOWED_OPERATORS
                                else "invalid_condition_value"
                            ),
                            str(e),
                            step_id=step_id,
                            step_type=step_type,
                            field=path,
                            suggested_question=e.hint,
                        ))

    return {
        "workflow_id": workflow_id,
        "workflow_url": workflow_url(workflow_id),
        "trigger_form_id": form_id,
        "trigger_form_url": form_url(form_id),
        "issues": issues,
        "ok_to_publish": not any(issue["severity"] == "error" for issue in issues),
    }
