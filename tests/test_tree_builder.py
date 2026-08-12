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