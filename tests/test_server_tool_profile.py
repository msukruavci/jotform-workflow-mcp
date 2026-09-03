import asyncio

from mcp_server import server
from mcp_server.server import mcp


def test_mcp_uses_single_tool_surface_even_when_profile_env_is_set(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PROFILE", "fast")

    tools = asyncio.run(mcp.list_tools())
    tool_names = {tool.name for tool in tools}

    assert len(tools) == 16
    assert "build_workflow_bulk" in tool_names
    assert "apply_workflow_canvas_diff" not in tool_names
    assert "create_form_with_ai" in tool_names
    assert "search_workflow_templates" in tool_names
    assert "get_workflow_template" not in tool_names
    assert "get_step_details" in tool_names
    assert "delete_step" not in tool_names
    assert "restore_workflow_revision" in tool_names
    assert "record_feature_request" in tool_names
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
    assert "Always call search_workflow_templates first" in instructions
    assert "top_k=1" in instructions
    assert "top_k=2 only if ambiguous" in instructions
    assert "get_form_fields" not in instructions
    assert "For form-submission workflows, call create_form_with_ai" in instructions
    assert 'trigger_type="schedule"' in instructions
    assert "Call build_workflow_bulk for one complete successful write" in instructions
    assert "Call show_workflow once" in instructions
    assert "build_workflow_bulk never creates a form" in instructions
    assert "get_workflow -> build_workflow_bulk -> show_workflow" in instructions
    assert "step_updates" in instructions
    assert "workflow_assign_form step with formID" in instructions
    assert 'type="workflow_integration"' in instructions
    assert "blank shell step" in instructions
    assert "+ Complete Settings" in instructions
    assert "Every build_workflow_bulk write leaves the workflow DISABLED" in instructions
    assert "Do not call publish_workflow as a post-build status check" in instructions
    assert "Do not answer the user" in instructions
    assert "assigned_forms[].form_url" in instructions
    assert "The iframe is permanently read-only" in instructions


def test_tool_schemas_expose_decoupled_form_then_workflow_contract(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PROFILE", "fast")

    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    create_tool = tools["create_form_with_ai"]
    build_tool = tools["build_workflow_bulk"]
    show_tool = tools["show_workflow"]
    publish_tool = tools["publish_workflow"]
    feature_tool = tools["record_feature_request"]

    assert "Call this only after search_workflow_templates" in create_tool.description
    assert "first write" in create_tool.description
    assert "not" in create_tool.description and "first tool call" in create_tool.description
    assert "workflow_assign_form.formID" in create_tool.description
    assert "exact field_id, name, label, type" in create_tool.description
    assert "search_workflow_templates -> create_form_with_ai -> build_workflow_bulk -> show_workflow" in build_tool.description
    assert 'trigger_type="schedule"' in build_tool.description
    assert "workflow_assign_form" in build_tool.description
    assert "formID" in build_tool.description
    assert "workflow_integration" in build_tool.description
    assert "blank shell step" in build_tool.description
    assert "assigned_forms" in build_tool.output_schema["properties"]
    assert "Call immediately after build_workflow_bulk" in show_tool.description
    assert publish_tool.input_schema["properties"]["confirm"]["default"] is False
    assert "only call even the preview after the user explicitly asks" in publish_tool.input_schema["properties"]["confirm"]["description"]
    assert "Do not use this as the normal final step" in publish_tool.description
    assert "explicitly confirms" in publish_tool.input_schema["properties"]["confirm"]["description"]
    assert "allow_draft_recipients" in publish_tool.input_schema["properties"]
    assert "explicitly accepts enabling" in publish_tool.input_schema["properties"]["allow_draft_recipients"]["description"]
    assert "Call only after `show_workflow`" in feature_tool.description
    assert "missing_template" in feature_tool.input_schema["properties"]["category"]["description"]

    output_schema = create_tool.output_schema
    assert {"form_id", "form_url", "title", "summary", "fields", "next_required_tool", "hint"} <= set(output_schema["properties"])
    assert "continue with build_workflow_bulk" in output_schema["properties"]["summary"]["description"]
    form_field_schema = output_schema["$defs"]["FormField"]["properties"]
    assert {"field_id", "name", "label", "type", "required", "options"} == set(form_field_schema)

    schema = build_tool.input_schema
    trigger_form_id = schema["properties"]["trigger_form_id"]["description"]
    assert "form_prompt" not in schema["properties"]
    assert "form_language" not in schema["properties"]
    assert "form_id obtained from this MCP server's create_form_with_ai" in trigger_form_id

    assert "step_updates" in schema["properties"]
    step_spec = schema["$defs"]["StepSpec"]["properties"]
    assert "subType" in step_spec
    assert "slack" in step_spec["subType"]["anyOf"][0]["enum"]
    assert "whatsapp-business" in step_spec["subType"]["anyOf"][0]["enum"]
    assert "do NOT fill any" in step_spec["subType"]["description"]
    assert "trigger_form_fields" not in build_tool.output_schema["properties"]
    assert "apply_workflow_canvas_diff" not in tools
