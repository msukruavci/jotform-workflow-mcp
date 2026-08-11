"""
Fresh, single-purpose workflow — created purely to (a) give us a clean
reference separate from the cluttered original (262164010869961), and
(b) test the element-type-persistence mystery in a tightly controlled
way: create -> read immediately -> configure -> read immediately again,
seconds apart, not days. If the type flips even here, the cause is in
the create/configure calls themselves, not something that happens later.

Run: python -m probes.fresh_workflow_and_type_test
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

    # Step 1: fresh workflow, minimal — just a start point, no placeholder,
    # no extra links, to keep this one clean from the start.
    create = probe(
        "POST", f"{BASE}/workflow",
        label="fresh_create", surface="public-api",
        json_body={
            "title": "Type Persistence Test Workflow",
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
    if create["status"] != 200:
        print("Creation failed, stopping.")
        return
    wf_id = parse(create)["content"]["id"]
    print(f"\n>>> Fresh workflow created: {wf_id} <<<")
    print(">>> Save this as SCRATCH_WORKFLOW_ID in .env if you want to keep using it <<<\n")

    # Step 2: bind trigger form
    probe(
        "PUT", f"{BASE}/workflow/{wf_id}/updateTree",
        label="fresh_bind_start", surface="public-api",
        json_body={"links": [], "elements": [{
            "action": "update", "elementID": 1,
            "data": {"element_id": 1, "resourceID": TEST_FORM_ID,
                     "resourceType": "FORM", "subType": "workflow_start_point_submission"},
        }]},
    )

    # Step 3: create ONE email element
    email_create = probe(
        "POST", f"{BASE}/workflow/{wf_id}/elements",
        label="fresh_create_email", surface="public-api",
        json_body={"type": "workflow_send_email"},
    )
    email_body = parse(email_create)
    email_id = email_body["content"]["element_id"]
    print(f"Created element {email_id}.")

    # Step 4: read it back IMMEDIATELY, before touching it further.
    check1 = probe(
        "GET", f"{BASE}/workflow/{wf_id}/elements/{email_id}",
        label="fresh_type_check_1_right_after_create", surface="public-api",
    )
    type1 = parse(check1)["content"].get("type")
    print(f"Type immediately after creation: {type1!r}")

    # Step 5: configure it
    recipient = {
        "id": "test-recipient-1", "value": MY_EMAIL, "text": MY_EMAIL,
        "isValid": True, "isQuestion": False, "style": {}, "isBright": False,
        "formTitle": "Form",
    }
    probe(
        "POST", f"{BASE}/workflow/{wf_id}/elements/{email_id}",
        label="fresh_configure_email", surface="public-api",
        json_body={
            "subject": "Type persistence test",
            "to": [recipient],
            "senderName": "Jotform",
            "senderEmail": "noreply@jotform.com",
        },
    )

    # Step 6: read it back IMMEDIATELY again.
    check2 = probe(
        "GET", f"{BASE}/workflow/{wf_id}/elements/{email_id}",
        label="fresh_type_check_2_right_after_configure", surface="public-api",
    )
    type2 = parse(check2)["content"].get("type")
    print(f"Type immediately after configuring: {type2!r}")

    print("\n=== RESULT ===")
    if type1 == "workflow_send_email" and type2 == "workflow_send_email":
        print("Type stayed correct both times — the mystery on the OLD workflow's "
              "element 5 was likely caused by something else (a later operation), "
              "not by create/configure themselves.")
    elif type1 != "workflow_send_email":
        print(f"Type was ALREADY wrong right after creation ({type1!r}) — the "
              "problem happens at creation time, not later.")
    else:
        print(f"Type was correct after creation but flipped to {type2!r} right "
              "after configuring — the configure call itself is the cause.")


if __name__ == "__main__":
    main()
