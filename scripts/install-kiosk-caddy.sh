#!/usr/bin/env bash
# Install Caddy (official apt repo) on the Vicharak kiosk if missing.
# Safe to re-run. Does not enable axon-attendance-caddy — use install-kiosk-systemd.sh.
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

if command -v caddy >/dev/null; then
  echo "Caddy already installed: $(caddy version 2>/dev/null | head -1)"
else
  echo "Installing Caddy from official apt repo..."
  "${SUDO[@]}" apt-get update -qq
  "${SUDO[@]}" apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | "${SUDO[@]}" gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | "${SUDO[@]}" tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  "${SUDO[@]}" apt-get update -qq
  "${SUDO[@]}" apt-get install -y caddy
  echo "Installed: $(caddy version 2>/dev/null | head -1)"
fi

# Stock package unit listens on :80/:443; we use axon-attendance-caddy on :8443.
if systemctl list-unit-files caddy.service >/dev/null 2>&1; then
  "${SUDO[@]}" systemctl disable --now caddy.service 2>/dev/null || true
  "${SUDO[@]}" systemctl mask caddy.service 2>/dev/null || true
  echo "Disabled stock caddy.service (masked) — axon-attendance-caddy owns HTTPS."
fi
