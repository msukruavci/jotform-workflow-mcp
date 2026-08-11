"""
Tests whether the elements write channel is open at all, WITHOUT inventing
new node data we don't understand the schema for yet.

Method: read the current elements (same ones inspect_workflow_elements.py
already saved), then send that exact same list back via PUT and via POST.
If either is accepted (not a CSRF-style block, not a schema-validation
error), we've learned the channel is open — safely, since we changed
nothing. If it's rejected, the error message itself usually tells us what
a valid write would need to look like.

Run inspect_workflow_elements.py first so workflow_elements_full.json
exists and is current.

Run: python -m probes.test_elements_write
"""
import json
import os
from pathlib import Path

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TEST_WORKFLOW_ID = os.environ.get("TEST_WORKFLOW_ID", "")
ELEMENTS_FILE = Path(__file__).parent / "findings" / "workflow_elements_full.json"


def main() -> None:
    if not TEST_WORKFLOW_ID:
        print("Set TEST_WORKFLOW_ID in .env first.")
        return
    if not ELEMENTS_FILE.exists():
        print("Run `python -m probes.inspect_workflow_elements` first.")
        return

    with open(ELEMENTS_FILE) as f:
        data = json.load(f)
    current_elements = data.get("content", data)

    if not isinstance(current_elements, list):
        print("Unexpected shape in workflow_elements_full.json — inspect it manually.")
        return

    print(f"Echoing back {len(current_elements)} unchanged elements via PUT, then POST...")

    probe(
        "PUT",
        f"{BASE}/workflow/{TEST_WORKFLOW_ID}/elements",
        label="elements_write_echo_put",
        surface="public-api",
        json_body={"elements": current_elements},
    )

    probe(
        "POST",
        f"{BASE}/workflow/{TEST_WORKFLOW_ID}/elements",
        label="elements_write_echo_post",
        surface="public-api",
        json_body={"elements": current_elements},
    )


if __name__ == "__main__":
    main()
