"""
What does updateTree actually require to write a link?

Adding an element works; adding a link returns:

    400 Missing parameters: type, fromPortName, toPortName, points

So port names are mandatory on write, even though they carry no meaning on
read. Real workflows show a vocabulary — DYNAMIC_BOTTOM_1_Out,
RIGHT_MIDDLE_Out, DYNAMIC_TOP_1_Out / DYNAMIC_TOP_1_In, LEFT_MIDDLE_In,
DYNAMIC_BOTTOM_1_In — but not the rule behind it.

Three questions, and the answers decide how tree_builder is designed:

  1. Is one safe default pair enough for a plain step-to-step link?
  2. Does the API validate port names, or accept anything non-empty?
  3. Does `points` need real content, or will an empty list do?

If the API validates, tree_builder needs a port model per step type. If it
does not, port names are cosmetic and one constant pair covers every link —
a much smaller problem.

Creates a throwaway workflow. Nothing touches existing work.

Run:
    python -m probes.test_link_ports
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402

# Each variant links the start point (1) to its own email element, so they
# can all be attempted in one workflow and read back together.
VARIANTS = [
    {
        "name": "A. copied from real data",
        "why": "the values a working workflow actually uses",
        "link": {"type": "default-link", "fromPortName": "DYNAMIC_BOTTOM_1_Out",
                 "toPortName": "DYNAMIC_TOP_1_In", "points": []},
    },
    {
        "name": "B. same, points omitted",
        "why": "is points required, or just named in the error?",
        "link": {"type": "default-link", "fromPortName": "DYNAMIC_BOTTOM_1_Out",
                 "toPortName": "DYNAMIC_TOP_1_In"},
    },
    {
        "name": "C. side ports",
        "why": "does a different valid-looking pair also work?",
        "link": {"type": "default-link", "fromPortName": "RIGHT_MIDDLE_Out",
                 "toPortName": "LEFT_MIDDLE_In", "points": []},
    },
    {
        "name": "D. nonsense port names",
        "why": "THE key question — if this passes, ports are cosmetic",
        "link": {"type": "default-link", "fromPortName": "BANANA_Out",
                 "toPortName": "BANANA_In", "points": []},
    },
    {
        "name": "E. nonsense link type",
        "why": "is `type` validated too?",
        "link": {"type": "banana-link", "fromPortName": "DYNAMIC_BOTTOM_1_Out",
                 "toPortName": "DYNAMIC_TOP_1_In", "points": []},
    },
]


def main() -> int:
    client = JotformClient()
    title = f"ZZ-ports-DELETE-ME-{datetime.now():%Y%m%d-%H%M%S}"

    created = client.create_workflow(title)
    workflow_id = created.get("id") or created.get("workflowID")
    if not workflow_id:
        print(f"Could not create workflow: {json.dumps(created)[:200]}")
        return 1
    print(f"Workflow: {title} ({workflow_id})\n")

    # One email element per variant, ids 2..N.
    elements = [
        {
            "action": "create", "elementID": i + 2,
            "data": {
                "element_id": i + 2, "id": i + 2,
                "type": "workflow_send_email",
                "elementType": "workflow_send_email",
                "name": f"Target {i + 2}",
                "position": {"x": i * 320, "y": 200}, "x": i * 320, "y": 200,
                "measured": {"width": 296, "height": 88},
            },
        }
        for i in range(len(VARIANTS))
    ]
    client.update_tree(workflow_id, elements=elements)
    print(f"{len(elements)} target elements created\n")

    results = []
    for i, variant in enumerate(VARIANTS):
        link_id = i + 1
        target = i + 2
        data = {"link_id": link_id, "fromElement": 1, "toElement": target, **variant["link"]}
        try:
            client.update_tree(
                workflow_id,
                links=[{"action": "create", "linkID": link_id, "data": data}],
            )
            accepted, detail = True, ""
        except JotformAPIError as e:
            accepted, detail = False, f"{e.status} {e.body[:150]}"
        results.append({"variant": variant["name"], "accepted": accepted, "detail": detail})
        print(f"[{'ACCEPTED' if accepted else 'REJECTED'}] {variant['name']}")
        print(f"           ({variant['why']})")
        if detail:
            print(f"           {detail}")

    # Accepted is not the same as persisted — read back and see what survived,
    # and whether the API rewrote any of the values we sent.
    print("\n" + "=" * 70)
    combined = client.get_workflow_combined(workflow_id)
    links = [l for l in (combined.get("links") or []) if isinstance(l, dict)]
    print(f"Links that actually persisted: {len(links)}/{len(VARIANTS)}")
    for link in sorted(links, key=lambda l: str(l.get("link_id"))):
        sent = next(
            (v["link"] for i, v in enumerate(VARIANTS) if str(i + 1) == str(link.get("link_id"))),
            {},
        )
        changed = {
            k: f"{sent.get(k)!r} -> {link.get(k)!r}"
            for k in ("type", "fromPortName", "toPortName")
            if k in sent and str(sent.get(k)) != str(link.get(k))
        }
        print(f"  link {link.get('link_id')} -> step {link.get('toElement')}"
              f"   {'REWRITTEN: ' + str(changed) if changed else 'kept as sent'}")

    print("\n" + "=" * 70)
    accepted_names = [r["variant"] for r in results if r["accepted"]]
    if any(n.startswith("D.") for n in accepted_names):
        print("Port names are NOT validated. tree_builder can use one constant")
        print("pair for every link — the port problem largely disappears.")
    elif accepted_names:
        print("Port names ARE validated. tree_builder needs a port model per")
        print("step type; work out the vocabulary before designing add_step.")
    else:
        print("No variant worked. Link writing needs more than these fields —")
        print("capture a real link write from the builder UI (F12 -> Network).")

    print(f"\nLeft behind: {title} ({workflow_id}) — delete it from the UI.")
    with open("probes/link_ports_result.json", "w") as f:
        json.dump({"workflow_id": workflow_id, "results": results,
                   "persisted": links}, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())