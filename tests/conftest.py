import os
import pytest


@pytest.fixture(autouse=True)
def isolate_audit_logs(tmp_path, monkeypatch):
    """
    Prevent unit tests from writing test session logs into the live mcp_server/logs/sessions directory.
    """
    test_log_dir = tmp_path / "logs"
    test_log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCP_AUDIT_LOG_DIR", str(test_log_dir))
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(test_log_dir / "test_audit.jsonl"))
