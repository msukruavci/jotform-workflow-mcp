import pytest

from mcp_server import revision_log
from mcp_server.jotform_client import (
    ConflictError,
    JotformClient,
    workflow_revision_id,
)
from mcp_server.models import (
    ConnectionSpec,
    StepSpec,
    WorkflowCanvasConnectionUpdate,
    WorkflowCanvasDiff,
)
from mcp_server.tools import building


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


def test_existing_bulk_update_requires_fresh_revision_token(monkeypatch):
    mcp = DummyMCP()
    client = CanvasClient()
    building.register(mcp, client)
    monkeypatch.setattr(revision_log, "capture_workflow_revision", lambda *args, **kwargs: {})

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        steps=[StepSpec(ref="finish", type="workflow_end_point", config={})],
        connections=[ConnectionSpec(from_ref="start", to_ref="finish")],
    )

    assert result.conflict is True
    assert "expected_revision_id" in result.error
    assert "get_workflow" in result.hint
    assert "do not call build_workflow_bulk again until the user confirms" in result.hint
    assert client.update_calls == []


class CanvasClient:
    def __init__(self):
        self.snapshot = _snapshot()
        self.update_calls = []
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


def test_canvas_diff_is_applied_in_one_revision_checked_write(monkeypatch):
    mcp = DummyMCP()
    client = CanvasClient()
    building.register(mcp, client)
    monkeypatch.setattr(revision_log, "capture_workflow_revision", lambda *args, **kwargs: {})
    base_revision = workflow_revision_id(client.snapshot)

    result = mcp.tools["apply_workflow_canvas_diff"](
        "wf_1",
        WorkflowCanvasDiff(
            added_steps=[StepSpec(ref="finish", type="workflow_end_point", config={})],
            updated_connections=[WorkflowCanvasConnectionUpdate(from_ref="start", to_ref="finish")],
            base_revision_id=base_revision,
        ),
    )

    assert result.error is None
    assert result.applied is True
    assert result.revision_id != base_revision
    assert result.added_steps == {"finish": "2"}
    assert len(client.update_calls) == 1
    assert len(client.update_calls[0]["elements"]) == 1
    assert len(client.update_calls[0]["links"]) == 1


def test_canvas_diff_returns_clean_conflict_result(monkeypatch):
    mcp = DummyMCP()
    client = CanvasClient()
    building.register(mcp, client)
    monkeypatch.setattr(revision_log, "capture_workflow_revision", lambda *args, **kwargs: {})

    result = mcp.tools["apply_workflow_canvas_diff"](
        "wf_1",
        WorkflowCanvasDiff(
            added_steps=[StepSpec(ref="finish", type="workflow_end_point", config={})],
            updated_connections=[WorkflowCanvasConnectionUpdate(from_ref="start", to_ref="finish")],
            base_revision_id="sha256:stale",
        ),
    )

    assert result.applied is False
    assert result.conflict is True
    assert result.revision_id is None
    assert result.updated_at is None
    assert "Reload" in result.hint
    assert "retry" not in result.error.lower()
    assert client.update_calls == []


def test_bulk_conflict_is_detected_before_expensive_graph_reads(monkeypatch):
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
    assert "Stop after reporting this conflict" in result.hint
    assert "ask the user" in result.hint
    assert result.current_revision_id is None
    assert result.current_updated_at is None
    assert "retry" not in result.error.lower()
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


def test_canvas_connection_replacement_deletes_and_creates_in_same_write(monkeypatch):
    mcp = DummyMCP()
    client = CanvasClient()
    client.snapshot = _snapshot(
        elements=client.snapshot["elements"] + [
            {"element_id": 2, "type": "workflow_end_point"}
        ],
        links=[{"link_id": 5, "fromElement": 1, "toElement": 2}],
    )
    building.register(mcp, client)
    monkeypatch.setattr(revision_log, "capture_workflow_revision", lambda *args, **kwargs: {})

    result = mcp.tools["apply_workflow_canvas_diff"](
        "wf_1",
        WorkflowCanvasDiff(
            updated_connections=[WorkflowCanvasConnectionUpdate(
                action="upsert",
                link_id="5",
                from_ref="1",
                to_ref="2",
            )],
            base_revision_id=workflow_revision_id(client.snapshot),
        ),
    )

    assert result.error is None
    assert result.updated_connections_count == 1
    assert len(client.update_calls) == 1
    assert [item["action"] for item in client.update_calls[0]["links"]] == [
        "delete",
        "create",
    ]
