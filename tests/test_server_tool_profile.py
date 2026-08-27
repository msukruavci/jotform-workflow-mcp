import asyncio

from mcp_server.server import mcp


def test_mcp_tool_profile_fast_filters_server_tool_list(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PROFILE", "fast")

    tools = asyncio.run(mcp.list_tools())
    tool_names = {tool.name for tool in tools}

    assert len(tools) == 14
    assert "build_workflow_bulk" in tool_names
    assert "update_step_settings" in tool_names
    assert "add_step" not in tool_names
    assert "connect_steps" not in tool_names
    assert "update_step" not in tool_names


def test_mcp_tool_profile_full_keeps_server_tool_list(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PROFILE", "full")

    tools = asyncio.run(mcp.list_tools())
    tool_names = {tool.name for tool in tools}

    assert len(tools) == 26
    assert "add_step" in tool_names
    assert "connect_steps" in tool_names
    assert "update_step" in tool_names
    assert "update_step_settings" in tool_names
