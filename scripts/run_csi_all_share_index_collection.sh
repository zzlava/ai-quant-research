#!/usr/bin/env bash
# Collect the sealed CSI All Share long-history sources without placing the
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
export PYTHONPATH="$project_root/src${PYTHONPATH:+:$PYTHONPATH}"
exec caffeinate -i .venv/bin/python -m app.cli collect-csi-all-share-long-history \
  --identity-contract ./config/research/csi-all-share-index-identity-v1.json \
  --staging-dir ./data/raw/csi-all-share-index-2005-2024-v1
