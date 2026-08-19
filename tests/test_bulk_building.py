from mcp_server.models import ConnectionSpec, StepSpec
from mcp_server.tools import building


class DummyMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


class DummyClient:
    def __init__(self):
        self.elements = [{"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}}]
        self.links = []
        self.update_calls = []

    def get_elements(self, workflow_id):
        return list(self.elements)

    def get_links(self, workflow_id):
        return list(self.links)

    def update_tree(self, workflow_id, *, elements=None, links=None):
        self.update_calls.append({"elements": elements, "links": links})
        return {}

    def get_workflow_combined(self, workflow_id):
        return {
            "workflow": {"id": workflow_id, "title": "Demo"},
            "elements": [{"element_id": 1, "type": "workflow_start_point", "resourceID": "form_1"}],
            "links": [],
        }

    def get_form_questions(self, form_id):
        return {
            "1_name": {"text": "Name", "type": "control_textbox"},
            "2_email": {"text": "Email", "type": "control_email", "name": "q2_email0"},
        }


def test_build_workflow_bulk_linear_chain():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(
            ref="approval_1",
            type="workflow_approval",
            config={"approver": "boss@company.com", "taskDescription": "Review request"},
        ),
        StepSpec(
            ref="notify_1",
            type="workflow_send_email",
            config={"to": "user@company.com", "subject": "Done", "content": "Your request is processed."},
        ),
    ]
    connections = [
        ConnectionSpec(from_ref="start", to_ref="approval_1"),
        ConnectionSpec(from_ref="approval_1", to_ref="notify_1", outcome="Approve"),
    ]

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)

    assert result.error is None
    assert result.workflow_id == "wf_1"
    assert result.created_steps == {"approval_1": "2", "notify_1": "3"}
    assert result.created_links_count == 2
    assert len(client.update_calls) == 1
    call = client.update_calls[0]
    assert len(call["elements"]) == 2
    assert len(call["links"]) == 2


def test_build_workflow_bulk_branching():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(
            ref="approval_1",
            type="workflow_approval",
            config={"approver": "boss@company.com", "taskDescription": "Review request"},
        ),
        StepSpec(
            ref="email_approve",
            type="workflow_send_email",
            config={"to": "user@company.com", "subject": "Approved", "content": "Approved"},
        ),
        StepSpec(
            ref="email_deny",
            type="workflow_send_email",
            config={"to": "user@company.com", "subject": "Rejected", "content": "Rejected"},
        ),
    ]
    connections = [
        ConnectionSpec(from_ref="start", to_ref="approval_1"),
        ConnectionSpec(from_ref="approval_1", to_ref="email_approve", outcome="Approve"),
        ConnectionSpec(from_ref="approval_1", to_ref="email_deny", outcome="Deny"),
    ]

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)

    assert result.error is None
    assert result.created_steps == {
        "approval_1": "2",
        "email_approve": "3",
        "email_deny": "4",
    }
    assert result.created_links_count == 3
    assert len(client.update_calls) == 1

    call = client.update_calls[0]
    elements = call["elements"]
    links = call["links"]

    # Verify link labels
    assert links[1]["data"]["labels"][0]["label"] == "Approve"
    assert links[2]["data"]["labels"][0]["label"] == "Deny"

    # Verify element outcomes have linkID assigned
    approval_elem = next(e for e in elements if e["elementID"] == 2)
    outcomes = approval_elem["data"]["outcomes"]
    approve_out = next(o for o in outcomes if o.get("name") == "Approve" or o.get("type") == "APPROVE" or o.get("text") == "Approve")
    deny_out = next(o for o in outcomes if o.get("name") == "Deny" or o.get("type") == "DENY" or o.get("text") == "Deny")
    assert approve_out.get("linkID") == 2
    assert deny_out.get("linkID") == 3


def test_build_workflow_bulk_custom_string_outcomes():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(
            ref="approval_1",
            type="workflow_approval",
            config={
                "name": "Yönetici Onayı",
                "approver": "manager@company.com",
                "taskDescription": "Review request",
                "outcomes": ["Onayla", "Reddet"],
            },
        ),
        StepSpec(
            ref="approve_email",
            type="workflow_send_email",
            config={"to": "user@company.com", "subject": "Onaylandı", "content": "İzniniz onaylandı."},
        ),
        StepSpec(
            ref="reject_email",
            type="workflow_send_email",
            config={"to": "user@company.com", "subject": "Reddedildi", "content": "İzniniz reddedildi."},
        ),
    ]
    connections = [
        ConnectionSpec(from_ref="start", to_ref="approval_1"),
        ConnectionSpec(from_ref="approval_1", to_ref="approve_email", outcome="Onayla"),
        ConnectionSpec(from_ref="approval_1", to_ref="reject_email", outcome="Reddet"),
    ]

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)

    assert result.error is None
    assert result.created_steps == {
        "approval_1": "2",
        "approve_email": "3",
        "reject_email": "4",
    }
    assert result.created_links_count == 3
    assert len(client.update_calls) == 1

    call = client.update_calls[0]
    elements = call["elements"]
    links = call["links"]

    # Verify custom outcome labels on links
    assert links[1]["data"]["labels"][0]["label"] == "Onayla"
    assert links[2]["data"]["labels"][0]["label"] == "Reddet"

    # Verify element outcomes are valid objects with text and linkID
    approval_elem = next(e for e in elements if e["elementID"] == 2)
    outcomes = approval_elem["data"]["outcomes"]
    assert isinstance(outcomes[0], dict)
    assert outcomes[0]["text"] == "Onayla"
    assert outcomes[0]["linkID"] == 2
    assert isinstance(outcomes[1], dict)
    assert outcomes[1]["text"] == "Reddet"
    assert outcomes[1]["linkID"] == 3


def test_build_workflow_bulk_empty_steps_rejected():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=[], connections=[])
    assert result.error
    assert "No steps provided" in result.error


def test_build_workflow_bulk_duplicate_ref_rejected():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(ref="step_1", type="workflow_send_email", config={"to": "a@b.com", "subject": "S", "content": "C"}),
        StepSpec(ref="step_1", type="workflow_send_email", config={"to": "a@b.com", "subject": "S", "content": "C"}),
    ]
    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=[])
    assert result.error
    assert "Duplicate step ref" in result.error


def test_build_workflow_bulk_invalid_connection_ref_rejected():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(ref="step_1", type="workflow_send_email", config={"to": "a@b.com", "subject": "S", "content": "C"}),
    ]
    connections = [
        ConnectionSpec(from_ref="non_existent", to_ref="step_1"),
    ]
    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)
    assert result.error
    assert "Connection from_ref 'non_existent' is invalid" in result.error


def test_build_workflow_bulk_missing_outcome_on_branching_rejected():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(ref="appr", type="workflow_approval", config={"approver": "a@b.com", "taskDescription": "D"}),
        StepSpec(ref="mail", type="workflow_send_email", config={"to": "a@b.com", "subject": "S", "content": "C"}),
    ]
    connections = [
        ConnectionSpec(from_ref="appr", to_ref="mail", outcome=""),
    ]
    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)
    assert result.error
    assert "branching step and requires an outcome" in result.error
