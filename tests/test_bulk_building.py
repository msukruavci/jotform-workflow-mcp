import asyncio

from mcp.server import MCPServer

from mcp_server.models import ConnectionSpec, StepSpec
from mcp_server import tree_builder as tb
from mcp_server.tools import building
from mcp_server.ui import create_workflow_apps


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
        self.get_workflow_combined_calls = []
        self.get_form_questions_calls = []

    def get_elements(self, workflow_id):
        return list(self.elements)

    def get_links(self, workflow_id):
        return list(self.links)

    def update_tree(self, workflow_id, *, elements=None, links=None):
        self.update_calls.append({"workflow_id": workflow_id, "elements": elements, "links": links})
        return {}

    def get_workflow_combined(self, workflow_id, *, fetch_essential=True):
        self.get_workflow_combined_calls.append(workflow_id)
        return {
            "workflow": {"id": workflow_id, "title": "Demo"},
            "elements": list(self.elements),
            "links": [],
        }

    def get_form(self, form_id):
        return {"id": form_id, "title": "Trigger Form"}

    def get_form_questions(self, form_id):
        self.get_form_questions_calls.append(form_id)
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


class DecoupledFlowClient(DummyClient):
    def _questions(self):
        return {
            "1": {
                "qid": "1",
                "text": "University Internship Application",
                "type": "control_head",
            },
            "2_email": {
                "qid": "2_email",
                "text": "Student Email",
                "type": "control_email",
                "name": "q2_email",
                "required": "Yes",
            },
            "3_department": {
                "qid": "3_department",
                "text": "Department",
                "type": "control_dropdown",
                "name": "q3_department",
                "required": "No",
                "options": "Engineering|Design|Operations",
            },
        }

    def create_form_with_ai(self, prompt, *, form_type="classic", language="en"):
        self.created_forms.append({"prompt": prompt, "form_type": form_type, "language": language})
        return {
            "resource_id": "form_ai_1",
            "questions": self._questions(),
            "summary": "AI-generated university internship form",
        }

    def get_form_questions(self, form_id):
        self.get_form_questions_calls.append(form_id)
        return self._questions()


def test_create_form_with_ai_returns_complete_exact_field_contract():
    mcp = DummyMCP()
    client = DecoupledFlowClient()
    building.register(mcp, client)

    result = mcp.tools["create_form_with_ai"](
        "Create a university internship application form.",
        language="en",
    )

    assert result.error is None
    assert result.form_id == "form_ai_1"
    assert result.form_url == "https://www.jotform.com/build/form_ai_1"
    assert result.title == "University Internship Application"
    assert result.summary == "AI-generated university internship form"
    assert [field.model_dump() for field in result.fields] == [
        {
            "field_id": "1",
            "label": "University Internship Application",
            "type": "control_head",
            "required": None,
            "options": [],
        },
        {
            "field_id": "2_email",
            "label": "Student Email",
            "type": "control_email",
            "required": "Yes",
            "options": [],
        },
        {
            "field_id": "3_department",
            "label": "Department",
            "type": "control_dropdown",
            "required": "No",
            "options": ["Engineering", "Design", "Operations"],
        },
    ]


def test_decoupled_university_workflow_runs_create_build_show_with_zero_retry():
    mcp = DummyMCP()
    client = DecoupledFlowClient()
    building.register(mcp, client)

    form = mcp.tools["create_form_with_ai"](
        "Create a university internship application form with student email and department."
    )
    email_field_id = next(
        field.field_id for field in form.fields if field.type == "control_email"
    )

    result = mcp.tools["build_workflow_bulk"](
        title="University Internship Review",
        trigger_form_id=form.form_id,
        steps=[
            StepSpec(
                ref="advisor_approval",
                type="workflow_approval",
                config={
                    "approver": "advisor@university.edu",
                    "taskDescription": "Review the internship application.",
                },
            ),
            StepSpec(
                ref="student_notification",
                type="workflow_send_email",
                config={
                    "to": email_field_id,
                    "subject": "Internship application reviewed",
                    "content": "Your internship application has been reviewed.",
                },
            ),
        ],
        connections=[
            ConnectionSpec(from_ref="start", to_ref="advisor_approval"),
            ConnectionSpec(
                from_ref="advisor_approval",
                to_ref="student_notification",
                outcome="Approve",
            ),
        ],
    )

    assert result.error is None
    assert result.workflow_id == "wf_new_1"
    assert result.trigger_form_id == form.form_id
    assert len(client.created_forms) == 1  # The bulk call did not create a second form.
    assert client.get_form_questions_calls == ["form_ai_1"]
    assert client.created_workflows == ["University Internship Review"]
    assert len(client.update_calls) == 1
    email_config = client.update_calls[0]["elements"][1]["data"]
    assert email_config["to"][0]["isQuestion"] is True
    assert email_config["to"][0]["value"] == "{q2_email}"

    ui_server = MCPServer(
        "decoupled-flow-test",
        extensions=[create_workflow_apps(client, html="<html>workflow</html>")],
    )
    preview = asyncio.run(
        ui_server.call_tool("show_workflow", {"workflow_id": result.workflow_id})
    )

    assert preview.structured_content["view"] == "workflow-preview"
    assert preview.structured_content["data"]["workflow_id"] == "wf_new_1"
    assert client.get_workflow_combined_calls == ["wf_new_1", "wf_new_1"]


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
    assert [field.model_dump() for field in result.trigger_form_fields] == [
        {
            "field_id": "1",
            "label": "AI Request Form",
            "type": "control_head",
            "required": None,
            "options": [],
        },
        {
            "field_id": "2_email",
            "label": "Email",
            "type": "control_email",
            "required": None,
            "options": [],
        },
    ]
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


