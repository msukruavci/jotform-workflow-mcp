"""
Builds a real, branching workflow from scratch, using everything this
project has learned:

  Start --> If/Else Condition --TRUE--> Send Email
                              --FALSE--> Assign Task

Each step is printed clearly so it can be explained afterward. Uses
workflow_binary_decision for the branch (we have a confirmed CREATE
recipe for it from real browser traffic) rather than
workflow_conditional_branch (which we've only ever read, never created).

Run: python -m probes.build_branching_workflow
"""
import json
import os

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TEST_FORM_ID = os.environ.get("TEST_FORM_ID", "")
MY_EMAIL = os.environ.get("MY_EMAIL", "")


def parse(record):
    try:
        return json.loads(record["response_snippet"])
    except (ValueError, KeyError):
        return None


def main() -> None:
    if not TEST_FORM_ID or not MY_EMAIL:
        print("Need TEST_FORM_ID and MY_EMAIL set in .env.")
        return

    # --- Step 0: find a real field on the trigger form to branch on ---
    questions = probe(
        "GET", f"{BASE}/form/{TEST_FORM_ID}/questions",
        label="get_form_questions", surface="public-api",
    )
    q_data = parse(questions)
    q_content = q_data.get("content", {}) if q_data else {}
    if not q_content:
        print("Couldn't read form questions — stopping.")
        return
    first_qid = list(q_content.keys())[0]
    first_q_text = q_content[first_qid].get("text", "")
    print(f"Using form field {first_qid} ({first_q_text!r}) as the condition field.\n")

    # --- Step 1: fresh workflow, start point only ---
    create = probe(
        "POST", f"{BASE}/workflow",
        label="branch_create", surface="public-api",
        json_body={
            "title": "Branching Demo Workflow",
            "triggerOnEdit": "ENABLED",
            "elements": [
                {"data": {"element_id": 1, "className": ["isStartPoint"],
                          "elementType": "workflow_start_point", "type": "workflow_start_point",
                          "id": 1, "position": {"x": 0, "y": 0},
                          "measured": {"width": 296, "height": 88}, "x": 0, "y": 0},
                 "elementID": 1, "action": "update"},
            ],
            "links": [],
        },
    )
    wf_id = parse(create)["content"]["id"]
    print(f">>> Workflow created: {wf_id} <<<\n")

    probe(
        "PUT", f"{BASE}/workflow/{wf_id}/updateTree",
        label="branch_bind_start", surface="public-api",
        json_body={"links": [], "elements": [{
            "action": "update", "elementID": 1,
            "data": {"element_id": 1, "resourceID": TEST_FORM_ID,
                     "resourceType": "FORM", "subType": "workflow_start_point_submission"},
        }]},
    )
    print("Step 1 done: start point bound to trigger form.\n")

    # --- Step 2: create the If/Else decision node ---
    decision = probe(
        "POST", f"{BASE}/workflow/{wf_id}/elements",
        label="branch_create_decision", surface="public-api",
        json_body={"type": "workflow_binary_decision"},
    )
    decision_id = parse(decision)["content"]["element_id"]
    print(f"Step 2 done: decision node created, element_id={decision_id}.\n")

    # Configure its condition + link it from the start point.
    probe(
        "PUT", f"{BASE}/workflow/{wf_id}/updateTree",
        label="branch_configure_decision_and_link_start", surface="public-api",
        json_body={
            "links": [{
                "action": "create", "linkID": 1,
                "data": {"link_id": 1, "fromElement": 1, "toElement": decision_id,
                         "fromPortName": "DYNAMIC_BOTTOM_1_Out", "toPortName": "DYNAMIC_TOP_1_In",
                         "type": "default-link", "labels": [], "points": [{"1": 2}]},
            }],
            "elements": [{
                "action": "update", "elementID": decision_id,
                "data": {
                    "element_id": decision_id,
                    "name": "If/Else Condition",
                    "conditionTermsMatchType": "All",
                    "conditionTerms": [{
                        "field": first_qid, "id": "term_demo_1", "operator": "equals",
                        "isError": False, "value": "test", "color": "#007862",
                    }],
                    "outcomes": [
                        {"id": 1, "outcomeID": 1, "type": "CONDITION", "conditionValue": "TRUE"},
                        {"id": 2, "outcomeID": 2, "type": "CONDITION", "conditionValue": "FALSE"},
                    ],
                },
            }],
        },
    )
    print("Step 3 done: condition configured, linked from start point.\n")

    # --- Step 3: TRUE branch -> send email ---
    email = probe(
        "POST", f"{BASE}/workflow/{wf_id}/elements",
        label="branch_create_email", surface="public-api",
        json_body={"type": "workflow_send_email"},
    )
    email_id = parse(email)["content"]["element_id"]

    recipient = {
        "id": "demo-recipient", "value": MY_EMAIL, "text": MY_EMAIL,
        "isValid": True, "isQuestion": False, "style": {}, "isBright": False,
        "formTitle": "Form",
    }
    probe(
        "POST", f"{BASE}/workflow/{wf_id}/elements/{email_id}",
        label="branch_configure_email", surface="public-api",
        json_body={
            "subject": "Condition was TRUE",
            "to": [recipient], "senderName": "Jotform", "senderEmail": "noreply@jotform.com",
        },
    )
    print(f"Step 4 done: email step created + configured, element_id={email_id}.\n")

    # --- Step 4: FALSE branch -> assign task ---
    task = probe(
        "POST", f"{BASE}/workflow/{wf_id}/elements",
        label="branch_create_task", surface="public-api",
        json_body={"type": "workflow_assign_task"},
    )
    task_id = parse(task)["content"]["element_id"]

    probe(
        "POST", f"{BASE}/workflow/{wf_id}/elements/{task_id}",
        label="branch_configure_task", surface="public-api",
        json_body={
            "assignee": [recipient],
            "outcomes": [{"outcomeID": 1, "type": "CUSTOM", "buttonColor": "#0075E3",
                          "text": "Complete", "textColor": "#FFFFFF"}],
        },
    )
    print(f"Step 5 done: task step created + configured, element_id={task_id}.\n")

    # --- Step 5: link both branches + tie outcomes to their links ---
    probe(
        "PUT", f"{BASE}/workflow/{wf_id}/updateTree",
        label="branch_link_both_outcomes", surface="public-api",
        json_body={
            "links": [
                {"action": "create", "linkID": 2,
                 "data": {"link_id": 2, "fromElement": decision_id, "toElement": email_id,
                          "fromPortName": "DYNAMIC_BOTTOM_1_Out", "toPortName": "DYNAMIC_TOP_1_In",
                          "type": "default-link", "labels": [], "points": [{"1": 2}]}},
                {"action": "create", "linkID": 3,
                 "data": {"link_id": 3, "fromElement": decision_id, "toElement": task_id,
                          "fromPortName": "DYNAMIC_TOP_1_Out", "toPortName": "DYNAMIC_TOP_1_In",
                          "type": "default-link", "labels": [], "points": [{"1": 2}]}},
            ],
            "elements": [{
                "action": "update", "elementID": decision_id,
                "data": {
                    "element_id": decision_id,
                    "outcomes": [
                        {"id": 1, "outcomeID": 1, "type": "CONDITION", "conditionValue": "TRUE", "linkID": 2},
                        {"id": 2, "outcomeID": 2, "type": "CONDITION", "conditionValue": "FALSE", "linkID": 3},
                    ],
                },
            }],
        },
    )
    print("Step 6 done: both branches linked, outcomes tied to their links.\n")

    # --- Step 6: publish ---
    probe("POST", f"{BASE}/workflow/{wf_id}/publish", label="branch_publish", surface="public-api")
    print("Step 7 done: published.\n")

    print(f"ALL DONE. Open workflow {wf_id} in the Jotform dashboard to see it visually.")


if __name__ == "__main__":
    main()
