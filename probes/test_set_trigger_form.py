"""
Does setResource + updateTree actually bind a trigger form to a workflow?

Creates a throwaway workflow, calls the updated 2-step set_trigger_form, 
reads the workflow back, and strictly verifies if the start point element's 
data holds the new resourceID and subType.

Needs TEST_FORM_ID in .env — a real form id from list_forms.

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
    start_before_element = next(
        (e for e in (before.get("elements") or [])
         if isinstance(e, dict) and e.get("type") == "workflow_start_point"),
        {},
    )
    # Değerler data içinde veya kökte olabilir, güvenli okuma yapıyoruz
    data_before = start_before_element.get("data", start_before_element)
    
    print(f"Start point before: resourceID={data_before.get('resourceID')!r}, "
          f"subType={data_before.get('subType')!r}")

    print(f"\nCalling 2-step set_trigger_form with form {form_id}...")
    try:
        result = client.set_trigger_form(workflow_id, form_id)
    except JotformAPIError as e:
        print(f"[FAIL] set_trigger_form rejected: {e}")
        return 1

    print(f"Response: {json.dumps(result)[:200]}")

    after = client.get_workflow_combined(workflow_id)
    start_after_element = next(
        (e for e in (after.get("elements") or [])
         if isinstance(e, dict) and e.get("type") == "workflow_start_point"),
        {},
    )
    
    # Jotform veriyi 'data' objesi içine yazar
    data_after = start_after_element.get("data", start_after_element)
    
    res_id_persisted = str(data_after.get('resourceID', ''))
    subtype_persisted = str(data_after.get('subType', ''))
    
    print(f"\nStart point after: resourceID={res_id_persisted!r}, "
          f"subType={subtype_persisted!r}")

    # Hem ID'nin hem de tetikleyici türünün doğru değiştiğini teyit ediyoruz
    bound = (res_id_persisted == str(form_id)) and (subtype_persisted == "workflow_start_point_submission")

    print("\n" + "=" * 70)
    if bound:
        print(f"[CONFIRMED] ✨ 2-Step Binding Works — start point is now perfectly bound to form {form_id}.")
        print("create_workflow's trigger_form_id parameter is completely safe to trust.")
    else:
        print("[NOT CONFIRMED] ❌ 200 came back but the element data did not fully match.")
        print(f"Expected resourceID: {form_id}, Got: {res_id_persisted}")
        print(f"Expected subType: workflow_start_point_submission, Got: {subtype_persisted}")
        print(f"Full element dump:\n{json.dumps(start_after_element, indent=2)}")

    print(f"\nCleaning up: deleting {workflow_id}...")
    try:
        client.delete_workflow(workflow_id)
        print("[OK] deleted.")
    except JotformAPIError as e:
        print(f"[WARN] cleanup failed, delete manually: {e}")

    return 0 if bound else 1


if __name__ == "__main__":
    sys.exit(main())