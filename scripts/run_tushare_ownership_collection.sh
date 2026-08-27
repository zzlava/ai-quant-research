#!/usr/bin/env bash
# Resume the development-period ownership proxy collection without putting the
# Tushare credential in shell history, process arguments, or repository files.
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
exec caffeinate -i .venv/bin/python -m app.cli collect-tushare-all-a-share-ownership \
  --start 2022-01-04 \
  --end 2024-12-31 \
  --strategy all_a_share_balanced_multifactor_v1 \
  --market-dir ./data/all-a-share-historical-v1/parquet \
  --fundamental-dir ./data/all-a-share-historical-v1/fundamentals-value-quality-v1 \
  --staging-dir ./data/raw/all-a-share-ownership-2022-2024-v2
