"""
Does the public API support deleting a whole workflow?

Never tried in this project. Worth knowing for two reasons: it's the
obvious Phase 4 tool we haven't built (delete_workflow), and it's also how
you clean up the growing pile of ZZ-...-DELETE-ME-... throwaways this
session's probes have left on the account.

Tries a few plausible endpoints — Jotform's form API uses
DELETE /form/{id}, so DELETE /workflow/{id} is the first guess — and
reports which one (if any) actually works. Does NOT touch any workflow
without asking first.

Run:
    python -m probes.test_delete_workflow              # interactive menu
    python -m probes.test_delete_workflow --sweep-probes # deletes every
                                                           # ZZ-...-DELETE-ME
                                                           # workflow, after
                                                           # one confirmation
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402

CANDIDATES = [
    ("DELETE /workflow/{id}", "DELETE", "/workflow/{id}"),
    ("POST /workflow/{id} status=DELETED", "POST", "/workflow/{id}", {"status": "DELETED"}),
    ("PUT /workflow/{id}/status", "PUT", "/workflow/{id}/status", {"status": "DELETED"}),
]


def try_delete(client: JotformClient, workflow_id: str) -> str | None:
    """Returns the label of whichever candidate worked, or None."""
    for entry in CANDIDATES:
        label, method, path_template, *body = entry
        path = path_template.format(id=workflow_id)
        try:
            client._request(method, path, json_body=(body[0] if body else None))
            return label
        except JotformAPIError as e:
            print(f"  [NO ] {label:<38} {e.status}")
            continue
    return None


def verify_gone(client: JotformClient, workflow_id: str) -> bool:
    try:
        workflows = client.list_workflows()
    except JotformAPIError:
        return False
    return workflow_id not in {w.get("id") for w in workflows}


def sweep_probes(client: JotformClient) -> int:
    workflows = client.list_workflows()
    litter = [w for w in workflows if str(w.get("title", "")).startswith("ZZ-")]
    if not litter:
        print("No ZZ-...-DELETE-ME workflows found.")
        return 0

    print(f"Found {len(litter)} probe workflows to delete:")
    for w in litter:
        print(f"  {w.get('id')}  {w.get('title')}")
    reply = input(f"\nDelete all {len(litter)}? [y/N] ").strip().lower()
    if reply != "y":
        print("Cancelled.")
        return 0

    ok, failed = 0, []
    for w in litter:
        wid = w.get("id")
        print(f"\nDeleting {wid} ({w.get('title')})...")
        which = try_delete(client, wid)
        if which:
            print(f"  [OK ] via {which}")
            ok += 1
        else:
            print("  [FAIL] no candidate worked")
            failed.append(wid)

    print(f"\n{ok}/{len(litter)} deleted.")
    if failed:
        print(f"Still on your account, delete manually: {failed}")
    return 0 if not failed else 1


def main() -> int:
    client = JotformClient()

    if "--sweep-probes" in sys.argv:
        return sweep_probes(client)

    workflows = client.list_workflows()
    if not workflows:
        print("No workflows on this account.")
        return 1

    print("Which workflow to test deletion on?")
    print("(pick one you don't mind losing — ideally a ZZ-...-DELETE-ME one)\n")
    for i, w in enumerate(workflows):
        print(f"  [{i}] {w.get('id')}  {w.get('title')}")
    choice = input("\nIndex: ").strip()
    try:
        target = workflows[int(choice)]
    except (ValueError, IndexError):
        print("Invalid choice.")
        return 1

    workflow_id = target.get("id")
    reply = input(f"Really delete '{target.get('title')}' ({workflow_id})? [y/N] ").strip().lower()
    if reply != "y":
        print("Cancelled.")
        return 0

    print(f"\nTrying candidates against {workflow_id}:")
    which = try_delete(client, workflow_id)

    if which is None:
        print("\nNo candidate endpoint worked. Deleting a workflow via the")
        print("public API is not confirmed possible — a finding for the gap")
        print("report, not a bug. delete_workflow should not be built as an")
        print("MCP tool until this changes.")
        return 1

    print(f"\n[OK] accepted via {which}")
    print("Verifying it's actually gone (not just a 200)...")
    if verify_gone(client, workflow_id):
        print(f"[CONFIRMED] {which} works and persists. Safe to build delete_workflow.")
        return 0
    else:
        print(f"[WARNING] {which} returned success but the workflow still")
        print("appears in list_workflows. A 200 does not mean it worked —")
        print("do not trust this endpoint without further checking.")
        return 1


if __name__ == "__main__":
    sys.exit(main())