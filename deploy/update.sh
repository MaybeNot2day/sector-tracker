#!/usr/bin/env bash
# Auto-deploy step, run by sector-tracker-update.timer as root:
# pull origin/main and restart the service only when new commits landed.
set -euo pipefail

APP_DIR="/opt/sector-tracker"
APP_USER="board"

run() { sudo -u "$APP_USER" "$@"; }

cd "$APP_DIR"
run git fetch --quiet origin main
LOCAL="$(run git rev-parse HEAD)"
REMOTE="$(run git rev-parse origin/main)"
[ "$LOCAL" = "$REMOTE" ] && exit 0

echo "Deploying $REMOTE (was $LOCAL)"
run git reset --hard --quiet origin/main
run "$APP_DIR/.venv/bin/pip" install --quiet -r requirements.txt
systemctl restart sector-tracker.service
