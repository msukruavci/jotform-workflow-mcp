import asyncio
from datetime import datetime, timezone

from mcp.server import MCPServer

from mcp_server.jotform_client import JotformAPIError, PartialWorkflowCreateError
from mcp_server.models import ConnectionSpec, StepSpec, StepUpdateSpec
from mcp_server import tree_builder as tb
from mcp_server.tools import building
from mcp_server.ui import create_workflow_apps


class DummyMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            def wrapped(*args, **kwargs):
                if fn.__name__ == "build_workflow_bulk":
                    workflow_id = kwargs.get("workflow_id") or (args[0] if args else "")
                    if workflow_id and not kwargs.get("expected_revision_id") and not kwargs.get("base_updated_at"):
                        kwargs["expected_revision_id"] = "test-revision"
                return fn(*args, **kwargs)

            self.tools[fn.__name__] = wrapped
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
        self.status_updates = []
        self.events = []

    def get_elements(self, workflow_id):
        return list(self.elements)

    def get_links(self, workflow_id):
        return list(self.links)

    def update_tree(
        self,
        workflow_id,
        *,
        elements=None,
        links=None,
        expected_revision_id=None,
        base_updated_at=None,
    ):
        self.events.append(("update_tree", workflow_id))
        self.update_calls.append({"workflow_id": workflow_id, "elements": elements, "links": links})
        return {}

    def get_workflow_combined(self, workflow_id, *, fetch_essential=True):
        self.get_workflow_combined_calls.append(workflow_id)
        return {
            "workflow": {"id": workflow_id, "title": "Demo"},
            "elements": list(self.elements),
            "links": list(self.links),
        }

    def assert_workflow_revision(
        self,
        workflow_id,
        *,
        expected_revision_id=None,
        base_updated_at=None,
    ):
        return {
            "revision_id": expected_revision_id,
            "updated_at": base_updated_at,
            "snapshot": self.get_workflow_combined(workflow_id),
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

    def create_workflow(self, title, **kwargs):
        self.created_workflows.append(title)
        self.elements = [{"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}}]
        return {"id": "wf_new_1"}

    def update_workflow_metadata(self, workflow_id, **fields):
        self.events.append(("status", workflow_id, fields))
        self.status_updates.append((workflow_id, fields))
        return {"status": fields.get("status")}

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
    assert result.next_required_tool == "build_workflow_bulk"
    assert "trigger_form_id='form_ai_1'" in result.hint
    assert [field.model_dump() for field in result.fields] == [
        {
            "field_id": "1",
            "name": "1",
            "label": "University Internship Application",
            "type": "control_head",
            "required": None,
            "options": [],
        },
        {
            "field_id": "2_email",
            "name": "q2_email",
            "label": "Student Email",
            "type": "control_email",
            "required": "Yes",
            "options": [],
        },
        {
            "field_id": "3_department",
            "name": "q3_department",
            "label": "Department",
            "type": "control_dropdown",
            "required": "No",
            "options": ["Engineering", "Design", "Operations"],
        },
    ]


def test_create_form_with_ai_strips_stop_after_form_prompt():
    mcp = DummyMCP()

    class StopSummaryClient(DecoupledFlowClient):
        def create_form_with_ai(self, prompt, *, form_type="classic", language="en"):
            response = super().create_form_with_ai(prompt, form_type=form_type, language=language)
            response["summary"] = "AI-generated university internship form. What do you want to do next?"
            return response

    client = StopSummaryClient()
    building.register(mcp, client)

    result = mcp.tools["create_form_with_ai"]("Create an internship workflow form.")

    assert result.summary == "AI-generated university internship form."
    assert result.next_required_tool == "build_workflow_bulk"


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
    assert result.status == "DISABLED"
    assert "The workflow is DISABLED after this bulk write" in result.hint
    assert "ask whether the user wants to enable it" in result.hint
    assert "Do not call publish_workflow until the user explicitly agrees" in result.hint
    assert result.trigger_form_id == form.form_id
    assert len(client.created_forms) == 1  # The bulk call did not create a second form.
    assert client.get_form_questions_calls == ["form_ai_1"]
    assert client.created_workflows == ["University Internship Review"]
    assert client.status_updates == [("wf_new_1", {"status": "DISABLED"})]
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
    assert client.get_workflow_combined_calls == ["wf_new_1", "wf_new_1", "wf_new_1"]


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
    assert result.status == "DISABLED"
    assert client.status_updates == [("wf_1", {"status": "DISABLED"})]
    assert client.events[:2] == [
        ("status", "wf_1", {"status": "DISABLED"}),
        ("update_tree", "wf_1"),
    ]
    assert "The workflow is DISABLED after this bulk write" in result.hint
    assert "ask whether the user wants to enable it" in result.hint
    assert client.created_workflows == []
    assert client.created_forms == []


def test_build_workflow_bulk_creates_workflow_with_trigger_form_when_workflow_id_omitted():
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
        trigger_form_id="form_ai_1",
    )

    assert result.error is None
    assert result.workflow_id == "wf_new_1"
    assert result.workflow_url == "https://www.jotform.com/workflow/wf_new_1/build"
    assert result.trigger_form_id == "form_ai_1"
    assert result.trigger_form_url == "https://www.jotform.com/build/form_ai_1"
    assert result.created_steps == {"approval_1": "2", "notify_1": "3"}
    assert result.created_links_count == 2
    assert client.created_forms == []
    assert client.get_form_questions_calls == ["form_ai_1"]
    assert client.created_workflows == ["Access Request Workflow"]
    assert client.bound_trigger_forms == [("wf_new_1", "form_ai_1")]
    assert client.status_updates == [("wf_new_1", {"status": "DISABLED"})]
    assert client.events.index(("status", "wf_new_1", {"status": "DISABLED"})) < client.events.index(("update_tree", "wf_new_1"))
    assert client.update_calls[0]["workflow_id"] == "wf_new_1"


def test_build_workflow_bulk_creates_schedule_workflow_without_trigger_form():
    class ScheduleClient(DummyClient):
        def __init__(self):
            super().__init__()
            self.created_workflow_kwargs = []

        def create_workflow(self, title, **kwargs):
            self.created_workflows.append(title)
            self.created_workflow_kwargs.append(kwargs)
            self.elements = [{
                "element_id": 1,
                "type": "workflow_start_point",
                "subType": "workflow_start_point_schedule",
                "resourceID": "auto_schedule_form",
                "resourceType": "FORM",
                "position": {"x": 0, "y": 0},
            }]
            return {"id": "wf_schedule_1"}

        def get_form_questions(self, form_id):
            self.get_form_questions_calls.append(form_id)
            if form_id == "form_report":
                return {
                    "1": {"qid": "1", "text": "Employee Weekly Report", "type": "control_head"},
                    "2": {"qid": "2", "text": "Employee Email", "type": "control_email", "name": "q2_employeeEmail"},
                }
            raise AssertionError("schedule trigger must not require trigger form questions")

        def set_trigger_form(self, workflow_id, form_id):
            raise AssertionError("schedule trigger must not bind a trigger form")

    mcp = DummyMCP()
    client = ScheduleClient()
    building.register(mcp, client)

    schedule = {
        "schedule__executeWhen__afterAmount": "1",
        "schedule__executeWhen__afterUnit": "week",
        "schedule__executeWhen__customDate": "2026-09-04T17:00:00.000Z",
    }

    result = mcp.tools["build_workflow_bulk"](
        title="Weekly Report Schedule",
        trigger_type="schedule",
        trigger_schedule=schedule,
        steps=[
            StepSpec(
                ref="assign_report",
                type="workflow_assign_form",
                config={
                    "formID": "form_report",
                    "assignee": "team@workflow.invalid",
                },
            ),
            StepSpec(
                ref="notify_ops",
                type="workflow_send_email",
                config={
                    "to": "ops@workflow.invalid",
                    "subject": "Weekly report assigned",
                    "content": "The weekly report form has been assigned.",
                },
            ),
        ],
        connections=[
            ConnectionSpec(from_ref="start", to_ref="assign_report"),
            ConnectionSpec(from_ref="assign_report", to_ref="notify_ops"),
        ],
    )

    assert result.error is None
    assert result.workflow_id == "wf_schedule_1"
    assert result.trigger_form_id is None
    assert result.assigned_forms == [{
        "step_ref": "assign_report",
        "step_id": "2",
        "form_id": "form_report",
        "form_url": "https://www.jotform.com/build/form_report",
    }]
    assert client.created_workflows == ["Weekly Report Schedule"]
    assert client.created_workflow_kwargs == [{
        "trigger_type": "schedule",
        "schedule_config": {
            **schedule,
            "schedule__executeWhen__executeOnCustomDate": "Yes",
            "schedule__end__recurring": "none",
        },
    }]
    assert client.bound_trigger_forms == []
    assert client.get_form_questions_calls == ["form_report"]
    assert client.update_calls[0]["workflow_id"] == "wf_schedule_1"
    assert any("Normalized fixed recipient addresses." in warning for warning in result.warnings)
    created = {entry["elementID"]: entry["data"] for entry in client.update_calls[0]["elements"]}
    assert created[3]["to"][0]["value"] == "ops@workflow.invalid"


def test_schedule_aliases_are_normalized_before_workflow_create(monkeypatch):
    monkeypatch.setattr(
        building,
        "_now_utc",
        lambda: datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
    )

    normalized, warnings, error = building._normalize_schedule_config({
        "schedule__type": "weekly",
        "schedule__days": ["friday"],
        "schedule__time": "5 PM",
        "schedule__timezone": "Europe/Istanbul",
    })

    assert error is None
    assert normalized == {
        "schedule__executeWhen__afterUnit": "week",
        "schedule__executeWhen__afterAmount": "1",
        "schedule__executeWhen__customDate": "2026-09-04T14:00:00.000Z",
        "schedule__executeWhen__executeOnCustomDate": "Yes",
        "schedule__end__recurring": "none",
    }
    assert any("Normalized schedule aliases" in warning for warning in warnings)


def test_schedule_aliases_require_timezone_when_no_default(monkeypatch):
    monkeypatch.delenv("MCP_DEFAULT_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(
        building,
        "_now_utc",
        lambda: datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
    )

    normalized, warnings, error = building._normalize_schedule_config({
        "schedule__type": "weekly",
        "schedule__days": ["friday"],
        "schedule__time": "5 PM",
    })

    assert normalized is None
    assert warnings == []
    assert "schedule timezone is required" in error


def test_schedule_aliases_use_configured_default_timezone(monkeypatch):
    monkeypatch.setenv("MCP_DEFAULT_TIMEZONE", "Europe/Istanbul")
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(
        building,
        "_now_utc",
        lambda: datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
    )

    normalized, warnings, error = building._normalize_schedule_config({
        "schedule__type": "weekly",
        "schedule__days": ["friday"],
        "schedule__time": "5 PM",
    })

    assert error is None
    assert normalized["schedule__executeWhen__customDate"] == "2026-09-04T14:00:00.000Z"
    assert any("used default timezone 'Europe/Istanbul' from MCP_DEFAULT_TIMEZONE" in warning for warning in warnings)


def test_schedule_aliases_use_jotform_user_timezone_before_env(monkeypatch):
    monkeypatch.setenv("MCP_DEFAULT_TIMEZONE", "America/New_York")
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(
        building,
        "_now_utc",
        lambda: datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
    )

    class Client:
        def get_user_timezone(self):
            return "Europe/Istanbul"

    normalized, warnings, error = building._normalize_schedule_config(
        {
            "schedule__type": "weekly",
            "schedule__days": ["friday"],
            "schedule__time": "5 PM",
        },
        client=Client(),
    )

    assert error is None
    assert normalized["schedule__executeWhen__customDate"] == "2026-09-04T14:00:00.000Z"
    assert any("from Jotform user profile" in warning for warning in warnings)


def test_schedule_assigned_form_fields_can_drive_email_recipients_before_create(monkeypatch):
    monkeypatch.setattr(
        building,
        "_now_utc",
        lambda: datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
    )

    class ScheduleAssignedClient(DummyClient):
        def __init__(self):
            super().__init__()
            self.created_workflow_kwargs = []

        def create_workflow(self, title, **kwargs):
            self.created_workflows.append(title)
            self.created_workflow_kwargs.append(kwargs)
            self.elements = [{
                "element_id": 1,
                "type": "workflow_start_point",
                "subType": "workflow_start_point_schedule",
                "resourceID": "auto_schedule_resource",
                "resourceType": "FORM",
                "position": {"x": 0, "y": 0},
            }]
            return {"id": "wf_schedule_1"}

        def get_form_questions(self, form_id):
            self.get_form_questions_calls.append(form_id)
            if form_id != "form_ai_1":
                raise AssertionError(f"unexpected form lookup: {form_id}")
            return {
                "1": {"qid": "1", "text": "Employee Weekly Status Report", "type": "control_head"},
                "2": {"qid": "2", "text": "Employee Name", "type": "control_textbox", "name": "q2_employeeName"},
                "3": {"qid": "3", "text": "Employee Email", "type": "control_email", "name": "q3_employeeEmail"},
                "4": {"qid": "4", "text": "Week Ending Date", "type": "control_datetime", "name": "q4_weekEndingDate"},
            }

        def set_trigger_form(self, workflow_id, form_id):
            raise AssertionError("schedule assigned form must not bind as trigger form")

    mcp = DummyMCP()
    client = ScheduleAssignedClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        title="Weekly Employee Report Approval",
        trigger_type="schedule",
        trigger_schedule={
            "schedule__type": "weekly",
            "schedule__days": ["friday"],
            "schedule__time": "09:00",
            "schedule__timezone": "America/New_York",
        },
        steps=[
            StepSpec(
                ref="assign_report_form",
                type="workflow_assign_form",
                config={
                    "formID": "form_ai_1",
                    "assignee": "employee@workflow.invalid",
                    "name": "Assign Weekly Report Form",
                    "requireLogin": "Yes",
                },
            ),
            StepSpec(
                ref="manager_approval",
                type="workflow_approval",
                config={
                    "approver": "manager@workflow.invalid",
                    "name": "Manager Approval",
                    "taskDescription": "Review {Employee Name}'s report for {Week Ending Date}.",
                },
            ),
            StepSpec(
                ref="approved_email",
                type="workflow_send_email",
                config={
                    "to": "{q3_employeeEmail}",
                    "subject": "Weekly report for {Employee Name} approved",
                    "content": "Hi {Employee Name}, your report was approved.",
                },
            ),
            StepSpec(
                ref="whatsapp_notify",
                type="workflow_integration",
                subType="whatsapp",
                config={"name": "WhatsApp Notification"},
            ),
        ],
        connections=[
            ConnectionSpec(from_ref="start", to_ref="assign_report_form"),
            ConnectionSpec(from_ref="assign_report_form", to_ref="manager_approval"),
            ConnectionSpec(from_ref="manager_approval", outcome="Approve", to_ref="approved_email"),
            ConnectionSpec(from_ref="approved_email", to_ref="whatsapp_notify"),
        ],
    )

    assert result.error is None
    assert result.workflow_id == "wf_schedule_1"
    assert result.trigger_form_id is None
    assert client.bound_trigger_forms == []
    assert client.created_workflow_kwargs[0]["trigger_type"] == "schedule"
    assert client.created_workflow_kwargs[0]["schedule_config"] == {
        "schedule__executeWhen__afterUnit": "week",
        "schedule__executeWhen__afterAmount": "1",
        "schedule__executeWhen__customDate": "2026-09-04T13:00:00.000Z",
        "schedule__executeWhen__executeOnCustomDate": "Yes",
        "schedule__end__recurring": "none",
    }
    assert client.get_form_questions_calls == ["form_ai_1"]
    assert any("Normalized schedule aliases" in warning for warning in result.warnings)
    created = {entry["elementID"]: entry["data"] for entry in client.update_calls[0]["elements"]}
    assert created[4]["to"][0]["isQuestion"] is True
    assert created[4]["to"][0]["value"] == "{q3_employeeEmail}"
    assert "q2_employeeName" in created[4]["subject"]
    assert created[5]["type"] == "workflow_integration"
    assert created[5]["subType"] == "whatsapp"


def test_new_schedule_workflow_autoconnects_single_detached_chain_to_start(monkeypatch):
    monkeypatch.setattr(
        building,
        "_now_utc",
        lambda: datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
    )

    class ScheduleAssignedClient(DummyClient):
        def __init__(self):
            super().__init__()
            self.created_workflow_kwargs = []

        def create_workflow(self, title, **kwargs):
            self.created_workflows.append(title)
            self.created_workflow_kwargs.append(kwargs)
            self.elements = [{
                "id": 1,
                "type": "workflow_start_point",
                "subType": "workflow_start_point_schedule",
                "position": {"x": 0, "y": 0},
            }]
            return {"id": "wf_schedule_1"}

        def get_form_questions(self, form_id):
            self.get_form_questions_calls.append(form_id)
            return {
                "1": {"qid": "1", "text": "Employee Weekly Status Report", "type": "control_head"},
                "2": {"qid": "2", "text": "Employee Email", "type": "control_email", "name": "q2_employeeEmail"},
            }

    mcp = DummyMCP()
    client = ScheduleAssignedClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        title="Weekly Employee Report",
        trigger_type="schedule",
        trigger_schedule={
            "schedule__type": "weekly",
            "schedule__days": ["friday"],
            "schedule__time": "17:00",
            "schedule__timezone": "Europe/Istanbul",
        },
        steps=[
            StepSpec(
                ref="assign_report_form",
                type="workflow_assign_form",
                config={"formID": "form_ai_1", "assignee": "employee@workflow.invalid"},
            ),
            StepSpec(
                ref="notify_employee",
                type="workflow_send_email",
                config={
                    "to": "{q2_employeeEmail}",
                    "subject": "Weekly report received",
                    "content": "Thanks for the update.",
                },
            ),
        ],
        connections=[
            ConnectionSpec(from_ref="assign_report_form", to_ref="notify_employee"),
        ],
    )

    assert result.error is None
    assert any("Added missing start connection" in warning for warning in result.warnings)
    created_links = [link for link in client.update_calls[0]["links"] if link["action"] == "create"]
    assert len(created_links) == 2
    assert created_links[0]["data"]["fromElement"] == "1"
    assert created_links[0]["data"]["toElement"] == 2
    assert created_links[1]["data"]["fromElement"] == 2
    assert created_links[1]["data"]["toElement"] == 3


