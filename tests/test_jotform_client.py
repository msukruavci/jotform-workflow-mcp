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
