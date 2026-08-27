# E11b-0b: Layer-Two Alpha Diagnostic Run Contract

## Purpose

This contract is a **registration and sealing artifact**, not a result.

Before any real PIT data is assembled, it content-addresses and freezes:

1. The hypothesis family (exactly 4 pooled h40 daily IC one-sided hypotheses)
2. The evidence windows (development, seen-robustness, consumed-OOS, new-frozen-OOS)
3. The diagnostic engine kernel version
4. The future PIT input slots (all unbound at this milestone)

The contract binds immutably to the upstream E11a alpha development protocol,
the diagnostic engine module, and the research trial ledger — all verified by
file SHA-256 and self-hash/ID checks.

## What This Is Not

- Not factor evidence. No IC, spread, Holm results, or coverage metrics are produced.
- Not a modification of the research trial ledger. The base ledger is read-only.
- Not wired to scoring, backtest, portfolio construction, orders, or trading.
- Not accessible from the CLI (intentionally excluded to prevent accidental execution).

## Binding Summary

| Upstream | Path | ID Verified | File SHA-256 Verified |
|----------|------|-------------|----------------------|
| E11a Protocol | `config/research/layer-two-alpha-development-protocol-v1.json` | `fa91f0e2...` | `88e58619...` |
| Diagnostic Engine | `src/app/research/layer_two_alpha_diagnostic_engine.py` | version `v0a` | `4680affb...` |
| Research Trial Ledger | `config/research/research-trial-ledger-v1.json` | `1fc94425...` | `40e12754...` |

All binding fields are checked against BOUND module constants at both
model construction time (Pydantic model_validator) and structural verification
(assert_binding_constants). Any drift — even after resealing contract_id —
fails at model construction.

## Hypothesis Family

Exactly four pooled h40 daily IC single-sided hypotheses, in frozen order:

| # | Hypothesis ID | Factor Family | H0 | H1 | HAC Lag | Holm Member |
|---|---------------|---------------|----|----|---------|-------------|
| 1 | h40-ic-quality | quality | mean ≤ 0 | mean > 0 | 39 | Yes |
| 2 | h40-ic-value | value | mean ≤ 0 | mean > 0 | 39 | Yes |
| 3 | h40-ic-medium_momentum_12_1 | medium_momentum_12_1 | mean ≤ 0 | mean > 0 | 39 | Yes |
| 4 | h40-ic-defensive_low_vol | defensive_low_vol | mean ≤ 0 | mean > 0 | 39 | Yes |

- Family-wise Holm alpha = 0.05, exactly 4 hypotheses.
- Each hypothesis_id must be exactly `h40-ic-{factor_family_id}`.
- Spread positivity, yearly direction, and cluster companion are **gates**, not hypotheses.
  They may not enter the hypotheses tuple.
- All bool fields (is_holm_family_member, is_gate_only, gate flags) enforce strict bool;
  integer 0/1 is rejected.

## Evidence Windows

| Window | Start | End | Selectable | Report Only | Forbidden |
|--------|-------|-----|------------|-------------|-----------|
| Development | 2022-01-01 | 2023-12-31 | true | false | false |
| Seen Robustness | 2024-01-01 | 2024-12-31 | false | true | false |
| Consumed OOS | 2025-01-01 | 2026-08-21 | false | false | true |
| New Frozen OOS begins | 2026-08-22 | — | Cannot be evaluated in this contract |

Per-role semantics are enforced at both model construction and assert_windows_valid.

All windows share frozen `label_horizons = (5, 20, 40)`, each item validated as
non-bool int.

### Endpoint Rules (mechanized from E11a)

Three strict Literal[True] fields in EvidenceWindows:

| Rule | Value |
|------|-------|
| `label_endpoint_must_remain_within_same_window` | true |
| `horizon_never_shifts_or_shortens` | true |
| `missing_or_unverified_endpoint_is_unknown` | true |

These are checked at model construction (strict bool, integer 1 rejected),
structural verification, and assert_windows_valid.

## Future PIT Input Slots

All slots are **unbound** at this milestone. A slot with `state=unbound` must have
`repo_relative_path=null`, `sha256=null`, and `snapshot_id=null`. Any non-null value
in an unbound slot is a validation failure. `required` enforces strict bool (integer 1 rejected).

| Slot Kind | State |
|-----------|-------|
| sealed_market_snapshot | unbound |
| candidate_eligibility_reports | unbound |
| financial_negative_list_reports | unbound |
| pit_fundamental_overlay | unbound |
| pit_daily_valuation | unbound |
| statistical_cluster_companion_reports | unbound |

## Readiness

All operational readiness flags are **false** (strict bool, integer 0/1 rejected):

- `research_only = true`
- `does_not_run_data = true`
- `ready_for_data = false`
- `ready_for_scoring = false`
- `ready_for_backtest = false`
- `ready_for_portfolio_construction = false`
- `ready_for_orders = false`
- `ready_for_trading = false`
- `auto_apply = false`

## Engine Version Verification

The `_verify_engine_version_constant` function uses AST parsing to read the
top-level `LAYER_TWO_ALPHA_DIAGNOSTIC_ENGINE_VERSION` assignment in the engine
module and requires its value to exactly equal the expected version string.
Source-string-contains tricks cannot bypass this check.

## Contract ID

`contract_id = f892b76c2140009e3b6dcad6599def52aaa1f0b62acc91bed747136d92e09df0`

The `contract_id` is the SHA-256 of the canonical JSON serialization (sort_keys,
separators `(",",":")`) of the contract payload excluding the `contract_id` field
itself. When non-null, it must be a 64-character lowercase hex string.

## Next Step

The next milestone is the **PIT input assembler** which will bind real data files
to the currently-unbound input slots. The old research trial ledger is not modified
by this contract.
