"""
Builds a polished, presentable workflow — same mechanics as
build_branching_workflow.py, but with genuinely well-written email
content instead of placeholder test text. Meant to be shown to the
mentor as a clean example, not just a technical proof.

  Start --> If/Else Condition --TRUE--> "Thank you" email (real HTML)
                              --FALSE--> Assign review task

Run: python -m probes.build_polished_demo_workflow
"""
import json
import os

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TEST_FORM_ID = os.environ.get("TEST_FORM_ID", "")
MY_EMAIL = os.environ.get("MY_EMAIL", "")

EMAIL_HTML = """
<div style="font-family: Arial, Helvetica, sans-serif; max-width: 560px; margin: 0 auto; padding: 32px 28px; background-color: #f7f8fc; border-radius: 12px;">
  <h2 style="color: #1a1a2e; margin: 0 0 16px 0;">Thank you for your submission!</h2>
  <p style="color: #444455; line-height: 1.6; margin: 0 0 14px 0;">
    We've received your form submission and our team is already on it.
    You can expect to hear back from us within 1-2 business days.
  </p>
  <p style="color: #444455; line-height: 1.6; margin: 0 0 14px 0;">
    If anything urgent comes up in the meantime, just reply directly to
    this email and a real person will see it.
  </p>
  <p style="color: #8888a0; font-size: 13px; margin-top: 28px;">
    &mdash; Sent automatically by your workflow
  </p>
</div>
""".strip()


def parse(record):
    try:
        return json.loads(record["response_snippet"])
    except (ValueError, KeyError):
        return None


def main() -> None:
    if not TEST_FORM_ID or not MY_EMAIL:
        print("Need TEST_FORM_ID and MY_EMAIL set in .env.")
        return

    questions = probe("GET", f"{BASE}/form/{TEST_FORM_ID}/questions",
                       label="polished_get_questions", surface="public-api")
    q_content = parse(questions).get("content", {})
    first_qid = list(q_content.keys())[0]

    create = probe(
        "POST", f"{BASE}/workflow",
        label="polished_create", surface="public-api",
        json_body={
            "title": "Submission Confirmation Workflow",
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
    print(f">>> Workflow created: {wf_id} <<<")

    probe("PUT", f"{BASE}/workflow/{wf_id}/updateTree",
          label="polished_bind_start", surface="public-api",
          json_body={"links": [], "elements": [{
              "action": "update", "elementID": 1,
              "data": {"element_id": 1, "resourceID": TEST_FORM_ID,
                       "resourceType": "FORM", "subType": "workflow_start_point_submission"},
          }]})
    print("Bound to trigger form.")

    decision = probe("POST", f"{BASE}/workflow/{wf_id}/elements",
                      label="polished_create_decision", surface="public-api",
                      json_body={"type": "workflow_binary_decision"})
    decision_id = parse(decision)["content"]["element_id"]

    probe("PUT", f"{BASE}/workflow/{wf_id}/updateTree",
          label="polished_configure_decision", surface="public-api",
          json_body={
              "links": [{"action": "create", "linkID": 1, "data": {
                  "link_id": 1, "fromElement": 1, "toElement": decision_id,
                  "fromPortName": "DYNAMIC_BOTTOM_1_Out", "toPortName": "DYNAMIC_TOP_1_In",
                  "type": "default-link", "labels": [], "points": [{"1": 2}]}}],
              "elements": [{"action": "update", "elementID": decision_id, "data": {
                  "element_id": decision_id,
                  "name": "If/Else Condition",
                  "conditionTermsMatchType": "All",
                  "conditionTerms": [{"field": first_qid, "id": "term_demo_1",
                                      "operator": "equals", "isError": False,
                                      "value": "test", "color": "#007862"}],
                  "outcomes": [
                      {"id": 1, "outcomeID": 1, "type": "CONDITION", "conditionValue": "TRUE"},
                      {"id": 2, "outcomeID": 2, "type": "CONDITION", "conditionValue": "FALSE"},
                  ],
              }}],
          })
    print(f"Decision node created + linked, element_id={decision_id}.")

    email = probe("POST", f"{BASE}/workflow/{wf_id}/elements",
                   label="polished_create_email", surface="public-api",
                   json_body={"type": "workflow_send_email"})
    email_id = parse(email)["content"]["element_id"]

    recipient = {
        "id": "demo-recipient", "value": MY_EMAIL, "text": MY_EMAIL,
        "isValid": True, "isQuestion": False, "style": {}, "isBright": False,
        "formTitle": "Form",
    }
    probe("POST", f"{BASE}/workflow/{wf_id}/elements/{email_id}",
          label="polished_configure_email", surface="public-api",
          json_body={
              "subject": "Thanks for your submission!",
              "content": EMAIL_HTML,
              "to": [recipient],
              "senderName": "Your Team",
              "senderEmail": "noreply@jotform.com",
              "replyTo": MY_EMAIL,
          })
    print(f"Email step created + configured with real content, element_id={email_id}.")

    task = probe("POST", f"{BASE}/workflow/{wf_id}/elements",
                 label="polished_create_task", surface="public-api",
                 json_body={"type": "workflow_assign_task"})
    task_id = parse(task)["content"]["element_id"]

    probe("POST", f"{BASE}/workflow/{wf_id}/elements/{task_id}",
          label="polished_configure_task", surface="public-api",
          json_body={
              "assignee": [recipient],
              "outcomes": [{"outcomeID": 1, "type": "CUSTOM", "buttonColor": "#0075E3",
                            "text": "Reviewed", "textColor": "#FFFFFF"}],
          })
    print(f"Task step created + configured, element_id={task_id}.")

    probe("PUT", f"{BASE}/workflow/{wf_id}/updateTree",
          label="polished_link_branches", surface="public-api",
          json_body={
              "links": [
                  {"action": "create", "linkID": 2, "data": {
                      "link_id": 2, "fromElement": decision_id, "toElement": email_id,
                      "fromPortName": "DYNAMIC_BOTTOM_1_Out", "toPortName": "DYNAMIC_TOP_1_In",
                      "type": "default-link", "labels": [], "points": [{"1": 2}]}},
                  {"action": "create", "linkID": 3, "data": {
                      "link_id": 3, "fromElement": decision_id, "toElement": task_id,
                      "fromPortName": "DYNAMIC_TOP_1_Out", "toPortName": "DYNAMIC_TOP_1_In",
                      "type": "default-link", "labels": [], "points": [{"1": 2}]}},
              ],
              "elements": [{"action": "update", "elementID": decision_id, "data": {
                  "element_id": decision_id,
                  "outcomes": [
                      {"id": 1, "outcomeID": 1, "type": "CONDITION", "conditionValue": "TRUE", "linkID": 2},
                      {"id": 2, "outcomeID": 2, "type": "CONDITION", "conditionValue": "FALSE", "linkID": 3},
                  ],
              }}],
          })
    print("Both branches linked.")

    probe("POST", f"{BASE}/workflow/{wf_id}/publish", label="polished_publish", surface="public-api")
    print("Published.\n")

    print(f"ALL DONE. Workflow {wf_id} ('Submission Confirmation Workflow') is live.")
    print("Open it in the Jotform dashboard — the email step now has real, readable content.")


if __name__ == "__main__":
    main()
