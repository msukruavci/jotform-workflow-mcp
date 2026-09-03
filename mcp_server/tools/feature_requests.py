"""Model-facing feedback capture for unsupported workflow requests."""
from __future__ import annotations

import os
from typing import Annotated, Literal

from mcp.server import MCPServer
from pydantic import BaseModel, Field

from mcp_server import audit_log


CLOSE_MATCH_THRESHOLD = float(os.environ.get("MCP_TEMPLATE_CLOSE_MATCH_THRESHOLD", "0.68"))
DASHBOARD_CLUSTER_THRESHOLD = float(os.environ.get("MCP_FEATURE_CLUSTER_THRESHOLD", "0.88"))


class FeatureRequestResult(BaseModel):
    recorded: bool
    reason: str


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    def record_feature_request(
        category: Annotated[
            Literal["missing_template", "unsupported_workflow_capability"],
            Field(
                description=(
                    "Use missing_template when no close workflow template exists; use "
                    "unsupported_workflow_capability when the requested trigger, step, "
                    "integration, or behavior is unavailable."
                )
            ),
        ],
        request_summary: Annotated[
            str,
            Field(description="Short English summary without PII, credentials, or form answers."),
        ],
        workflow_id: str = "",
        workflow_url: str = "",
        top_template_id: str = "",
        top_template_title: str = "",
        top_template_score: float | None = None,
        missing_capability: str = "",
        evidence: str = "",
    ) -> FeatureRequestResult:
        """
        Record a genuine product gap after the workflow has been shown.

        Call only after `show_workflow`, and only for a missing template or an
        unsupported workflow capability. Do not record ordinary validation or
        transient Jotform API failures as feature requests.
        """
        summary = " ".join(str(request_summary or "").split())[:500]
        if not summary:
            return FeatureRequestResult(recorded=False, reason="A non-empty request_summary is required.")
        if category == "missing_template" and top_template_score is not None and top_template_score >= CLOSE_MATCH_THRESHOLD:
            return FeatureRequestResult(
                recorded=False,
                reason="Not recorded because the top template score is at or above close_match_threshold.",
            )

        audit_log.write_event(
            "feature_request.recorded",
            category=category,
            request_summary=summary,
            workflow_id=str(workflow_id or "") or None,
            workflow_url=str(workflow_url or "") or None,
            top_template_id=str(top_template_id or "") or None,
            top_template_title=str(top_template_title or "") or None,
            top_template_score=top_template_score,
            close_match_threshold=CLOSE_MATCH_THRESHOLD,
            missing_capability=str(missing_capability or "")[:300] or None,
            evidence=" ".join(str(evidence or "").split())[:500] or None,
            dashboard_cluster_threshold=DASHBOARD_CLUSTER_THRESHOLD,
        )
        reason = (
            "Recorded because no close template match was found."
            if category == "missing_template"
            else "Recorded because the requested workflow capability is unsupported."
        )
        return FeatureRequestResult(recorded=True, reason=reason)
