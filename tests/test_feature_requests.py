import json

from mcp_server import audit_log
from mcp_server.tools import feature_requests


class DummyMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _registered_tool():
    mcp = DummyMCP()
    feature_requests.register(mcp)
    return mcp.tools["record_feature_request"]


def test_record_feature_request_logs_low_template_match(monkeypatch, tmp_path):
    path = tmp_path / "feature_requests.jsonl"
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(path))
    monkeypatch.setattr(audit_log, "SESSION_ID", "session_feature")

    result = _registered_tool()(
        category="missing_template",
        request_summary="Create a partner onboarding workflow with regional legal review.",
        workflow_id="wf_1",
        workflow_url="https://www.jotform.com/workflow/wf_1/build",
        top_template_id="tmpl_1",
        top_template_title="Basic Approval",
        top_template_score=0.42,
        evidence="Best template score was 0.42.",
    )

    assert result.recorded is True
    assert result.reason == "Recorded because no close template match was found."

    entry = json.loads(path.read_text().strip())
    assert entry["event_type"] == "feature_request.recorded"
    assert entry["category"] == "missing_template"
    assert entry["session_id"] == "session_feature"
    assert entry["top_template_score"] == 0.42
    assert entry["close_match_threshold"] == 0.68
    assert entry["dashboard_cluster_threshold"] == 0.88


def test_record_feature_request_skips_close_template_match(monkeypatch, tmp_path):
    path = tmp_path / "feature_requests.jsonl"
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(path))

    result = _registered_tool()(
        category="missing_template",
        request_summary="Create a purchase approval workflow.",
        top_template_score=0.71,
    )

    assert result.recorded is False
    assert "at or above close_match_threshold" in result.reason
    assert not path.exists()


def test_record_feature_request_logs_unsupported_capability(monkeypatch, tmp_path):
    path = tmp_path / "feature_requests.jsonl"
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(path))

    result = _registered_tool()(
        category="unsupported_workflow_capability",
        request_summary="Use a Shopify order trigger before fulfillment review.",
        missing_capability="Shopify trigger",
        top_template_score=0.93,
        evidence="Requested trigger is not available in this MCP surface.",
    )

    assert result.recorded is True
    entry = json.loads(path.read_text().strip())
    assert entry["category"] == "unsupported_workflow_capability"
    assert entry["missing_capability"] == "Shopify trigger"


def test_list_feature_requests_reads_dashboard_events(monkeypatch, tmp_path):
    path = tmp_path / "feature_requests.jsonl"
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(path))
    monkeypatch.setattr(audit_log, "SESSION_ID", "session_dashboard")

    _registered_tool()(
        category="missing_template",
        request_summary="Create a workflow for custom lab sample intake.",
        workflow_id="wf_2",
        top_template_score=0.11,
    )

    result = audit_log.list_feature_requests(limit=10)

    assert result["count"] == 1
    assert result["feature_requests"][0]["session_id"] == "session_dashboard"
    assert result["feature_requests"][0]["request_summary"] == "Create a workflow for custom lab sample intake."
