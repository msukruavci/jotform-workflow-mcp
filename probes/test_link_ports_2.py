"""
What does updateTree require to write a link? (round 2)

Round 1 result: all five variants failed identically with
`Missing parameters: points` — including the one that sent `"points": []`.
An empty array is treated as absent, which is what PHP's empty() does. Real
workflows carry non-empty junk there: [{'a': '1'}], [{'1': 2}].

So nothing was learned about port names; the request never got past the
points check. This runs in two phases:

  Phase 1 — hold ports at known-good values, vary `points` until one is
            accepted.
  Phase 2 — take the winning points shape and vary the ports, to answer the
            question that actually shapes tree_builder: does the API
            validate port names, or accept anything non-empty?

If ports are not validated, tree_builder can use one constant pair for every
link. If they are, it needs a port model per step type, and that is a much
bigger piece of work to plan for.

Creates a throwaway workflow. Nothing touches existing work.

Run:
    python -m probes.test_link_ports2
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402

GOOD_FROM_PORT = "DYNAMIC_BOTTOM_1_Out"
GOOD_TO_PORT = "DYNAMIC_TOP_1_In"

POINTS_CANDIDATES = [
    ('[{"a": "1"}]', [{"a": "1"}], "verbatim from a real workflow"),
    ('[{"1": 2}]', [{"1": 2}], "the other shape seen in real data"),
    ('[{"x": 0, "y": 0}]', [{"x": 0, "y": 0}], "plausible coordinates"),
    ('[[0, 0]]', [[0, 0]], "coordinate pair as an array"),
    ('[{}]', [{}], "minimal non-empty — is only presence checked?"),
]

PORT_CANDIDATES = [
    ("real pair", GOOD_FROM_PORT, GOOD_TO_PORT, "the known-good baseline"),
    ("side ports", "RIGHT_MIDDLE_Out", "LEFT_MIDDLE_In", "another pair seen in real data"),
    ("nonsense", "BANANA_Out", "BANANA_In", "THE question — if accepted, ports are cosmetic"),
    ("empty strings", "", "", "is presence checked, or the value?"),
]


class Runner:
    def __init__(self, client: JotformClient, workflow_id: str):
        self.client = client
        self.workflow_id = workflow_id
        self.next_element = 2
        self.next_link = 1
        self.log: list[dict] = []

    def new_target(self) -> int:
        """A fresh element per attempt, so no attempt can be blocked by a
        target that already has an incoming link."""
        eid = self.next_element
        self.next_element += 1
        self.client.update_tree(self.workflow_id, elements=[{
            "action": "create", "elementID": eid,
            "data": {
                "element_id": eid, "id": eid,
                "type": "workflow_send_email", "elementType": "workflow_send_email",
                "name": f"Target {eid}",
                "position": {"x": (eid - 2) * 320, "y": 220},
                "x": (eid - 2) * 320, "y": 220,
                "measured": {"width": 296, "height": 88},
            },
        }])
        return eid

    def try_link(self, label: str, why: str, *, points, from_port, to_port,
                 link_type: str = "default-link") -> bool:
        target = self.new_target()
        link_id = self.next_link
        self.next_link += 1
        data = {
            "link_id": link_id, "fromElement": 1, "toElement": target,
            "type": link_type, "fromPortName": from_port,
            "toPortName": to_port, "points": points,
        }
        try:
            self.client.update_tree(self.workflow_id, links=[
                {"action": "create", "linkID": link_id, "data": data},
            ])
            ok, detail = True, ""
        except JotformAPIError as e:
            try:
                detail = json.loads(e.body).get("message", e.body[:120])
            except (ValueError, TypeError):
                detail = e.body[:120]
            ok = False
        self.log.append({"label": label, "accepted": ok, "detail": detail,
                         "link_id": link_id, "target": target})
        print(f"  [{'OK ' if ok else 'NO '}] {label:<22} {why}")
        if not ok:
            print(f"         -> {detail}")
        return ok


def main() -> int:
    client = JotformClient()
    title = f"ZZ-ports2-DELETE-ME-{datetime.now():%Y%m%d-%H%M%S}"
    created = client.create_workflow(title)
    workflow_id = created.get("id") or created.get("workflowID")
    if not workflow_id:
        print(f"Could not create workflow: {json.dumps(created)[:200]}")
        return 1
    print(f"Workflow: {title} ({workflow_id})\n")

    runner = Runner(client, workflow_id)

    print("PHASE 1 — which `points` shape is accepted?")
    print("-" * 70)
    winner = None
    for label, points, why in POINTS_CANDIDATES:
        if runner.try_link(label, why, points=points,
                           from_port=GOOD_FROM_PORT, to_port=GOOD_TO_PORT):
            winner = points
            print(f"\n  -> accepted: {label}\n")
            break

    if winner is None:
        print("\nNo points shape worked. Everything else is blocked behind this.")
        print("Capture a real link write from the builder: F12 -> Network ->")
        print("drag a connection -> find the updateTree call -> copy its body.")
        return finish(client, workflow_id, title, runner)

    print("PHASE 2 — are port names validated?")
    print("-" * 70)
    for label, from_port, to_port, why in PORT_CANDIDATES:
        runner.try_link(label, why, points=winner,
                        from_port=from_port, to_port=to_port)

    runner.try_link("nonsense link type", "is `type` checked?", points=winner,
                    from_port=GOOD_FROM_PORT, to_port=GOOD_TO_PORT,
                    link_type="banana-link")

    return finish(client, workflow_id, title, runner)


def finish(client: JotformClient, workflow_id: str, title: str, runner: Runner) -> int:
    print("\n" + "=" * 70)
    combined = client.get_workflow_combined(workflow_id)
    links = [l for l in (combined.get("links") or []) if isinstance(l, dict)]
    by_id = {str(l.get("link_id")): l for l in links}

    accepted = [e for e in runner.log if e["accepted"]]
    print(f"Accepted: {len(accepted)}/{len(runner.log)}   Persisted: {len(links)}")

    # Accepted is not persisted, and persisted is not unchanged. The API may
    # quietly rewrite what it was sent, which would be the worst failure mode
    # to discover later.
    for entry in accepted:
        stored = by_id.get(str(entry["link_id"]))
        if stored is None:
            print(f"  {entry['label']:<22} ACCEPTED BUT NOT SAVED")
            continue
        print(f"  {entry['label']:<22} saved   ports: "
              f"{stored.get('fromPortName')!r} -> {stored.get('toPortName')!r}   "
              f"points: {json.dumps(stored.get('points'))[:40]}")

    print("\n" + "=" * 70)
    nonsense = next((e for e in runner.log if e["label"] == "nonsense"), None)
    if nonsense and nonsense["accepted"]:
        print("Port names are NOT validated — tree_builder can use one constant")
        print("pair for every link. Check above whether the API rewrote them:")
        print("if it did, it computes the real ports itself and there is nothing")
        print("left to model.")
    elif nonsense:
        print("Port names ARE validated. tree_builder needs to know the valid")
        print("ports per step type — work that out before designing add_step.")

    print(f"\nLeft behind: {title} ({workflow_id}) — delete it from the UI.")
    with open("probes/link_ports2_result.json", "w") as f:
        json.dump({"workflow_id": workflow_id, "attempts": runner.log,
                   "persisted": links}, f, indent=2)
    print("Full detail: probes/link_ports2_result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())