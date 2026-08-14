"""
What does a real, working workflow_approval element's `outcomes` actually
look like?

schema_registry doesn't declare a `default` for workflow_approval.outcomes
(confirmed — unlike workflow_binary_decision's TRUE/FALSE), so
get_field_defaults has nothing to inject. Rather than hand-write a
plausible-looking APPROVE/DENY default (exactly the kind of unverified
fabrication this project avoids everywhere else), this pulls the real
shape from an existing, working approval step already in the account.

Also checks workflow_conditional_branch and any other step type worth
knowing, and prints which types genuinely have no schema-declared default
so BRANCHING_TYPES + a hand-maintained default table can be built from
real data, not guesses.

Usage:
    python -m probes.inspect_approval_outcomes <workflow_id> <step_id>

With no args, tries to find a workflow_approval step automatically by
scanning list_workflows/get_workflow.
"""
from __future__ import annotations

import json
import sys

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402


def find_approval_step(client: JotformClient) -> tuple[str, str] | None:
    for w in client.list_workflows():
        wid = w.get("id")
        try:
            combined = client.get_workflow_combined(wid)
        except JotformAPIError:
            continue
        for el in combined.get("elements") or []:
            if isinstance(el, dict) and el.get("type") == "workflow_approval":
                return wid, str(el.get("element_id"))
    return None


def main() -> int:
    client = JotformClient()

    if len(sys.argv) >= 3:
        workflow_id, step_id = sys.argv[1], sys.argv[2]
    else:
        found = find_approval_step(client)
        if not found:
            print("No workflow_approval step found on this account.")
            print("Pass one explicitly: python -m probes.inspect_approval_outcomes <workflow_id> <step_id>")
            return 1
        workflow_id, step_id = found
        print(f"(no args given — found one at workflow {workflow_id}, step {step_id})\n")

    try:
        element = client.get_element(workflow_id, step_id)
    except JotformAPIError as e:
        print(f"Could not read element: {e}")
        return 1

    print(f"type: {element.get('type')}")
    print(f"subType: {element.get('subType')!r}")
    print()
    outcomes = element.get("outcomes")
    print("outcomes (real, ground-truth shape):")
    print(json.dumps(outcomes, indent=2, ensure_ascii=False))

    if not outcomes:
        print("\nThis particular step has no outcomes set either — try a")
        print("different workflow, or one where Approve/Deny visibly work")
        print("in the Jotform builder UI.")
        return 1

    print("\n" + "=" * 70)
    print("Copy this shape (with outcomeID/linkID reset appropriately) into")
    print("a hand-maintained default for workflow_approval in schema_registry.py")
    print("— the same pattern already used for UI_NAMES/DESCRIPTIONS, since")
    print("Jotform's own schema doesn't declare one here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())