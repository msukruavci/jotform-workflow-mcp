from mcp_server import revision_log


class FakeClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.elements = {}

    def get_workflow_combined(self, workflow_id):
        return self.snapshot

    def get_element(self, workflow_id, element_id):
        return self.elements.get(str(element_id), {})


def test_capture_and_list_revisions(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_REVISION_LOG_DIR", str(tmp_path))
    client = FakeClient({
        "workflow": {"id": "wf_1", "title": "Demo"},
        "elements": [{"element_id": 1}, {"element_id": 2}],
        "links": [{"link_id": 1}],
    })

    first = revision_log.capture_workflow_revision(client, "wf_1", "before add")
    second = revision_log.capture_workflow_revision(client, "wf_1", "before update")

    summaries = revision_log.list_workflow_revisions("wf_1")

    assert [s["revision_id"] for s in summaries] == [
        second["revision_id"],
        first["revision_id"],
    ]
    assert summaries[0]["workflow_url"] == "https://www.jotform.com/workflow/wf_1/build"
    assert summaries[0]["step_count"] == 2
    assert summaries[0]["link_count"] == 1


def test_capture_hydrates_full_element_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_REVISION_LOG_DIR", str(tmp_path))
    client = FakeClient({
        "workflow": {"id": "wf_1", "title": "Demo"},
        "elements": [{"element_id": "7", "type": "workflow_send_email", "subject": "Hi"}],
        "links": [],
    })
    client.elements["7"] = {
        "element_id": "7",
        "type": "workflow_send_email",
        "subject": "Hi",
        "content": "Full email body",
        "to": [{"value": "{q3_email1}", "isQuestion": True}],
    }

    record = revision_log.capture_workflow_revision(client, "wf_1", "before email update")

    element = record["snapshot"]["elements"][0]
    assert element["subject"] == "Hi"
    assert element["content"] == "Full email body"
    assert element["to"][0]["value"] == "{q3_email1}"


def test_load_latest_or_specific_revision(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_REVISION_LOG_DIR", str(tmp_path))
    client = FakeClient({"workflow": {"id": "wf_1"}, "elements": [], "links": []})
    first = revision_log.capture_workflow_revision(client, "wf_1", "first")
    second = revision_log.capture_workflow_revision(client, "wf_1", "second")

    assert revision_log.load_workflow_revision("wf_1")["revision_id"] == second["revision_id"]
    assert revision_log.load_workflow_revision("wf_1", first["revision_id"])["reason"] == "first"


def test_build_restore_payloads_deletes_current_links_then_restores_target():
    current = {
        "elements": [{"element_id": 1, "type": "start"}, {"element_id": 3, "type": "old"}],
        "links": [{"link_id": 7, "fromElement": 1, "toElement": 3}],
    }
    target = {
        "elements": [{"element_id": 1, "type": "start"}, {"element_id": 2, "type": "new"}],
        "links": [{"link_id": 5, "fromElement": 1, "toElement": 2}],
    }

    link_deletes, element_writes, link_creates = revision_log.build_restore_payloads(
        current, target
    )

    assert link_deletes == [{"action": "delete", "linkID": "7", "data": {"link_id": "7"}}]
    assert {"action": "delete", "elementID": "3", "data": {"element_id": "3"}} in element_writes
    assert {"action": "update", "elementID": "1", "data": {"element_id": "1", "type": "start"}} in element_writes
    assert {"action": "create", "elementID": "2", "data": {"element_id": "2", "type": "new"}} in element_writes
    assert link_creates == [
        {
            "action": "create",
            "linkID": "5",
            "data": {"link_id": "5", "fromElement": 1, "toElement": 2},
        }
    ]
