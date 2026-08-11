"""
Reads every element of the ORIGINAL workflow one by one via the
"read one" endpoint (GET .../elements/{id}), to see two things:

1. Does GET .../elements/{id} return the FULL rich config (like
   updateTree gives us), or just the lightweight summary the "list all"
   endpoint (GET .../elements) returns? We've never actually checked this.
2. What does the old "workflow_conditional_branch" element's full config
   look like — is it the same shape as the newly-discovered
   workflow_binary_decision, just with a different type name, or
   genuinely different fields?

Note: fetches the element listing directly with `requests` (not through
probe()'s log, which truncates long responses at 4000 chars and would
break json parsing on a list this size) — the listing is still logged
via probe() afterward for the record, just not used for parsing.

Run: python -m probes.compare_conditional_branch_types
"""
import json
import os

import requests

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
API_KEY = os.environ.get("JOTFORM_API_KEY", "")
ORIGINAL_WORKFLOW_ID = os.environ.get("TEST_WORKFLOW_ID", "")


def main() -> None:
    if not ORIGINAL_WORKFLOW_ID:
        print("Set TEST_WORKFLOW_ID in .env first.")
        return

    # Log it for the record (truncated log is fine here, we don't parse this copy).
    probe(
        "GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}/elements",
        label="list_elements_for_comparison", surface="public-api",
    )

    # Fetch the FULL, untruncated response ourselves for actual parsing.
    resp = requests.get(
        f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}/elements",
        params={"apiKey": API_KEY}, timeout=15,
    )
    data = resp.json()
    elements = data.get("content", [])

    print(f"\nFound {len(elements)} elements in the listing.\n")

    for el in elements:
        el_id = el.get("element_id") or el.get("id")
        el_type = el.get("type")
        print(f"\n--- Reading element_id={el_id} (type in listing: {el_type}) individually ---")
        detail = probe(
            "GET", f"{BASE}/workflow/{ORIGINAL_WORKFLOW_ID}/elements/{el_id}",
            label=f"read_element_{el_id}_detail", surface="public-api",
        )
        if el_type == "workflow_conditional_branch":
            print(">>> THIS IS THE conditional_branch ELEMENT — full detail above <<<")


if __name__ == "__main__":
    main()

