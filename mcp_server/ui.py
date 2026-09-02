"""MCP Apps resource and presentation tools for workflow UI surfaces."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from mcp.server.apps import Apps, ResourceCsp
from pydantic import Field

from mcp_server.jotform_client import JotformClient
from mcp_server.models import WorkflowListUIResult, WorkflowPreviewUIResult
from mcp_server.tools.reading import read_workflow_list, read_workflow_preview

WORKFLOW_UI_RESOURCE_VERSION = 51
WORKFLOW_UI_RESOURCE_URI = (
    f"ui://jotform/workflows/v{WORKFLOW_UI_RESOURCE_VERSION}.html"
)
WORKFLOW_UI_LEGACY_RESOURCE_URIS: tuple[str, ...] = tuple(
    f"ui://jotform/workflows/v{version}.html"
    for version in range(1, WORKFLOW_UI_RESOURCE_VERSION)
)
LOGGER = logging.getLogger(__name__)

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
    resource_domains = [
        "https://*.jotform.com",
        "https://*.jotform.io",
        "https://cdn.jotfor.ms",
    ]
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
                connect_domains=["https://api.jotform.com", "https://*.jotform.com"],
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
    def show_workflows() -> WorkflowListUIResult:
        """
        Show the user's workflows in the interactive workflow list UI.

        Use this presentation tool when the user asks to see, browse, list,
        or choose from their workflows. It reads Jotform directly; never build
        its payload from assistant prose or remembered tool results.
        """
        return WorkflowListUIResult(data=read_workflow_list(client))

    @apps.tool(
        resource_uri=WORKFLOW_UI_RESOURCE_URI,
        title="Jotform Workflow",
        meta={
            **compatibility_meta,
            "openai/toolInvocation/invoking": "Loading workflow…",
            "openai/toolInvocation/invoked": "Workflow loaded",
        },
    )
    def show_workflow(
        workflow_id: Annotated[
            str,
            Field(description="Workflow id from list_workflows, create_workflow, or a write result."),
        ],
    ) -> WorkflowPreviewUIResult:
        """
        Show one workflow in the interactive read-only workflow preview UI.

        Use when the user asks to open, show, preview, or inspect a workflow.
        Also call this exactly once after all requested workflow creation or
        update operations have finished and their final state was read back.
        Do not call it for intermediate write steps.
        """
        data = read_workflow_preview(client, workflow_id)
        data.settings_runtime_url = settings_runtime_url
        return WorkflowPreviewUIResult(data=data)

    return apps
