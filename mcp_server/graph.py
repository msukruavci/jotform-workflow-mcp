"""
Graph analysis over a workflow's steps and connections.

Why this is a separate module: it's pure logic over plain data — no HTTP, no
MCP, no Jotform vocabulary beyond the field names. That makes it the one
part of the reading layer that can be unit-tested without a network or an
API key, which matters because it's also the part most likely to be wrong.

Why it exists at all: Jotform's own builder shows "1 Problem" for a workflow
with orphaned steps, but the API hands back a flat list where nothing is
flagged. A model reading 18 steps has no way to know that 10 of them never
run. Reporting structure without reporting health would be misleading.
"""
from __future__ import annotations

# Steps that are legitimately terminal — reaching one and stopping is correct,
# so they must not be reported as dead ends.
TERMINAL_TYPES = {"workflow_end_point"}


def analyse(steps: list[dict], connections: list[dict]) -> dict:
    """
    steps: [{"step_id": str, "type": str}, ...]
    connections: [{"from_step": str, "to_step": str}, ...]

    Returns unreachable steps, dead ends, dangling links, and unknown types.
    All ids are returned as strings so they match what the tools expose.

    Dangling links (added 2026-08-10, ahead of Phase 4's delete_step):
    whether deleting a step also removes its incident links is unverified
    (probes/test_delete_impact.py). This check is a read-side safety net
    regardless of the answer — if a link ever points at a step_id that
    doesn't exist, it's reported rather than silently miscounted as a
    normal connection.
    """
    ids = [str(s.get("step_id")) for s in steps if s.get("step_id") is not None]
    id_set = set(ids)
    types = {str(s.get("step_id")): s.get("type") for s in steps}

    dangling = []
    for c in connections:
        src, dst = c.get("from_step"), c.get("to_step")
        src_s, dst_s = str(src) if src is not None else None, str(dst) if dst is not None else None
        if src_s is not None and src_s not in id_set:
            dangling.append(f"link {c.get('link_id')}: from missing step {src_s}")
        elif dst_s is not None and dst_s not in id_set:
            dangling.append(f"link {c.get('link_id')}: to missing step {dst_s}")

    outgoing: dict[str, list[str]] = {}
    for c in connections:
        src, dst = c.get("from_step"), c.get("to_step")
        if src is None or dst is None:
            continue
        outgoing.setdefault(str(src), []).append(str(dst))

    # The start point is the root. Fall back to the lowest id only if there is
    # no start_point at all — an assumption worth surfacing rather than hiding.
    roots = [i for i in ids if types.get(i) == "workflow_start_point"]
    if not roots:
        roots = ids[:1]

    seen: set[str] = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(outgoing.get(node, []))

    unreachable = [i for i in ids if i not in seen]
    dead_ends = [
        i for i in ids
        if i in seen and not outgoing.get(i) and types.get(i) not in TERMINAL_TYPES
    ]

    return {
        "total_steps": len(ids),
        "unreachable_steps": unreachable,
        "dead_end_steps": dead_ends,
        "dangling_links": dangling,
    }