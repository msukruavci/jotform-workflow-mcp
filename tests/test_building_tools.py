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
        self.created = False
        self.fetched_elements = False
        self.fetched_combined = False
        self.elements = [{"element_id": 1, "position": {"x": 0, "y": 0}}]
        self.updated = False
        self.update_calls = []
        self.element_details = {}

    def create_workflow(self, title, **kwargs):
        self.created = True
        return {"id": "wf_1"}

    def get_elements(self, workflow_id):
        self.fetched_elements = True
        return self.elements

    def get_links(self, workflow_id):
        return []

    def update_tree(self, workflow_id, *, elements=None, links=None):
        self.updated = True
        self.update_calls.append({"elements": elements, "links": links})
        return {}

    def get_element(self, workflow_id, step_id):
        return self.element_details.get(str(step_id), {"element_id": step_id})

    def get_workflow_combined(self, workflow_id):
        self.fetched_combined = True
        return {
            "workflow": {"id": workflow_id, "title": "Demo"},
            "elements": [{"element_id": 1, "type": "workflow_start_point", "resourceID": "form_1"}],
            "links": [],
        }

    def get_form_questions(self, form_id):
        return {
            "1_name": {"text": "Name", "type": "control_textbox"},
            "2": {"text": "Full Name", "type": "control_fullname", "name": "q2_fullname0"},
            "2_email": {"text": "Email", "type": "control_email"},
            "3": {"text": "Email Address", "type": "control_email", "name": "q3_email1"},
        }


def test_create_workflow_requires_trigger_strategy_by_default():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["create_workflow"]("Demo")

    assert result.error
    assert "trigger form" in result.error
    assert client.created is False


def test_create_workflow_allows_explicit_no_trigger_draft():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["create_workflow"]("Demo", allow_without_trigger=True)

    assert result.error is None
    assert result.workflow_id == "wf_1"
    assert client.created is True


def test_add_task_refuses_empty_assignee_and_description_before_network():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["add_step"]("wf_1", "workflow_assign_task", {})

    assert result.error
    assert "assignee" in result.error
    assert "taskDescription" in result.error
    assert client.fetched_elements is False


def test_add_approval_refuses_empty_approver_before_network():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["add_step"]("wf_1", "workflow_approval", {"taskDescription": "Review it"})

    assert result.error
    assert "approver" in result.error
    assert client.fetched_elements is False


def test_add_email_refuses_empty_message_details_before_network():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["add_step"]("wf_1", "workflow_send_email", {"to": [{"text": "a@b.com"}]})

    assert result.error
    assert "subject" in result.error
    assert "content" in result.error
    assert client.fetched_elements is False


def test_add_conditional_branch_refuses_default_only_outcomes_before_network():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["add_step"]("wf_1", "workflow_conditional_branch", {})

    assert result.error
    assert "outcomes" in result.error
    assert client.fetched_elements is False


def test_add_condition_refuses_plain_text_field_before_write():
    mcp = DummyMCP()
    client = DummyClient()
    building.register(mcp, client)

    result = mcp.tools["add_step"]("wf_1", "workflow_binary_decision", {
        "conditionTerms": [{
            "field": "Email",
            "operator": "equals",
            "value": "yes@example.com",
        }]
    })

    assert result.error
    assert "real field_id" in result.error
    assert "Email" in result.error
    assert "2_email" in result.hint
    assert client.fetched_combined is True
    assert client.fetched_elements is False


def test_merge_outcome_updates_preserves_existing_link_ids():
    current = {
        "type": "workflow_conditional_branch",
        "outcomes": [
            {
                "id": 1,
                "outcomeID": 1,
                "branchName": "Rejected Application",
                "conditionValue": "CUSTOM",
                "linkID": 6,
            },
            {
                "id": 2,
                "outcomeID": 2,
                "branchName": "Continue Review",
                "conditionValue": "OTHER",
                "linkID": 7,
            },
        ],
    }
    config = {
        "outcomes": [
            {
                "id": 1,
                "outcomeID": 1,
                "branchName": "Rejected Application",
                "conditionValue": "CUSTOM",
            },
            {
                "id": 2,
                "outcomeID": 2,
                "branchName": "Accepted Application",
                "conditionValue": "OTHER",
            },
        ],
    }

    merged = building._merge_outcome_updates(current, config)

    assert merged["outcomes"][0]["linkID"] == 6
    assert merged["outcomes"][1]["linkID"] == 7
    assert merged["outcomes"][1]["branchName"] == "Accepted Application"


