"""
Thin wrapper around the public Jotform API (api.jotform.com).

Deliberately does NOT touch www.jotform.com/API (the internal BFF) —
that surface rejects any call made outside a browser session with
"Cross-Site Requests not allowed!". Every endpoint used here was
individually verified working from outside a browser with just an
apiKey. See docs/gap-report.md for the evidence trail.
"""
from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

import requests

from mcp_server.audit_log import log_jotform_request

BASE_URL = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TIMEOUT = 20

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


class JotformClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("JOTFORM_API_KEY", "")
        if not self.api_key:
            raise ValueError("JOTFORM_API_KEY is not set")

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: Any = None, headers: dict | None = None) -> dict:
        params = dict(params or {})
        params["apiKey"] = self.api_key
        url = f"{BASE_URL}{path}"
        try:
            resp = log_jotform_request(
                method=method,
                url=url,
                params=params,
                json_body=json_body,
                send=lambda: requests.request(
                    method, url, params=params, json=json_body, headers=headers, timeout=TIMEOUT
                ),
            )
        except requests.Timeout as error:
            raise JotformAPIError(
                0, f"Request timed out for {method} {path}; no response was received"
            ) from error
        except requests.RequestException as error:
            # requests exceptions can include the prepared URL, and our API key
            # is a query parameter. Keep the MCP-facing error intentionally terse.
            raise JotformAPIError(
                0, f"Request failed for {method} {path} ({type(error).__name__})"
            ) from error
        if resp.status_code not in (200, 201):
            raise JotformAPIError(resp.status_code, resp.text.replace(self.api_key, "[REDACTED]"))
        try:
            return resp.json()
        except ValueError as error:
            raise JotformAPIError(
                resp.status_code,
                f"Invalid JSON response for {method} {path}",
            ) from error

    # ---------- forms ----------

    def list_forms(self, *, status: str | None = None) -> list[dict]:
        params = {}
        if status:
            params["filter"] = f'{{"status:eq":"{status}"}}'
        return self._request("GET", "/user/forms", params=params).get("content", [])

    def get_form_questions(self, form_id: str) -> dict:
        return self._request(
            "GET", f"/form/{form_id}/questions", params={"parseJSON": 1}
        ).get("content", {})

    def create_form_with_ai(
        self,
        prompt: str,
        *,
        form_type: str = "classic",
        language: str = "en",
    ) -> dict:
        """
        Creates a form from a natural-language prompt.

        Confirmed 2026-08-13 against api.jotform.com:
        POST /workflow/copilot/createWorkflowForm works with an apiKey, while
        the same path on www.jotform.com/API returns "Cross-Site Requests not
        allowed!".
        """
        return self._request(
            "POST",
            "/workflow/copilot/createWorkflowForm",
            json_body={
                "prompt": prompt,
                "formType": form_type,
                "preferences": {"language": language},
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "jf-v2-source": "mcp",
                "jf-v2-target": "workflow-copilot",
            },
        ).get("content", {})

    # ---------- workflows: read ----------

    def list_workflows(self) -> list[dict]:
        """Confirmed working 2026-08-07 (phase0_close_gaps.py)."""
        return self._request(
            "GET", "/user/workflows",
            params={
                "filter": '{"status:ne":["DELETED","PURGED","ARCHIVED"],"type:eq":"default"}',
                "orderby": "updated_at", "limit": 50, "offset": 0,
            },
        ).get("content", [])

    def get_workflow_combined(self, workflow_id: str) -> dict:
        """
        Metadata + elements + links in one call. Confirmed working
        2026-08-07 — preferred over three separate calls.
        """
        content = self._request(
            "GET", f"/workflow/{workflow_id}/combined",
            params={"fetchEssentialElementProps": 1},
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

    def create_workflow(self, title: str, *, trigger_on_edit: str = "ENABLED") -> dict:
        return self._request(
            "POST", "/workflow",
            json_body={
                "title": title,
                "triggerOnEdit": trigger_on_edit,
                "elements": [{
                    "action": "update", "elementID": 1,
                    "data": {
                        "element_id": 1, "id": 1,
                        "type": "workflow_start_point",
                        "elementType": "workflow_start_point",
                        "subType": "workflow_start_point_submission",
                        "className": ["isStartPoint"],
                        "position": {"x": 0, "y": 0}, "x": 0, "y": 0,
                        "measured": {"width": 296, "height": 88},
                    },
                }],
                "links": [],
            },
        ).get("content", {})

    def update_workflow_metadata(self, workflow_id: str, **fields) -> dict:
        return self._request("POST", f"/workflow/{workflow_id}", json_body=fields).get("content", {})

    def create_element(self, workflow_id: str, step_type: str) -> dict:
        """Creates a bare element. Only `type` is required; config comes after."""
        return self._request(
            "POST", f"/workflow/{workflow_id}/elements",
            json_body={"type": step_type},
        ).get("content", {})

    def update_tree(self, workflow_id: str, *, elements: list | None = None,
                    links: list | None = None) -> dict:
        """
        The master endpoint — add/update/delete elements and links in one
        call. This is what Jotform's own UI uses for every change, and
        it's the most reliable write path we found.
        """
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
        """
        Binds a specific form to a workflow's starting point (Element 1).
        Based on UI behavior, this requires two sequential API calls.
        """
        # Adım 1: Workspace/İlişki kaydı için setResource çağrısı
        set_resource_url = f"/workflow/{workflow_id}/setResource"
        resource_payload = {
            "resourceType": "FORM",
            "resourceID": form_id
        }
        self._request("POST", set_resource_url, json_body=resource_payload)

        # Adım 2: Canvas üzerindeki başlangıç noktasını (Element 1) güncelleme
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
        
        # Asıl bağlama işleminin yapıldığı updateTree çağrısını döndürüyoruz
        return self._request("PUT", update_tree_url, json_body=tree_payload).get("content", {})

    def publish_workflow(self, workflow_id: str) -> dict:
        return self._request("POST", f"/workflow/{workflow_id}/publish").get("content", {})

    def delete_workflow(self, workflow_id: str) -> dict:
        """
        Confirmed working 2026-08-10 (probes/test_delete_workflow.py) —
        DELETE /workflow/{id}, verified by checking the workflow no longer
        appears in list_workflows afterward, not just by the 200 response.
        """
        return self._request("DELETE", f"/workflow/{workflow_id}")