def test_build_workflow_bulk_auto_defaults_common_missing_details_and_aliases():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(
            ref="approval",
            type="workflow_approval",
            config={"approver_email": "boss@company.com"},
        ),
        StepSpec(
            ref="task",
            type="workflow_assign_task",
            config={"assignee_email": "ops@company.com"},
        ),
        StepSpec(
            ref="email",
            type="workflow_send_email",
            config={"recipient_email": {"email": "requester@company.com"}, "body": "Done."},
        ),
    ]
    connections = [
        ConnectionSpec(from_ref="start", to_ref="approval"),
        ConnectionSpec(from_ref="approval", to_ref="task", outcome="Approve"),
        ConnectionSpec(from_ref="task", to_ref="email", outcome="Complete"),
    ]

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)

    assert result.error is None
    assert any("alias 'approver_email' normalized to 'approver'" in warning for warning in result.warnings)
    assert any("auto-filled missing 'taskDescription'" in warning for warning in result.warnings)
    assert any("alias 'body' normalized to 'content'" in warning for warning in result.warnings)

    created = {entry["elementID"]: entry["data"] for entry in client.update_calls[0]["elements"]}
    assert created[2]["approver"][0]["value"] == "boss@company.com"
    assert created[2]["taskDescription"] == "Review Approval and approve or deny it."
    assert created[3]["assignee"][0]["value"] == "ops@company.com"
    assert created[3]["taskDescription"] == "Review Task and complete this task."
    assert created[4]["to"][0]["value"] == "requester@company.com"
    assert created[4]["subject"] == "Email Notification"


def test_build_workflow_bulk_reuses_trigger_form_questions_during_normalization():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(
            ref="approval",
            type="workflow_approval",
            config={
                "name": "Manager Approval",
                "approver": "manager@company.com",
                "taskDescription": "Review the request.",
            },
        ),
        StepSpec(
            ref="branch",
            type="workflow_binary_decision",
            config={
                "name": "Email present?",
                "conditionTerms": [{"field": "Email", "operator": "contains", "value": "@"}],
            },
        ),
        StepSpec(
            ref="email_ok",
            type="workflow_send_email",
            config={
                "name": "Approved Email",
                "to": "{Email}",
                "subject": "Approved",
                "content": "Approved for {Email}.",
            },
        ),
        StepSpec(
            ref="email_no",
            type="workflow_send_email",
            config={
                "name": "Rejected Email",
                "to": "{Email}",
                "subject": "Rejected",
                "content": "Rejected for {Email}.",
            },
        ),
    ]
    connections = [
        ConnectionSpec(from_ref="start", to_ref="approval"),
        ConnectionSpec(from_ref="approval", to_ref="branch", outcome="Approve"),
        ConnectionSpec(from_ref="branch", to_ref="email_ok", outcome="TRUE"),
        ConnectionSpec(from_ref="branch", to_ref="email_no", outcome="FALSE"),
    ]

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)

    assert result.error is None
    # One read discovers trigger form fields, one read captures the revision snapshot.
    assert client.get_workflow_combined_calls == ["wf_1", "wf_1"]
    assert client.get_form_questions_calls == ["form_1"]


