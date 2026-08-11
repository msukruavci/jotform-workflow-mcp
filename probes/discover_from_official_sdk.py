"""
Pulls the FULL list of officially documented public API endpoints straight
from Jotform's own official Python client source (github.com/jotform/
jotform-api-python), rather than guessing paths or scraping the docs page
by hand. This is the closest thing to an authoritative spec Jotform
publishes — they've confirmed in support threads they don't maintain an
OpenAPI/Swagger file.

Notably: as of the version fetched, this client has ZERO methods
referencing "workflow" anywhere. That's a real finding, not an omission
on our part — see docs/gap-report.md.

Run: python -m probes.discover_from_official_sdk
Writes: docs/public-api-surface.json
"""
import json
import re
from pathlib import Path

import requests

SOURCE_URL = "https://raw.githubusercontent.com/jotform/jotform-api-python/master/jotform.py"
OUTPUT_PATH = Path(__file__).parent.parent / "docs" / "public-api-surface.json"

SKIP_METHODS = {
    "fetch_url", "__init__", "_log", "set_baseurl", "get_debugMode",
    "set_debugMode", "get_outputType", "set_outputType",
    "create_conditions", "create_history_query",
}


def parse_client_source(src: str) -> list[dict]:
    lines = src.split("\n")
    blocks, current = [], []
    for line in lines:
        if re.match(r"^    def \w+\(", line) and current:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))

    results = []
    for block in blocks:
        name_match = re.match(r"\s*def (\w+)\(self[^)]*\):", block)
        if not name_match or name_match.group(1) in SKIP_METHODS:
            continue
        fname = name_match.group(1)

        call_match = re.search(r"fetch_url\((.+)\)\s*$", block, re.MULTILINE)
        if not call_match:
            continue
        call_args = call_match.group(1)

        depth, split_idx = 0, None
        for idx, ch in enumerate(call_args):
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                split_idx = idx
                break
        url_expr = call_args[:split_idx] if split_idx is not None else call_args

        url_template = re.sub(r"'\s*\+\s*(\w+)\s*\+\s*'", r"{\1}", url_expr)
        url_template = re.sub(r"'\s*\+\s*(\w+)\s*$", r"{\1}", url_template)
        url_template = url_template.strip().strip("'").strip()

        verb_match = re.search(r"'(GET|POST|PUT|DELETE)'", call_match.group(0))
        verb = verb_match.group(1) if verb_match else "GET"

        results.append({
            "client_method": fname,
            "http_method": verb,
            "path_template": "/" + url_template.lstrip("/"),
            "mutating": verb != "GET",
        })
    return results


def main() -> None:
    resp = requests.get(SOURCE_URL, timeout=15)
    resp.raise_for_status()
    endpoints = parse_client_source(resp.text)

    workflow_related = [e for e in endpoints if "workflow" in e["path_template"].lower()]

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump({
            "source": SOURCE_URL,
            "endpoint_count": len(endpoints),
            "workflow_endpoint_count": len(workflow_related),
            "endpoints": endpoints,
        }, f, indent=2)

    print(f"{len(endpoints)} documented endpoints extracted -> {OUTPUT_PATH}")
    print(f"Of those, {len(workflow_related)} mention 'workflow'.")
    if not workflow_related:
        print(
            "Confirms: workflow authoring/reading has no presence in "
            "Jotform's own official SDK. Everything we've found about "
            "workflow endpoints was discovered empirically (DevTools/HAR), "
            "not from a sanctioned source — treat it accordingly in the "
            "gap report."
        )


if __name__ == "__main__":
    main()
