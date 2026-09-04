"""
Thin wrapper around the public Jotform API (api.jotform.com).

Deliberately does NOT touch www.jotform.com/API (the internal BFF) —
that surface rejects any call made outside a browser session with
"Cross-Site Requests not allowed!". Every endpoint used here was
individually verified working from outside a browser with just an
apiKey. See docs/gap-report.md for the evidence trail.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import requests

from mcp_server.audit_log import log_jotform_request, trace_function

BASE_URL = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TIMEOUT = 20
COPILOT_TIMEOUT = float(os.environ.get("JOTFORM_COPILOT_TIMEOUT", "18"))
MAX_AI_FORM_REQUEST_CHARS = int(os.environ.get("JOTFORM_AI_FORM_PROMPT_CHARS", "700"))

# Workflow's frontend flattens only these object-valued element properties
# before updateTree. The public API may accept nested values with HTTP 200
# while silently dropping some of them, especially pause/wait configuration.
# Keep this targeted so ordinary nested email fields such as attachment/to
# keep the builder-compatible shape we measured from HAR.
FLATTENED_ELEMENT_PROPERTIES = frozenset({
    "reassign", "approvalEmail", "assignTaskEmail", "comment", "escalation",
    "expiration", "reminder", "autoFinish", "assignEmail", "checklist",
    "pause", "wait", "schedule", "timing", "repeat", "reminderEmail",
})
_UNFLATTENED_JSON_PROPERTIES = frozenset({"assignmentOptions"})
_FLAT_DELIMITER = "__"


def _flatten_mapping(value: dict, *, prefix: str, result: dict) -> None:
    if not value:
        result[prefix] = {}
        return
    for key, child in value.items():
        flat_key = f"{prefix}{_FLAT_DELIMITER}{key}"
        if isinstance(child, dict):
            _flatten_mapping(child, prefix=flat_key, result=result)
        else:
            result[flat_key] = deepcopy(child)


def flatten_element_properties(properties: dict) -> dict:
    """Convert selected nested element fields to the wire shape used by Workflow UI."""
    if not any(key in FLATTENED_ELEMENT_PROPERTIES for key in properties):
        return deepcopy(properties)

    flattened = {}
    for key, value in properties.items():
        if key in _UNFLATTENED_JSON_PROPERTIES or key not in FLATTENED_ELEMENT_PROPERTIES:
            flattened[key] = deepcopy(value)
        elif isinstance(value, dict):
            _flatten_mapping(value, prefix=key, result=flattened)
        else:
            flattened[key] = deepcopy(value)
    return flattened


def unflatten_element_properties(properties: dict) -> dict:
    """Return flattened API read-backs in the nested shape exposed by MCP schemas."""
    result = {
        key: deepcopy(value)
        for key, value in properties.items()
        if _FLAT_DELIMITER not in key
    }
    for flat_key, value in properties.items():
        if _FLAT_DELIMITER not in flat_key:
            continue
        parts = flat_key.split(_FLAT_DELIMITER)
        target = result
        for part in parts[:-1]:
            current = target.get(part)
            if not isinstance(current, dict):
                current = {}
                target[part] = current
            target = current
        target[parts[-1]] = deepcopy(value)
    return result


def _normalise_combined_content(content: dict) -> dict:
    normalised = deepcopy(content)
    elements = normalised.get("elements")
    if isinstance(elements, list):
        normalised["elements"] = [
            unflatten_element_properties(item) if isinstance(item, dict) else item
            for item in elements
        ]
    return normalised


class JotformAPIError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Jotform API error {status}: {body[:300]}")
        self.status = status
        self.body = body

    @property
    def retryable(self) -> bool:
        return self.status in (0, 429, 502, 503, 504)


def simplify_ai_form_prompt(prompt: str, *, language: str = "en") -> str:
    """Keep Copilot focused on a small intake form instead of workflow design."""
    request = re.sub(r"\s+", " ", str(prompt or "")).strip()
    if len(request) > MAX_AI_FORM_REQUEST_CHARS:
        shortened = request[:MAX_AI_FORM_REQUEST_CHARS]
        sentence_end = max(shortened.rfind("."), shortened.rfind(";"), shortened.rfind(","))
        request = shortened[:sentence_end] if sentence_end >= MAX_AI_FORM_REQUEST_CHARS // 2 else shortened
        request = request.rstrip(" ,;.") + "."
    language_name = "Turkish" if str(language).lower().startswith("tr") else "English"
    return (
        f"Create a simple {language_name} intake form. Use at most 8 essential fields. "
        "Collect only information needed to start the workflow; do not describe or build "
        f"the workflow itself. Request: {request}"
    )


class ConflictError(RuntimeError):
    """Raised when a caller tries to write from a stale workflow snapshot."""

    def __init__(
        self,
        workflow_id: str,
        *,
        expected_revision_id: str | None = None,
        current_revision_id: str | None = None,
        base_updated_at: str | None = None,
        current_updated_at: str | None = None,
        current_snapshot: dict | None = None,
    ):
        super().__init__(
            f"Workflow {workflow_id} changed in Jotform after it was read. "
            "Reload it with get_workflow, recalculate your changes on top of the new version, "
            "and retry applying them automatically."
        )
        self.workflow_id = str(workflow_id)
        self.expected_revision_id = expected_revision_id
        self.current_revision_id = current_revision_id
        self.base_updated_at = base_updated_at
        self.current_updated_at = current_updated_at
        self.current_snapshot = current_snapshot


class PartialWorkflowCreateError(JotformAPIError):
    """The workflow exists, but one of its required setup writes failed."""

    def __init__(self, workflow_id: str, stage: str, cause: JotformAPIError):
        super().__init__(cause.status, f"Workflow {workflow_id} was created, but {stage} failed: {cause.body}")
        self.workflow_id = str(workflow_id)
        self.stage = stage


def workflow_updated_at(combined: dict) -> str | None:
    """Extract the cloud update timestamp from known combined-response shapes."""
    workflow = combined.get("workflow") if isinstance(combined, dict) else None
    workflow = workflow if isinstance(workflow, dict) else {}
    value = (
        workflow.get("updated_at")
        or workflow.get("updatedAt")
        or combined.get("updated_at")
        or combined.get("updatedAt")
    )
    return str(value) if value not in (None, "") else None


def workflow_revision_id(combined: dict) -> str:
    """Return a stable token from fields shared by compact and full snapshots."""
    workflow = combined.get("workflow") if isinstance(combined, dict) else {}
    workflow = workflow if isinstance(workflow, dict) else {}
    # Only hash fields guaranteed on both fetchEssentialElementProps modes.
    # Any config/layout-only cloud edit is still detected through updated_at.
    element_keys = ("element_id", "id", "type", "resourceID", "resourceType")
    link_keys = ("link_id", "id", "fromElement", "toElement")
    semantic_snapshot = {
        "workflow": {
            key: workflow.get(key)
            for key in (
                "id", "title", "status", "publishStatus", "updated_at", "updatedAt"
            )
            if key in workflow
        },
        "elements": [
            {key: item.get(key) for key in element_keys if key in item}
            for item in (combined.get("elements") or [])
            if isinstance(item, dict)
        ],
        "links": [
            {key: item.get(key) for key in link_keys if key in item}
            for item in (combined.get("links") or [])
            if isinstance(item, dict)
        ],
    }
    encoded = json.dumps(
        semantic_snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        if text.isdigit():
            number = int(text)
            if number > 10_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number, tz=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)
    except (OverflowError, ValueError):
        return None


for _traced_helper_name in (
    "flatten_element_properties",
    "unflatten_element_properties",
    "_normalise_combined_content",
    "workflow_updated_at",
    "workflow_revision_id",
    "_parse_timestamp",
):
    globals()[_traced_helper_name] = trace_function(globals()[_traced_helper_name])


class JotformClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("JOTFORM_API_KEY", "")
        if not self.api_key:
            raise ValueError("JOTFORM_API_KEY is not set")
        self._idempotent_form_results: dict[str, dict] = {}

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: Any = None, headers: dict | None = None,
                 timeout: float | None = None) -> dict:
        params = dict(params or {})
        params["apiKey"] = self.api_key
        url = f"{BASE_URL}{path}"
        req_timeout = timeout or TIMEOUT
        
        headers = dict(headers or {})
        from mcp_server.telemetry_context import get_current_field
        trace_id = get_current_field("trace_id")
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        attempts = 3 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            try:
                resp = log_jotform_request(
                    method=method,
                    url=url,
                    params=params,
                    json_body=json_body,
                    send=lambda: requests.request(
                        method, url, params=params, json=json_body, headers=headers, timeout=req_timeout
                    ),
                )
            except requests.Timeout as error:
                if attempt + 1 < attempts:
                    time.sleep(0.2 * (2 ** attempt))
                    continue
                raise JotformAPIError(
                    0, f"Request timed out for {method} {path}; no response was received"
                ) from error
            except requests.RequestException as error:
                if attempt + 1 < attempts:
                    time.sleep(0.2 * (2 ** attempt))
                    continue
                # requests exceptions can include the prepared URL, and our API key
                # is a query parameter. Keep the MCP-facing error intentionally terse.
                raise JotformAPIError(
                    0, f"Request failed for {method} {path} ({type(error).__name__})"
                ) from error
            if resp.status_code in (429, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(0.2 * (2 ** attempt))
                continue
            break
        if resp.status_code not in (200, 201):
            raise JotformAPIError(resp.status_code, resp.text.replace(self.api_key, "[REDACTED]"))
        try:
            return resp.json()
        except ValueError as error:
            raise JotformAPIError(
                resp.status_code,
                f"Invalid JSON response for {method} {path}",
            ) from error

    def get_user_timezone(self) -> str | None:
        cached = getattr(self, "_cached_timezone", None)
        if cached:
            return cached
        try:
            res = self._request("GET", "/user/settings")
        except JotformAPIError:
            return None
        timezone_name = res.get("content", {}).get("time_zone")
        if timezone_name:
            self._cached_timezone = timezone_name
        return timezone_name

    # ---------- forms ----------

    def list_forms(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        params = {"limit": max(1, min(int(limit), 100)), "offset": max(0, int(offset))}
        if status:
            params["filter"] = f'{{"status:eq":"{status}"}}'
        return self._request("GET", "/user/forms", params=params).get("content", [])

    def get_form_questions(self, form_id: str) -> dict:
        return self._request(
            "GET", f"/form/{form_id}/questions", params={"parseJSON": 1}
        ).get("content", {})

    def get_form(self, form_id: str) -> dict:
        """Return one form's metadata without depending on list pagination."""
        return self._request("GET", f"/form/{form_id}").get("content", {})

    def _fallback_ai_form_payload(self, prompt: str, *, language: str = "en") -> dict:
        prompt_l = prompt.lower()
        is_tr = language.lower().startswith("tr") or any(
            token in prompt_l for token in ("talep", "başvuru", "musteri", "müşteri", "bayi")
        )
        title = "Talep Formu" if is_tr else "Request Form"
        if any(token in prompt_l for token in ("garanti", "warranty", "servis", "service")):
            title = "Garanti Servis Talep Formu" if is_tr else "Warranty Service Request Form"
        elif any(token in prompt_l for token in ("iade", "refund", "return", "değişim", "exchange")):
            title = "İade ve Değişim Talep Formu" if is_tr else "Refund and Exchange Request Form"
        elif any(token in prompt_l for token in ("bayi", "dealer", "partner")):
            title = "Bayi Başvuru Formu" if is_tr else "Dealer Application Form"
        elif any(token in prompt_l for token in ("ekipman", "equipment", "bakım", "maintenance")):
            title = "Ekipman Bakım Talep Formu" if is_tr else "Equipment Maintenance Request Form"

        labels = {
            "name": "Ad Soyad" if is_tr else "Full Name",
            "email": "E-posta Adresi" if is_tr else "Email Address",
            "phone": "Telefon Numarası" if is_tr else "Phone Number",
            "category": "Talep Türü" if is_tr else "Request Type",
            "reference": "Referans / Sipariş / Ürün Numarası" if is_tr else "Reference / Order / Product Number",
            "details": "Talep Detayları" if is_tr else "Request Details",
            "urgency": "Öncelik" if is_tr else "Priority",
        }
        options = "Düşük|Normal|Yüksek" if is_tr else "Low|Normal|High"
        questions = {
            "1": {"type": "control_head", "text": title, "order": "1", "name": "header"},
            "2": {"type": "control_fullname", "text": labels["name"], "order": "2", "name": "fullName", "required": "Yes"},
            "3": {"type": "control_email", "text": labels["email"], "order": "3", "name": "email", "required": "Yes"},
            "4": {"type": "control_phone", "text": labels["phone"], "order": "4", "name": "phone"},
            "5": {"type": "control_textbox", "text": labels["category"], "order": "5", "name": "requestType", "required": "Yes"},
            "6": {"type": "control_textbox", "text": labels["reference"], "order": "6", "name": "referenceNumber"},
            "7": {"type": "control_textarea", "text": labels["details"], "order": "7", "name": "requestDetails", "required": "Yes"},
            "8": {"type": "control_dropdown", "text": labels["urgency"], "order": "8", "name": "priority", "options": options},
        }
        return {
            "questions": questions,
            "properties": {"title": title, "height": "600"},
        }

    def _create_form_with_public_api_fallback(self, prompt: str, *, language: str = "en", reason: str = "") -> dict:
        form_payload = self._fallback_ai_form_payload(prompt, language=language)
        created = self._request("PUT", "/form", json_body=form_payload).get("content", {})
        form_id = (
            created.get("id")
            or created.get("formID")
            or created.get("form_id")
            or created.get("resource_id")
        )
        if not form_id:
            raise JotformAPIError(0, f"No form id in fallback form response: {created!r}")
        questions = None
        for _ in range(2):
            try:
                questions = self.get_form_questions(str(form_id))
                break
            except JotformAPIError:
                continue
        verified = isinstance(questions, dict)
        if not verified:
            questions = form_payload["questions"]
        return {
            "resource_id": str(form_id),
            "questions": questions,
            "summary": (
                "Created through public form API fallback because the Workflow AI form endpoint "
                f"was unavailable. {reason}".strip()
            ),
            "ai_fallback": True,
            "fallback_reason": reason,
            "verified": verified,
        }

    def create_form_with_ai(
        self,
        prompt: str,
        *,
        form_type: str = "classic",
        language: str = "en",
        operation_id: str = "",
    ) -> dict:
        """
        Creates a form from a natural-language prompt.

        Confirmed 2026-08-13 against api.jotform.com:
        POST /workflow/copilot/createWorkflowForm works with an apiKey, while
        the same path on www.jotform.com/API returns "Cross-Site Requests not
        allowed!".
        """
        operation_id = str(operation_id or "").strip()
        if operation_id and operation_id in self._idempotent_form_results:
            return deepcopy(self._idempotent_form_results[operation_id])

        simplified_prompt = simplify_ai_form_prompt(prompt, language=language)
        try:
            result = self._request(
                "POST",
                "/workflow/copilot/createWorkflowForm",
                json_body={
                    "prompt": simplified_prompt,
                    "formType": form_type,
                    "preferences": {"language": language},
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "jf-v2-source": "mcp",
                    "jf-v2-target": "workflow-copilot",
                },
                timeout=COPILOT_TIMEOUT,
            ).get("content", {})
            if isinstance(result, dict):
                result = {**result, "ai_fallback": False, "verified": True}
        except JotformAPIError as error:
            if error.status not in (0, 404, 429, 502, 503, 504):
                raise
            result = self._create_form_with_public_api_fallback(
                simplified_prompt,
                language=language,
                reason=f"Original AI endpoint error: {error.body or error.status}.",
            )
        if operation_id:
            self._idempotent_form_results[operation_id] = deepcopy(result)
        return result

    # ---------- workflows: read ----------

    def list_workflows(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        """Confirmed working 2026-08-07 (phase0_close_gaps.py)."""
        return self._request(
            "GET", "/user/workflows",
            params={
                "filter": '{"status:ne":["DELETED","PURGED","ARCHIVED"],"type:eq":"default"}',
                "orderby": "updated_at",
                "limit": max(1, min(int(limit), 100)),
                "offset": max(0, int(offset)),
            },
        ).get("content", [])

    def get_workflow_combined(
        self,
        workflow_id: str,
        *,
        fetch_essential: bool = True,
    ) -> dict:
        """
        Metadata + elements + links in one call. Confirmed working
        2026-08-07 — preferred over three separate calls.

        Tool reads use the compact essential shape by default. The MCP UI asks
        for the complete persisted element properties so Jotform's own native
        preview can render form names, conditions, outcomes and node details.
        """
        params = {"fetchEssentialElementProps": 1} if fetch_essential else {}
        content = self._request(
            "GET", f"/workflow/{workflow_id}/combined",
            params=params,
        ).get("content", {})
        return _normalise_combined_content(content) if isinstance(content, dict) else content

    def get_workflow(self, workflow_id: str) -> dict:
        return self._request("GET", f"/workflow/{workflow_id}").get("content", {})

    def get_elements(self, workflow_id: str) -> list:
        content = self._request("GET", f"/workflow/{workflow_id}/elements").get("content", [])
        return [
            unflatten_element_properties(item) if isinstance(item, dict) else item
            for item in content
        ] if isinstance(content, list) else []

    def get_element(self, workflow_id: str, element_id: int | str) -> dict:
        """Returns the FULL config for one element (the list endpoint only summarizes)."""
        content = self._request(
            "GET", f"/workflow/{workflow_id}/elements/{element_id}"
        ).get("content", {})
        return unflatten_element_properties(content) if isinstance(content, dict) else content

    def get_links(self, workflow_id: str) -> list:
        content = self._request("GET", f"/workflow/{workflow_id}/links").get("content", [])
        return content if isinstance(content, list) else []

    # ---------- workflows: write ----------

    def create_workflow(
        self,
        title: str,
        *,
        trigger_on_edit: str = "ENABLED",
        initial_status: str = "DISABLED",
        trigger_type: str = "form",
        trigger_form_id: str | None = None,
        schedule_config: dict | None = None,
    ) -> dict:
        """Create a workflow as a non-running draft and verify that status."""
        initial_status = str(initial_status or "DISABLED").strip().upper()
        if initial_status not in {"ENABLED", "DISABLED"}:
            raise ValueError("initial_status must be ENABLED or DISABLED")

        start_point_data = {
            "element_id": 1, "id": 1,
            "type": "workflow_start_point",
            "elementType": "workflow_start_point",
            "className": ["isStartPoint"],
            "position": {"x": 0, "y": 0}, "x": 0, "y": 0,
            "measured": {"width": 296, "height": 88},
        }

        if trigger_type == "schedule":
            start_point_data["subType"] = "workflow_start_point_schedule"
            if schedule_config:
                for k, v in schedule_config.items():
                    if k.startswith("schedule__"):
                        start_point_data[k] = str(v)
        else:
            start_point_data["subType"] = "workflow_start_point_submission"
            if trigger_form_id:
                start_point_data["resourceID"] = str(trigger_form_id)
                start_point_data["resourceType"] = "FORM"

        created = self._request(
            "POST", "/workflow",
            json_body={
                "title": title,
                "status": initial_status,
                "triggerOnEdit": trigger_on_edit,
                "elements": [{
                    "action": "update", "elementID": 1,
                    "data": start_point_data,
                }],
                "links": [],
            },
        ).get("content", {})

        workflow_id = None
        if isinstance(created, dict):
            workflow_id = created.get("id") or created.get("workflowID")

        if workflow_id:
            # POST /workflow is lossy for start point configs, especially schedule metadata.
            # We must force-apply the intended start_point_data via updateTree.
            try:
                self.update_tree(
                    str(workflow_id),
                    elements=[{
                        "action": "update",
                        "elementID": 1,
                        "data": start_point_data,
                    }],
                    links=[]
                )
            except JotformAPIError as error:
                raise PartialWorkflowCreateError(
                    str(workflow_id), "persisting the start point", error
                ) from error
            try:
                status_result = self.update_workflow_metadata(str(workflow_id), status=initial_status)
            except JotformAPIError as error:
                raise PartialWorkflowCreateError(
                    str(workflow_id), f"forcing {initial_status} status", error
                ) from error
            if isinstance(created, dict):
                created = {**created, "status": status_result.get("status", initial_status)}

        return created

    def update_workflow_metadata(self, workflow_id: str, **fields) -> dict:
        return self._request("POST", f"/workflow/{workflow_id}", json_body=fields).get("content", {})

    def create_element(self, workflow_id: str, step_type: str) -> dict:
        """Creates a bare element. Only `type` is required; config comes after."""
        return self._request(
            "POST", f"/workflow/{workflow_id}/elements",
            json_body={"type": step_type},
        ).get("content", {})

    def assert_workflow_revision(
        self,
        workflow_id: str,
        *,
        expected_revision_id: str | None = None,
        base_updated_at: str | None = None,
    ) -> dict:
        """Re-read cloud state and reject a stale optimistic-locking token."""
        current = self.get_workflow_combined(workflow_id)
        current_revision_id = workflow_revision_id(current)
        current_updated_at = workflow_updated_at(current)

        revision_changed = bool(
            expected_revision_id and expected_revision_id != current_revision_id
        )
        base_time = _parse_timestamp(base_updated_at)
        current_time = _parse_timestamp(current_updated_at)
        timestamp_changed = bool(base_time and current_time and current_time > base_time)
        if revision_changed or timestamp_changed:
            raise ConflictError(
                workflow_id,
                expected_revision_id=expected_revision_id,
                current_revision_id=current_revision_id,
                base_updated_at=base_updated_at,
                current_updated_at=current_updated_at,
                current_snapshot=current,
            )
        return {
            "revision_id": current_revision_id,
            "updated_at": current_updated_at,
            "snapshot": current,
        }

    def update_tree(self, workflow_id: str, *, elements: list | None = None,
                    links: list | None = None,
                    expected_revision_id: str | None = None,
                    base_updated_at: str | None = None) -> dict:
        """
        The master endpoint — add/update/delete elements and links in one
        call. This is what Jotform's own UI uses for every change, and
        it's the most reliable write path we found.
        """
        if expected_revision_id or base_updated_at:
            self.assert_workflow_revision(
                workflow_id,
                expected_revision_id=expected_revision_id,
                base_updated_at=base_updated_at,
            )

        wire_elements = []
        for element in elements or []:
            wire_element = deepcopy(element)
            data = wire_element.get("data")
            if isinstance(data, dict):
                wire_element["data"] = flatten_element_properties(data)
            wire_elements.append(wire_element)

        content = self._request(
            "PUT", f"/workflow/{workflow_id}/updateTree",
            json_body={"elements": wire_elements, "links": links or []},
        ).get("content", {})
        result = content.get("result") if isinstance(content, dict) else None
        if isinstance(result, dict) and isinstance(result.get("elements"), list):
            for item in result["elements"]:
                if isinstance(item, dict) and isinstance(item.get("data"), dict):
                    item["data"] = unflatten_element_properties(item["data"])
        return content

    def set_trigger_form(self, workflow_id: str, form_id: str) -> dict:
        """Bind a form to the workflow start point and persist it on the canvas."""
        self._request(
            "POST",
            f"/workflow/{workflow_id}/setResource",
            json_body={"resourceType": "FORM", "resourceID": form_id},
        )
        update_tree_url = f"/workflow/{workflow_id}/updateTree"
        tree_payload = {
            "links": [],
            "elements": [
                {
                    "elementID": 1,
                    "action": "update",
                    "data": {
                        "resourceID": form_id,
                        "resourceType": "FORM",
                        "element_id": 1,
                        "subType": "workflow_start_point_submission"
                    }
                }
            ]
        }

        return self._request("PUT", update_tree_url, json_body=tree_payload).get("content", {})

    def publish_workflow(self, workflow_id: str) -> dict:
        return self.update_workflow_metadata(workflow_id, status="ENABLED")

    def delete_workflow(self, workflow_id: str) -> dict:
        """
        Confirmed working 2026-08-10 (probes/test_delete_workflow.py) —
        DELETE /workflow/{id}, verified by checking the workflow no longer
        appears in list_workflows afterward, not just by the 200 response.
        """
        return self._request("DELETE", f"/workflow/{workflow_id}")