def test_existing_schedule_retry_without_trigger_type_does_not_use_start_resource_as_form():
    class ExistingScheduleClient(DummyClient):
        def __init__(self):
            super().__init__()
            self.elements = [{
                "element_id": 1,
                "type": "workflow_start_point",
                "subType": "workflow_start_point_schedule",
                "resourceID": "auto_schedule_resource",
                "resourceType": "FORM",
                "position": {"x": 0, "y": 0},
            }]

        def get_form_questions(self, form_id):
            self.get_form_questions_calls.append(form_id)
            if form_id != "form_ai_1":
                raise AssertionError(f"unexpected form lookup: {form_id}")
            return {
                "3": {"qid": "3", "text": "Employee Email", "type": "control_email", "name": "q3_employeeEmail"},
            }

    mcp = DummyMCP()
    client = ExistingScheduleClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        workflow_id="wf_schedule_1",
        steps=[
            StepSpec(
                ref="assign_report_form",
                type="workflow_assign_form",
                config={"formID": "form_ai_1", "assignee": "employee@workflow.invalid"},
            ),
            StepSpec(
                ref="approved_email",
                type="workflow_send_email",
                config={
                    "to": "{q3_employeeEmail}",
                    "subject": "Approved",
                    "content": "Approved.",
                },
            ),
        ],
        connections=[
            ConnectionSpec(from_ref="start", to_ref="assign_report_form"),
            ConnectionSpec(from_ref="assign_report_form", to_ref="approved_email"),
        ],
    )

    assert result.error is None
    assert any("Detected an existing schedule start point" in warning for warning in result.warnings)
    assert client.get_form_questions_calls == ["form_ai_1"]
    created = {entry["elementID"]: entry["data"] for entry in client.update_calls[0]["elements"]}
    assert created[3]["to"][0]["value"] == "{q3_employeeEmail}"