def test_build_workflow_bulk_repairs_conditional_branch_with_binary_terms():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(
            ref="branch",
            type="workflow_conditional_branch",
            config={
                "name": "Email present?",
                "conditionTerms": [{"field": "Email", "operator": "contains", "value": "@"}],
            },
        ),
        StepSpec(
            ref="email_ok",
            type="workflow_send_email",
            config={"name": "Email OK", "to": "{Email}", "subject": "OK", "content": "OK"},
        ),
        StepSpec(
            ref="email_no",
            type="workflow_send_email",
            config={"name": "Email Missing", "to": "{Email}", "subject": "Missing", "content": "Missing"},
        ),
    ]
    connections = [
        ConnectionSpec(from_ref="start", to_ref="branch"),
        ConnectionSpec(from_ref="branch", to_ref="email_ok", outcome="TRUE"),
        ConnectionSpec(from_ref="branch", to_ref="email_no", outcome="FALSE"),
    ]

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)

    assert result.error is None
    assert any("normalized to 'workflow_binary_decision'" in warning for warning in result.warnings)
    created = {entry["elementID"]: entry["data"] for entry in client.update_calls[0]["elements"]}
    assert created[2]["type"] == "workflow_binary_decision"


def test_build_workflow_bulk_recovers_form_prompt_only_with_standard_draft():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        title="Fast Draft",
        form_prompt="Create a request form with requester email.",
    )

    assert result.error is None
    assert result.workflow_id == "wf_new_1"
    assert result.trigger_form_id == "form_ai_1"
    assert result.created_steps == {
        "approval_1": "2",
        "email_approved": "3",
        "email_rejected": "4",
        "end_1": "5",
    }
    assert result.created_links_count == 5
    assert any("No steps were provided" in warning for warning in result.warnings)
    assert client.created_forms
    assert client.created_workflows == ["Fast Draft"]


def test_build_workflow_bulk_normalizes_condition_field_labels_after_ai_form_creation():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(
            ref="decision_1",
            type="workflow_binary_decision",
            config={
                "name": "Has email?",
                "conditionTerms": [{
                    "field": "Email",
                    "operator": "isFilled",
                }],
            },
        ),
        StepSpec(
            ref="notify_1",
            type="workflow_send_email",
            config={"to": "user@company.com", "subject": "Ok", "content": "Has email."},
        ),
        StepSpec(
            ref="notify_2",
            type="workflow_send_email",
            config={"to": "user@company.com", "subject": "Missing", "content": "Missing email."},
        ),
    ]
    connections = [
        ConnectionSpec(from_ref="start", to_ref="decision_1"),
        ConnectionSpec(from_ref="decision_1", to_ref="notify_1", outcome="TRUE"),
        ConnectionSpec(from_ref="decision_1", to_ref="notify_2", outcome="FALSE"),
    ]

    result = mcp.tools["build_workflow_bulk"](
        steps=steps,
        connections=connections,
        title="Email Check Workflow",
        form_prompt="Create a form with an Email field.",
    )

    assert result.error is None
    decision = client.update_calls[0]["elements"][0]["data"]
    assert decision["conditionTerms"][0]["field"] == "2_email"
    assert any("Normalized condition field references" in warning for warning in result.warnings)


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


def test_build_workflow_bulk_prefers_trigger_form_id_over_form_prompt_fallback():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        title="Deterministic Trigger Workflow",
        trigger_form_id="form_existing_1",
        form_prompt="This compatibility fallback must be ignored.",
        steps=[
            StepSpec(
                ref="notify_1",
                type="workflow_send_email",
                config={"to": "2_email", "subject": "Received", "content": "Received."},
            ),
        ],
        connections=[ConnectionSpec(from_ref="start", to_ref="notify_1")],
    )

    assert result.error is None
    assert result.trigger_form_id == "form_existing_1"
    assert client.created_forms == []
    assert client.get_form_questions_calls == ["form_existing_1"]
    assert any("used trigger_form_id" in warning for warning in result.warnings)


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
    assert "form_prompt as a standalone" in result.error


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


def test_build_workflow_bulk_numeric_step_ref_rejected():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(ref="8", type="workflow_send_email", config={"to": "a@b.com", "subject": "S", "content": "C"}),
    ]

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=[])

    assert result.error
    assert "Numeric refs are reserved for existing Jotform step IDs" in result.error
    assert client.update_calls == []


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


