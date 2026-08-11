"""
Probes against the documented public API (api.jotform.com).

Run: python -m probes.run_public_api
"""
import os

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TEST_FORM_ID = os.environ.get("TEST_FORM_ID", "")
TEST_WORKFLOW_ID = os.environ.get("TEST_WORKFLOW_ID", "")


def main() -> None:
    # --- CONFIRMED WORKING (2026-08-05) ---
    probe("GET", f"{BASE}/user/forms", label="list_forms", surface="public-api")

    if TEST_FORM_ID:
        probe(
            "GET",
            f"{BASE}/form/{TEST_FORM_ID}/submissions",
            label="get_submissions",
            surface="public-api",
        )
        # Open question: does workflowStatus actually appear, and does it
        # carry enough detail to be useful? Read the logged response.
        probe(
            "GET",
            f"{BASE}/user/forms",
            label="list_forms_with_workflow_flag",
            surface="public-api",
            params={
                "filter": '{"status:eq":"ENABLED"}',
                "addWorkflow": 1,
            },
        )

    # --- CONFIRMED WORKING (2026-08-05), contradicts the 2023 support
    # thread that claimed this was locked. Returns workflow METADATA only
    # (title, status, publishStatus, createdWithAI, ...) — no elements/links.
    if TEST_WORKFLOW_ID:
        probe(
            "GET",
            f"{BASE}/workflow/{TEST_WORKFLOW_ID}",
            label="get_workflow_undocumented",
            surface="public-api",
        )

        # --- NEW, UNVERIFIED (added after the GET above surprised us) ---
        # Does this same public-api path accept writes, unlike its
        # www.jotform.com/API counterpart which is CSRF-blocked? Only
        # sends a no-op-ish field so a 200 doesn't silently corrupt state
        # before we've decided this is safe to build on.
        probe(
            "POST",
            f"{BASE}/workflow/{TEST_WORKFLOW_ID}",
            label="update_workflow_metadata_via_public_api",
            surface="public-api",
            json_body={"title": "Automatic Assignment Workflow"},  # same value, non-destructive
        )

        # Guesses at where the full node/link tree might live under the
        # public API. All unverified — expect several 404s, that's fine,
        # each one is still a finding worth logging.
        for guess in ("tree", "elements", "flow", "structure", "links", "connections"):
            probe(
                "GET",
                f"{BASE}/workflow/{TEST_WORKFLOW_ID}/{guess}",
                label=f"get_workflow_{guess}_guess",
                surface="public-api",
            )

        # --- BIG NEW QUESTION (added after /elements surprised us by
        # returning real node data) ---
        # We can read elements. Can we write them? GET the current
        # elements first so we can PUT/POST back something extremely
        # close to identical (non-destructive-ish) rather than guessing
        # at a payload shape and risking corrupting the real test
        # workflow. Still: this is a write test against your own account
        # data, go in with eyes open.
        current = probe(
            "GET",
            f"{BASE}/workflow/{TEST_WORKFLOW_ID}/elements",
            label="get_workflow_elements_for_writeback_test",
            surface="public-api",
        )
        # NOTE: response_snippet is truncated to 1500 chars in the log.
        # Run probes/inspect_workflow_elements.py for the untruncated body
        # before attempting any write — you want to see the full shape
        # first, not just the first element.

        # Does the public API expose a parallel, non-CSRF-blocked publish?
        probe(
            "POST",
            f"{BASE}/workflow/{TEST_WORKFLOW_ID}/publish",
            label="publish_workflow_via_public_api",
            surface="public-api",
        )

        # Same question for setResource — send back the same form id it
        # already has, to keep this non-destructive if it does work.
        probe(
            "POST",
            f"{BASE}/workflow/{TEST_WORKFLOW_ID}/setResource",
            label="set_resource_via_public_api",
            surface="public-api",
            json_body={"resourceType": "FORM", "resourceID": TEST_FORM_ID},
        )


if __name__ == "__main__":
    main()
