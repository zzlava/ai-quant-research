#!/usr/bin/env bash
# One-time VPS setup for the ETF reminder. Run as root from the repository root:
#   sudo bash deploy/vps/install.sh
# Expects the repository at /opt/ai-quant-research (Debian/Ubuntu with systemd).
set -euo pipefail

APP_DIR=/opt/ai-quant-research
ENV_DIR=/etc/ai-quant

if [[ "$(pwd)" != "$APP_DIR" ]]; then
  echo "run this from $APP_DIR (clone the repository there first)" >&2
  exit 1
fi

id aiq >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin aiq

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi

uv venv --allow-existing --python 3.12 "$APP_DIR/.venv"
uv pip install --python "$APP_DIR/.venv/bin/python" -e "$APP_DIR"

mkdir -p "$APP_DIR/data/manual"
chown -R aiq:aiq "$APP_DIR/data"

mkdir -p "$ENV_DIR"
if [[ ! -f "$ENV_DIR/telegram.env" ]]; then
  install -m 600 deploy/vps/telegram.env.example "$ENV_DIR/telegram.env"
  echo "edit $ENV_DIR/telegram.env with your bot token and chat id"
fi

install -m 644 deploy/vps/aiq-etf-daily.service deploy/vps/aiq-etf-daily.timer \
  deploy/vps/aiq-etf-weekly.service deploy/vps/aiq-etf-weekly.timer deploy/vps/aiq-etf-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aiq-etf-daily.timer aiq-etf-weekly.timer
if grep -q '^AIQ_TELEGRAM_CHAT_ID=[0-9-]' "$ENV_DIR/telegram.env" && [[ -f "$APP_DIR/data/manual/etf-multi-asset-v1/ledger.jsonl" ]]; then
  systemctl enable aiq-etf-bot.service
  systemctl restart aiq-etf-bot.service
else
  echo "bot not started yet: fill $ENV_DIR/telegram.env and create the ledger, then rerun this script"
fi
systemctl list-timers 'aiq-etf-*' --no-pager
