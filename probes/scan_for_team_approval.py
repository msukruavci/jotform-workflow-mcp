"""
Scan a set of workflows for any workflow_approval element and print its
subType — looking for a naturally-occurring "Team Approval" step before
asking anyone to build one by hand in the builder.

Why this might work without manual setup: Jotform's own template
workflows are usually built to showcase varied step types, and "Approve
& Sign" was found exactly this way — reading a real element's data
turned out to have a subType ("workflow_approval_with_sign") no one had
guessed correctly. Team Approval may be the same story: still
workflow_approval, just a different subType.

Edit WORKFLOW_IDS below to scan whichever workflows you want checked —
defaults to the template-sounding ones already in this account.

Run:
    python -m probes.scan_for_team_approval
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from mcp_server.jotform_client import JotformAPIError, JotformClient  # noqa: E402

WORKFLOW_IDS = {
    "262182006257957": "Safety Declaration Approval Workflow Template",
    "262181848664972": "Recruiting Workflow Template",
    "262164010869961": "Automatic Assignment Workflow",
    "262182806337964": "Submission Confirmation Workflow",
    "262163314338958": "Workflow",
    "262181881267968": "İş Akışı",
    "262173642347963": "Workflow",
    "262143154565960": "Workflow",
}


def main() -> int:
    client = JotformClient()
    found_any = False

    for workflow_id, title in WORKFLOW_IDS.items():
        try:
            combined = client.get_workflow_combined(workflow_id)
        except JotformAPIError as e:
            print(f"[{workflow_id}] {title!r} — [FAIL] {e}")
            continue

        elements = [e for e in (combined.get("elements") or []) if isinstance(e, dict)]
        approvals = [e for e in elements if e.get("type") == "workflow_approval"]

        if not approvals:
            print(f"[{workflow_id}] {title!r} — no workflow_approval elements")
            continue

        for el in approvals:
            found_any = True
            sub = el.get("subType")
            name = el.get("name")
            outcomes = el.get("outcomes") or []
            outcome_texts = [o.get("text") or o.get("branchName") for o in outcomes
                            if isinstance(o, dict)]
            flag = "  <-- NOT workflow_approval_with_sign / plain approval" \
                if sub not in (None, "", "workflow_approval_with_sign") else ""
            print(f"[{workflow_id}] {title!r} — element {el.get('element_id')}: "
                  f"name={name!r} subType={sub!r} outcomes={outcome_texts}{flag}")

    print()
    if not found_any:
        print("No workflow_approval elements found in any scanned workflow. "
              "Team Approval will need to be built by hand in the builder, "
              "then read the same way inspect_approval_outcomes.py did for "
              "Approve & Sign.")
    return 0


if __name__ == "__main__":
    sys.exit(main())