"""
Structured audit logging for MCP traffic and Jotform API calls.

Each event is a single JSON line under a per-session file in
mcp_server/logs/sessions/. JSONL keeps the runtime dependency-free while
still being easy to inspect with jq or ship to a log collector later.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
import fcntl
from copy import deepcopy
from collections.abc import Callable
from functools import wraps
from inspect import Signature, getdoc, signature
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp_server.tool_profiles import FAST_TOOLS, current_profile, filter_tools

_CURRENT_SESSION_ID = os.environ.get("MCP_AUDIT_SESSION_ID") or uuid.uuid4().hex
_CURRENT_SESSION_STARTED_AT = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
_LAST_ACTIVITY_TIME = time.time()
_HAS_ACTIVITY = False
INACTIVITY_TIMEOUT_SEC = float(os.environ.get("MCP_AUDIT_INACTIVITY_TIMEOUT_SEC", "60"))
LIST_TOOLS_SESSION_BOUNDARY_GAP_SEC = float(os.environ.get("MCP_AUDIT_LIST_TOOLS_SESSION_BOUNDARY_GAP_SEC", "2"))

SESSION_ID = _CURRENT_SESSION_ID
SESSION_STARTED_AT = _CURRENT_SESSION_STARTED_AT
DEFAULT_LOG_DIR = Path(os.environ.get("MCP_AUDIT_LOG_DIR", Path(__file__).parent / "logs"))
MAX_FIELD_CHARS = int(os.environ.get("MCP_AUDIT_MAX_FIELD_CHARS", "12000"))
DEFAULT_DIR_MODE = int(os.environ.get("MCP_AUDIT_DIR_MODE", "755"), 8)
DEFAULT_FILE_MODE = int(os.environ.get("MCP_AUDIT_FILE_MODE", "644"), 8)
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
PII_KEYS = {
    "email", "phone", "telephone", "address", "firstname", "lastname", "fullname",
    "to", "cc", "bcc", "replyto", "approver", "assignee", "recipients",
}
PII_KEY_PARTS = ("email_address", "phone_number", "street_address", "postal_address")
FALSE_ENV_VALUES = {"0", "false", "no", "off"}
_FUNCTION_TRACE_METADATA: dict[str, dict[str, Any]] = {}
_IDEMPOTENT_TOOLS = frozenset({"create_form_with_ai", "build_workflow_bulk"})
_IDEMPOTENCY_TTL_SEC = float(os.environ.get("MCP_IDEMPOTENCY_TTL_SEC", "900"))
_IDEMPOTENT_RESULTS: dict[tuple[str, str, str], tuple[float, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_path_segment(value: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    while ".." in segment:
        segment = segment.replace("..", "-")
    return segment.strip(".-")[:80]


from mcp_server.telemetry_context import bind_context, get_current_field, get_current_session_id


def get_active_session_id(event_type: str = "", tool_name: str = "") -> str:
    global _CURRENT_SESSION_ID, _CURRENT_SESSION_STARTED_AT, _LAST_ACTIVITY_TIME, _HAS_ACTIVITY, SESSION_ID, SESSION_STARTED_AT

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

    # 4. Last-resort STDIO/direct grouping when the caller supplies no session id.
    now = time.time()
    gap = now - _LAST_ACTIVITY_TIME

    starts_new_tool_session = (
        event_type == "mcp.list_tools.started"
        and _HAS_ACTIVITY
        and gap > LIST_TOOLS_SESSION_BOUNDARY_GAP_SEC
    )
    if gap > INACTIVITY_TIMEOUT_SEC or starts_new_tool_session:
        _CURRENT_SESSION_ID = uuid.uuid4().hex
        _CURRENT_SESSION_STARTED_AT = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        SESSION_ID = _CURRENT_SESSION_ID
        SESSION_STARTED_AT = _CURRENT_SESSION_STARTED_AT

    _LAST_ACTIVITY_TIME = now
    _HAS_ACTIVITY = True
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
    safe_sid = _safe_path_segment(str(sid)) or "unknown-session"
    filename = f"{started_at}_{safe_sid}.jsonl"
    if prefix:
        filename = f"{prefix}_{filename}"
    return DEFAULT_LOG_DIR / "sessions" / filename


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if (
                key_lower in SENSITIVE_KEYS
                or key_lower in PII_KEYS
                or any(part in key_lower for part in SENSITIVE_KEY_PARTS)
                or any(part in key_lower for part in PII_KEY_PARTS)
            ):
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
    try:
        path.parent.chmod(DEFAULT_DIR_MODE)
    except OSError:
        pass
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        f.flush()
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    try:
        path.chmod(DEFAULT_FILE_MODE)
    except OSError:
        pass


def function_tracing_enabled() -> bool:
    return os.environ.get("MCP_AUDIT_FUNCTION_TRACES", "1").strip().lower() not in FALSE_ENV_VALUES


def _annotation_text(value: Any) -> str:
    if value is Signature.empty:
        return ""
    return getattr(value, "__name__", repr(value))


def _humanized_function_purpose(function_name: str, doc: str) -> str:
    if doc:
        first_line = next((line.strip() for line in doc.splitlines() if line.strip()), "")
        if first_line:
            return first_line.rstrip(".")
    short_name = function_name.rsplit(".", 1)[-1].strip("_")
    return short_name.replace("_", " ") or function_name


def _function_trace_metadata(inner: Callable, function_name: str) -> dict[str, Any]:
    try:
        sig = signature(inner)
    except Exception:
        return {
            "purpose": _humanized_function_purpose(function_name, getdoc(inner) or ""),
            "signature": "",
            "input_parameters": [],
            "return_annotation": "",
        }

    parameters = []
    for param in sig.parameters.values():
        default = None if param.default is Signature.empty else repr(param.default)
        parameters.append({
            "name": param.name,
            "kind": str(param.kind).replace("_", " ").lower(),
            "required": param.default is Signature.empty,
            "default": default,
            "annotation": _annotation_text(param.annotation),
        })
    return {
        "purpose": _humanized_function_purpose(function_name, getdoc(inner) or ""),
        "signature": str(sig),
        "input_parameters": parameters,
        "return_annotation": _annotation_text(sig.return_annotation),
    }


def trace_function(func: Callable | None = None, *, name: str | None = None, include_io: bool = True, min_duration_ms: float = 0.0):
    """Log duration plus redacted input/output for selected internal functions."""
    def decorator(inner: Callable):
        if hasattr(inner, "__wrapped__") or hasattr(inner, "_is_traced"):
            return inner

        function_name = name or f"{inner.__module__}.{inner.__qualname__}"
        _FUNCTION_TRACE_METADATA[function_name] = _function_trace_metadata(inner, function_name)

        @wraps(inner)
        def wrapper(*args, **kwargs):
            if not function_tracing_enabled():
                return inner(*args, **kwargs)

            request_id = str(uuid.uuid4())
            started = time.perf_counter()
            parent_span = get_current_field("span_id")
            try:
                bound = signature(inner).bind_partial(*args, **kwargs)
                bound.apply_defaults()
                arguments = dict(bound.arguments)
            except Exception:
                arguments = {"args": args, "kwargs": kwargs}

            with bind_context(span_id=request_id, parent_span_id=parent_span):
                start_fields = {
                    "request_id": request_id,
                    "span_id": request_id,
                    "parent_span_id": parent_span,
                    "function": function_name,
                    "module": inner.__module__,
                    "qualname": inner.__qualname__,
                    "function_meta": _FUNCTION_TRACE_METADATA.get(function_name, {}),
                }
                if include_io:
                    start_fields["arguments"] = arguments

                if min_duration_ms <= 0:
                    write_event("function.call.started", **start_fields)

                try:
                    result = inner(*args, **kwargs)
                except Exception as exc:
                    duration_ms = (time.perf_counter() - started) * 1000
                    if min_duration_ms > 0:
                        write_event("function.call.started", **start_fields)
                    failed_fields = {
                        **start_fields,
                        "duration_ms": round(duration_ms, 2),
                        "error": repr(exc),
                    }
                    write_event("function.call.failed", **failed_fields)
                    raise

                duration_ms = (time.perf_counter() - started) * 1000
                if duration_ms >= min_duration_ms:
                    if min_duration_ms > 0:
                        write_event("function.call.started", **start_fields)
                    completed_fields = {
                        **start_fields,
                        "duration_ms": round(duration_ms, 2),
                    }
                    if include_io:
                        completed_fields["result"] = result
                    write_event("function.call.completed", **completed_fields)
                return result

        wrapper._is_traced = True
        return wrapper

    if func is not None:
        return decorator(func)
    return decorator


def auto_instrument_module(module, min_duration_ms: float = 0.0):
    """Automatically apply trace_function to all callables in a module."""
    import types
    import inspect

    for name, obj in vars(module).items():
        if name.startswith("_"):
            continue

        if isinstance(obj, types.FunctionType) and obj.__module__ == module.__name__:
            setattr(module, name, trace_function(min_duration_ms=min_duration_ms)(obj))
        elif isinstance(obj, type) and obj.__module__ == module.__name__:
            for attr_name, attr_value in vars(obj).items():
                if isinstance(attr_value, types.FunctionType):
                    setattr(obj, attr_name, trace_function(min_duration_ms=min_duration_ms)(attr_value))


def _session_log_paths() -> list[Path]:
    sessions_dir = DEFAULT_LOG_DIR / "sessions"
    paths: list[Path] = []
    override = os.environ.get("MCP_AUDIT_LOG_PATH")
    if override:
        override_path = Path(override)
        if override_path.is_file():
            paths.append(override_path)
    if sessions_dir.exists():
        paths.extend(
            sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        )
    return paths


def _read_recent_events(event_types: set[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in _session_log_paths():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("event_type") in event_types:
                events.append(entry)
    return events


def list_function_traces(*, limit: int = 100, offset: int = 0, session_id: str = "") -> dict[str, Any]:
    """Return internal function spans for dashboard timing and necessity analysis."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    wanted_session = str(session_id or "").strip()
    raw_events = _read_recent_events({"function.call.completed", "function.call.failed"})
    traces: list[dict[str, Any]] = []
    aggregate: dict[str, dict[str, Any]] = {}

    for entry in raw_events:
        if wanted_session and str(entry.get("session_id")) != wanted_session:
            continue
        status = "failed" if entry.get("event_type") == "function.call.failed" else "completed"
        function_name = str(entry.get("function") or "")
        duration = float(entry.get("duration_ms") or 0)
        function_meta = (
            _FUNCTION_TRACE_METADATA.get(function_name)
            or entry.get("function_meta")
            or {}
        )
        traces.append({
            "timestamp": entry.get("timestamp"),
            "session_id": entry.get("session_id"),
            "request_id": entry.get("request_id"),
            "trace_id": entry.get("trace_id"),
            "span_id": entry.get("span_id"),
            "parent_span_id": entry.get("parent_span_id"),
            "status": status,
            "function": function_name,
            "purpose": function_meta.get("purpose"),
            "signature": function_meta.get("signature"),
            "input_parameters": function_meta.get("input_parameters"),
            "return_annotation": function_meta.get("return_annotation"),
            "module": entry.get("module"),
            "qualname": entry.get("qualname"),
            "duration_ms": duration,
            "arguments": entry.get("arguments"),
            "result": entry.get("result"),
            "error": entry.get("error"),
        })
        stats = aggregate.setdefault(function_name, {
            "function": function_name,
            "count": 0,
            "error_count": 0,
            "total_duration_ms": 0.0,
            "min_duration_ms": None,
            "max_duration_ms": 0.0,
            "purpose": function_meta.get("purpose"),
            "signature": function_meta.get("signature"),
            "input_parameters": function_meta.get("input_parameters"),
            "return_annotation": function_meta.get("return_annotation"),
            "last_seen": None,
            "last_status": None,
            "last_arguments": None,
            "last_result": None,
            "last_error": None,
        })
        if function_meta and not stats.get("purpose"):
            stats.update({
                "purpose": function_meta.get("purpose"),
                "signature": function_meta.get("signature"),
                "input_parameters": function_meta.get("input_parameters"),
                "return_annotation": function_meta.get("return_annotation"),
            })
        stats["count"] += 1
        if status == "failed":
            stats["error_count"] += 1
        stats["total_duration_ms"] += duration
        stats["max_duration_ms"] = max(stats["max_duration_ms"], duration)
        stats["min_duration_ms"] = duration if stats["min_duration_ms"] is None else min(stats["min_duration_ms"], duration)
        if stats["last_seen"] is None:
            stats["last_seen"] = entry.get("timestamp")
            stats["last_status"] = status
            stats["last_arguments"] = entry.get("arguments")
            stats["last_result"] = entry.get("result")
            stats["last_error"] = entry.get("error")

    for stats in aggregate.values():
        stats["total_duration_ms"] = round(stats["total_duration_ms"], 1)
        stats["avg_duration_ms"] = round(stats["total_duration_ms"] / stats["count"], 1) if stats["count"] else 0
        stats["min_duration_ms"] = round(stats["min_duration_ms"] or 0, 1)
        stats["max_duration_ms"] = round(stats["max_duration_ms"], 1)

    page = traces[offset:offset + limit]
    next_offset = offset + limit if offset + limit < len(traces) else None
    return {
        "function_traces": page,
        "functions": sorted(aggregate.values(), key=lambda item: item["total_duration_ms"], reverse=True),
        "limit": limit,
        "offset": offset,
        "count": len(page),
        "total_count": len(traces),
        "has_more": next_offset is not None,
        "next_offset": next_offset,
    }


