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
from typing import Any

import requests

BASE_URL = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TIMEOUT = 20


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
                 json_body: Any = None) -> dict:
        params = dict(params or {})
        params["apiKey"] = self.api_key
        resp = requests.request(
            method, f"{BASE_URL}{path}", params=params, json=json_body, timeout=TIMEOUT
        )
        if resp.status_code not in (200, 201):
            raise JotformAPIError(resp.status_code, resp.text)
        return resp.json()

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
        return self._request(
            "GET", f"/workflow/{workflow_id}/combined",
            params={"fetchEssentialElementProps": 1},
        ).get("content", {})

    def get_workflow(self, workflow_id: str) -> dict:
        return self._request("GET", f"/workflow/{workflow_id}").get("content", {})

    def get_elements(self, workflow_id: str) -> list:
        content = self._request("GET", f"/workflow/{workflow_id}/elements").get("content", [])
        return content if isinstance(content, list) else []

    def get_element(self, workflow_id: str, element_id: int | str) -> dict:
        """Returns the FULL config for one element (the list endpoint only summarizes)."""
        return self._request(
            "GET", f"/workflow/{workflow_id}/elements/{element_id}"
        ).get("content", {})

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
        return self._request(
            "PUT", f"/workflow/{workflow_id}/updateTree",
            json_body={"elements": elements or [], "links": links or []},
        ).get("content", {})

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