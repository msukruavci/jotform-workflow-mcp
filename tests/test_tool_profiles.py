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
        "delete_workflow",
    ]


def test_fast_tools_exposes_decoupled_workflow_sequence_as_first_class_tools():
    assert {
        "create_form_with_ai",
        "build_workflow_bulk",
        "apply_workflow_canvas_diff",
        "show_workflow",
    } <= FAST_TOOLS


def test_profile_arguments_and_env_do_not_change_the_single_surface(monkeypatch):
    monkeypatch.setenv("MCP_TOOL_PROFILE", "fast")
    tools = _tools("list_workflows", "get_form_fields", "build_workflow_bulk")

    assert current_profile() == "default"
    assert [tool.name for tool in filter_tools(tools, profile="full")] == [
        "list_workflows",
        "build_workflow_bulk",
    ]


def test_templates_stay_enabled_and_gap_check_stays_disabled():
    assert feature_enabled("templates") is True
    assert feature_enabled("gap_check") is False
