import asyncio
from unittest.mock import patch

from mcp.server import MCPServer
from mcp.server.apps import APP_MIME_TYPE

from mcp_server.ui import (
    WORKFLOW_UI_LEGACY_RESOURCE_URIS,
    WORKFLOW_UI_RESOURCE_URI,
    create_workflow_apps,
)


class FakeClient:
    preview_requested = False

    def list_workflows(self):
        return [{
            "id": "wf-1",
            "title": "Verified workflow",
            "status": "ENABLED",
            "updated_at": "2026-08-14",
            "instance_summary": {"total": 3},
        }]

    def get_workflow_combined(self, workflow_id, *, fetch_essential=True):
        assert workflow_id == "wf-1"
        self.preview_requested = not fetch_essential
        return {
            "workflow": {
                "id": workflow_id,
                "title": "Verified workflow",
                "status": "ENABLED",
                "publishStatus": "DRAFT",
            },
            "elements": [
                {
                    "element_id": "1",
                    "type": "workflow_start_point",
                    "subType": "workflow_start_point_submission",
                    "resourceID": "form-1",
                    "resourceType": "FORM",
                    "x": 10,
                    "y": 20,
                },
                {
                    "element_id": "2",
                    "type": "workflow_assign_task",
                    "name": "Review request",
                    "outcomes": [{"id": 1, "text": "Complete", "linkID": 2}],
                    "x": 10,
                    "y": 200,
                },
                {
                    "element_id": "3",
                    "type": "workflow_send_email",
                    "name": "Unfinished external email",
                    "to": [{"value": "student@example.com", "text": "student@example.com"}],
                    "subject": "",
                    "content": "",
                    "x": 10,
                    "y": 380,
                },
            ],
            "links": [
                {
                    "link_id": "2",
                    "fromElement": "2",
                    "toElement": "1",
                    "fromPortName": "DYNAMIC_BOTTOM_1_Out",
                    "toPortName": "DYNAMIC_TOP_1_In",
                    "type": "default-link",
                },
                {
                    "link_id": "3",
                    "fromElement": "1",
                    "toElement": "3",
                    "fromPortName": "RIGHT_MIDDLE_Out",
                    "toPortName": "LEFT_MIDDLE_In",
                    "type": "default-link",
                },
            ],
        }

    def get_form(self, form_id):
        assert form_id == "form-1"
        return {"id": form_id, "title": "Registration form"}

    def get_form_questions(self, form_id):
        assert form_id == "form-1"
        return {
            "5": {
                "qid": "5",
                "name": "q5_department",
                "text": "Department",
                "type": "control_dropdown",
            }
        }


def _server():
    apps = create_workflow_apps(FakeClient(), html="<html><body>workflow ui</body></html>")
    return MCPServer("test", extensions=[apps])


def test_ui_resource_is_registered_with_mcp_app_mime_type():
    assert WORKFLOW_UI_RESOURCE_URI == "ui://jotform/workflows/v53.html"

    with patch.dict("os.environ", {"WORKFLOW_SETTINGS_RUNTIME_URL": ""}):
        server = _server()
    resources = asyncio.run(server.list_resources())

    resource = next(item for item in resources if str(item.uri) == WORKFLOW_UI_RESOURCE_URI)
    assert resource.mime_type == APP_MIME_TYPE
    assert "domain" not in resource.meta["ui"]
    assert resource.meta["ui"]["csp"] == {
        "connectDomains": ["https://api.jotform.com"],
        "resourceDomains": ["https://www.jotform.com", "https://cdn.jotfor.ms"],
        "frameDomains": [],
        "baseUriDomains": [],
    }
    assert resource.meta["ui"]["prefersBorder"] is True

    contents = list(asyncio.run(server.read_resource(WORKFLOW_UI_RESOURCE_URI)))
    assert contents[0].mime_type == APP_MIME_TYPE
    assert "workflow ui" in contents[0].content


def test_ui_csp_uses_exact_origins_and_disallows_nested_frames():
    with patch.dict("os.environ", {"WORKFLOW_SETTINGS_RUNTIME_URL": ""}):
        resources = asyncio.run(_server().list_resources())
    resource = next(item for item in resources if str(item.uri) == WORKFLOW_UI_RESOURCE_URI)
    csp = resource.meta["ui"]["csp"]

    assert csp["connectDomains"] == ["https://api.jotform.com"]
    assert csp["resourceDomains"] == [
        "https://www.jotform.com",
        "https://cdn.jotfor.ms",
    ]
    assert csp["frameDomains"] == []
    assert csp["baseUriDomains"] == []
    assert all("*" not in origin for origins in csp.values() for origin in origins)


