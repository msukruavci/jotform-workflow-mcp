import pytest
import requests

from mcp_server import jotform_client
from mcp_server.jotform_client import (
    JotformAPIError,
    JotformClient,
    flatten_element_properties,
    unflatten_element_properties,
)


class DummyResponse:
    def __init__(self, status_code=200, text='{"content": {"ok": true}}'):
        self.status_code = status_code
        self.text = text

    def json(self):
        if self.text == "not-json":
            raise ValueError("bad json")
        return {"content": {"ok": True}}


def test_flatten_element_properties_only_targets_known_nested_workflow_fields():
    payload = {
        "pause": {"activated": "Yes", "executeWhen": {"afterAmount": "1", "afterUnit": "day"}},
        "attachment": {"name": "", "url": "", "type": ""},
        "to": [{"value": "{q3_email1}", "isQuestion": True}],
    }

    flattened = flatten_element_properties(payload)

    assert flattened["pause__activated"] == "Yes"
    assert flattened["pause__executeWhen__afterAmount"] == "1"
    assert flattened["pause__executeWhen__afterUnit"] == "day"
    assert flattened["attachment"] == {"name": "", "url": "", "type": ""}
    assert flattened["to"] == [{"value": "{q3_email1}", "isQuestion": True}]


def test_unflatten_element_properties_restores_readback_shape():
    assert unflatten_element_properties({
        "pause__activated": "Yes",
        "pause__executeWhen__afterAmount": "1",
        "name": "Wait",
    }) == {
        "pause": {"activated": "Yes", "executeWhen": {"afterAmount": "1"}},
        "name": "Wait",
    }


def test_request_redacts_api_key_from_error_response(monkeypatch):
    client = JotformClient(api_key="secret-key")

    def fake_request(*args, **kwargs):
        return DummyResponse(status_code=400, text="bad apiKey=secret-key")

    monkeypatch.setattr(requests, "request", fake_request)

    with pytest.raises(JotformAPIError) as exc:
        client.get_workflow("123")

    assert "secret-key" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


def test_request_timeout_message_does_not_include_prepared_url(monkeypatch):
    client = JotformClient(api_key="secret-key")

    def fake_request(*args, **kwargs):
        raise requests.Timeout("https://api.jotform.com?apiKey=secret-key")

    monkeypatch.setattr(requests, "request", fake_request)

    with pytest.raises(JotformAPIError) as exc:
        client.get_workflow("123")

    assert "secret-key" not in str(exc.value)
    assert "timed out" in str(exc.value)


def test_invalid_json_response_becomes_tool_readable_error(monkeypatch):
    client = JotformClient(api_key="secret-key")

    def fake_request(*args, **kwargs):
        return DummyResponse(status_code=200, text="not-json")

    monkeypatch.setattr(requests, "request", fake_request)

    with pytest.raises(JotformAPIError) as exc:
        client.get_workflow("123")

    assert "Invalid JSON response" in str(exc.value)


def test_create_form_with_ai_falls_back_to_public_form_api_when_ai_endpoint_is_unavailable(monkeypatch):
    client = JotformClient(api_key="secret-key")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json_body")))
        if path == "/workflow/copilot/createWorkflowForm":
            raise JotformAPIError(404, "Requested URL is not available")
        if path == "/form":
            assert method == "PUT"
            assert kwargs["json_body"]["properties"]["title"] == "Garanti Servis Talep Formu"
            return {"content": {"id": "form_fallback_1"}}
        if path == "/form/form_fallback_1/questions":
            return {
                "content": {
                    "1": {"text": "Garanti Servis Talep Formu", "type": "control_head"},
                    "3": {"text": "E-posta Adresi", "type": "control_email", "name": "email"},
                }
            }
        raise AssertionError(path)

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.create_form_with_ai(
        "Garanti servis talepleri için müşteri bilgisi, ürün bilgisi ve açıklama topla.",
        language="tr",
    )

    assert result["resource_id"] == "form_fallback_1"
    assert result["ai_fallback"] is True
    assert result["questions"]["3"]["type"] == "control_email"
    assert [call[:2] for call in calls] == [
        ("POST", "/workflow/copilot/createWorkflowForm"),
        ("PUT", "/form"),
        ("GET", "/form/form_fallback_1/questions"),
    ]


def test_update_tree_flattens_wire_payload_and_unflattens_echo(monkeypatch):
    client = JotformClient(api_key="secret-key")
    captured = {}

    class UpdateResponse(DummyResponse):
        def json(self):
            return {
                "content": {
                    "result": {
                        "elements": [{
                            "data": {
                                "pause__activated": "Yes",
                                "pause__executeWhen__afterAmount": "1",
                            }
                        }]
                    }
                }
            }

    def fake_request(*args, **kwargs):
        captured["json"] = kwargs["json"]
        return UpdateResponse()

    monkeypatch.setattr(requests, "request", fake_request)

    result = client.update_tree(
        "wf_1",
        elements=[{
            "action": "update",
            "elementID": 7,
            "data": {"pause": {"activated": "Yes", "executeWhen": {"afterAmount": "1"}}},
        }],
    )

    data = captured["json"]["elements"][0]["data"]
    assert "pause" not in data
    assert data["pause__activated"] == "Yes"
    assert data["pause__executeWhen__afterAmount"] == "1"
    assert result["result"]["elements"][0]["data"]["pause"]["executeWhen"]["afterAmount"] == "1"
