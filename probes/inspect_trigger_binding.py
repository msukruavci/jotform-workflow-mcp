"""
Where does setResource actually record the trigger form binding?

test_set_trigger_form.py checked the start point element's resourceID —
it stayed null after a 200 response from setResource. Two live
possibilities: the binding lives on the *workflow* object instead of the
element, or setResource genuinely doesn't do anything from the public
surface (the internal BFF version was confirmed CSRF-blocked; this would
mean the public one is a no-op look-alike, not the working equivalent
Phase 0 hypothesized).

This dumps every top-level key on both the workflow metadata and the
start point element, before and after, and prints only the ones that
differ — so the binding (if it exists anywhere in this response shape)
shows up on its own, without guessing a field name in advance.

Needs TEST_FORM_ID in .env.

Run:
    python -m probes.inspect_trigger_binding
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402


def snapshot(client: JotformClient, workflow_id: str) -> dict:
    combined = client.get_workflow_combined(workflow_id)
    meta = client.get_workflow(workflow_id)  # separate endpoint, own field set
    start = next(
        (e for e in (combined.get("elements") or [])
         if isinstance(e, dict) and e.get("type") == "workflow_start_point"),
        {},
    )
    return {"workflow_metadata": meta, "workflow_combined": combined.get("workflow", {}),
           "start_point_element": start}


def diff(before: dict, after: dict, label: str) -> None:
    keys = set(before) | set(after)
    changed = {k for k in keys if before.get(k) != after.get(k)}
    print(f"\n--- {label}: {len(changed)} field(s) changed ---")
    for k in sorted(changed):
        print(f"  {k}: {before.get(k)!r} -> {after.get(k)!r}")
    if not changed:
        print("  (nothing)")


def main() -> int:
    client = JotformClient()
    form_id = os.environ.get("TEST_FORM_ID", "")
    if not form_id:
        print("Set TEST_FORM_ID in .env first.")
        return 1

    title = f"ZZ-triggerbind-DELETE-ME-{datetime.now():%Y%m%d-%H%M%S}"
    created = client.create_workflow(title)
    workflow_id = created.get("id") or created.get("workflowID")
    if not workflow_id:
        print(f"Could not create workflow: {json.dumps(created)[:200]}")
        return 1
    print(f"Workflow: {title} ({workflow_id})")

    before = snapshot(client, workflow_id)

    print(f"\nCalling setResource(form={form_id})...")
    try:
        result = client.set_trigger_form(workflow_id, form_id)
        print(f"Response: {json.dumps(result)}")
    except JotformAPIError as e:
        print(f"[FAIL] {e}")
        cleanup(client, workflow_id)
        return 1

    after = snapshot(client, workflow_id)

    diff(before["workflow_metadata"], after["workflow_metadata"], "GET /workflow/{id} (metadata)")
    diff(before["workflow_combined"], after["workflow_combined"], "combined -> workflow{}")
    diff(before["start_point_element"], after["start_point_element"], "start point element")

    print("\n" + "=" * 70)
    any_change = any([
        before["workflow_metadata"] != after["workflow_metadata"],
        before["workflow_combined"] != after["workflow_combined"],
        before["start_point_element"] != after["start_point_element"],
    ])
    if any_change:
        print("[FOUND] Something changed — see the diff above for the real field.")
        print("Update jotform_client / reading.py to read the binding from there.")
    else:
        print("[CONCLUSION] Nothing readable changed anywhere this probe checked, even")
        print("though the call returned `true`. setResource on the public surface")
        print("looks like a no-op — likely the same CSRF-style gate as the internal-bff")
        print("version, just responding 200 instead of erroring. create_workflow's")
        print("trigger_form_id parameter should be treated as NOT WORKING until proven")
        print("otherwise, and its silent-success path is actively misleading — it should")
        print("either warn plainly or be removed.")

    cleanup(client, workflow_id)
    return 0


def cleanup(client: JotformClient, workflow_id: str) -> None:
    try:
        client.delete_workflow(workflow_id)
        print(f"\n[OK] cleaned up {workflow_id}")
    except JotformAPIError as e:
        print(f"\n[WARN] cleanup failed: {e}")


if __name__ == "__main__":
    sys.exit(main())