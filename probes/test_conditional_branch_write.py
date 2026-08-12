"""
End-to-end: can this project build a NEW workflow_conditional_branch step
from scratch, with real named custom branches — not just correctly read
one built by hand in the builder (that part is what
inspect_conditional_branch_outcomes.py already confirmed)?

Exercises the full path a model would take, mirroring what
tools/building.py's add_step/connect_steps do internally (calling
JotformClient + tree_builder directly, same as test_set_trigger_form.py
and test.py do, rather than going through mcp.call_tool):

  1. create_workflow, bound to a real trigger form (TEST_FORM_ID) — so
     step 2 has a real field id to use, never invented.
  2. get_form_fields — pick one real field id for the branch conditions.
  3. add two workflow_end_point steps as branch targets.
  4. add the conditional_branch itself, with an explicit `outcomes`
     config built from schema_registry's item_fields hint (ground-truth
     sourced, see schema_registry._OUTCOME_ITEM_FIELDS_OVERRIDE) — two
     named custom branches, no catch-all, to keep the assertion simple.
  5. read back the created element — did the branches persist with the
     names/conditions sent, or did something get dropped or renamed?
  6. connect_steps to each branch BY NAME ("Test Branch A" /
     "Test Branch B") — this is what the outcome_label fix
     (2026-08-12) is actually for; a regression here would silently wire
     the wrong branch or fail to resolve either one.
  7. read back again — do the right link_ids land on the right branches,
     distinguishably (not both landing on whichever branch sorts first)?

Deletes the workflow on full success. Leaves it and prints the builder
URL if any check fails, so it can be inspected directly.

Needs TEST_FORM_ID in .env — same as test_set_trigger_form.py.

Run:
    python -m probes.test_conditional_branch_write
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from mcp_server import tree_builder as tb  # noqa: E402
from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402

BRANCH_A = "Test Branch A"
BRANCH_B = "Test Branch B"


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def main() -> int:
    client = JotformClient()
    form_id = os.environ.get("TEST_FORM_ID", "")
    if not form_id:
        print("Set TEST_FORM_ID in .env first — pick a real form id from list_forms.")
        return 1

    ok = True
    title = f"ZZ-condbranch-DELETE-ME-{datetime.now():%Y%m%d-%H%M%S}"

    # 1. Create + bind trigger form -----------------------------------
    print(f"1. Creating workflow {title!r}, binding form {form_id}...")
    created = client.create_workflow(title)
    workflow_id = created.get("id") or created.get("workflowID")
    if not workflow_id:
        fail(f"no workflow id in response: {created!r}")
        return 1
    client.set_trigger_form(workflow_id, form_id)
    start_check = client.get_element(workflow_id, 1)
    if str(start_check.get("resourceID")) != str(form_id):
        fail("trigger form did not bind — aborting before building on top of it")
        print(f"   inspect: https://www.jotform.com/workflow/{workflow_id}/build")
        return 1
    print(f"   -> workflow_id={workflow_id}, trigger form bound and verified")

    # 2. Real field id --------------------------------------------------
    fields = client.get_form_questions(form_id)
    field_id = next(iter(fields), None) if fields else None
    if not field_id:
        fail(f"form {form_id} has no fields to build a condition on")
        return 1
    field_label = fields[field_id].get("text", "")
    print(f"2. Using real field id {field_id!r} ({field_label!r}) for conditions")

    # 3. Two branch targets ----------------------------------------------
    elements = client.get_elements(workflow_id)
    end_a_id = tb.next_id([e.get("element_id") for e in elements])
    pos_a = tb.compute_position(elements, None)
    client.update_tree(workflow_id, elements=[
        tb.build_element_create("workflow_end_point", end_a_id, {}, pos_a)
    ])
    elements = client.get_elements(workflow_id)
    end_b_id = tb.next_id([e.get("element_id") for e in elements])
    pos_b = tb.compute_position(elements, None)
    client.update_tree(workflow_id, elements=[
        tb.build_element_create("workflow_end_point", end_b_id, {}, pos_b)
    ])
    print(f"3. Branch targets created: end_a={end_a_id}, end_b={end_b_id}")

    # 4. The conditional_branch step itself ------------------------------
    outcomes_config = [
        {
            "id": 1, "outcomeID": 1, "type": "CONDITION",
            "conditionValue": "CUSTOM", "branchName": BRANCH_A,
            "conditionTermsMatchType": "All",
            "conditionTerms": [{
                "field": str(field_id), "id": "term_test_a",
                "operator": "isFilled", "value": "", "isError": False,
                "color": "#007862",
            }],
        },
        {
            "id": 2, "outcomeID": 2, "type": "CONDITION",
            "conditionValue": "CUSTOM", "branchName": BRANCH_B,
            "conditionTermsMatchType": "All",
            "conditionTerms": [{
                "field": str(field_id), "id": "term_test_b",
                "operator": "isEmpty", "value": "", "isError": False,
                "color": "#007862",
            }],
        },
    ]
    elements = client.get_elements(workflow_id)
    branch_id = tb.next_id([e.get("element_id") for e in elements])
    position = tb.compute_position(elements, "1")  # anchor to start point
    create_entry = tb.build_element_create(
        "workflow_conditional_branch", branch_id,
        {"outcomes": outcomes_config}, position,
    )
    try:
        client.update_tree(workflow_id, elements=[create_entry])
    except JotformAPIError as e:
        fail(f"add_step-equivalent write rejected: {e}")
        print(f"   inspect: https://www.jotform.com/workflow/{workflow_id}/build")
        return 1
    print(f"4. Conditional branch created: step_id={branch_id}, "
          f"2 custom outcomes sent")

    # 5. Read back — did the branches actually persist? ------------------
    print("\n5. Reading back the created element...")
    persisted = client.get_element(workflow_id, branch_id)
    persisted_outcomes = persisted.get("outcomes") or []
    print(f"   type={persisted.get('type')!r}, {len(persisted_outcomes)} outcome(s) persisted")

    names_seen = {tb.outcome_label(o) for o in persisted_outcomes if isinstance(o, dict)}
    for expected in (BRANCH_A, BRANCH_B):
        if expected in names_seen:
            print(f"   [OK] {expected!r} persisted")
        else:
            fail(f"{expected!r} NOT found after write — persisted names: {names_seen}")
            ok = False

    # 6. connect_steps-equivalent, BY NAME --------------------------------
    print(f"\n6. Wiring {BRANCH_A!r} -> end_a, {BRANCH_B!r} -> end_b, by name...")
    source = client.get_element(workflow_id, branch_id)
    for branch_name, target_id in ((BRANCH_A, end_a_id), (BRANCH_B, end_b_id)):
        try:
            matched = tb.resolve_outcome(source, branch_name)
        except tb.ValidationError as e:
            fail(f"resolve_outcome({branch_name!r}) raised: {e}")
            ok = False
            continue

        links = client.get_links(workflow_id)
        link_id = tb.next_id([l.get("link_id") for l in links])
        client.update_tree(
            workflow_id, links=[tb.build_link_create(link_id, branch_id, target_id)]
        )
        client.update_tree(
            workflow_id,
            elements=[tb.build_outcome_update(source, matched["outcomeID"], link_id)],
        )
        print(f"   [OK] {branch_name!r} -> link {link_id} -> step {target_id}")
        # re-fetch so the next resolve_outcome call sees this one as connected
        source = client.get_element(workflow_id, branch_id)

    # 7. Final read-back — right link on right branch? --------------------
    print("\n7. Final verification...")
    final = client.get_element(workflow_id, branch_id)
    final_outcomes = {tb.outcome_label(o): o.get("linkID") for o in (final.get("outcomes") or [])}
    print(f"   {json.dumps(final_outcomes, indent=2, ensure_ascii=False)}")

    link_a, link_b = final_outcomes.get(BRANCH_A), final_outcomes.get(BRANCH_B)
    if link_a and link_b and link_a != link_b:
        print(f"   [OK] {BRANCH_A!r} and {BRANCH_B!r} landed on distinct links "
              f"({link_a} != {link_b})")
    else:
        fail(f"branches did not land on distinct links: "
             f"{BRANCH_A!r}={link_a!r}, {BRANCH_B!r}={link_b!r}")
        ok = False

    print("\n" + "=" * 70)
    keep = "--keep" in sys.argv
    if ok:
        print("[CONFIRMED] Building a new conditional branch with named "
              "custom branches works end-to-end: create, persist, and "
              "resolve-by-name all check out.")
        if keep:
            print(f"\n--keep passed — leaving it in place for inspection:")
            print(f"  https://www.jotform.com/workflow/{workflow_id}/build")
        else:
            print(f"\nCleaning up: deleting {workflow_id}...")
            try:
                client.delete_workflow(workflow_id)
                print("[OK] deleted.")
            except JotformAPIError as e:
                print(f"[WARN] cleanup failed, delete manually: {e}")
    else:
        print("[NOT CONFIRMED] one or more checks failed — workflow left "
              "in place for inspection:")
        print(f"  https://www.jotform.com/workflow/{workflow_id}/build")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())