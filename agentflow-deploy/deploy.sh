#!/usr/bin/env bash
# AgentFlow one-shot deploy script for Linux.
#
# Run after extracting the deploy tarball:
#   tar xzf agentflow-deploy-*.tar.gz -C /opt
#   sudo bash /opt/agentflow-deploy/deploy.sh
#
# What this does:
#   1. Move bundle to canonical install path /opt/agentflow/   (matches .service)
#   2. Create system user `agentflow`                          (idempotent)
#   3. Create venv + pip install -e backend/                    (idempotent)
#   4. Bootstrap .env from .env.template if missing             (chmod 600)
#   5. Install systemd unit + daemon-reload + enable + start
#
# Re-running this script on top of an existing install is safe — it skips
# steps that already succeeded.

set -euo pipefail

PREFIX="${PREFIX:-/opt/agentflow}"
USER_NAME="${AGENTFLOW_USER:-agentflow}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVICE_NAME="agentflow-review"

# 0. Where am I running from? Bundle layout looks like:
#       /opt/agentflow-deploy/{backend,config-examples,*.service,deploy.sh}
#    We move everything except deploy.sh into PREFIX/.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "must run as root (sudo bash $0)"
[ -d "$SCRIPT_DIR/backend" ] || fail "expected backend/ next to deploy.sh; bundle layout is wrong"

# 1. Create system user (idempotent)
if id "$USER_NAME" >/dev/null 2>&1; then
  log "user $USER_NAME already exists"
else
  log "creating system user $USER_NAME"
  useradd --system --create-home --shell /usr/sbin/nologin "$USER_NAME"
fi

# 2. Move payload into PREFIX (refuse to clobber unless --force)
if [ -d "$PREFIX" ] && [ "${1:-}" != "--force" ]; then
  log "$PREFIX already exists — skipping move (use --force to overwrite payload)"
else
  log "installing payload to $PREFIX"
  mkdir -p "$PREFIX"
  cp -a "$SCRIPT_DIR"/* "$PREFIX/"
  rm -f "$PREFIX/deploy.sh"   # don't ship the installer inside the install
  chown -R "$USER_NAME:$USER_NAME" "$PREFIX"
fi

# 3. venv + install
VENV="$PREFIX/backend/.venv"
if [ -x "$VENV/bin/af" ]; then
  log "venv already populated at $VENV — running pip install -e to pick up changes"
else
  log "creating venv at $VENV"
  sudo -u "$USER_NAME" "$PYTHON_BIN" -m venv "$VENV"
fi
log "pip install -U pip + agentflow"
sudo -u "$USER_NAME" "$VENV/bin/pip" install -q -U pip
sudo -u "$USER_NAME" "$VENV/bin/pip" install -q -e "$PREFIX/backend"

# 4. .env bootstrap (idempotent: never overwrites an existing .env)
ENV_FILE="$PREFIX/backend/.env"
if [ -f "$ENV_FILE" ]; then
  log ".env already present — leaving it alone"
else
  if [ -f "$PREFIX/backend/.env.template" ]; then
    log "bootstrapping .env from backend/.env.template"
    cp "$PREFIX/backend/.env.template" "$ENV_FILE"
  elif [ -f "$PREFIX/.env.template" ]; then
    log "bootstrapping .env from top-level .env.template"
    cp "$PREFIX/.env.template" "$ENV_FILE"
  else
    warn "no .env.template found — skipping .env bootstrap; daemon will fail until you create one"
  fi
fi
if [ -f "$ENV_FILE" ]; then
  chown "$USER_NAME:$USER_NAME" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  log "$ENV_FILE chmod 600 ✓"
fi

# 5. systemd unit
UNIT_SRC="$PREFIX/agentflow-review.service"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
[ -f "$UNIT_SRC" ] || fail "missing $UNIT_SRC; bundle is broken"
log "installing systemd unit at $UNIT_DST"
cp "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
log "starting $SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl --no-pager --lines=20 status "$SERVICE_NAME" || true

cat <<EOF

────────────────────────────────────────────────────────────────────
✓ AgentFlow deploy complete

Install path:    $PREFIX
Service name:    $SERVICE_NAME
Service status:  systemctl status $SERVICE_NAME
Logs:            journalctl -u $SERVICE_NAME -f
Edit env:        sudoedit $ENV_FILE   (then: systemctl restart $SERVICE_NAME)

Next:
  1. sudoedit $ENV_FILE  ← fill TELEGRAM_BOT_TOKEN, TELEGRAM_REVIEW_CHAT_ID,
     MOONSHOT_API_KEY, JINA_API_KEY, ATLASCLOUD_API_KEY (publisher keys optional)
     Make sure MOCK_LLM=false and AGENTFLOW_MOCK_PUBLISHERS=false (template default)
  2. systemctl restart $SERVICE_NAME
  3. From a logged-in shell on this VM:  sudo -u $USER_NAME $VENV/bin/af doctor
────────────────────────────────────────────────────────────────────
EOF
