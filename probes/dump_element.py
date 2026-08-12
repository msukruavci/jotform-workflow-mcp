"""
General-purpose: dump one element's full raw data. Not specific to any
step type — a reusable version of what inspect_approval_outcomes.py and
inspect_conditional_branch_outcomes.py each did by hand.

Use this whenever a step type's real shape (subType, outcomes, field
names) needs confirming from a real, working element instead of being
guessed — the project's standing rule for anything that ends up in
schema_registry.py's hand-verified tables.

Run:
    python -m probes.dump_element <workflow_id> <step_id>
"""
from __future__ import annotations

import json
import sys

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python -m probes.dump_element <workflow_id> <step_id>")
        return 1

    workflow_id, step_id = sys.argv[1], sys.argv[2]
    client = JotformClient()

    try:
        element = client.get_element(workflow_id, step_id)
    except JotformAPIError as e:
        print(f"[FAIL] {e}")
        return 1

    print(json.dumps(element, indent=2, ensure_ascii=False))

    print(f"\n--- quick summary ---")
    print(f"type:    {element.get('type')!r}")
    print(f"subType: {element.get('subType')!r}")
    print(f"name:    {element.get('name')!r}")
    outcomes = element.get("outcomes")
    if isinstance(outcomes, list):
        print(f"outcomes: {len(outcomes)} entries")
        for o in outcomes:
            if isinstance(o, dict):
                print(f"  - {json.dumps(o, ensure_ascii=False)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())