"""
Ground-truth inspection for workflow_conditional_branch outcomes —
same method used for workflow_approval (probes/inspect_approval_outcomes.py,
2026-08-11), applied to conditional branch.

Why this can't be guessed: unlike approval's Approve/Deny (identical on
every instance, hence _OUTCOMES_OVERRIDE), conditional branch names are
user-defined. There is no universal default to inject. This probe exists
to answer three questions before writing any fix:

  1. Does the schema declare a `default` for `outcomes` at all
     (get_field_defaults would already inject *something* — is it useful
     or just an empty/wrong shape)?
  2. What does a REAL, working conditional-branch outcome object actually
     look like — same field as if/else's conditionValue, or something
     else entirely (mirroring the approval surprise: text/type, not
     conditionValue)?
  3. Is `outcomes` even exposed as a configurable field by
     get_simplified_schema — or does validate_config silently strip it
     because it's not in the field list, which would mean the real fix is
     in schema_registry/tree_builder, not a data problem at all?

Needs a REAL conditional-branch step, built by hand in the Jotform
builder with at least two named custom branches, each wired to something.
Pass its workflow_id and step_id as arguments.

Run:
    python -m probes.inspect_conditional_branch_outcomes <workflow_id> <step_id>
"""
from __future__ import annotations

import json
import sys

from dotenv import load_dotenv

load_dotenv()

from mcp_server import schema_registry  # noqa: E402
from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402

STEP_TYPE = "workflow_conditional_branch"


def main() -> int:
    if len(sys.argv) < 3:
        print(f"Usage: python -m probes.inspect_conditional_branch_outcomes "
              f"<workflow_id> <step_id>")
        print("\nBoth must belong to a REAL conditional-branch step you built "
              "by hand in the Jotform builder, with at least two named custom "
              "branches, each wired to something. Do not point this at a "
              "step created only through this project's own tools — that "
              "would just confirm our own guess, not ground truth.")
        return 1

    workflow_id, step_id = sys.argv[1], sys.argv[2]
    client = JotformClient()

    print(f"=== 1. Raw element data (workflow_id={workflow_id}, step_id={step_id}) ===\n")
    try:
        element = client.get_element(workflow_id, step_id)
    except JotformAPIError as e:
        print(f"[FAIL] could not fetch element: {e}")
        return 1

    print(json.dumps(element, indent=2, ensure_ascii=False))

    actual_type = element.get("type")
    if actual_type != STEP_TYPE:
        print(f"\n[WARN] element's type is {actual_type!r}, expected {STEP_TYPE!r}. "
              f"Wrong step_id? Continuing anyway — the schema-comparison "
              f"section below will still be useful.")

    outcomes = element.get("outcomes")
    print(f"\n=== 2. outcomes field — {type(outcomes).__name__}, "
          f"{len(outcomes) if isinstance(outcomes, list) else 'n/a'} entries ===\n")
    if isinstance(outcomes, list):
        for i, o in enumerate(outcomes):
            print(f"  [{i}] {json.dumps(o, ensure_ascii=False)}")
            if isinstance(o, dict):
                # Same three candidate fields the approval fix found —
                # check which one(s) actually carry the branch's name here.
                for field in ("conditionValue", "text", "type", "name", "label"):
                    if field in o:
                        print(f"       -> has '{field}' = {o[field]!r}")
    else:
        print("  (not a list — note this exactly, it changes the fix shape)")

    print(f"\n=== 3. What get_field_defaults({STEP_TYPE!r}) would inject today ===\n")
    defaults = schema_registry.get_field_defaults(STEP_TYPE)
    print(json.dumps(defaults, indent=2, ensure_ascii=False) if defaults else "  (nothing)")

    print(f"\n=== 4. What get_simplified_schema({STEP_TYPE!r}) exposes as configurable ===\n")
    simplified = schema_registry.get_simplified_schema(STEP_TYPE)
    if simplified is None:
        print(f"  [!!] No schema on record for {STEP_TYPE} at all.")
    else:
        field_names = [f["name"] for f in simplified["fields"]]
        print(f"  configurable fields: {field_names}")
        outcomes_field = next((f for f in simplified["fields"] if f["name"] == "outcomes"), None)
        if outcomes_field:
            print(f"\n  'outcomes' field detail:")
            print(f"  {json.dumps(outcomes_field, indent=2, ensure_ascii=False)}")
        else:
            print(f"\n  [!!] 'outcomes' is NOT in the configurable field list — "
                  f"validate_config would silently strip it from any config "
                  f"passed to add_step/update_step. If that's the case, the "
                  f"fix is here (schema_registry/tree_builder), not a data "
                  f"problem — no default, override, or config value could "
                  f"ever reach the API through the current tools.")

    print("\n" + "=" * 70)
    print("Compare sections 2 and 4. If 'outcomes' isn't configurable (4) but")
    print("carries real branch data (2), that's the actual gap: reading works,")
    print("writing doesn't. If it IS configurable, the fix is likely just")
    print("making sure add_step/update_step callers pass named branches")
    print("explicitly in config — not a schema-level default, since names")
    print("are user-chosen and can't be guessed for every workflow.")

    return 0


if __name__ == "__main__":
    sys.exit(main())