"""
Phase 0: close the three gaps blocking the MCP architecture.

1. list_workflows — HAR shows the UI calls
   www.jotform.com/API/listings/user/workflows, and the response header
   x-raw-uri says the server sees it as "user/workflows". So try that
   path (and a few variants) on api.jotform.com.
2. /combined — the endpoints doc mentions
   GET /workflow/{id}/combined?fetchEssentialElementProps=1 which would
   fetch metadata + elements + links in one call. Never tested publicly.
3. delete — we know the updateTree delete schema from a manual UI delete
   captured in HAR, but have NEVER tried it via the public API. Tested
   here against a disposable element in a scratch workflow.

Run: python -m probes.phase0_close_gaps
"""
import json
import os

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
SCRATCH_WORKFLOW_ID = os.environ.get("SCRATCH_WORKFLOW_ID", "262183096859975")
ORIGINAL_WORKFLOW_ID = os.environ.get("TEST_WORKFLOW_ID", "")


def parse(record):
    try:
        return json.loads(record["response_snippet"])
    except (ValueError, KeyError):
        return None


def main() -> None:
    print("=" * 60)
    print("GAP 1: list workflows")
    print("=" * 60)
    # The HAR path was /API/listings/user/workflows but the server's
    # x-raw-uri header reported it as "user/workflows" — so the listings/
    # prefix is likely frontend routing, not part of the real route.
    for path in ["/user/workflows", "/listings/user/workflows", "/workflows"]:
        probe("GET", f"{BASE}{path}", label=f"list_workflows_via{path.replace('/', '_')}",
              surface="public-api")

    # Also try with the same filter the UI sends, in case a bare call is
    # rejected but a filtered one works.
    probe("GET", f"{BASE}/user/workflows",
          label="list_workflows_with_ui_filter", surface="public-api",
          params={
              "filter": '{"status:ne":["DELETED","PURGED","ARCHIVED"],"type:eq":"default"}',
              "offset": 0, "orderby": "updated_at", "limit": 50,
          })

    print("\n" + "=" * 60)
    print("GAP 2: /combined (everything in one call)")
    print("=" * 60)
    if ORIGINAL_WORKFLOW_ID:
        probe("GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}/combined",
              label="workflow_combined", surface="public-api",
              params={"fetchEssentialElementProps": 1})

    print("\n" + "=" * 60)
    print("GAP 3: delete via updateTree (on scratch workflow only)")
    print("=" * 60)
    # Create a throwaway element first, so we're deleting something we
    # just made rather than touching anything that matters.
    created = probe("POST", f"{BASE}/workflow/{SCRATCH_WORKFLOW_ID}/elements",
                    label="delete_test_create_victim", surface="public-api",
                    json_body={"type": "workflow_placeholder"})
    body = parse(created)
    if not body or created["status"] != 200:
        print("Couldn't create a throwaway element — skipping delete test.")
        return
    victim_id = body["content"]["element_id"]
    print(f"Created throwaway element {victim_id}, now deleting it...")

    probe("PUT", f"{BASE}/workflow/{SCRATCH_WORKFLOW_ID}/updateTree",
          label="delete_test_delete", surface="public-api",
          json_body={
              "links": [],
              "elements": [{"action": "delete", "elementID": victim_id,
                            "data": {"element_id": victim_id}}],
          })

    # Verify it's actually gone.
    check = probe("GET", f"{BASE}/workflow/{SCRATCH_WORKFLOW_ID}/elements/{victim_id}",
                  label="delete_test_verify_gone", surface="public-api")
    if check["status"] == 200:
        print(f"\nElement {victim_id} still readable after delete — "
              f"check whether it's actually removed or just flagged.")
    else:
        print(f"\nElement {victim_id} no longer readable (status "
              f"{check['status']}) — delete appears to work.")


if __name__ == "__main__":
    main()
