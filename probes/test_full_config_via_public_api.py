"""
Tests whether the rich per-element config fields we learned from
DevTools (subject/to/content/etc. for an email step) can be written via
api.jotform.com, using two different hypotheses:

1. Does api.jotform.com mirror www.jotform.com/API's updateTree endpoint?
2. Does the already-confirmed-working POST .../elements accept these
   extra fields directly (flat), beyond just "type"?

Uses your own email as the recipient (from .env) — a real, safe value,
not a guess. Targets the element we already created in an earlier probe
(element_id 5, the floating unconfigured "Email" node) rather than
creating yet another node.

Run: python -m probes.test_full_config_via_public_api
"""
import os

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TEST_WORKFLOW_ID = os.environ.get("TEST_WORKFLOW_ID", "")
MY_EMAIL = os.environ.get("MY_EMAIL", "")  # add this to .env: your own email
EXISTING_EMAIL_ELEMENT_ID = 5  # from the earlier discover_element_schema.py run


def main() -> None:
    if not TEST_WORKFLOW_ID:
        print("Set TEST_WORKFLOW_ID in .env first.")
        return
    if not MY_EMAIL:
        print("Add MY_EMAIL=your@email.com to .env first (used as a safe, real recipient).")
        return

    recipient = {
        "id": "test-recipient-1",
        "value": MY_EMAIL,
        "text": MY_EMAIL,
        "isValid": True,
        "isQuestion": False,
        "style": {},
        "isBright": False,
        "formTitle": "Form",
    }

    # Hypothesis 1: api.jotform.com mirrors updateTree
    probe(
        "PUT",
        f"{BASE}/workflow/{TEST_WORKFLOW_ID}/updateTree",
        label="updateTree_via_public_api",
        surface="public-api",
        json_body={
            "links": [],
            "elements": [{
                "action": "update",
                "elementID": EXISTING_EMAIL_ELEMENT_ID,
                "data": {
                    "element_id": EXISTING_EMAIL_ELEMENT_ID,
                    "subject": "Test subject via public API",
                    "to": [recipient],
                    "senderName": "Jotform",
                    "senderEmail": "noreply@jotform.com",
                },
            }],
        },
    )

    # Hypothesis 2: the working POST .../elements accepts rich fields flat,
    # targeting the SAME element by including its id in the body.
    probe(
        "POST",
        f"{BASE}/workflow/{TEST_WORKFLOW_ID}/elements",
        label="elements_rich_config_with_id_in_body",
        surface="public-api",
        json_body={
            "element_id": EXISTING_EMAIL_ELEMENT_ID,
            "type": "workflow_send_email",
            "subject": "Test subject via public API",
            "to": [recipient],
            "senderName": "Jotform",
            "senderEmail": "noreply@jotform.com",
        },
    )

    # Hypothesis 3: maybe there's an update-by-id path variant.
    probe(
        "POST",
        f"{BASE}/workflow/{TEST_WORKFLOW_ID}/elements/{EXISTING_EMAIL_ELEMENT_ID}",
        label="elements_update_by_id_path",
        surface="public-api",
        json_body={
            "subject": "Test subject via public API",
            "to": [recipient],
        },
    )


if __name__ == "__main__":
    main()
