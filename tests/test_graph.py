"""
Unit tests for the graph analysis.

These need no API key and no network — that's the point. Everything else in
the reading layer is only as correct as the last live call; this part can be
proved. The fixture is the real 18-step workflow from the test account, so a
regression here shows up as a change in a number we've already eyeballed.

Run:  python -m pytest tests/ -q
"""
from mcp_server.graph import analyse

REAL_STEPS = [
    {"step_id": "1", "type": "workflow_start_point"},
    {"step_id": "2", "type": "workflow_binary_decision"},
    {"step_id": "3", "type": "workflow_send_email"},
    {"step_id": "4", "type": "workflow_assign_task"},
    {"step_id": "5", "type": "workflow_end_point"},
    {"step_id": "6", "type": "workflow_pause"},
    {"step_id": "7", "type": "workflow_pause"},
    {"step_id": "8", "type": "workflow_merge"},
    {"step_id": "9", "type": "workflow_split"},
    {"step_id": "10", "type": "workflow_split"},
    {"step_id": "11", "type": "workflow_merge"},
    {"step_id": "12", "type": "workflow_merge"},
    {"step_id": "13", "type": "workflow_merge"},
    {"step_id": "14", "type": "workflow_conditional_branch"},
    {"step_id": "15", "type": "workflow_split"},
    {"step_id": "16", "type": "workflow_merge"},
    {"step_id": "17", "type": "workflow_pause"},
    {"step_id": "18", "type": "workflow_payment_verification"},
]
REAL_LINKS = [
    {"from_step": "1", "to_step": "2"},
    {"from_step": "2", "to_step": "10"},
    {"from_step": "2", "to_step": "4"},
    {"from_step": "10", "to_step": "3"},
    {"from_step": "10", "to_step": "13"},
    {"from_step": "13", "to_step": "11"},
    {"from_step": "13", "to_step": "14"},
]


def test_real_workflow_orphans():
    r = analyse(REAL_STEPS, REAL_LINKS)
    assert r["total_steps"] == 18
    assert r["unreachable_steps"] == ["5", "6", "7", "8", "9", "12", "15", "16", "17", "18"]


def test_end_point_is_not_a_dead_end():
    steps = [
        {"step_id": "1", "type": "workflow_start_point"},
        {"step_id": "2", "type": "workflow_end_point"},
    ]
    r = analyse(steps, [{"from_step": "1", "to_step": "2"}])
    assert r["dead_end_steps"] == []
    assert r["unreachable_steps"] == []


def test_reached_step_with_no_exit_is_a_dead_end():
    steps = [
        {"step_id": "1", "type": "workflow_start_point"},
        {"step_id": "2", "type": "workflow_send_email"},
    ]
    r = analyse(steps, [{"from_step": "1", "to_step": "2"}])
    assert r["dead_end_steps"] == ["2"]


def test_cycle_does_not_hang():
    steps = [
        {"step_id": "1", "type": "workflow_start_point"},
        {"step_id": "2", "type": "workflow_send_email"},
        {"step_id": "3", "type": "workflow_send_email"},
    ]
    links = [
        {"from_step": "1", "to_step": "2"},
        {"from_step": "2", "to_step": "3"},
        {"from_step": "3", "to_step": "2"},
    ]
    assert analyse(steps, links)["unreachable_steps"] == []


def test_ids_compare_as_strings_not_ints():
    """Jotform mixes int and str ids across endpoints; a link keyed on int 2
    must still match a step keyed on str "2"."""
    steps = [
        {"step_id": 1, "type": "workflow_start_point"},
        {"step_id": 2, "type": "workflow_end_point"},
    ]
    assert analyse(steps, [{"from_step": 1, "to_step": 2}])["unreachable_steps"] == []


def test_empty_workflow():
    assert analyse([], [])["total_steps"] == 0


# --- branch label mapping -------------------------------------------------
# The label lives on the deciding element (outcomes[].linkID), not the link.
# These lock that in, since the wrong answer (fromPortName) looked right on
# real data and would have silently mislabelled every branch.

from mcp_server.tools.reading import _outcome_map


def test_binary_decision_outcomes_map_to_links():
    elements = [{
        "element_id": 2,
        "type": "workflow_binary_decision",
        "outcomes": [
            {"outcomeID": 1, "conditionValue": "TRUE", "linkID": 2},
            {"outcomeID": 2, "conditionValue": "FALSE", "linkID": 3},
        ],
    }]
    mapping, unconnected = _outcome_map(elements)
    assert mapping == {"2": "TRUE", "3": "FALSE"}
    assert unconnected == []


def test_branch_with_no_link_is_reported():
    elements = [{
        "element_id": 2,
        "type": "workflow_binary_decision",
        "outcomes": [
            {"outcomeID": 1, "conditionValue": "TRUE", "linkID": 2},
            {"outcomeID": 2, "conditionValue": "FALSE", "linkID": None},
        ],
    }]
    mapping, unconnected = _outcome_map(elements)
    assert mapping == {"2": "TRUE"}
    assert unconnected == ["step 2 FALSE"]


def test_split_carries_no_outcomes():
    """A split's paths are equivalent — it must not be treated as branching."""
    mapping, unconnected = _outcome_map([{"element_id": 9, "type": "workflow_split"}])
    assert mapping == {} and unconnected == []


def test_conditional_branch_uses_its_own_names():
    elements = [{
        "element_id": 14,
        "type": "workflow_conditional_branch",
        "outcomes": [
            {"outcomeID": 1, "conditionValue": "Refund", "linkID": 10},
            {"outcomeID": 999, "conditionValue": "OTHER", "linkID": 11},
        ],
    }]
    mapping, _ = _outcome_map(elements)
    assert mapping == {"10": "Refund", "11": "OTHER"}


# --- dangling links -------------------------------------------------------
# Whether the API cleans up links when their step is deleted is unverified
# (probes/test_delete_impact.py). This is a safety net regardless of the
# answer: a link naming a step_id that doesn't exist must be reported, not
# silently treated as a normal connection.

def test_dangling_link_to_missing_step_is_reported():
    steps = [{"step_id": "1", "type": "workflow_start_point"}]
    links = [{"link_id": "1", "from_step": "1", "to_step": "99"}]
    assert analyse(steps, links)["dangling_links"] == ["link 1: to missing step 99"]


def test_dangling_link_from_missing_step_is_reported():
    steps = [{"step_id": "1", "type": "workflow_start_point"}]
    links = [{"link_id": "1", "from_step": "99", "to_step": "1"}]
    assert analyse(steps, links)["dangling_links"] == ["link 1: from missing step 99"]


def test_normal_links_are_not_flagged_dangling():
    steps = [{"step_id": "1", "type": "workflow_start_point"},
             {"step_id": "2", "type": "workflow_end_point"}]
    links = [{"link_id": "1", "from_step": "1", "to_step": "2"}]
    assert analyse(steps, links)["dangling_links"] == []