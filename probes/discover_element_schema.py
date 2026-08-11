"""
Iteratively discovers what fields POST /workflow/{id}/elements requires,
by reading its "Missing parameters: X" error messages one at a time and
adding only fields we already have a REAL, known-good value for (from
data we already read via GET .../elements). The moment we hit a field we
don't have a trustworthy value for, we STOP and print what's needed —
we do not invent placeholder/guessed values, because a guess that happens
to be accepted could create a real, malformed node in your workflow.

This is a read-mostly reconnaissance script. It will only ever succeed
(actually create something) if every required field happens to be one we
already knew from reading the workflow — in which case what it creates is
a close duplicate of an existing node, not garbage.

Run: python -m probes.discover_element_schema
"""
import json
import os

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TEST_WORKFLOW_ID = os.environ.get("TEST_WORKFLOW_ID", "")
TEST_FORM_ID = os.environ.get("TEST_FORM_ID", "")

# Only fields we're confident about, from values already seen in GET
# .../elements responses. Extend this dict only when you've actually seen
# the real value somewhere — never with a guess.
KNOWN_GOOD_VALUES = {
    "type": "workflow_send_email",
    "resourceType": "FORM",
    "resourceID": TEST_FORM_ID,
}

MAX_ITERATIONS = 6


def main() -> None:
    if not TEST_WORKFLOW_ID:
        print("Set TEST_WORKFLOW_ID in .env first.")
        return

    payload: dict = {}
    url = f"{BASE}/workflow/{TEST_WORKFLOW_ID}/elements"

    for i in range(1, MAX_ITERATIONS + 1):
        record = probe(
            "POST", url,
            label=f"discover_schema_attempt_{i}",
            surface="public-api",
            json_body=payload,
        )

        try:
            body = json.loads(record["response_snippet"])
        except (ValueError, KeyError):
            print("Couldn't parse response as JSON, stopping. Read the raw log.")
            return

        if record["status"] in (200, 201):
            print(f"\nSUCCESS on attempt {i} with payload: {payload}")
            print("A new element may now exist in your real workflow — check the UI.")
            return

        message = body.get("message", "")
        print(f"  -> {message}")

        if message.startswith("Missing parameters:"):
            missing_field = message.split(":", 1)[1].strip().split(",")[0].strip()
            if missing_field in payload:
                print(f"Already sent '{missing_field}' but still asked for it — stopping to avoid a loop.")
                return
            if missing_field in KNOWN_GOOD_VALUES:
                payload[missing_field] = KNOWN_GOOD_VALUES[missing_field]
                print(f"  Adding known value for '{missing_field}', retrying...")
                continue
            else:
                print(
                    f"\nSTOPPING: need a value for '{missing_field}' and don't have "
                    f"a trustworthy one. Payload so far: {payload}\n"
                    f"Look at workflow_elements_full.json for a real example of this "
                    f"field's value, add it to KNOWN_GOOD_VALUES, and re-run — or "
                    f"test it deliberately by hand via Bruno instead of guessing here."
                )
                return
        else:
            print(f"\nSTOPPING: got a different kind of error, not a missing-field "
                  f"one. Payload so far: {payload}. Full message: {message}")
            return

    print(f"\nHit MAX_ITERATIONS ({MAX_ITERATIONS}) without resolving. Payload so far: {payload}")


if __name__ == "__main__":
    main()
