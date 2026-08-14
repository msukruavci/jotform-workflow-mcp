#!/usr/bin/env bash
# Start the OpenAI Secure Tunnel for this MCP server.
#
# This is the one-command path for testing the connector in ChatGPT:
# it loads this project's .env, maps OPENAI_API_KEY to the tunnel client's
# preferred CONTROL_PLANE_API_KEY name, then starts the existing
# jotform-workflow tunnel profile. The profile launches run_server.sh over
# stdio, so api.py/port 8000 are not involved.

set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -z "${CONTROL_PLANE_API_KEY:-}" && -n "${OPENAI_API_KEY:-}" ]]; then
  export CONTROL_PLANE_API_KEY="$OPENAI_API_KEY"
fi

if [[ -z "${CONTROL_PLANE_API_KEY:-}" ]]; then
  echo "[HATA] OPENAI_API_KEY veya CONTROL_PLANE_API_KEY .env içinde yok."
  exit 1
fi

TUNNEL_CLIENT="${TUNNEL_CLIENT:-/home/avci/Desktop/jotform-mcp-phase1/tunnel-client}"
PROFILE="${TUNNEL_CLIENT_PROFILE:-jotform-workflow}"

if [[ ! -x "$TUNNEL_CLIENT" ]]; then
  echo "[HATA] tunnel-client bulunamadı veya executable değil: $TUNNEL_CLIENT"
  exit 1
fi

echo "OpenAI Secure Tunnel başlatılıyor..."
echo "profile=$PROFILE"
echo "mcp_command=$(pwd)/run_server.sh"
echo "admin_ui=http://127.0.0.1:8080/ui"
echo

ARGS=(run --profile "$PROFILE" --control-plane.api-key env:CONTROL_PLANE_API_KEY)

if [[ -n "${TUNNEL_ID:-}" ]]; then
  echo "not: .env içinde TUNNEL_ID var, ama profil tunnel_id'si kullanılacak."
  echo "     Override etmek istersen JFMCP_USE_ENV_TUNNEL_ID=1 jfmcp-tunnel kullan."
fi

if [[ "${JFMCP_USE_ENV_TUNNEL_ID:-}" == "1" && -n "${TUNNEL_ID:-}" ]]; then
  ARGS+=(--control-plane.tunnel-id "$TUNNEL_ID")
fi

exec "$TUNNEL_CLIENT" "${ARGS[@]}"
