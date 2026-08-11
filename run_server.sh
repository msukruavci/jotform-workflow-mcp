#!/usr/bin/env bash
# Entry point for the MCP server.
#
# Exists because MCP clients (Inspector, Claude Desktop) launch the server as
# a subprocess with a different working directory and a minimal PATH, so
# relative paths and bare `python` both fail. This fixes cwd and uses an
# absolute interpreter, so clients only ever need this one path.
set -euo pipefail
cd "$(dirname "$0")"
exec ./.venv/bin/python -m mcp_server.server
