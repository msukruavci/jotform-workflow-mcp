"""Short-lived, session-scoped workflow snapshots used for safe rebasing.

The model can occasionally omit an optimistic-lock token even immediately
after reading a workflow.  Keeping the actual read snapshot server-side lets
the builder compare only the steps and links a mutation will touch without
putting a full workflow payload back into the conversation.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from threading import RLock

from mcp_server.jotform_client import workflow_revision_id, workflow_updated_at
from mcp_server.telemetry_context import get_current_session_id

_MAX_SNAPSHOTS = 128
_LOCK = RLock()
_SNAPSHOTS: OrderedDict[tuple[str, str, str], dict] = OrderedDict()


def remember_workflow_snapshot(workflow_id: str, snapshot: dict) -> None:
    """Remember a cloud read for the active MCP session."""
    session_id = get_current_session_id()
    if not session_id or not isinstance(snapshot, dict):
        return
    revision_id = workflow_revision_id(snapshot)
    key = (session_id, str(workflow_id), revision_id)
    with _LOCK:
        _SNAPSHOTS[key] = deepcopy(snapshot)
        _SNAPSHOTS.move_to_end(key)
        while len(_SNAPSHOTS) > _MAX_SNAPSHOTS:
            _SNAPSHOTS.popitem(last=False)


def load_workflow_snapshot(
    workflow_id: str,
    *,
    revision_id: str | None = None,
    updated_at: str | None = None,
) -> dict | None:
    """Load the matching (or newest) read snapshot for this MCP session."""
    session_id = get_current_session_id()
    if not session_id:
        return None
    workflow_id = str(workflow_id)
    with _LOCK:
        matches = [
            (key, snapshot)
            for key, snapshot in _SNAPSHOTS.items()
            if key[0] == session_id and key[1] == workflow_id
            and (not revision_id or key[2] == revision_id)
            and (not updated_at or workflow_updated_at(snapshot) == updated_at)
        ]
        if not matches:
            return None
        key, snapshot = matches[-1]
        _SNAPSHOTS.move_to_end(key)
        return deepcopy(snapshot)


def clear_workflow_snapshots() -> None:
    """Test helper; production eviction is bounded by ``_MAX_SNAPSHOTS``."""
    with _LOCK:
        _SNAPSHOTS.clear()
