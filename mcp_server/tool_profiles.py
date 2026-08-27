"""Tool-surface profiles shared by the MCP server and direct agent runners."""
from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

DEFAULT_PROFILE = "full"

FAST_TOOL_NAMES = frozenset({
    "show_workflows",
    "show_workflow",
    "list_step_types",
    "get_step_schema",
    "list_workflows",
    "get_workflow",
    "inspect_workflow_gaps",
    "list_forms",
    "get_form_fields",
    "search_workflow_templates",
    "build_workflow_bulk",
    "update_step_settings",
    "publish_workflow",
    "delete_workflow",
})

READ_TOOL_NAMES = frozenset({
    "show_workflows",
    "show_workflow",
    "list_workflows",
    "get_workflow",
    "get_step_details",
    "inspect_workflow_gaps",
    "list_forms",
    "get_form_fields",
})

PROFILE_TOOL_NAMES = {
    "full": None,
    "fast": FAST_TOOL_NAMES,
    "read": READ_TOOL_NAMES,
}


def current_profile(env_var: str = "MCP_TOOL_PROFILE") -> str:
    return os.environ.get(env_var, DEFAULT_PROFILE).strip().lower() or DEFAULT_PROFILE


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name", ""))
    return str(getattr(tool, "name", ""))


def filter_tools(tools: Iterable[Any], profile: str | None = None) -> list[Any]:
    """Return the tools visible to the model for the selected profile."""
    selected_profile = (profile or current_profile()).strip().lower() or DEFAULT_PROFILE
    if selected_profile not in PROFILE_TOOL_NAMES:
        known = ", ".join(sorted(PROFILE_TOOL_NAMES))
        raise ValueError(f"Unknown tool profile {selected_profile!r}. Use one of: {known}.")

    tools = list(tools)
    allowed = PROFILE_TOOL_NAMES[selected_profile]
    if allowed is None:
        return tools

    return [tool for tool in tools if _tool_name(tool) in allowed]
