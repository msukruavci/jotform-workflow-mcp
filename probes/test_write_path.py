"""
Does the write path actually work from outside a browser?

Phase 3 assumes four things. None have been exercised in one run:
  1. POST /workflow                     — create a workflow
  2. PUT  /workflow/{id}/updateTree     — add an element
  3. PUT  .../updateTree (link)         — connect two elements
  4. PUT  .../updateTree (delete)       — remove an element

If any fails, the Phase 3 design changes. Better to find out in ten
minutes than after three days of building tree_builder.

Creates a throwaway workflow on your own account with an obvious name.
Nothing touches an existing workflow. Publishing is deliberately NOT
tested — publishing makes a workflow live, and a probe should not.

Run:
    python -m probes.test_write_path
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402

RESULTS: list[dict] = []


def step(label: str, fn):
    try:
        value = fn()
        RESULTS.append({"step": label, "ok": True})
        print(f"[PASS] {label}")
        return value
    except JotformAPIError as e:
        RESULTS.append({"step": label, "ok": False, "reason": f"{e.status}: {e.body[:200]}"})
        print(f"[FAIL] {label}\n       HTTP {e.status}: {e.body[:200]}")
        return None
    except Exception as e:  # noqa: BLE001
        RESULTS.append({"step": label, "ok": False, "reason": f"{type(e).__name__}: {e}"})
        print(f"[FAIL] {label}\n       {type(e).__name__}: {e}")
        return None


def main() -> int:
    client = JotformClient()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    title = f"ZZ-probe-DELETE-ME-{stamp}"

    print("=" * 70)
    print(f"Throwaway workflow: {title}")
    print("=" * 70)

    created = step("1. create workflow", lambda: client.create_workflow(title))
    if not created:
        print("\nWrite path is closed at the first step. Phase 3 cannot proceed")
        print("as designed — this is a finding for the gap report, not a bug.")
        return summarize()

    workflow_id = created.get("id") or created.get("workflowID")
    print(f"       id: {workflow_id}")
    if not workflow_id:
        print(f"       !! no id in response: {json.dumps(created)[:200]}")
        return summarize()

    # Element ids are ours to choose in updateTree; the start point is 1.
    email_id = 2

    step("2. add an email element", lambda: client.update_tree(
        workflow_id,
        elements=[{
            "action": "create",
            "elementID": email_id,
            "data": {
                "element_id": email_id,
                "id": email_id,
                "type": "workflow_send_email",
                "elementType": "workflow_send_email",
                "name": "Probe email",
                "position": {"x": 0, "y": 200}, "x": 0, "y": 200,
                "measured": {"width": 296, "height": 88},
            },
        }],
    ))

    step("3. link start -> email", lambda: client.update_tree(
        workflow_id,
        links=[{
            "action": "create",
            "linkID": 1,
            "data": {"link_id": 1, "fromElement": 1, "toElement": email_id},
        }],
    ))

    # Read back. A write that returns 200 but changes nothing is the failure
    # mode worth catching — the response body alone does not prove persistence.
    def verify():
        combined = client.get_workflow_combined(workflow_id)
        elements = combined.get("elements") or []
        links = combined.get("links") or []
        types = [e.get("type") for e in elements if isinstance(e, dict)]
        print(f"       read back: {len(elements)} elements {types}, {len(links)} links")
        if "workflow_send_email" not in types:
            raise RuntimeError("email element did not persist")
        if not links:
            raise RuntimeError("link did not persist")
        return combined

    step("4. read back and confirm both persisted", verify)

    step("5. delete the email element", lambda: client.update_tree(
        workflow_id,
        elements=[{"action": "delete", "elementID": email_id, "data": {"element_id": email_id}}],
    ))

    def verify_deleted():
        combined = client.get_workflow_combined(workflow_id)
        types = [e.get("type") for e in (combined.get("elements") or []) if isinstance(e, dict)]
        print(f"       read back: {types}")
        if "workflow_send_email" in types:
            raise RuntimeError("element still present after delete")
        return True

    step("6. confirm the delete stuck", verify_deleted)

    print(f"\nLeft behind on your account: {title} ({workflow_id})")
    print("Delete it from the Jotform UI when you're done looking at it.")
    return summarize()


def summarize() -> int:
    passed = sum(r["ok"] for r in RESULTS)
    print()
    print("=" * 70)
    print(f"{passed}/{len(RESULTS)} passed")
    for r in RESULTS:
        if not r["ok"]:
            print(f"  FAIL {r['step']}: {r['reason']}")
    print("=" * 70)
    with open("probes/write_path_result.json", "w") as f:
        json.dump(RESULTS, f, indent=2)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
