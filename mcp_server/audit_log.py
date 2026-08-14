"""
Structured audit logging for MCP traffic and Jotform API calls.

Each event is a single JSON line under a per-session file in
mcp_server/logs/sessions/. JSONL keeps the runtime dependency-free while
still being easy to inspect with jq or ship to a log collector later.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

SESSION_ID = os.environ.get("MCP_AUDIT_SESSION_ID") or uuid.uuid4().hex
SESSION_STARTED_AT = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
DEFAULT_LOG_DIR = Path(os.environ.get("MCP_AUDIT_LOG_DIR", Path(__file__).parent / "logs"))
MAX_FIELD_CHARS = int(os.environ.get("MCP_AUDIT_MAX_FIELD_CHARS", "12000"))
SENSITIVE_KEYS = {
    "apikey",
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
SENSITIVE_KEY_PARTS = ("api-key", "apikey", "authorization", "cookie", "password", "secret", "token")
SENSITIVE_VALUE_PARTS = ("bearer ", "basic ", "apikey=", "api_key=", "access_token=", "refresh_token=")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_path() -> Path:
    """
    Current audit log path.

    MCP_AUDIT_LOG_PATH is kept as an explicit escape hatch for tests or log
    collectors. Without it, every server process/session writes a different
    JSONL file.
    """
    override = os.environ.get("MCP_AUDIT_LOG_PATH")
    if override:
        return Path(override)
    return DEFAULT_LOG_DIR / "sessions" / f"{SESSION_STARTED_AT}_{SESSION_ID}.jsonl"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if key_lower in SENSITIVE_KEYS or any(part in key_lower for part in SENSITIVE_KEY_PARTS):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str) and any(part in value.lower() for part in SENSITIVE_VALUE_PARTS):
        return "[REDACTED]"
    return value


def _jsonable(value: Any) -> Any:
    value = _redact(value)
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        if hasattr(value, "model_dump"):
            return _jsonable(value.model_dump(mode="json"))
        if hasattr(value, "dict"):
            return _jsonable(value.dict())
        return repr(value)


def _truncate(value: Any) -> Any:
    text = json.dumps(_jsonable(value), ensure_ascii=False, default=str)
    if len(text) <= MAX_FIELD_CHARS:
        return _jsonable(value)
    return {
        "truncated": True,
        "chars": len(text),
        "preview": text[:MAX_FIELD_CHARS],
    }


def write_event(event_type: str, **fields: Any) -> None:
    sanitized_fields = _redact(fields)
    entry = {
        "timestamp": _now(),
        "session_id": SESSION_ID,
        "event_type": event_type,
        **{key: _truncate(value) for key, value in sanitized_fields.items()},
    }
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


class AuditedMCPServer(MCPServer):
    async def list_tools(self):
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        write_event("mcp.list_tools.started", request_id=request_id)
        try:
            tools = await super().list_tools()
        except Exception as exc:
            write_event(
                "mcp.list_tools.failed",
                request_id=request_id,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                error=repr(exc),
            )
            raise

        write_event(
            "mcp.list_tools.completed",
            request_id=request_id,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            tool_count=len(tools),
            tools=[tool.name for tool in tools],
        )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None):
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        write_event("mcp.tool_call.started", request_id=request_id, tool=name, arguments=arguments or {})
        try:
            result = await super().call_tool(name, arguments or {}, context)
        except Exception as exc:
            write_event(
                "mcp.tool_call.failed",
                request_id=request_id,
                tool=name,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                error=repr(exc),
            )
            raise

        write_event(
            "mcp.tool_call.completed",
            request_id=request_id,
            tool=name,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            result=result,
            is_error=getattr(result, "is_error", None),
        )
        return result


def log_jotform_request(
    *,
    method: str,
    url: str,
    params: dict[str, Any],
    json_body: Any,
    send: Callable[[], Any],
) -> Any:
    request_id = str(uuid.uuid4())
    started = time.perf_counter()
    write_event(
        "jotform.request.started",
        request_id=request_id,
        method=method,
        url=url,
        params=params,
        json_body=json_body,
    )
    try:
        response = send()
    except Exception as exc:
        write_event(
            "jotform.request.failed",
            request_id=request_id,
            method=method,
            url=url,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            error=repr(exc),
        )
        raise

    write_event(
        "jotform.request.completed",
        request_id=request_id,
        method=method,
        url=url,
        status_code=response.status_code,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
        response_text=response.text,
    )
    return response
