"""
End-to-end: build a brand-new Jotform workflow from nothing, purely via
api.jotform.com, one confirmed step at a time.

Everything in this project so far operated on a workflow that already
existed (262164010869961). This is the first test of the base
POST /workflow (no id) endpoint via the public surface — the one piece
we never tried. If step 1 fails, nothing after it can run; the script
stops there rather than guessing forward.

Run: python -m probes.build_workflow_from_scratch
"""
import json
import os

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TEST_FORM_ID = os.environ.get("TEST_FORM_ID", "")
MY_EMAIL = os.environ.get("MY_EMAIL", "")


def parse(record: dict) -> dict | None:
    try:
        return json.loads(record["response_snippet"])
    except (ValueError, KeyError):
        return None


def main() -> None:
    if not TEST_FORM_ID or not MY_EMAIL:
        print("Need TEST_FORM_ID and MY_EMAIL set in .env first.")
        return

    # --- Step 1: create a brand-new workflow from scratch ---
    # Payload shape mirrors EXACTLY what the real UI sent when creating a
    # workflow (captured via HAR on www.jotform.com/API/workflow) — reusing
    # a known-good shape rather than guessing a simpler one, to maximize
    # the chance this untested endpoint accepts it on the first try.
    create = probe(
        "POST", f"{BASE}/workflow",
        label="create_workflow_from_scratch",
        surface="public-api",
        json_body={
            "title": "MCP Test Workflow",
            "triggerOnEdit": "ENABLED",
            "elements": [
                {"data": {"element_id": 1, "className": ["isStartPoint"],
                          "elementType": "workflow_start_point", "type": "workflow_start_point",
                          "id": 1, "position": {"x": 0, "y": 0},
                          "measured": {"width": 296, "height": 88}, "x": 0, "y": 0},
                 "elementID": 1, "action": "update"},
                {"data": {"element_id": 2, "name": "Empty Element", "Icon": None, "className": None,
                          "elementType": "workflow_placeholder", "type": "workflow_placeholder",
                          "id": 2, "position": {"x": 0, "y": 180},
                          "measured": {"width": 296, "height": 88}, "x": 0, "y": 180},
                 "elementID": 2, "action": "create"},
            ],
            "links": [
                {"data": {"id": 1, "fromElement": "1", "toElement": "2",
                          "fromPortName": "DYNAMIC_BOTTOM_1_Out", "toPortName": "DYNAMIC_TOP_1_In",
                          "type": "default-link", "labels": [], "link_id": 1, "points": [{"1": 2}]},
                 "linkID": 1, "action": "create"},
            ],
        },
    )
    if create["status"] != 200:
        print("\nCreation failed — stopping here, nothing downstream can run.")
        print("This itself is a real finding: log it in gap-report.md as-is.")
        return

    body = parse(create)
    new_id = body["content"]["id"]
    print(f"\n>>> New workflow created: {new_id} <<<\n")

    # --- Step 2: bind the trigger form to the start point ---
    probe(
        "PUT", f"{BASE}/workflow/{new_id}/updateTree",
        label="bind_start_point", surface="public-api",
        json_body={"links": [], "elements": [{
            "action": "update", "elementID": 1,
            "data": {"element_id": 1, "resourceID": TEST_FORM_ID,
                     "resourceType": "FORM", "subType": "workflow_start_point_submission"},
        }]},
    )

    # --- Step 3: create a real email step ---
    email_create = probe(
        "POST", f"{BASE}/workflow/{new_id}/elements",
        label="create_email_step", surface="public-api",
        json_body={"type": "workflow_send_email"},
    )
    email_body = parse(email_create)
    if not email_body or email_create["status"] != 200:
        print("Email step creation failed — stopping before configure/link steps.")
        return
    email_id = email_body["content"]["element_id"]
    print(f">>> Email step created: element_id {email_id} <<<")

    # --- Step 4: configure the email step ---
    recipient = {
        "id": "test-recipient-1", "value": MY_EMAIL, "text": MY_EMAIL,
        "isValid": True, "isQuestion": False, "style": {}, "isBright": False,
        "formTitle": "Form",
    }
    probe(
        "POST", f"{BASE}/workflow/{new_id}/elements/{email_id}",
        label="configure_email_step", surface="public-api",
        json_body={
            "subject": "Hello from the MCP project",
            "to": [recipient],
            "senderName": "Jotform",
            "senderEmail": "noreply@jotform.com",
        },
    )

    # --- Step 5: link start point -> email step ---
    probe(
        "PUT", f"{BASE}/workflow/{new_id}/updateTree",
        label="link_start_to_email", surface="public-api",
        json_body={"elements": [], "links": [{
            "action": "create", "linkID": 100,
            "data": {"link_id": 100, "fromElement": 1, "toElement": email_id,
                     "fromPortName": "DYNAMIC_BOTTOM_1_Out", "toPortName": "DYNAMIC_TOP_1_In",
                     "type": "default-link", "labels": [], "points": [{"1": 2}]},
        }]},
    )

    # --- Step 6: publish ---
    probe("POST", f"{BASE}/workflow/{new_id}/publish", label="publish_new_workflow", surface="public-api")

    # --- Step 7: read everything back to verify ---
    probe("GET", f"{BASE}/workflow/{new_id}", label="verify_metadata", surface="public-api")
    probe("GET", f"{BASE}/workflow/{new_id}/elements", label="verify_elements", surface="public-api")
    probe("GET", f"{BASE}/workflow/{new_id}/links", label="verify_links", surface="public-api")

    print(f"\nDone. Open the Jotform dashboard and look for workflow {new_id} "
          f"('MCP Test Workflow') to see it visually — that's the real proof, "
          f"not just the JSON responses.")


if __name__ == "__main__":
    main()
