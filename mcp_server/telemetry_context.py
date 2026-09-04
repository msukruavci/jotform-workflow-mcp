"""Async-safe correlation context shared by agent, MCP and HTTP logging."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_FIELDS = ("task_id", "session_id", "turn_id", "model_step_id", "trace_id", "span_id", "parent_span_id", "provider", "model")
_VARS = {name: ContextVar(f"telemetry_{name}", default=None) for name in _FIELDS}
_sequence_no: ContextVar[int] = ContextVar("telemetry_sequence_no", default=0)


def new_id() -> str:
    return uuid.uuid4().hex


def get_current_session_id() -> str | None:
    return _VARS["session_id"].get()


def get_current_field(name: str) -> str | None:
    var = _VARS.get(name)
    return var.get() if var else None


def current_context() -> dict[str, str]:
    return {name: value for name in _FIELDS if (value := _VARS[name].get()) is not None}


def next_sequence() -> int:
    value = _sequence_no.get() + 1
    _sequence_no.set(value)
    return value


@contextmanager
def bind_context(**values: str | None) -> Iterator[None]:
    tokens = {}
    for name, value in values.items():
        if name not in _VARS:
            raise ValueError(f"Unknown telemetry context field: {name}")
        tokens[name] = _VARS[name].set(value)
    try:
        yield
    finally:
        for name, token in reversed(tokens.items()):
            _VARS[name].reset(token)


def reset_sequence() -> None:
    _sequence_no.set(0)

