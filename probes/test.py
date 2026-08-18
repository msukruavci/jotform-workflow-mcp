"""
Creates a visible workflow in the Jotform UI and binds a trigger form to it.
Does NOT delete the workflow at the end so it can be inspected in the browser.

Needs TEST_FORM_ID in .env.

Run:
    python -m probes.create_visible_workflow
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient


def main() -> int:
    client = JotformClient()
    form_id = os.environ.get("TEST_FORM_ID", "")
    if not form_id:
        print("Set TEST_FORM_ID in .env first — pick a real form id.")
        return 1

    title = f"UI-Test-Workflow-{datetime.now():%Y%m%d-%H%M%S}"
    print(f"1. Creating workflow: {title}...")
    
    try:
        created = client.create_workflow(title)
        workflow_id = created.get("id") or created.get("workflowID")
        if not workflow_id:
            print(f"[FAIL] Could not create workflow: {created}")
            return 1
        print(f"-> Created Workflow ID: {workflow_id}")

        print(f"\n2. Binding form {form_id} to Element 1...")
        client.set_trigger_form(workflow_id, form_id)
        print("-> Binding successful!")

        print("\n" + "=" * 70)
        print("✨ SUCCESS! Workflow is ready to view in the UI.")
        print("Click the link below to verify the trigger binding:")
        print(f"👉 https://www.jotform.com/workflow/{workflow_id}/build")
        print("=" * 70 + "\n")
        
        return 0

    except JotformAPIError as e:
        print(f"\n[FAIL] API Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())