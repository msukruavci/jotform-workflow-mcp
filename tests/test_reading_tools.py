from mcp_server import sync_state
from mcp_server.jotform_client import JotformAPIError
from mcp_server.telemetry_context import bind_context
from mcp_server.tools import reading


def test_workflow_list_pagination_metadata_and_forwarding():
    class Client:
        def __init__(self):
            self.args = None

        def list_workflows(self, *, limit, offset):
            self.args = (limit, offset)
            return [{"id": str(offset + index)} for index in range(limit)]

    client = Client()
    result = reading.read_workflow_list(client, limit=2, offset=4)

    assert client.args == (2, 4)
    assert result.count == 2
    assert result.has_more is True
    assert result.next_offset == 6


class FakeWorkflowClient:
    def get_workflow_combined(self, workflow_id, *, fetch_essential=True):
        return {
            "workflow": {
                "id": workflow_id,
                "title": "Support flow",
                "status": "ENABLED",
                "publishStatus": "DRAFT",
            },
            "elements": [
                {
                    "element_id": "1",
                    "type": "workflow_start_point",
                    "resourceID": "form_1",
                    "resourceType": "FORM",
                },
                {
                    "element_id": "2",
                    "type": "workflow_send_email",
                    "name": "Notify customer",
                    "to": [{"value": "{Email Address}", "text": "{Email Address}"}],
                },
                {
                    "element_id": "3",
                    "type": "workflow_send_email",
                    "name": "External unfinished email",
                    "to": [{"value": "{Email Address}", "text": "{Email Address}"}],
                    "subject": "",
                    "content": "",
                },
            ],
            "links": [
                {"link_id": "1", "fromElement": "1", "toElement": "2"},
                {"link_id": "2", "fromElement": "1", "toElement": "3"},
            ],
        }

    def get_form_questions(self, form_id):
        assert form_id == "form_1"
        return {
            "3": {
                "qid": "3",
                "text": "Email Address",
                "type": "control_email",
                "required": "Yes",
            },
            "4": {
                "qid": "4",
                "text": "Request Type",
                "type": "control_radio",
                "required": "No",
                "options": "Refund|Warranty",
            },
        }


def test_read_workflow_detail_includes_trigger_form_fields():
    result = reading.read_workflow_detail(FakeWorkflowClient(), "wf_1")

    assert result.error is None
    assert result.workflow_id == "wf_1"
    assert [field.model_dump() for field in result.trigger_form_fields] == [
        {
            "field_id": "3",
            "name": "3",
            "label": "Email Address",
            "type": "control_email",
            "required": "Yes",
            "options": [],
        },
        {
            "field_id": "4",
            "name": "4",
            "label": "Request Type",
            "type": "control_radio",
            "required": "No",
            "options": ["Refund", "Warranty"],
        },
    ]
    serialized = result.model_dump()
    assert "health" not in serialized
    assert "diagnostics" not in serialized


def test_read_workflow_detail_warns_when_trigger_fields_cannot_be_loaded():
    class FailingFieldClient(FakeWorkflowClient):
        def get_form_questions(self, form_id):
            raise JotformAPIError(503, "temporarily unavailable")

    result = reading.read_workflow_detail(FailingFieldClient(), "wf_1")

    assert result.error is None
    assert result.trigger_form_fields == []
    assert "Do not assume the form has no fields" in result.warnings[0]


def test_read_workflow_detail_remembers_session_snapshot_for_next_mutation():
    sync_state.clear_workflow_snapshots()
    with bind_context(session_id="reading-snapshot"):
        result = reading.read_workflow_detail(FakeWorkflowClient(), "wf_1")
        snapshot = sync_state.load_workflow_snapshot(
            "wf_1",
            revision_id=result.revision_id,
        )

    assert snapshot is not None
    assert snapshot["workflow"]["id"] == "wf_1"
    assert len(snapshot["elements"]) == 3


