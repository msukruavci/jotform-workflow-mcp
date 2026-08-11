"""
One-off: dump the FULL, untruncated response of GET /workflow/{id}/elements
to a pretty-printed JSON file. probes/client.py truncates response_snippet
in the log for readability — this script bypasses that so you can actually
see the whole node graph, including whether links/connections between
nodes live in this same response or need a separate call.

Run BEFORE attempting any write test against elements — you want the full
shape in front of you, not a guess based on the first truncated element.

Run: python -m probes.inspect_workflow_elements
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
OUT_PATH = Path(__file__).parent / "findings" / "workflow_elements_full.json"


def main() -> None:
    if not TEST_WORKFLOW_ID:
        print("Set TEST_WORKFLOW_ID in .env first.")
        return

    resp = requests.get(
        f"{BASE}/workflow/{TEST_WORKFLOW_ID}/elements",
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
    element_count = len(content) if isinstance(content, list) else "n/a (not a list)"
    print(f"Full response written to {OUT_PATH}")
    print(f"Element count: {element_count}")
    if isinstance(content, list):
        types_seen = sorted({el.get("type") for el in content if isinstance(el, dict)})
        print(f"Distinct element 'type' values seen: {types_seen}")
        print(
            "Look through the written file for any field that references "
            "OTHER elements by id/uuid (e.g. 'next', 'target', 'parent', "
            "'links') — that's how you'll tell whether connections between "
            "nodes live here too, or need a separate endpoint."
        )


if __name__ == "__main__":
    main()
