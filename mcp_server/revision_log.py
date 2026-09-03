"""
Workflow revision snapshots.

Each mutating tool captures the full combined workflow payload before it
writes. The snapshot is intentionally raw: it keeps workflow metadata,
elements, links, outcomes, trigger bindings, and any extra Jotform fields we
do not yet summarize in get_workflow.
"""
from __future__ import annotations

import json
import os
import uuid
import fcntl
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp_server.jotform_client import (
    JotformClient,
    workflow_revision_id,
    workflow_updated_at,
)

PROCESS_SESSION_ID = os.environ.get("MCP_REVISION_SESSION_ID") or uuid.uuid4().hex
MAX_REVISIONS_PER_WORKFLOW = max(1, int(os.environ.get("MCP_REVISION_MAX_PER_WORKFLOW", "50")))
DEFAULT_DIR_MODE = int(os.environ.get("MCP_REVISION_DIR_MODE", "700"), 8)
DEFAULT_FILE_MODE = int(os.environ.get("MCP_REVISION_FILE_MODE", "600"), 8)


def _workflow_url(workflow_id: str | None) -> str | None:
    return f"https://www.jotform.com/workflow/{workflow_id}/build" if workflow_id else None


def _revision_dir() -> Path:
    return Path(os.environ.get("MCP_REVISION_LOG_DIR", "mcp_server/revisions"))


def _revision_path(workflow_id: str) -> Path:
    safe_id = "".join(ch for ch in str(workflow_id) if ch.isalnum() or ch in ("-", "_"))
    return _revision_dir() / f"{safe_id}.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _count_items(snapshot: dict, key: str) -> int:
    value = snapshot.get(key)
    return len(value) if isinstance(value, list) else 0


def summarize_revision(record: dict) -> dict:
    snapshot = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else {}
    workflow = snapshot.get("workflow") if isinstance(snapshot.get("workflow"), dict) else {}
    workflow_id = str(record.get("workflow_id") or workflow.get("id") or "")
    return {
        "revision_id": record.get("revision_id"),
        "timestamp": record.get("timestamp"),
        "session_id": record.get("session_id"),
        "workflow_id": workflow_id or None,
        "workflow_url": _workflow_url(workflow_id or None),
        "reason": record.get("reason"),
        "title": workflow.get("title"),
        "step_count": _count_items(snapshot, "elements"),
        "link_count": _count_items(snapshot, "links"),
        "remote_revision_id": record.get("remote_revision_id"),
        "remote_updated_at": record.get("remote_updated_at"),
    }


