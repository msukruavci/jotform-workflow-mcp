from mcp_server.tools import reading


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
                },
            ],
            "links": [
                {"link_id": "1", "fromElement": "1", "toElement": "2"},
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
            "label": "Email Address",
            "type": "control_email",
            "required": "Yes",
            "options": [],
        },
        {
            "field_id": "4",
            "label": "Request Type",
            "type": "control_radio",
            "required": "No",
            "options": ["Refund", "Warranty"],
        },
    ]