def test_config_from_update_tree_result_merges_the_matching_element_response():
    config = building._config_from_update_tree_result(
        current={"element_id": 7, "name": "Approval", "untouched": True},
        applied_config={"approver": [{"text": "submitted@example.com"}]},
        update_response={
            "result": {
                "elements": [{
                    "action": "update",
                    "elementID": 7,
                    "data": {
                        "approver": [{"text": "saved@example.com"}],
                        "serverFlag": "Yes",
                    },
                }]
            }
        },
        step_id="7",
    )

    assert config == {
        "element_id": 7,
        "name": "Approval",
        "untouched": True,
        "approver": [{"text": "saved@example.com"}],
        "serverFlag": "Yes",
    }


def test_config_from_update_tree_result_falls_back_to_validated_write_config():
    config = building._config_from_update_tree_result(
        current={"element_id": 7, "name": "Approval"},
        applied_config={"approver": [{"text": "saved@example.com"}]},
        update_response={},
        step_id="7",
    )

    assert config["name"] == "Approval"
    assert config["approver"] == [{"text": "saved@example.com"}]


def test_update_step_returns_saved_config_from_update_tree(monkeypatch):
    mcp = DummyMCP()
    client = DummyClient()
    client.element_details["7"] = {
        "element_id": 7,
        "type": "workflow_approval",
        "name": "Approval",
        "approver": [{"text": "old@example.com", "value": "old@example.com"}],
    }

    def update_tree(workflow_id, *, elements=None, links=None):
        client.updated = True
        client.update_calls.append({"elements": elements, "links": links})
        return {
            "result": {
                "elements": [{
                    "action": "update",
                    "elementID": 7,
                    "data": {
                        "approver": [{
                            "text": "saved@example.com",
                            "value": "saved@example.com",
                        }]
                    },
                }]
            }
        }

    client.update_tree = update_tree
    monkeypatch.setattr(building.revision_log, "capture_workflow_revision", lambda *args, **kwargs: None)
    building.register(mcp, client)

    result = mcp.tools["update_step"](
        "wf_1",
        "7",
        {"approver": [{"text": "saved@example.com", "value": "saved@example.com"}]},
    )

    assert result.error is None
    assert result.config["name"] == "Approval"
    assert result.config["approver"] == [{
        "text": "saved@example.com",
        "value": "saved@example.com",
    }]


def test_update_email_step_does_not_overwrite_untouched_settings(monkeypatch):
    mcp = DummyMCP()
    client = DummyClient()
    client.element_details["7"] = {
        "element_id": 7,
        "type": "workflow_send_email",
        "subject": "Old subject",
        "senderEmail": "custom@example.com",
        "cc": [{"text": "copy@example.com"}],
    }
    monkeypatch.setattr(building.revision_log, "capture_workflow_revision", lambda *args, **kwargs: None)
    building.register(mcp, client)

    result = mcp.tools["update_step"]("wf_1", "7", {"subject": "New subject"})

    assert result.error is None
    update_data = client.update_calls[-1]["elements"][0]["data"]
    assert update_data["subject"] == "New subject"
    assert update_data["isDirty"] == "Yes"
    assert "senderEmail" not in update_data
    assert "cc" not in update_data
    assert "attachment" not in update_data


def test_update_email_step_keeps_sender_name_separate_from_creator_metadata(monkeypatch):
    mcp = DummyMCP()
    client = DummyClient()
    client.element_details["7"] = {
        "element_id": 7,
        "type": "workflow_send_email",
        "fromName": "Gökhan",
        "senderName": "Jotform",
    }
    monkeypatch.setattr(building.revision_log, "capture_workflow_revision", lambda *args, **kwargs: None)
    building.register(mcp, client)

    result = mcp.tools["update_step"]("wf_1", "7", {"senderName": "Jotform2"})

    assert result.error is None
    update_data = client.update_calls[-1]["elements"][0]["data"]
    assert update_data["senderName"] == "Jotform2"
    assert "fromName" not in update_data