def capture_workflow_revision(
    client: JotformClient,
    workflow_id: str,
    reason: str,
    *,
    tool_name: str | None = None,
) -> dict:
    snapshot = _read_full_workflow_snapshot(client, workflow_id)
    record = {
        "revision_id": uuid.uuid4().hex,
        "timestamp": _now(),
        "session_id": PROCESS_SESSION_ID,
        "workflow_id": str(workflow_id),
        "workflow_url": _workflow_url(str(workflow_id)),
        "reason": reason,
        "tool_name": tool_name,
        "remote_revision_id": workflow_revision_id(snapshot),
        "remote_updated_at": workflow_updated_at(snapshot),
        "snapshot": snapshot,
    }

    path = _revision_path(workflow_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(DEFAULT_DIR_MODE)
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.write(json.dumps(record, ensure_ascii=False, default=str))
        fh.write("\n")
        fh.flush()
        fh.seek(0)
        lines = fh.read().splitlines()
        if len(lines) > MAX_REVISIONS_PER_WORKFLOW:
            fh.seek(0)
            fh.truncate()
            fh.write("\n".join(lines[-MAX_REVISIONS_PER_WORKFLOW:]) + "\n")
            fh.flush()
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    path.chmod(DEFAULT_FILE_MODE)
    return record


def _read_full_workflow_snapshot(client: JotformClient, workflow_id: str) -> dict:
    """Prefer one full /combined read; hydrate only compatibility clients/mocks."""
    try:
        return client.get_workflow_combined(workflow_id, fetch_essential=False)
    except TypeError:
        snapshot = client.get_workflow_combined(workflow_id)
        return hydrate_workflow_snapshot(client, workflow_id, snapshot)


def _trim_revision_file(path: Path) -> None:
    with path.open("r+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        lines = fh.read().splitlines()
        if len(lines) > MAX_REVISIONS_PER_WORKFLOW:
            fh.seek(0)
            fh.truncate()
            fh.write("\n".join(lines[-MAX_REVISIONS_PER_WORKFLOW:]) + "\n")
            fh.flush()
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    path.chmod(DEFAULT_FILE_MODE)


def hydrate_workflow_snapshot(client: JotformClient, workflow_id: str, snapshot: dict) -> dict:
    """
    Enrich /combined with full element configs.

    /workflow/{id}/combined is great for graph shape, but it can omit fields
    such as email content, taskDescription, and other step-specific metadata.
    Revisions need those fields to be useful for rollback, so each element is
    read through get_element and merged back into the snapshot.
    """
    elements = []
    for element in snapshot.get("elements") or []:
        if not isinstance(element, dict):
            elements.append(element)
            continue
        element_id = element.get("element_id") or element.get("id")
        if element_id is None or not hasattr(client, "get_element"):
            elements.append(element)
            continue
        try:
            full = client.get_element(workflow_id, element_id)
        except Exception:
            elements.append(element)
            continue
        elements.append({**element, **full} if isinstance(full, dict) else element)
    return {**snapshot, "elements": elements}


def read_revision_records(workflow_id: str) -> list[dict]:
    path = _revision_path(workflow_id)
    if not path.exists():
        return []

    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def list_workflow_revisions(workflow_id: str, *, limit: int = 10) -> list[dict]:
    records = read_revision_records(workflow_id)
    summaries = [summarize_revision(record) for record in records]
    return summaries[-max(limit, 1):][::-1]


def load_workflow_revision(workflow_id: str, revision_id: str | None = None) -> dict | None:
    records = read_revision_records(workflow_id)
    if revision_id:
        return next((r for r in records if r.get("revision_id") == revision_id), None)
    return records[-1] if records else None


def _element_id(element: dict) -> str | None:
    value = element.get("element_id", element.get("id"))
    return str(value) if value is not None else None


def _link_id(link: dict) -> str | None:
    value = link.get("link_id", link.get("id"))
    return str(value) if value is not None else None


def build_restore_payloads(current: dict, target: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Return (link_deletes, element_writes, link_creates) for updateTree.

    Links are deleted first in a separate updateTree call. That avoids stale
    branch/outcome references while element configs are restored.
    """
    current_elements = [
        e for e in (current.get("elements") or []) if isinstance(e, dict) and _element_id(e)
    ]
    target_elements = [
        e for e in (target.get("elements") or []) if isinstance(e, dict) and _element_id(e)
    ]
    current_links = [
        l for l in (current.get("links") or []) if isinstance(l, dict) and _link_id(l)
    ]
    target_links = [
        l for l in (target.get("links") or []) if isinstance(l, dict) and _link_id(l)
    ]

    current_element_ids = {_element_id(e) for e in current_elements}
    target_element_ids = {_element_id(e) for e in target_elements}

    link_deletes = [
        {"action": "delete", "linkID": _link_id(link), "data": {"link_id": _link_id(link)}}
        for link in current_links
    ]

    element_writes = []
    for element in current_elements:
        eid = _element_id(element)
        if eid not in target_element_ids:
            element_writes.append({"action": "delete", "elementID": eid, "data": {"element_id": eid}})

    for element in target_elements:
        eid = _element_id(element)
        data = deepcopy(element)
        data["element_id"] = eid
        action = "update" if eid in current_element_ids else "create"
        element_writes.append({"action": action, "elementID": eid, "data": data})

    link_creates = []
    for link in target_links:
        lid = _link_id(link)
        data = deepcopy(link)
        data["link_id"] = lid
        link_creates.append({"action": "create", "linkID": lid, "data": data})

    return link_deletes, element_writes, link_creates


def restore_workflow_revision(
    client: JotformClient,
    workflow_id: str,
    revision: dict,
) -> tuple[dict, dict]:
    current = _read_full_workflow_snapshot(client, workflow_id)
    target = revision.get("snapshot")
    if not isinstance(target, dict):
        raise ValueError("Revision has no workflow snapshot.")

    link_deletes, element_writes, link_creates = build_restore_payloads(current, target)
    if link_deletes:
        client.update_tree(workflow_id, links=link_deletes)
    try:
        if element_writes or link_creates:
            client.update_tree(workflow_id, elements=element_writes, links=link_creates)
    except Exception as error:
        if link_deletes:
            _, rollback_elements, rollback_links = build_restore_payloads(target, current)
            try:
                client.update_tree(
                    workflow_id,
                    elements=rollback_elements,
                    links=rollback_links,
                )
            except Exception as rollback_error:
                raise RuntimeError(
                    f"Restore failed and automatic rollback also failed: {rollback_error}"
                ) from error
        raise
    return current, target
