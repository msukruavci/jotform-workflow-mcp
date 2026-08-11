"""
Dumps the FULL, untruncated detail of specific elements to files —
element 2 (workflow_conditional_branch, to finally see its complete
conditionTerms/outcomes) and element 5 (the one with a suspicious
type/content mismatch worth double-checking).

Run: python -m probes.dump_specific_elements
"""
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
API_KEY = os.environ.get("JOTFORM_API_KEY", "")
WORKFLOW_ID = os.environ.get("TEST_WORKFLOW_ID", "")
OUT_DIR = Path(__file__).parent / "findings"

ELEMENT_IDS_TO_DUMP = [2, 5]


def main() -> None:
    if not WORKFLOW_ID:
        print("Set TEST_WORKFLOW_ID in .env first.")
        return

    for el_id in ELEMENT_IDS_TO_DUMP:
        resp = requests.get(
            f"{BASE}/workflow/{WORKFLOW_ID}/elements/{el_id}",
            params={"apiKey": API_KEY}, timeout=15,
        )
        data = resp.json()
        out_path = OUT_DIR / f"element_{el_id}_full.json"
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2)
        content = data.get("content", {})
        print(f"element {el_id}: type={content.get('type')!r}, "
              f"name={content.get('name')!r} -> written to {out_path}")


if __name__ == "__main__":
    main()
