"""
Small helper — find a workflow's id (by title) and, inside it, the
element_id of its workflow_conditional_branch step. Feeds directly into
inspect_conditional_branch_outcomes.py.

Run with no arguments to list all workflows:
    python -m probes.find_conditional_branch_ids

Run with a workflow_id to list that workflow's elements:
    python -m probes.find_conditional_branch_ids <workflow_id>
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402


def main() -> int:
    client = JotformClient()

    if len(sys.argv) < 2:
        print("Your workflows:\n")
        try:
            workflows = client.list_workflows()
        except JotformAPIError as e:
            print(f"[FAIL] {e}")
            return 1
        for w in workflows:
            print(f"  {w.get('id')}  -  {w.get('title')}  ({w.get('status')})")
        print(f"\nRe-run with one of these ids to see its elements:")
        print(f"  python -m probes.find_conditional_branch_ids <workflow_id>")
        return 0

    workflow_id = sys.argv[1]
    try:
        combined = client.get_workflow_combined(workflow_id)
    except JotformAPIError as e:
        print(f"[FAIL] {e}")
        return 1

    elements = [e for e in (combined.get("elements") or []) if isinstance(e, dict)]
    print(f"Elements in workflow {workflow_id}:\n")
    branch_ids = []
    for e in elements:
        eid, etype, name = e.get("element_id"), e.get("type"), e.get("name")
        marker = ""
        if etype == "workflow_conditional_branch":
            marker = "  <-- this one"
            branch_ids.append(eid)
        print(f"  {eid}\t{etype}\t{name or ''}{marker}")

    if branch_ids:
        print(f"\nFound workflow_conditional_branch step(s): {branch_ids}")
        print(f"Next: python -m probes.inspect_conditional_branch_outcomes "
              f"{workflow_id} {branch_ids[0]}")
    else:
        print(f"\nNo workflow_conditional_branch step in this workflow — "
              f"add one in the builder first.")

    return 0


if __name__ == "__main__":
    sys.exit(main())