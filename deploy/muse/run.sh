#!/usr/bin/env bash
# Cron entry point for a no-root deployment (e.g. Muse VM). Usage: run.sh repo|daily|weekly
# Notifications go to data/manual/etf-multi-asset-v1/outbox/pending for Muse to relay.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO" || exit 1
ENV_FILE="${AIQ_ENV_FILE:-$HOME/.config/ai-quant/env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a; . "$ENV_FILE"; set +a
fi
export AIQ_NOTIFY_CHANNEL="${AIQ_NOTIFY_CHANNEL:-outbox}"

job="${1:-}"
case "$job" in
  repo)   args=(repo-check --notify action) ;;
  daily)  args=(plan --fetch-sse --skip-stale-quotes --notify action) ;;
  weekly) args=(plan --fetch-sse --skip-stale-quotes --notify always) ;;
  *) echo "usage: $0 repo|daily|weekly" >&2; exit 2 ;;
esac

mkdir -p data/manual/logs
{
  echo "== $(date -Is) $job"
  "$REPO/.venv/bin/ai-quant" etf "${args[@]}"
  echo "exit=$?"
} >> "data/manual/logs/$job.log" 2>&1