def test_legacy_ui_resources_serve_the_current_bundle():
    server = _server()

    for resource_uri in WORKFLOW_UI_LEGACY_RESOURCE_URIS:
        contents = list(asyncio.run(server.read_resource(resource_uri)))
        assert contents[0].mime_type == APP_MIME_TYPE
        assert "workflow ui" in contents[0].content


def test_show_tools_are_bound_to_the_ui_resource():
    tools = asyncio.run(_server().list_tools())
    by_name = {tool.name: tool for tool in tools}

    for name in ("show_workflows", "show_workflow"):
        assert by_name[name].meta["ui"]["resourceUri"] == WORKFLOW_UI_RESOURCE_URI
        assert by_name[name].meta["openai/outputTemplate"] == WORKFLOW_UI_RESOURCE_URI
        assert by_name[name].meta["openai/widgetAccessible"] is True

    assert by_name["show_workflows"].title == "Jotform Workflows"
    assert by_name["show_workflow"].title == "Jotform Workflow"


def test_show_workflows_returns_versioned_authoritative_payload():
    result = asyncio.run(_server().call_tool("show_workflows", {}))

    assert result.structured_content["view"] == "workflow-list"
    assert result.structured_content["schemaVersion"] == 1
    assert result.structured_content["data"]["workflows"][0]["workflow_id"] == "wf-1"


def test_show_workflow_returns_versioned_authoritative_payload():
    server = _server()
    result = asyncio.run(server.call_tool("show_workflow", {"workflow_id": "wf-1"}))

    assert result.structured_content["view"] == "workflow-preview"
    assert result.structured_content["schemaVersion"] == 2
    assert result.structured_content["data"]["workflow_id"] == "wf-1"
    assert result.structured_content["data"]["workflow_url"] == "https://www.jotform.com/workflow/wf-1/build"
    assert result.structured_content["data"]["elements"][0]["element_id"] == "1"
    assert result.structured_content["data"]["elements"][0]["x"] == 10
    assert result.structured_content["data"]["elements"][0]["resourceObject"] == {
        "id": "form-1",
        "title": "Registration form",
        "questions": {
            "5": {
                "qid": "5",
                "name": "q5_department",
                "text": "Department",
                "type": "control_dropdown",
            }
        },
    }
    assert result.structured_content["data"]["elements"][1]["outcomes"][0]["linkID"] == 2
    assert result.structured_content["data"]["links"][0]["labels"] == [{"label": "Complete"}]
    assert result.structured_content["data"]["step_states"][2] == {
        "step_id": "3",
        "type": "workflow_send_email",
        "label": "Unfinished external email",
        "incoming": [{"link_id": "3", "step_id": "1", "outcome": None}],
        "outgoing": [],
        "key_config": {
            "to": ["student@example.com"],
            "subject": None,
            "content_present": False,
            "content_excerpt": None,
        },
        "missing_fields": ["subject", "content"],
        "config_complete": False,
    }
    assert result.structured_content["data"]["email_steps"] == [
        {
            "step_id": "3",
            "label": "Unfinished external email",
            "to": ["student@example.com"],
            "subject": None,
            "content_present": False,
            "content_excerpt": None,
            "missing_fields": ["subject", "content"],
            "incoming": [{"link_id": "3", "from_step": "1", "outcome": None}],
        }
    ]
    assert result.structured_content["data"]["warnings"] == []
    assert "health" not in result.structured_content["data"]
    assert "diagnostics" not in result.structured_content["data"]
    assert len(result.content) == 1
    assert "resourceObject" not in result.content[0].text
    assert len(result.content[0].text) < 500


def test_configured_settings_runtime_is_scoped_to_payload_and_csp():
    runtime_url = "https://developer.jotform.test/s/umd/dev/for-workflow-settings.js"
    with patch.dict("os.environ", {"WORKFLOW_SETTINGS_RUNTIME_URL": runtime_url}):
        server = _server()

    resource = next(
        item for item in asyncio.run(server.list_resources())
        if str(item.uri) == WORKFLOW_UI_RESOURCE_URI
    )
    result = asyncio.run(server.call_tool("show_workflow", {"workflow_id": "wf-1"}))

    assert result.structured_content["data"]["settings_runtime_url"] == runtime_url
    assert "https://developer.jotform.test" in resource.meta["ui"]["csp"]["resourceDomains"]


def test_non_https_settings_runtime_is_ignored():
    with patch.dict(
        "os.environ",
        {"WORKFLOW_SETTINGS_RUNTIME_URL": "http://localhost:3000/runtime.js"},
    ):
        server = _server()

    result = asyncio.run(server.call_tool("show_workflow", {"workflow_id": "wf-1"}))
    assert result.structured_content["data"]["settings_runtime_url"] is None
