"""
Structured audit logging for MCP traffic and Jotform API calls.

Each event is a single JSON line under a per-session file in
mcp_server/logs/sessions/. JSONL keeps the runtime dependency-free while
still being easy to inspect with jq or ship to a log collector later.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp_server.tool_profiles import current_profile, filter_tools

_CURRENT_SESSION_ID = os.environ.get("MCP_AUDIT_SESSION_ID") or uuid.uuid4().hex
_CURRENT_SESSION_STARTED_AT = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
_LAST_ACTIVITY_TIME = time.time()
INACTIVITY_TIMEOUT_SEC = 60.0  # 60s inactivity between prompts triggers a fresh session in STDIO/direct mode

SESSION_ID = _CURRENT_SESSION_ID
SESSION_STARTED_AT = _CURRENT_SESSION_STARTED_AT
DEFAULT_LOG_DIR = Path(os.environ.get("MCP_AUDIT_LOG_DIR", Path(__file__).parent / "logs"))
MAX_FIELD_CHARS = int(os.environ.get("MCP_AUDIT_MAX_FIELD_CHARS", "12000"))
EXPERIMENT_ENV_KEYS = {
    "experiment_id": "MCP_EXPERIMENT_ID",
    "experiment_scenario": "MCP_EXPERIMENT_SCENARIO",
    "experiment_prompt_id": "MCP_EXPERIMENT_PROMPT_ID",
    "experiment_prompt": "MCP_EXPERIMENT_PROMPT",
}
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


def _safe_path_segment(value: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return segment.strip("-")[:80]


from mcp_server.telemetry_context import bind_context, get_current_field, get_current_session_id


def get_active_session_id(event_type: str = "", tool_name: str = "") -> str:
    global _CURRENT_SESSION_ID, _CURRENT_SESSION_STARTED_AT, _LAST_ACTIVITY_TIME, SESSION_ID, SESSION_STARTED_AT

    # 1. Check ContextVar (e.g. from HTTP/SSE middleware or AuditedMCPServer.call_tool)
    ctx_sid = get_current_session_id()
    if ctx_sid:
        return ctx_sid

    # 2. Check explicit environment override
    env_sid = os.environ.get("MCP_AUDIT_SESSION_ID")
    if env_sid:
        return env_sid

    # 3. Check if module-level SESSION_ID was overridden/mocked
    if SESSION_ID and SESSION_ID != _CURRENT_SESSION_ID:
        return SESSION_ID

    # 4. Dynamic STDIO / Subprocess Inactivity & Intent Check
    now = time.time()
    gap = now - _LAST_ACTIVITY_TIME

    is_new_session = False
    if gap > INACTIVITY_TIMEOUT_SEC:
        is_new_session = True
    elif gap > 12.0 and (tool_name in ("search_workflow_templates", "create_form_with_ai", "restore_workflow_revision") or event_type == "mcp.list_tools.started"):
        is_new_session = True

    if is_new_session:
        _CURRENT_SESSION_ID = uuid.uuid4().hex
        _CURRENT_SESSION_STARTED_AT = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        SESSION_ID = _CURRENT_SESSION_ID
        SESSION_STARTED_AT = _CURRENT_SESSION_STARTED_AT

    _LAST_ACTIVITY_TIME = now
    return _CURRENT_SESSION_ID


def log_path(session_id: str | None = None) -> Path:
    """
    Current audit log path.

    MCP_AUDIT_LOG_PATH is kept as an explicit escape hatch for tests or log
    collectors. Without it, every session writes a distinct JSONL file.
    """
    override = os.environ.get("MCP_AUDIT_LOG_PATH")
    if override:
        return Path(override)
    sid = session_id or get_active_session_id()
    labels = [
        _safe_path_segment(os.environ[env_key])
        for env_key in ("MCP_EXPERIMENT_ID", "MCP_EXPERIMENT_SCENARIO", "MCP_EXPERIMENT_PROMPT_ID")
        if os.environ.get(env_key)
    ]
    prefix = "_".join(labels)
    started_at = SESSION_STARTED_AT or _CURRENT_SESSION_STARTED_AT
    filename = f"{started_at}_{sid}.jsonl"
    if prefix:
        filename = f"{prefix}_{filename}"
    return DEFAULT_LOG_DIR / "sessions" / filename


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
    experiment_fields = {
        key: os.environ[env_key]
        for key, env_key in EXPERIMENT_ENV_KEYS.items()
        if os.environ.get(env_key)
    }
    sid = get_active_session_id()

    provider = fields.get("provider") or get_current_field("provider") or os.environ.get("MCP_CLIENT_PROVIDER")
    model = fields.get("model") or get_current_field("model") or os.environ.get("MCP_CLIENT_MODEL")
    trace_id = fields.get("trace_id") or get_current_field("trace_id")
    span_id = fields.get("span_id") or get_current_field("span_id")
    parent_span_id = fields.get("parent_span_id") or get_current_field("parent_span_id")

    client_fields = {}
    if provider:
        client_fields["provider"] = provider
    if model:
        client_fields["model"] = model
    if trace_id:
        client_fields["trace_id"] = trace_id
    if span_id:
        client_fields["span_id"] = span_id
    if parent_span_id:
        client_fields["parent_span_id"] = parent_span_id

    entry = {
        "timestamp": _now(),
        "session_id": sid,
        **client_fields,
        **experiment_fields,
        "event_type": event_type,
        **{key: _truncate(value) for key, value in sanitized_fields.items() if key not in ("provider", "model", "trace_id", "span_id", "parent_span_id")},
    }
    path = log_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


class AuditedMCPServer(MCPServer):
    async def list_tools(self):
        sid = get_active_session_id(event_type="mcp.list_tools.started")
        request_id = str(uuid.uuid4())
        started = time.perf_counter()

        parent_span = get_current_field("span_id")
        trace_id = get_current_field("trace_id") or sid

        with bind_context(session_id=sid, trace_id=trace_id, span_id=request_id, parent_span_id=parent_span):
            write_event("mcp.list_tools.started", request_id=request_id, span_id=request_id)
            try:
                tools = filter_tools(await super().list_tools())
            except Exception as exc:
                write_event(
                    "mcp.list_tools.failed",
                    request_id=request_id,
                    span_id=request_id,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    error=repr(exc),
                )
                raise

            write_event(
                "mcp.list_tools.completed",
                request_id=request_id,
                span_id=request_id,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                tool_surface=current_profile(),
                tool_count=len(tools),
                tools=[tool.name for tool in tools],
            )
            return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None):
        client_sid = None
        if context and hasattr(context, "session_id") and context.session_id:
            client_sid = str(context.session_id)
        elif isinstance(context, dict) and context.get("session_id"):
            client_sid = str(context["session_id"])

        sid = client_sid or get_active_session_id(event_type="mcp.tool_call.started", tool_name=name)

        request_id = str(uuid.uuid4())
        started = time.perf_counter()

        parent_span = get_current_field("span_id")
        trace_id = get_current_field("trace_id") or sid

        with bind_context(session_id=sid, trace_id=trace_id, span_id=request_id, parent_span_id=parent_span):
            write_event("mcp.tool_call.started", request_id=request_id, span_id=request_id, tool=name, arguments=arguments or {})
            try:
                result = await super().call_tool(name, arguments or {}, context)
            except Exception as exc:
                write_event(
                    "mcp.tool_call.failed",
                    request_id=request_id,
                    span_id=request_id,
                    tool=name,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    error=repr(exc),
                )
                raise

            write_event(
                "mcp.tool_call.completed",
                request_id=request_id,
                span_id=request_id,
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
    
    parent_span = get_current_field("span_id")
    
    with bind_context(span_id=request_id, parent_span_id=parent_span):
        write_event(
            "jotform.request.started",
            request_id=request_id,
            span_id=request_id,
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
                span_id=request_id,
                method=method,
                url=url,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                error=repr(exc),
            )
            raise

        write_event(
            "jotform.request.completed",
            request_id=request_id,
            span_id=request_id,
            method=method,
            url=url,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            response_text=response.text,
        )
        return response
