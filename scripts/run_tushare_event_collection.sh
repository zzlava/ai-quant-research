#!/usr/bin/env bash
# Run the resumable Tushare event collector without storing its token in this
# repository, a shell profile, or command history. The caller must first add
# the token to the login keychain under service aiq-tushare-token.
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
keychain_service="aiq-tushare-token"
keychain_account="$(id -un)"

if ! command -v security >/dev/null 2>&1; then
  printf 'macOS security command is unavailable; cannot read Tushare token from Keychain.\n' >&2
  exit 1
fi

if ! AIQ_TUSHARE_TOKEN="$(security find-generic-password \
  -a "$keychain_account" \
  -s "$keychain_service" \
  -w)"; then
  printf 'Missing Keychain item service=%s account=%s. Add it before running this script.\n' \
    "$keychain_service" "$keychain_account" >&2
  exit 1
fi
if [[ -z "$AIQ_TUSHARE_TOKEN" ]]; then
  printf 'Keychain returned an empty Tushare token.\n' >&2
  exit 1
fi
export AIQ_TUSHARE_TOKEN
trap 'unset AIQ_TUSHARE_TOKEN' EXIT

cd "$project_root"
exec .venv/bin/python -m app.cli collect-tushare-all-a-share-events \
  --start 2022-01-01 \
  --end 2024-12-31 \
  --market-dir ./data/all-a-share-historical-v1/parquet \
  --staging-dir ./data/raw/a-share-events-2022-2024-v1 \
  --source-version a-share-events-2022-2024-v1
