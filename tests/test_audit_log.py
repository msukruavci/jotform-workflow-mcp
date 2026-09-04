import asyncio
import json
from types import SimpleNamespace

from mcp.server import MCPServer

from mcp_server import audit_log
from mcp_server.audit_log import _redact


def test_redact_masks_sensitive_key_variants():
    value = {
        "x-api-key": "secret",
        "refreshToken": "secret",
        "nested": {"Authorization": "Bearer secret"},
    }
    assert _redact(value) == {
        "x-api-key": "[REDACTED]",
        "refreshToken": "[REDACTED]",
        "nested": {"Authorization": "[REDACTED]"},
    }


def test_redact_masks_bearer_value_even_under_unknown_key():
    assert _redact({"header": "Bearer secret"}) == {"header": "[REDACTED]"}


def test_redact_masks_recipient_pii():
    assert _redact({"to": "person@example.com", "phone_number": "555"}) == {
        "to": "[REDACTED]",
        "phone_number": "[REDACTED]",
    }


def test_default_log_path_is_session_scoped(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_AUDIT_LOG_PATH", raising=False)
    monkeypatch.delenv("MCP_EXPERIMENT_ID", raising=False)
    monkeypatch.delenv("MCP_EXPERIMENT_SCENARIO", raising=False)
    monkeypatch.delenv("MCP_EXPERIMENT_PROMPT_ID", raising=False)
    monkeypatch.setattr(audit_log, "DEFAULT_LOG_DIR", tmp_path)
    monkeypatch.setattr(audit_log, "SESSION_STARTED_AT", "20260813T090000Z")
    monkeypatch.setattr(audit_log, "SESSION_ID", "session_1")

    assert audit_log.log_path() == tmp_path / "sessions" / "20260813T090000Z_session_1.jsonl"


def test_log_path_includes_experiment_labels(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_AUDIT_LOG_PATH", raising=False)
    monkeypatch.setenv("MCP_EXPERIMENT_ID", "abcd 2026/08/24")
    monkeypatch.setenv("MCP_EXPERIMENT_SCENARIO", "C")
    monkeypatch.setenv("MCP_EXPERIMENT_PROMPT_ID", "dealer-onboarding")
    monkeypatch.setattr(audit_log, "DEFAULT_LOG_DIR", tmp_path)
    monkeypatch.setattr(audit_log, "SESSION_STARTED_AT", "20260813T090000Z")
    monkeypatch.setattr(audit_log, "SESSION_ID", "session_1")

    assert audit_log.log_path() == tmp_path / "sessions" / "abcd-2026-08-24_C_dealer-onboarding_20260813T090000Z_session_1.jsonl"


def test_list_tools_can_start_new_fallback_session(monkeypatch):
    monkeypatch.delenv("MCP_AUDIT_SESSION_ID", raising=False)
    monkeypatch.setattr(audit_log, "_CURRENT_SESSION_ID", "session_a")
    monkeypatch.setattr(audit_log, "SESSION_ID", "session_a")
    monkeypatch.setattr(audit_log, "_HAS_ACTIVITY", True)
    monkeypatch.setattr(audit_log, "_LAST_ACTIVITY_TIME", 100.0)
    monkeypatch.setattr(audit_log, "LIST_TOOLS_SESSION_BOUNDARY_GAP_SEC", 2.0)
    monkeypatch.setattr(audit_log.time, "time", lambda: 103.0)
    monkeypatch.setattr(audit_log.uuid, "uuid4", lambda: type("UUID", (), {"hex": "session_b"})())

    assert audit_log.get_active_session_id(event_type="mcp.list_tools.started") == "session_b"


def test_idle_gap_starts_new_fallback_session(monkeypatch):
    monkeypatch.delenv("MCP_AUDIT_SESSION_ID", raising=False)
    monkeypatch.setattr(audit_log, "_CURRENT_SESSION_ID", "session_a")
    monkeypatch.setattr(audit_log, "SESSION_ID", "session_a")
    monkeypatch.setattr(audit_log, "_HAS_ACTIVITY", True)
    monkeypatch.setattr(audit_log, "_LAST_ACTIVITY_TIME", 100.0)
    monkeypatch.setattr(audit_log, "INACTIVITY_TIMEOUT_SEC", 60.0)
    monkeypatch.setattr(audit_log.time, "time", lambda: 161.0)
    monkeypatch.setattr(audit_log.uuid, "uuid4", lambda: type("UUID", (), {"hex": "session_b"})())

    assert audit_log.get_active_session_id(event_type="mcp.tool_call.started") == "session_b"


def test_write_event_adds_session_id_and_honors_path_override(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(path))
    monkeypatch.setenv("MCP_EXPERIMENT_SCENARIO", "B")
    monkeypatch.setattr(audit_log, "SESSION_ID", "session_2")

    audit_log.write_event("demo.event", token="secret")

    entry = json.loads(path.read_text().strip())
    assert entry["session_id"] == "session_2"
    assert entry["experiment_scenario"] == "B"
    assert entry["event_type"] == "demo.event"
    assert entry["token"] == "[REDACTED]"


def test_write_event_uses_private_permissions_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_AUDIT_LOG_PATH", raising=False)
    monkeypatch.setattr(audit_log, "DEFAULT_LOG_DIR", tmp_path)
    monkeypatch.setattr(audit_log, "SESSION_ID", "session_permissions")
    monkeypatch.setattr(audit_log, "SESSION_STARTED_AT", "20260901T090000Z")

    audit_log.write_event("demo.event")

    path = tmp_path / "sessions" / "20260901T090000Z_session_permissions.jsonl"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_log_path_sanitizes_session_id(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_AUDIT_LOG_PATH", raising=False)
    monkeypatch.setattr(audit_log, "DEFAULT_LOG_DIR", tmp_path)
    monkeypatch.setattr(audit_log, "SESSION_STARTED_AT", "20260901T090000Z")

    path = audit_log.log_path("../../outside/session")

    assert path.parent == tmp_path / "sessions"
    assert ".." not in path.name


def test_audited_server_marks_embedded_tool_error():
    result = SimpleNamespace(structured_content={"error": "broken"}, is_error=False)

    assert audit_log._mark_embedded_tool_error(result) is True
    assert result.is_error is True


def test_audited_server_keeps_partial_side_effect_as_warning():
    result = SimpleNamespace(
        structured_content={
            "workflow_id": "wf_partial_1",
            "error": "Workflow was created but graph write timed out.",
        },
        is_error=False,
    )

    assert audit_log._result_issue_severity(result) == "warning"
    assert audit_log._mark_embedded_tool_error(result) is False
    assert result.is_error is False


def test_audited_server_reports_fallback_result_as_warning():
    result = SimpleNamespace(
        structured_content={"form_id": "form_1", "fallback_used": True},
        is_error=False,
    )

    assert audit_log._result_issue_severity(result) == "warning"
    assert audit_log._mark_embedded_tool_error(result) is False


def test_audited_server_rejects_registered_but_hidden_tool():
    assert audit_log._tool_call_allowed("build_workflow_bulk") is True
    assert audit_log._tool_call_allowed("add_step") is False


def test_concurrent_operation_id_executes_mutating_tool_once(monkeypatch):
    calls = 0
    events = []

    async def fake_call_tool(self, name, arguments, context=None):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return SimpleNamespace(structured_content={"form_id": "form_1"}, is_error=False)

    monkeypatch.setattr(MCPServer, "call_tool", fake_call_tool)
    monkeypatch.setattr(audit_log, "get_active_session_id", lambda **kwargs: "session_1")
    monkeypatch.setattr(audit_log, "write_event", lambda event_type, **fields: events.append((event_type, fields)))
    audit_log._IDEMPOTENT_RESULTS.clear()
    server = object.__new__(audit_log.AuditedMCPServer)
    server._mutation_locks = {}

    async def run_calls():
        arguments = {"prompt": "Collect name and email", "operation_id": "form-request-1"}
        return await asyncio.gather(
            server.call_tool("create_form_with_ai", arguments),
            server.call_tool("create_form_with_ai", arguments),
        )

    first, second = asyncio.run(run_calls())

    assert calls == 1
    assert first.structured_content == second.structured_content == {"form_id": "form_1"}
    completed = [fields for event_type, fields in events if event_type == "mcp.tool_call.completed"]
    assert sum(bool(fields.get("idempotent_replay")) for fields in completed) == 1


def test_jotform_response_body_is_not_logged_by_default(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(path))
    monkeypatch.delenv("MCP_AUDIT_INCLUDE_RESPONSE_BODIES", raising=False)

    class Response:
        status_code = 200
        text = '{"email":"sensitive@example.com"}'

    audit_log.log_jotform_request(
        method="GET",
        url="https://api.jotform.com/form/1",
        params={},
        json_body=None,
        send=Response,
    )

    logged = path.read_text()
    assert "sensitive@example.com" not in logged
    assert '"response_body_logged": false' in logged


def test_trace_function_logs_duration_and_redacted_io(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(path))
    monkeypatch.setenv("MCP_AUDIT_FUNCTION_TRACES", "1")
    monkeypatch.setattr(audit_log, "SESSION_ID", "session_fn")

    @audit_log.trace_function
    def demo(email: str, value: int) -> dict:
        return {"to": email, "value": value + 1}

    assert demo("person@example.com", 4) == {"to": "person@example.com", "value": 5}

    entries = [json.loads(line) for line in path.read_text().splitlines()]
    completed = next(entry for entry in entries if entry["event_type"] == "function.call.completed")
    assert completed["session_id"] == "session_fn"
    assert completed["function"].endswith("demo")
    assert completed["duration_ms"] >= 0
    assert completed["arguments"]["email"] == "[REDACTED]"
    assert completed["result"]["to"] == "[REDACTED]"
    assert completed["result"]["value"] == 5


def test_list_function_traces_returns_dashboard_aggregate(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(path))
    monkeypatch.setattr(audit_log, "SESSION_ID", "session_fn_dashboard")

    audit_log.write_event(
        "function.call.completed",
        request_id="r1",
        function="module.fast",
        module="module",
        qualname="fast",
        duration_ms=3.0,
        arguments={"x": 1},
        result={"ok": True},
    )
    audit_log.write_event(
        "function.call.completed",
        request_id="r2",
        function="module.fast",
        module="module",
        qualname="fast",
        duration_ms=7.0,
    )

    result = audit_log.list_function_traces(limit=10, session_id="session_fn_dashboard")

    assert result["count"] == 2
    assert result["total_count"] == 2
    assert result["function_traces"][0]["function"] == "module.fast"
    assert result["functions"][0]["function"] == "module.fast"
    assert result["functions"][0]["count"] == 2
    assert result["functions"][0]["total_duration_ms"] == 10.0
    assert result["functions"][0]["avg_duration_ms"] == 5.0


def test_trace_function_metadata_reaches_dashboard_aggregate(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(path))
    monkeypatch.setenv("MCP_AUDIT_FUNCTION_TRACES", "1")
    monkeypatch.setattr(audit_log, "SESSION_ID", "session_fn_meta")

    @audit_log.trace_function
    def normalize_item(value: int, *, enabled: bool = True) -> dict:
        """Normalize one item for writing."""
        return {"value": value, "enabled": enabled}

    normalize_item(3, enabled=False)

    result = audit_log.list_function_traces(limit=10, session_id="session_fn_meta")
    function = result["functions"][0]
    trace = result["function_traces"][0]

    assert function["purpose"] == "Normalize one item for writing"
    assert "value" in function["signature"]
    assert function["last_arguments"]["value"] == 3
    assert function["last_result"] == {"value": 3, "enabled": False}
    assert trace["purpose"] == "Normalize one item for writing"
    assert trace["input_parameters"][0]["name"] == "value"
