"""
Shared harness for probing Jotform endpoints and logging what actually
happens. This is the source of truth for the gap report: every claim in
docs/gap-report.md about "X works" or "Y doesn't" should trace back to a
line in probes/findings/*.jsonl produced by this module.

Deliberately does NOT retry on 4xx/CSRF-style failures or spoof headers to
get around them — a failure is a finding, not a bug to route around.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

FINDINGS_DIR = Path(__file__).parent / "findings"
FINDINGS_DIR.mkdir(exist_ok=True)

API_KEY = os.environ.get("JOTFORM_API_KEY", "")


def probe(
    method: str,
    url: str,
    *,
    label: str,
    surface: str,  # "public-api" | "internal-bff"
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    include_api_key: bool = True,
    log_file: str = "gap_findings.jsonl",
) -> dict[str, Any]:
    """
    Make one HTTP call, log the outcome, return it.

    surface distinguishes the documented public API (api.jotform.com) from
    the internal frontend BFF (www.jotform.com/API) — keep these separate
    in every report table, their auth and support guarantees are different.
    """
    params = dict(params or {})
    if include_api_key and API_KEY:
        params["apiKey"] = API_KEY

    resp = requests.request(method, url, params=params, json=json_body, timeout=15)

    record = {
        "label": label,
        "surface": surface,
        "method": method.upper(),
        "url": url,
        "status": resp.status_code,
        "request_params_keys": list(params.keys()),  # keys only, never log the key value
        "request_body": json_body,
        "response_snippet": resp.text[:4000],
        "tested_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(FINDINGS_DIR / log_file, "a") as f:
        f.write(json.dumps(record) + "\n")

    print(f"[{surface}] {method.upper()} {label} -> {resp.status_code}")
    print(f"  {resp.text[:300]}")
    return record
