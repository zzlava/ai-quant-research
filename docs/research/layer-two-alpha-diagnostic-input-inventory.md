# E11b-1a: Layer-Two Alpha Diagnostic Input Inventory

## Purpose

Content-addressed read-only manifest that binds verified market and fundamental
data inputs to the frozen E11b-0b diagnostic run contract. This is a separate
manifest — it does not edit or reseal the run contract itself.

## Contract binding

The inventory is bound to exactly one run contract:

- **Contract ID**: `f892b76c2140009e3b6dcad6599def52aaa1f0b62acc91bed747136d92e09df0`
- **Contract file SHA-256**: `91385e6faccd7f7e05fb8792fb1e1c333c478cc5e2006f47153bded7993c7237`
- **Contract path**: `config/research/layer-two-alpha-diagnostic-run-contract-v1.json`

The inventory verifies the contract via its existing file verifier before
proceeding.

## Slot kinds

The inventory tracks exactly six input slot kinds (matching the run contract's
`future_input_slots`):

| Slot | State | Reason |
|------|-------|--------|
| `sealed_market_snapshot` | bound | Verified via `load_verified_snapshot` |
| `candidate_eligibility_reports` | blocked_missing | Derived reports do not exist; strict verifier required |
| `financial_negative_list_reports` | blocked_missing | Raw balance-sheet warning fields unavailable; unknown ≠ clean |
| `pit_fundamental_overlay` | bound | Verified via `load_verified_fundamental_snapshot` |
| `pit_daily_valuation` | bound | Verified via `load_verified_fundamental_snapshot` |
| `statistical_cluster_companion_reports` | blocked_missing | Derived reports do not exist; strict verifier required |

## Readiness

Because three derived slots remain `blocked_missing`, the inventory is
`ready_for_data=false`. All scoring/backtest/portfolio/order/trading/auto_apply
flags are literal `false`. Both `research_only` and `read_only` are literal
`true`.

## Verification guarantees

1. Market snapshot verified via full content-hash recomputation
2. Fundamental snapshot verified via full content-hash recomputation
3. Fundamental `base_market_snapshot_id` must exactly equal market `snapshot_id`
4. Coverage must contain 2022-01-01 through 2024-12-31
5. All paths must be repo-relative, non-symlink, and inside the repo root
6. Any path under `2025/` or `oos/` namespaces is rejected
7. Derived slot binding requires a strict verifier (forbidden in this milestone)
8. SHA-256 is computed from actual bytes, not trusted from JSON alone

## Rejection rules

- Extra/missing/duplicate slot kinds → validation error
- Mutable readiness after construction → frozen model error
- Mismatched SHA/snapshot/base-snapshot/table-hash → ValueError
- Coverage drift (missing required date range) → ValueError
- Symlinks or path escape → ValueError
- Bool-as-int for any flag → validation error
- Missing files → ValueError
- Binding a derived slot without strict verifier → forbidden (blocked_missing only)

## CLI usage

```
python -m app.cli inventory-layer-two-alpha-diagnostic-inputs \
    --market-dir data/all-a-share-historical-v1/parquet \
    --fundamental-dir data/all-a-share-historical-v1/fundamentals-value-quality-v1 \
    --output data/all-a-share-historical-v1/research/layer-two-alpha-diagnostic-input-inventory-v1.json
```

Add `--replace-existing` to overwrite an existing inventory file.

## Output artifact

The inventory is written to:
```
data/all-a-share-historical-v1/research/layer-two-alpha-diagnostic-input-inventory-v1.json
```

This is a research artifact, not authorization. It records what was verified at
build time and what remains missing.

## Design decisions

- The inventory is a separate manifest from the run contract to preserve the
  contract's sealed state.
- Derived slots (candidate eligibility, financial negative list, statistical
  clusters) cannot be weakly bound. They must remain `blocked_missing` until a
  strict verifier exists for each.
- The financial negative list issue explicitly states that raw balance-sheet
  fields are unavailable and that unknown cannot be treated as clean.
- The inventory uses the same canonical JSON hashing pattern as the run contract.
