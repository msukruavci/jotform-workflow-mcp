"""
Exposes the Jotform Workflow MCP server over SSE, for connectors that
reach it over HTTP (ChatGPT custom connectors, remote MCP clients) rather
than spawning it as a local stdio subprocess (Claude Desktop's mode).

Why this file looks nothing like the one it replaces: that version was
written against the mcp package's older, low-level Server API
(manually wiring SseServerTransport + InitializationOptions and calling
server.run(read_stream, write_stream, init_options)). The mcp 2.0 SDK
actually installed here uses a different top-level class, MCPServer,
with no InitializationOptions step and no get_capabilities() call needed
— it builds a ready-made ASGI app for you. Confirmed against the
installed package directly (inspect.signature), not assumed from an
older tutorial or an LLM's memory of an earlier SDK version:

    MCPServer.sse_app(sse_path="/sse", message_path="/messages/", ...) -> Starlette

That's the whole integration. CORS is added to the returned Starlette
app the same way you'd add it to any Starlette/FastAPI app.

Run:
    python api.py
Then tunnel port 8000 and point the connector's Server URL at
<tunnel>/sse — matches the /sse path this app already serves.
"""
import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from mcp_server.audit_log import write_event
from mcp_server.server import mcp

# sse_app(), given no explicit `host`, defaults that parameter to "127.0.0.1"
# — and when it sees that, it silently auto-enables DNS rebinding protection
# with an allowlist of only localhost/127.0.0.1 (mcp/server/mcpserver/server.py,
# the "Auto-enable DNS rebinding protection for localhost" branch in sse_app).
# That allowlist rejects every request that arrives through a tunnel, since
# the tunnel's Host header is never localhost — confirmed directly (421,
# "Request validation failed") and reproduced in isolation against the SDK's
# own TransportSecurityMiddleware.validate_request.
#
# localhost.run hands out a fresh random subdomain on every run
# (917975b230634d.lhr.life, then 46df2caf8920c9.lhr.life, ...), so hardcoding
# one allowed host doesn't survive a restart. Disabling the check is the
# correct tradeoff for this dev/tunnel setup specifically — same category of
# decision as allow_origins=["*"] below. Before this server has a stable
# domain (a real deployment, not a throwaway tunnel), replace this with an
# explicit TransportSecuritySettings(allowed_hosts=[...]) naming that domain.
# Modern MCP SDKs support Streamable HTTP transport via streamable_http_app.
# Claude.ai web connectors use Streamable HTTP (sending JSON-RPC messages over POST /sse).
# Using streamable_http_app handles both GET and POST /sse natively, executing
# JSON-RPC messages instead of returning static text.
app = mcp.streamable_http_app(
    streamable_http_path="/sse",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# Route root / to the same streamable HTTP handler as /sse for clients connecting at the root URL
from starlette.routing import Route  # noqa: E402
sse_endpoint = app.routes[0].endpoint
app.routes.append(Route("/", endpoint=sse_endpoint, methods=["GET", "POST", "OPTIONS", "HEAD"]))


class AuditHTTPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        body = await request.body()
        headers = dict(request.headers)
        query = dict(request.query_params)
        ua = (headers.get("user-agent") or "").lower()
        origin = (headers.get("origin") or "").lower()
        q_plat = (query.get("platform") or query.get("client") or "").lower()

        provider = None
        model = None
        if q_plat == "claude" or "claude" in ua or "anthropic" in ua or "claude.ai" in origin:
            provider = "anthropic"
            model = "Claude Web / Connector"
        elif q_plat in ("gpt", "openai", "chatgpt") or "chatgpt" in ua or "openai" in ua or "chatgpt.com" in origin or "openai.com" in origin:
            provider = "openai"
            model = "ChatGPT Developer Connector"

        write_event(
            "mcp.http.request",
            method=request.method,
            path=request.url.path,
            query=query,
            client=request.client.host if request.client else None,
            headers=headers,
            provider=provider,
            model=model,
            body=body.decode("utf-8", errors="replace") if body else "",
        )
        response = await call_next(request)
        write_event(
            "mcp.http.response",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            provider=provider,
            model=model,
        )
        return response


app.add_middleware(AuditHTTPMiddleware)

# ChatGPT and Claude connectors need to reach this from different origins —
# permissive CORS is what makes that possible. Tighten allow_origins to
# your actual connector-facing domain before this goes anywhere near
# production; "*" is fine for a local tunnel during development only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Claude & Remote MCP Client Compatibility Routes ---
# Claude.ai web connectors enforce an OAuth handshake (even when unauthenticated).
# These endpoints automatically fulfill the OAuth discovery, authorization,
# and token exchange requests so Claude connects seamlessly without needing an
# external OAuth provider or manual sign-in step.

async def mock_register(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    redirect_uris = body.get("redirect_uris", ["https://claude.ai/api/mcp/auth_callback"])
    return JSONResponse(
        {
            "client_id": "mock_client_id",
            "client_secret": "mock_client_secret",
            "client_id_issued_at": 1700000000,
            "client_secret_expires_at": 0,
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )

async def mock_authorize(request):
    redirect_uri = request.query_params.get("redirect_uri")
    state = request.query_params.get("state")
    if redirect_uri:
        delim = "&" if "?" in redirect_uri else "?"
        target = f"{redirect_uri}{delim}code=mock_code"
        if state:
            target += f"&state={state}"
        return RedirectResponse(url=target)
    return Response("OK", status_code=200)

async def mock_token(request):
    return JSONResponse({
        "access_token": "mock_token",
        "token_type": "bearer",
        "expires_in": 86400,
    })

async def oauth_meta(request):
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "registration_endpoint": f"{base_url}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "resource": base_url,
    })

from starlette.responses import RedirectResponse, JSONResponse, Response  # noqa: E402

app.add_route("/register", mock_register, methods=["POST", "GET"])
app.add_route("/authorize", mock_authorize, methods=["GET", "POST"])
app.add_route("/token", mock_token, methods=["GET", "POST"])
app.add_route("/.well-known/oauth-authorization-server", oauth_meta, methods=["GET"])
app.add_route("/.well-known/oauth-protected-resource", oauth_meta, methods=["GET"])
app.add_route("/.well-known/oauth-protected-resource/", oauth_meta, methods=["GET"])
app.add_route("/.well-known/oauth-protected-resource/sse", oauth_meta, methods=["GET"])


if __name__ == "__main__":
    print("SSE server starting on http://127.0.0.1:8000/sse")
    uvicorn.run(app, host="127.0.0.1", port=8000)

