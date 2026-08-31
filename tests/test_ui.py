import asyncio

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
            ],
            "links": [{
                "link_id": "2",
                "fromElement": "2",
                "toElement": "1",
                "fromPortName": "DYNAMIC_BOTTOM_1_Out",
                "toPortName": "DYNAMIC_TOP_1_In",
                "type": "default-link",
            }],
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
    assert WORKFLOW_UI_RESOURCE_URI == "ui://jotform/workflows/v46.html"

    server = _server()
    resources = asyncio.run(server.list_resources())

    resource = next(item for item in resources if str(item.uri) == WORKFLOW_UI_RESOURCE_URI)
    assert resource.mime_type == APP_MIME_TYPE
    assert "domain" not in resource.meta["ui"]
    assert resource.meta["ui"]["csp"] == {
        "connectDomains": ["https://api.jotform.com", "https://*.jotform.com"],
        "resourceDomains": ["https://*.jotform.com", "https://*.jotform.io", "https://cdn.jotfor.ms"],
        "frameDomains": [],
        "baseUriDomains": [],
    }
    assert resource.meta["ui"]["prefersBorder"] is True

    contents = list(asyncio.run(server.read_resource(WORKFLOW_UI_RESOURCE_URI)))
    assert contents[0].mime_type == APP_MIME_TYPE
    assert "workflow ui" in contents[0].content


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
