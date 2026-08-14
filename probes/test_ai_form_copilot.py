"""
Probe the workflow copilot endpoint that creates a form from an AI prompt.

This intentionally lives under probes/ until the exact request/response
contract is verified against the live API. By default it tries to delete any
created form it can identify from the response; pass --keep to inspect it in
the builder.

Usage:
    ./.venv/bin/python probes/test_ai_form_copilot.py
    ./.venv/bin/python probes/test_ai_form_copilot.py --keep --prompt "..."
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.environ.get("JOTFORM_WEB_API_BASE", "https://www.jotform.com/API")
TIMEOUT = 30


def request(
    method: str,
    path: str,
    *,
    base_url: str,
    api_key: str,
    json_body: dict | None = None,
) -> requests.Response:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        # Seen in the workflow copilot request notes. Values are intentionally
        # conservative and easy to spot in server-side logs if Jotform surfaces
        # them while we are still mapping the endpoint.
        "jf-v2-source": "mcp-probe",
        "jf-v2-target": "workflow-copilot",
    }
    response = requests.request(
        method,
        f"{base_url}{path}",
        params={"apiKey": api_key},
        headers=headers,
        json=json_body,
        timeout=TIMEOUT,
    )
    response.url = f"{base_url}{path}"
    return response


def find_form_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("formID", "formId", "form_id", "id"):
            item = value.get(key)
            if item is not None and str(item).isdigit():
                return str(item)
        for item in value.values():
            found = find_form_id(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_form_id(item)
            if found:
                return found
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prompt",
        default=(
            "Create a small temporary English contact form for an MCP probe. "
            "Fields: name, email, phone, message. Title: MCP AI Probe Form."
        ),
    )
    parser.add_argument("--form-type", default="classic")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    api_key = os.environ.get("JOTFORM_API_KEY")
    if not api_key:
        print("JOTFORM_API_KEY is not set")
        return 1

    body = {
        "prompt": args.prompt,
        "formType": args.form_type,
        "preferences": {"language": "en"},
    }

    base_url = args.base_url.rstrip("/")

    print(f"POST {base_url}/workflow/copilot/createWorkflowForm")
    try:
        response = request(
            "POST",
            "/workflow/copilot/createWorkflowForm",
            base_url=base_url,
            api_key=api_key,
            json_body=body,
        )
    except requests.RequestException as exc:
        print(f"request_error={type(exc).__name__}: {exc.__class__.__name__}")
        return 1
    print(f"status={response.status_code}")
    print(response.text[:4000])

    try:
        data = response.json()
    except ValueError:
        data = None

    form_id = find_form_id(data)
    if not form_id:
        print("No form id found in response.")
        return 0 if response.ok else 1

    print(f"form_id={form_id}")
    if args.keep:
        print("--keep passed; leaving the form in the account.")
        return 0 if response.ok else 1

    print(f"DELETE {base_url}/form/{form_id}")
    delete_response = request("DELETE", f"/form/{form_id}", base_url=base_url, api_key=api_key)
    print(f"delete_status={delete_response.status_code}")
    print(delete_response.text[:2000])
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
