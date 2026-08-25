#!/usr/bin/env bash
# Resume the OOS-3 event collection without placing its credential in shell
# history, process arguments, or repository files.
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
keychain_service="aiq-tushare-token"
keychain_account="$(id -un)"

if ! command -v security >/dev/null 2>&1; then
  printf 'macOS security command is unavailable; cannot read the Tushare credential from Keychain.\n' >&2
  exit 1
fi

if ! AIQ_TUSHARE_TOKEN="$(security find-generic-password \
  -a "$keychain_account" \
  -s "$keychain_service" \
  -w)"; then
  printf 'Missing Keychain item service=%s account=%s.\n' \
    "$keychain_service" "$keychain_account" >&2
  exit 1
fi
if [[ -z "$AIQ_TUSHARE_TOKEN" ]]; then
  printf 'Keychain returned an empty Tushare credential.\n' >&2
  exit 1
fi
export AIQ_TUSHARE_TOKEN
trap 'unset AIQ_TUSHARE_TOKEN' EXIT

cd "$project_root"
exec caffeinate -i .venv/bin/python -m app.cli collect-tushare-all-a-share-events \
  --start 2024-10-08 \
  --end 2026-08-21 \
  --market-dir ./data/all-a-share-oos-20241001-20260821-v1/parquet \
  --staging-dir ./data/raw/a-share-events-oos-20241008-20260821-v1 \
  --source-version a-share-events-oos-20241008-20260821-v1
