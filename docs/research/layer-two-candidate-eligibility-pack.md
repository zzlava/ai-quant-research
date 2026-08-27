# Layer-Two Candidate Eligibility Pack (E11b-1b)

## Purpose

Compact, strictly verifiable Parquet pack of E10a candidate eligibility verdicts
for every market trading decision date in **2022-01-01..2024-12-31**.

This is **research input only**:

- `ready_for_scoring = false`
- `ready_for_trading = false`
- **Not alpha evidence** and **not authorization** for trading
- Statistical clusters deliberately wait until financial verdicts define the
  final denominator

## Pack Structure

```
<output_dir>/
├── eligibility_verdicts.parquet   # One row per (symbol, as_of)
└── manifest.json                  # Sealed cryptographic manifest
```

### Parquet Schema

| Column | Type | Description |
|--------|------|-------------|
| symbol | string | Six-digit .SH/.SZ canonical symbol |
| as_of | string (ISO date) | Decision date |
| decision_at | string (ISO datetime) | 17:30 Asia/Shanghai on as_of |
| eligible_for_new_entry | bool | E10a final verdict |
| unknown_critical_input | bool | Any input missing/unknown |
| market_scope_pass | bool? | SSE/SZSE ordinary-A pass |
| tradability_pass | bool? | Not suspended on decision date |
| listing_history_pass | bool? | >= 180 listed market trading days |
| st_delist_pass | bool? | Not ST/PT risk |
| liquidity_structure_pass | bool? | 20 obs available |
| liquidity_tradable_count_pass | bool? | >= 15 tradable in lookback |
| liquidity_median_pass | bool? | Median amount >= 50M CNY |
| liquidity_capacity_pass | bool? | Planned buy <= 0.1% of avg amount |
| size_cap_pass | bool? | Free-float cap >= 3bn CNY |
| median_daily_amount_cny | float? | 20-day median amount |
| average_daily_amount_cny | float? | 20-day average amount |
| tradable_days_in_lookback | int? | Tradable day count in lookback |
| pit_free_float_market_cap_cny | float? | PIT circ_mv * 10000 in CNY |
| size_multiplier | float? | 0.5 / 0.75 / 1.0 from size band |
| adjusted_planned_notional_cny | float? | 8000 * size_multiplier |
| reason_codes | string | Comma-separated E10a reason codes |
| source_input_hash | string | Deterministic provenance hash |

## Domain Semantics

### Candidate Domain

Derived from point-in-time `stock_basic`: all ordinary SSE/SZSE common-A
symbols with `list_date <= as_of` and `delist_date` absent or `> as_of`.
Does **NOT** require a same-day bar for inclusion — if a domain name has no
same-day bar, the row is emitted with critical status/liquidity fields unknown
(fail-closed). Does **NOT** reuse the legacy derived-liquid universe, current
membership, or `list_status`.

Duplicate `ts_code` entries in `stock_basic` are rejected at load time (no
silent first-wins or last-wins behavior).

### Ordinary-A Eligibility

Based on historically collected `stock_basic` domain rules:
- Exact six-digit .SH/.SZ format
- No SH 900 B-shares, no SZ 200 B-shares
- No BSE exchange
- Does **not** use future `list_status` or future `delist_date` to exclude an
  earlier decision
- On/after effective `delist_date`, the symbol exits the new-entry domain

### PIT Decision Timing

- `decision_at` is exactly **17:30 Asia/Shanghai** on each `as_of`
- All `available_at` must be `<= decision_at`

### Security Status

- Prefers sealed `daily_bars` `is_st` field for same-day ST status
- `is_suspended` from same-day sealed bar when present
- If no same-day bar exists, those fields remain unknown (fail-closed)
- **`security_status_available_at` = 15:00 Asia/Shanghai (market close)**, NOT
  `decision_at`. Bars are considered available after exchange close, well before
  the 17:30 decision cutoff.

### Listed Market Trading Days