def test_build_workflow_bulk_can_insert_after_existing_branch_and_reuse_existing_end():
    mcp = DummyMCP()
    client = DummyClient()
    client.elements = [
        {
            "element_id": 1,
            "type": "workflow_start_point",
            "position": {"x": 0, "y": 0},
            "resourceID": "form_1",
            "resourceType": "FORM",
        },
        {
            "element_id": 4,
            "type": "workflow_assign_task",
            "position": {"x": 100, "y": 200},
            "assignee": "it@company.com",
            "taskDescription": "Prepare IT equipment and accounts.",
            "outcomes": [{"outcomeID": 1, "text": "Tamamlandı", "linkID": 10}],
        },
        {
            "element_id": 7,
            "type": "workflow_end_point",
            "position": {"x": 100, "y": 400},
        },
    ]
    client.links = [{"link_id": 10, "fromElement": 4, "toElement": 7}]
    building.register(mcp, client)

    steps = [
        StepSpec(
            ref="finance_prep",
            type="workflow_assign_task",
            config={
                "assignee": "finance@company.com",
                "taskDescription": "Prepare payroll and benefits.",
                "outcomes": ["Tamamlandı"],
            },
        ),
        StepSpec(
            ref="notify_employee",
            type="workflow_send_email",
            config={
                "to": "{3}",
                "subject": "Onboarding hazırlıkları tamamlandı",
                "content": "Onboarding hazırlıklarınız tamamlandı.",
            },
        ),
    ]
    connections = [
        ConnectionSpec(from_ref="4", to_ref="finance_prep", outcome="Tamamlandı"),
        ConnectionSpec(from_ref="finance_prep", to_ref="notify_employee", outcome="Tamamlandı"),
        ConnectionSpec(from_ref="notify_employee", to_ref="7"),
    ]

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)

    assert result.error is None
    assert result.created_steps == {"finance_prep": "8", "notify_employee": "9"}
    assert result.created_links_count == 3
    call = client.update_calls[0]
    assert {"action": "delete", "linkID": 10, "data": {"link_id": 10}} in call["links"]
    assert any(link["action"] == "create" and link["data"]["fromElement"] == "4" for link in call["links"])
    existing_update = next(e for e in call["elements"] if e["action"] == "update" and e["elementID"] == "4")
    assert existing_update["data"]["outcomes"][0]["linkID"] == 11
    end_update = next(e for e in call["elements"] if e["action"] == "update" and e["elementID"] == "7")
    email_create = next(e for e in call["elements"] if e["action"] == "create" and e["elementID"] == 9)
    assert end_update["data"]["x"] == email_create["data"]["x"]
    assert end_update["data"]["y"] > email_create["data"]["y"]
    assert any("Rewired outcome 'Tamamlandı'" in warning for warning in result.warnings)


def test_build_workflow_bulk_rejects_missing_existing_numeric_ref_before_writing():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(ref="notify", type="workflow_send_email", config={"to": "a@b.com", "subject": "S", "content": "C"}),
    ]
    connections = [
        ConnectionSpec(from_ref="404", to_ref="notify"),
    ]

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)

    assert result.error
    assert "Connection from_ref '404' does not exist" in result.error
    assert client.update_calls == []


def test_build_workflow_bulk_rejects_duplicate_connection_from_same_existing_outcome():
    mcp = DummyMCP()
    client = DummyClient()
    client.elements = [
        {
            "element_id": 1,
            "type": "workflow_start_point",
            "position": {"x": 0, "y": 0},
            "resourceID": "form_1",
            "resourceType": "FORM",
        },
        {
            "element_id": 4,
            "type": "workflow_assign_task",
            "position": {"x": 100, "y": 200},
            "assignee": "it@company.com",
            "taskDescription": "Prepare IT equipment.",
            "outcomes": [{"outcomeID": 1, "text": "Done", "linkID": 3}],
        },
        {"element_id": 7, "type": "workflow_end_point", "position": {"x": 100, "y": 400}},
    ]
    client.links = [{"link_id": 3, "fromElement": 4, "toElement": 7}]
    building.register(mcp, client)

    steps = [
        StepSpec(ref="mail_a", type="workflow_send_email", config={"to": "a@b.com", "subject": "A", "content": "A"}),
        StepSpec(ref="mail_b", type="workflow_send_email", config={"to": "b@b.com", "subject": "B", "content": "B"}),
    ]
    connections = [
        ConnectionSpec(from_ref="4", to_ref="mail_a", outcome="Done"),
        ConnectionSpec(from_ref="4", to_ref="mail_b", outcome="Done"),
    ]

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)

    assert result.error
    assert "already used in this bulk update" in result.error
    assert client.update_calls == []