def test_new_workflow_fills_missing_staff_approver_with_draft_placeholder():
    mcp = DummyMCP()
    client = DecoupledFlowClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        title="Internship Application Workflow",
        trigger_form_id="form_ai_1",
        steps=[
            StepSpec(
                ref="hr_review",
                type="workflow_approval",
                config={
                    "name": "HR Review",
                    "taskDescription": "Review the internship application.",
                },
            ),
            StepSpec(
                ref="student_notification",
                type="workflow_send_email",
                config={
                    "to": "Student Email",
                    "subject": "Internship application received",
                    "content": "Your internship application has been sent to HR.",
                },
            ),
            StepSpec(
                ref="student_rejection",
                type="workflow_send_email",
                config={
                    "to": "Student Email",
                    "subject": "Internship application update",
                    "content": "Your internship application was reviewed but not approved.",
                },
            ),
        ],
        connections=[
            ConnectionSpec(from_ref="TRIGGER", to_ref="hr_review"),
            ConnectionSpec(
                from_ref="hr_review",
                to_ref="student_notification",
                outcome="Approved",
            ),
            ConnectionSpec(
                from_ref="hr_review",
                to_ref="student_rejection",
                outcome="Rejected",
            ),
        ],
    )

    assert result.error is None
    assert any("hr@workflow.invalid" in warning for warning in result.warnings)
    assert any("TRIGGER" in warning and "start" in warning for warning in result.warnings)
    assert any("Approved" in warning and "Approve" in warning for warning in result.warnings)
    assert any("Rejected" in warning and "Deny" in warning for warning in result.warnings)
    approval_config = client.update_calls[0]["elements"][0]["data"]
    assert approval_config["approver"][0]["value"] == "hr@workflow.invalid"


