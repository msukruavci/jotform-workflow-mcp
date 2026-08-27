from mcp_server.tools import ui_settings


class DummyMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class DummyClient:
    def __init__(self, element):
        self.element = dict(element)
        self.update_calls = []

    def get_element(self, workflow_id, step_id):
        return dict(self.element)

    def update_tree(self, workflow_id, *, elements=None, links=None):
        self.update_calls.append({"elements": elements, "links": links})
        for update in elements or []:
            self.element.update(update["data"])
        return {}


def register_tool(monkeypatch, element):
    monkeypatch.setattr(
        ui_settings.revision_log,
        "capture_workflow_revision",
        lambda *args, **kwargs: None,
    )
    mcp = DummyMCP()
    client = DummyClient(element)
    ui_settings.register(mcp, client)
    return mcp.tools["update_step_settings"], client


def test_updates_allow_listed_email_settings(monkeypatch):
    tool, client = register_tool(monkeypatch, {
        "element_id": "6",
        "type": "workflow_send_email",
        "name": "Old name",
        "subject": "Old subject",
        "content": "Keep this message",
    })

    result = tool("wf_1", "6", {
        "name": "Approval notification",
        "subject": "Request approved",
    })

    assert result.error is None
    assert result.updated_fields == ["name", "subject"]
    assert result.config["name"] == "Approval notification"
    assert result.config["subject"] == "Request approved"
    assert result.config["content"] == "Keep this message"
    assert client.update_calls[0]["elements"] == [{
        "action": "update",
        "elementID": "6",
        "data": {
            "element_id": "6",
            "name": "Approval notification",
            "subject": "Request approved",
        },
    }]


def test_updates_email_message_html_from_embedded_ui(monkeypatch):
    tool, client = register_tool(monkeypatch, {
        "element_id": "6",
        "type": "workflow_send_email",
        "name": "Email",
        "content": "Original",
    })

    content = '<p style="text-align: center;"><strong>Changed</strong></p>'
    result = tool("wf_1", "6", {"content": content})

    assert result.error is None
    assert result.updated_fields == ["content"]
    assert result.config["content"] == content
    assert client.update_calls[0]["elements"] == [{
        "action": "update",
        "elementID": "6",
        "data": {
            "element_id": "6",
            "content": content,
        },
    }]


def test_updates_email_recipients_with_emails_and_form_fields(monkeypatch):
    tool, client = register_tool(monkeypatch, {
        "element_id": "6",
        "type": "workflow_send_email",
        "name": "Email",
        "to": [{"text": "old@example.com", "value": "old@example.com"}],
    })

    recipients = [
        {"text": "new@example.com", "value": "new@example.com", "isValid": True},
        {"text": "Employee Email", "value": "{q3_email1}", "isQuestion": True},
    ]
    result = tool("wf_1", "6", {"to": recipients})

    assert result.error is None
    assert result.updated_fields == ["to"]
    assert result.config["to"] == recipients
    assert client.update_calls[0]["elements"][0]["data"]["to"] == recipients


def test_rejects_invalid_email_recipient(monkeypatch):
    tool, client = register_tool(monkeypatch, {
        "element_id": "6",
        "type": "workflow_send_email",
        "name": "Email",
        "to": [],
    })

    result = tool("wf_1", "6", {
        "to": [{"text": "not-an-email", "value": "not-an-email", "isValid": False}],
    })

    assert result.error == "Recipient 1 is not a valid email address."
    assert client.update_calls == []


def test_rejects_empty_email_recipient_list(monkeypatch):
    tool, client = register_tool(monkeypatch, {
        "element_id": "6",
        "type": "workflow_send_email",
        "name": "Email",
        "to": [{"text": "old@example.com"}],
    })

    result = tool("wf_1", "6", {"to": []})

    assert result.error == "Recipients cannot be empty."
    assert client.update_calls == []


def test_rejects_unsupported_step_type(monkeypatch):
    tool, client = register_tool(monkeypatch, {
        "element_id": "3",
        "type": "workflow_conditional_branch",
        "name": "Branch",
    })

    result = tool("wf_1", "3", {"name": "Changed"})

    assert "cannot be edited" in result.error
    assert client.update_calls == []


def test_rejects_blank_email_step_name(monkeypatch):
    tool, client = register_tool(monkeypatch, {
        "element_id": "6",
        "type": "workflow_send_email",
        "name": "Email",
    })

    result = tool("wf_1", "6", {"name": "   "})

    assert result.error == "Step name cannot be empty."
    assert client.update_calls == []


def test_skips_network_write_when_value_did_not_change(monkeypatch):
    tool, client = register_tool(monkeypatch, {
        "element_id": "6",
        "type": "workflow_send_email",
        "subject": "Already saved",
    })

    result = tool("wf_1", "6", {"subject": "Already saved"})

    assert result.error is None
    assert result.updated_fields == []
    assert client.update_calls == []
