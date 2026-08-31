"""Single model-facing tool surface shared by the MCP server and agent runners."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

DEFAULT_PROFILE = "default"

# The intentionally small, first-class surface exposed to models. Keep this
# allowlist explicit so newly registered low-level helpers do not become model
# facing by accident. create_form_with_ai is part of the normal workflow build
# path, not a hidden compatibility helper.
FAST_TOOLS = frozenset({
    "create_form_with_ai",
    "build_workflow_bulk",
    "apply_workflow_canvas_diff",
    "show_workflow",
    "show_workflows",
    "list_step_types",
    "get_step_schema",
    "list_workflows",
    "get_workflow",
    "get_step_details",
    "list_workflow_revisions",
    "list_forms",
    "search_workflow_templates",
    "delete_step",
    "publish_workflow",
    "restore_workflow_revision",
    "delete_workflow",
})

DEPRECATED_TOOL_NAMES = frozenset({
    "add_step",
    "connect_steps",
    "create_workflow",
    "create_workflow_with_ai_form",
    "disconnect_steps",
    "get_form_fields",
    "inspect_workflow_gaps",
    "update_step",
})

TOOL_FEATURES = {"templates": True, "gap_check": False}


def current_profile(env_var: str = "MCP_TOOL_PROFILE") -> str:
    return DEFAULT_PROFILE


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name", ""))
    return str(getattr(tool, "name", ""))


def filter_tools(tools: Iterable[Any], profile: str | None = None) -> list[Any]:
    """Return the single tool surface visible to the model."""
    return [
        tool for tool in tools
        if _tool_name(tool) in FAST_TOOLS
        and _tool_name(tool) not in DEPRECATED_TOOL_NAMES
    ]


def feature_enabled(feature: str, profile: str | None = None) -> bool:
    return bool(TOOL_FEATURES.get(feature))
