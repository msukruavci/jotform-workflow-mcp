import asyncio

import httpx

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

    async def exercise_paths():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                responses = []
                for path in ("/mcp", "/", "/sse"):
                    responses.append(
                        await client.post(path, json=initialize, headers=headers)
                    )
                return responses

    for response in asyncio.run(exercise_paths()):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["mcp-session-id"]
        assert '"result"' in response.text
