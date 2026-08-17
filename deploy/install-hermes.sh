#!/usr/bin/env bash
# Idempotent installer for the hermes-box side of the pipeline.
#
# The repo is the source of truth for the scripts and systemd user units
# that run on the Hermes box (uploader, delivery watchdog, auto-stop
# monitor, nightly board backup, Fringe risk stats + weekly review). This syncs them, reloads systemd,
# enables every trigger, and verifies checksums so "the box matches git"
# is a command, not a hope.
#
#   deploy/install-hermes.sh [host]     # default host: hermes-ts
set -euo pipefail

HOST="${1:-hermes-ts}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

SCRIPTS=(
  vault_report_uploader.py
  report_pipeline_watchdog.py
  fringe_stop_monitor.py
  board_backup.py
  fringe_stats_notepad.py
  fringe_weekly_review.py
)
UNITS=(
  sector-tracker-uploader.service
  sector-tracker-uploader.path
  sector-tracker-uploader.timer
  sector-tracker-report-watchdog.service
  sector-tracker-report-watchdog.timer
  sector-tracker-stops.service
  sector-tracker-stops.timer
  sector-tracker-backup.service
  sector-tracker-backup.timer
  sector-tracker-fringe-stats.service
  sector-tracker-fringe-stats.timer
  sector-tracker-fringe-review.service
  sector-tracker-fringe-review.timer
)
TRIGGERS=(
  sector-tracker-uploader.path
  sector-tracker-uploader.timer
  sector-tracker-report-watchdog.timer
  sector-tracker-stops.timer
  sector-tracker-backup.timer
  sector-tracker-fringe-stats.timer
  sector-tracker-fringe-review.timer
)

echo "==> Syncing ${#SCRIPTS[@]} scripts and ${#UNITS[@]} units to $HOST"
ssh "$HOST" 'mkdir -p .local/bin .config/systemd/user'
scp -q "${SCRIPTS[@]/#/$REPO/scripts/}" "$HOST":.local/bin/
scp -q "${UNITS[@]/#/$REPO/deploy/}" "$HOST":.config/systemd/user/
ssh "$HOST" "cd .local/bin && chmod +x ${SCRIPTS[*]}"

echo "==> Verifying checksums"
for script in "${SCRIPTS[@]}"; do
  local_sum="$(shasum -a 256 "$REPO/scripts/$script" | cut -d' ' -f1)"
  remote_sum="$(ssh "$HOST" "sha256sum .local/bin/$script" | cut -d' ' -f1)"
  if [ "$local_sum" != "$remote_sum" ]; then
    echo "checksum mismatch: $script" >&2
    exit 1
  fi
done

echo "==> Enabling triggers"
ssh "$HOST" "systemctl --user daemon-reload && systemctl --user enable --now ${TRIGGERS[*]}"

echo "==> Installed. Active sector-tracker timers on $HOST:"
ssh "$HOST" 'systemctl --user list-timers "sector-tracker-*" --no-pager'
