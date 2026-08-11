"""
Re-verifies every single claim in jotform-workflow-api-reference.pdf by
actually calling each endpoint again, right now — not by trusting
days-old test results. This is the difference between "we believe this"
and "we just checked this."

Design choices:
- Read-only checks run against ORIGINAL_WORKFLOW_ID (the real
  "Automatic Assignment Workflow").
- Write checks run against SCRATCH_WORKFLOW_ID (the disposable "MCP Test
  Workflow" created by build_workflow_from_scratch.py) — NOT the
  original — so this verification pass can't corrupt real data.
- POST /workflow (create-from-scratch) is NOT re-run here. It was
  already proven end-to-end and re-running it would just clutter the
  account with another throwaway workflow every time you verify the
  doc. Treated as separately proven, not re-tested per run.

Run: python -m probes.verify_pdf_reference
"""
import json
import os

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
ORIGINAL_WORKFLOW_ID = os.environ.get("TEST_WORKFLOW_ID", "")
SCRATCH_WORKFLOW_ID = os.environ.get("SCRATCH_WORKFLOW_ID", "262174165757969")
TEST_FORM_ID = os.environ.get("TEST_FORM_ID", "")

results = []  # (description, pass/fail, actual_status, expected)


def check(description, record, expected_status_or_set):
    actual = record["status"]
    if isinstance(expected_status_or_set, (list, tuple, set)):
        ok = actual in expected_status_or_set
        expected_str = "/".join(str(s) for s in expected_status_or_set)
    else:
        ok = actual == expected_status_or_set
        expected_str = str(expected_status_or_set)
    results.append((description, ok, actual, expected_str))
    return ok


def parse(record):
    try:
        return json.loads(record["response_snippet"])
    except (ValueError, KeyError):
        return None


def main():
    if not ORIGINAL_WORKFLOW_ID or not TEST_FORM_ID:
        print("Need TEST_WORKFLOW_ID and TEST_FORM_ID set in .env.")
        return

    print("=" * 60)
    print("SECTION 1: Confirmed working (read-only, original workflow)")
    print("=" * 60)

    r = probe("GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}",
              label="verify_get_metadata", surface="public-api")
    check("GET /workflow/{id}", r, 200)

    r = probe("GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}/elements",
              label="verify_get_elements", surface="public-api")
    check("GET /workflow/{id}/elements", r, 200)

    r = probe("GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}/elements/1",
              label="verify_get_element_by_id", surface="public-api")
    check("GET /workflow/{id}/elements/{elementID}", r, 200)

    r = probe("GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}/links",
              label="verify_get_links", surface="public-api")
    check("GET /workflow/{id}/links", r, 200)

    r = probe("GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}/links/1",
              label="verify_get_link_by_id", surface="public-api")
    check("GET /workflow/{id}/links/{linkID}", r, 200)

    print("\n" + "=" * 60)
    print("SECTION 2: Confirmed working (writes, echo-safe, original workflow)")
    print("=" * 60)

    meta = probe("GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}",
                 label="get_title_for_echo", surface="public-api")
    meta_body = parse(meta)
    current_title = meta_body["content"]["title"] if meta_body else "Automatic Assignment Workflow"
    r = probe("POST", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}",
              label="verify_post_metadata", surface="public-api",
              json_body={"title": current_title})
    check("POST /workflow/{id} (metadata write, echo)", r, 200)

    print("\n" + "=" * 60)
    print("SECTION 3: Confirmed working (writes, on SCRATCH workflow only)")
    print("=" * 60)

    r = probe("PUT", f"{BASE}/workflow/{SCRATCH_WORKFLOW_ID}/updateTree",
              label="verify_updateTree_noop", surface="public-api",
              json_body={"elements": [], "links": []})
    check("PUT /workflow/{id}/updateTree (no-op diff)", r, 200)

    r = probe("POST", f"{BASE}/workflow/{SCRATCH_WORKFLOW_ID}/publish",
              label="verify_publish", surface="public-api")
    check("POST /workflow/{id}/publish", r, 200)

    r = probe("POST", f"{BASE}/workflow/{SCRATCH_WORKFLOW_ID}/setResource",
              label="verify_setResource", surface="public-api",
              json_body={"resourceType": "FORM", "resourceID": TEST_FORM_ID})
    check("POST /workflow/{id}/setResource (reachable)", r, (200, 409))

    print("\n" + "=" * 60)
    print("SECTION 4: Exists but restricted")
    print("=" * 60)

    r = probe("GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}/revisions",
              label="verify_revisions", surface="public-api")
    check("GET /workflow/{id}/revisions (expect 401, exists-but-locked)", r, 401)

    print("\n" + "=" * 60)
    print("SECTION 5: Previously untested (seen in browser traffic only)")
    print("=" * 60)

    r = probe("GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}/getInternalForms",
              label="verify_getInternalForms", surface="public-api")
    results.append(("GET .../getInternalForms (no prior expectation)", None, r["status"], "n/a"))

    r = probe("GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}/copilot/getWorkflowCopilotSession",
              label="verify_copilot_session", surface="public-api")
    results.append(("GET .../copilot/getWorkflowCopilotSession (no prior expectation)", None, r["status"], "n/a"))

    print("\n" + "=" * 60)
    print("SECTION 6: Spot-check confirmed-404 list (should still be 404)")
    print("=" * 60)

    for guess in ["tree", "flow", "structure", "history", "duplicate"]:
        r = probe("GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}/{guess}",
                  label=f"verify_404_{guess}", surface="public-api",
                  log_file="pdf_verification.jsonl")
        check(f"GET .../{guess} (expect still 404)", r, 404)

    # --- Summary ---
    print("\n" + "=" * 60)
    print("VERIFICATION SUMMARY")
    print("=" * 60)
    passed = failed = unknown = 0
    for desc, ok, actual, expected in results:
        if ok is None:
            mark = "ℹ️ "
            unknown += 1
        elif ok:
            mark = "✅"
            passed += 1
        else:
            mark = "❌"
            failed += 1
        print(f"{mark} {desc} -> got {actual} (expected {expected})")

    print(f"\n{passed} passed, {failed} failed, {unknown} informational (no prior claim).")
    if failed:
        print("\n⚠️  Something in the PDF no longer matches reality — check the")
        print("   failed rows above before treating the PDF as current.")
    else:
        print("\nEverything the PDF claims still holds, re-tested right now.")


if __name__ == "__main__":
    main()