def test_build_workflow_bulk_rejects_missing_details_instead_of_inventing_defaults():
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

    assert "taskDescription" in result.error
    assert client.update_calls == []


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
    # At least early lock check, revision snapshot capture, and post-write revision read.
    assert len(client.get_workflow_combined_calls) >= 3
    assert set(client.get_workflow_combined_calls) == {"wf_1"}
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


def test_build_workflow_bulk_rejects_new_workflow_without_trigger_form_id():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        title="Fast Draft",
    )

    assert result.error
    assert "trigger_form_id is required for new form-submission workflows" in result.error
    assert "search_workflow_templates" in result.error
    assert client.created_forms == []
    assert client.created_workflows == []


def test_build_workflow_bulk_rejects_trigger_form_only_without_steps():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        title="Fast Draft",
        trigger_form_id="form_ai_1",
    )

    assert "No steps provided" in result.error
    assert client.created_forms == []
    assert client.created_workflows == []


def test_build_workflow_bulk_updates_existing_step_in_the_single_tree_write():
    mcp = DummyMCP()
    client = DummyClient()
    client.elements.append({
        "element_id": 2,
        "type": "workflow_send_email",
        "to": [{"value": "user@company.com", "isQuestion": False}],
        "subject": "Old subject",
        "content": "<p>Existing body</p>",
    })
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        step_updates=[StepUpdateSpec(step_id="2", config={"subject": "New subject"})],
    )

    assert result.error is None
    assert result.updated_steps == ["2"]
    assert len(client.update_calls) == 1
    update = next(item for item in client.update_calls[0]["elements"] if item["elementID"] == "2")
    assert update["action"] == "update"
    assert update["data"] == {"element_id": "2", "subject": "New subject"}


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
        trigger_form_id="form_ai_1",
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


