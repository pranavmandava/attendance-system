#!/usr/bin/env bash
# Run the Cloudflare tunnel that exposes local Flask at
# https://mbp.korukondacoachingcentre.com → http://127.0.0.1:1337
#
# Token resolution (first match wins):
#   1. CLOUDFLARED_TUNNEL_TOKEN env
#   2. Existing LaunchDaemon plist (installed via `cloudflared service install`)
set -euo pipefail

PLIST="/Library/LaunchDaemons/com.cloudflare.cloudflared.plist"
WAIT_FOR_FLASK=false

for arg in "$@"; do
  case "$arg" in
    --wait) WAIT_FOR_FLASK=true ;;
  esac
done

if ! command -v cloudflared >/dev/null; then
  echo "Install cloudflared: nix/brew, or https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/"
  exit 1
fi

if [[ -z "${CLOUDFLARED_TUNNEL_TOKEN:-}" ]]; then
  if [[ -f "$PLIST" ]]; then
    # Prefer PlistBuddy; fall back to python if unavailable.
    if command -v /usr/libexec/PlistBuddy >/dev/null; then
      CLOUDFLARED_TUNNEL_TOKEN="$(
        /usr/libexec/PlistBuddy -c 'Print :ProgramArguments:4' "$PLIST" 2>/dev/null || true
      )"
    fi
    if [[ -z "${CLOUDFLARED_TUNNEL_TOKEN:-}" ]]; then
      CLOUDFLARED_TUNNEL_TOKEN="$(
        python3 - <<'PY'
import plistlib
from pathlib import Path
plist = plistlib.loads(Path("/Library/LaunchDaemons/com.cloudflare.cloudflared.plist").read_bytes())
args = plist.get("ProgramArguments") or []
# [... cloudflared, tunnel, run, --token, <TOKEN>]
for i, a in enumerate(args):
    if a == "--token" and i + 1 < len(args):
        print(args[i + 1])
        break
PY
      )"
    fi
  fi
fi

if [[ -z "${CLOUDFLARED_TUNNEL_TOKEN:-}" ]]; then
  echo "No tunnel token found."
  echo "Set CLOUDFLARED_TUNNEL_TOKEN, or install the service:"
  echo "  sudo cloudflared service install <token>"
  exit 1
fi

if ! curl -sf --max-time 1 http://127.0.0.1:1337/ >/dev/null; then
  if "$WAIT_FOR_FLASK"; then
    echo "Waiting for Flask on :1337..."
    until curl -sf --max-time 1 http://127.0.0.1:1337/ >/dev/null; do
      sleep 1
    done
  else
    echo "Flask is not running on :1337. Start it first (or pass --wait)."
    exit 1
  fi
fi

# Avoid duplicate connectors fighting over the same tunnel.
existing="$(pgrep -f 'cloudflared tunnel run' || true)"
if [[ -n "$existing" ]]; then
  echo "cloudflared already running (PIDs: $(echo "$existing" | tr '\n' ' '))"
  echo "Leaving existing process in place."
  # Keep the pane alive so tmux layout stays stable.
  while pgrep -f 'cloudflared tunnel run' >/dev/null; do
    sleep 30
  done
  echo "Existing cloudflared exited — starting a new one."
fi

echo "Tunnel: https://mbp.korukondacoachingcentre.com → http://127.0.0.1:1337"
exec cloudflared tunnel run --token "$CLOUDFLARED_TUNNEL_TOKEN"
