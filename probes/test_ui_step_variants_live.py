"""Live probe for UI-facing workflow step variants.

Creates one temporary workflow, writes candidate canonical type + subType
payloads, reads every element back, then deletes the workflow.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from mcp_server.jotform_client import JotformAPIError, JotformClient


VARIANTS = [
    {
        "step_type": "workflow_approval_with_sign",
        "canonical_type": "workflow_approval",
        "subtype": "workflow_approval_with_sign",
        "ui_name": "Approve & Sign",
        "config": {"name": "Probe Approve & Sign", "approver": "probe@example.com"},
    },
    {
        "step_type": "workflow_team_approval",
        "canonical_type": "workflow_approval",
        "subtype": "workflow_team_approval",
        "ui_name": "Team Approval",
        "config": {"name": "Probe Team Approval", "team": "probe-team"},
    },
    {
        "step_type": "workflow_send_pdf",
        "canonical_type": "workflow_send_email",
        "subtype": "workflow_send_pdf",
        "ui_name": "PDF",
        "config": {
            "name": "Probe PDF",
            "to": [{"text": "probe@example.com", "value": "probe@example.com"}],
            "subject": "Probe PDF",
            "content": "Probe PDF",
        },
    },
    {
        "step_type": "workflow_send_approval_report",
        "canonical_type": "workflow_send_email",
        "subtype": "workflow_send_approval_report",
        "ui_name": "Flow Report",
        "config": {
            "name": "Probe Flow Report",
            "to": [{"text": "probe@example.com", "value": "probe@example.com"}],
            "subject": "Probe Flow Report",
            "content": "Probe Flow Report",
        },
    },
    {
        "step_type": "workflow_payment_form",
        "canonical_type": "workflow_assign_form",
        "subtype": "workflow_payment_form",
        "ui_name": "Payment Form",
        "config": {"name": "Probe Payment Form", "assignee": "probe@example.com"},
    },
    {
        "step_type": "workflow_pause_duration",
        "canonical_type": "workflow_pause",
        "subtype": "workflow_pause_duration",
        "ui_name": "Wait for Duration",
        "config": {
            "name": "Probe Wait Duration",
            "pause": {
                "activated": "Yes",
                "executeWhen": {"afterAmount": "1", "afterUnit": "day"},
            },
        },
    },
    {
        "step_type": "workflow_pause_wait",
        "canonical_type": "workflow_pause",
        "subtype": "workflow_pause_wait",
        "ui_name": "Wait Until",
        "config": {
            "name": "Probe Wait Until",
            "pause": {
                "activated": "Yes",
                "executeWhen": {"mode": "specified_date", "customDate": "2030-01-01"},
            },
        },
    },
]


def element_create(element_id: int, variant: dict, test_form_id: str | None) -> dict:
    x = (element_id - 2) * 360
    y = 260
    config = dict(variant["config"])
    if variant["step_type"] == "workflow_payment_form" and test_form_id:
        config.setdefault("formID", test_form_id)
    data = {
        "element_id": element_id,
        "id": element_id,
        "type": variant["canonical_type"],
        "elementType": variant["canonical_type"],
        "subType": variant["subtype"],
        "position": {"x": x, "y": y},
        "x": x,
        "y": y,
        "measured": {"width": 296, "height": 88},
        **config,
    }
    return {"action": "create", "elementID": element_id, "data": data}


def element_delete(element_id: int) -> dict:
    return {
        "action": "delete",
        "elementID": element_id,
        "data": {"element_id": element_id},
    }


def main() -> int:
    load_dotenv()
    client = JotformClient()
    workflow_id = None
    created_workflow = False
    created_element_ids: list[int] = []
    results = []
    title = "MCP UI variant probe " + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    test_form_id = os.environ.get("TEST_FORM_ID")

    try:
        workflow_id = os.environ.get("TEST_WORKFLOW_ID")
        if workflow_id:
            elements = client.get_elements(workflow_id)
        else:
            workflow = client.create_workflow(title)
            workflow_id = str(workflow.get("id"))
            created_workflow = True
            if not workflow_id or workflow_id == "None":
                raise RuntimeError(f"create_workflow did not return an id: {workflow!r}")
            elements = client.get_elements(workflow_id)

        existing_ids = [
            int(item.get("element_id"))
            for item in elements
            if isinstance(item, dict) and str(item.get("element_id", "")).isdigit()
        ]
        next_element_id = max(existing_ids or [1]) + 1

        for offset, variant in enumerate(VARIANTS):
            element_id = next_element_id + offset
            result = {
                "step_type": variant["step_type"],
                "ui_name": variant["ui_name"],
                "expected_type": variant["canonical_type"],
                "expected_subtype": variant["subtype"],
                "element_id": element_id,
            }
            try:
                client.update_tree(
                    workflow_id,
                    elements=[element_create(element_id, variant, test_form_id)],
                )
                created_element_ids.append(element_id)
                read_back = client.get_element(workflow_id, element_id)
                result.update({
                    "ok": (
                        read_back.get("type") == variant["canonical_type"]
                        and read_back.get("subType") == variant["subtype"]
                    ),
                    "actual_type": read_back.get("type"),
                    "actual_subtype": read_back.get("subType"),
                    "actual_name": read_back.get("name"),
                    "keys": sorted(read_back.keys()),
                })
            except Exception as error:
                result.update({"ok": False, "error": str(error)})
            results.append(result)
    finally:
        if workflow_id and created_element_ids and not created_workflow:
            try:
                client.update_tree(
                    workflow_id,
                    elements=[element_delete(element_id) for element_id in created_element_ids],
                )
            except JotformAPIError as error:
                results.append({
                    "cleanup_error": str(error),
                    "workflow_id": workflow_id,
                    "element_ids": created_element_ids,
                })
        if workflow_id and created_workflow:
            try:
                client.delete_workflow(workflow_id)
            except JotformAPIError as error:
                results.append({"cleanup_error": str(error), "workflow_id": workflow_id})

    print(json.dumps({
        "workflow_id": workflow_id,
        "created_workflow": created_workflow,
        "created_element_ids": created_element_ids,
        "all_ok": all(item.get("ok") for item in results if "step_type" in item),
        "results": results,
    }, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
