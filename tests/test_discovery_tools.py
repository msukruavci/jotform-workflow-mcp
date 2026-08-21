from mcp_server.tools import discovery


class DummyMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _tools():
    mcp = DummyMCP()
    discovery.register(mcp)
    return mcp.tools


def test_get_step_schema_single_step_backwards_compatible():
    result = _tools()["get_step_schema"](step_type="workflow_send_email")

    assert result.step_type == "workflow_send_email"
    assert result.schemas == {}
    assert result.error is None
    assert any(field.name == "subject" for field in result.fields)


def test_get_step_schema_batch_returns_schema_map():
    result = _tools()["get_step_schema"](
        step_types=["workflow_send_email", "workflow_assign_task"]
    )

    assert result.step_type is None
    assert set(result.schemas) == {"workflow_send_email", "workflow_assign_task"}
    assert result.schemas["workflow_send_email"].error is None
    assert any(field.name == "subject" for field in result.schemas["workflow_send_email"].fields)
    assert any(field.name == "taskDescription" for field in result.schemas["workflow_assign_task"].fields)


def test_get_step_schema_accepts_comma_separated_step_type_batch():
    result = _tools()["get_step_schema"](
        step_type="workflow_send_email, workflow_not_a_real_type"
    )

    assert set(result.schemas) == {"workflow_send_email", "workflow_not_a_real_type"}
    assert result.schemas["workflow_send_email"].error is None
    assert "Unknown step type" in result.schemas["workflow_not_a_real_type"].error
