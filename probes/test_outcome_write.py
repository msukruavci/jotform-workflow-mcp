"""
Can `outcomes` be written, not just read?

Gap 1 established that branch identity lives on the deciding element:
`outcomes[] = {outcomeID, conditionValue, linkID}`. That was read-only
evidence. This is the missing half: does an `action: "update"` on that
element through updateTree actually let us set `linkID`, or does the
builder set it through some other path entirely?

This is the one unknown that decides how connect_steps() gets built:

  - if outcomes update cleanly -> connect_steps() is: write the link with
    the constant port payload (gap 2), then update the decision element's
    outcomes so the right outcomeID points at the new linkID. Ordinary
    tree_builder work.
  - if it does not -> branching needs a different mechanism, and that has
    to be found before add_step's branching case is designed at all.

Steps:
  1. create workflow
  2. add a workflow_binary_decision element (default outcomes: TRUE/FALSE,
     both with linkID unset — matches what get_step_schema shows as default)
  3. add two target email elements
  4. write TWO links from the decision element
  5. update the decision element: set outcomes[0].linkID and
     outcomes[1].linkID to the two new link ids
  6. read back — does get_element show the updated linkIDs? does
     get_workflow's outcome_by_link mapping resolve correctly?

Creates a throwaway workflow. Nothing touches existing work.

Run:
    python -m probes.test_outcome_write
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402

DECISION_ID = 2
TRUE_TARGET = 3
FALSE_TARGET = 4
TRUE_LINK = 1
FALSE_LINK = 2

# The constant payload gap 2 established works for any link.
LINK_DEFAULTS = {
    "type": "default-link", "points": [{"a": "1"}],
    "fromPortName": "DYNAMIC_BOTTOM_1_Out", "toPortName": "DYNAMIC_TOP_1_In",
}


def step(label: str, fn):
    try:
        value = fn()
        print(f"[PASS] {label}")
        return True, value
    except JotformAPIError as e:
        detail = e.body[:200]
        try:
            detail = json.loads(e.body).get("message", detail)
        except (ValueError, TypeError):
            pass
        print(f"[FAIL] {label}\n       {e.status}: {detail}")
        return False, None
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {label}\n       {type(e).__name__}: {e}")
        return False, None


def main() -> int:
    client = JotformClient()
    title = f"ZZ-outcomes-DELETE-ME-{datetime.now():%Y%m%d-%H%M%S}"

    ok, created = step("1. create workflow", lambda: client.create_workflow(title))
    if not ok:
        return 1
    workflow_id = created.get("id") or created.get("workflowID")
    print(f"       id: {workflow_id}\n")

    def add_elements():
        client.update_tree(workflow_id, elements=[
            {
                "action": "create", "elementID": DECISION_ID,
                "data": {
                    "element_id": DECISION_ID, "id": DECISION_ID,
                    "type": "workflow_binary_decision",
                    "elementType": "workflow_binary_decision",
                    "name": "Probe decision",
                    "position": {"x": 0, "y": 150}, "x": 0, "y": 150,
                    "measured": {"width": 296, "height": 88},
                    "outcomes": [
                        {"id": 1, "outcomeID": 1, "type": "CONDITION", "conditionValue": "TRUE"},
                        {"id": 2, "outcomeID": 2, "type": "CONDITION", "conditionValue": "FALSE"},
                    ],
                },
            },
            {
                "action": "create", "elementID": TRUE_TARGET,
                "data": {
                    "element_id": TRUE_TARGET, "id": TRUE_TARGET,
                    "type": "workflow_send_email", "elementType": "workflow_send_email",
                    "name": "TRUE target",
                    "position": {"x": -200, "y": 320}, "x": -200, "y": 320,
                    "measured": {"width": 296, "height": 88},
                },
            },
            {
                "action": "create", "elementID": FALSE_TARGET,
                "data": {
                    "element_id": FALSE_TARGET, "id": FALSE_TARGET,
                    "type": "workflow_send_email", "elementType": "workflow_send_email",
                    "name": "FALSE target",
                    "position": {"x": 200, "y": 320}, "x": 200, "y": 320,
                    "measured": {"width": 296, "height": 88},
                },
            },
        ])

    ok, _ = step("2. add decision + two targets", add_elements)
    if not ok:
        return finish(client, workflow_id, title, False)

    def add_links():
        client.update_tree(workflow_id, links=[
            {"action": "create", "linkID": TRUE_LINK,
             "data": {"link_id": TRUE_LINK, "fromElement": DECISION_ID,
                      "toElement": TRUE_TARGET, **LINK_DEFAULTS}},
            {"action": "create", "linkID": FALSE_LINK,
             "data": {"link_id": FALSE_LINK, "fromElement": DECISION_ID,
                      "toElement": FALSE_TARGET, **LINK_DEFAULTS}},
        ])

    ok, _ = step("3. link decision -> both targets (linkID unset on outcomes)", add_links)
    if not ok:
        return finish(client, workflow_id, title, False)

    # This is the actual question: does updating the element's `outcomes`
    # stick, using the same action:"update" pattern as any other field edit?
    def update_outcomes():
        client.update_tree(workflow_id, elements=[{
            "action": "update", "elementID": DECISION_ID,
            "data": {
                "element_id": DECISION_ID,
                "outcomes": [
                    {"id": 1, "outcomeID": 1, "type": "CONDITION",
                     "conditionValue": "TRUE", "linkID": TRUE_LINK},
                    {"id": 2, "outcomeID": 2, "type": "CONDITION",
                     "conditionValue": "FALSE", "linkID": FALSE_LINK},
                ],
            },
        }])

    ok, _ = step("4. update outcomes to point at the two links", update_outcomes)

    return finish(client, workflow_id, title, ok)


def finish(client: JotformClient, workflow_id: str, title: str, write_attempted: bool) -> int:
    print("\n" + "=" * 70)
    print("5. reading back")
    print("=" * 70)
    try:
        el = client.get_element(workflow_id, DECISION_ID)
    except JotformAPIError as e:
        print(f"get_element failed: {e}")
        el = {}

    outcomes = el.get("outcomes") or []
    print(f"outcomes on decision element:\n{json.dumps(outcomes, indent=2)}")

    linked = {o.get("conditionValue"): o.get("linkID") for o in outcomes if isinstance(o, dict)}
    success = write_attempted and linked.get("TRUE") == TRUE_LINK and linked.get("FALSE") == FALSE_LINK

    print("\n" + "=" * 70)
    if success:
        print("CONFIRMED: outcomes.linkID can be set via action:'update' on the")
        print("element, same pattern as any other field edit. connect_steps() can")
        print("be: write the link (constant payload), then update() the source")
        print("element's outcomes to point the right outcomeID at the new linkID.")
    else:
        print("NOT CONFIRMED — outcomes did not update as expected. Read the")
        print("outcomes dump above: if linkID is still null/0, the update call")
        print("was silently ignored for that field, and branch-writing needs a")
        print("different approach (maybe outcomes must be set at element-create")
        print("time, together with the links, in the same updateTree call).")

    print(f"\nLeft behind: {title} ({workflow_id}) — delete it from the UI.")
    with open("probes/outcome_write_result.json", "w") as f:
        json.dump({"workflow_id": workflow_id, "outcomes_after": outcomes,
                   "success": success}, f, indent=2)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())