Exact count of open SSE `trade_cal` days from `list_date` through `as_of`,
using bisect on the precomputed sorted calendar (O(log n) per symbol, not
O(n) calendar scan).

### Liquidity Observations

- Exact last 20 SSE open days ending on `as_of` (inclusive), never gap-compressed
- Availability: **15:00 Asia/Shanghai** on observation_date (market close),
  documented deterministic availability definition no later than decision
- Sealed `amount` in CNY from daily bars
- Known full-day suspension: `amount_cny = 0`
- Missing bar/field stays unknown (explicit unknown slot); never fills a
  non-suspension gap
- Invalid/nonfinite/negative amount values: treated as unknown (never becomes
  a false known value)

### PIT Free-Float Market Cap

- Uses **exact same-day valuation only** (`val_date == as_of`)
- Raw `available_at` treated as naive UTC, converted to timezone-aware UTC,
  required `available_at <= decision_at` (17:30 Asia/Shanghai)
- **`pit_free_float_market_cap_available_at` = actual stored `available_at`**
  from the valuation row (converted to timezone-aware UTC), NOT `decision_at`.
  This actual value enters `CandidateInput` and `source_input_hash`.
- Tushare `circ_mv` is in 10,000 CNY units: convert exactly `circ_mv * 10000` to CNY
- Missing, late (`available_at > decision_at`), nonpositive, nonfinite, invalid, or
  duplicate/conflicting same-day rows stay unknown (fail-closed)
- Future rows (`date > as_of`) are never used
- Never relabels a stale prior-day row as current day

### Source Input Hash

Canonical JSON SHA-256 of the **complete** `LayerTwoCandidateInput` model dump
plus exact source bindings (`market_snapshot_id`, `raw_request_id`,
`valuation_source_row_hash`, `inventory_id`, `fundamental_snapshot_id`,
`valuation_file_sha256`, `two_layer_contract_id`, `allocation_protocol_id`).
Deterministic: any gate input or binding change produces a different hash.
Valuation `source_row_hash` must be a valid 64-char lowercase hex string to be
accepted; otherwise valuation is treated as unknown.

### E10a Evaluator

Invokes `evaluate_layer_two_candidate` with the bound policy. The pack
materializer exactly matches the sealed E10a evaluator logic.

## Bound Sources

| Source | Binding |
|--------|---------|
| E11b-1a Inventory | `data/all-a-share-historical-v1/research/layer-two-alpha-diagnostic-input-inventory-v1.json` |
| Inventory ID | `e11a2108dac3b5735a85d8dfdf529a72179c8e681033c1aa2648b688fb1a05c3` |
| Raw Collection | `data/raw/all-a-share-history-20211008-20241231-v1` |
| Two-Layer Contract ID | `27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6` |
| Allocation Protocol ID | `0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2` |
| Planned Buy Notional | 8000 CNY (pre-multiplier base slot minimum) |

## Module Provenance

- `pack_module_sha256`: SHA-256 of the pack module `.py` file at materialization
  time; verifier requires the current module SHA to match
- `e10a_module_sha256`: SHA-256 of the E10a evaluator module; bound to
  `d6c29ee4da8eed4515c7444974afe944065c7b17e04d79f38b6bf57c30b4b4e0`

## CLI Commands

```bash
# Materialize (writes new pack; refuses if output_dir exists)
python -m app.cli materialize-layer-two-candidate-eligibility-pack \
  --output-dir data/all-a-share-historical-v1/research/candidate-eligibility-pack-v1

# Verify (read-only, full recomputation)
python -m app.cli verify-layer-two-candidate-eligibility-pack \
  --pack-dir data/all-a-share-historical-v1/research/candidate-eligibility-pack-v1
```

## Verification

The strict full-recomputation verifier uses **streaming output comparison** with
**memory-resident source indexes**:

