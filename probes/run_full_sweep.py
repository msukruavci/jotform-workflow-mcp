"""
Sweeps the canonical endpoint list produced by discover_from_official_sdk.py.

Safety rule, deliberate: only GET endpoints are ever called automatically.
POST/PUT/DELETE endpoints are listed and logged as "skipped (mutating)" —
never fired blindly, because several of them are destructive against your
real account (delete_form, delete_submission, delete_folder, even
logout_user). If you want to test one of those, do it deliberately via
Bruno or a one-off probe() call where you've read exactly what it does
first.

Run: python -m probes.discover_from_official_sdk   (once, or whenever you
     want to refresh docs/public-api-surface.json)
     python -m probes.run_full_sweep
"""
import json
import os
from pathlib import Path

from probes.client import probe

SURFACE_PATH = Path(__file__).parent.parent / "docs" / "public-api-surface.json"
BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")

# Only IDs we actually have test values for get swept automatically.
# Add more (folderID, reportID, webhookID, qid, propertyKey, plan_name)
# to .env + here once you have real test values for them — don't invent
# fake ones just to force a call.
PLACEHOLDER_VALUES = {
    "formID": os.environ.get("TEST_FORM_ID", ""),
    "sid": os.environ.get("TEST_SUBMISSION_ID", ""),
}


def fill_template(template: str) -> str | None:
    """Returns None if a required placeholder has no known test value."""
    result = template
    for key, value in PLACEHOLDER_VALUES.items():
        if f"{{{key}}}" in result:
            if not value:
                return None
            result = result.replace(f"{{{key}}}", value)
    if "{" in result:  # some other placeholder we don't have a value for
        return None
    return result


def main() -> None:
    if not SURFACE_PATH.exists():
        print("Run `python -m probes.discover_from_official_sdk` first.")
        return

    with open(SURFACE_PATH) as f:
        surface = json.load(f)

    called, skipped_mutating, skipped_no_id = 0, 0, 0

    for ep in surface["endpoints"]:
        if ep["mutating"]:
            skipped_mutating += 1
            print(f"[skip: mutating] {ep['http_method']} {ep['path_template']}")
            continue

        path = fill_template(ep["path_template"])
        if path is None:
            skipped_no_id += 1
            print(f"[skip: no test id] {ep['http_method']} {ep['path_template']}")
            continue

        probe(
            "GET",
            f"{BASE}{path}",
            label=f"sdk_sweep_{ep['client_method']}",
            surface="public-api",
            log_file="sdk_sweep_findings.jsonl",
        )
        called += 1

    print(f"\nDone. Called {called}, skipped {skipped_mutating} mutating, "
          f"skipped {skipped_no_id} missing-test-id.")
    print("See probes/findings/sdk_sweep_findings.jsonl for full results.")


if __name__ == "__main__":
    main()
