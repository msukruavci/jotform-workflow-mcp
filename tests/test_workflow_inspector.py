from mcp_server import workflow_inspector


def test_inspector_reports_empty_links_missing_assignee_and_invalid_condition_field():
    combined = {
        "workflow": {"id": "wf_1", "title": "Demo"},
        "elements": [
            {"element_id": 1, "type": "workflow_start_point", "resourceID": "form_1"},
            {"element_id": 2, "type": "workflow_assign_task", "assignee": "", "taskDescription": ""},
            {
                "element_id": 3,
                "type": "workflow_binary_decision",
                "conditionTerms": [{"field": "Email", "operator": "equals", "value": "x"}],
                "outcomes": [
                    {"outcomeID": 1, "conditionValue": "TRUE"},
                    {"outcomeID": 2, "conditionValue": "FALSE", "linkID": 2},
                ],
            },
        ],
        "links": [
            {"link_id": 1, "fromElement": 1, "toElement": ""},
            {"link_id": 2, "fromElement": 3, "toElement": 99},
        ],
    }
    questions = {"2_email": {"text": "Email", "type": "control_email"}}

    report = workflow_inspector.inspect_workflow(combined, questions)
    categories = {issue["category"] for issue in report["issues"]}

    assert report["trigger_form_id"] == "form_1"
    assert report["ok_to_publish"] is False
    assert "empty_link" in categories
    assert "missing_assignee" in categories
    assert "invalid_condition_field" in categories
    assert "unconnected_outcome" in categories


def test_invalid_field_references_allows_real_form_field_ids():
    config = {
        "conditionTerms": [{"field": "2_email", "operator": "isFilled"}],
    }

    invalid = workflow_inspector.invalid_field_references(
        config, "workflow_binary_decision", {"2_email"}
    )

    assert invalid == []


def test_inspector_does_not_flag_present_email_content_or_question_recipient():
    combined = {
        "workflow": {"id": "wf_1", "title": "Demo"},
        "elements": [
            {"element_id": 1, "type": "workflow_start_point", "resourceID": "form_1"},
            {
                "element_id": 2,
                "type": "workflow_send_email",
                "to": [{"text": "Email Address", "value": "{q3_email1}", "isQuestion": True}],
                "subject": "Application received",
                "content": "Thanks",
            },
        ],
        "links": [{"link_id": 1, "fromElement": 1, "toElement": 2}],
    }

    report = workflow_inspector.inspect_workflow(combined, {
        "3": {"text": "Email Address", "type": "control_email"},
    })
    categories = {issue["category"] for issue in report["issues"]}

    assert "static_text_recipient" not in categories
    assert "missing_content" not in categories
    assert "missing_subject" not in categories


def test_branch_diagnostics_reports_outgoing_link_without_outcome_mapping():
    combined = {
        "workflow": {"id": "wf_1", "title": "Demo"},
        "elements": [
            {"element_id": 1, "type": "workflow_start_point", "resourceID": "form_1"},
            {
                "element_id": 2,
                "type": "workflow_assign_task",
                "outcomes": [{"outcomeID": 1, "text": "Approved"}],
            },
            {"element_id": 3, "type": "workflow_send_email", "to": [], "subject": "x", "content": "x"},
        ],
        "links": [{"link_id": 9, "fromElement": 2, "toElement": 3}],
    }

    report = workflow_inspector.inspect_workflow(combined, {})
    categories = {issue["category"] for issue in report["issues"]}

    assert "unconnected_outcome" in categories
    assert "unlabelled_branch_link" in categories


def test_branch_diagnostics_reports_outcome_link_that_leaves_another_step():
    combined = {
        "workflow": {"id": "wf_1", "title": "Demo"},
        "elements": [
            {"element_id": 1, "type": "workflow_start_point", "resourceID": "form_1"},
            {
                "element_id": 2,
                "type": "workflow_binary_decision",
                "conditionTerms": [{"field": "2_status", "operator": "equals", "value": "Yes"}],
                "outcomes": [{"outcomeID": 1, "conditionValue": "TRUE", "linkID": 7}],
            },
            {"element_id": 3, "type": "workflow_send_email", "to": [], "subject": "x", "content": "x"},
        ],
        "links": [{"link_id": 7, "fromElement": 1, "toElement": 3}],
    }

    report = workflow_inspector.inspect_workflow(
        combined,
        {"2_status": {"text": "Status", "type": "control_radio", "options": "Yes|No"}},
    )
    categories = {issue["category"] for issue in report["issues"]}

    assert "invalid_branch_link" in categories


def test_inspector_reports_choice_condition_value_not_in_form_options():
    combined = {
        "workflow": {"id": "wf_1", "title": "Demo"},
        "elements": [
            {"element_id": 1, "type": "workflow_start_point", "resourceID": "form_1"},
            {
                "element_id": 2,
                "type": "workflow_binary_decision",
                "conditionTerms": [{"field": "2_status", "operator": "equals", "value": "Maybe"}],
                "outcomes": [{"outcomeID": 1, "conditionValue": "TRUE"}],
            },
        ],
        "links": [],
    }

    report = workflow_inspector.inspect_workflow(
        combined,
        {"2_status": {"text": "Status", "type": "control_radio", "options": "Yes|No"}},
    )
    categories = {issue["category"] for issue in report["issues"]}

    assert "invalid_condition_value" in categories


def test_inspector_reports_unknown_condition_operator():
    combined = {
        "workflow": {"id": "wf_1", "title": "Demo"},
        "elements": [
            {"element_id": 1, "type": "workflow_start_point", "resourceID": "form_1"},
            {
                "element_id": 2,
                "type": "workflow_binary_decision",
                "conditionTerms": [{"field": "2_status", "operator": "roughly", "value": "Yes"}],
                "outcomes": [{"outcomeID": 1, "conditionValue": "TRUE"}],
            },
        ],
        "links": [],
    }

    report = workflow_inspector.inspect_workflow(
        combined,
        {"2_status": {"text": "Status", "type": "control_radio", "options": "Yes|No"}},
    )
    categories = {issue["category"] for issue in report["issues"]}

    assert "invalid_condition_operator" in categories