def test_normalize_email_recipients_converts_field_id_to_question_reference():
    client = DummyClient()

    normalized, hint, error = building._normalize_email_recipients(
        client,
        "wf_1",
        {"to": [{"text": "Email Address", "id": "3"}]},
    )

    assert error is None
    assert hint
    assert normalized["to"][0]["isQuestion"] is True
    assert normalized["to"][0]["value"] == "{q3_email1}"
    assert normalized["to"][0]["text"] == "Email Address"


def test_normalize_email_config_matches_builder_email_save_shape():
    client = DummyClient()

    normalized, hint, error = building._normalize_email_config(
        client,
        "wf_1",
        {
            "to": [{"text": "Email Address", "id": "3"}],
            "subject": "Application update",
            "content": "Thank you for your interest.\n\nWe cannot proceed.",
        },
    )

    assert error is None
    assert "wrapped plain text" in hint
    assert "Normalized recipient field references" in hint
    assert normalized["content"].startswith("<!DOCTYPE html>")
    assert "<p>Thank you for your interest.</p>" in normalized["content"]
    assert "<p>We cannot proceed.</p>" in normalized["content"]
    assert normalized["to"][0]["value"] == "{q3_email1}"
    assert normalized["to"][0]["formTitle"] == "Form"
    assert normalized["attachment"] == {"name": "", "url": "", "type": ""}
    assert normalized["senderEmail"] == "noreply@jotform.com"
    assert normalized["hideEmptyFields"] == "1"
    assert normalized["recipientLimit"] == 10
    assert normalized["isDirty"] == "Yes"


def test_normalize_email_config_wraps_html_fragment_without_escaping_tags():
    client = DummyClient()

    normalized, hint, error = building._normalize_email_config(
        client,
        "wf_1",
        {
            "to": [{"text": "Email Address", "id": "3"}],
            "subject": "Application update",
            "content": "<p>Merhaba {2}</p><p>Süreç tamamlandı.</p>",
        },
    )

    assert error is None
    assert "wrapped plain text" in hint
    assert "<p>Merhaba {q2_fullname0}</p>" in normalized["content"]
    assert "&lt;p&gt;" not in normalized["content"]


def test_normalize_email_config_rewrites_content_field_tokens_to_question_names():
    client = DummyClient()

    normalized, hint, error = building._normalize_email_config(
        client,
        "wf_1",
        {
            "to": [{"text": "Email Address", "id": "3"}],
            "subject": "Application received",
            "content": "Hi {2}, we received your application.",
        },
    )

    assert error is None
    assert "normalized email content field tokens" in hint
    assert "{q2_fullname0}" in normalized["content"]
    assert "{2}" not in normalized["content"]


def test_normalize_email_config_preserves_rich_text_markup_on_partial_update():
    client = DummyClient()

    normalized, _, error = building._normalize_email_config(
        client,
        "wf_1",
        {"content": "<p>Hello <strong>{q2_fullname0}</strong></p>"},
        apply_defaults=False,
    )

    assert error is None
    assert "<p>Hello <strong>{q2_fullname0}</strong></p>" in normalized["content"]
    assert "&lt;p&gt;" not in normalized["content"]
    assert normalized["isDirty"] == "Yes"


def test_normalize_email_config_rewrites_subject_and_camelcase_content_tokens():
    client = DummyClient()

    normalized, hint, error = building._normalize_email_config(
        client,
        "wf_1",
        {
            "to": "{employeeEmail}, hr@workflow.invalid",
            "subject": "Weekly report for {Full Name}",
            "content": "Hi {fullName}, your report was reviewed.",
        },
        trigger_context=(
            "form_1",
            {
                "2": {"text": "Full Name", "type": "control_fullname", "name": "q2_fullname0"},
                "3": {"text": "Employee Email", "type": "control_email", "name": "employeeEmail"},
            },
            None,
        ),
    )

    assert error is None
    assert "normalized email subject field tokens" in hint
    assert "normalized email content field tokens" in hint
    assert normalized["subject"] == "Weekly report for {q2_fullname0}"
    assert "{q2_fullname0}" in normalized["content"]
    assert "{fullName}" not in normalized["content"]
    assert len(normalized["to"]) == 2
    assert normalized["to"][0]["isQuestion"] is True
    assert normalized["to"][0]["value"] == "{employeeEmail}"
    assert normalized["to"][0]["text"] == "Employee Email"
    assert normalized["to"][1]["isQuestion"] is False
    assert normalized["to"][1]["value"] == "hr@workflow.invalid"


