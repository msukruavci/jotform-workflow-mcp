"""
Export the server's tool definitions in Anthropic API tool-use format.

Why generate rather than hand-write: the docstrings and Pydantic models
in mcp_server/ are the single source of truth for what each tool accepts.
A hand-maintained copy of the same schemas would drift the moment
someone edits a docstring, and a drifted tool definition is worse than
no definition — the model would be reasoning about a contract the server
no longer honours.

Re-run this whenever a tool's signature or docstring changes.

Usage:
    python agent/export_tool_schemas.py                # human-readable summary
    python agent/export_tool_schemas.py --json         # full JSON to stdout
    python agent/export_tool_schemas.py --out tools.json
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv()

from mcp_server.server import mcp  # noqa: E402


async def collect() -> list[dict]:
    tools = await mcp.list_tools()
    out = []
    for t in tools:
        # Anthropic's tool-use format: name, description, input_schema.
        # The MCP SDK already produced input_schema from the type hints,
        # so this is a rename, not a translation — nothing is invented here.
        out.append({
            "name": t.name,
            "description": (t.description or "").strip(),
            "input_schema": t.input_schema,
        })
    return out


def main() -> int:
    tools = asyncio.run(collect())

    if "--json" in sys.argv:
        print(json.dumps(tools, indent=2, ensure_ascii=False))
        return 0

    if "--out" in sys.argv:
        idx = sys.argv.index("--out")
        if idx + 1 >= len(sys.argv):
            print("--out needs a filename")
            return 1
        path = sys.argv[idx + 1]
        with open(path, "w") as f:
            json.dump(tools, f, indent=2, ensure_ascii=False)
        print(f"{len(tools)} tools written to {path}")
        return 0

    print(f"{len(tools)} tools\n")
    total_desc = 0
    for t in tools:
        params = list((t["input_schema"].get("properties") or {}).keys())
        required = t["input_schema"].get("required") or []
        total_desc += len(t["description"])
        print(f"{t['name']}")
        print(f"  description: {len(t['description'])} chars")
        print(f"  params: {', '.join(params) if params else '(none)'}")
        if required:
            print(f"  required: {', '.join(required)}")
        print()

    print(f"Total description budget: ~{total_desc} chars "
          f"(~{total_desc // 4} tokens), sent on every request.")
    print("\nRun with --json or --out <file> to get the actual definitions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