1. Validates manifest self-hash (SHA-256 of canonical JSON excluding `pack_id`)
2. Verifies pack module provenance (current module SHA must match manifest);
   enforces `BOUND_E10A_MODULE_SHA256` and `BOUND_INVENTORY_ID` via pure checks
3. Calls `verify_inventory` for full disk revalidation of market/fundamental/valuation
4. Recomputes **all** raw collection dataset hashes from staged parquet files and
   requires **full dict equality** (`recomputed == manifest_dataset_hashes`),
   THEN also enforces the three bound constants
5. Verifies Parquet file SHA-256 matches manifest
6. Computes **streaming canonical table hash** via `ParquetFile.iter_batches()`
   — never loads the full output table into memory
7. Constructs the exact expected `PackSourceBinding` from verified inventory
   slots and frozen constants, then requires **model equality** with the stored
   manifest binding
8. **Sequential row-by-row recomputation verification** via production helper
   `_verify_expected_rows_streaming`: iterates stored parquet rows via
   `ParquetFile.iter_batches()` alongside a deterministic expected-row iterator
   (sorted by `as_of` then `symbol`). Requires **exact Arrow schema equality**
   (field order, names, types). Compares **every column** of each stored row
   against the recomputed expected row. Each stored row is a bounded per-row dict
   created during iteration (not a global stored-row dict). No `pl.from_arrow`,
   no `pq.read_table`, no DataFrame.
9. Detects extra, missing, duplicate, or reordered keys via sequential
   comparison (exhaustion check on both sides)
10. Tracks per-year row counts **during iteration** (not via DataFrame filter)
11. Validates trading date set hash and count

A re-sealed tampered manifest+Parquet will fail against sources. Missing source
paths fail immediately (never skip).

## Scale and Atomicity

- **Source file ingestion** streams required columns only via
  `ParquetFile.iter_batches(columns=[...])` — no full Polars/Arrow table load
  during source reading.
- **Compact source indexes** (`daily_bars` as `dict[str, list[BarTuple]]`,
  `daily_valuation` as `dict[str, list[ValTuple]]`, `stock_basic` as flat dict)
  remain **memory-resident** for O(1)/O(log n) lookup per candidate. This is
  appropriate for ~5000 symbols × 3 years but means the verifier does not
  operate entirely in streaming mode.
- **Output Parquet** is written and verified in streaming mode:
  - Builder streams rows by decision-date batch via `ParquetWriter`
  - Canonical table hash computed via `ParquetFile.iter_batches()` (no full table load)
  - Verifier reads stored rows sequentially via `iter_batches()` using the extracted
    production helper `_verify_expected_rows_streaming`
- `writer.close()` wrapped in `try/finally` to ensure close even on exceptions
- Valuation resolution uses binary search (`_bisect_find_val`) directly on the
  sorted `ValTuple` list — no per-call date-list allocation
- Bar lookup via `_bisect_find_bar`; duplicate bars detected by adjacent check
  after sorting (no global `seen_sym_date` set)
- Canonical table hash uses named column access (`batch.column(col_name)`),
  batch-boundary independent
- Builds in a sibling temporary directory and atomically renames on success
- Cleans temp directory on failure (no parquet-without-manifest residue)
- Refuses if output directory already exists
- Shared row-generation function (`_generate_row_for_candidate`) used by both
  builder and verifier ensures exact parity

## Security Properties

- Path escape, symlink traversal, and OOS (2025+) namespace are rejected
- Source hash drift (any changed source file) fails verification
- Dataset hashes recomputed from actual staged parquet bytes **and compared as
  full dict** (not just three keys)
- Full source binding equality check against expected binding (not partial field checks)
- Readiness flags are Literal-typed and cannot be mutated to True
- Duplicate `ts_code` in stock_basic rejected at load time
- Duplicate/conflicting same-day valuation rows → unknown (not silently use first)
- Invalid/nonfinite/negative amounts → unknown (never false known value)
- Allocation protocol path must exist and be verified (missing = failure)
