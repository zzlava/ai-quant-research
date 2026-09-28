#!/usr/bin/env bash
# No-root install for a Muse VM (or any Linux user account with cron). Safe to rerun.
#   bash deploy/muse/install.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

if command -v uv >/dev/null 2>&1; then
  uv venv --allow-existing --python 3.12 .venv
  uv pip install --python .venv/bin/python -e .
else
  PY="$(command -v python3.12 || true)"
  if [[ -z "$PY" ]]; then
    echo "python3.12 not found; install it or uv (curl -LsSf https://astral.sh/uv/install.sh | sh)" >&2
    exit 1
  fi
  [[ -x .venv/bin/python ]] || "$PY" -m venv .venv
  .venv/bin/pip install -q -e .
fi

mkdir -p data/manual/logs "$HOME/.config/ai-quant"
if [[ ! -f "$HOME/.config/ai-quant/env" ]]; then
  install -m 600 deploy/muse/env.example "$HOME/.config/ai-quant/env"
fi
chmod +x deploy/muse/run.sh

BLOCK="$(.venv/bin/ai-quant etf muse-crontab --repo-dir "$REPO")"
# Read the current crontab fully before writing, and keep every line outside our managed block.
EXISTING="$(crontab -l 2>/dev/null || true)"
OTHERS="$(printf '%s\n' "$EXISTING" | sed '/^# BEGIN ai-quant-etf/,/^# END ai-quant-etf/d')"
printf '%s\n%s\n' "$OTHERS" "$BLOCK" | sed '/./,$!d' | crontab -
echo "installed cron jobs:"
crontab -l | sed -n '/^# BEGIN ai-quant-etf/,/^# END ai-quant-etf/p'

if [[ ! -f data/manual/etf-multi-asset-v1/ledger.jsonl ]]; then
  echo
  echo "next: create the ledger, e.g.  .venv/bin/ai-quant etf init --cash 80000"
fi
