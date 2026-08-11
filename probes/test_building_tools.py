"""
End-to-end test of Layer 3 (building) — through the tools, not the client.

Why call_tool and not JotformClient directly: the client working proves the
API works. It doesn't prove add_step's linking rule, connect_steps' outcome
validation, or that Pydantic models serialise the way the model will see
them. This exercises the actual path a conversation takes.

Builds one real workflow: start -> if/else -> {TRUE: email, FALSE: task},
then a second linear step chained after the email. Also deliberately
triggers three failure modes, because a tool that behaves correctly under
misuse matters as much as one that behaves correctly under correct use.

Run:
    python -m probes.test_building_tools
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from mcp_server.server import mcp  # noqa: E402

RESULTS: list[dict] = []


def unwrap(result):
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        return sc
    content = getattr(result, "content", None) or []
    texts = [getattr(c, "text", None) for c in content]
    texts = [t for t in texts if t]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except (ValueError, TypeError):
            return texts[0]
    return [json.loads(t) if t.strip().startswith("{") else t for t in texts]


async def call(label: str, tool: str, args: dict, expect_error: bool = False):
    result = unwrap(await mcp.call_tool(tool, args))
    has_error = isinstance(result, dict) and bool(result.get("error"))
    ok = has_error if expect_error else not has_error
    RESULTS.append({"label": label, "ok": ok})
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {label}")
    if isinstance(result, dict) and (result.get("error") or result.get("warnings")):
        if result.get("error"):
            print(f"       error: {result['error']}")
        if result.get("hint"):
            print(f"       hint:  {result['hint']}")
        if result.get("warnings"):
            print(f"       warnings: {result['warnings']}")
    return result


async def main() -> int:
    title = f"ZZ-buildtools-DELETE-ME-{datetime.now():%Y%m%d-%H%M%S}"

    print("=" * 70)
    print("HAPPY PATH")
    print("=" * 70)

    wf = await call("create_workflow", "create_workflow", {"title": title})
    workflow_id = wf.get("workflow_id")
    if not workflow_id:
        print("\nCannot continue without a workflow id.")
        return summarize()

    decision = await call(
        "add_step: if/else after start (1)", "add_step",
        {"workflow_id": workflow_id, "step_type": "workflow_binary_decision",
         "config": {"name": "Probe decision"}, "after_step_id": "1"},
    )
    decision_id = decision.get("step_id")

    email = await call(
        "add_step: email (unattached)", "add_step",
        {"workflow_id": workflow_id, "step_type": "workflow_send_email",
         "config": {"subject": "Approved"}},
    )
    email_id = email.get("step_id")

    task = await call(
        "add_step: task (unattached)", "add_step",
        {"workflow_id": workflow_id, "step_type": "workflow_assign_task",
         "config": {"name": "Follow up"}},
    )
    task_id = task.get("step_id")

    if decision_id and email_id:
        await call(
            "connect_steps: decision -TRUE-> email", "connect_steps",
            {"workflow_id": workflow_id, "from_step_id": decision_id,
             "to_step_id": email_id, "outcome": "true"},  # lowercase on purpose
        )
    if decision_id and task_id:
        await call(
            "connect_steps: decision -FALSE-> task", "connect_steps",
            {"workflow_id": workflow_id, "from_step_id": decision_id,
             "to_step_id": task_id, "outcome": "FALSE"},
        )

    followup = None
    if email_id:
        followup = await call(
            "add_step: chained after email via after_step_id", "add_step",
            {"workflow_id": workflow_id, "step_type": "workflow_pause",
             "config": {}, "after_step_id": email_id},
        )

    if email_id:
        await call(
            "update_step: change email subject", "update_step",
            {"workflow_id": workflow_id, "step_id": email_id,
             "config": {"subject": "Approved!!"}},
        )

    print()
    print("=" * 70)
    print("FAILURE MODES — these should all report an error, not raise")
    print("=" * 70)

    await call(
        "connect_steps: branching step with NO outcome", "connect_steps",
        {"workflow_id": workflow_id, "from_step_id": decision_id,
         "to_step_id": task_id, "outcome": ""},
        expect_error=True,
    )
    await call(
        "connect_steps: outcome that doesn't exist", "connect_steps",
        {"workflow_id": workflow_id, "from_step_id": decision_id,
         "to_step_id": task_id, "outcome": "MAYBE"},
        expect_error=True,
    )
    await call(
        "connect_steps: outcome already connected (TRUE again)", "connect_steps",
        {"workflow_id": workflow_id, "from_step_id": decision_id,
         "to_step_id": task_id, "outcome": "TRUE"},
        expect_error=True,
    )
    if email_id and followup and followup.get("step_id"):
        await call(
            "add_step: after_step_id already has an exit", "add_step",
            {"workflow_id": workflow_id, "step_type": "workflow_send_email",
             "config": {}, "after_step_id": email_id},
            expect_error=True,
        )
    await call(
        "add_step: unknown step_type", "add_step",
        {"workflow_id": workflow_id, "step_type": "workflow_not_real",
         "config": {}, "after_step_id": ""},
        expect_error=True,
    )
    await call(
        "add_step: config field that doesn't exist -> warning, not error",
        "add_step",
        {"workflow_id": workflow_id, "step_type": "workflow_send_email",
         "config": {"nonsense_field": "x"}, "after_step_id": ""},
        expect_error=False,  # should succeed WITH a warning
    )

    print()
    print("=" * 70)
    print("READING IT BACK — does get_workflow agree with what we built?")
    print("=" * 70)
    final = await call("get_workflow (final state)", "get_workflow",
                       {"workflow_id": workflow_id})
    print(f"steps: {len(final.get('steps', []))}   "
          f"connections: {len(final.get('connections', []))}")
    outcomes_seen = {c.get("outcome") for c in final.get("connections", []) if c.get("outcome")}
    print(f"outcomes present: {outcomes_seen}")
    if {"TRUE", "FALSE"} - outcomes_seen:
        RESULTS.append({"label": "get_workflow shows both TRUE and FALSE", "ok": False})
        print("[FAIL] expected both TRUE and FALSE labelled in the read-back")
    else:
        RESULTS.append({"label": "get_workflow shows both TRUE and FALSE", "ok": True})
        print("[PASS] both branches correctly labelled on read-back")

    health = final.get("health") or {}
    print(f"health: {health}")

    print(f"\nLeft behind: {title} ({workflow_id}) — delete it from the UI.")
    return summarize()


def summarize() -> int:
    passed = sum(r["ok"] for r in RESULTS)
    total = len(RESULTS)
    print()
    print("=" * 70)
    print(f"{passed}/{total} passed")
    for r in RESULTS:
        if not r["ok"]:
            print(f"  FAIL: {r['label']}")
    print("=" * 70)
    with open("probes/building_tools_result.json", "w") as f:
        json.dump(RESULTS, f, indent=2)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))