def test_normalize_email_recipients_rejects_ambiguous_email_field_token():
    client = DummyClient()

    normalized, hint, error = building._normalize_email_recipients(
        client,
        "wf_1",
        {"to": "{missingEmail}"},
        trigger_context=(
            "form_1",
            {
                "3": {"text": "Employee Email", "type": "control_email", "name": "employeeEmail"},
                "4": {"text": "Manager Email", "type": "control_email", "name": "managerEmail"},
            },
            None,
        ),
    )

    assert hint is None
    assert "does not match a real trigger form email field" in error
    assert normalized["to"] == "{missingEmail}"


def test_question_id_by_token_prefers_visible_label_when_name_is_generated():
    questions = {
        "3": {
            "qid": "3",
            "name": "q3_email1",
            "text": "Yetkili Kişi E-posta Adresi",
            "type": "control_email",
        }
    }

    assert building._question_id_by_token(questions, "yetkili kisi eposta adresi") == "3"


def test_question_id_by_token_accepts_unique_email_alias_without_matching_name():
    questions = {
        "7": {
            "qid": "7",
            "name": "q7_generated",
            "text": "Kurumsal E-posta Adresi",
            "type": "control_email",
        }
    }

    assert building._question_id_by_token(questions, "email") == "7"


def test_question_id_by_token_refuses_ambiguous_field_labels():
    questions = {
        "4": {"qid": "4", "name": "q4_email", "text": "Müşteri E-posta Adresi", "type": "control_email"},
        "5": {"qid": "5", "name": "q5_email", "text": "Yönetici E-posta Adresi", "type": "control_email"},
    }

    assert building._question_id_by_token(questions, "email") is None


def test_normalize_assignee_fields_matches_builder_fixed_email_shape():
    client = DummyClient()

    normalized, hint, error = building._normalize_assignee_fields(
        client,
        "wf_1",
        {"assignee": "hr@example.com"},
        ("assignee",),
    )

    assert error is None
    assert hint
    assert normalized["assignee"][0]["value"] == "hr@example.com"
    assert normalized["assignee"][0]["text"] == "hr@example.com"
    assert normalized["assignee"][0]["isValid"] is True
    assert normalized["assignee"][0]["isQuestion"] is False


def test_normalize_assignee_fields_rejects_invalid_static_text():
    client = DummyClient()

    normalized, hint, error = building._normalize_assignee_fields(
        client,
        "wf_1",
        {"assignee": [{"text": "avc"}]},
        ("assignee",),
    )

    assert normalized["assignee"] == [{"text": "avc"}]
    assert hint is None
    assert "valid email address" in error


def test_normalize_assignee_fields_accepts_trigger_email_field():
    client = DummyClient()

    normalized, hint, error = building._normalize_assignee_fields(
        client,
        "wf_1",
        {"assignee": [{"text": "Email Address", "id": "3"}]},
        ("assignee",),
    )

    assert error is None
    assert hint
    assert normalized["assignee"][0]["isQuestion"] is True
    assert normalized["assignee"][0]["value"] == "{q3_email1}"


def test_normalize_assignee_fields_resolves_field_token_string():
    client = DummyClient()
    # DummyClient has questions: q3 is "Email Address"
    normalized, hint, error = building._normalize_assignee_fields(
        client,
        "wf_1",
        {"assignee": "{Email Address}"},
        ("assignee",),
    )

    assert error is None
    assert hint
    assert normalized["assignee"][0]["isQuestion"] is True
    assert normalized["assignee"][0]["value"] == "{q3_email1}"


