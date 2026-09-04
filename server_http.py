"""Streamable HTTP wrapper for the Jotform Workflow MCP server.

``/mcp`` is the canonical endpoint.  ``/`` and the project's historical
``/sse`` URL are kept as aliases so existing ChatGPT and Claude connections do
not break when the transport entrypoint changes.
"""
import os

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from mcp_server.server import mcp
from starlette.routing import Route

# Disable DNS rebinding host validation so public tunnel URLs (like Cloudflare) work without 421 errors
sec_settings = TransportSecuritySettings(enable_dns_rebinding_protection=False)
app = mcp.streamable_http_app(transport_security=sec_settings)

# Existing connector configurations in this project used either the public
# tunnel root or ``/sse``.  They must reach the same Streamable HTTP ASGI
# handler as the SDK's canonical ``/mcp`` route; otherwise both clients receive
# a plain 404 before MCP initialization can begin.
_mcp_endpoint = app.routes[0].endpoint
app.routes.extend(
    (
        Route("/", endpoint=_mcp_endpoint),
        Route("/sse", endpoint=_mcp_endpoint),
    )
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Starting Jotform Workflow MCP HTTP server on 0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
