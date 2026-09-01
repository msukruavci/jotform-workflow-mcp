from mcp_server.tools import reading


class DummyMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def test_field_options_splits_jotform_pipe_options():
    assert reading._field_options({"options": "Designer|Engineer|Other"}) == [
        "Designer",
        "Engineer",
        "Other",
    ]


def test_field_options_accepts_list_options():
    assert reading._field_options({"options": ["Yes", "No"]}) == ["Yes", "No"]


def test_get_form_fields_preserves_the_canonical_question_name():
    class Client:
        def get_form_questions(self, form_id):
            assert form_id == "form-1"
            return {
                "3": {
                    "name": "q3_email",
                    "text": "Email",
                    "type": "control_email",
                    "required": "Yes",
                },
            }

    mcp = DummyMCP()
    reading.register(mcp, Client())

    result = mcp.tools["get_form_fields"]("form-1")

    assert result.fields[0].field_id == "3"
    assert result.fields[0].name == "q3_email"
