"""
Does publish_workflow actually work?

Deliberately never called anywhere else in this project — publishing
makes a workflow live. Tested here, once, on a throwaway workflow created
specifically for this, then deleted immediately after. Never run against
a real workflow.

Tries publishing a bare workflow (start point only) first. If Jotform
requires a bound trigger form to publish, binds TEST_FORM_ID and retries —
that requirement, if it exists, is itself worth knowing (it would mean
publish_workflow's tool docstring should say so).

Also checks whether a published workflow can still be deleted via
delete_workflow — that's new information delete_workflow's probe never
covered, since it was only ever tested against unpublished throwaways.

Run:
    python -m probes.test_publish_workflow
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
    title = f"ZZ-publish-DELETE-ME-{datetime.now():%Y%m%d-%H%M%S}"

    created = client.create_workflow(title)
    workflow_id = created.get("id") or created.get("workflowID")
    if not workflow_id:
        print(f"Could not create workflow: {json.dumps(created)[:200]}")
        return 1
    print(f"Workflow: {title} ({workflow_id})")

    meta_before = client.get_workflow(workflow_id)
    print(f"publishStatus before: {meta_before.get('publishStatus')!r}, "
          f"hasPublishedFlow: {meta_before.get('hasPublishedFlow')!r}")

    print("\nAttempting publish on a bare workflow (no trigger form)...")
    try:
        result = client.publish_workflow(workflow_id)
        print(f"[OK] accepted: {json.dumps(result)[:200]}")
    except JotformAPIError as e:
        print(f"[REJECTED] {e.status}: {e.body[:200]}")
        form_id = os.environ.get("TEST_FORM_ID", "")
        if not form_id:
            print("\nNo TEST_FORM_ID in .env to retry with a bound trigger form.")
            print("Set one and re-run if this rejection looks trigger-related.")
            cleanup(client, workflow_id)
            return 1

        print(f"\nBinding trigger form {form_id} and retrying...")
        try:
            client.set_trigger_form(workflow_id, form_id)
            result = client.publish_workflow(workflow_id)
            print(f"[OK] accepted after binding a trigger form: {json.dumps(result)[:200]}")
            print("\n[FINDING] publish requires a bound trigger form. "
                  "publish_workflow's docstring/preview should say this.")
        except JotformAPIError as e2:
            print(f"[STILL REJECTED] {e2.status}: {e2.body[:200]}")
            cleanup(client, workflow_id)
            return 1

    meta_after = client.get_workflow(workflow_id)
    print(f"\npublishStatus after: {meta_after.get('publishStatus')!r}, "
          f"hasPublishedFlow: {meta_after.get('hasPublishedFlow')!r}")

    changed = (meta_after.get("publishStatus") != meta_before.get("publishStatus")
              or meta_after.get("hasPublishedFlow") != meta_before.get("hasPublishedFlow"))

    print("\n" + "=" * 70)
    if changed:
        print("[CONFIRMED] publish_workflow works — metadata actually changed, not")
        print("just a 200. Safe to trust as shipped (still confirm-gated, correctly).")
    else:
        print("[NOT CONFIRMED] 200 came back but no metadata field changed that we")
        print("checked. Either the real signal is a field we're not reading, or the")
        print("call had no effect. Don't remove publish_workflow's caution based on")
        print("this alone.")

    cleanup(client, workflow_id)
    return 0 if changed else 1


def cleanup(client: JotformClient, workflow_id: str) -> None:
    print(f"\nCleaning up: deleting {workflow_id} (now published — new territory for delete)...")
    try:
        client.delete_workflow(workflow_id)
        print("[OK] a published workflow can also be deleted via the public API.")
    except JotformAPIError as e:
        print(f"[WARN] delete failed on a published workflow: {e}")
        print("Delete it manually from the UI. Also a real finding — note in")
        print("gap-report.md if this happens: delete_workflow may need to check")
        print("publish status first, or unpublish before deleting.")


if __name__ == "__main__":
    sys.exit(main())