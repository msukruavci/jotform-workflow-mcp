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
import os
import re
import unicodedata
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from typing import Annotated
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server import MCPServer
from pydantic import Field

from mcp_server import audit_log, revision_log, schema_registry, sync_state, tree_builder as tb, workflow_inspector
from mcp_server.integrations import supported_integration_subtypes_text
from mcp_server.jotform_client import (
    ConflictError,
    JotformAPIError,
    JotformClient,
    PartialWorkflowCreateError,
    workflow_revision_id,
    workflow_updated_at,
)
from mcp_server.models import (
    AddStepResult, BuildWorkflowBulkResult, ConnectStepsResult, ConnectionSpec,
    CreateAIFormResult, CreateWorkflowResult,
    CreateWorkflowWithAIFormResult,
    DisconnectStepsResult, StepSpec, StepUpdateSpec, UpdateStepResult,
)
from mcp_server.tools.reading import form_fields_from_questions


def _workflow_url(workflow_id: str | None) -> str | None:
    return f"https://www.jotform.com/workflow/{workflow_id}/build" if workflow_id else None


def _form_url(form_id: str | None) -> str | None:
    return f"https://www.jotform.com/build/{form_id}" if form_id else None


def _element_axis(element: dict, axis: str, default=0):
    value = element.get(axis)
    if value is None and isinstance(element.get("position"), dict):
        value = element["position"].get(axis)
    return default if value is None else value


_LAYOUT_ONLY_ELEMENT_KEYS = frozenset({
    "className",
    "dragging",
    "measured",
    "position",
    "privateRendererState",
    "selected",
    "style",
    "x",
    "y",
})

_IGNORED_VERIFICATION_FIELDS = {
    "workflow_send_email": frozenset({
        "recipientLimit", "replyTo", "showCcField", "showBccField",
        "pdfPassword", "isRecipientExpanded", "uploadAttachment",
        "uploadAttachmentEnable",
    }),
    "workflow_start_point": frozenset({
        "schedule__end__recurring", "schedule__executeWhen__afterAmount",
        "schedule__executeWhen__afterUnit", "schedule__executeWhen__customDate",
        "schedule__executeWhen__executeOnCustomDate", "schedule__days",
        "schedule__time", "schedule__timezone", "schedule__type"
    }),
}


def _element_id(element: dict) -> str | None:
    for key in ("element_id", "elementID", "id"):
        value = element.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _elements_by_id(snapshot: dict | None) -> dict[str, dict]:
    if not isinstance(snapshot, dict):
        return {}
    return {
        element_id: element
        for element in (snapshot.get("elements") or [])
        if isinstance(element, dict)
        for element_id in [_element_id(element)]
        if element_id is not None
    }


def _semantic_element_for_scope(element: dict | None) -> dict | None:
    if element is None:
        return None
    return {
        key: deepcopy(value)
        for key, value in element.items()
        if key not in _LAYOUT_ONLY_ELEMENT_KEYS
    }


def _semantic_value_matches(expected, actual) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _semantic_value_matches(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            _semantic_value_matches(left, right)
            for left, right in zip(expected, actual)
        )
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    return str(expected) == str(actual)


def _verify_bulk_snapshot(
    snapshot: dict,
    *,
    expected_steps: dict[str, tuple[str, dict]],
    expected_connections: list[tuple[str, str, str]],
    deleted_step_ids: list[str],
    deleted_link_ids: list[str],
) -> list[str]:
    """Return semantic mismatches found in a full post-write snapshot."""
    elements = _elements_by_id(snapshot)
    links = [item for item in (snapshot.get("links") or []) if isinstance(item, dict)]
    issues: list[str] = []

    for step_id, (step_type, expected_config) in expected_steps.items():
        actual = elements.get(str(step_id))
        if actual is None:
            issues.append(f"step {step_id} is missing")
            continue
        if str(actual.get("type") or "") != str(step_type):
            issues.append(f"step {step_id} type is {actual.get('type')!r}, expected {step_type!r}")
            continue
        ignored_for_type = _IGNORED_VERIFICATION_FIELDS.get(str(step_type)) or frozenset()
        for key, expected_value in expected_config.items():
            if key in _LAYOUT_ONLY_ELEMENT_KEYS or key in ignored_for_type:
                continue
            if key not in actual or not _semantic_value_matches(expected_value, actual.get(key)):
                issues.append(f"step {step_id} field {key!r} did not persist")

    for source_id, target_id, expected_outcome in expected_connections:
        matching_links = [
            link
            for link in links
            if (
            str(link.get("fromElement")) == str(source_id)
            and str(link.get("toElement")) == str(target_id)
            )
        ]
        if not matching_links:
            issues.append(f"connection {source_id}->{target_id} is missing")
            continue
        if expected_outcome:
            source = elements.get(str(source_id)) or {}
            actual_outcomes = []
            for link in matching_links:
                link_id = link.get("link_id") or link.get("id")
                linked_outcome = tb.find_outcome_by_link(source, link_id)
                label = tb.outcome_label(linked_outcome)
                if label:
                    actual_outcomes.append(label)
            if not any(
                str(label).strip().lower() == str(expected_outcome).strip().lower()
                for label in actual_outcomes
            ):
                issues.append(
                    f"connection {source_id}->{target_id} outcome is {actual_outcomes!r}, "
                    f"expected {expected_outcome!r}"
                )

    for step_id in deleted_step_ids:
        if str(step_id) in elements:
            issues.append(f"deleted step {step_id} is still present")
    for link_id in deleted_link_ids:
        if any(str(link.get("link_id") or link.get("id")) == str(link_id) for link in links):
            issues.append(f"deleted connection {link_id} is still present")

    workflow = snapshot.get("workflow") if isinstance(snapshot, dict) else None
    status = workflow.get("status") if isinstance(workflow, dict) else None
    if status and str(status).upper() != "DISABLED":
        issues.append(f"workflow status is {status!r}, expected 'DISABLED'")
    return issues


def _changed_affected_step_ids(
    base_snapshot: dict | None,
    current_snapshot: dict | None,
    affected_step_ids: set[str],
) -> list[str]:
    base_by_id = _elements_by_id(base_snapshot)
    current_by_id = _elements_by_id(current_snapshot)
    changed = []
    for step_id in sorted({str(sid) for sid in affected_step_ids}, key=lambda value: (not value.isdigit(), value)):
        if _semantic_element_for_scope(base_by_id.get(step_id)) != _semantic_element_for_scope(current_by_id.get(step_id)):
            changed.append(step_id)
    return changed


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


def _conflict_hint() -> str:
    return (
        "The workflow changed after the revision you used. Instead of stopping to ask "
        "for permission, you MUST automatically call get_workflow to fetch the new live "
        "graph, recalculate your changes on top of it, and retry build_workflow_bulk "
        "immediately. Do not ask the user for approval to retry."
    )


def _norm_text(value) -> str:
    return str(value or "").strip().lower()


_SCHEDULE_AFTER_UNITS = {"hour", "day", "weekday", "weekend", "week", "month", "year"}
_SCHEDULE_END_MODES = {"none", "date", "amount"}
_SCHEDULE_REPEAT_ALIASES = {
    "hourly": "hour",
    "daily": "day",
    "everyday": "day",
    "every day": "day",
    "weekday": "weekday",
    "weekdays": "weekday",
    "business days": "weekday",
    "weekend": "weekend",
    "weekends": "weekend",
    "weekly": "week",
    "monthly": "month",
    "yearly": "year",
    "annually": "year",
    "annual": "year",
}
_WEEKDAY_ALIASES = {
    "monday": 0,
    "mon": 0,
    "pazartesi": 0,
    "tuesday": 1,
    "tue": 1,
    "sali": 1,
    "salı": 1,
    "wednesday": 2,
    "wed": 2,
    "carsamba": 2,
    "çarşamba": 2,
    "thursday": 3,
    "thu": 3,
    "persembe": 3,
    "perşembe": 3,
    "friday": 4,
    "fri": 4,
    "cuma": 4,
    "saturday": 5,
    "sat": 5,
    "cumartesi": 5,
    "sunday": 6,
    "sun": 6,
    "pazar": 6,
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _first_value(value):
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if item not in (None, ""):
                return item
        return None
    return value


def _parse_schedule_time(value) -> time | None:
    if value in (None, ""):
        return None
    if isinstance(value, time):
        return value
    text = str(value).strip().lower().replace(".", "")
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = match.group(3)
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour=hour, minute=minute)


