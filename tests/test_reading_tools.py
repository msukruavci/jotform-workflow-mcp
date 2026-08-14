from mcp_server.tools import reading


def test_field_options_splits_jotform_pipe_options():
    assert reading._field_options({"options": "Designer|Engineer|Other"}) == [
        "Designer",
        "Engineer",
        "Other",
    ]


def test_field_options_accepts_list_options():
    assert reading._field_options({"options": ["Yes", "No"]}) == ["Yes", "No"]
