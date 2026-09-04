import asyncio
import json
from pathlib import Path

from mcp_types.methods import serialize_server_result

from mcp_server.server import mcp


def test_checked_in_tool_schema_matches_live_surface():
    path = Path(__file__).resolve().parents[1] / "tool_schemas.json"
    checked_in = json.loads(path.read_text(encoding="utf-8"))
    tools = asyncio.run(mcp.list_tools())
    live = [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
        }
        for tool in tools
    ]

    assert checked_in == live, "Run .venv/bin/python scripts/export_tool_schemas.py"


def test_all_declared_output_schemas_are_mcp_objects():
    tools = asyncio.run(mcp.list_tools())

    for tool in tools:
        if tool.output_schema is not None:
            assert tool.output_schema.get("type") == "object", tool.name


def test_tool_list_serializes_for_current_mcp_protocol():
    tools = asyncio.run(mcp.list_tools())
    result = serialize_server_result(
        "tools/list",
        "2026-07-28",
        {
            "cacheScope": "private",
            "resultType": "complete",
            "ttlMs": 0,
            "tools": [tool.model_dump(by_alias=True) for tool in tools],
        },
    )

    assert len(result["tools"]) == 16