def test_exact_field_id_wins_over_a_colliding_question_name():
    questions = {
        "2_email": {"text": "Student Email", "type": "control_email"},
        "9": {"name": "2_email", "text": "Legacy Alias", "type": "control_textbox"},
    }

    assert building._question_id_by_token(questions, "2_email") == "2_email"



def test_connect_assign_task_requires_named_outcome():
    mcp = DummyMCP()
    client = DummyClient()
    client.element_details["5"] = {
        "element_id": 5,
        "type": "workflow_assign_task",
        "outcomes": [
            {
                "id": 1,
                "outcomeID": 1,
                "type": "CUSTOM",
                "text": "Review",
                "buttonColor": "#58d7e3",
                "textColor": "#fff",
            }
        ],
    }
    building.register(mcp, client)

    result = mcp.tools["connect_steps"]("wf_1", "5", "8")

    assert result.error
    assert "requires an outcome" in result.error
    assert "Review" in result.hint
    assert client.updated is False


def test_connect_assign_task_writes_builder_visible_link_label():
    mcp = DummyMCP()
    client = DummyClient()
    client.element_details["5"] = {
        "element_id": 5,
        "type": "workflow_assign_task",
        "outcomes": [
            {
                "id": 1,
                "outcomeID": 1,
                "type": "CUSTOM",
                "text": "Review",
                "buttonColor": "#58d7e3",
                "textColor": "#fff",
            }
        ],
    }
    building.register(mcp, client)

    result = mcp.tools["connect_steps"]("wf_1", "5", "8", outcome="Review")

    assert result.error is None
    assert result.link_id == "1"
    label_update = client.update_calls[-1]["links"][0]
    outcome_update = client.update_calls[-1]["elements"][0]
    assert label_update["action"] == "update"
    assert label_update["data"]["labels"] == [{"justCreated": True, "label": "Review"}]
    assert outcome_update["data"]["outcomes"][0]["linkID"] == 1


def test_connect_assign_task_handles_string_outcomes_returned_by_api():
    mcp = DummyMCP()
    client = DummyClient()
    client.element_details["3"] = {
        "element_id": "3",
        "type": "workflow_assign_task",
        "outcomes": ["Proceed to Interview", "Reject", "Needs More Information"],
    }
    building.register(mcp, client)

    result = mcp.tools["connect_steps"](
        "wf_1", "3", "4", outcome="Proceed to Interview"
    )

    assert result.error is None
    label_update = client.update_calls[-1]["links"][0]
    outcome_update = client.update_calls[-1]["elements"][0]
    assert label_update["data"]["labels"] == [
        {"justCreated": True, "label": "Proceed to Interview"}
    ]
    assert outcome_update["data"]["outcomes"][0]["text"] == "Proceed to Interview"
    assert outcome_update["data"]["outcomes"][0]["linkID"] == 1
    assert outcome_update["data"]["outcomes"][1] == "Reject"


def test_add_step_refuses_duplicate_name_by_default():
    mcp = DummyMCP()
    client = DummyClient()
    client.elements = [
        {"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}},
        {"element_id": 7, "type": "workflow_send_email", "name": "Application received email"},
    ]
    building.register(mcp, client)

    result = mcp.tools["add_step"]("wf_1", "workflow_send_email", {
        "name": "Application received email",
        "to": [{"text": "a@b.com"}],
        "subject": "Application received",
        "content": "Thanks",
    })

    assert result.error
    assert result.existing_step_id == "7"
    assert client.updated is False


def test_add_step_allows_duplicate_when_explicit():
    mcp = DummyMCP()
    client = DummyClient()
    client.elements = [
        {"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}},
        {"element_id": 7, "type": "workflow_send_email", "name": "Application received email"},
    ]
    building.register(mcp, client)

    result = mcp.tools["add_step"](
        "wf_1",
        "workflow_send_email",
        {
            "name": "Application received email",
            "to": [{"text": "a@b.com"}],
            "subject": "Application received",
            "content": "Thanks",
        },
        allow_duplicate=True,
    )

    assert result.error is None
    assert result.step_id == "8"
    assert client.updated is True
