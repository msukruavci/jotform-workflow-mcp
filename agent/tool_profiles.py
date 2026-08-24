"""Tool-surface profiles for direct agent runners."""
from __future__ import annotations

from typing import Any

from mcp_server.tool_profiles import (  # noqa: F401
    DEFAULT_PROFILE,
    FAST_TOOL_NAMES,
    PROFILE_TOOL_NAMES,
    READ_TOOL_NAMES,
    filter_tools as _filter_tools,
    current_profile as _current_profile,
)


def current_profile() -> str:
    return _current_profile("AGENT_TOOL_PROFILE")


def filter_tools(tools: list[Any], profile: str | None = None) -> list[Any]:
    return _filter_tools(tools, profile or current_profile())
