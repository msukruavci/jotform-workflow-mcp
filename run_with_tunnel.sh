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

echo "1) api.py başlatılıyor (arka planda, port $PORT)..."
python api.py &
API_PID=$!
sleep 2

if ! kill -0 "$API_PID" 2>/dev/null; then
  echo "[HATA] api.py hemen çöktü — port $PORT dolu olabilir ya da başka bir hata var."
  echo "       'python api.py' komutunu ön planda çalıştırıp gerçek hatayı gör."
  exit 1
fi
echo "   -> api.py ayakta (PID=$API_PID), http://127.0.0.1:$PORT/sse"
echo

echo "2) Tünel açılıyor — aşağıda çıkan URL'in sonuna /sse ekleyip"
echo "   ChatGPT connector'ının Server URL alanına onu yapıştır."
echo

if command -v ngrok >/dev/null 2>&1; then
  if [[ -n "$NGROK_DOMAIN" ]]; then
    echo "   [ngrok, sabit domain: $NGROK_DOMAIN]"
    ngrok http --url="$NGROK_DOMAIN" "$PORT"
  else
    echo "   [ngrok, rastgele domain — sabit istiyorsan: $0 senin-domainin.ngrok-free.app]"
    ngrok http "$PORT"
  fi
elif command -v cloudflared >/dev/null 2>&1; then
  echo "   [cloudflared, hızlı tünel — URL her çalıştırmada değişir]"
  cloudflared tunnel --url "http://localhost:$PORT"
else
  echo "   [ngrok/cloudflared bulunamadı — localhost.run (SSH) deneniyor]"
  ssh -o ServerAliveInterval=60 -R 80:localhost:"$PORT" nokey@localhost.run
fi