import pytest

from mcp_server import revision_log, sync_state
from mcp_server.jotform_client import (
    ConflictError,
    JotformAPIError,
    JotformClient,
    workflow_revision_id,
)
from mcp_server.models import (
    ConnectionSpec,
    StepSpec,
)
from mcp_server.tools import building
from mcp_server.telemetry_context import bind_context


class DummyMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _snapshot(*, updated_at="2026-08-31T10:00:00Z", elements=None, links=None):
    return {
        "workflow": {
            "id": "wf_1",
            "title": "Synced workflow",
            "updated_at": updated_at,
        },
        "elements": elements or [
            {"element_id": 1, "type": "workflow_start_point", "resourceID": "form_1"}
        ],
        "links": links or [],
    }


def test_update_tree_rejects_external_edit_before_put(monkeypatch):
    client = JotformClient(api_key="test-key")
    base = _snapshot()
    remote = _snapshot(
        updated_at="2026-08-31T10:05:00Z",
        elements=base["elements"] + [{"element_id": 7, "type": "workflow_approval"}],
    )
    writes = []

    def fake_request(method, path, **kwargs):
        if method == "GET":
            return {"content": remote}
        writes.append((method, path, kwargs))
        return {"content": {}}

    monkeypatch.setattr(client, "_request", fake_request)

    with pytest.raises(ConflictError) as raised:
        client.update_tree(
            "wf_1",
            elements=[],
            expected_revision_id=workflow_revision_id(base),
        )

    assert raised.value.current_revision_id == workflow_revision_id(remote)
    assert writes == []


def test_revision_token_is_stable_across_compact_and_full_read_shapes():
    compact = _snapshot()
    full = _snapshot(elements=[{
        **compact["elements"][0],
        "name": "Submission",
        "position": {"x": 10, "y": 20},
        "x": 10,
        "y": 20,
        "privateRendererState": {"expanded": True},
    }])

    assert workflow_revision_id(compact) == workflow_revision_id(full)


def test_update_tree_rejects_newer_cloud_timestamp(monkeypatch):
    client = JotformClient(api_key="test-key")
    remote = _snapshot(updated_at="2026-08-31T10:05:00Z")
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path, **kwargs: {"content": remote},
    )

    with pytest.raises(ConflictError):
        client.update_tree(
            "wf_1",
            links=[],
            base_updated_at="2026-08-31T10:00:00Z",
        )


def test_existing_bulk_update_without_token_uses_fresh_live_revision(monkeypatch):
    mcp = DummyMCP()
    client = CanvasClient()
    building.register(mcp, client)
    monkeypatch.setattr(revision_log, "capture_workflow_revision", lambda *args, **kwargs: {})

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        steps=[StepSpec(ref="finish", type="workflow_end_point", config={})],
        connections=[ConnectionSpec(from_ref="start", to_ref="finish")],
    )

    assert result.conflict is False
    assert result.error is None
    assert any("No revision token was supplied" in warning for warning in result.warnings)
    assert len(client.update_calls) == 1


class CanvasClient:
    def __init__(self):
        self.snapshot = _snapshot()
        self.update_calls = []
        self.status_updates = []
        self.get_elements_calls = 0
        self.get_links_calls = 0

    def get_workflow_combined(self, workflow_id, *, fetch_essential=True):
        return self.snapshot

    def assert_workflow_revision(
        self,
        workflow_id,
        *,
        expected_revision_id=None,
        base_updated_at=None,
    ):
        current_revision = workflow_revision_id(self.snapshot)
        if expected_revision_id and expected_revision_id != current_revision:
            raise ConflictError(
                workflow_id,
                expected_revision_id=expected_revision_id,
                current_revision_id=current_revision,
                current_updated_at=self.snapshot["workflow"]["updated_at"],
                current_snapshot=self.snapshot,
            )
        return {
            "revision_id": current_revision,
            "updated_at": self.snapshot["workflow"]["updated_at"],
            "snapshot": self.snapshot,
        }

    def get_elements(self, workflow_id):
        self.get_elements_calls += 1
        return self.snapshot["elements"]

    def get_links(self, workflow_id):
        self.get_links_calls += 1
        return self.snapshot["links"]

    def get_element(self, workflow_id, step_id):
        return next(
            item for item in self.snapshot["elements"]
            if str(item.get("element_id")) == str(step_id)
        )

    def get_form_questions(self, form_id):
        return {}

    def update_workflow_metadata(self, workflow_id, **fields):
        self.status_updates.append({"workflow_id": workflow_id, "fields": fields})
        return {"status": fields.get("status")}

    def update_tree(
        self,
        workflow_id,
        *,
        elements=None,
        links=None,
        expected_revision_id=None,
        base_updated_at=None,
    ):
        current_revision = workflow_revision_id(self.snapshot)
        if expected_revision_id != current_revision:
            raise ConflictError(
                workflow_id,
                expected_revision_id=expected_revision_id,
                current_revision_id=current_revision,
                current_updated_at=self.snapshot["workflow"]["updated_at"],
            )
        self.update_calls.append({"elements": elements or [], "links": links or []})
        created = [item["data"] for item in (elements or []) if item.get("action") == "create"]
        created_links = [item["data"] for item in (links or []) if item.get("action") == "create"]
        self.snapshot = _snapshot(
            updated_at="2026-08-31T10:01:00Z",
            elements=self.snapshot["elements"] + created,
            links=self.snapshot["links"] + created_links,
        )
        return {}