def list_feature_requests(*, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Return recorded feature request telemetry for dashboard views."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    paths = _session_log_paths()
    if not paths:
        return {
            "feature_requests": [],
            "limit": limit,
            "offset": offset,
            "count": 0,
            "has_more": False,
            "next_offset": None,
        }

    events: list[dict[str, Any]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("event_type") != "feature_request.recorded":
                continue
            events.append({
                "timestamp": entry.get("timestamp"),
                "session_id": entry.get("session_id"),
                "request_id": entry.get("request_id"),
                "category": entry.get("category"),
                "request_summary": entry.get("request_summary"),
                "workflow_id": entry.get("workflow_id"),
                "workflow_url": entry.get("workflow_url"),
                "top_template_id": entry.get("top_template_id"),
                "top_template_title": entry.get("top_template_title"),
                "top_template_score": entry.get("top_template_score"),
                "close_match_threshold": entry.get("close_match_threshold"),
                "missing_capability": entry.get("missing_capability"),
                "evidence": entry.get("evidence"),
                "dashboard_cluster_threshold": entry.get("dashboard_cluster_threshold"),
            })

    page = events[offset:offset + limit]
    next_offset = offset + limit if offset + limit < len(events) else None
    return {
        "feature_requests": page,
        "limit": limit,
        "offset": offset,
        "count": len(page),
        "has_more": next_offset is not None,
        "next_offset": next_offset,
    }


def _tool_call_allowed(name: str) -> bool:
    return name in FAST_TOOLS


def _structured_result(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    return structured if isinstance(structured, dict) else {}


def _result_has_side_effect(result: Any) -> bool:
    structured = _structured_result(result)
    return bool(structured.get("form_id") or structured.get("workflow_id"))


def _result_issue_severity(result: Any) -> str:
    structured = _structured_result(result)
    if structured.get("error"):
        if (
            _result_has_side_effect(result)
            or structured.get("partial_success")
            or structured.get("fallback_used")
            or structured.get("ai_fallback")
        ):
            return "warning"
        return "error"
    if structured.get("warnings") or structured.get("health_warnings") or structured.get("fallback_used"):
        return "warning"
    return "ok"


def _mark_embedded_tool_error(result: Any) -> bool:
    if _result_issue_severity(result) == "error":
        result.is_error = True
        return True
    return False


def _idempotency_key(session_id: str, tool_name: str, arguments: dict[str, Any]) -> tuple[str, str, str] | None:
    operation_id = str(arguments.get("operation_id") or "").strip()
    if tool_name not in _IDEMPOTENT_TOOLS or not operation_id:
        return None
    return session_id, tool_name, operation_id[:120]


def _prune_idempotent_results(now: float) -> None:
    expired = [
        key for key, (created_at, _) in _IDEMPOTENT_RESULTS.items()
        if now - created_at > _IDEMPOTENCY_TTL_SEC
    ]
    for key in expired:
        _IDEMPOTENT_RESULTS.pop(key, None)


class AuditedMCPServer(MCPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mutation_locks: dict[str, asyncio.Lock] = {}

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
        sid = get_active_session_id(event_type="mcp.tool_call.started", tool_name=name)

        request_id = str(uuid.uuid4())
        started = time.perf_counter()

        parent_span = get_current_field("span_id")
        trace_id = get_current_field("trace_id") or sid

        with bind_context(session_id=sid, trace_id=trace_id, span_id=request_id, parent_span_id=parent_span):
            write_event("mcp.tool_call.started", request_id=request_id, span_id=request_id, tool=name, arguments=arguments or {})
            if not _tool_call_allowed(name):
                error = ValueError(f"Tool '{name}' is not available on this MCP server.")
                write_event(
                    "mcp.tool_call.failed",
                    request_id=request_id,
                    span_id=request_id,
                    tool=name,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    error=repr(error),
                )
                raise error
            now = time.monotonic()
            _prune_idempotent_results(now)
            idempotency_key = _idempotency_key(sid, name, arguments or {})
            cached = _IDEMPOTENT_RESULTS.get(idempotency_key) if idempotency_key else None
            if cached is not None:
                result = deepcopy(cached[1])
                write_event(
                    "mcp.tool_call.completed",
                    request_id=request_id,
                    span_id=request_id,
                    tool=name,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    result=result,
                    is_error=getattr(result, "is_error", None),
                    result_severity=_result_issue_severity(result),
                    idempotent_replay=True,
                )
                return result
            replayed = False
            try:
                mutation_key = None
                if name == "build_workflow_bulk":
                    mutation_key = str(
                        (arguments or {}).get("workflow_id")
                        or (arguments or {}).get("operation_id")
                        or ""
                    ).strip()
                elif idempotency_key:
                    mutation_key = ":".join(idempotency_key)
                if mutation_key:
                    lock = self._mutation_locks.setdefault(mutation_key, asyncio.Lock())
                    async with lock:
                        cached = _IDEMPOTENT_RESULTS.get(idempotency_key) if idempotency_key else None
                        if cached is not None:
                            result = deepcopy(cached[1])
                            replayed = True
                        else:
                            result = await super().call_tool(name, arguments or {}, context)
                            _mark_embedded_tool_error(result)
                            if idempotency_key and (
                                not getattr(result, "is_error", False) or _result_has_side_effect(result)
                            ):
                                _IDEMPOTENT_RESULTS[idempotency_key] = (time.monotonic(), deepcopy(result))
                else:
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

            if not mutation_key:
                _mark_embedded_tool_error(result)
                if idempotency_key and (
                    not getattr(result, "is_error", False) or _result_has_side_effect(result)
                ):
                    _IDEMPOTENT_RESULTS[idempotency_key] = (time.monotonic(), deepcopy(result))

            write_event(
                "mcp.tool_call.completed",
                request_id=request_id,
                span_id=request_id,
                tool=name,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                result=result,
                is_error=getattr(result, "is_error", None),
                result_severity=_result_issue_severity(result),
                idempotent_replay=replayed,
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

        response_fields = {
            "response_chars": len(response.text or ""),
            "response_body_logged": False,
        }
        if os.environ.get("MCP_AUDIT_INCLUDE_RESPONSE_BODIES") == "1":
            response_fields = {
                "response_text": response.text,
                "response_body_logged": True,
            }
        write_event(
            "jotform.request.completed",
            request_id=request_id,
            span_id=request_id,
            method=method,
            url=url,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            **response_fields,
        )
        return response