def test_build_workflow_bulk_uses_trigger_form_id_without_ai_form_fallback():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        title="Deterministic Trigger Workflow",
        trigger_form_id="form_existing_1",
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
    assert "search_workflow_templates" in result.error


def test_build_workflow_bulk_creates_blank_integration_shell():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        steps=[
            StepSpec(
                ref="slack_step",
                type="workflow_integration",
                subType="slack",
                config={
                    "name": "Slack",
                    "channel": "#ops",
                    "message": "Do not persist this",
                    "integrationAccountID": "account_should_not_persist",
                },
            )
        ],
        connections=[ConnectionSpec(from_ref="start", to_ref="slack_step")],
    )

    assert result.error is None
    assert result.created_steps == {"slack_step": "2"}
    assert any("workflow_integration shell ignored config field 'channel'" in warning for warning in result.warnings)
    assert any("workflow_integration shell ignored config field 'message'" in warning for warning in result.warnings)
    create = next(item for item in client.update_calls[0]["elements"] if item["action"] == "create")
    data = create["data"]
    assert data["type"] == "workflow_integration"
    assert data["subType"] == "slack"
    assert data["name"] == "Slack"
    assert data["integrationAccountID"] == ""
    assert data["integrationID"] == ""
    assert data["internalFormID"] == ""
    assert data["mode"] == ""
    assert data["responseMap"] == []
    assert "channel" not in data
    assert "message" not in data


