#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
run_contract_path="config/research/financial-negative-list-collection-run-contract-v3.json"
fixed_staging_dir="data/raw/a-share-financial-negative-list-20200101-20241231-v3"
keychain_service="aiq-tushare-token"
keychain_account="$(id -un)"

if [[ -z "${AIQ_E11B_COLLECTION_AUTHORIZATION_FILE:-}" ]]; then
  printf 'Missing env AIQ_E11B_COLLECTION_AUTHORIZATION_FILE. Refusing before any Keychain access.\n' >&2
  exit 1
fi

cd "$project_root"
export PYTHONPATH="$project_root/src${PYTHONPATH:+:$PYTHONPATH}"
authorization_file="$AIQ_E11B_COLLECTION_AUTHORIZATION_FILE"
if [[ "$authorization_file" != /* ]]; then
  authorization_file="$project_root/$authorization_file"
fi
.venv/bin/python -m app.cli verify-financial-negative-list-collection-run-contract \
  --run-contract "$run_contract_path" \
  --require-authorized \
  --authorization-file "$authorization_file"

if ! command -v security >/dev/null 2>&1; then
  printf 'macOS security command is unavailable; cannot read Tushare credential from Keychain.\n' >&2
  exit 1
fi

if ! AIQ_TUSHARE_TOKEN="$(security find-generic-password -a "$keychain_account" -s "$keychain_service" -w)"; then
  printf 'Missing Keychain item service=%s account=%s.\n' "$keychain_service" "$keychain_account" >&2
  exit 1
fi
if [[ -z "$AIQ_TUSHARE_TOKEN" ]]; then
  printf 'Keychain returned an empty Tushare credential.\n' >&2
  exit 1
fi
export AIQ_TUSHARE_TOKEN
trap 'unset AIQ_TUSHARE_TOKEN' EXIT

exec caffeinate -i .venv/bin/python -m app.cli collect-tushare-financial-negative-list \
  --run-contract "$run_contract_path" \
  --authorization-file "$authorization_file" \
  --staging-dir "$fixed_staging_dir"
