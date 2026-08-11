"""
Why does an API-built workflow open to a blank canvas in Jotform's UI?

get_workflow_combined confirms the data exists server-side — steps and
links are there. A blank canvas with no error means the builder's
frontend loaded successfully but found nothing it could render. That
points at a field-shape mismatch: tree_builder.build_element_create sends
a minimal payload (element_id, id, type, elementType, position, x, y,
measured, plus config) — real, UI-created elements may carry additional
fields (uuid, subType, className, resourceType, ...) that the renderer
keys off of.

This fetches elements from two workflows — one built entirely through
this project's updateTree calls, one pre-existing and known to open
correctly in the builder — and diffs the key sets per step type, so the
missing fields show up directly instead of being guessed at.

Usage:
    python -m probes.compare_element_shapes <broken_workflow_id> [<good_workflow_id>]

If <good_workflow_id> is omitted, uses the first workflow in the account
whose title does NOT start with "Claude Desktop Test" or "ZZ-" (i.e. an
older, presumably UI-built one).
"""
from __future__ import annotations

import json
import sys

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402


def pick_good_workflow(client: JotformClient, exclude_id: str) -> str | None:
    for w in client.list_workflows():
        title = str(w.get("title", ""))
        if w.get("id") == exclude_id:
            continue
        if title.startswith(("Claude Desktop Test", "ZZ-")):
            continue
        return w.get("id")
    return None


def dump_elements(client: JotformClient, workflow_id: str) -> dict[str, dict]:
    """type -> one representative raw element of that type, full shape."""
    combined = client.get_workflow_combined(workflow_id)
    elements = [e for e in (combined.get("elements") or []) if isinstance(e, dict)]
    by_type: dict[str, dict] = {}
    for el in elements:
        t = el.get("type")
        if t and t not in by_type:
            by_type[t] = el
    return by_type


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python -m probes.compare_element_shapes <broken_workflow_id> [<good_workflow_id>]")
        return 1

    client = JotformClient()
    broken_id = sys.argv[1]

    try:
        broken = dump_elements(client, broken_id)
    except JotformAPIError as e:
        print(f"Could not read {broken_id}: {e}")
        return 1

    print(f"Broken workflow ({broken_id}) — element types found: {list(broken)}\n")

    if len(sys.argv) >= 3:
        good_id = sys.argv[2]
    else:
        good_id = pick_good_workflow(client, broken_id)
        if not good_id:
            print("Could not find a candidate 'known good' workflow automatically.")
            print("Pass one explicitly: python -m probes.compare_element_shapes "
                  f"{broken_id} <good_workflow_id>")
            return 1
        print(f"(no good workflow id given — using {good_id})\n")

    try:
        good = dump_elements(client, good_id)
    except JotformAPIError as e:
        print(f"Could not read {good_id}: {e}")
        return 1

    print(f"Reference workflow ({good_id}) — element types found: {list(good)}\n")
    print("=" * 70)

    # Always compare the start point first — it's built the same way
    # (create_workflow's hardcoded payload) in both cases, so it should
    # match closely. If it DOESN'T, the problem isn't tree_builder at all.
    types_to_compare = ["workflow_start_point"] + [
        t for t in broken if t != "workflow_start_point"
    ]

    for step_type in types_to_compare:
        b = broken.get(step_type)
        g = good.get(step_type)
        print(f"\n--- {step_type} ---")
        if b is None:
            print("  not present in broken workflow")
            continue
        if g is None:
            print(f"  no reference element of this type in {good_id} — skipping diff")
            print(f"  broken element keys: {sorted(b.keys())}")
            continue

        b_keys, g_keys = set(b.keys()), set(g.keys())
        missing = sorted(g_keys - b_keys)
        extra = sorted(b_keys - g_keys)

        if missing:
            print(f"  MISSING in our element (present in known-good): {missing}")
            for k in missing:
                print(f"    {k}: {json.dumps(g.get(k))[:150]}")
        if extra:
            print(f"  EXTRA in our element (not in known-good): {extra}")
        if not missing and not extra:
            print("  key sets match — check VALUES, not just presence:")
            for k in sorted(b_keys):
                if json.dumps(b.get(k), sort_keys=True) != json.dumps(g.get(k), sort_keys=True):
                    print(f"    {k}: ours={b.get(k)!r}  known-good={g.get(k)!r}")

    print("\n" + "=" * 70)
    print("If workflow_start_point itself shows missing/extra fields above,")
    print("the create_workflow payload has drifted from what the builder")
    print("expects — check that first, since it's shared by every workflow")
    print("this project creates. If start_point matches but other types")
    print("don't, the fix belongs in tree_builder.build_element_create.")
    return 0


if __name__ == "__main__":
    sys.exit(main())