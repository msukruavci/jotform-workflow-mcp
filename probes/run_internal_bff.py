"""
Probes against the internal frontend BFF (www.jotform.com/API).

These are EXPECTED to fail with a same-origin/CSRF rejection when called
outside a real browser session — that's the finding, not a bug. This
script exists so the rejection is re-confirmed and re-dated every time it
runs, rather than resting on one manual curl from one afternoon. If Jotform
ever changes this behavior, this script is what will catch it.

Run: python -m probes.run_internal_bff
"""
import os

from probes.client import probe

BASE = os.environ.get("JOTFORM_INTERNAL_BFF_BASE", "https://www.jotform.com/API")
TEST_WORKFLOW_ID = os.environ.get("TEST_WORKFLOW_ID", "")
TEST_FORM_ID = os.environ.get("TEST_FORM_ID", "")


def main() -> None:
    if not TEST_WORKFLOW_ID:
        print("Set TEST_WORKFLOW_ID in .env to run these probes.")
        return

    # Confirmed 2026-08-05: apiKey alone is not enough, no session ->
    # "Cross-Site Requests not allowed!"
    probe(
        "POST",
        f"{BASE}/workflow/{TEST_WORKFLOW_ID}/setResource",
        label="set_resource",
        surface="internal-bff",
        json_body={"resourceType": "FORM", "resourceID": TEST_FORM_ID},
    )

    probe(
        "PUT",
        f"{BASE}/workflow/{TEST_WORKFLOW_ID}/updateTree",
        label="update_tree",
        surface="internal-bff",
        json_body={"links": [], "elements": []},
    )

    probe(
        "POST",
        f"{BASE}/workflow/{TEST_WORKFLOW_ID}/publish",
        label="publish_workflow",
        surface="internal-bff",
    )


if __name__ == "__main__":
    main()
