#!/usr/bin/env bash
# Auto-deploy step, run by sector-tracker-update.timer as root. A revision is
# marked deployed only after its local health endpoint passes; failures roll back.
set -euo pipefail

APP_DIR="/opt/sector-tracker"
APP_USER="board"
PORT="${PORT:-8787}"
# Written only after pip + restart succeed; comparing against HEAD would wedge
# forever if a deploy died after `git reset` had already advanced HEAD.
MARKER="$APP_DIR/.deployed-rev"

run() { sudo -u "$APP_USER" "$@"; }

health_check() {
  for _ in {1..30}; do
    if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" |
      python3 -c 'import json,sys; raise SystemExit(json.load(sys.stdin).get("status") != "ok")'
    then
      return 0
    fi
    sleep 1
  done
  return 1
}

deploy_revision() {
  revision="$1"
  run git reset --hard --quiet "$revision" &&
    run "$APP_DIR/.venv/bin/pip" install --quiet --require-hashes -r requirements.txt &&
    systemctl restart sector-tracker.service
}

cd "$APP_DIR"
run git fetch --quiet origin main
DEPLOYED="$(cat "$MARKER" 2>/dev/null || true)"
REMOTE="$(run git rev-parse origin/main)"
[ "$DEPLOYED" = "$REMOTE" ] && exit 0

echo "Deploying $REMOTE (was ${DEPLOYED:-unknown})"
if ! deploy_revision "$REMOTE" || ! health_check; then
  echo "Deployment failed health check; rolling back to ${DEPLOYED:-unavailable}" >&2
  if [ -n "$DEPLOYED" ]; then
    deploy_revision "$DEPLOYED" || true
    health_check || echo "Rollback health check also failed" >&2
  fi
  exit 1
fi
printf '%s\n' "$REMOTE" | run tee "$MARKER" >/dev/null
