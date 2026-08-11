"""
One-off: dump the FULL, untruncated response of GET /workflow/{id}/links
to a pretty-printed JSON file — the sibling of inspect_workflow_elements.py.

elements gave us the nodes; links (confirmed working 2026-08-06) gives us
the connections between them (fromElement/toElement/fromPortName/
toPortName). Run this to see the whole connection graph before attempting
any write test against it.

Run: python -m probes.inspect_workflow_links
"""
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
API_KEY = os.environ.get("JOTFORM_API_KEY", "")
TEST_WORKFLOW_ID = os.environ.get("TEST_WORKFLOW_ID", "")
OUT_PATH = Path(__file__).parent / "findings" / "workflow_links_full.json"


def main() -> None:
    if not TEST_WORKFLOW_ID:
        print("Set TEST_WORKFLOW_ID in .env first.")
        return

    resp = requests.get(
        f"{BASE}/workflow/{TEST_WORKFLOW_ID}/links",
        params={"apiKey": API_KEY},
        timeout=15,
    )
    print(f"Status: {resp.status_code}")

    try:
        data = resp.json()
    except ValueError:
        print("Response wasn't JSON:")
        print(resp.text[:2000])
        return

    with open(OUT_PATH, "w") as f:
        json.dump(data, f, indent=2)

    content = data.get("content", data)
    link_count = len(content) if isinstance(content, list) else "n/a (not a list)"
    print(f"Full response written to {OUT_PATH}")
    print(f"Link count: {link_count}")
    if isinstance(content, list):
        print(
            "Cross-check each link's fromElement/toElement against the "
            "uuids you saw in workflow_elements_full.json — that's what "
            "ties the two files into one full graph."
        )


if __name__ == "__main__":
    main()
