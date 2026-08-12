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
  "$ROOT/scripts/install-kiosk-systemd.sh"

for unit in \
  axon-attendance-api.service \
  axon-attendance-ui.service \
  axon-attendance-update.service \
  axon-attendance-update.timer
do
  "${SUDO[@]}" install -m 0644 "$UNIT_SRC/$unit" "$UNIT_DST/$unit"
done

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable --now axon-attendance-api.service
"${SUDO[@]}" systemctl enable --now axon-attendance-ui.service
"${SUDO[@]}" systemctl enable --now axon-attendance-update.timer

# LAN Caddy retired — HTTPS is via cloudflared. Leave unit disabled if present.
if [[ -f "$UNIT_DST/axon-attendance-caddy.service" ]]; then
  "${SUDO[@]}" systemctl disable --now axon-attendance-caddy.service >/dev/null 2>&1 || true
fi

echo "Installed. Status:"
systemctl --no-pager --full status \
  axon-attendance-api.service \
  axon-attendance-ui.service \
  axon-attendance-update.timer || true
echo
echo "Logs:"
echo "  journalctl -u axon-attendance-api -f"
echo "  journalctl -u axon-attendance-ui -f"
echo "  journalctl -u axon-attendance-update -f"
echo
echo "HTTPS: cloudflared tunnel (axon-attendance-caddy is not enabled)"
