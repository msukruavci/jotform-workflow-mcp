"""
Unit tests for tree_builder — no network, no API key.

This is the part of Phase 3 that can be proven rather than trusted. The
values baked into build_link_create come straight from probes/test_link_ports2.py;
a change here should fail a test before it reaches a live account.
"""
import pytest

from mcp_server import tree_builder as tb
from mcp_server import schema_registry as sr


# --- id allocation ----------------------------------------------------

def test_next_id_empty():
    assert tb.next_id([]) == 1


def test_next_id_picks_max_plus_one():
    assert tb.next_id(["1", "3", "2"]) == 4


def test_next_id_ignores_junk():
    assert tb.next_id([None, "abc", "2"]) == 3


# --- layout -------------------------------------------------------------

def test_position_no_anchor_goes_below_everything():
    elements = [
        {"element_id": "1", "position": {"x": 0, "y": 0}},
        {"element_id": "2", "position": {"x": 0, "y": 500}},
    ]
    pos = tb.compute_position(elements, after_step_id=None)
    assert pos["y"] > 500


def test_position_with_anchor_goes_directly_below_it():
    elements = [{"element_id": "5", "position": {"x": 100, "y": 200}}]
    pos = tb.compute_position(elements, after_step_id="5")
    assert pos == {"x": 100, "y": 200 + tb.STEP_Y}


def test_position_missing_anchor_falls_back_to_below_everything():
    elements = [{"element_id": "1", "position": {"x": 0, "y": 0}}]
    pos = tb.compute_position(elements, after_step_id="does-not-exist")
    assert pos["y"] > 0


def test_position_reads_flat_x_y_when_no_position_dict():
    elements = [{"element_id": "1", "x": 40, "y": 60}]
    pos = tb.compute_position(elements, after_step_id="1")
    assert pos == {"x": 40, "y": 60 + tb.STEP_Y}


def test_position_with_anchor_avoids_occupied_slot():
    elements = [
        {"element_id": "1", "position": {"x": 100, "y": 100}},
        {"element_id": "2", "position": {"x": 100, "y": 100 + tb.STEP_Y}},
    ]

    pos = tb.compute_position(elements, after_step_id="1")

    assert pos == {"x": 100 + tb.BRANCH_X, "y": 100 + tb.STEP_Y}


def test_position_branch_offset_uses_stable_column():
    elements = [{"element_id": "1", "position": {"x": 100, "y": 100}}]

    pos = tb.compute_position(elements, after_step_id="1", branch_offset=-0.5)

    assert pos == {"x": 100 - tb.BRANCH_X * 0.5, "y": 100 + tb.STEP_Y}


