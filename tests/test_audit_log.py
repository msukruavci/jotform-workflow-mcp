import json

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