class StatusMutatingCanvasClient(CanvasClient):
    def update_workflow_metadata(self, workflow_id, **fields):
        result = super().update_workflow_metadata(workflow_id, **fields)
        workflow = {
            **self.snapshot.get("workflow", {}),
            **fields,
            "updated_at": "2026-08-31T10:00:30Z",
        }
        self.snapshot = {**self.snapshot, "workflow": workflow}
        return result


def test_failed_graph_write_restores_previous_enabled_status(monkeypatch):
    class FailingClient(StatusMutatingCanvasClient):
        def update_tree(self, *args, **kwargs):
            raise JotformAPIError(503, "temporary failure")

    mcp = DummyMCP()
    client = FailingClient()
    client.snapshot["workflow"]["status"] = "ENABLED"
    building.register(mcp, client)
    monkeypatch.setattr(revision_log, "capture_workflow_revision", lambda *args, **kwargs: {})
    revision_id = workflow_revision_id(client.snapshot)

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        steps=[StepSpec(ref="finish", type="workflow_end_point", config={})],
        connections=[ConnectionSpec(from_ref="start", to_ref="finish")],
        expected_revision_id=revision_id,
    )

    assert result.error
    assert [item["fields"]["status"] for item in client.status_updates] == ["DISABLED", "ENABLED"]
    assert any("Restored workflow status" in warning for warning in result.warnings)


def test_bulk_rejects_stale_revision_without_session_snapshot(monkeypatch):
    mcp = DummyMCP()
    client = CanvasClient()
    building.register(mcp, client)
    monkeypatch.setattr(revision_log, "capture_workflow_revision", lambda *args, **kwargs: {})

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        steps=[StepSpec(ref="finish", type="workflow_end_point", config={})],
        connections=[ConnectionSpec(from_ref="start", to_ref="finish")],
        expected_revision_id="sha256:stale",
    )

    assert result.conflict is True
    assert result.error
    assert "No write was attempted" in result.error
    assert client.get_elements_calls == 0
    assert client.get_links_calls == 0
    assert client.update_calls == []


def test_successful_bulk_update_returns_next_revision_token(monkeypatch):
    mcp = DummyMCP()
    client = CanvasClient()
    building.register(mcp, client)
    monkeypatch.setattr(revision_log, "capture_workflow_revision", lambda *args, **kwargs: {})
    base_revision = workflow_revision_id(client.snapshot)

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        steps=[StepSpec(ref="finish", type="workflow_end_point", config={})],
        connections=[ConnectionSpec(from_ref="start", to_ref="finish")],
        expected_revision_id=base_revision,
    )

    assert result.error is None
    assert result.revision_id
    assert result.revision_id != base_revision
    assert result.updated_at == "2026-08-31T10:01:00Z"


def test_bulk_refreshes_revision_after_its_own_disable_write(monkeypatch):
    mcp = DummyMCP()
    client = StatusMutatingCanvasClient()
    building.register(mcp, client)
    monkeypatch.setattr(revision_log, "capture_workflow_revision", lambda *args, **kwargs: {})
    base_revision = workflow_revision_id(client.snapshot)

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        steps=[StepSpec(ref="finish", type="workflow_end_point", config={})],
        connections=[ConnectionSpec(from_ref="start", to_ref="finish")],
        expected_revision_id=base_revision,
    )

    assert result.conflict is False
    assert result.error is None
    assert any("Refreshed workflow revision after disabling" in warning for warning in result.warnings)
    assert len(client.status_updates) == 1
    assert len(client.update_calls) == 1