def test_build_workflow_bulk_unconnected_new_step_rejected_before_writing():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    steps = [
        StepSpec(ref="mail", type="workflow_send_email", config={"to": "a@b.com", "subject": "S", "content": "C"}),
        StepSpec(ref="unused_end", type="workflow_end_point", config={"name": "Unused"}),
    ]
    connections = [
        ConnectionSpec(from_ref="start", to_ref="mail"),
    ]

    result = mcp.tools["build_workflow_bulk"]("wf_1", steps=steps, connections=connections)

    assert result.error
    assert "Unconnected step refs" in result.error
    assert "unused_end" in result.error
    assert client.update_calls == []


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


def test_build_workflow_bulk_with_delete_step_ids_only():
    mcp = DummyMCP()
    client = DummyClient()
    client.elements = [
        {"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}},
        {"element_id": 8, "type": "workflow_assign_task", "name": "Old Task", "position": {"x": 0, "y": 100}},
        {"element_id": 9, "type": "workflow_send_email", "name": "Old Mail", "position": {"x": 0, "y": 200}},
        {"element_id": 7, "type": "workflow_end_point", "position": {"x": 0, "y": 300}},
    ]
    client.links = [
        {"link_id": 101, "fromElement": 1, "toElement": 8},
        {"link_id": 102, "fromElement": 8, "toElement": 9},
        {"link_id": 103, "fromElement": 9, "toElement": 7},
    ]
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"]("wf_1", delete_step_ids=["8", "9"])

    assert result.error is None
    assert result.deleted_steps == ["8", "9"]
    assert len(client.update_calls) == 1
    call = client.update_calls[0]
    assert {"action": "delete", "elementID": "8", "data": {"element_id": "8"}} in call["elements"]
    assert {"action": "delete", "elementID": "9", "data": {"element_id": "9"}} in call["elements"]
    assert {"action": "delete", "linkID": 101, "data": {"link_id": 101}} in call["links"]
    assert {"action": "delete", "linkID": 102, "data": {"link_id": 102}} in call["links"]
    assert {"action": "delete", "linkID": 103, "data": {"link_id": 103}} in call["links"]


def test_build_workflow_bulk_replaces_steps_with_delete_and_create_in_one_shot():
    mcp = DummyMCP()
    client = DummyClient()
    client.elements = [
        {"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}, "resourceID": "form_1", "resourceType": "FORM"},
        {
            "element_id": 4,
            "type": "workflow_approval",
            "approver": "boss@co.com",
            "taskDescription": "Review",
            "outcomes": [{"outcomeID": 1, "name": "Approve", "linkID": 201}],
            "position": {"x": 0, "y": 100},
        },
        {"element_id": 8, "type": "workflow_send_email", "name": "Old Approve Mail", "position": {"x": -100, "y": 200}},
        {"element_id": 7, "type": "workflow_end_point", "position": {"x": 0, "y": 300}},
    ]
    client.questions = {"3": {"qid": "3", "type": "control_email", "text": "Email"}}
    client.links = [
        {"link_id": 200, "fromElement": 1, "toElement": 4},
        {"link_id": 201, "fromElement": 4, "toElement": 8},
        {"link_id": 202, "fromElement": 8, "toElement": 7},
    ]
    building.register(mcp, client)

    steps = [
        StepSpec(
            ref="new_mail",
            type="workflow_send_email",
            config={"to": "employee@co.com", "subject": "Approved!", "content": "Your request is approved."},
        ),
    ]
    connections = [
        ConnectionSpec(from_ref="4", to_ref="new_mail", outcome="Approve"),
        ConnectionSpec(from_ref="new_mail", to_ref="7"),
    ]

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        delete_step_ids=["8"],
        steps=steps,
        connections=connections,
    )

    assert result.error is None
    assert result.deleted_steps == ["8"]
    assert result.created_steps == {"new_mail": "9"}
    call = client.update_calls[0]
    # Check element 8 is deleted and new element 9 is created
    assert {"action": "delete", "elementID": "8", "data": {"element_id": "8"}} in call["elements"]
    assert any(e.get("action") == "create" and e.get("elementID") == 9 for e in call["elements"])
    # Check link 201 and 202 are deleted
    assert {"action": "delete", "linkID": 201, "data": {"link_id": 201}} in call["links"]
    assert {"action": "delete", "linkID": 202, "data": {"link_id": 202}} in call["links"]
    # Check new link is created from 4 to 9 with outcome Approve
    assert any(l.get("action") == "create" and l.get("data", {}).get("fromElement") == "4" for l in call["links"])


def test_build_workflow_bulk_rejects_deleting_start_point():
    mcp = DummyMCP()
    client = DummyClient()
    client.elements = [
        {"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}},
    ]
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"]("wf_1", delete_step_ids=["1"])

    assert result.error
    assert "Cannot delete workflow start point" in result.error
    assert client.update_calls == []
