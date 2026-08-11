"""
Does deleting an element clean up its links, or leave them dangling?

test_write_path.py deleted an element that had no incoming link, so this
was never actually observed. delete_step's preview relies on knowing the
answer to describe the impact accurately, and graph.py's dangling_links
check exists as a safety net either way — this is what confirms which one
is actually true.

Builds: start -> A -> B (two links). Deletes A. Reads back and checks
whether the start->A link and the A->B link survived, and in what shape.

Creates a throwaway workflow. Nothing touches existing work.

Run:
    python -m probes.test_delete_impact
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from mcp_server import tree_builder as tb  # noqa: E402
from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402

A_ID, B_ID = 2, 3
LINK_IN, LINK_OUT = 1, 2


def main() -> int:
    client = JotformClient()
    title = f"ZZ-deleteimpact-DELETE-ME-{datetime.now():%Y%m%d-%H%M%S}"

    created = client.create_workflow(title)
    workflow_id = created.get("id") or created.get("workflowID")
    if not workflow_id:
        print(f"Could not create workflow: {json.dumps(created)[:200]}")
        return 1
    print(f"Workflow: {title} ({workflow_id})\n")

    client.update_tree(workflow_id, elements=[
        tb.build_element_create("workflow_send_email", A_ID, {"name": "A"}, {"x": 0, "y": 200}),
        tb.build_element_create("workflow_send_email", B_ID, {"name": "B"}, {"x": 0, "y": 400}),
    ])
    client.update_tree(workflow_id, links=[
        tb.build_link_create(LINK_IN, 1, A_ID),
        tb.build_link_create(LINK_OUT, A_ID, B_ID),
    ])
    print("Built: start -> A -> B\n")

    before = client.get_workflow_combined(workflow_id)
    print(f"Before delete: {len(before.get('elements', []))} elements, "
          f"{len(before.get('links', []))} links")

    print(f"\nDeleting A (element {A_ID})...")
    try:
        client.update_tree(workflow_id, elements=[
            {"action": "delete", "elementID": A_ID, "data": {"element_id": A_ID}},
        ])
    except JotformAPIError as e:
        print(f"Delete failed: {e}")
        return 1

    after = client.get_workflow_combined(workflow_id)
    elements = [e for e in (after.get("elements") or []) if isinstance(e, dict)]
    links = [l for l in (after.get("links") or []) if isinstance(l, dict)]

    print(f"After delete: {len(elements)} elements, {len(links)} links")
    print(f"element ids remaining: {[e.get('element_id') for e in elements]}")
    print(f"links remaining: "
          f"{[(l.get('link_id'), l.get('fromElement'), l.get('toElement')) for l in links]}")

    print("\n" + "=" * 70)
    surviving_ids = {str(l.get("link_id")) for l in links}
    in_survived = str(LINK_IN) in surviving_ids
    out_survived = str(LINK_OUT) in surviving_ids

    if not in_survived and not out_survived:
        print("Both incident links were removed automatically. delete_step")
        print("needs no extra link cleanup — the API cascades the delete.")
    else:
        dangling = []
        if in_survived:
            dangling.append(f"link {LINK_IN} (start -> A) still exists, now points at a gone step")
        if out_survived:
            dangling.append(f"link {LINK_OUT} (A -> B) still exists, now points FROM a gone step")
        print("Links were NOT cleaned up automatically:")
        for d in dangling:
            print(f"  - {d}")
        print("\ndelete_step should explicitly delete incident links in the same")
        print("call as the element, rather than relying on the API to cascade.")
        print("get_workflow's health.dangling_links (already defensive) would")
        print("catch this if it ever happened without the explicit cleanup.")

    print(f"\nLeft behind: {title} ({workflow_id}) — delete it from the UI.")
    with open("probes/delete_impact_result.json", "w") as f:
        json.dump({"workflow_id": workflow_id, "links_after": links}, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())