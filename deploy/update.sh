#!/usr/bin/env bash
# Auto-deploy step, run by sector-tracker-update.timer as root. A revision is
# marked deployed only after its local readiness endpoint passes; failures roll back.
set -euo pipefail
umask 077
# Manual updates and the timer must never overlap.
exec 9>"${UPDATE_LOCK:-/run/lock/sector-tracker-update.lock}"
flock -n 9 || exit 0

APP_DIR="${APP_DIR:-/opt/sector-tracker}"
APP_USER="${APP_USER:-board}"
PORT="${PORT:-8787}"
# Written only after pip + restart succeed; comparing against HEAD would wedge
# forever if a deploy died after `git reset` had already advanced HEAD.
MARKER="$APP_DIR/.deployed-rev"

run() { sudo -u "$APP_USER" "$@"; }

health_check() {
  local allow_legacy="${1:-false}" response status
  response="$(mktemp)" || return 1
  for _ in {1..120}; do
    if status="$(curl -sS --max-time 2 -o "$response" -w '%{http_code}' \
      "http://127.0.0.1:$PORT/api/ready")"; then
      if [ "$status" = 200 ] &&
        python3 -c 'import json,sys; raise SystemExit(json.load(open(sys.argv[1])).get("status") != "ok")' "$response"
      then
        rm -f "$response"
        return 0
      fi
      # A legacy rollback revision predates readiness. Only an absent endpoint
      # may use its old health gate; a present-but-unready endpoint never may.
      if [ "$allow_legacy" = true ] && [ "$status" = 404 ] &&
        curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" |
          python3 -c 'import json,sys; raise SystemExit(json.load(sys.stdin).get("status") != "ok")'
      then
        rm -f "$response"
        return 0
      fi
    fi
    sleep 1
  done
  rm -f "$response"
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
if [ -z "$DEPLOYED" ]; then
  # Legacy installs without a marker must prove their current revision ready
  # BEFORE resetting HEAD; otherwise there is no sound rollback target.
  health_check true || { echo "No healthy rollback revision; repair the running service first." >&2; exit 1; }
  DEPLOYED="$(run git rev-parse HEAD)"
  printf '%s\n' "$DEPLOYED" | run tee "$MARKER" >/dev/null
fi
run git cat-file -e "$DEPLOYED^{commit}"
# Preserve runtime edits before resetting the legacy tracked seed.
run mkdir -p "$APP_DIR/data"
if [ ! -e "$APP_DIR/data/watchlists.yaml" ] && [ -f "$APP_DIR/config/watchlists.yaml" ]; then
  run cp "$APP_DIR/config/watchlists.yaml" "$APP_DIR/data/watchlists.yaml"
fi
if [ -f "$APP_DIR/.env" ]; then
  chmod 0600 "$APP_DIR/.env"
  env_tmp="$(run mktemp "$APP_DIR/.env.XXXXXX")"
  run sed -E 's|^WATCHLIST_PATH=(\./)?config/watchlists\.yaml$|WATCHLIST_PATH=./data/watchlists.yaml|' "$APP_DIR/.env" |
    run tee "$env_tmp" >/dev/null
  run chmod 0600 "$env_tmp"
  run mv "$env_tmp" "$APP_DIR/.env"
fi
REMOTE="$(run git rev-parse origin/main)"
[ "$DEPLOYED" = "$REMOTE" ] && exit 0

echo "Deploying $REMOTE (was ${DEPLOYED:-unknown})"
if ! deploy_revision "$REMOTE" || ! health_check; then
  echo "Deployment failed readiness check; rolling back to $DEPLOYED" >&2
  if ! deploy_revision "$DEPLOYED" || ! health_check true; then
    echo "Rollback failed; inspect sector-tracker.service immediately." >&2
  fi
  exit 1
fi
printf '%s\n' "$REMOTE" | run tee "$MARKER" >/dev/null
