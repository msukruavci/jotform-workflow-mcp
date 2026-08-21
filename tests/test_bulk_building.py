from mcp_server.models import ConnectionSpec, StepSpec
from mcp_server import tree_builder as tb
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
        self.elements = [{
            "element_id": 1,
            "type": "workflow_start_point",
            "position": {"x": 0, "y": 0},
            "resourceID": "form_1",
            "resourceType": "FORM",
        }]
        self.links = []
        self.update_calls = []
        self.created_forms = []
        self.created_workflows = []
        self.bound_trigger_forms = []

    def get_elements(self, workflow_id):
        return list(self.elements)

    def get_links(self, workflow_id):
        return list(self.links)

    def update_tree(self, workflow_id, *, elements=None, links=None):
        self.update_calls.append({"workflow_id": workflow_id, "elements": elements, "links": links})
        return {}

    def get_workflow_combined(self, workflow_id):
        return {
            "workflow": {"id": workflow_id, "title": "Demo"},
            "elements": list(self.elements),
            "links": [],
        }

    def get_form_questions(self, form_id):
        return {
            "1": {"qid": "1", "text": "Request Form", "type": "control_head"},
            "1_name": {"text": "Name", "type": "control_textbox"},
            "2_email": {"text": "Email", "type": "control_email", "name": "q2_email0"},
        }

    def create_form_with_ai(self, prompt, *, form_type="classic", language="en"):
        self.created_forms.append({"prompt": prompt, "form_type": form_type, "language": language})
        return {
            "resource_id": "form_ai_1",
            "questions": {
                "1": {"qid": "1", "text": "AI Request Form", "type": "control_head"},
                "2_email": {"qid": "2_email", "text": "Email", "type": "control_email", "name": "q2_email"},
            },
            "summary": "AI generated form",
        }

    def create_workflow(self, title):
        self.created_workflows.append(title)
        self.elements = [{"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}}]
        return {"id": "wf_new_1"}

    def set_trigger_form(self, workflow_id, form_id):
        self.bound_trigger_forms.append((workflow_id, form_id))
        self.elements[0] = {**self.elements[0], "resourceID": form_id, "resourceType": "FORM"}
        return {}

    def get_element(self, workflow_id, element_id):
        assert str(element_id) == "1"
        return self.elements[0]


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
    assert client.created_workflows == []
    assert client.created_forms == []


def test_build_workflow_bulk_creates_workflow_with_ai_form_when_workflow_id_omitted():
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

    result = mcp.tools["build_workflow_bulk"](
        steps=steps,
        connections=connections,
        title="Access Request Workflow",
        form_prompt="Create a trigger form for access requests with requester email.",
        form_language="tr",
    )

    assert result.error is None
    assert result.workflow_id == "wf_new_1"
    assert result.workflow_url == "https://www.jotform.com/workflow/wf_new_1/build"
    assert result.trigger_form_id == "form_ai_1"
    assert result.trigger_form_url == "https://www.jotform.com/build/form_ai_1"
    assert result.created_steps == {"approval_1": "2", "notify_1": "3"}
    assert result.created_links_count == 2
    assert client.created_forms == [{
        "prompt": "Create a trigger form for access requests with requester email.",
        "form_type": "classic",
        "language": "tr",
    }]
    assert client.created_workflows == ["Access Request Workflow"]
    assert client.bound_trigger_forms == [("wf_new_1", "form_ai_1")]
    assert client.update_calls[0]["workflow_id"] == "wf_new_1"


def test_build_workflow_bulk_creates_workflow_with_existing_trigger_form():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(
            ref="notify_1",
            type="workflow_send_email",
            config={"to": "user@company.com", "subject": "Received", "content": "We received it."},
        ),
    ]
    connections = [ConnectionSpec(from_ref="start", to_ref="notify_1")]

    result = mcp.tools["build_workflow_bulk"](
        steps=steps,
        connections=connections,
        title="Existing Trigger Workflow",
        trigger_form_id="form_existing_1",
    )

    assert result.error is None
    assert result.workflow_id == "wf_new_1"
    assert result.trigger_form_id == "form_existing_1"
    assert client.created_forms == []
    assert client.created_workflows == ["Existing Trigger Workflow"]
    assert client.bound_trigger_forms == [("wf_new_1", "form_existing_1")]


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


def test_build_workflow_bulk_uses_layered_layout_for_nested_branches():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(
            ref="approval",
            type="workflow_approval",
            config={"approver": "boss@company.com", "taskDescription": "Approve access"},
        ),
        StepSpec(
            ref="provision_task",
            type="workflow_assign_task",
            config={
                "assignee": "it@company.com",
                "taskDescription": "Provision requested access",
                "outcomes": ["Provisioned", "Unable to Provision"],
            },
        ),
        StepSpec(
            ref="denied_email",
            type="workflow_send_email",
            config={"to": "user@company.com", "subject": "Denied", "content": "Access denied."},
        ),
        StepSpec(
            ref="success_email",
            type="workflow_send_email",
            config={"to": "user@company.com", "subject": "Ready", "content": "Access provisioned."},
        ),
        StepSpec(
            ref="failure_email",
            type="workflow_send_email",
            config={"to": "user@company.com", "subject": "Failed", "content": "Access could not be provisioned."},
        ),
    ]
    connections = [
        ConnectionSpec(from_ref="start", to_ref="approval"),
        ConnectionSpec(from_ref="approval", to_ref="provision_task", outcome="Approve"),
        ConnectionSpec(from_ref="approval", to_ref="denied_email", outcome="Deny"),
        ConnectionSpec(from_ref="provision_task", to_ref="success_email", outcome="Provisioned"),
        ConnectionSpec(from_ref="provision_task", to_ref="failure_email", outcome="Unable to Provision"),
    ]

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)

    assert result.error is None
    approval = next(e["data"] for e in client.update_calls[0]["elements"] if e["elementID"] == 2)
    provision_task = next(e["data"] for e in client.update_calls[0]["elements"] if e["elementID"] == 3)
    denied_email = next(e["data"] for e in client.update_calls[0]["elements"] if e["elementID"] == 4)
    success_email = next(e["data"] for e in client.update_calls[0]["elements"] if e["elementID"] == 5)
    failure_email = next(e["data"] for e in client.update_calls[0]["elements"] if e["elementID"] == 6)

    assert approval["y"] == tb.STEP_Y
    assert provision_task["y"] == denied_email["y"] == tb.STEP_Y * 2
    assert success_email["y"] == failure_email["y"] == tb.STEP_Y * 3
    assert provision_task["x"] < denied_email["x"]
    assert success_email["x"] < failure_email["x"]


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
