"""
Find out how Jotform labels a link's exit branch.

The builder UI draws TRUE and FALSE on the two edges leaving an if/else
step, so that label exists in the data. We do not yet know which field
carries it. This dumps every key on every link, then points at the steps
where a label must exist, so the answer is read off real data rather than
guessed.

Run:
    python -m probes.inspect_links [workflow_id]

With no argument it uses TEST_WORKFLOW_ID from .env, or the first workflow
on the account.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402


def main() -> int:
    client = JotformClient()

    workflow_id = (
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TEST_WORKFLOW_ID", "")
    )
    if not workflow_id:
        workflows = client.list_workflows()
        if not workflows:
            print("No workflows on this account.")
            return 1
        workflow_id = workflows[0]["id"]
        print(f"(no id given — using {workflow_id})")

    # Both sources, because /combined may trim what the dedicated endpoint
    # returns. If the counts differ, get_workflow's single-call optimisation
    # is unsafe and that is worth knowing on its own.
    try:
        combined = client.get_workflow_combined(workflow_id)
        combined_links = [l for l in (combined.get("links") or []) if isinstance(l, dict)]
    except JotformAPIError as e:
        print(f"/combined failed: {e}")
        combined_links = []

    try:
        direct_links = [l for l in client.get_links(workflow_id) if isinstance(l, dict)]
    except JotformAPIError as e:
        print(f"/links failed: {e}")
        direct_links = []

    print("=" * 70)
    print(f"/combined links: {len(combined_links)}   /links: {len(direct_links)}")
    if len(combined_links) != len(direct_links):
        print("!! COUNTS DIFFER — /combined is dropping links. get_workflow")
        print("   cannot rely on it; fetch links separately.")
    print("=" * 70)

    links = direct_links or combined_links
    if not links:
        print("No links to inspect.")
        return 1

    key_counts = Counter(k for link in links for k in link)
    print("\nKeys across all links (key: how many links have it):")
    for key, count in key_counts.most_common():
        sample = next((l[key] for l in links if l.get(key) not in (None, "")), None)
        print(f"  {key:<24} {count:>3}/{len(links)}   e.g. {sample!r}"[:110])

    # Where a branch label MUST exist: any step with more than one exit.
    exits: dict[str, list[dict]] = {}
    for link in links:
        src = link.get("fromElement") or link.get("from_element")
        if src is not None:
            exits.setdefault(str(src), []).append(link)

    branching = {k: v for k, v in exits.items() if len(v) > 1}
    print(f"\nSteps with more than one exit: {sorted(branching, key=str) or 'none'}")
    print("The field that differs between these links is the branch label.\n")

    for step_id, step_links in branching.items():
        print(f"--- step {step_id} ---")
        differing = [
            k for k in key_counts
            if len({json.dumps(l.get(k), sort_keys=True, default=str) for l in step_links}) > 1
        ]
        for link in step_links:
            trimmed = {k: link.get(k) for k in differing}
            print(f"  -> {link.get('toElement')}   {trimmed}")
        print(f"  differing fields: {differing}")
        print()

    with open("probes/links_raw.json", "w") as f:
        json.dump({"combined": combined_links, "direct": direct_links}, f, indent=2)
    print("Full dump: probes/links_raw.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
