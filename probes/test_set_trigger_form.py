"""
Does setResource actually bind a trigger form to a workflow?

create_workflow's trigger_form_id parameter calls
client.set_trigger_form() -> POST /workflow/{id}/setResource, inherited
from the original Phase 0 client with no independent confirmation this
project ever ran. Untested code path in a tool that's already shipped.

Method: create a bare workflow, call setResource with a real form id, read
the workflow back, check whether the start point element's resourceID
actually changed to that form. A 200 alone doesn't prove the bind stuck —
same lesson as every other write path in this project.

Needs TEST_FORM_ID in .env — a real form id from list_forms.

Creates a throwaway workflow, cleans it up at the end via delete_workflow
(confirmed working).

Run:
    python -m probes.test_set_trigger_form
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402


def main() -> int:
    client = JotformClient()
    form_id = os.environ.get("TEST_FORM_ID", "")
    if not form_id:
        print("Set TEST_FORM_ID in .env first — pick a real form id from list_forms.")
        return 1

    title = f"ZZ-setresource-DELETE-ME-{datetime.now():%Y%m%d-%H%M%S}"
    created = client.create_workflow(title)
    workflow_id = created.get("id") or created.get("workflowID")
    if not workflow_id:
        print(f"Could not create workflow: {json.dumps(created)[:200]}")
        return 1
    print(f"Workflow: {title} ({workflow_id})")

    before = client.get_workflow_combined(workflow_id)
    start_before = next(
        (e for e in (before.get("elements") or [])
         if isinstance(e, dict) and e.get("type") == "workflow_start_point"),
        {},
    )
    print(f"Start point before: resourceID={start_before.get('resourceID')!r}, "
          f"resourceType={start_before.get('resourceType')!r}")

    print(f"\nCalling setResource with form {form_id}...")
    try:
        result = client.set_trigger_form(workflow_id, form_id)
    except JotformAPIError as e:
        print(f"[FAIL] setResource rejected: {e}")
        print("\nCode calling this (create_workflow's trigger_form_id) is unsafe as")
        print("shipped — it swallows this failure into a soft error field, but the")
        print("tool result should make clear the workflow was NOT bound to a form.")
        return 1

    print(f"Response: {json.dumps(result)[:200]}")

    after = client.get_workflow_combined(workflow_id)
    start_after = next(
        (e for e in (after.get("elements") or [])
         if isinstance(e, dict) and e.get("type") == "workflow_start_point"),
        {},
    )
    print(f"\nStart point after: resourceID={start_after.get('resourceID')!r}, "
          f"resourceType={start_after.get('resourceType')!r}")

    bound = str(start_after.get("resourceID")) == str(form_id)

    print("\n" + "=" * 70)
    if bound:
        print(f"[CONFIRMED] setResource works — start point now bound to form {form_id}.")
        print("create_workflow's trigger_form_id parameter is safe to trust as-is.")
    else:
        print("[NOT CONFIRMED] 200 came back but resourceID did not change to the")
        print("form id sent. Either a different field holds the binding, or the")
        print("call had no real effect. Do not trust create_workflow's")
        print("trigger_form_id until this is resolved — check the full element")
        print(f"dump: {json.dumps(start_after, indent=2)[:500]}")

    print(f"\nCleaning up: deleting {workflow_id}...")
    try:
        client.delete_workflow(workflow_id)
        print("[OK] deleted.")
    except JotformAPIError as e:
        print(f"[WARN] cleanup failed, delete manually: {e}")

    return 0 if bound else 1


if __name__ == "__main__":
    sys.exit(main())