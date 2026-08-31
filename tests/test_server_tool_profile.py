import asyncio

from mcp_server import server
from mcp_server.server import mcp


def test_mcp_uses_single_tool_surface_even_when_profile_env_is_set(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PROFILE", "fast")

    tools = asyncio.run(mcp.list_tools())
    tool_names = {tool.name for tool in tools}

    assert len(tools) == 17
    assert "build_workflow_bulk" in tool_names
    assert "apply_workflow_canvas_diff" in tool_names
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


def test_server_instructions_describe_decoupled_three_tool_sequence(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PROFILE", "fast")

    instructions = server.build_server_instructions()

    assert "Tool profile:" not in instructions
    assert "automatically call search_workflow_templates first" in instructions
    assert "top_k=1 for simple/narrow requests" in instructions
    assert "top_k=2 only when broad or ambiguous" in instructions
    assert "do not call inspect_workflow_gaps" in instructions
    assert "Do not use or suggest any separate Jotform Form plugin/tool" in instructions
    assert "Standalone workflow creation tools and low-level updateTree tools" in instructions
    assert "add_step/connect_steps/disconnect_steps/update_step are intentionally hidden" in instructions
    assert "One-write rule" in instructions
    assert "get_form_fields" not in instructions
    assert "first call create_form_with_ai(prompt=...)" in instructions
    assert "Then call build_workflow_bulk(trigger_form_id=..., steps=..., connections=...)" in instructions
    assert "Finally call show_workflow(workflow_id)" in instructions
    assert "form_prompt on build_workflow_bulk remains a backward-compatible fallback only" in instructions
    assert "Only use create_form_with_ai when the user asks for a standalone form" not in instructions
    assert "Prefer keeping AI form creation, workflow creation" not in instructions


def test_tool_schemas_expose_decoupled_form_then_workflow_contract(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PROFILE", "fast")

    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    create_tool = tools["create_form_with_ai"]
    build_tool = tools["build_workflow_bulk"]
    show_tool = tools["show_workflow"]
    canvas_diff_tool = tools["apply_workflow_canvas_diff"]

    assert "Call this first when building a new workflow" in create_tool.description
    assert "exact field_id, label, type" in create_tool.description
    assert "Pass the trigger_form_id obtained from create_form_with_ai" in build_tool.description
    assert "Call immediately after build_workflow_bulk" in show_tool.description

    output_schema = create_tool.output_schema
    assert {"form_id", "form_url", "title", "summary", "fields"} <= set(output_schema["properties"])
    form_field_schema = output_schema["$defs"]["FormField"]["properties"]
    assert {"field_id", "label", "type", "required", "options"} == set(form_field_schema)

    schema = build_tool.input_schema
    form_prompt = schema["properties"]["form_prompt"]["description"]
    trigger_form_id = schema["properties"]["trigger_form_id"]["description"]
    assert "backward-compatible fallback" in form_prompt
    assert "If both are supplied, trigger_form_id takes precedence" in form_prompt
    assert "form_id obtained from create_form_with_ai" in trigger_form_id

    diff_schema = canvas_diff_tool.input_schema["$defs"]["WorkflowCanvasDiff"]["properties"]
    assert {
        "added_steps",
        "deleted_step_ids",
        "updated_connections",
        "base_revision_id",
        "base_updated_at",
    } == set(diff_schema)