def test_build_workflow_bulk_rejects_unsupported_integration_subtype():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        steps=[
            {
                "ref": "shopify_step",
                "type": "workflow_integration",
                "subType": "shopify",
                "config": {"name": "Shopify"},
            }
        ],
        connections=[ConnectionSpec(from_ref="start", to_ref="shopify_step")],
    )

    assert result.error
    assert "Unsupported workflow integration subType 'shopify'" in result.error
    assert "slack" in result.hint
    assert "Do not include auth or settings fields" in result.hint
    assert client.update_calls == []


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
    assert result.hint
    assert "numeric step_id" in result.hint
    assert "do not use guessed semantic refs" in result.hint


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
                "to": "{2_email}",
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


def test_new_workflow_missing_outcome_is_rejected_before_workflow_creation():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        title="Safe preflight",
        trigger_form_id="form_1",
        steps=[
            StepSpec(
                ref="approval",
                type="workflow_approval",
                config={"approver": "a@b.com", "taskDescription": "Review"},
            ),
            StepSpec(
                ref="email",
                type="workflow_send_email",
                config={"to": "a@b.com", "subject": "Done", "content": "Done"},
            ),
        ],
        connections=[
            ConnectionSpec(from_ref="start", to_ref="approval"),
            ConnectionSpec(from_ref="approval", to_ref="email"),
        ],
    )

    assert "requires an outcome" in result.error
    assert client.created_workflows == []
    assert client.status_updates == []