def test_read_workflow_detail_includes_compact_email_step_summaries():
    result = reading.read_workflow_detail(FakeWorkflowClient(), "wf_1")

    state_by_id = {step.step_id: step.model_dump() for step in result.step_states}
    assert state_by_id["2"] == {
        "step_id": "2",
        "type": "workflow_send_email",
        "label": "Notify customer",
        "incoming": [{"link_id": "1", "step_id": "1", "outcome": None}],
        "outgoing": [],
        "key_config": {
            "to": ["{Email Address}"],
            "subject": None,
            "content_present": False,
            "content_excerpt": None,
        },
        "missing_fields": ["subject", "content"],
        "config_complete": False,
    }
    assert [email.model_dump() for email in result.email_steps] == [
        {
            "step_id": "2",
            "label": "Notify customer",
            "to": ["{Email Address}"],
            "subject": None,
            "content_present": False,
            "content_excerpt": None,
            "missing_fields": ["subject", "content"],
            "incoming": [{"link_id": "1", "from_step": "1", "outcome": None}],
        },
        {
            "step_id": "3",
            "label": "External unfinished email",
            "to": ["{Email Address}"],
            "subject": None,
            "content_present": False,
            "content_excerpt": None,
            "missing_fields": ["subject", "content"],
            "incoming": [{"link_id": "2", "from_step": "1", "outcome": None}],
        },
    ]
    assert result.diagnostics["incomplete_email_steps"] == [
        (
            "Email step 2 (Notify customer) is incomplete: missing subject, content. "
            "Do not treat it as satisfying a requested email/survey/notification."
        ),
        (
            "Email step 3 (External unfinished email) is incomplete: missing subject, content. "
            "Do not treat it as satisfying a requested email/survey/notification."
        ),
    ]
    assert result.diagnostics["incomplete_steps"][0] == (
        "Step 2 (Notify customer, workflow_send_email) is incomplete: missing subject, content. "
        "Do not treat it as satisfying a requested workflow action."
    )


class GenericFieldTokenWorkflowClient(FakeWorkflowClient):
    def get_workflow_combined(self, workflow_id, *, fetch_essential=True):
        return {
            "workflow": {
                "id": workflow_id,
                "title": "Weekly report",
                "status": "DISABLED",
                "publishStatus": "DRAFT",
            },
            "elements": [
                {
                    "element_id": "1",
                    "type": "workflow_start_point",
                    "resourceID": "form_1",
                    "resourceType": "FORM",
                },
                {
                    "element_id": "2",
                    "type": "workflow_send_email",
                    "name": "Notify employee",
                    "to": [{"value": "{q3_email0}", "text": "{q3_email0}"}],
                    "subject": "Weekly report approved for {q2_textbox0}",
                    "content": (
                        "<p>Hi {q2_textbox0}, your weekly status report has "
                        "been reviewed and approved.</p>"
                    ),
                },
            ],
            "links": [{"link_id": "1", "fromElement": "1", "toElement": "2"}],
        }

    def get_form_questions(self, form_id):
        assert form_id == "form_1"
        return {
            "2": {
                "qid": "2",
                "name": "q2_textbox0",
                "text": "Employee Name",
                "type": "control_textbox",
            },
            "3": {
                "qid": "3",
                "name": "q3_email0",
                "text": "Employee Email",
                "type": "control_email",
            },
        }


def test_read_workflow_detail_humanizes_generic_field_tokens_in_model_summaries():
    sync_state.clear_workflow_snapshots()

    with bind_context(session_id="field-token-display"):
        result = reading.read_workflow_detail(GenericFieldTokenWorkflowClient(), "wf_1")
        snapshot = sync_state.load_workflow_snapshot("wf_1")

    email = result.email_steps[0]
    assert email.to == ["{Employee Email}"]
    assert email.subject == "Weekly report approved for {Employee Name}"
    assert email.content_excerpt == (
        "Hi {Employee Name}, your weekly status report has been reviewed and approved."
    )
    assert result.step_states[1].key_config["content_excerpt"] == email.content_excerpt

    assert "{q2_textbox0}" in snapshot["elements"][1]["content"]
