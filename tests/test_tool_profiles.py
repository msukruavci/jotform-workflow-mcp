from types import SimpleNamespace

import pytest

from agent.tool_profiles import FAST_TOOL_NAMES, filter_tools


def _tools(*names):
    return [SimpleNamespace(name=name) for name in names]


def test_full_profile_keeps_every_tool():
    tools = _tools("list_workflows", "add_step", "delete_workflow")

    assert [tool.name for tool in filter_tools(tools, profile="full")] == [
        "list_workflows",
        "add_step",
        "delete_workflow",
    ]


def test_fast_profile_hides_low_level_write_tools():
    tools = _tools("list_workflows", "build_workflow_bulk", "add_step", "connect_steps")

    filtered = filter_tools(tools, profile="fast")

    assert [tool.name for tool in filtered] == ["list_workflows", "build_workflow_bulk"]
    assert "add_step" not in FAST_TOOL_NAMES
    assert "connect_steps" not in FAST_TOOL_NAMES


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError, match="Unknown tool profile"):
        filter_tools(_tools("list_workflows"), profile="tiny")