def _parse_schedule_datetime(value, tz_name: str | None) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        if not tz_name:
            return None
        tz = _schedule_zoneinfo(tz_name)
        if tz is None:
            return None
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def _format_utc_millis(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _schedule_zoneinfo(tz_name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(str(tz_name or "UTC").strip() or "UTC")
    except ZoneInfoNotFoundError:
        return None


def _configured_schedule_timezone(client: JotformClient | None = None) -> tuple[str | None, str | None]:
    if client is not None and hasattr(client, "get_user_timezone"):
        api_tz = client.get_user_timezone()
        if api_tz and _schedule_zoneinfo(api_tz):
            return api_tz, "Jotform user profile"
    for env_key in ("MCP_DEFAULT_TIMEZONE", "TZ"):
        tz_name = str(os.environ.get(env_key) or "").strip()
        if tz_name and _schedule_zoneinfo(tz_name):
            return tz_name, env_key
    return None, None


def _next_weekday_datetime(day_value, at_time: time, tz_name: str) -> datetime | None:
    day_text = str(_first_value(day_value) or "").strip().lower()
    target_weekday = _WEEKDAY_ALIASES.get(day_text)
    if target_weekday is None:
        return None
    tz = _schedule_zoneinfo(tz_name)
    if tz is None:
        return None
    now_local = _now_utc().astimezone(tz)
    days_ahead = (target_weekday - now_local.weekday()) % 7
    candidate_date = now_local.date() + timedelta(days=days_ahead)
    candidate = datetime.combine(candidate_date, at_time, tzinfo=tz)
    if candidate <= now_local:
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc)


def _coerce_positive_int_string(value, default: str = "1") -> str | None:
    if value in (None, ""):
        return default
    try:
        number = int(str(value).strip())
    except ValueError:
        return None
    if number < 1:
        return None
    return str(number)


def _normalize_schedule_config(
    config: dict | None,
    client: JotformClient | None = None,
) -> tuple[dict | None, list[str], str | None]:
    if config is None:
        return None, [], "trigger_schedule is required when trigger_type='schedule'."
    if not isinstance(config, dict):
        return None, [], "trigger_schedule must be an object when trigger_type='schedule'."
    if not config:
        return None, [], (
            "trigger_schedule cannot be empty for scheduled workflows; provide "
            "schedule__executeWhen__customDate, afterAmount, afterUnit, and end.recurring."
        )

    raw = deepcopy(config)
    flat: dict[str, str] = {}
    warnings: list[str] = []

    schedule_obj = raw.get("schedule")
    if isinstance(schedule_obj, dict):
        execute_when = schedule_obj.get("executeWhen")
        if isinstance(execute_when, dict):
            for src, dest in (
                ("afterAmount", "schedule__executeWhen__afterAmount"),
                ("afterUnit", "schedule__executeWhen__afterUnit"),
                ("customDate", "schedule__executeWhen__customDate"),
                ("executeOnCustomDate", "schedule__executeWhen__executeOnCustomDate"),
            ):
                if execute_when.get(src) not in (None, ""):
                    flat[dest] = str(execute_when[src])
        end = schedule_obj.get("end")
        if isinstance(end, dict):
            for src, dest in (
                ("recurring", "schedule__end__recurring"),
                ("customDate", "schedule__end__customDate"),
                ("afterAmount", "schedule__end__afterAmount"),
            ):
                if end.get(src) not in (None, ""):
                    flat[dest] = str(end[src])

    for key, value in raw.items():
        if key.startswith("schedule__") and key not in {
            "schedule__type",
            "schedule__days",
            "schedule__time",
            "schedule__timezone",
            "schedule__startDate",
            "schedule__date",
            "schedule__repeat",
            "schedule__interval",
            "schedule__every",
            "schedule__end",
            "schedule__endDate",
            "schedule__endTime",
            "schedule__endTimezone",
            "schedule__runs",
        }:
            flat[key] = str(value)

    repeat_alias = (
        raw.get("schedule__executeWhen__afterUnit")
        or raw.get("schedule__type")
        or raw.get("schedule__repeat")
        or raw.get("type")
        or raw.get("repeat")
    )
    repeat_text = str(repeat_alias or "").strip().lower()
    repeat_unit = _SCHEDULE_REPEAT_ALIASES.get(repeat_text, repeat_text)
    if repeat_unit:
        flat["schedule__executeWhen__afterUnit"] = repeat_unit

    amount = _coerce_positive_int_string(
        raw.get("schedule__executeWhen__afterAmount")
        or raw.get("schedule__interval")
        or raw.get("schedule__every")
        or raw.get("interval")
        or raw.get("every")
    )
    if amount is None:
        return None, warnings, "schedule executeWhen.afterAmount must be a positive integer."
    flat.setdefault("schedule__executeWhen__afterAmount", amount)

    explicit_tz_name = str(
        raw.get("schedule__timezone")
        or raw.get("timezone")
        or raw.get("tz")
        or ""
    ).strip()
    default_tz_name, default_tz_source = _configured_schedule_timezone(client)
    tz_name = explicit_tz_name or default_tz_name
    if tz_name and _schedule_zoneinfo(tz_name) is None:
        return None, warnings, f"Unknown schedule timezone '{tz_name}'. Use an IANA timezone like America/New_York."
    if tz_name and not explicit_tz_name:
        warnings.append(
            f"No schedule timezone was provided; used default timezone '{tz_name}' from {default_tz_source}. "
            "Pass schedule__timezone explicitly to avoid ambiguity."
        )

    alias_time = _parse_schedule_time(raw.get("schedule__time") or raw.get("time"))
    alias_day = raw.get("schedule__days") or raw.get("days") or raw.get("day")
    alias_date = raw.get("schedule__startDate") or raw.get("schedule__date") or raw.get("startDate") or raw.get("date")
    custom_dt = _parse_schedule_datetime(flat.get("schedule__executeWhen__customDate") or alias_date, tz_name)
    if custom_dt is None and alias_day not in (None, "") and alias_time is not None:
        if not tz_name:
            return None, warnings, (
                "schedule timezone is required when using local day/time aliases. "
                "Ask the user for an IANA timezone such as Europe/Istanbul or America/New_York, "
                "or pass a timezone-aware UTC ISO customDate."
            )
        custom_dt = _next_weekday_datetime(alias_day, alias_time, tz_name)
    if custom_dt is None:
        return None, warnings, (
            "schedule executeWhen.customDate is required. Use a timezone-aware UTC ISO value such as "
            "schedule__executeWhen__customDate='2026-09-04T14:00:00.000Z', or provide "
            "schedule__days plus schedule__time plus an explicit schedule__timezone so the server can normalize it."
        )
    flat["schedule__executeWhen__customDate"] = _format_utc_millis(custom_dt)
    flat.setdefault("schedule__executeWhen__executeOnCustomDate", "Yes")

    after_unit = str(flat.get("schedule__executeWhen__afterUnit") or "").strip().lower()
    if after_unit not in _SCHEDULE_AFTER_UNITS:
        return None, warnings, (
            "schedule executeWhen.afterUnit must be one of "
            f"{sorted(_SCHEDULE_AFTER_UNITS)}; got '{after_unit or 'missing'}'."
        )
    flat["schedule__executeWhen__afterUnit"] = after_unit

    end_mode = str(
        raw.get("schedule__end__recurring")
        or raw.get("schedule__end")
        or raw.get("end")
        or "none"
    ).strip().lower()
    end_mode = {
        "never": "none",
        "exact date & time": "date",
        "exact_date_time": "date",
        "exact": "date",
        "runs": "amount",
        "number of runs": "amount",
    }.get(end_mode, end_mode)
    if end_mode not in _SCHEDULE_END_MODES:
        return None, warnings, (
            "schedule end.recurring must be one of 'none', 'date', or 'amount'."
        )
    flat["schedule__end__recurring"] = end_mode

    if end_mode == "date":
        end_tz_name = str(raw.get("schedule__endTimezone") or raw.get("endTimezone") or tz_name or "").strip()
        end_dt = _parse_schedule_datetime(
            raw.get("schedule__end__customDate") or raw.get("schedule__endDate") or raw.get("endDate"),
            end_tz_name,
        )
        if end_dt is None:
            return None, warnings, "schedule end.recurring='date' requires schedule__end__customDate or schedule__endDate."
        if raw.get("schedule__endTime") or raw.get("endTime"):
            end_time = _parse_schedule_time(raw.get("schedule__endTime") or raw.get("endTime"))
            tz = _schedule_zoneinfo(end_tz_name)
            if end_time is not None and tz is not None:
                end_local = end_dt.astimezone(tz)
                end_dt = datetime.combine(end_local.date(), end_time, tzinfo=tz).astimezone(timezone.utc)
        flat["schedule__end__customDate"] = _format_utc_millis(end_dt)
    elif end_mode == "amount":
        end_amount = _coerce_positive_int_string(
            raw.get("schedule__end__afterAmount")
            or raw.get("schedule__runs")
            or raw.get("runs")
        )
        if end_amount is None:
            return None, warnings, "schedule end.recurring='amount' requires a positive run count."
        flat["schedule__end__afterAmount"] = end_amount

    legacy_keys = {"schedule__type", "schedule__days", "schedule__time", "schedule__timezone"}
    if legacy_keys.intersection(raw):
        warnings.append(
            "Normalized schedule aliases to Jotform's executeWhen/end schema; "
            "schedule__type, schedule__days, schedule__time, and schedule__timezone are not persisted directly."
        )
    return flat, warnings, None


def _ensure_new_workflow_reachable_from_start(
    conn_items: list[tuple[str, str, str]],
    step_refs: list[str],
    warnings: list[str],
) -> tuple[list[tuple[str, str, str]], str | None]:
    if not step_refs:
        return conn_items, None

    step_ref_set = set(step_refs)
    start_children = [to_ref for from_ref, to_ref, _ in conn_items if from_ref in {"start", "1"} and to_ref in step_ref_set]
    if not start_children:
        incoming_refs = {to_ref for _, to_ref, _ in conn_items if to_ref in step_ref_set}
        root_refs = [ref for ref in step_refs if ref not in incoming_refs]
        if len(root_refs) == 1:
            root_ref = root_refs[0]
            conn_items = [("start", root_ref, ""), *conn_items]
            warnings.append(
                f"Added missing start connection to '{root_ref}' so the new workflow is reachable from the trigger."
            )
        else:
            return conn_items, (
                "New workflow steps are not connected to the start point. Add exactly one first "
                "connection like ConnectionSpec(from_ref='start', to_ref='<first_step_ref>'), "
                f"or disambiguate the first step. Candidate roots: {root_refs or step_refs}."
            )

    adjacency: dict[str, list[str]] = {"start": []}
    for from_ref, to_ref, _ in conn_items:
        adjacency.setdefault(from_ref, []).append(to_ref)

    reachable: set[str] = set()
    queue = ["start", "1"]
    while queue:
        current = queue.pop(0)
        for child in adjacency.get(current, []):
            if child in reachable:
                continue
            reachable.add(child)
            queue.append(child)

    unreachable = [ref for ref in step_refs if ref not in reachable]
    if unreachable:
        return conn_items, (
            "New workflow contains detached step refs that are not reachable from start: "
            f"{unreachable}. Connect the first detached step from start or from an existing reachable step."
        )
    return conn_items, None


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

ROLE_PLACEHOLDER_DOMAIN = "workflow.invalid"
ROLE_PLACEHOLDER_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("human resources", "hr", "ik"), "hr"),
    (("manager", "mgr", "supervisor", "lead"), "manager"),
    (("finance", "accounting", "billing"), "finance"),
    (("legal", "compliance"), "legal"),
    (("advisor", "adviser", "counselor"), "advisor"),
    (("review", "approval", "approve"), "reviewer"),
)
START_REF_ALIASES = {"trigger", "trigger_form", "form", "submission", "start_point"}
END_REF_ALIASES = {"end_point"}
OUTCOME_ALIASES: dict[str, tuple[str, ...]] = {
    "approved": ("approve",),
    "accepted": ("approve",),
    "accept": ("approve",),
    "rejected": ("reject", "deny"),
    "reject": ("reject", "deny"),
    "denied": ("deny", "reject"),
    "declined": ("deny", "reject"),
    "completed": ("complete",),
    "done": ("complete",),
    "yes": ("true", "approve"),
    "no": ("false", "deny", "reject"),
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


def _human_step_name(step_ref: str, config: dict) -> str:
    name = str(config.get("name") or "").strip()
    if name:
        return name
    words = re.sub(r"[_-]+", " ", step_ref).strip()
    return words.title() if words else "Workflow Step"


def _normalize_connection_ref(value: str, *, source: bool) -> tuple[str, str | None]:
    raw = str(value or "").strip()
    folded = raw.casefold().replace("-", "_").replace(" ", "_")
    if source and folded in START_REF_ALIASES:
        return "start", f"normalized connection from_ref '{raw}' to 'start'"
    if not source and folded in END_REF_ALIASES:
        return "end", f"normalized connection to_ref '{raw}' to 'end'"
    return raw, None


def _normalize_connection_outcome(value: str, available: list[str]) -> tuple[str, str | None]:
    raw = str(value or "").strip()
    if not raw:
        return raw, None
    by_folded = {
        str(label).strip().casefold(): str(label).strip()
        for label in available
        if str(label).strip()
    }
    direct = by_folded.get(raw.casefold())
    if direct:
        return direct, None if direct == raw else f"normalized outcome '{raw}' to '{direct}'"
    for candidate in OUTCOME_ALIASES.get(raw.casefold(), ()):
        if candidate in by_folded:
            return by_folded[candidate], f"normalized outcome '{raw}' to '{by_folded[candidate]}'"
    return raw, None


def _preflight_connection_outcomes(
    step_items: list[tuple[str, str, dict]],
    clean_configs: dict[str, dict],
    conn_items: list[tuple[str, str, str]],
    existing_elements: list[dict] | None = None,
) -> tuple[str | None, str | None]:
    """Validate branch semantics without creating or mutating a workflow."""
    sources = {
        str(_element_id(element)): element
        for element in (existing_elements or [])
        if isinstance(element, dict) and _element_id(element) is not None
    }
    for index, (step_ref, step_type, _) in enumerate(step_items, start=10):
        sources[step_ref] = tb.build_element_create(
            step_type,
            index,
            clean_configs[step_ref],
            {"x": 0, "y": 0},
        )["data"]
    sources.setdefault("start", {"type": "workflow_start_point"})
    sources.setdefault("1", sources["start"])

    used_outcomes: set[tuple[str, str]] = set()
    for source_ref, _, requested_outcome in conn_items:
        source = sources.get(source_ref)
        if not source:
            continue
        source_type = source.get("type")
        is_branching = source_type in schema_registry.BRANCHING_TYPES
        outcomes = source.get("outcomes") or []
        available = [tb.outcome_label(item) for item in outcomes]
        available = [label for label in available if label]

        if not is_branching and requested_outcome:
            return (
                f"Step '{source_ref}' ({source_type}) does not branch; it takes no outcome.",
                None,
            )
        if not is_branching:
            continue
        if not requested_outcome and len(available) != 1:
            return (
                f"Step '{source_ref}' ({source_type}) is a branching step and requires an outcome.",
                f"Available outcomes: {available}",
            )

        outcome = requested_outcome or available[0]
        normalized_outcome, _ = _normalize_connection_outcome(outcome, available)
        matched = next(
            (label for label in available if label.casefold() == normalized_outcome.casefold()),
            None,
        )
        if matched is None:
            return (
                f"'{outcome}' is not an outcome on step '{source_ref}'. Available: {available}",
                None,
            )
        outcome_key = (source_ref, matched.casefold())
        if outcome_key in used_outcomes:
            return (
                f"Outcome '{matched}' on step '{source_ref}' is already used in this bulk update. "
                "A branching outcome can point to only one target.",
                None,
            )
        used_outcomes.add(outcome_key)
    return None, None


def _restore_workflow_status(
    client: JotformClient,
    workflow_id: str,
    previous_status: str | None,
    warnings: list[str],
) -> None:
    """Best-effort compensation when a graph write fails after disabling."""
    normalized = str(previous_status or "").strip().upper()
    if not normalized or normalized == "DISABLED":
        return
    try:
        client.update_workflow_metadata(workflow_id, status=normalized)
        warnings.append(f"Restored workflow status to {normalized} after the failed graph write.")
    except JotformAPIError as error:
        warnings.append(
            f"Could not restore the previous workflow status ({normalized}) after failure: {error}"
        )


def _draft_role_placeholder(step_ref: str, step_type: str, config: dict, field: str) -> str:
    text = " ".join(
        str(part or "")
        for part in (
            config.get("name"),
            config.get("label"),
            step_ref,
            schema_registry.default_label(step_type),
        )
    ).lower()
    for keywords, role in ROLE_PLACEHOLDER_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return f"{role}@{ROLE_PLACEHOLDER_DOMAIN}"
    role = "approver" if field == "approver" else "assignee"
    return f"{role}@{ROLE_PLACEHOLDER_DOMAIN}"


def _fill_draft_assignee_placeholders(step_ref: str, step_type: str, config: dict) -> tuple[dict, list[str]]:
    assignee_fields = ASSIGNEE_FIELDS_BY_STEP_TYPE.get(step_type, ())
    if not assignee_fields:
        return config, []
    normalized = dict(config)
    warnings = []
    for field in assignee_fields:
        if _has_value(normalized.get(field)):
            continue
        placeholder = _draft_role_placeholder(step_ref, step_type, normalized, field)
        normalized[field] = placeholder
        warnings.append(
            f"inserted draft placeholder {placeholder} for missing {field}; replace before publishing"
        )
    return normalized, warnings


def _normalize_contact_aliases(value):
    if isinstance(value, list):
        return [_normalize_contact_aliases(item) for item in value]
    if isinstance(value, dict):
        if "email" in value and not any(key in value for key in ("id", "text", "value")):
            return str(value.get("email") or "").strip()
        return value
    return value


def _normalize_step_config_aliases(step_type: str, config: dict) -> tuple[dict, list[str]]:
    """Accept compact model-friendly aliases and map them to Jotform fields."""
    canonical_type = schema_registry.resolve_step_type(step_type)["canonical_type"]
    normalized = dict(config or {})
    warnings = []

    def move(alias: str, target: str) -> None:
        if alias not in normalized or target in normalized:
            return
        normalized[target] = _normalize_contact_aliases(normalized.pop(alias))
        warnings.append(f"alias '{alias}' normalized to '{target}'")

    if canonical_type in ("workflow_send_email", "workflow_reminder_email"):
        move("recipient_email", "to")
        move("recipient", "to")
        move("recipients", "to")
        move("body", "content")
        move("message", "content")
    elif canonical_type == "workflow_approval":
        move("approver_email", "approver")
        move("approvers", "approver")
        move("description", "taskDescription")
        move("task_details", "taskDescription")
        move("body", "taskDescription")
    elif canonical_type in ("workflow_assign_task", "workflow_assign", "workflow_assign_form"):
        move("assignee_email", "assignee")
        move("assignees", "assignee")
        move("description", "taskDescription")
        move("task_details", "taskDescription")
        move("body", "taskDescription")

    return normalized, warnings


def _normalize_step_type_aliases(step_type: str, config: dict) -> tuple[str, list[str]]:
    """Repair common type/config mismatches that otherwise cause a schema retry."""
    canonical_type = schema_registry.resolve_step_type(step_type)["canonical_type"]
    warnings = []

    if (
        canonical_type == "workflow_conditional_branch"
        and isinstance(config, dict)
        and config.get("conditionTerms")
        and not config.get("outcomes")
    ):
        warnings.append(
            "step type 'workflow_conditional_branch' normalized to 'workflow_binary_decision' for TRUE/FALSE conditionTerms"
        )
        return "workflow_binary_decision", warnings

    return canonical_type, warnings


def _auto_default_required_step_details(step_ref: str, step_type: str, config: dict) -> tuple[dict, list[str]]:
    """Fill safe draft values for common missing details instead of burning an LLM retry."""
    canonical_type = schema_registry.resolve_step_type(step_type)["canonical_type"]
    normalized = dict(config or {})
    step_name = _human_step_name(step_ref, normalized)
    warnings = []

    def default(field: str, value) -> None:
        if _has_value(normalized.get(field)):
            return
        normalized[field] = value
        warnings.append(f"auto-filled missing '{field}' with a safe draft default")

    if canonical_type == "workflow_approval":
        default("name", step_name)
        default("approver", "approver@company.com")
        default("taskDescription", f"Review {step_name} and approve or deny it.")
    elif canonical_type == "workflow_send_email":
        default("name", step_name)
        default("to", "{Email Address}")
        default("subject", f"{step_name} Notification")
        default("content", f"<p>This is an automatic update for {step_name}.</p>")
    elif canonical_type == "workflow_assign_task":
        default("name", step_name)
        default("assignee", "assignee@company.com")
        default("taskDescription", f"Review {step_name} and complete this task.")

    return normalized, warnings


def _standard_draft_steps_and_connections(title: str) -> tuple[list[StepSpec], list[ConnectionSpec]]:
    workflow_name = title or "Draft Workflow"
    return (
        [
            StepSpec(
                ref="approval_1",
                type="workflow_approval",
                config={
                    "name": "Draft Approval",
                    "approver": "approver@company.com",
                    "taskDescription": f"Review {workflow_name} and approve or deny it.",
                },
            ),
            StepSpec(
                ref="email_approved",
                type="workflow_send_email",
                config={
                    "name": "Approved Notification",
                    "to": "{Email Address}",
                    "subject": f"{workflow_name} Approved",
                    "content": "<p>Your request has been approved.</p>",
                },
            ),
            StepSpec(
                ref="email_rejected",
                type="workflow_send_email",
                config={
                    "name": "Rejected Notification",
                    "to": "{Email Address}",
                    "subject": f"{workflow_name} Rejected",
                    "content": "<p>Your request was not approved.</p>",
                },
            ),
            StepSpec(
                ref="end_1",
                type="workflow_end_point",
                config={"name": "End"},
            ),
        ],
        [
            ConnectionSpec(from_ref="start", to_ref="approval_1"),
            ConnectionSpec(from_ref="approval_1", to_ref="email_approved", outcome="Approve"),
            ConnectionSpec(from_ref="approval_1", to_ref="email_rejected", outcome="Deny"),
            ConnectionSpec(from_ref="email_approved", to_ref="end_1"),
            ConnectionSpec(from_ref="email_rejected", to_ref="end_1"),
        ],
    )


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
            "Bind a trigger form first, then use get_workflow.trigger_form_fields or a visible field label.",
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
            "Use one of get_workflow.trigger_form_fields or ask the user which "
            f"field to use, then retry. Available fields: {available}"
        ),
    )


