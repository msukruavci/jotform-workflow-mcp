"""
Explores plausible workflow-related paths we have NEVER tried, to find
the edges of what this undocumented API surface actually supports.

Only GET is used — this is pure reconnaissance, nothing here can change
or delete anything. Expect MOST of these to 404; that's fine, a 404 is
still a real answer ("this doesn't exist"). Anything that returns
200/400/403 (anything other than 404) is worth a closer look — a 400
often means "this exists, you're just missing a parameter," which is
exactly how we found /elements and /links in the first place.

Run: python -m probes.explore_workflow_surface
"""
import os

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TEST_WORKFLOW_ID = os.environ.get("TEST_WORKFLOW_ID", "")

# Grouped by what kind of feature we're guessing at, so results are easy
# to scan. None of these are confirmed — they're informed guesses based
# on common patterns in workflow/automation products.
CANDIDATE_PATHS = {
    "history / versions": [
        "history", "versions", "revisions", "changelog", "activity",
    ],
    "runs / executions (has this workflow actually fired?)": [
        "runs", "executions", "logs", "instances", "history/runs",
    ],
    "duplication / templates": [
        "duplicate", "clone", "copy", "template",
    ],
    "scheduling": [
        "schedule", "trigger", "cron",
    ],
    "variables / settings beyond elements": [
        "variables", "settings", "config", "conditions",
    ],
    "sharing / permissions": [
        "share", "permissions", "collaborators", "teams",
    ],
    "export": [
        "export",
    ],
    "single element/link detail (vs the list endpoints we know)": [
        "elements/1", "links/1",
    ],
}


def main() -> None:
    if not TEST_WORKFLOW_ID:
        print("Set TEST_WORKFLOW_ID in .env first.")
        return

    interesting = []

    for category, paths in CANDIDATE_PATHS.items():
        print(f"\n--- {category} ---")
        for path in paths:
            record = probe(
                "GET", f"{BASE}/workflow/{TEST_WORKFLOW_ID}/{path}",
                label=f"explore_{path.replace('/', '_')}",
                surface="public-api",
                log_file="workflow_surface_exploration.jsonl",
            )
            if record["status"] != 404:
                interesting.append((path, record["status"]))

    print("\n" + "=" * 60)
    if interesting:
        print("Non-404 results — look at these first:")
        for path, status in interesting:
            print(f"  {path}: {status}")
    else:
        print("Everything 404'd. That's a real, useful result too — it")
        print("means the workflow surface is limited to what we already")
        print("mapped, at least under these guessed names.")


if __name__ == "__main__":
    main()
