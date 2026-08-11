"""
Simple: read element 2's type, send an empty updateTree, read the type
again. If it changes, we found the cause of the mystery.

Run: python -m probes.test_noop_updatetree_effect
"""
import json
import os

from probes.client import probe

BASE = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
WORKFLOW_ID = os.environ.get("SCRATCH_WORKFLOW_ID", "262183096859975")


def get_type():
    r = probe("GET", f"{BASE}/workflow/{WORKFLOW_ID}/elements/2",
              label="check_type", surface="public-api")
    return json.loads(r["response_snippet"])["content"].get("type")


def main():
    print("Type before:", get_type())
    probe("PUT", f"{BASE}/workflow/{WORKFLOW_ID}/updateTree",
          label="noop_updatetree", surface="public-api",
          json_body={"elements": [], "links": []})
    print("Type after empty updateTree:", get_type())


if __name__ == "__main__":
    main()
