"""
Where does `outcomes` live?

Branch labels (TRUE/FALSE, and custom names on a conditional branch) are not
on the links — they're on the deciding element, as
`outcomes[] = {outcomeID, conditionValue, linkID}`. `linkID` is what ties an
outcome to a connection.

The question this answers: does /combined already carry `outcomes`, or does
get_workflow need an extra per-element call to read them? That decides
whether branch labels are free or cost one request per decision step.

Run:
    python -m probes.inspect_outcomes [workflow_id]
"""
from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402

# Types that branch, so are expected to carry outcomes.
DECIDING_TYPES = {"workflow_binary_decision", "workflow_conditional_branch"}


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

    combined = client.get_workflow_combined(workflow_id)
    elements = [e for e in (combined.get("elements") or []) if isinstance(e, dict)]
    links = [l for l in (combined.get("links") or []) if isinstance(l, dict)]

    deciders = [e for e in elements if e.get("type") in DECIDING_TYPES]
    print(f"{len(elements)} elements, {len(deciders)} of them branching\n")
    if not deciders:
        print("No branching steps in this workflow — try one with an if/else.")
        return 1

    for el in deciders:
        eid = el.get("element_id")
        print("=" * 66)
        print(f"step {eid}  {el.get('type')}")
        print("=" * 66)

        in_combined = el.get("outcomes")
        print(f"/combined has outcomes: {'YES' if in_combined else 'NO'}")
        if in_combined:
            print(f"  {json.dumps(in_combined, indent=2)[:600]}")

        try:
            full = client.get_element(workflow_id, eid)
        except JotformAPIError as e:
            print(f"  per-element fetch failed: {e}")
            continue

        in_full = full.get("outcomes")
        print(f"/elements/{eid} has outcomes: {'YES' if in_full else 'NO'}")
        if in_full and not in_combined:
            print(f"  {json.dumps(in_full, indent=2)[:600]}")
            print("\n  -> /combined is NOT enough. get_workflow needs one extra")
            print("     call per branching step to label the branches.")

        outcomes = in_full or in_combined or []
        if outcomes:
            print("\n  outcome -> link -> target:")
            by_link = {str(l.get("link_id")): l for l in links}
            for o in outcomes:
                link_id = o.get("linkID")
                target = by_link.get(str(link_id), {}).get("toElement", "—")
                print(f"    {o.get('conditionValue', '?'):<10} linkID={link_id!s:<6} -> step {target}")
            unmapped = [o for o in outcomes if o.get("linkID") in (None, 0)]
            if unmapped:
                print(f"    !! {len(unmapped)} outcome(s) with no linkID — that branch")
                print("       is defined but not connected to anything.")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())