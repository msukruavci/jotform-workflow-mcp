from mcp_server.tools import risky


class DummyMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


class DeleteClient:
    def __init__(self):
        self.elements = [
            {"element_id": 1, "type": "workflow_start_point"},
            {
                "element_id": 2,
                "type": "workflow_binary_decision",
                "outcomes": [{"outcomeID": 1, "conditionValue": "TRUE", "linkID": 5}],
            },
            {"element_id": 3, "type": "workflow_send_email", "name": "Email"},
        ]
        self.links = [{"link_id": 5, "fromElement": 2, "toElement": 3}]

    def get_element(self, workflow_id, step_id):
        for element in self.elements:
            if str(element.get("element_id")) == str(step_id):
                return element
        return {"element_id": step_id}

    def get_elements(self, workflow_id):
        return list(self.elements)

    def get_links(self, workflow_id):
        return list(self.links)

    def update_tree(self, workflow_id, *, elements=None, links=None):
        for entry in elements or []:
            if entry.get("action") == "delete":
                element_id = str(entry.get("elementID"))
                self.elements = [
                    item for item in self.elements
                    if str(item.get("element_id")) != element_id
                ]
            if entry.get("action") == "update":
                element_id = str(entry.get("elementID"))
                for item in self.elements:
                    if str(item.get("element_id")) == element_id:
                        item.update(entry.get("data") or {})

        for entry in links or []:
            if entry.get("action") == "delete":
                link_id = str(entry.get("linkID"))
                self.links = [
                    item for item in self.links
                    if str(item.get("link_id")) != link_id
                ]


class PublishPreviewClient:
    def __init__(self):
        self.published = False

    def get_workflow_combined(self, workflow_id):
        return {
            "workflow": {"id": workflow_id, "title": "Demo"},
            "elements": [
                {"element_id": 1, "type": "workflow_start_point"},
                {
                    "element_id": 2,
                    "type": "workflow_binary_decision",
                    "conditionTerms": [{"field": "2_status", "operator": "equals", "value": "Yes"}],
                    "outcomes": [
                        {"outcomeID": 1, "conditionValue": "TRUE"},
                        {"outcomeID": 2, "conditionValue": "FALSE", "linkID": 99},
                    ],
                },
                {"element_id": 3, "type": "workflow_send_email"},
            ],
            "links": [{"link_id": 7, "fromElement": 2, "toElement": 3}],
        }

    def publish_workflow(self, workflow_id):
        self.published = True
        return {"live": "1"}


def test_delete_step_verifies_step_links_and_branch_outcome_cleanup(monkeypatch):
    monkeypatch.setattr(
        risky.revision_log,
        "capture_workflow_revision",
        lambda *args, **kwargs: {"revision_id": "rev_1"},
    )
    mcp = DummyMCP()
    client = DeleteClient()
    risky.register(mcp, client)

    result = mcp.tools["delete_step"]("wf_1", "3", confirm=True)

    assert result.error is None
    assert result.deleted is True
    assert result.verified is True
    assert client.links == []
    source = client.get_element("wf_1", "2")
    assert source["outcomes"][0]["linkID"] is None


def test_delete_step_reports_incomplete_verify_when_link_survives(monkeypatch):
    monkeypatch.setattr(
        risky.revision_log,
        "capture_workflow_revision",
        lambda *args, **kwargs: {"revision_id": "rev_1"},
    )
    mcp = DummyMCP()
    client = DeleteClient()

    original_update_tree = client.update_tree

    def update_without_deleting_links(workflow_id, *, elements=None, links=None):
        original_update_tree(workflow_id, elements=elements, links=[])

    client.update_tree = update_without_deleting_links
    risky.register(mcp, client)

    result = mcp.tools["delete_step"]("wf_1", "3", confirm=True)

    assert result.error
    assert result.verified is False
    assert "did not persist completely" in result.error


def test_publish_reports_branch_diagnostics_and_publishes(monkeypatch):
    monkeypatch.setattr(
        risky.revision_log,
        "capture_workflow_revision",
        lambda *args, **kwargs: {"revision_id": "rev_1"},
    )
    mcp = DummyMCP()
    client = PublishPreviewClient()
    risky.register(mcp, client)

    result = mcp.tools["publish_workflow"]("wf_1")

    assert result.needs_confirmation is False
    assert result.published is True
    assert client.published is True
    assert any("unconnected branch outcome" in warning for warning in result.health_warnings)
    assert any("unlabelled branching link" in warning for warning in result.health_warnings)
    assert any("invalid branch mapping" in warning for warning in result.health_warnings)