def test_bulk_rebases_stale_global_revision_when_affected_scope_is_unchanged(monkeypatch):
    mcp = DummyMCP()
    client = CanvasClient()
    base = _snapshot(elements=[
        {"element_id": 1, "type": "workflow_start_point", "resourceID": "form_1"},
        {"element_id": 9, "type": "workflow_send_email", "name": "Old unrelated email"},
    ])
    client.snapshot = _snapshot(
        updated_at="2026-08-31T10:05:00Z",
        elements=[
            {"element_id": 1, "type": "workflow_start_point", "resourceID": "form_1"},
            {"element_id": 9, "type": "workflow_send_email", "name": "Edited unrelated email"},
        ],
    )
    building.register(mcp, client)
    monkeypatch.setattr(revision_log, "capture_workflow_revision", lambda *args, **kwargs: {})
    sync_state.clear_workflow_snapshots()

    with bind_context(session_id="scope-unrelated"):
        sync_state.remember_workflow_snapshot("wf_1", base)
        result = mcp.tools["build_workflow_bulk"](
            "wf_1",
            steps=[StepSpec(ref="finish", type="workflow_end_point", config={})],
            connections=[ConnectionSpec(from_ref="start", to_ref="finish")],
            expected_revision_id=workflow_revision_id(base),
        )

    assert result.conflict is False
    assert result.error is None
    assert any("changed only outside" in warning for warning in result.warnings)
    assert len(client.update_calls) == 1


def test_bulk_rejects_change_inside_affected_scope_before_write(monkeypatch):
    mcp = DummyMCP()
    client = CanvasClient()
    base = _snapshot(elements=[{
        "element_id": 1,
        "type": "workflow_start_point",
        "resourceID": "form_1",
        "name": "Old start",
    }])
    client.snapshot = _snapshot(
        updated_at="2026-08-31T10:05:00Z",
        elements=[{
            "element_id": 1,
            "type": "workflow_start_point",
            "resourceID": "form_1",
            "name": "Externally changed start",
        }],
    )
    building.register(mcp, client)
    monkeypatch.setattr(revision_log, "capture_workflow_revision", lambda *args, **kwargs: {})
    sync_state.clear_workflow_snapshots()

    with bind_context(session_id="scope-changed"):
        sync_state.remember_workflow_snapshot("wf_1", base)
        result = mcp.tools["build_workflow_bulk"](
            "wf_1",
            steps=[StepSpec(ref="finish", type="workflow_end_point", config={})],
            connections=[ConnectionSpec(from_ref="start", to_ref="finish")],
            expected_revision_id=workflow_revision_id(base),
        )

    assert result.conflict is True
    assert "affected scope" in result.error
    assert client.update_calls == []


def test_bulk_ignores_layout_only_change_inside_affected_scope(monkeypatch):
    mcp = DummyMCP()
    client = CanvasClient()
    base = _snapshot(elements=[{
        "element_id": 1,
        "type": "workflow_start_point",
        "resourceID": "form_1",
        "x": 0,
        "y": 0,
    }])
    client.snapshot = _snapshot(
        updated_at="2026-08-31T10:05:00Z",
        elements=[{
            "element_id": 1,
            "type": "workflow_start_point",
            "resourceID": "form_1",
            "x": 640,
            "y": 320,
        }],
    )
    building.register(mcp, client)
    monkeypatch.setattr(revision_log, "capture_workflow_revision", lambda *args, **kwargs: {})
    sync_state.clear_workflow_snapshots()

    with bind_context(session_id="scope-layout"):
        sync_state.remember_workflow_snapshot("wf_1", base)
        result = mcp.tools["build_workflow_bulk"](
            "wf_1",
            steps=[StepSpec(ref="finish", type="workflow_end_point", config={})],
            connections=[ConnectionSpec(from_ref="start", to_ref="finish")],
            expected_revision_id=workflow_revision_id(base),
        )

    assert result.conflict is False
    assert result.error is None
    assert len(client.update_calls) == 1


def test_bulk_rejects_affected_content_race_during_payload_preparation(monkeypatch):
    mcp = DummyMCP()
    client = CanvasClient()
    client.snapshot = _snapshot(elements=[{
        "element_id": 1,
        "type": "workflow_start_point",
        "resourceID": "form_1",
        "name": "Original start",
    }])
    building.register(mcp, client)

    def capture_and_simulate_external_edit(*args, **kwargs):
        client.snapshot = _snapshot(elements=[{
            "element_id": 1,
            "type": "workflow_start_point",
            "resourceID": "form_1",
            "name": "Externally edited while building",
        }])
        return {"snapshot": client.snapshot}

    monkeypatch.setattr(revision_log, "capture_workflow_revision", capture_and_simulate_external_edit)
    base_revision = workflow_revision_id(client.snapshot)

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        steps=[StepSpec(ref="finish", type="workflow_end_point", config={})],
        connections=[ConnectionSpec(from_ref="start", to_ref="finish")],
        expected_revision_id=base_revision,
    )

    assert result.conflict is True
    assert "affected scope while the write was being prepared" in result.error
    assert client.update_calls == []
