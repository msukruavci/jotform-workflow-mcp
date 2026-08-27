from starlette.testclient import TestClient

from server_http import app


def test_streamable_http_connector_paths_initialize():
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "connector-test", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}

    with TestClient(app) as client:
        for path in ("/mcp", "/", "/sse"):
            response = client.post(path, json=initialize, headers=headers)

            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["mcp-session-id"]
            assert '"result"' in response.text