def test_partial_workflow_creation_returns_existing_id_without_creating_replacement():
    class PartialCreateClient(DummyClient):
        def create_workflow(self, title, **kwargs):
            self.created_workflows.append(title)
            raise PartialWorkflowCreateError(
                "wf_partial_1",
                "persisting the start point",
                JotformAPIError(504, "upstream timeout"),
            )

    mcp = DummyMCP()
    client = PartialCreateClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        title="Partial create protection",
        trigger_form_id="form_1",
        steps=[
            StepSpec(
                ref="email",
                type="workflow_send_email",
                config={"to": "a@b.com", "subject": "Done", "content": "Done"},
            ),
        ],
        connections=[ConnectionSpec(from_ref="start", to_ref="email")],
    )

    assert result.error
    assert result.workflow_id == "wf_partial_1"
    assert result.status is None
    assert "do not create a replacement" in result.hint
    assert client.created_workflows == ["Partial create protection"]
    assert client.update_calls == []


def test_bulk_rejects_config_fields_that_would_be_silently_dropped():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        steps=[
            StepSpec(
                ref="email",
                type="workflow_send_email",
                config={"to": "a@b.com", "subject": "Done", "content": "Done", "subjet": "typo"},
            ),
        ],
        connections=[ConnectionSpec(from_ref="start", to_ref="email")],
    )

    assert "silently dropped" in result.error
    assert client.update_calls == []


def test_bulk_snapshot_verifier_detects_missing_config_and_connection():
    issues = building._verify_bulk_snapshot(
        {
            "workflow": {"status": "DISABLED"},
            "elements": [{"element_id": "10", "type": "workflow_send_email", "subject": "Saved"}],
            "links": [],
        },
        expected_steps={"10": ("workflow_send_email", {"subject": "Expected"})},
        expected_connections=[("1", "10", "")],
        deleted_step_ids=[],
        deleted_link_ids=[],
    )

    assert "step 10 field 'subject' did not persist" in issues
    assert "connection 1->10 is missing" in issues


def test_bulk_snapshot_verifier_detects_wrong_branch_outcome():
    issues = building._verify_bulk_snapshot(
        {
            "workflow": {"status": "DISABLED"},
            "elements": [
                {
                    "element_id": "2",
                    "type": "workflow_binary_decision",
                    "outcomes": [
                        {"conditionValue": "TRUE", "linkID": "20"},
                        {"conditionValue": "FALSE", "linkID": "21"},
                    ],
                },
                {"element_id": "3", "type": "workflow_send_email"},
            ],
            "links": [{"id": "20", "fromElement": "2", "toElement": "3"}],
        },
        expected_steps={},
        expected_connections=[("2", "3", "FALSE")],
        deleted_step_ids=[],
        deleted_link_ids=[],
    )

    assert "connection 2->3 outcome is ['TRUE'], expected 'FALSE'" in issues


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


