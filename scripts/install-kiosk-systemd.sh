#!/usr/bin/env bash
# Install / refresh axon attendance systemd units on the kiosk.
# Run on axon as vicharak (NOPASSWD sudo available).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$ROOT/deploy/systemd"
UNIT_DST=/etc/systemd/system

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

chmod 0755 \
  "$ROOT/scripts/kiosk-update.sh" \
  "$ROOT/scripts/install-kiosk-systemd.sh" \
  "$ROOT/scripts/install-kiosk-caddy.sh" \
  "$ROOT/scripts/kiosk-caddy.sh"

# Ensure Caddy binary is present (official apt repo) before enabling the unit.
"$ROOT/scripts/install-kiosk-caddy.sh"

for unit in \
  axon-attendance-api.service \
  axon-attendance-ui.service \
  axon-attendance-caddy.service \
  axon-attendance-update.service \
  axon-attendance-update.timer
do
  "${SUDO[@]}" install -m 0644 "$UNIT_SRC/$unit" "$UNIT_DST/$unit"
done

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable --now axon-attendance-api.service
"${SUDO[@]}" systemctl enable --now axon-attendance-ui.service
"${SUDO[@]}" systemctl enable --now axon-attendance-caddy.service
"${SUDO[@]}" systemctl enable --now axon-attendance-update.timer

echo "Installed. Status:"
systemctl --no-pager --full status \
  axon-attendance-api.service \
  axon-attendance-ui.service \
  axon-attendance-caddy.service \
  axon-attendance-update.timer || true
echo
echo "Logs:"
echo "  journalctl -u axon-attendance-api -f"
echo "  journalctl -u axon-attendance-ui -f"
echo "  journalctl -u axon-attendance-caddy -f"
echo "  journalctl -u axon-attendance-update -f"
echo
LAN_IP="$(python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print(s.getsockname()[0])" 2>/dev/null || true)"
if [[ -n "${LAN_IP:-}" ]]; then
  echo "LAN HTTPS: https://${LAN_IP}:8443"
  echo "Trust CA on phones: ~/.local/share/caddy/pki/authorities/local/root.crt"
fi