def _trigger_form_questions(client: JotformClient, workflow_id: str) -> tuple[str | None, dict, str | None]:
    try:
        combined = client.get_workflow_combined(workflow_id)
    except JotformAPIError as e:
        return None, {}, f"Could not read workflow trigger form: {e}"

    elements = [e for e in (combined.get("elements") or []) if isinstance(e, dict)]
    return _trigger_form_questions_from_elements(client, elements)


def _trigger_form_questions_from_elements(
    client: JotformClient,
    elements: list[dict],
) -> tuple[str | None, dict, str | None]:
    trigger_form_id = workflow_inspector.trigger_form_id(elements)
    if not trigger_form_id:
        return None, {}, "Workflow has no trigger form."

    try:
        questions = client.get_form_questions(trigger_form_id)
        if isinstance(questions, list):
            questions = {}
        return trigger_form_id, questions, None
    except JotformAPIError as e:
        return trigger_form_id, {}, f"Could not read trigger form fields: {e}"


def _assigned_form_questions_from_step_configs(
    client: JotformClient,
    step_items: list[tuple[str, str, dict]],
    clean_configs: dict[str, dict],
) -> tuple[str | None, dict, str | None]:
    form_refs: list[tuple[str, str]] = []
    for s_ref, s_type, _ in step_items:
        if s_type != "workflow_assign_form":
            continue
        form_id = str(clean_configs.get(s_ref, {}).get("formID") or "").strip()
        if form_id:
            form_refs.append((s_ref, form_id))

    unique_form_ids = sorted({form_id for _, form_id in form_refs})
    if not unique_form_ids:
        return None, {}, "Scheduled workflow has no assigned form field context."
    if len(unique_form_ids) > 1:
        refs = ", ".join(f"{ref}:{form_id}" for ref, form_id in form_refs)
        return None, {}, (
            "Multiple workflow_assign_form steps were provided, so field references "
            f"are ambiguous: {refs}."
        )

    form_id = unique_form_ids[0]
    try:
        questions = client.get_form_questions(form_id)
        if isinstance(questions, list):
            questions = {}
        return form_id, questions, None
    except JotformAPIError as e:
        return form_id, {}, f"Could not read assigned form {form_id} fields: {e}"


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
    qid = _question_id_by_token(questions, token)
    if qid and isinstance(questions.get(qid), dict):
        return str(questions[qid].get("name") or qid)
    return None


def _question_id_by_token(questions: dict, token: str) -> str | None:
    raw_wanted = token.strip().strip("{}")

    # create_form_with_ai returns these exact dictionary keys as field_id.
    # Give an exact field ID absolute precedence over aliases, names, labels,
    # and fuzzy matching so the decoupled two-step path is deterministic even
    # when another question happens to have a colliding name or label.
    for qid, question in questions.items():
        if isinstance(question, dict) and str(qid) == raw_wanted:
            return str(qid)

    wanted = raw_wanted.lower()
    normalized_wanted = _field_lookup_key(wanted)
    email_aliases = {"email", "emailaddress", "eposta", "epostaadresi", "mail"}

    if normalized_wanted in email_aliases:
        email_matches = [
            str(qid)
            for qid, question in questions.items()
            if isinstance(question, dict) and question.get("type") == "control_email"
        ]
        if len(email_matches) == 1:
            return email_matches[0]

    exact_matches: list[str] = []
    fuzzy_matches: list[str] = []
    for qid, question in questions.items():
        if not isinstance(question, dict):
            continue
        raw_candidates = [
            str(qid).strip().lower(),
            str(question.get("qid") or "").strip().lower(),
            str(question.get("name") or "").strip().lower(),
            str(question.get("text") or "").strip().lower(),
        ]
        candidates = {candidate for candidate in raw_candidates if candidate}
        if wanted in candidates:
            exact_matches.append(str(qid))
            continue
        normalized_candidates = {
            _field_lookup_key(candidate) for candidate in candidates
        }
        if normalized_wanted in normalized_candidates:
            exact_matches.append(str(qid))
            continue
        if len(normalized_wanted) >= 4 and any(
            normalized_wanted in candidate or candidate in normalized_wanted
            for candidate in normalized_candidates
            if candidate
        ):
            fuzzy_matches.append(str(qid))

    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        return None
    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0]
    return None


