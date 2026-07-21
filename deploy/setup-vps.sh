#!/usr/bin/env bash
# One-command VPS setup for the Cross-Asset Board.
#
# On a fresh Ubuntu 22.04/24.04 (or Debian 12) Tailscale node:
#
#   curl -fsSL https://raw.githubusercontent.com/MaybeNot2day/sector-tracker/main/deploy/setup-vps.sh | sudo bash
#
# Installs the app under /opt/sector-tracker with a dedicated system user,
# binds it to loopback, and publishes it only through Tailscale Serve HTTPS.
# A timer polls origin/main every 2 minutes and health-gates each deployment.
# Re-running the script is safe (idempotent).
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/MaybeNot2day/sector-tracker.git}"
APP_DIR="/opt/sector-tracker"
APP_USER="board"
UPDATE_SCRIPT="/usr/local/sbin/sector-tracker-update"
PORT="${PORT:-8787}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: curl -fsSL .../setup-vps.sh | sudo bash" >&2
  exit 1
fi

echo "==> Installing packages"
apt-get update -y -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  git curl ca-certificates python3 python3-venv

if ! command -v tailscale >/dev/null 2>&1; then
  echo "==> Installing Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  echo "Tailscale is not connected. Run 'sudo tailscale up', then re-run this script." >&2
  exit 1
fi

# The app needs Python >=3.11 (pyproject.toml); Ubuntu 22.04 ships 3.10 as
# python3. Fall back to python3.11 from universe, then deadsnakes if missing.
PYTHON=python3
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  if ! DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      python3.11 python3.11-venv; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -y -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      python3.11 python3.11-venv
  fi
  PYTHON=python3.11
fi

echo "==> Creating service user"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> Fetching the app"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone --quiet "$REPO_URL" "$APP_DIR"
else
  # Re-runs may pass a different REPO_URL; keep origin pointed at it. Run as
  # the owning user: root git in the board-owned worktree trips git's
  # dubious-ownership fatal and aborts the re-run under set -e.
  sudo -u "$APP_USER" git -C "$APP_DIR" remote set-url origin "$REPO_URL"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Installing dependencies"
sudo -u "$APP_USER" bash -ec "
  cd '$APP_DIR'
  # A previous run may have built the venv with a pre-3.11 interpreter.
  if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    rm -rf .venv
  fi
  [ -d .venv ] || $PYTHON -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet --require-hashes -r requirements.txt
"

# First setup only: ship .env with a random EDIT_TOKEN. The token still guards
# mutation endpoints if the tailnet URL is shared with another device/user.
if [ ! -f "$APP_DIR/.env" ]; then
  if command -v openssl >/dev/null 2>&1; then
    EDIT_TOKEN="$(openssl rand -hex 24)"
  else
    EDIT_TOKEN="$(od -vAn -N24 -tx1 /dev/urandom | tr -d ' \n')"
  fi
  sudo -u "$APP_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  sudo -u "$APP_USER" sed -i "s/^EDIT_TOKEN=.*/EDIT_TOKEN=$EDIT_TOKEN/" "$APP_DIR/.env"
  echo "==> Generated EDIT_TOKEN=$EDIT_TOKEN"
  echo "    Store it safely; it guards report edits on the board."
fi

# The timer runs as root so it can restart the app service. Never execute the
# app-user-owned repo copy directly: an app compromise could rewrite it.
install -o root -g root -m 0755 "$APP_DIR/deploy/update.sh" "$UPDATE_SCRIPT"

echo "==> Installing systemd units"
cat > /etc/systemd/system/sector-tracker.service <<EOF
[Unit]
Description=Cross-Asset Market Board
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=5
EnvironmentFile=-$APP_DIR/.env
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
NoNewPrivileges=true
PrivateDevices=true
PrivateTmp=true
ProtectControlGroups=true
ProtectHome=true
ProtectKernelModules=true
ProtectKernelTunables=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR/data $APP_DIR/config
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/sector-tracker-update.service <<EOF
[Unit]
Description=Cross-Asset Market Board auto-deploy (pull + restart on change)

[Service]
Type=oneshot
Environment=PORT=$PORT
ExecStart=$UPDATE_SCRIPT
EOF

cat > /etc/systemd/system/sector-tracker-update.timer <<EOF
[Unit]
Description=Poll GitHub for new Cross-Asset Board commits

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable sector-tracker.service
systemctl restart sector-tracker.service
systemctl enable --now sector-tracker-update.timer

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  echo "==> Closing direct port $PORT in ufw"
  ufw --force delete allow "$PORT/tcp" >/dev/null 2>&1 || true
fi

echo "==> Publishing private HTTPS endpoint through Tailscale Serve"
tailscale serve --bg "http://127.0.0.1:$PORT"
DNS_NAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
echo
echo "Done. Board:   https://$DNS_NAME"
echo "Logs:          journalctl -u sector-tracker -f"
echo "Auto-deploy:   pushes to main go live within ~2 minutes"
