#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAN_IP="$(python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print(s.getsockname()[0])")"
CA_CERT="$HOME/Library/Application Support/Caddy/pki/authorities/local/root.crt"
WAIT_FOR_FLASK=false

for arg in "$@"; do
  case "$arg" in
    --wait) WAIT_FOR_FLASK=true ;;
  esac
done

cd "$ROOT"

if ! command -v caddy >/dev/null; then
  echo "Install Caddy: brew install caddy"
  exit 1
fi

if ! curl -sf http://127.0.0.1:1337/ >/dev/null; then
  if "$WAIT_FOR_FLASK"; then
    echo "Waiting for Flask on :1337..."
    until curl -sf http://127.0.0.1:1337/ >/dev/null; do
      sleep 1
    done
  else
    echo "Flask is not running on :1337. Start it first:"
    echo "  uv run python -m src.api.server"
    exit 1
  fi
fi

echo "LAN HTTPS URL for iPhone: https://${LAN_IP}:8443"
echo "Test endpoints:"
echo "  https://${LAN_IP}:8443/test/clock/ist"
echo "  https://${LAN_IP}:8443/test/clock/utc"
echo ""
echo "Trust Caddy's cert on iPhone (one-time):"
echo "  1. AirDrop or email this file to your phone:"
echo "     ${CA_CERT}"
echo "  2. Settings → General → VPN & Device Management → install profile"
echo "  3. Settings → General → About → Certificate Trust Settings → enable full trust"
echo ""
echo "PWA LAN Test / Railway env:"
echo "  VITE_AXON_API_URL=https://${LAN_IP}:8443"
echo ""

exec caddy run --config Caddyfile
