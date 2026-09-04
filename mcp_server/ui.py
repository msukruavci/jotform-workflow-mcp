"""MCP Apps resource and presentation tools for workflow UI surfaces."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from mcp.server.apps import Apps, ResourceCsp
from mcp_types import CallToolResult, TextContent
from pydantic import Field

from mcp_server.jotform_client import JotformClient
from mcp_server.models import WorkflowListUIResult, WorkflowPreviewUIResult
from mcp_server.tools.reading import read_workflow_list, read_workflow_preview

# Bump this whenever the embedded MCP UI or its CSP contract changes. Clients
# cache `ui://` resources by URI, so reusing a version can leave an older host
# unable to load a newly configured settings runtime.
WORKFLOW_UI_RESOURCE_VERSION = 53
WORKFLOW_UI_RESOURCE_URI = (
    f"ui://jotform/workflows/v{WORKFLOW_UI_RESOURCE_VERSION}.html"
)
WORKFLOW_UI_LEGACY_RESOURCE_URIS: tuple[str, ...] = tuple(
    f"ui://jotform/workflows/v{version}.html"
    for version in range(1, WORKFLOW_UI_RESOURCE_VERSION)
)
LOGGER = logging.getLogger(__name__)

# ResourceCsp controls destinations loaded *by* the sandboxed MCP App. Keep
# these as exact origins: wildcard Jotform subdomains would let a compromised
# or user-controlled tenant origin become an allowed script/network source.
WORKFLOW_UI_CONNECT_ORIGINS = ("https://api.jotform.com",)
WORKFLOW_UI_RESOURCE_ORIGINS = (
    "https://www.jotform.com",
    "https://cdn.jotfor.ms",
)

_FALLBACK_HTML = """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
  <body style="font-family:system-ui,sans-serif;padding:24px">
    <h2>Workflow preview is unavailable</h2>
    <p>The Workflow MCP UI asset has not been built for this server deployment.</p>
  </body>
</html>
"""


def workflow_settings_runtime_url() -> str | None:
    """Return the explicitly configured HTTPS runtime without guessing a host."""
    value = os.environ.get("WORKFLOW_SETTINGS_RUNTIME_URL", "").strip()
    if not value:
        return None

    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        LOGGER.warning("Ignoring invalid WORKFLOW_SETTINGS_RUNTIME_URL; HTTPS is required.")
        return None
    return value


def _runtime_resource_origin(runtime_url: str | None) -> str | None:
    if not runtime_url:
        return None
    parsed = urlsplit(runtime_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _ui_asset_candidates() -> list[Path]:
    configured = os.environ.get("WORKFLOW_MCP_UI_HTML_PATH")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())

    repository_root = Path(__file__).resolve().parents[1]
    workspace_root = repository_root.parent
    candidates.extend([
        repository_root / "mcp_server" / "assets" / "workflow-mcp-ui.html",
        workspace_root / "frontend" / "packages" / "apps" / "workflow-mcp-ui" / "build" / "mcp-app.html",
    ])
    return candidates


def load_workflow_ui_html() -> str:
    """Load the packaged frontend without making server startup depend on it."""
    for candidate in _ui_asset_candidates():
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")

    LOGGER.warning(
        "Workflow MCP UI asset was not found. Build the frontend resource or set "
        "WORKFLOW_MCP_UI_HTML_PATH. Falling back to a diagnostic page."
    )
    return _FALLBACK_HTML


def create_workflow_apps(client: JotformClient, *, html: str | None = None) -> Apps:
    """Create the UI extension before it is attached to the MCP server."""
    apps = Apps()
    resource_html = html if html is not None else load_workflow_ui_html()
    settings_runtime_url = workflow_settings_runtime_url()
    runtime_resource_origin = _runtime_resource_origin(settings_runtime_url)
    resource_domains = list(WORKFLOW_UI_RESOURCE_ORIGINS)
    if runtime_resource_origin and runtime_resource_origin not in resource_domains:
        resource_domains.append(runtime_resource_origin)

    for resource_uri in (*WORKFLOW_UI_LEGACY_RESOURCE_URIS, WORKFLOW_UI_RESOURCE_URI):
        apps.add_html_resource(
            resource_uri,
            resource_html,
            name="Jotform Workflow UI",
            title="Jotform Workflows",
            description="Read-only workflow list and verified workflow graph preview.",
            csp=ResourceCsp(
                connect_domains=list(WORKFLOW_UI_CONNECT_ORIGINS),
                resource_domains=resource_domains,
                frame_domains=[],
                base_uri_domains=[],
            ),
            prefers_border=True,
        )

    compatibility_meta = {
        "openai/outputTemplate": WORKFLOW_UI_RESOURCE_URI,
        "openai/widgetAccessible": True,
    }

    @apps.tool(
        resource_uri=WORKFLOW_UI_RESOURCE_URI,
        title="Jotform Workflows",
        meta={
            **compatibility_meta,
            "openai/toolInvocation/invoking": "Loading workflows…",
            "openai/toolInvocation/invoked": "Workflows loaded",
        },
    )
    async def show_workflows(
        limit: Annotated[int, Field(description="Page size, 1-100. Default 50.")] = 50,
        offset: Annotated[int, Field(description="Zero-based page offset. Default 0.")] = 0,
    ) -> WorkflowListUIResult:
        """
        Show the user's workflows in the interactive workflow list UI.

        Use this presentation tool when the user asks to see, browse, list,
        or choose from their workflows. It reads Jotform directly; never build
        its payload from assistant prose or remembered tool results.
        """
        return WorkflowListUIResult(data=read_workflow_list(client, limit=limit, offset=offset))

    @apps.tool(
        resource_uri=WORKFLOW_UI_RESOURCE_URI,
        title="Jotform Workflow",
        meta={
            **compatibility_meta,
            "openai/toolInvocation/invoking": "Loading workflow…",
            "openai/toolInvocation/invoked": "Workflow loaded",
        },
    )
    async def show_workflow(
        workflow_id: Annotated[
            str,
            Field(description="Workflow id returned by build_workflow_bulk or resolved from list_workflows."),
        ],
    ) -> CallToolResult:
        """
        Show one workflow in the interactive read-only workflow preview UI.

        Use when the user asks to open, show, preview, or inspect a workflow.
        Call immediately after build_workflow_bulk to present the interactive UI
        canvas, or after any other workflow update operations have finished.
        Do NOT insert an extra get_workflow call before show_workflow — build_workflow_bulk
        already returns the complete summary, and show_workflow fetches and verifies the
        live workflow graph internally. Do not call it for intermediate write steps.
        """
        data = read_workflow_preview(client, workflow_id)
        data.settings_runtime_url = settings_runtime_url
        payload = WorkflowPreviewUIResult(data=data)
        structured = payload.model_dump(mode="json", by_alias=True)
        data = structured["data"]
        summary = {
            "view": "workflow-preview",
            "workflow_id": data.get("workflow_id"),
            "workflow_url": data.get("workflow_url"),
            "title": data.get("title"),
            "status": data.get("status"),
            "revision_id": data.get("revision_id"),
            "step_count": len(data.get("step_states") or []),
            "warnings": data.get("warnings") or [],
            "error": data.get("error"),
            "ui_rendered": True,
        }
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(summary, ensure_ascii=False))],
            structured_content=structured,
        )

    return apps