def test_layered_dag_layout_expands_nested_branch_lanes():
    elements = [{"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}}]
    positions = tb.compute_layered_dag_positions(
        elements,
        ["approval", "task", "denied_email", "success_email", "failure_email"],
        [
            ("start", "approval", ""),
            ("approval", "task", "Approve"),
            ("approval", "denied_email", "Deny"),
            ("task", "success_email", "Provisioned"),
            ("task", "failure_email", "Unable to Provision"),
        ],
    )

    assert positions["approval"]["y"] == tb.STEP_Y
    assert positions["task"]["y"] == positions["denied_email"]["y"] == tb.STEP_Y * 2
    assert positions["success_email"]["y"] == positions["failure_email"]["y"] == tb.STEP_Y * 3
    assert positions["task"]["x"] < positions["denied_email"]["x"]
    assert positions["success_email"]["x"] < positions["failure_email"]["x"]
    assert positions["failure_email"]["x"] <= positions["denied_email"]["x"] - tb.BRANCH_X


def test_layered_dag_layout_centers_merge_below_lowest_parent():
    elements = [{"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}}]
    positions = tb.compute_layered_dag_positions(
        elements,
        ["approval", "approved_email", "denied_email", "final_email"],
        [
            ("start", "approval", ""),
            ("approval", "approved_email", "Approve"),
            ("approval", "denied_email", "Deny"),
            ("approved_email", "final_email", ""),
            ("denied_email", "final_email", ""),
        ],
    )

    parent_center = (positions["approved_email"]["x"] + positions["denied_email"]["x"]) / 2
    lowest_parent_y = max(positions["approved_email"]["y"], positions["denied_email"]["y"])
    assert positions["final_email"]["x"] == parent_center
    assert positions["final_email"]["y"] == lowest_parent_y + tb.STEP_Y


def test_layered_dag_layout_moves_merge_to_avoid_vertical_edge_crossing():
    elements = [{"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}}]
    positions = tb.compute_layered_dag_positions(
        elements,
        ["receipt", "support_review", "finance_review", "payout", "approved_email", "rejected_email"],
        [
            ("start", "receipt", ""),
            ("receipt", "support_review", ""),
            ("support_review", "payout", "Process Refund"),
            ("support_review", "rejected_email", "Reject Refund"),
            ("support_review", "finance_review", "Escalate to Finance"),
            ("finance_review", "payout", "Approve"),
            ("finance_review", "rejected_email", "Deny"),
            ("payout", "approved_email", "Refund Issued"),
        ],
    )

    assert positions["finance_review"]["x"] == positions["support_review"]["x"]
    assert positions["payout"]["x"] != positions["support_review"]["x"]
    assert positions["approved_email"]["x"] == positions["payout"]["x"]


def test_layered_dag_layout_distributes_n_way_branch_symmetrically():
    elements = [{"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}}]
    positions = tb.compute_layered_dag_positions(
        elements,
        ["branch", "low", "medium", "high"],
        [
            ("start", "branch", ""),
            ("branch", "low", "Low"),
            ("branch", "medium", "Medium"),
            ("branch", "high", "High"),
        ],
    )

    child_xs = [positions["low"]["x"], positions["medium"]["x"], positions["high"]["x"]]
    assert child_xs == [-tb.BRANCH_X, 0, tb.BRANCH_X]
    assert all(positions[ref]["y"] == tb.STEP_Y * 2 for ref in ("low", "medium", "high"))


# --- config validation ----------------------------------------------------

def test_validate_config_keeps_known_fields():
    clean, warnings = tb.validate_config("workflow_send_email", {"subject": "Hi"})
    assert clean == {"subject": "Hi"}
    assert warnings == []


def test_validate_config_drops_unknown_field():
    clean, warnings = tb.validate_config("workflow_send_email", {"bogus": "x"})
    assert clean == {}
    assert "bogus" in warnings[0]


def test_validate_config_strips_layout_and_identity_fields():
    clean, warnings = tb.validate_config(
        "workflow_send_email", {"x": 1, "y": 2, "type": "x", "element_id": 9}
    )
    assert clean == {}
    assert warnings == []  # stripped silently, not "unknown" — these are expected noise


def test_validate_config_rejects_bad_enum_value():
    # workflow_ai_generate_text's `length` field is enum short/medium/long
    clean, warnings = tb.validate_config("workflow_ai_generate_text", {"length": "huge"})
    assert "length" not in clean
    assert warnings


def test_validate_config_unknown_type_raises():
    with pytest.raises(tb.ValidationError):
        tb.validate_config("workflow_not_a_real_type", {})


def test_verified_ui_variants_are_listed_with_canonical_type_and_subtype():
    variants = {
        item["step_type"]: item
        for item in sr.list_types()
        if item["step_type"] == "workflow_team_approval"
    }

    variant = variants["workflow_team_approval"]
    assert variant["canonical_type"] == "workflow_approval"
    assert variant["subtype"] == "workflow_team_approval"
    assert variant["ui_name"] == "Team Approval"
    assert variant["schema_available"] is True


def test_variant_schema_uses_canonical_schema_and_fixed_subtype():
    schema = sr.get_simplified_schema("workflow_pause_duration")

    assert schema["canonical_type"] == "workflow_pause"
    assert schema["subtype"] == "workflow_pause_duration"
    subtype_field = next(field for field in schema["fields"] if field["name"] == "subType")
    assert subtype_field["fixed_value"] == "workflow_pause_duration"


def test_payment_verification_schema_is_composed_from_live_verified_shape():
    schema = sr.get_simplified_schema("workflow_payment_verification")
    defaults = sr.get_field_defaults("workflow_payment_verification")

    assert schema["step_type"] == "workflow_payment_verification"
    assert sr.is_known_type("workflow_payment_verification") is True
    assert defaults["verificationMethod"] == "manual"
    assert defaults["outcomes"][0]["type"] == "VERIFY"
    assert defaults["outcomes"][1]["type"] == "NOT_VERIFY"


def test_pause_duration_schema_exposes_convenience_aliases():
    schema = sr.get_simplified_schema("workflow_pause_duration")
    fields = {field["name"]: field for field in schema["fields"]}

    assert "afterAmount" in fields
    assert fields["afterUnit"]["allowed_values"] == ["minute", "hour", "day", "week", "month", "year"]
    assert "afterAmount" not in {
        field["name"] for field in sr.get_simplified_schema("workflow_pause_wait")["fields"]
    }


def test_conditional_branch_rejects_custom_branch_without_terms():
    with pytest.raises(tb.ValidationError, match="conditionTerms"):
        tb.validate_config("workflow_conditional_branch", {
            "outcomes": [{
                "id": 1,
                "outcomeID": 1,
                "type": "CONDITION",
                "conditionValue": "CUSTOM",
                "branchName": "Olumlu Başvuru",
                "conditionTermsMatchType": "All",
                "conditionTerms": [],
            }]
        })


def test_conditional_branch_normalizes_valid_custom_branch():
    clean, warnings = tb.validate_config("workflow_conditional_branch", {
        "outcomes": [{
            "branchName": "Olumsuz Başvuru",
            "conditionTerms": [{
                "field": "1_f262233901394960",
                "operator": "startsWith",
                "value": "2006-08-07",
            }],
        }]
    })
    outcome = clean["outcomes"][0]
    term = outcome["conditionTerms"][0]
    assert warnings == []
    assert outcome["id"] == 1
    assert outcome["outcomeID"] == 1
    assert outcome["conditionValue"] == "CUSTOM"
    assert outcome["conditionTermsMatchType"] == "All"
    assert term["id"] == "term_1_1"
    assert term["isError"] is False


def test_conditional_branch_allows_other_branch_without_terms():
    clean, _ = tb.validate_config("workflow_conditional_branch", {
        "outcomes": [{
            "id": 999,
            "outcomeID": 999,
            "conditionValue": "OTHER",
            "conditionTerms": [],
        }]
    })
    assert clean["outcomes"][0]["conditionValue"] == "OTHER"
    assert clean["outcomes"][0]["conditionTerms"] == []


# --- element payloads -----------------------------------------------------

def test_build_element_create_shape():
    entry = tb.build_element_create(
        "workflow_send_email", 3, {"subject": "Hi"}, {"x": 10, "y": 20}
    )
    assert entry["action"] == "create"
    assert entry["elementID"] == 3
    assert entry["data"]["type"] == "workflow_send_email"
    assert entry["data"]["subject"] == "Hi"
    assert entry["data"]["x"] == 10 and entry["data"]["position"]["x"] == 10


def test_build_element_create_resolves_verified_ui_variant_subtype():
    entry = tb.build_element_create(
        "workflow_send_pdf", 3, {"name": "PDF"}, {"x": 10, "y": 20}
    )

    assert entry["data"]["type"] == "workflow_send_email"
    assert entry["data"]["elementType"] == "workflow_send_email"
    assert entry["data"]["subType"] == "workflow_send_pdf"
    assert entry["data"]["name"] == "PDF"


def test_pause_duration_aliases_are_normalized_to_nested_execute_when():
    clean, warnings = tb.validate_config(
        "workflow_pause_duration",
        {"afterAmount": 2, "afterUnit": "days"},
    )

    assert warnings == []
    assert clean["pause"] == {
        "activated": "Yes",
        "executeWhen": {"afterAmount": "2", "afterUnit": "day"},
    }


def test_build_element_update_shape():
    entry = tb.build_element_update(3, {"subject": "New"})
    assert entry["action"] == "update"
    assert entry["data"]["subject"] == "New"
    assert "type" not in entry["data"]  # update never re-sends type


# --- link payloads: pinned to the measured API rules -----------------------

def test_build_link_create_matches_measured_working_payload():
    entry = tb.build_link_create(7, from_id=1, to_id=2)
    data = entry["data"]
    assert data["type"] == "default-link"
    assert data["points"] == [{"a": "1"}]  # must be non-empty; content is ignored
    assert data["fromPortName"] and data["toPortName"]  # presence required
    assert data["fromElement"] == 1 and data["toElement"] == 2


def test_link_type_is_never_caller_supplied():
    """type is unvalidated AND uncorrected by the API (probes/test_link_ports2.py) —
    a typo here produces a silently broken link. It must be a constant."""
    entry = tb.build_link_create(1, 1, 2)
    assert entry["data"]["type"] == tb.LINK_DEFAULTS["type"] == "default-link"


def test_build_link_label_update_matches_builder_outcome_payload():
    entry = tb.build_link_label_update(6, "Review")

    assert entry == {
        "action": "update",
        "linkID": 6,
        "data": {
            "id": 6,
            "link_id": 6,
            "labels": [{"justCreated": True, "label": "Review"}],
        },
    }


# --- outcome resolution -----------------------------------------------------

def test_resolve_outcome_matches_case_insensitively():
    el = {"outcomes": [{"outcomeID": 1, "conditionValue": "TRUE"},
                        {"outcomeID": 2, "conditionValue": "FALSE"}]}
    match = tb.resolve_outcome(el, "true")
    assert match["outcomeID"] == 1


def test_resolve_outcome_unknown_label_lists_available():
    el = {"outcomes": [{"outcomeID": 1, "conditionValue": "TRUE"},
                        {"outcomeID": 2, "conditionValue": "FALSE"}]}
    with pytest.raises(tb.ValidationError, match="TRUE.*FALSE|FALSE.*TRUE"):
        tb.resolve_outcome(el, "maybe")


def test_resolve_outcome_already_connected_refuses():
    el = {"outcomes": [{"outcomeID": 1, "conditionValue": "TRUE", "linkID": 9}]}
    with pytest.raises(tb.ValidationError, match="already connected"):
        tb.resolve_outcome(el, "TRUE")


def test_resolve_outcome_treats_string_zero_as_unconnected():
    el = {"outcomes": [{"outcomeID": 1, "conditionValue": "TRUE", "linkID": "0"}]}

    assert tb.resolve_outcome(el, "TRUE")["outcomeID"] == 1


def test_build_outcome_update_preserves_other_outcomes():
    el = {
        "element_id": "2",
        "outcomes": [
            {"outcomeID": 1, "conditionValue": "TRUE", "linkID": None},
            {"outcomeID": 2, "conditionValue": "FALSE", "linkID": None},
        ],
    }
    entry = tb.build_outcome_update(el, outcome_id=1, link_id=5)
    outcomes = entry["data"]["outcomes"]
    assert outcomes[0]["linkID"] == 5
    assert outcomes[1]["linkID"] is None  # untouched, not dropped
    assert outcomes[1]["conditionValue"] == "FALSE"


def test_build_outcome_clears_for_links_only_touches_matching_links():
    el = {
        "element_id": "2",
        "outcomes": [
            {"outcomeID": 1, "conditionValue": "TRUE", "linkID": 5},
            {"outcomeID": 2, "conditionValue": "FALSE", "linkID": 6},
        ],
    }
    entry = tb.build_outcome_clears_for_links(el, [5, 99])
    outcomes = entry["data"]["outcomes"]
    assert outcomes[0]["linkID"] is None
    assert outcomes[1]["linkID"] == 6


def test_build_outcome_clears_for_links_returns_none_without_match():
    el = {
        "element_id": "2",
        "outcomes": [{"outcomeID": 1, "conditionValue": "TRUE", "linkID": 5}],
    }
    assert tb.build_outcome_clears_for_links(el, [99]) is None


# --- default outcome injection ---------------------------------------------
# Measured finding: the JSON Schema `default` for `outcomes` is not applied
# by Jotform's server on create — an if/else created without it comes back
# with no outcomes at all, and connect_steps then has nothing to wire to,
# permanently. tree_builder fills this in the same way it fills in ports:
# it's plumbing, not something a model should have to know to supply.

def test_binary_decision_gets_true_false_outcomes_by_default():
    entry = tb.build_element_create("workflow_binary_decision", 2, {}, {"x": 0, "y": 0})
    values = {o["conditionValue"] for o in entry["data"]["outcomes"]}
    assert values == {"TRUE", "FALSE"}


def test_conditional_branch_gets_a_default_outcome():
    entry = tb.build_element_create("workflow_conditional_branch", 2, {}, {"x": 0, "y": 0})
    assert entry["data"]["outcomes"]


def test_non_branching_type_gets_no_outcomes_field():
    entry = tb.build_element_create("workflow_send_email", 2, {}, {"x": 0, "y": 0})
    assert "outcomes" not in entry["data"]


def test_caller_supplied_outcomes_are_not_overwritten():
    custom = [{"outcomeID": 1, "conditionValue": "CUSTOM"}]
    entry = tb.build_element_create(
        "workflow_binary_decision", 2, {"outcomes": custom}, {"x": 0, "y": 0}
    )
    assert entry["data"]["outcomes"] == custom


# --- general field-default injection ---------------------------------------
# Measured 2026-08-10 (probes/compare_element_shapes.py): a workflow built
# entirely through this project opened to a blank canvas in Jotform's own
# builder. Jotform's server auto-defaults most fields on create, but not a
# specific handful — email `to`, decision `conditionTerms`, task `assignee`
# and `outcomes` — all arrays a renderer would iterate over, plausibly
# crashing on `undefined` client-side. The old branching-only outcomes
# special case was generalized into schema_registry.get_field_defaults,
# applied to every field with a declared schema default. These tests pin
# the fields actually proven missing, not just the ones checked before.

def test_email_gets_empty_recipient_list_by_default():
    entry = tb.build_element_create("workflow_send_email", 2, {}, {"x": 0, "y": 0})
    assert entry["data"]["to"] == []


def test_task_gets_empty_assignee_and_complete_button_by_default():
    entry = tb.build_element_create("workflow_assign_task", 2, {}, {"x": 0, "y": 0})
    assert entry["data"]["assignee"] == ""
    assert entry["data"]["outcomes"][0]["text"] == "Complete"


def test_task_outcome_strings_are_normalized_to_builder_objects():
    clean, warnings = tb.validate_config(
        "workflow_assign_task",
        {"outcomes": ["Proceed to Interview", "Reject"]},
    )

    assert warnings == []
    assert clean["outcomes"] == [
        {
            "id": 1,
            "outcomeID": 1,
            "type": "CUSTOM",
            "buttonColor": "#0075E3",
            "text": "Proceed to Interview",
            "textColor": "#FFFFFF",
        },
        {
            "id": 2,
            "outcomeID": 2,
            "type": "CUSTOM",
            "buttonColor": "#0075E3",
            "text": "Reject",
            "textColor": "#FFFFFF",
        },
    ]


def test_decision_gets_empty_condition_terms_by_default():
    entry = tb.build_element_create("workflow_binary_decision", 2, {}, {"x": 0, "y": 0})
    assert entry["data"]["conditionTerms"] == []


def test_caller_supplied_recipient_list_is_not_overwritten():
    entry = tb.build_element_create(
        "workflow_send_email", 2, {"to": [{"text": "a@b.com"}]}, {"x": 0, "y": 0}
    )
    assert entry["data"]["to"] == [{"text": "a@b.com"}]


def test_name_is_never_auto_defaulted():
    """A real, correctly-rendering Jotform element can omit `name` entirely
    (confirmed against a live reference workflow) — injecting a generic
    default would be a needless deviation, so this field is excluded even
    though the schema declares one."""
    entry = tb.build_element_create("workflow_send_email", 2, {}, {"x": 0, "y": 0})
    assert "name" not in entry["data"]


def test_defaults_do_not_leak_across_calls():
    """copy.deepcopy in get_field_defaults matters: without it, mutating one
    element's injected list/dict would corrupt every other element created
    from the same step type."""
    a = tb.build_element_create("workflow_assign_task", 2, {}, {"x": 0, "y": 0})
    a["data"]["outcomes"][0]["text"] = "MUTATED"
    b = tb.build_element_create("workflow_assign_task", 3, {}, {"x": 0, "y": 0})
    assert b["data"]["outcomes"][0]["text"] == "Complete"


# --- workflow_approval branching: a real gap found via ChatGPT testing ------
# 2026-08-11. A tester's checklist expected "connect the Approve outcome"
# to work the same way it does on an if/else. It didn't, for two separate,
# stacked reasons — both confirmed against real data, neither guessed:
#
#   1. workflow_approval was never in BRANCHING_TYPES, so connect_steps
#      didn't even recognise it as a step that branches.
#   2. Its outcome objects carry no `conditionValue` at all — they use
#      `text`/`type` instead (workflow_binary_decision's field). Fixing (1)
#      alone would still fail every match.
#
# The verified defaults below are copied from probes/inspect_approval_outcomes.py's
# real output against an actual approval step, with `linkID` stripped (that
# value belonged to an already-wired instance; carrying it into a fresh
# step's default would make resolve_outcome think Approve/Deny were already
# connected to something that doesn't exist for a new step).

def test_workflow_approval_is_now_branching():
    assert "workflow_approval" in sr.BRANCHING_TYPES


def test_workflow_approval_gets_verified_approve_deny_defaults():
    entry = tb.build_element_create("workflow_approval", 2, {}, {"x": 0, "y": 0})
    outcomes = entry["data"]["outcomes"]
    types = {o["type"] for o in outcomes}
    assert types == {"APPROVE", "DENY"}
    assert all("linkID" not in o for o in outcomes)  # never a stale link


def test_resolve_outcome_matches_approval_by_text_not_conditionvalue():
    el = {"outcomes": [
        {"outcomeID": 1, "type": "APPROVE", "text": "Approve"},
        {"outcomeID": 2, "type": "DENY", "text": "Deny"},
    ]}
    assert tb.resolve_outcome(el, "approve")["outcomeID"] == 1
    assert tb.resolve_outcome(el, "Deny")["outcomeID"] == 2


def test_resolve_task_outcome_returned_as_string_by_jotform():
    el = {"outcomes": ["Proceed to Interview", "Reject"]}

    match = tb.resolve_outcome(el, "Proceed to Interview")

    assert match["outcomeID"] == 1
    assert match["text"] == "Proceed to Interview"


def test_build_outcome_update_converts_task_string_outcome_to_object():
    el = {
        "element_id": "3",
        "type": "workflow_assign_task",
        "outcomes": ["Proceed to Interview", "Reject"],
    }

    entry = tb.build_outcome_update(el, outcome_id=1, link_id=7)

    assert entry["data"]["outcomes"][0] == {
        "id": 1,
        "outcomeID": 1,
        "type": "CUSTOM",
        "buttonColor": "#0075E3",
        "text": "Proceed to Interview",
        "textColor": "#FFFFFF",
        "linkID": 7,
    }
    assert entry["data"]["outcomes"][1] == "Reject"


def test_resolve_outcome_falls_back_to_type_if_no_text():
    el = {"outcomes": [{"outcomeID": 1, "type": "APPROVE"}]}
    assert tb.resolve_outcome(el, "APPROVE")["outcomeID"] == 1


def test_resolve_outcome_still_prefers_conditionvalue_for_decisions():
    """The generalisation must not break the original, most common case."""
    el = {"outcomes": [{"outcomeID": 1, "conditionValue": "TRUE"}]}
    assert tb.resolve_outcome(el, "true")["outcomeID"] == 1


def test_resolve_outcome_error_lists_real_labels_not_none():
    """Before the fix, an unmatched approval outcome error showed
    "Available: [None, None]" — useless to whoever reads it."""
    el = {"outcomes": [{"outcomeID": 1, "type": "APPROVE", "text": "Approve"}]}
    with pytest.raises(tb.ValidationError, match="Approve"):
        tb.resolve_outcome(el, "Maybe")
