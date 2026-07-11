#!/usr/bin/env bash
# Auto-deploy step, run by sector-tracker-update.timer as root:
# pull origin/main and restart the service only when new commits landed.
set -euo pipefail

APP_DIR="/opt/sector-tracker"
APP_USER="board"
# Written only after pip + restart succeed; comparing against HEAD would wedge
# forever if a deploy died after `git reset` had already advanced HEAD.
MARKER="$APP_DIR/.deployed-rev"

run() { sudo -u "$APP_USER" "$@"; }

cd "$APP_DIR"
run git fetch --quiet origin main
DEPLOYED="$(cat "$MARKER" 2>/dev/null || true)"
REMOTE="$(run git rev-parse origin/main)"
[ "$DEPLOYED" = "$REMOTE" ] && exit 0

echo "Deploying $REMOTE (was ${DEPLOYED:-unknown})"
run git reset --hard --quiet origin/main
run "$APP_DIR/.venv/bin/pip" install --quiet -r requirements.txt
systemctl restart sector-tracker.service
printf '%s\n' "$REMOTE" | run tee "$MARKER" >/dev/null  # marker stays $APP_USER-owned