def test_build_workflow_bulk_delete_parent_requires_choice_for_orphaned_children():
    mcp = DummyMCP()
    client = DummyClient()
    client.elements = [
        {"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}},
        {"element_id": 8, "type": "workflow_split", "name": "Split Parent", "position": {"x": 0, "y": 100}},
        {"element_id": 9, "type": "workflow_send_email", "name": "Child A", "position": {"x": -100, "y": 200}},
        {"element_id": 10, "type": "workflow_send_email", "name": "Child B", "position": {"x": 100, "y": 200}},
    ]
    client.links = [
        {"link_id": 101, "fromElement": 1, "toElement": 8},
        {"link_id": 102, "fromElement": 8, "toElement": 9},
        {"link_id": 103, "fromElement": 8, "toElement": 10},
    ]
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"]("wf_1", delete_step_ids=["8"])

    assert result.needs_confirmation is True
    assert result.error
    assert "would leave downstream child steps unreachable" in result.error
    assert result.orphaned_step_ids == ["9", "10"]
    assert result.orphaned_steps == [
        {"step_id": "9", "type": "workflow_send_email", "label": "Child A"},
        {"step_id": "10", "type": "workflow_send_email", "label": "Child B"},
    ]
    assert result.delete_impacts
    impact = result.delete_impacts[0]
    assert impact["deleted_step"] == {"step_id": "8", "type": "workflow_split", "label": "Split Parent"}
    assert impact["incoming"][0]["from"]["step_id"] == "1"
    assert [item["to"]["step_id"] for item in impact["outgoing"]] == ["9", "10"]
    assert impact["reconnect_candidates"] == [
        {"step_id": "9", "type": "workflow_send_email", "label": "Child A"},
        {"step_id": "10", "type": "workflow_send_email", "label": "Child B"},
    ]
    assert "multiple child paths" in impact["suggested_question"]
    assert "reconnect_candidates/end_candidates" in result.hint
    assert client.update_calls == []


def test_build_workflow_bulk_delete_middle_step_requires_reconnect_even_when_child_is_end():
    mcp = DummyMCP()
    client = DummyClient()
    client.elements = [
        {"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}},
        {"element_id": 8, "type": "workflow_assign_task", "name": "Middle Task", "position": {"x": 0, "y": 100}},
        {"element_id": 9, "type": "workflow_end_point", "name": "Done", "position": {"x": 0, "y": 200}},
    ]
    client.links = [
        {"link_id": 101, "fromElement": 1, "toElement": 8},
        {"link_id": 102, "fromElement": 8, "toElement": 9},
    ]
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"]("wf_1", delete_step_ids=["8"])

    assert result.needs_confirmation is True
    assert result.orphaned_step_ids == []
    assert "would break existing flow paths" in result.error
    impact = result.delete_impacts[0]
    assert impact["incoming"][0]["from"]["step_id"] == "1"
    assert impact["outgoing"][0]["to"]["step_id"] == "9"
    assert impact["end_candidates"] == [
        {"step_id": "9", "type": "workflow_end_point", "label": "Done"}
    ]
    assert client.update_calls == []


def test_build_workflow_bulk_delete_middle_step_allows_explicit_reconnect_plan():
    mcp = DummyMCP()
    client = DummyClient()
    client.elements = [
        {"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}},
        {"element_id": 8, "type": "workflow_assign_task", "name": "Middle Task", "position": {"x": 0, "y": 100}},
        {"element_id": 9, "type": "workflow_end_point", "name": "Done", "position": {"x": 0, "y": 200}},
    ]
    client.links = [
        {"link_id": 101, "fromElement": 1, "toElement": 8},
        {"link_id": 102, "fromElement": 8, "toElement": 9},
    ]
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        delete_step_ids=["8"],
        connections=[ConnectionSpec(from_ref="1", to_ref="9")],
    )

    assert result.error is None
    assert result.needs_confirmation is False
    assert result.deleted_steps == ["8"]
    call = client.update_calls[0]
    assert {"action": "delete", "elementID": "8", "data": {"element_id": "8"}} in call["elements"]
    assert any(link.get("action") == "create" for link in call["links"])


def test_build_workflow_bulk_delete_parent_can_leave_orphans_after_confirmation():
    mcp = DummyMCP()
    client = DummyClient()
    client.elements = [
        {"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}},
        {"element_id": 8, "type": "workflow_split", "name": "Split Parent", "position": {"x": 0, "y": 100}},
        {"element_id": 9, "type": "workflow_send_email", "name": "Child A", "position": {"x": -100, "y": 200}},
        {"element_id": 10, "type": "workflow_send_email", "name": "Child B", "position": {"x": 100, "y": 200}},
    ]
    client.links = [
        {"link_id": 101, "fromElement": 1, "toElement": 8},
        {"link_id": 102, "fromElement": 8, "toElement": 9},
        {"link_id": 103, "fromElement": 8, "toElement": 10},
    ]
    building.register(mcp, client)

    result = mcp.tools["build_workflow_bulk"](
        "wf_1",
        delete_step_ids=["8"],
        confirm_orphaned_downstream=True,
    )

    assert result.error is None
    assert result.needs_confirmation is False
    assert result.deleted_steps == ["8"]
    assert len(client.update_calls) == 1


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
