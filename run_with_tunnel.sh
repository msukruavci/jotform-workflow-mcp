#!/usr/bin/env bash
# api.py'yi arka planda başlatır, sonra bir tünel açar (ngrok > cloudflared >
# localhost.run sırasıyla, hangisi kuruluysa onu kullanır).
#
# Kullanım:
#   ./run_with_tunnel.sh                              # rastgele/ephemeral tünel
#   ./run_with_tunnel.sh senin-domainin.ngrok-free.app # ngrok sabit domain varsa
#
# Ctrl+C'ye basınca hem tünel hem api.py kapanır.

set -euo pipefail

PORT=8000
NGROK_DOMAIN="${1:-}"

cleanup() {
  echo
  echo "Kapatılıyor..."
  if [[ -n "${API_PID:-}" ]] && kill -0 "$API_PID" 2>/dev/null; then
    kill "$API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "1) HTTP server başlatılıyor (arka planda, port $PORT)..."
./.venv/bin/python api.py &
API_PID=$!
sleep 2

if ! kill -0 "$API_PID" 2>/dev/null; then
  echo "[HATA] HTTP sunucusu hemen çöktü — port $PORT dolu olabilir ya da başka bir hata var."
  echo "       './.venv/bin/python api.py' komutunu ön planda çalıştırıp gerçek hatayı gör."
  exit 1
fi
echo "   -> HTTP sunucusu ayakta (PID=$API_PID), http://127.0.0.1:$PORT/sse"
echo

echo "2) Tünel açılıyor — aşağıda çıkan URL'in sonuna /sse ekleyip"
echo "   MCP connector'ının Server URL alanına onu yapıştır."
echo

if command -v cloudflared >/dev/null 2>&1 || [[ -x /home/avci/Desktop/jotform-mcp-phase1/cloudflared ]]; then
  CLOUDFLARED_BIN="$(command -v cloudflared 2>/dev/null || echo /home/avci/Desktop/jotform-mcp-phase1/cloudflared)"
  echo "   [cloudflared, hızlı tünel (uyarı sayfası yok) — URL her çalıştırmada değişir]"
  "$CLOUDFLARED_BIN" tunnel --protocol http2 --url "http://localhost:$PORT"
elif command -v ngrok >/dev/null 2>&1; then
  if [[ -n "$NGROK_DOMAIN" ]]; then
    echo "   [ngrok, sabit domain: $NGROK_DOMAIN]"
    ngrok http --url="$NGROK_DOMAIN" "$PORT"
  else
    echo "   [ngrok, rastgele domain — sabit istiyorsan: $0 senin-domainin.ngrok-free.app]"
    ngrok http "$PORT"
  fi
else
  echo "   [ngrok/cloudflared bulunamadı — localhost.run (SSH) deneniyor]"
  ssh -o ServerAliveInterval=60 -R 80:localhost:"$PORT" nokey@localhost.run
fi