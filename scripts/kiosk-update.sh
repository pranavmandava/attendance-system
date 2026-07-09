#!/usr/bin/env bash
# Pull origin/main into the kiosk checkout and restart services when code changes.
# Intended to run as a systemd oneshot (axon-attendance-update.service).
set -euo pipefail

ROOT="${KIOSK_ROOT:-/home/vicharak/attendance-system}"
REMOTE_URL="${KIOSK_REMOTE_URL:-https://github.com/pranavmandava/attendance-system.git}"
BRANCH="${KIOSK_BRANCH:-main}"
LOG_TAG="axon-attendance-update"

log() {
  echo "[$LOG_TAG] $*"
}

cd "$ROOT"

# Prefer the canonical remote (old inspireface-trial URL still redirects).
current_url="$(git remote get-url origin 2>/dev/null || true)"
if [[ "$current_url" != "$REMOTE_URL" ]]; then
  log "Setting origin to $REMOTE_URL (was: ${current_url:-none})"
  git remote set-url origin "$REMOTE_URL"
fi

git fetch --prune origin "$BRANCH"

local_sha="$(git rev-parse HEAD)"
remote_sha="$(git rev-parse "origin/$BRANCH")"

if [[ "$local_sha" == "$remote_sha" ]]; then
  log "Already up to date at ${local_sha:0:7}"
  exit 0
fi

log "Updating ${local_sha:0:7} -> ${remote_sha:0:7}"

# Discard local tracked drift so the kiosk always tracks origin/main.
# data/, .env, and .venv stay put (.gitignore / untracked).
git checkout -B "$BRANCH" "origin/$BRANCH"
git reset --hard "origin/$BRANCH"
git clean -fd --exclude=.env --exclude=data --exclude=.venv

if command -v uv >/dev/null 2>&1; then
  log "Syncing dependencies with uv"
  uv sync --frozen || uv sync
else
  log "WARNING: uv not on PATH; skipping dependency sync"
fi

log "Restarting attendance services"
# Unit files are system-wide; vicharak has NOPASSWD sudo on axon.
sudo systemctl restart axon-attendance-api.service
# UI Requires= API; restart UI explicitly so the Qt process reloads code.
sudo systemctl restart axon-attendance-ui.service

log "Update complete at $(git rev-parse --short HEAD)"
