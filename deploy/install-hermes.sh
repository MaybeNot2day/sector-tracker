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

# User units require a persistent user manager after SSH disconnects.
ssh "$HOST" '
  set -eu
  python3 -c "import sys; sys.exit(sys.version_info < (3, 11))" ||
    { echo "Hermes automation requires Python >=3.11" >&2; exit 1; }
  test -x "$HOME/.local/bin/hermes" ||
    { echo "Install Hermes for this SSH user first" >&2; exit 1; }
  test -f "$HOME/.config/sector-tracker/uploader.env" ||
    { echo "Configure ~/.config/sector-tracker/uploader.env first" >&2; exit 1; }
  chmod 0600 "$HOME/.config/sector-tracker/uploader.env"
  if [ "$(loginctl show-user "$(id -un)" -p Linger --value)" != yes ]; then
    sudo -n loginctl enable-linger "$(id -un)" ||
      { echo "Enable lingering for this SSH user with sudo loginctl enable-linger" >&2; exit 1; }
  fi
  systemctl --user show-environment >/dev/null
'

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
# One grammar source serves both the server and the standalone uploader.
scp -q "$REPO/app/fringe_grammar.py" "$HOST":.local/bin/
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
local_sum="$(shasum -a 256 "$REPO/app/fringe_grammar.py" | cut -d' ' -f1)"
remote_sum="$(ssh "$HOST" "sha256sum .local/bin/fringe_grammar.py" | cut -d' ' -f1)"
[ "$local_sum" = "$remote_sum" ] || { echo "checksum mismatch: fringe_grammar.py" >&2; exit 1; }

# Use the configured vault, not an assumed username or home directory.
ssh "$HOST" 'python3 -' <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".local/bin"))
from vault_report_uploader import load_config

vault = Path(load_config().get("VAULT_DIR") or Path.home() / "hermes-research").expanduser()
if not vault.is_absolute() or not vault.is_dir():
    raise SystemExit("VAULT_DIR must name an existing absolute directory")
if any(character in str(vault) for character in "\n\r"):
    raise SystemExit("VAULT_DIR contains a newline")
directory = Path.home() / ".config/systemd/user/sector-tracker-uploader.path.d"
directory.mkdir(parents=True, exist_ok=True)
(directory / "vault.conf").write_text(
    "[Path]\nPathModified=\nPathModified=" + str(vault).replace("%", "%%") + "\n",
    encoding="utf-8",
)
PY

echo "==> Enabling triggers"
ssh "$HOST" "systemctl --user daemon-reload && systemctl --user enable --now ${TRIGGERS[*]} && systemctl --user restart ${TRIGGERS[*]}"

echo "==> Installed. Active sector-tracker timers on $HOST:"
ssh "$HOST" 'systemctl --user list-timers "sector-tracker-*" --no-pager'
