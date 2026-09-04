from types import SimpleNamespace

from agent.tool_profiles import filter_tools
from mcp_server.tool_profiles import FAST_TOOLS, current_profile, feature_enabled


def _tools(*names):
    return [SimpleNamespace(name=name) for name in names]


def test_single_tool_surface_hides_deprecated_and_field_lookup_tools():
    tools = _tools(
        "list_workflows",
        "get_workflow",
        "get_form_fields",
        "build_workflow_bulk",
        "create_form_with_ai",
        "record_feature_request",
        "create_workflow",
        "create_workflow_with_ai_form",
        "add_step",
        "connect_steps",
        "disconnect_steps",
        "update_step",
        "inspect_workflow_gaps",
        "delete_step",
        "delete_workflow",
    )

    assert [tool.name for tool in filter_tools(tools)] == [
        "list_workflows",
        "get_workflow",
        "build_workflow_bulk",
        "create_form_with_ai",
        "record_feature_request",
        "delete_workflow",
    ]


def test_fast_tools_exposes_decoupled_workflow_sequence_as_first_class_tools():
    assert {
        "create_form_with_ai",
        "build_workflow_bulk",
        "show_workflow",
        "record_feature_request",
    } <= FAST_TOOLS


def test_profile_arguments_and_env_do_not_change_the_single_surface(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PROFILE", "fast")
    tools = _tools("list_workflows", "get_form_fields", "build_workflow_bulk")

    assert current_profile() == "default"
    assert [tool.name for tool in filter_tools(tools, profile="full")] == [
        "list_workflows",
        "build_workflow_bulk",
    ]


def test_embedded_editor_tools_stay_hidden_but_are_callable():
    from mcp_server.audit_log import _tool_call_allowed

    tools = _tools("get_form_fields", "update_step")

    assert filter_tools(tools) == []
    assert _tool_call_allowed("get_form_fields") is True
    assert _tool_call_allowed("update_step") is True


def test_templates_stay_enabled_and_gap_check_stays_disabled():
    assert feature_enabled("templates") is True
    assert feature_enabled("gap_check") is False