def _field_lookup_key(value: str) -> str:
    turkish_translations = str.maketrans({
        "ı": "i",
        "İ": "I",
        "ğ": "g",
        "Ğ": "G",
        "ü": "u",
        "Ü": "U",
        "ş": "s",
        "Ş": "S",
        "ö": "o",
        "Ö": "O",
        "ç": "c",
        "Ç": "C",
    })
    ascii_value = (
        unicodedata.normalize("NFKD", value.translate(turkish_translations))
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return re.sub(r"[^a-z0-9]+", "", ascii_value.lower())


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


def _recipient_input_items(value) -> list:
    if isinstance(value, list):
        items = []
        for item in value:
            items.extend(_recipient_input_items(item))
        return items
    if isinstance(value, str):
        parts = [
            part.strip()
            for part in re.split(r"[,;]", value)
            if part.strip()
        ]
        return parts or [value]
    return [value]


def _normalize_condition_field_tokens(
    config: dict,
    step_type: str,
    questions: dict,
) -> tuple[dict, str | None, str | None]:
    terms = workflow_inspector.extract_condition_terms(step_type, config)
    if not terms:
        return config, None, None

    normalized = deepcopy(config)
    changed: list[str] = []

    def resolve(raw_field: object) -> str | None:
        raw = str(raw_field or "").strip()
        if not raw:
            return None
        return _question_id_by_token(questions, raw)

    def normalize_term(term: dict, path: str) -> str | None:
        raw_field = term.get("field")
        resolved = resolve(raw_field)
        if resolved:
            if str(raw_field) != resolved:
                term["field"] = resolved
                changed.append(f"{path}.field {raw_field!r}->{resolved!r}")
            return None
        available = [
            f"{qid}: {q.get('text')}"
            for qid, q in questions.items()
            if isinstance(q, dict)
        ]
        return (
            f"{path}.field={raw_field!r} does not match a trigger form field. "
            f"Available fields: {available}"
        )

    if step_type == "workflow_binary_decision":
        for idx, term in enumerate(normalized.get("conditionTerms") or [], start=1):
            if isinstance(term, dict):
                error = normalize_term(term, f"conditionTerms[{idx}]")
                if error:
                    return normalized, None, error

    if step_type == "workflow_conditional_branch":
        for outcome_idx, outcome in enumerate(normalized.get("outcomes") or [], start=1):
            if not isinstance(outcome, dict):
                continue
            label = tb.outcome_label(outcome) or f"outcome {outcome_idx}"
            for term_idx, term in enumerate(outcome.get("conditionTerms") or [], start=1):
                if isinstance(term, dict):
                    error = normalize_term(
                        term,
                        f"outcomes[{label}].conditionTerms[{term_idx}]",
                    )
                    if error:
                        return normalized, None, error

    hint = (
        "Normalized condition field references from trigger form: "
        + "; ".join(changed)
        if changed else None
    )
    return normalized, hint, None


def _normalize_assignee_fields(
    client: JotformClient,
    workflow_id: str,
    config: dict,
    fields: tuple[str, ...],
    trigger_context: tuple[str | None, dict, str | None] | None = None,
    context_label: str = "trigger form",
) -> tuple[dict, str | None, str | None]:
    if not any(field in config for field in fields):
        return config, None, None

    trigger_form_id, questions, trigger_error = (
        trigger_context if trigger_context is not None else _trigger_form_questions(client, workflow_id)
    )
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
        items = _recipient_input_items(value)
        next_items = []
        for item in items:
            if isinstance(item, dict) and item.get("isQuestion") is True:
                next_items.append(item)
                continue

            raw = ""
            if isinstance(item, dict):
                raw = str(item.get("value") or item.get("email") or item.get("text") or item.get("id") or "").strip()
            elif isinstance(item, str):
                raw = item.strip()

            if not raw:
                next_items.append(item)
                continue

            # 1. Match against form email questions using token/label/qid
            matched_qid = _question_id_by_token(email_questions, raw) or _question_id_by_token(questions, raw)
            if not matched_qid and len(email_questions) == 1 and any(
                token in raw.lower() for token in ("email", "posta", "mail", "musteri", "müşteri", "submitter", "applicant")
            ):
                matched_qid = next(iter(email_questions.keys()))

            if matched_qid and matched_qid in questions:
                q = questions[matched_qid]
                next_items.append(_email_field_reference(matched_qid, q, form_title))
                changed = True
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

            # 2. Match valid email address string
            clean_raw = raw.strip("{}").strip()
            if EMAIL_RE.match(raw) or EMAIL_RE.match(clean_raw):
                next_items.append(_fixed_email_reference(clean_raw if EMAIL_RE.match(clean_raw) else raw))
                changed = True
                continue

            # 3. Fallback fixed draft email reference for draft placeholder tokens like {Advisor Email}
            if raw.startswith("{") and raw.endswith("}"):
                clean_slug = re.sub(r"[^a-zA-Z0-9]+", ".", clean_raw).strip(".")
                draft_email = f"{clean_slug}@draft.internal" if clean_slug else f"{field}@draft.internal"
                next_items.append(_fixed_email_reference(draft_email))
                changed = True
                continue

            if trigger_error and not questions:
                return normalized, None, trigger_error
            return normalized, None, (
                f"{field} must be a valid email address or a real email field "
                f"from the {context_label}; got {raw!r}."
            )
        normalized[field] = next_items

    hint = (
        f"Normalized assignee/approver fields using builder recipient shape"
        f"{' and ' + context_label + ' ' + trigger_form_id if trigger_form_id else ''}."
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
    lower = stripped.lower()
    if "<html" in lower or "<body" in lower:
        return content
    if re.search(r"</?(p|div|br|table|ul|ol|li|span|strong|em|b|i|a|h[1-6])\b", lower):
        return "<!DOCTYPE html>\n<html>\n<head>\n</head>\n<body>\n" + stripped + "\n</body>\n</html>"
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
    trigger_context: tuple[str | None, dict, str | None] | None = None,
    context_label: str = "trigger form",
) -> tuple[dict, str | None, str | None]:
    normalized = {**EMAIL_MODAL_DEFAULTS, **config}
    hint_parts = []
    trigger_form_id, questions, _ = (
        trigger_context if trigger_context is not None else _trigger_form_questions(client, workflow_id)
    )
    if isinstance(normalized.get("subject"), str) and normalized["subject"].strip() and questions:
        normalized_subject, changed = _normalize_content_field_tokens(
            normalized["subject"], questions
        )
        if changed:
            normalized["subject"] = normalized_subject
            hint_parts.append(
                f"normalized email subject field tokens from {context_label} {trigger_form_id}"
            )
    if isinstance(normalized.get("content"), str) and normalized["content"].strip():
        html_content = _html_email_content(normalized["content"])
        if html_content != normalized["content"]:
            normalized["content"] = html_content
            hint_parts.append("wrapped plain text email content as HTML")
        if questions:
            normalized_content, changed = _normalize_content_field_tokens(
                normalized["content"], questions
            )
            if changed:
                normalized["content"] = normalized_content
                hint_parts.append(
                    f"normalized email content field tokens from {context_label} {trigger_form_id}"
                )

    normalized, recipient_hint, recipient_error = _normalize_email_recipients(
        client, workflow_id, normalized, trigger_context, context_label
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
    trigger_context: tuple[str | None, dict, str | None] | None = None,
    context_label: str = "trigger form",
) -> tuple[dict, str | None, str | None]:
    recipient_fields = ("to", "replyTo", "cc", "bcc")
    if not any(field in config for field in recipient_fields):
        return config, None, None

    trigger_form_id, questions, error = (
        trigger_context if trigger_context is not None else _trigger_form_questions(client, workflow_id)
    )
    fallback_note = None
    if error and not questions:
        fallback_note = error

    email_questions = {
        str(qid): q for qid, q in questions.items()
        if isinstance(q, dict) and q.get("type") == "control_email"
    }
    all_questions = {
        str(qid): q for qid, q in questions.items()
        if isinstance(q, dict)
    }

    form_title = None
    for question in questions.values():
        if isinstance(question, dict) and question.get("type") == "control_head":
            form_title = question.get("text")
            break

    normalized = dict(config)
    changed = False
    used_question_ref = False
    for field in recipient_fields:
        value = normalized.get(field)
        if value is None:
            continue
        items = _recipient_input_items(value)
        next_recipients = []
        for item in items:
            if isinstance(item, dict) and item.get("isQuestion") is True and item.get("value"):
                next_recipients.append(item)
                continue

            raw = ""
            if isinstance(item, dict):
                raw = str(item.get("id") or item.get("email") or item.get("text") or item.get("value") or item.get("name") or "").strip()
            elif isinstance(item, str):
                raw = item.strip()

            if not raw:
                continue

            # 1. Match against form email questions using token/label/qid
            matched_qid = _question_id_by_token(email_questions, raw) or _question_id_by_token(all_questions, raw)
            if not matched_qid and len(email_questions) == 1 and any(
                token in raw.lower() for token in ("email", "posta", "mail", "musteri", "müşteri", "submitter", "applicant")
            ):
                matched_qid = next(iter(email_questions.keys()))

            if matched_qid and matched_qid in all_questions:
                q = all_questions[matched_qid]
                next_recipients.append(_email_field_reference(matched_qid, q, form_title))
                changed = True
                used_question_ref = True
                continue

            # 2. Match valid email address string
            if EMAIL_RE.match(raw):
                next_recipients.append(_fixed_email_reference(raw))
                changed = True
                continue

            # 3. Unknown field-looking tokens must not be guessed.
            if raw.startswith("{") and raw.endswith("}"):
                if error and not questions:
                    return normalized, None, error
                return normalized, None, (
                    f"{field} field token {raw!r} does not match a real {context_label} email field. "
                    "Use the exact field_id, name, or label returned by create_form_with_ai/get_workflow."
                )

            # 4. Fallback fixed reference
            next_recipients.append(_fixed_email_reference(raw if "@" in raw else f"{raw}@draft.internal"))
            changed = True

        normalized[field] = next_recipients

    if used_question_ref and trigger_form_id:
        hint = f"Normalized recipient field references from {context_label} {trigger_form_id}."
    elif changed:
        hint = "Normalized fixed recipient addresses."
    else:
        hint = None
    if fallback_note and changed:
        hint = (hint + " " if hint else "") + f"Used draft recipient fallback because {fallback_note}"
    return normalized, hint, None


def _normalize_field_dependent_step_configs(
    client: JotformClient,
    workflow_id: str,
    step_items: list[tuple[str, str, dict]],
    clean_configs: dict[str, dict],
    field_context: tuple[str | None, dict, str | None],
    *,
    context_label: str,
) -> tuple[dict[str, dict], list[str], str | None, str | None]:
    context_form_id, questions, context_error = field_context
    normalized_configs = dict(clean_configs)
    warnings: list[str] = []

    for s_ref, s_type, _ in step_items:
        clean_cfg = normalized_configs[s_ref]

        if questions:
            clean_cfg, condition_hint, condition_error = _normalize_condition_field_tokens(
                clean_cfg,
                s_type,
                questions,
            )
            if condition_error:
                return normalized_configs, warnings, f"Step '{s_ref}': {condition_error}", None
            if condition_hint:
                warnings.append(f"[{s_ref}] {condition_hint}")

            invalid = workflow_inspector.invalid_field_references(
                clean_cfg,
                s_type,
                {str(qid) for qid in questions},
            )
            if invalid:
                return normalized_configs, warnings, (
                    f"Step '{s_ref}': condition fields must match {context_label} "
                    f"fields; invalid references: {', '.join(invalid)}."
                ), None
        elif workflow_inspector.extract_condition_terms(s_type, clean_cfg):
            if context_error:
                return normalized_configs, warnings, f"Step '{s_ref}': {context_error}", None
            return normalized_configs, warnings, (
                f"Step '{s_ref}': condition fields cannot be verified because "
                f"{context_label} fields are unavailable."
            ), None

        assignee_fields = ASSIGNEE_FIELDS_BY_STEP_TYPE.get(s_type, ())
        if assignee_fields:
            clean_cfg, assignee_hint, assignee_error = _normalize_assignee_fields(
                client,
                workflow_id,
                clean_cfg,
                assignee_fields,
                field_context,
                context_label,
            )
            if assignee_error:
                return normalized_configs, warnings, f"Step '{s_ref}': {assignee_error}", None
            if assignee_hint:
                warnings.append(f"[{s_ref}] {assignee_hint}")

        if s_type in ("workflow_send_email", "workflow_reminder_email"):
            clean_cfg, recipient_hint, recipient_error = _normalize_email_config(
                client,
                workflow_id,
                clean_cfg,
                field_context,
                context_label,
            )
            if recipient_error:
                return normalized_configs, warnings, f"Step '{s_ref}': {recipient_error}", None
            if recipient_hint:
                warnings.append(f"[{s_ref}] {recipient_hint}")

        normalized_configs[s_ref] = clean_cfg

    hint = None
    if context_error and not questions:
        hint = context_error
    elif context_form_id:
        hint = f"Used {context_label} {context_form_id} as the form-field context."
    return normalized_configs, warnings, None, hint


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


def _extract_ai_form_title(content: dict, questions: dict) -> str | None:
    """Return the best available form title from an AI/public API response."""
    for key in ("title", "form_title", "name"):
        value = content.get(key)
        if value:
            return str(value)
    for question in questions.values():
        if (
            isinstance(question, dict)
            and question.get("type") == "control_head"
            and question.get("text")
        ):
            return str(question["text"])
    return None


def _normalize_ai_form_summary(summary: object) -> str | None:
    if summary is None:
        return None
    text = str(summary).strip()
    if not text:
        return None
    return re.sub(
        r"\s*what do you want to do next\??\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


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


for _traced_helper_name in (
    "_changed_affected_step_ids",
    "_normalize_schedule_config",
    "_ensure_new_workflow_reachable_from_start",
    "_find_duplicate_step",
    "_missing_required_step_details",
    "_normalize_connection_ref",
    "_normalize_connection_outcome",
    "_fill_draft_assignee_placeholders",
    "_normalize_step_config_aliases",
    "_normalize_step_type_aliases",
    "_trigger_form_questions",
    "_trigger_form_questions_from_elements",
    "_assigned_form_questions_from_step_configs",
    "_question_id_by_token",
    "_normalize_content_field_tokens",
    "_normalize_condition_field_tokens",
    "_normalize_assignee_fields",
    "_normalize_email_config",
    "_normalize_email_recipients",
    "_normalize_field_dependent_step_configs",
    "_merge_outcome_updates",
    "_extract_ai_form_id",
    "_extract_ai_form_title",
    "_normalize_ai_form_summary",
    "_bind_and_verify_trigger",
):
    globals()[_traced_helper_name] = audit_log.trace_function(globals()[_traced_helper_name])


def register(mcp: MCPServer, client: JotformClient) -> None:
    @mcp.tool()
    def create_form_with_ai(
        prompt: Annotated[str, Field(
            description=(
                "Call this only after search_workflow_templates when building a "
                "new workflow that needs an AI-generated form: a trigger form for "
                "form-submission workflows, or an assigned form for scheduled "
                "workflows. This is the first write, not the first tool call. Use "
                "this MCP workflow tool for that write. Do not use external "
                "Jotform form plugins/tools for workflow requests; they do not "
                "return this server's field contract. Describe the form's purpose, "
                "language, and only the essential intake fields (maximum 8). Omit workflow "
                "steps, routing, notifications, styling, and long explanations. The result includes form_id and exact "
                "fields, including field names for email variables, for the subsequent "
                "build_workflow_bulk call. For scheduled "
                "workflows, use the returned form_id as workflow_assign_form.formID, "
                "not trigger_form_id."
            )
        )],
        form_type: Annotated[str, Field(
            description='Form type preference for Jotform AI. Default "classic".'
        )] = "classic",
        language: Annotated[str, Field(
            description='Form language preference. Default "en"; use "tr" when the user wants Turkish.'
        )] = "en",
        operation_id: Annotated[str, Field(
            description=(
                "Stable unique ID for this form-creation intent. Reuse the same value when retrying "
                "the same request so a timeout or model retry cannot create another form."
            )
        )] = "",
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
    ) -> CreateAIFormResult:
        """
        Create a new Jotform form from an AI prompt.

        Call this only after search_workflow_templates when building a new
        workflow that needs an AI-generated form. This is the first write, not
        the first tool call. For form-submission workflows, the form becomes
        trigger_form_id. For scheduled workflows, the form is assigned after the
        schedule starts with workflow_assign_form.formID. Do not use external
        Jotform form plugins/tools for workflow requests; they do not return
        this server's field contract or stay inside the workflow audit/build
        chain. It returns form_id plus the exact field_id, name, label, type,
        required, and options values for the subsequent build_workflow_bulk call. Use
        field name values inside email subject/content variables and recipient
        field references. No
        separate form-field lookup is needed. This form-only result is not
        complete when the user requested a workflow; if template search has not
        already happened, call search_workflow_templates before build_workflow_bulk.
        """
        operation_id = str(operation_id or "").strip()
        if len(operation_id) > 120:
            return CreateAIFormResult(error="operation_id must be 120 characters or fewer.")
        try:
            create_kwargs = {"form_type": form_type, "language": language}
            if operation_id:
                create_kwargs["operation_id"] = operation_id
            content = client.create_form_with_ai(prompt, **create_kwargs)
        except JotformAPIError as e:
            return CreateAIFormResult(error=str(e))

        form_id = _extract_ai_form_id(content)
        if not form_id:
            return CreateAIFormResult(error=f"No form id in AI form response: {content!r}")

        questions = content.get("questions") if isinstance(content.get("questions"), dict) else {}
        title = _extract_ai_form_title(content, questions)
        verified = bool(content.get("verified", not content.get("ai_fallback")))
        return CreateAIFormResult(
            form_id=form_id,
            form_url=_form_url(form_id),
            title=title,
            generation_mode="fallback" if content.get("ai_fallback") else "copilot",
            fallback_used=bool(content.get("ai_fallback")),
            fallback_reason=content.get("fallback_reason"),
            verified=verified,
            summary=_normalize_ai_form_summary(content.get("summary")),
            questions=questions,
            fields=form_fields_from_questions(questions),
            next_required_tool="build_workflow_bulk",
            hint=(
                "Workflow request is not complete yet. Use the already retrieved template blueprint "
                "when choosing steps and connections. For a form-submission workflow, next call "
                f"build_workflow_bulk(title=..., trigger_form_id='{form_id}', steps=[...], connections=[...]). "
                "The bulk tool will read and validate the form fields again before creating the workflow. "
                "For a scheduled workflow that assigns this form after the schedule starts, next call "
                f"build_workflow_bulk(trigger_type='schedule', trigger_schedule=..., steps=[workflow_assign_form.formID='{form_id}', ...], connections=[...]). "
                "Then call show_workflow before answering the user."
            ),
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
                "with the form_id returned by create_form_with_ai instead of "
                "this low-level tool. "
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

        For ordinary new-workflow requests, first call create_form_with_ai,
        then call build_workflow_bulk with title/trigger_form_id so workflow
        creation and step wiring happen in one bulk write. Use this tool only
        for manual/partial setup.

        1. Use an existing form — then call list_forms and pass the chosen
           form id as trigger_form_id.
        2. Create a new form first — call create_form_with_ai, then pass its
           form_id to build_workflow_bulk as trigger_form_id.
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
                    "chosen form id as trigger_form_id. New form: call "
                    "create_form_with_ai first, then use build_workflow_bulk "
                    "with title, trigger_form_id, steps, and connections. Only use "
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

        For complete new workflows, prefer create_form_with_ai followed by one
        build_workflow_bulk call with title, trigger_form_id, steps, and
        connections. Use this hidden compatibility helper only for legacy flows.
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
        form_title = _extract_ai_form_title(form_content, questions)
        return CreateWorkflowWithAIFormResult(
            workflow_id=str(workflow_id),
            workflow_url=_workflow_url(str(workflow_id)),
            title=title,
            trigger_form_id=form_id,
            trigger_form_url=_form_url(form_id),
            form_title=form_title,
            form_summary=form_content.get("summary"),
            questions=questions,
            fields=form_fields_from_questions(questions),
        )

    @mcp.tool()
    def build_workflow_bulk(
        workflow_id: Annotated[str, Field(
            description=(
                "Optional. ID of an existing workflow to update or add/delete steps. If omitted, "
                "a new workflow is created automatically using title plus either "
                "trigger_form_id for form-submission workflows or trigger_type='schedule' "
                "with trigger_schedule for scheduled workflows."
            )
        )] = "",
        *,
        steps: Annotated[list[StepSpec], Field(
            default=[],
            description=(
                "List of steps to create. Each step has a unique 'ref' name (e.g. 'approval_1', 'notify_mgr', 'reject_email'), "
                "'type' (e.g. 'workflow_approval', 'workflow_send_email', 'workflow_conditional_branch'), and 'config' dict. "
                "Can be empty when only deleting steps via delete_step_ids."
            )
        )] = [],
        connections: Annotated[list[ConnectionSpec], Field(
            default=[],
            description=(
                "List of connections between steps. 'from_ref' can be 'start' (or '1') for the trigger form, "
                "any new step 'ref', or an existing step_id when workflow_id is provided. 'to_ref' is the target "
                "step's 'ref' or an existing step_id. 'outcome' is required for branching steps "
                "(e.g. 'Approve', 'Deny', 'TRUE', 'FALSE', or branch name). When inserting before an existing END, "
                "reuse that END step_id as the final to_ref instead of creating a duplicate END."
            )
        )] = [],
        step_updates: Annotated[list[StepUpdateSpec], Field(
            default=[],
            description=(
                "Existing step configuration edits to apply in the same updateTree write. "
                "Each item needs numeric step_id from get_workflow and a config dict."
            ),
        )] = [],
        delete_step_ids: Annotated[list[str], Field(
            default=[],
            description=(
                "Optional list of existing step IDs to delete from this workflow (e.g. ['8', '9']). "
                "All incident links touching these steps are deleted automatically, and any parent "
                "branch outcomes are cleaned up or rewired."
            )
        )] = [],
        delete_link_ids: Annotated[list[str], Field(
            default=[],
            description=(
                "Optional existing connection IDs to remove in the same updateTree write. "
                "Primarily used by semantic Canvas diffs and connection rewires."
            ),
        )] = [],
        title: Annotated[str, Field(
            description="Optional. Name of the workflow when creating a new one."
        )] = "",
        trigger_form_id: Annotated[str, Field(
            description=(
                "Primary trigger binding for a new form-submission workflow. Pass the form_id "
                "obtained from this MCP server's create_form_with_ai, or an existing form ID "
                "explicitly selected by the user, along with the complete steps and connections. "
                "Leave blank for scheduled workflows; assigned forms belong in workflow_assign_form "
                "step config as formID. Do not source this from an external Jotform AI form plugin "
                "during workflow creation."
            )
        )] = "",
        confirm_orphaned_downstream: Annotated[bool, Field(
            description=(
                "Set true only after the user explicitly confirms that downstream child "
                "steps may be left disconnected/unreachable by this delete."
            )
        )] = False,
        trigger_type: Annotated[str, Field(
            description="Type of the trigger. Default is 'form'. Set to 'schedule' for scheduled workflows."
        )] = "form",
        trigger_schedule: Annotated[dict | None, Field(
            default=None,
            description=(
                "Required if trigger_type is 'schedule'. "
                "The server automatically handles timezone resolution (by fetching the user's "
                "Jotform profile timezone) and UTC date math. "
                "To schedule for a specific day/time (e.g., 'every Friday at 17:00'), "
                "simply provide `schedule__days` (e.g. 'Friday'), `schedule__time` (e.g. '17:00'), "
                "and the interval config (`schedule__executeWhen__afterAmount`='1', `schedule__executeWhen__afterUnit`='week', "
                "`schedule__end__recurring`='none'). "
                "Do NOT stop to ask the user for their timezone. Submit the schedule to the tool first; "
                "the server will fetch it from Jotform. Only ask the user for a timezone if this tool returns "
                "a validation error explicitly complaining about a missing timezone."
            )
        )] = None,
        expected_revision_id: Annotated[str, Field(
            description=(
                "Optimistic-lock token from get_workflow.revision_id or show_workflow.data.revision_id "
                "when mutating an existing workflow. Prefer passing it. If omitted, this tool reads a fresh "
                "live snapshot and guards the final write with that snapshot."
            )
        )] = "",
        base_updated_at: Annotated[str, Field(
            description="Optional get_workflow.updated_at fallback for timestamp-based clients."
        )] = "",
        operation_id: Annotated[str, Field(
            description=(
                "Stable unique ID for this workflow mutation intent. Reuse it for retries of the "
                "same request; completed or partially-created operations are replayed instead of duplicated."
            )
        )] = "",
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
    ) -> BuildWorkflowBulkResult:
        """
        Create or mutate a workflow graph with one final updateTree write.

        New form-submission flow: search_workflow_templates -> create_form_with_ai -> build_workflow_bulk -> show_workflow.
        New scheduled flow: search_workflow_templates -> build_workflow_bulk(trigger_type="schedule", trigger_schedule=...) -> show_workflow.
        New scheduled assigned-form flow: search_workflow_templates -> create_form_with_ai -> build_workflow_bulk(trigger_type="schedule", trigger_schedule=..., workflow_assign_form.formID=...) -> show_workflow.
        Do not use external Jotform form plugins/tools for the AI trigger form.
        Existing workflow flow: get_workflow -> build_workflow_bulk -> show_workflow.
        Use step_updates for existing configuration edits and steps for new nodes.
        If the user asks to add a 3rd-party integration (Slack, WhatsApp,
        Zendesk, Asana, Google Sheets, Microsoft Teams, etc.), you MUST add it
        as a blank shell step: set type="workflow_integration", set StepSpec
        subType to the specific supported integration ID, and DO NOT fill any
        authentication, OAuth, account, mapping, channel, project, ticket, or
        message configuration fields. The user completes settings in Jotform UI.
        Missing content/subject/body/outcome config is an error; the server validates
        but does not invent email content or fallback graph nodes for you. Draft
        reasonable content from the user's request and template blueprint. For new draft workflows,
        missing staff approvers/assignees are filled with reserved role placeholders
        such as hr@workflow.invalid or manager@workflow.invalid. Do not ask the user
        solely for draft staff emails. Alias normalization is limited to equivalent
        field names. Deletes that would orphan downstream nodes return a preview and
        require explicit confirmation. Every bulk write leaves the workflow
        DISABLED, including edits to an existing workflow; publish_workflow is
        only for a later explicit user request to enable it.

        For common workflows, do not call list_step_types or get_step_schema first.
        Use these known configs directly:
        approval: type=workflow_approval, config has name, approver=hr@workflow.invalid if unknown, taskDescription.
        task: type=workflow_assign_task, config has name, assignee=manager@workflow.invalid if unknown, taskDescription, outcomes=["Complete"].
        assign form: type=workflow_assign_form, config has name, formID, assignee, requireLogin="Yes".
        Use assign form to add a form after a scheduled start; do not bind that form as trigger_form_id.
        integration shell: type=workflow_integration, subType is one of the supported IDs; config is empty or name only.
        email: type=workflow_send_email, config has name, to, subject, content. Use a trigger-form email field for applicant/customer notifications.
        binary branch: type=workflow_binary_decision with conditionTerms and TRUE/FALSE connections.
        Choose the number of steps from the user's domain and detail level; do not follow a fixed
        count. Include intake/receipt notification, review/approval/task paths, parallel work,
        escalation, and outcome notifications only when they are useful.
        """
        workflow_id = str(workflow_id or "").strip()
        operation_id = str(operation_id or "").strip()
        if len(operation_id) > 120:
            return BuildWorkflowBulkResult(error="operation_id must be 120 characters or fewer.")
        title = str(title or "").strip()
        trigger_form_id = str(trigger_form_id or "").strip()
        trigger_type = str(trigger_type or "form").strip().lower()
        if len(title) > 200:
            return BuildWorkflowBulkResult(error="title must be 200 characters or fewer.")
        if len(steps or []) > 100:
            return BuildWorkflowBulkResult(error="A bulk write can create at most 100 steps.")
        if len(connections or []) > 300:
            return BuildWorkflowBulkResult(error="A bulk write can create at most 300 connections.")
        if len(step_updates or []) > 100:
            return BuildWorkflowBulkResult(error="A bulk write can update at most 100 steps.")
        if len(delete_step_ids or []) > 200 or len(delete_link_ids or []) > 300:
            return BuildWorkflowBulkResult(error="The bulk delete request exceeds the allowed operation limit.")
        if trigger_type not in {"form", "schedule"}:
            return BuildWorkflowBulkResult(error="trigger_type must be either 'form' or 'schedule'.")
        creating_new_workflow = not workflow_id
        normalized_delete_ids = [str(sid).strip() for sid in (delete_step_ids or []) if str(sid).strip()]
        normalized_delete_link_ids = [
            str(link_id).strip()
            for link_id in (delete_link_ids or [])
            if str(link_id).strip()
        ]
        deleted_set = set(normalized_delete_ids)
        warnings: list[str] = []
        update_items: list[tuple[str, dict]] = []
        seen_update_ids: set[str] = set()
        for update in step_updates or []:
            step_id = str(
                getattr(update, "step_id", None)
                or (update.get("step_id") if isinstance(update, dict) else "")
                or ""
            ).strip()
            config = getattr(update, "config", None) if not isinstance(update, dict) else update.get("config")
            if not step_id or not step_id.isdigit():
                return BuildWorkflowBulkResult(error="Every step_updates item needs a numeric step_id from get_workflow.")
            if step_id in seen_update_ids:
                return BuildWorkflowBulkResult(error=f"Duplicate step update for step_id '{step_id}'.")
            if not isinstance(config, dict) or not config:
                return BuildWorkflowBulkResult(error=f"Step update '{step_id}' needs at least one config field.")
            seen_update_ids.add(step_id)
            update_items.append((step_id, dict(config)))

        if (
            creating_new_workflow
            and not trigger_form_id
            and trigger_type != "schedule"
            and not normalized_delete_ids
            and not normalized_delete_link_ids
        ):
            return BuildWorkflowBulkResult(
                warnings=warnings,
                error=(
                    "trigger_form_id is required for new form-submission workflows. "
                    "Start with search_workflow_templates, then call create_form_with_ai "
                    "and pass its form_id here."
                ),
                hint="Use search_workflow_templates(query=...) -> create_form_with_ai(prompt=...) -> build_workflow_bulk(title=..., trigger_form_id=..., steps=..., connections=...).",
            )
        if not steps and not update_items and not normalized_delete_ids and not normalized_delete_link_ids and not connections:
            return BuildWorkflowBulkResult(
                error=(
                    "No steps provided to build_workflow_bulk. For a new workflow, start with "
                    "search_workflow_templates and create_form_with_ai if a new trigger or assigned "
                    "form is needed, then retry with complete steps and connections. Existing "
                    "workflows may instead provide step_updates or delete IDs."
                )
            )
        if update_items and not workflow_id:
            return BuildWorkflowBulkResult(error="step_updates requires an existing workflow_id.")
        if (normalized_delete_ids or normalized_delete_link_ids) and not workflow_id:
            return BuildWorkflowBulkResult(
                error="delete_step_ids and delete_link_ids require workflow_id to be provided."
            )
        lock_snapshot: dict | None = None
        base_snapshot: dict | None = None
        lock_revision_id: str | None = None
        lock_updated_at: str | None = None
        stale_lock_conflict: ConflictError | None = None
        if workflow_id:
            base_snapshot = sync_state.load_workflow_snapshot(
                workflow_id,
                revision_id=expected_revision_id or None,
                updated_at=base_updated_at or None,
            )
            try:
                lock = client.assert_workflow_revision(
                    workflow_id,
                    expected_revision_id=expected_revision_id or None,
                    base_updated_at=base_updated_at or None,
                )
                lock_snapshot = lock.get("snapshot") if isinstance(lock, dict) else None
                lock_revision_id = lock.get("revision_id") if isinstance(lock, dict) else None
                lock_updated_at = lock.get("updated_at") if isinstance(lock, dict) else None
                if not (expected_revision_id or base_updated_at):
                    warnings.append(
                        "No revision token was supplied; validated references against a fresh live snapshot "
                        "and guarded the final write with that snapshot's revision."
                    )
            except ConflictError as e:
                stale_lock_conflict = e
                lock_snapshot = getattr(e, "current_snapshot", None)
                lock_revision_id = e.current_revision_id
                lock_updated_at = e.current_updated_at
                if lock_snapshot is None:
                    try:
                        lock_snapshot = client.get_workflow_combined(workflow_id)
                        lock_revision_id = workflow_revision_id(lock_snapshot)
                        lock_updated_at = workflow_updated_at(lock_snapshot)
                    except JotformAPIError as api_error:
                        return BuildWorkflowBulkResult(
                            workflow_id=workflow_id,
                            workflow_url=_workflow_url(workflow_id),
                            conflict=True,
                            error=f"{e} Also could not reload the live workflow for scoped conflict analysis: {api_error}",
                            hint=_conflict_hint(),
                            warnings=warnings,
                        )
                if base_snapshot:
                    warnings.append(
                        "Workflow revision was stale; reloaded the latest live graph and rebased this bulk update automatically."
                    )
                else:
                    warnings.append(
                        "Workflow revision was stale and no matching session snapshot was available; "
                        "reloaded the latest live graph and recalculated this bulk update automatically."
                    )

        if workflow_id and trigger_type == "form" and not trigger_form_id:
            snapshot_elements = (
                lock_snapshot.get("elements")
                if isinstance(lock_snapshot, dict) and isinstance(lock_snapshot.get("elements"), list)
                else []
            )
            start_element = next(
                (
                    element for element in snapshot_elements
                    if isinstance(element, dict)
                    and (
                        str(element.get("element_id")) == "1"
                        or element.get("type") == "workflow_start_point"
                    )
                ),
                None,
            )
            if workflow_inspector.is_schedule_start_point(start_element):
                trigger_type = "schedule"
                warnings.append(
                    "Detected an existing schedule start point; treating this bulk update as trigger_type='schedule'."
                )

        # 1. Check uniqueness of step refs
        step_items: list[tuple[str, str, dict]] = []
        seen_refs: set[str] = set()
        for s in steps:
            s_ref = str(getattr(s, "ref", None) or (s.get("ref") if isinstance(s, dict) else "") or "").strip()
            s_type = str(getattr(s, "type", None) or (s.get("type") if isinstance(s, dict) else "") or "").strip()
            s_config = getattr(s, "config", None) if not isinstance(s, dict) else s.get("config")
            s_config = dict(s_config or {})
            s_subtype = getattr(s, "subType", None) if not isinstance(s, dict) else (s.get("subType") or s.get("subtype"))
            if s_subtype not in (None, ""):
                s_config.setdefault("subType", str(s_subtype).strip())
            if not s_ref:
                return BuildWorkflowBulkResult(error="Every step in steps must have a non-empty 'ref'.")
            if s_ref in seen_refs:
                return BuildWorkflowBulkResult(error=f"Duplicate step ref '{s_ref}' found in steps list.")
            if s_ref.lower() in ("start", "1"):
                return BuildWorkflowBulkResult(error=f"Step ref '{s_ref}' is reserved for the trigger form start point.")
            if s_ref.isdigit():
                return BuildWorkflowBulkResult(
                    error=(
                        f"Step ref '{s_ref}' is invalid. Numeric refs are reserved for existing "
                        "Jotform step IDs in connections; use a semantic ref like "
                        "'finance_check' or 'notify_employee' for new steps."
                    )
                )
            seen_refs.add(s_ref)
            step_items.append((s_ref, s_type, s_config))

        # 2. Check connections validity
        conn_items: list[tuple[str, str, str]] = []
        connected_refs: set[str] = set()
        for c in connections or []:
            c_from = str(getattr(c, "from_ref", None) or (c.get("from_ref") if isinstance(c, dict) else "") or "").strip()
            c_to = str(getattr(c, "to_ref", None) or (c.get("to_ref") if isinstance(c, dict) else "") or "").strip()
            c_outcome = str(getattr(c, "outcome", None) or (c.get("outcome") if isinstance(c, dict) else "") or "").strip()
            c_from, from_ref_warning = _normalize_connection_ref(c_from, source=True)
            c_to, to_ref_warning = _normalize_connection_ref(c_to, source=False)
            if from_ref_warning:
                warnings.append(from_ref_warning)
            if to_ref_warning:
                warnings.append(to_ref_warning)
            if c_from in deleted_set:
                return BuildWorkflowBulkResult(
                    error=f"Connection from_ref '{c_from}' cannot be used because it is in delete_step_ids."
                )
            if c_to in deleted_set:
                return BuildWorkflowBulkResult(
                    error=f"Connection to_ref '{c_to}' cannot be used because it is in delete_step_ids."
                )
            c_from_is_existing = bool(workflow_id and c_from.isdigit())
            c_to_is_existing = bool(workflow_id and c_to.isdigit())
            if c_from not in seen_refs and c_from.lower() not in ("start", "1") and not c_from_is_existing:
                return BuildWorkflowBulkResult(
                    error=(
                        f"Connection from_ref '{c_from}' is invalid. Must be 'start', '1', "
                        f"one of the new step refs {list(seen_refs)}, or an existing numeric "
                        "step_id when workflow_id is provided."
                    ),
                    hint=(
                        "For existing workflows, do not use guessed semantic refs for existing nodes. "
                        "Reload with get_workflow/show_workflow and pass numeric step_id values for "
                        "existing nodes; semantic refs only refer to newly created steps in this same call."
                    ),
                )
            if c_to not in seen_refs and c_to.lower() not in ("end", "2") and not c_to_is_existing:
                return BuildWorkflowBulkResult(
                    error=(
                        f"Connection to_ref '{c_to}' is invalid. Must be 'end', '2', "
                        f"one of the new step refs {list(seen_refs)}, or an existing numeric step_id when workflow_id is provided."
                    )
                )
            conn_items.append((c_from, c_to, c_outcome))
            if c_from in seen_refs:
                connected_refs.add(c_from)
            connected_refs.add(c_to)

        if creating_new_workflow:
            conn_items, reachability_error = _ensure_new_workflow_reachable_from_start(
                conn_items,
                [s_ref for s_ref, _, _ in step_items],
                warnings,
            )
            if reachability_error:
                return BuildWorkflowBulkResult(
                    warnings=warnings,
                    error=reachability_error,
                    hint=(
                        "For new workflows, include the first edge from the trigger: "
                        "ConnectionSpec(from_ref='start', to_ref='<first_step_ref>')."
                    ),
                )
            connected_refs = set()
            for c_from, c_to, _ in conn_items:
                if c_from in seen_refs:
                    connected_refs.add(c_from)
                connected_refs.add(c_to)

        unconnected_refs = [s_ref for s_ref, _, _ in step_items if s_ref not in connected_refs]
        if unconnected_refs:
            return BuildWorkflowBulkResult(
                error=(
                    "Every new step must be connected before build_workflow_bulk writes to Jotform. "
                    f"Unconnected step refs: {unconnected_refs}."
                ),
                hint=(
                    "Remove the unused steps or add connections that include each ref as from_ref or to_ref. "
                    "For example, connect email steps to an end step, and do not create extra end steps."
                ),
            )

        clean_configs: dict[str, dict] = {}
        trigger_form_fields = []
        trigger_form_questions: dict = {}

        # 3. Validate each step config
        normalized_step_items: list[tuple[str, str, dict]] = []
        for s_ref, s_type, s_config in step_items:
            s_config, alias_warnings = _normalize_step_config_aliases(s_type, s_config)
            for w in alias_warnings:
                warnings.append(f"[{s_ref}] {w}")
            s_type, type_warnings = _normalize_step_type_aliases(s_type, s_config)
            for w in type_warnings:
                warnings.append(f"[{s_ref}] {w}")
            try:
                clean_cfg, step_warnings = tb.validate_config(s_type, s_config)
            except tb.ValidationError as e:
                hint = "Call list_step_types to see valid values."
                if s_type == "workflow_integration":
                    hint = (
                        "Use type='workflow_integration' with one supported blank-shell subType: "
                        f"{supported_integration_subtypes_text()}. Do not include auth or settings fields."
                    )
                return BuildWorkflowBulkResult(
                    error=f"Step '{s_ref}' ({s_type}) config error: {e}",
                    hint=hint,
                )
            for w in step_warnings:
                warnings.append(f"[{s_ref}] {w}")
            dropped_fields = [
                warning
                for warning in step_warnings
                if "unknown field" in warning or "field dropped" in warning
            ]
            if dropped_fields and s_type != "workflow_integration":
                return BuildWorkflowBulkResult(
                    warnings=warnings,
                    error=(
                        f"Step '{s_ref}' ({s_type}) contains config that would be silently dropped: "
                        + "; ".join(dropped_fields)
                    ),
                    hint="Correct the field names or values before retrying the bulk write.",
                )

            if creating_new_workflow:
                clean_cfg, placeholder_warnings = _fill_draft_assignee_placeholders(
                    s_ref, s_type, clean_cfg
                )
                for w in placeholder_warnings:
                    warnings.append(f"[{s_ref}] {w}")

            missing = _missing_required_step_details(s_type, clean_cfg)
            if missing:
                return BuildWorkflowBulkResult(
                    warnings=warnings,
                    error=f"Step '{s_ref}' ({s_type}) needs more detail before it can be added. Missing: {', '.join(missing)}.",
                    hint=(
                        "Provide the missing content/subject/body/outcomes. For draft staff "
                        "approvers or assignees, use reserved role placeholders like "
                        "hr@workflow.invalid instead of asking the user."
                    )
                )

            clean_configs[s_ref] = clean_cfg
            normalized_step_items.append((s_ref, s_type, s_config))
        step_items = normalized_step_items

        preflight_elements = (
            [element for element in lock_snapshot.get("elements", []) if isinstance(element, dict)]
            if isinstance(lock_snapshot, dict)
            else []
        )
        outcome_error, outcome_hint = _preflight_connection_outcomes(
            step_items,
            clean_configs,
            conn_items,
            preflight_elements,
        )
        if outcome_error:
            return BuildWorkflowBulkResult(
                workflow_id=workflow_id or None,
                workflow_url=_workflow_url(workflow_id or None),
                warnings=warnings,
                error=outcome_error,
                hint=outcome_hint,
            )

        created_trigger_form_id: str | None = None
        field_context: tuple[str | None, dict, str | None] = (None, {}, None)
        field_context_label = "trigger form"
        status_after_bulk = "DISABLED"
        disabled_before_graph_write = False
        if not workflow_id:
            if trigger_type != "schedule" and not trigger_form_id:
                return BuildWorkflowBulkResult(
                    warnings=warnings,
                    error=(
                        "trigger_form_id is required for new form-submission workflows. "
                        "Start with search_workflow_templates, then call create_form_with_ai "
                        "and pass its form_id here."
                    ),
                    hint=(
                        "Use search_workflow_templates(query=...) -> create_form_with_ai(prompt=...) -> build_workflow_bulk("
                        "title=..., trigger_form_id=..., steps=..., connections=...)."
                    ),
                )

            if trigger_type == "schedule":
                if trigger_form_id:
                    warnings.append(
                        "Ignored trigger_form_id because trigger_type='schedule'; assign forms with workflow_assign_form.formID."
                    )
                if trigger_schedule is not None and not isinstance(trigger_schedule, dict):
                    return BuildWorkflowBulkResult(
                        warnings=warnings,
                        error="trigger_schedule must be an object when trigger_type='schedule'.",
                    )
                trigger_schedule, schedule_warnings, schedule_error = _normalize_schedule_config(trigger_schedule, client=client)
                warnings.extend(schedule_warnings)
                if schedule_error:
                    return BuildWorkflowBulkResult(
                        warnings=warnings,
                        error=schedule_error,
                        hint=(
                            "Pass trigger_schedule with Jotform schedule fields, for example "
                            "{'schedule__executeWhen__afterAmount':'1', "
                            "'schedule__executeWhen__afterUnit':'week', "
                            "'schedule__executeWhen__customDate':'2026-09-04T14:00:00.000Z', "
                            "'schedule__executeWhen__executeOnCustomDate':'Yes', "
                            "'schedule__end__recurring':'none'}."
                        ),
                    )
                assigned_form_id, assigned_form_questions, assigned_form_error = _assigned_form_questions_from_step_configs(
                    client,
                    step_items,
                    clean_configs,
                )
                field_context = (assigned_form_id, assigned_form_questions, assigned_form_error)
                field_context_label = "assigned form"
            else:
                created_trigger_form_id = trigger_form_id
                try:
                    trigger_form_questions = client.get_form_questions(trigger_form_id)
                    trigger_form_fields = form_fields_from_questions(trigger_form_questions)
                    field_context = (trigger_form_id, trigger_form_questions, None)
                except JotformAPIError as e:
                    return BuildWorkflowBulkResult(
                        trigger_form_id=trigger_form_id,
                        trigger_form_url=_form_url(trigger_form_id),
                        warnings=warnings,
                        error=f"Could not validate trigger form {trigger_form_id} before creating the workflow: {e}",
                    )

            clean_configs, field_warnings, field_error, _ = _normalize_field_dependent_step_configs(
                client,
                "",
                step_items,
                clean_configs,
                field_context,
                context_label=field_context_label,
            )
            warnings.extend(field_warnings)
            if field_error:
                return BuildWorkflowBulkResult(
                    trigger_form_id=created_trigger_form_id,
                    trigger_form_url=_form_url(created_trigger_form_id),
                    trigger_form_fields=trigger_form_fields,
                    warnings=warnings,
                    error=field_error,
                )

            if not title:
                title = "Untitled Workflow"

            try:
                if trigger_type == "schedule":
                    created = client.create_workflow(
                        title,
                        trigger_type=trigger_type,
                        schedule_config=trigger_schedule,
                    )
                else:
                    created = client.create_workflow(title)
            except PartialWorkflowCreateError as e:
                return BuildWorkflowBulkResult(
                    workflow_id=e.workflow_id,
                    workflow_url=_workflow_url(e.workflow_id),
                    trigger_form_id=trigger_form_id or None,
                    trigger_form_url=_form_url(trigger_form_id or None),
                    warnings=warnings,
                    error=str(e),
                    hint=(
                        "The workflow already exists. Reuse this workflow_id with the same operation_id "
                        "after reloading it; do not create a replacement workflow."
                    ),
                )
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

            created_status = str(created.get("status") or "").strip().upper()
            if created_status == "DISABLED":
                status_after_bulk = "DISABLED"
                disabled_before_graph_write = True
            try:
                if disabled_before_graph_write:
                    status_result = {"status": "DISABLED"}
                else:
                    status_result = client.update_workflow_metadata(workflow_id, status="DISABLED")
                status_after_bulk = (
                    str(status_result.get("status") or "DISABLED").upper()
                    if isinstance(status_result, dict)
                    else "DISABLED"
                )
                disabled_before_graph_write = True
            except JotformAPIError as e:
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    trigger_form_id=trigger_form_id or None,
                    trigger_form_url=_form_url(trigger_form_id or None),
                    warnings=warnings,
                    error=f"Workflow was created, but forcing DISABLED status before graph write failed: {e}",
                )

            if trigger_form_id and trigger_type != "schedule":
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
        elif trigger_form_id and trigger_type != "schedule":
            created_trigger_form_id = trigger_form_id
            try:
                trigger_form_questions = client.get_form_questions(trigger_form_id)
                trigger_form_fields = form_fields_from_questions(trigger_form_questions)
            except JotformAPIError as e:
                warnings.append(f"Could not load trigger form {trigger_form_id} fields: {e}")
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
        elif workflow_id and trigger_type != "schedule":
            if isinstance(lock_snapshot, dict) and isinstance(lock_snapshot.get("elements"), list):
                trigger_form_id_for_fields, questions_for_fields, trigger_error = _trigger_form_questions_from_elements(
                    client,
                    [e for e in lock_snapshot.get("elements", []) if isinstance(e, dict)],
                )
            else:
                trigger_form_id_for_fields, questions_for_fields, trigger_error = _trigger_form_questions(
                    client,
                    workflow_id,
                )
            if questions_for_fields:
                trigger_form_questions = questions_for_fields
                trigger_form_fields = form_fields_from_questions(questions_for_fields)
                created_trigger_form_id = created_trigger_form_id or trigger_form_id_for_fields
            elif trigger_error:
                warnings.append(trigger_error)

        should_normalize_after_create = True
        if workflow_id:
            if trigger_type == "schedule":
                if not field_context[1]:
                    assigned_form_id, assigned_form_questions, assigned_form_error = _assigned_form_questions_from_step_configs(
                        client,
                        step_items,
                        clean_configs,
                    )
                    field_context = (assigned_form_id, assigned_form_questions, assigned_form_error)
                field_context_label = "assigned form"
            elif not trigger_form_questions:
                if isinstance(lock_snapshot, dict) and isinstance(lock_snapshot.get("elements"), list):
                    trigger_form_id_for_fields, questions_for_fields, trigger_error = _trigger_form_questions_from_elements(
                        client,
                        [e for e in lock_snapshot.get("elements", []) if isinstance(e, dict)],
                    )
                else:
                    trigger_form_id_for_fields, questions_for_fields, trigger_error = _trigger_form_questions(
                        client,
                        workflow_id,
                    )
                if questions_for_fields:
                    trigger_form_questions = questions_for_fields
                    trigger_form_fields = form_fields_from_questions(questions_for_fields)
                    created_trigger_form_id = created_trigger_form_id or trigger_form_id_for_fields
                elif trigger_error:
                    warnings.append(trigger_error)
                field_context = (trigger_form_id_for_fields, questions_for_fields, trigger_error)
                field_context_label = "trigger form"
            else:
                field_context = (created_trigger_form_id or trigger_form_id, trigger_form_questions, None)
                field_context_label = "trigger form"
            if creating_new_workflow:
                should_normalize_after_create = False

        if should_normalize_after_create:
            clean_configs, field_warnings, field_error, field_hint = _normalize_field_dependent_step_configs(
                client,
                workflow_id,
                step_items,
                clean_configs,
                field_context,
                context_label=field_context_label,
            )
            warnings.extend(field_warnings)
            if field_error:
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    trigger_form_id=created_trigger_form_id,
                    trigger_form_url=_form_url(created_trigger_form_id),
                    trigger_form_fields=trigger_form_fields,
                    warnings=warnings,
                    error=field_error,
                    hint=field_hint,
                )

        # 4. Fetch current workflow elements & links
        if isinstance(lock_snapshot, dict) and isinstance(lock_snapshot.get("elements"), list):
            existing_elements = [e for e in lock_snapshot.get("elements", []) if isinstance(e, dict)]
            existing_links = [l for l in (lock_snapshot.get("links") or []) if isinstance(l, dict)]
        else:
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
        start_id = _element_id(start_elem) if start_elem else None
        start_id = start_id or 1

        existing_elements_by_id = {
            element_id: e
            for e in existing_elements
            for element_id in [_element_id(e)]
            if element_id is not None
        }
        existing_links_by_id = {
            str(l.get("link_id")): l
            for l in existing_links
            if l.get("link_id") is not None
        }
        explicit_updates_by_element: dict[str, dict] = {}
        for step_id, update_config in update_items:
            current = existing_elements_by_id.get(step_id)
            if current is None:
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    warnings=warnings,
                    error=f"Step '{step_id}' in step_updates does not exist in workflow {workflow_id}.",
                )
            step_type = str(current.get("type") or "")
            if not step_type:
                return BuildWorkflowBulkResult(error=f"Could not determine the type of step '{step_id}'.")
            update_config, alias_warnings = _normalize_step_config_aliases(step_type, update_config)
            for warning in alias_warnings:
                warnings.append(f"[{step_id}] {warning}")
            try:
                clean_update, update_warnings = tb.validate_config(step_type, update_config)
            except tb.ValidationError as error:
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    warnings=warnings,
                    error=f"Step '{step_id}' ({step_type}) update config error: {error}",
                )
            for warning in update_warnings:
                warnings.append(f"[{step_id}] {warning}")
            if not clean_update:
                return BuildWorkflowBulkResult(error=f"Step '{step_id}' has no valid config fields to update.")
            explicit_updates_by_element[step_id] = tb.build_element_update(step_id, clean_update)

        if trigger_type == "schedule" and trigger_schedule and start_id:
            start_update = explicit_updates_by_element.get(str(start_id))
            if start_update is None:
                start_update = {"action": "update", "elementID": start_id, "data": {"element_id": start_id}}
                explicit_updates_by_element[str(start_id)] = start_update
            start_update["data"]["subType"] = "workflow_start_point_schedule"
            start_update["data"].update(trigger_schedule)


        for link_id in normalized_delete_link_ids:
            if link_id not in existing_links_by_id:
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    warnings=warnings,
                    error=f"Connection '{link_id}' in delete_link_ids does not exist in workflow {workflow_id}.",
                )

        # Validate delete_step_ids
        for del_id in normalized_delete_ids:
            if del_id not in existing_elements_by_id:
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    trigger_form_id=created_trigger_form_id,
                    trigger_form_url=_form_url(created_trigger_form_id),
                    trigger_form_fields=trigger_form_fields,
                    warnings=warnings,
                    error=f"Step '{del_id}' in delete_step_ids does not exist in workflow {workflow_id}.",
                )
            del_elem = existing_elements_by_id[del_id]
            if del_elem.get("type") == "workflow_start_point":
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    trigger_form_id=created_trigger_form_id,
                    trigger_form_url=_form_url(created_trigger_form_id),
                    trigger_form_fields=trigger_form_fields,
                    warnings=warnings,
                    error=f"Cannot delete workflow start point (step '{del_id}').",
                )

        if normalized_delete_ids and not connections and not confirm_orphaned_downstream:
            impacts = []
            orphaned_steps = []
            for del_id in normalized_delete_ids:
                deleted = existing_elements_by_id.get(del_id, {})
                incoming_links = [
                    link for link in existing_links
                    if str(link.get("toElement")) == del_id
                    and str(link.get("fromElement")) not in deleted_set
                ]
                outgoing_links = [
                    link for link in existing_links
                    if str(link.get("fromElement")) == del_id
                    and str(link.get("toElement")) not in deleted_set
                ]
                if not incoming_links or not outgoing_links:
                    continue

                def brief(step_id: object) -> dict:
                    element = existing_elements_by_id.get(str(step_id), {})
                    return {
                        "step_id": str(step_id),
                        "type": element.get("type"),
                        "label": element.get("name") or element.get("label") or element.get("type"),
                    }

                outgoing_targets = [brief(link.get("toElement")) for link in outgoing_links]
                reconnect_candidates = [
                    item for item in outgoing_targets
                    if item.get("type") != "workflow_end_point"
                ]
                end_candidates = [
                    item for item in outgoing_targets
                    if item.get("type") == "workflow_end_point"
                ]
                orphaned_steps.extend(reconnect_candidates)
                impacts.append({
                    "deleted_step": brief(del_id),
                    "incoming": [
                        {"link_id": link.get("link_id"), "from": brief(link.get("fromElement"))}
                        for link in incoming_links
                    ],
                    "outgoing": [
                        {"link_id": link.get("link_id"), "to": brief(link.get("toElement"))}
                        for link in outgoing_links
                    ],
                    "reconnect_candidates": reconnect_candidates,
                    "end_candidates": end_candidates,
                    "suggested_question": (
                        "This delete has multiple child paths; choose a reconnect target, "
                        "delete the downstream subtree, or confirm leaving it disconnected."
                        if len(outgoing_links) > 1
                        else "Choose a reconnect target or confirm leaving the downstream path disconnected."
                    ),
                })

            if impacts and len(normalized_delete_ids) == 1:
                orphaned_step_ids = [item["step_id"] for item in orphaned_steps]
                error = (
                    "Deleting this step would leave downstream child steps unreachable."
                    if orphaned_step_ids
                    else "Deleting this step would break existing flow paths."
                )
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    trigger_form_id=created_trigger_form_id,
                    trigger_form_url=_form_url(created_trigger_form_id),
                    needs_confirmation=True,
                    orphaned_step_ids=orphaned_step_ids,
                    orphaned_steps=orphaned_steps,
                    delete_impacts=impacts,
                    warnings=warnings,
                    error=error,
                    hint=(
                        "Ask the user what should happen after the deleted node before writing. "
                        "For each delete_impacts entry, offer the reconnect_candidates/end_candidates, "
                        "or ask whether to delete the downstream subtree or leave downstream nodes "
                        "disconnected. If the user explicitly chooses to leave nodes orphaned, retry "
                        "with confirm_orphaned_downstream=true."
                    ),
                )

        element_deletes = [
            {"action": "delete", "elementID": sid, "data": {"element_id": sid}}
            for sid in normalized_delete_ids
        ]

        deleted_incident_links = [
            l for l in existing_links
            if str(l.get("fromElement")) in deleted_set or str(l.get("toElement")) in deleted_set
        ]
        removed_link_values = {
            str(link.get("link_id")): link.get("link_id")
            for link in deleted_incident_links
            if link.get("link_id") is not None
        }
        for link_id in normalized_delete_link_ids:
            removed_link_values[link_id] = existing_links_by_id[link_id].get("link_id", link_id)
        removed_link_ids = set(removed_link_values)
        link_deletes = [
            {"action": "delete", "linkID": value, "data": {"link_id": value}}
            for _, value in sorted(removed_link_values.items())
        ]

        outcome_updates_by_element: dict[str, dict] = {}
        # Clear outcomes for non-deleted branching steps whose links were deleted
        for source_id, source_elem in existing_elements_by_id.items():
            if source_id in deleted_set:
                continue
            if source_elem.get("type") not in schema_registry.BRANCHING_TYPES:
                continue
            clear = tb.build_outcome_clears_for_links(source_elem, list(removed_link_ids))
            if clear is not None:
                outcome_updates_by_element[str(source_id)] = clear

        existing_elem_ids = [
            int(element_id)
            for e in existing_elements
            for element_id in [_element_id(e)]
            if str(element_id or "").isdigit()
        ]
        curr_elem_id = max(existing_elem_ids, default=1)

        ref_to_id: dict[str, int | str] = {"start": start_id, "1": start_id}
        end_elem = next((e for e in existing_elements if e.get("type") == "workflow_end_point"), None)
        end_id = end_elem.get("element_id") if end_elem else 2
        ref_to_id["end"] = end_id
        ref_to_id["2"] = end_id
        ref_to_id[str(end_id)] = end_id
        for existing_id in existing_elements_by_id:
            if existing_id not in deleted_set:
                ref_to_id[existing_id] = existing_id
        for s_ref, _, _ in step_items:
            curr_elem_id += 1
            ref_to_id[s_ref] = curr_elem_id

        for c_from, c_to, _ in conn_items:
            if c_from not in ref_to_id:
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    trigger_form_id=created_trigger_form_id,
                    trigger_form_url=_form_url(created_trigger_form_id),
                    trigger_form_fields=trigger_form_fields,
                    warnings=warnings,
                    error=f"Connection from_ref '{c_from}' does not exist in workflow {workflow_id}.",
                    hint="Call get_workflow and use either a new step ref or an existing step_id from the steps list.",
                )
            if c_to not in ref_to_id:
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    trigger_form_id=created_trigger_form_id,
                    trigger_form_url=_form_url(created_trigger_form_id),
                    trigger_form_fields=trigger_form_fields,
                    warnings=warnings,
                    error=f"Connection to_ref '{c_to}' does not exist in workflow {workflow_id}.",
                    hint="Call get_workflow and use either a new step ref or an existing step_id from the steps list.",
                )

        affected_step_ids: set[str] = set(seen_update_ids) | set(normalized_delete_ids)
        for link_id in normalized_delete_link_ids:
            link = existing_links_by_id.get(link_id)
            if not link:
                continue
            for key in ("fromElement", "toElement"):
                value = link.get(key)
                if value is not None and str(value) in existing_elements_by_id:
                    affected_step_ids.add(str(value))
        for c_from, c_to, _ in conn_items:
            for ref in (c_from, c_to):
                step_id = ref_to_id.get(ref)
                if step_id is not None and str(step_id) in existing_elements_by_id:
                    affected_step_ids.add(str(step_id))

        if stale_lock_conflict:
            changed_steps = _changed_affected_step_ids(
                base_snapshot,
                lock_snapshot,
                affected_step_ids,
            )
            if changed_steps:
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    trigger_form_id=created_trigger_form_id,
                    trigger_form_url=_form_url(created_trigger_form_id),
                    conflict=True,
                    current_revision_id=lock_revision_id,
                    current_updated_at=lock_updated_at,
                    warnings=warnings,
                    error=(
                        "Workflow changed inside this mutation's affected scope "
                        f"(steps {', '.join(changed_steps)}). No write was attempted."
                    ),
                    hint=_conflict_hint(),
                )
            else:
                warnings.append(
                "Workflow revision changed only outside this mutation's affected steps/connections; "
                "rebased the update onto the fresh live graph."
                )

        all_elements = [e for e in existing_elements if str(_element_id(e)) not in deleted_set]
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
        position_updates_by_element: dict[str, dict] = {}
        used_branch_outcomes: set[tuple[str, str]] = set()

        for c_from, c_to, c_outcome in conn_items:
            curr_link_id += 1
            lid = curr_link_id
            from_id = ref_to_id[c_from]
            to_id = ref_to_id[c_to]

            from_elem_data = created_data_by_id.get(from_id)
            if from_elem_data is None:
                from_elem_data = next((e for e in existing_elements if str(_element_id(e)) == str(from_id)), {})

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
                outcomes_list = from_elem_data.get("outcomes") or []
                available = [tb.outcome_label(o) for o in outcomes_list]
                c_outcome, outcome_warning = _normalize_connection_outcome(c_outcome, available)
                if outcome_warning:
                    warnings.append(f"[{c_from}] {outcome_warning}")
                matched_outcome = None
                for idx, outcome_item in enumerate(outcomes_list, start=1):
                    candidate = tb._task_outcome_object(outcome_item, idx) if isinstance(outcome_item, str) else outcome_item
                    if (tb.outcome_label(candidate) or "").strip().lower() == c_outcome.strip().lower():
                        matched_outcome = candidate
                        break
                if matched_outcome is None:
                    return BuildWorkflowBulkResult(
                        warnings=warnings,
                        error=f"'{c_outcome}' is not an outcome on this step. Available: {available}",
                    )

                outcome_label = tb.outcome_label(matched_outcome) or c_outcome
                outcome_key = (str(from_id), outcome_label.strip().lower())
                if outcome_key in used_branch_outcomes:
                    return BuildWorkflowBulkResult(
                        workflow_id=workflow_id,
                        workflow_url=_workflow_url(workflow_id),
                        trigger_form_id=created_trigger_form_id,
                        trigger_form_url=_form_url(created_trigger_form_id),
                        trigger_form_fields=trigger_form_fields,
                        warnings=warnings,
                        error=(
                            f"Outcome '{outcome_label}' on step '{c_from}' is already used "
                            "in this bulk update. A branching outcome can point to only one target."
                        ),
                    )
                used_branch_outcomes.add(outcome_key)
                link_payload["data"]["labels"] = [{"justCreated": True, "label": outcome_label}]

                outcome_id = matched_outcome.get("outcomeID") or matched_outcome.get("id") if isinstance(matched_outcome, dict) else 1
                previous_link_id = matched_outcome.get("linkID") if isinstance(matched_outcome, dict) else None
                if previous_link_id not in (None, 0, "0", ""):
                    if str(previous_link_id) not in removed_link_ids:
                        link_deletes.append(tb.build_link_delete(previous_link_id))
                        removed_link_ids.add(str(previous_link_id))
                    previous_link = existing_links_by_id.get(str(previous_link_id))
                    previous_to = previous_link.get("toElement") if previous_link else None
                    warnings.append(
                        f"[{c_from}] Rewired outcome '{outcome_label}' from existing link "
                        f"{previous_link_id}" + (f" to step {previous_to}" if previous_to else "") + "."
                    )

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
                if from_id not in created_data_by_id:
                    outcome_updates_by_element[str(from_id)] = tb.build_element_update(
                        from_id,
                        {"outcomes": updated_outcomes},
                    )

            link_creates.append(link_payload)

            if c_from in seen_refs and c_to not in seen_refs:
                target_existing = existing_elements_by_id.get(str(to_id))
                source_new = created_data_by_id.get(from_id)
                if (
                    target_existing
                    and source_new
                    and target_existing.get("type") == "workflow_end_point"
                    and source_new.get("y") is not None
                ):
                    source_y = int(source_new.get("y") or 0)
                    target_y = int(_element_axis(target_existing, "y", 0) or 0)
                    if target_y <= source_y:
                        position_updates_by_element[str(to_id)] = tb.build_element_update(
                            to_id,
                            {
                                "x": source_new.get("x", _element_axis(target_existing, "x", 0)),
                                "y": source_y + tb.STEP_Y,
                            },
                        )

        preparation_snapshot = (
            lock_snapshot
            if isinstance(lock_snapshot, dict)
            else {"workflow": {}, "elements": deepcopy(existing_elements), "links": deepcopy(existing_links)}
        )
        previous_workflow = preparation_snapshot.get("workflow", {}) if isinstance(preparation_snapshot, dict) else {}
        previous_status = (
            previous_workflow.get("status") or previous_workflow.get("publishStatus")
            if isinstance(previous_workflow, dict)
            else None
        )

        # 6. Atomic write via update_tree
        try:
            revision_desc = f"before build_workflow_bulk ({len(steps)} steps, {len(normalized_delete_ids)} deletes)"
            captured_revision = revision_log.capture_workflow_revision(
                client,
                workflow_id,
                _revision_reason(revision_desc, intent, reason),
                tool_name="build_workflow_bulk",
            )

            write_revision_id = lock_revision_id or expected_revision_id or None
            write_updated_at = lock_updated_at or base_updated_at or None
            # The captured revision uses fetch_essential=False to store full UI details.
            # We must fetch the essential shape for an apples-to-apples conflict diff.
            prewrite_snapshot = (
                client.get_workflow_combined(workflow_id)
                if not creating_new_workflow
                else None
            )
            if not creating_new_workflow and prewrite_snapshot is not None:
                changed_steps = _changed_affected_step_ids(
                    preparation_snapshot,
                    prewrite_snapshot,
                    affected_step_ids,
                )
                if changed_steps:
                    return BuildWorkflowBulkResult(
                        workflow_id=workflow_id,
                        workflow_url=_workflow_url(workflow_id),
                        trigger_form_id=created_trigger_form_id,
                        trigger_form_url=_form_url(created_trigger_form_id),
                        conflict=True,
                        current_revision_id=workflow_revision_id(prewrite_snapshot),
                        current_updated_at=workflow_updated_at(prewrite_snapshot),
                        warnings=warnings,
                        error=(
                            "Workflow changed inside this mutation's affected scope while the write "
                            f"was being prepared (steps {', '.join(changed_steps)}). No graph write was attempted."
                        ),
                        hint=_conflict_hint(),
                    )
                write_revision_id = workflow_revision_id(prewrite_snapshot)
                write_updated_at = workflow_updated_at(prewrite_snapshot)

            if not disabled_before_graph_write:
                try:
                    status_result = client.update_workflow_metadata(workflow_id, status="DISABLED")
                    status_after_bulk = (
                        str(status_result.get("status") or "DISABLED").upper()
                        if isinstance(status_result, dict)
                        else "DISABLED"
                    )
                    disabled_before_graph_write = True
                except JotformAPIError as e:
                    return BuildWorkflowBulkResult(
                        workflow_id=workflow_id,
                        workflow_url=_workflow_url(workflow_id),
                        trigger_form_id=created_trigger_form_id,
                        trigger_form_url=_form_url(created_trigger_form_id),
                        error=f"Workflow graph was not written because forcing DISABLED status before graph write failed: {e}",
                        warnings=warnings,
                    )
                try:
                    post_status_snapshot = client.get_workflow_combined(workflow_id)
                    write_revision_id = workflow_revision_id(post_status_snapshot)
                    write_updated_at = workflow_updated_at(post_status_snapshot)
                    warnings.append(
                        "Refreshed workflow revision after disabling the workflow before graph write."
                    )
                except JotformAPIError as e:
                    _restore_workflow_status(client, workflow_id, previous_status, warnings)
                    return BuildWorkflowBulkResult(
                        workflow_id=workflow_id,
                        workflow_url=_workflow_url(workflow_id),
                        trigger_form_id=created_trigger_form_id,
                        trigger_form_url=_form_url(created_trigger_form_id),
                        error=(
                            "Workflow graph was not written because its revision could not be "
                            f"refreshed after disabling: {e}"
                        ),
                        warnings=warnings,
                    )
            locking_args = {}
            if write_revision_id:
                locking_args["expected_revision_id"] = write_revision_id
            elif write_updated_at:
                locking_args["base_updated_at"] = write_updated_at
            client.update_tree(
                workflow_id,
                elements=(
                    element_deletes
                    + list(explicit_updates_by_element.values())
                    + element_creates
                    + list(outcome_updates_by_element.values())
                    + list(position_updates_by_element.values())
                ),
                links=link_deletes + link_creates,
                **locking_args,
            )
        except ConflictError as e:
            if not creating_new_workflow and disabled_before_graph_write:
                _restore_workflow_status(client, workflow_id, previous_status, warnings)
            return BuildWorkflowBulkResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                trigger_form_id=created_trigger_form_id,
                trigger_form_url=_form_url(created_trigger_form_id),
                conflict=True,
                error=str(e),
                hint=_conflict_hint(),
                warnings=warnings,
            )
        except JotformAPIError as e:
            if not creating_new_workflow and disabled_before_graph_write:
                _restore_workflow_status(client, workflow_id, previous_status, warnings)
            return BuildWorkflowBulkResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                trigger_form_id=created_trigger_form_id,
                trigger_form_url=_form_url(created_trigger_form_id),
                error=str(e),
                warnings=warnings,
            )

        revision_id = None
        updated_at = None
        verified = False
        try:
            try:
                written_snapshot = client.get_workflow_combined(
                    workflow_id,
                    fetch_essential=not isinstance(client, JotformClient),
                )
            except TypeError:
                written_snapshot = client.get_workflow_combined(workflow_id)
            revision_id = workflow_revision_id(written_snapshot)
            updated_at = workflow_updated_at(written_snapshot)
            if isinstance(client, JotformClient):
                expected_steps = {
                    str(ref_to_id[step_ref]): (step_type, clean_configs[step_ref])
                    for step_ref, step_type, _ in step_items
                }
                for step_id, update_payload in explicit_updates_by_element.items():
                    current = existing_elements_by_id.get(str(step_id), {})
                    expected_steps[str(step_id)] = (
                        str(current.get("type") or ""),
                        {
                            key: value
                            for key, value in (update_payload.get("data") or {}).items()
                            if key not in {"element_id", "elementID", "id"}
                        },
                    )
                verification_issues = _verify_bulk_snapshot(
                    written_snapshot,
                    expected_steps=expected_steps,
                    expected_connections=[
                        (str(ref_to_id[source]), str(ref_to_id[target]), outcome)
                        for source, target, outcome in conn_items
                    ],
                    deleted_step_ids=normalized_delete_ids,
                    deleted_link_ids=[
                        str(item.get("linkID"))
                        for item in link_deletes
                        if item.get("linkID") is not None
                    ],
                )
                if verification_issues:
                    return BuildWorkflowBulkResult(
                        workflow_id=workflow_id,
                        workflow_url=_workflow_url(workflow_id),
                        trigger_form_id=created_trigger_form_id,
                        trigger_form_url=_form_url(created_trigger_form_id),
                        trigger_form_fields=trigger_form_fields,
                        revision_id=revision_id,
                        updated_at=updated_at,
                        status=status_after_bulk,
                        verified=False,
                        error=(
                            "Workflow write completed but read-back verification failed: "
                            + "; ".join(verification_issues[:8])
                        ),
                        hint=(
                            "The workflow remains DISABLED. Reload it with get_workflow before "
                            "deciding whether a targeted repair is safe. Do not create another workflow."
                        ),
                        warnings=warnings,
                    )
            verified = True
        except JotformAPIError as e:
            if isinstance(client, JotformClient):
                return BuildWorkflowBulkResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    trigger_form_id=created_trigger_form_id,
                    trigger_form_url=_form_url(created_trigger_form_id),
                    trigger_form_fields=trigger_form_fields,
                    status=status_after_bulk,
                    verified=False,
                    error=f"Workflow write completed, but read-back verification failed: {e}",
                    hint=(
                        "The workflow remains DISABLED. Reload this workflow by ID; do not create "
                        "a replacement workflow for the same request."
                    ),
                    warnings=warnings,
                )
            warnings.append(f"Could not read workflow revision after write: {e}")

        created_steps = {s_ref: str(ref_to_id[s_ref]) for s_ref, _, _ in step_items}
        assigned_forms = []
        for s_ref, s_type, _ in step_items:
            if s_type != "workflow_assign_form":
                continue
            form_id = clean_configs.get(s_ref, {}).get("formID")
            if form_id:
                assigned_forms.append({
                    "step_ref": s_ref,
                    "step_id": created_steps.get(s_ref),
                    "form_id": str(form_id),
                    "form_url": _form_url(str(form_id)),
                })

        return BuildWorkflowBulkResult(
            workflow_id=workflow_id,
            workflow_url=_workflow_url(workflow_id),
            trigger_form_id=created_trigger_form_id,
            trigger_form_url=_form_url(created_trigger_form_id),
            trigger_form_fields=trigger_form_fields,
            created_steps=created_steps,
            assigned_forms=assigned_forms,
            updated_steps=[step_id for step_id, _ in update_items],
            deleted_steps=normalized_delete_ids,
            deleted_links=normalized_delete_link_ids,
            created_links_count=len(link_creates),
            verified=verified,
            revision_id=revision_id,
            updated_at=updated_at,
            warnings=warnings,
            status=status_after_bulk,
            hint=(
                f"Next required step: call show_workflow(workflow_id='{workflow_id}') immediately "
                "to display the interactive visual workflow canvas to the user. "
                "Do not answer the user or ask about publishing before show_workflow has been called. "
                "When answering after show_workflow, include workflow_url and every form_url returned here. "
                "The workflow is DISABLED after this bulk write; after showing it, ask whether the user "
                "wants to enable it. Do not call publish_workflow until the user explicitly agrees."
            ),
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
                    hint="Use a valid fixed email address or choose a real email field from trigger_form_fields.",
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
                    hint="Use a valid fixed email address or choose a real email field from trigger_form_fields.",
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
