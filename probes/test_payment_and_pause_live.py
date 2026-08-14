"""Live probes for payment verification and pause configuration persistence."""
from __future__ import annotations

import json
import os
from copy import deepcopy

from dotenv import load_dotenv

from mcp_server.jotform_client import JotformAPIError, JotformClient


PAYMENT_VERIFICATION_OUTCOMES = [
    {
        "id": 1,
        "outcomeID": 1,
        "type": "VERIFY",
        "buttonColor": "#01bd6f",
        "text": "Verify",
        "textColor": "#fff",
    },
    {
        "id": 2,
        "outcomeID": 2,
        "type": "NOT_VERIFY",
        "buttonColor": "#D53049",
        "text": "Not Verify",
        "textColor": "#fff",
    },
]

PROBES = [
    {
        "name": "payment_verification",
        "type": "workflow_payment_verification",
        "config": {
            "name": "Probe Verify Payment",
            "approver": "probe@example.com",
            "verificationMethod": "manual",
            "outcomes": PAYMENT_VERIFICATION_OUTCOMES,
        },
        "expect": {
            "type": "workflow_payment_verification",
            "verificationMethod": "manual",
            "outcomes": PAYMENT_VERIFICATION_OUTCOMES,
        },
        "needs_form_id": True,
    },
    {
        "name": "pause_duration_config",
        "type": "workflow_pause",
        "config": {
            "name": "Probe Pause Duration Config",
            "subType": "workflow_pause_duration",
            "pause": {
                "activated": "Yes",
                "executeWhen": {"afterAmount": "2", "afterUnit": "day"},
            },
        },
        "expect": {
            "type": "workflow_pause",
            "subType": "workflow_pause_duration",
            "pause": {
                "activated": "Yes",
                "executeWhen": {"afterAmount": "2", "afterUnit": "day"},
            },
        },
    },
    {
        "name": "pause_wait_config",
        "type": "workflow_pause",
        "config": {
            "name": "Probe Pause Wait Config",
            "subType": "workflow_pause_wait",
            "pause": {
                "activated": "Yes",
                "executeWhen": {"mode": "specified_date", "customDate": "2030-01-01"},
            },
        },
        "expect": {
            "type": "workflow_pause",
            "subType": "workflow_pause_wait",
            "pause": {
                "activated": "Yes",
                "executeWhen": {"mode": "specified_date", "customDate": "2030-01-01"},
            },
        },
    },
]


def nested_get(data: dict, path: str):
    current = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def matches(read_back: dict, expected: dict) -> tuple[bool, dict]:
    details = {}
    ok = True
    for key, expected_value in expected.items():
        actual = nested_get(read_back, key) if "." in key else read_back.get(key)
        same = actual == expected_value
        details[key] = {"ok": same, "actual": actual, "expected": expected_value}
        ok = ok and same
    return ok, details


def element_create(element_id: int, probe: dict, test_form_id: str | None) -> dict:
    x = (element_id - 2) * 360
    y = 520
    config = deepcopy(probe["config"])
    if probe.get("needs_form_id") and test_form_id:
        config["formID"] = test_form_id
        probe.setdefault("expect", {})["formID"] = test_form_id
    data = {
        "element_id": element_id,
        "id": element_id,
        "type": probe["type"],
        "elementType": probe["type"],
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
    workflow_id = os.environ.get("TEST_WORKFLOW_ID")
    test_form_id = os.environ.get("TEST_FORM_ID")
    if not workflow_id:
        raise RuntimeError("TEST_WORKFLOW_ID is required for this probe.")

    elements = client.get_elements(workflow_id)
    existing_ids = [
        int(item.get("element_id"))
        for item in elements
        if isinstance(item, dict) and str(item.get("element_id", "")).isdigit()
    ]
    next_element_id = max(existing_ids or [1]) + 1
    created_element_ids: list[int] = []
    results = []

    try:
        for offset, probe in enumerate(PROBES):
            element_id = next_element_id + offset
            result = {"probe": probe["name"], "element_id": element_id}
            try:
                client.update_tree(
                    workflow_id,
                    elements=[element_create(element_id, probe, test_form_id)],
                )
                created_element_ids.append(element_id)
                read_back = client.get_element(workflow_id, element_id)
                ok, details = matches(read_back, probe["expect"])
                result.update({
                    "ok": ok,
                    "type": read_back.get("type"),
                    "subType": read_back.get("subType"),
                    "keys": sorted(read_back.keys()),
                    "details": details,
                })
            except Exception as error:
                result.update({"ok": False, "error": str(error)})
            results.append(result)
    finally:
        if created_element_ids:
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

    print(json.dumps({
        "workflow_id": workflow_id,
        "created_element_ids": created_element_ids,
        "all_ok": all(item.get("ok") for item in results if "probe" in item),
        "results": results,
    }, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
