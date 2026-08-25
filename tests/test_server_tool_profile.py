import asyncio

from mcp_server import server
from mcp_server.server import mcp


def test_mcp_uses_single_tool_surface_even_when_profile_env_is_set(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PROFILE", "fast")

    tools = asyncio.run(mcp.list_tools())
    tool_names = {tool.name for tool in tools}

    assert len(tools) == 16
    assert "build_workflow_bulk" in tool_names
    assert "create_form_with_ai" in tool_names
    assert "search_workflow_templates" in tool_names
    assert "get_workflow_template" not in tool_names
    assert "get_step_details" in tool_names
    assert "delete_step" in tool_names
    assert "restore_workflow_revision" in tool_names
    assert "get_form_fields" not in tool_names
    assert "create_workflow" not in tool_names
    assert "create_workflow_with_ai_form" not in tool_names
    assert "inspect_workflow_gaps" not in tool_names
    assert "add_step" not in tool_names
    assert "connect_steps" not in tool_names
    assert "disconnect_steps" not in tool_names
    assert "update_step" not in tool_names


def test_server_instructions_describe_single_surface_without_get_form_fields(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PROFILE", "fast")

    instructions = server.build_server_instructions()

    assert "Tool profile:" not in instructions
    assert "automatically call search_workflow_templates first" in instructions
    assert "default 2, max 3" in instructions
    assert "do not call inspect_workflow_gaps" in instructions
    assert "Do not use or suggest any separate Jotform Form plugin/tool" in instructions
    assert "Standalone workflow creation tools and low-level updateTree tools" in instructions
    assert "add_step/connect_steps/disconnect_steps/update_step are intentionally hidden" in instructions
    assert "trigger_form_fields" in instructions
    assert "get_form_fields" not in instructions
    assert "Only use create_form_with_ai when the user asks for a standalone form" in instructions


def test_build_workflow_bulk_schema_discourages_external_form_plugin(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PROFILE", "fast")

    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    build_tool = tools["build_workflow_bulk"]

    assert "Do not create the trigger form with any separate" in build_tool.description
    schema = build_tool.input_schema
    form_prompt = schema["properties"]["form_prompt"]["description"]
    trigger_form_id = schema["properties"]["trigger_form_id"]["description"]
    assert "instead of any separate Jotform Form plugin/tool" in form_prompt
    assert "otherwise use form_prompt" in trigger_form_id
