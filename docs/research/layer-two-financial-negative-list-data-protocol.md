# Layer-Two Financial Negative-List Data Protocol (E11b-2a)

Frozen PIT financial-negative-list data collection and evidence-availability protocol
for the E10b adjudicator.

**This protocol is not alpha evidence and not authorization.**

It does NOT modify the existing E10b evaluator or deploy rules. Any future
threshold/scope change requires a new protocol version and ledger entry.

## Protocol Identity

| Field | Value |
|-------|-------|
| protocol_id | `314e9d644b897ed4398cc349e3772b09bbe6f80cfd2d518a7cdbf19bb651d2ea` |
| schema_version | 1 |
| protocol_version | layer-two-financial-negative-list-data-protocol-v1 |
| status | frozen_for_development |
| config path | `config/research/layer-two-financial-negative-list-data-protocol-v1.json` |

## Bindings

| Binding | Value |
|---------|-------|
| E10b two-layer decision contract ID | `27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6` |
| Contract file SHA-256 | `0e1afbf963c5d5b11e6db86d8fb5f7ccec3c364eb304c2227e7d9ae9eda345f6` |
| E10b engine version | `layer-two-financial-negative-list-engine-v1` |
| E10b module SHA-256 | `5eba8c96392620bcae54f41dd532756ad528a3e5bbc939d134987e309f4fc15c` |
| Candidate pack path | `data/all-a-share-historical-v1/research/candidate-eligibility-pack-v1` |
| Candidate pack_id | `cd904a7974d019689d933bcb0c0e329e51e38f96a26e30cdea4c5b15aaa4d09e` |
| Candidate pack parquet SHA-256 | `6f6518828df99f7111b6632e0fad51335e6feaa8e06a65dc06457e062a65ffd4` |
| Candidate pack row_count | 3,597,408 |
| Candidate pack coverage | 2022-01-01 .. 2024-12-31 |
| Raw collection dir | `data/raw/all-a-share-history-20211008-20241231-v1` |
| Raw collection request_id | `0b1e4abf58af7c68e7e00e2ecddc7b205010e8a9f26c6c2bb9f7a81e0699f7d1` |
| Raw collection manifest SHA-256 | `2e79423dbcfd49dca8148960071495d45abcb36c439b97f226f29ddd6757bbfa` |
| Raw quality report SHA-256 | `8fe834efd812d685228ad8a74733270e9526ea8b1ade876f349cb29da4b00081` |

## Windows

- **Decision window**: 2022-01-01 .. 2024-12-31
- **Announcement collection window**: 2020-01-01 .. 2024-12-31

The 2020 start is required because consecutive YoY evidence (rule C: two periods
of same-quarter year-over-year) for early 2022 decisions needs FY2020 annual data
announced in early 2021 and FY2021 annual data announced in early 2022.

## Source Endpoints

### 1. balancesheet

- API: `balancesheet` | [doc](https://tushare.pro/document/2?doc_id=36)
- Fields: ts_code, ann_date, f_ann_date, end_date, report_type, comp_type,
  end_type, money_cap, notes_receiv, accounts_receiv, oth_receiv, inventories,
  goodwill, total_assets, st_borr, lt_borr, st_bonds_payable, non_cur_liab_due_1y,
  bond_payable, total_hldr_eqy_exc_min_int, update_flag

### 2. income

- API: `income` | [doc](https://tushare.pro/document/2?doc_id=33)
- Fields: ts_code, ann_date, f_ann_date, end_date, report_type, comp_type,
  end_type, revenue, total_revenue, update_flag

### 3. fina_indicator

- API: `fina_indicator` | [doc](https://tushare.pro/document/2?doc_id=79)
- Fields: ts_code, ann_date, end_date, interestdebt, update_flag

### 4. fina_audit

- API: `fina_audit` | [doc](https://tushare.pro/document/2?doc_id=80)
- Fields: ts_code, ann_date, end_date, audit_result, audit_fees, audit_agency,
  audit_sign

## Collection Semantics

- Partition per endpoint/symbol, resumable
- Raw response row hashes recorded
- Request/quality/collection manifests
- Complete empty partitions allowed only after successful audited response
- Truncation/duplicate/conflict fail closed
- `collected_at`/download time NEVER becomes historical `available_at`

## PIT Availability

- **Effective disclosure date** = max(valid ann_date, valid f_ann_date when present)
- **available_at** = 23:59:59 Asia/Shanghai on effective disclosure date
- Same-day disclosure is **unusable** at same-day 17:30 decision
- First usable at the **next** decision date
- Missing/invalid announcement date → row unusable unknown
- All evidence requires available_at ≤ decision_at
- Preserve every retrieved version and source_row_hash; never overwrite older rows
- report_type {1, 4, 5} consolidated cumulative only; {2,3,6-12} excluded
- update_flag is evidence metadata, never an availability timestamp
- Same semantic key/availability with conflicting required values → unknown
- Ambiguous restatement chronology → unknown

## Scope

- Four generic accounting-warning rules apply ONLY when comp_type=1 (general
  industrial) is consistently known across required balance/income rows
- comp_type 2 (bank), 3 (insurer), 4 (securities), missing/conflicting →
  generic rules unknown; E10b report remains `insufficient_evidence`
- fina_audit (rule A) applies to EVERY company regardless of comp_type
- Future financial-sector v2 is backlog

## Rule Math

All thresholds use strict `>` (not `>=`). Negative numerators are invalid unknown.
Currency/accounting values are CNY; ratios are unit-invariant.

### Rule A: non_standard_audit

Latest usable **annual** audit; max report-period age 550 calendar days.

- `audit_result` exactly `"标准无保留意见"` → false (clean)
- Any other nonblank result → true (hit)
- Missing/conflict → unknown

### Rule B: large_cash_and_interest_bearing_debt

Latest usable consolidated cumulative statement; max report-period age 240 days.

- total_assets > 0
- money_cap / total_assets > 0.25 AND interestdebt / total_assets > 0.25
- interestdebt from same-period latest usable fina_indicator
- Component debt sum (`st_borr + lt_borr + st_bonds_payable + non_cur_liab_due_1y +
  bond_payable`) is cross-check/fallback ONLY if all five are explicit numeric;
  partial components never zero-filled

### Rule C: receivables_inventory_growth_vs_revenue_two_periods

- Exposure = notes_receiv + accounts_receiv + inventories (all three explicit numeric)
- Revenue uses `revenue` ONLY — no silent `total_revenue` fallback
- Latest two consecutive standard quarter periods available at decision
- Each needs exact prior-year same-quarter values
- All prior denominators > 0
- gap = (exposure_current/exposure_prior - 1) - (revenue_current/revenue_prior - 1)
- true ONLY if **both** consecutive gaps > 0.20
- false when complete and not both true
- Missing/skipped/nonconsecutive/conflict → unknown

### Rule D: other_receivables_to_assets_over_5pct

- oth_receiv / total_assets > 0.05
- Denominator > 0; negative numerator → unknown
- Latest usable; missing → unknown

### Rule E: goodwill_to_net_assets_over_30pct

- goodwill / total_hldr_eqy_exc_min_int > 0.30
- Denominator > 0; nonpositive equity → unknown
- Negative goodwill → unknown
- Latest usable; missing → unknown

## Freshness

- Audit: max report-period age 550 calendar days
- Statement rules (B/C/D/E): max report-period age 240 calendar days
- Age = (decision_date - end_date).days
- Stale beyond max age → unknown

## Row/Version Resolution

- Latest usable by effective disclosure date wins
- Ties with same availability and same values: deduplicated
- Ties with same availability and conflicting values: unknown
- Chronological restatement: latest wins ONLY if unambiguous
- Ambiguous restatement sequence: unknown

## Issue Codes

| Code | Meaning |
|------|---------|
| FNLD-001 | missing_ann_date_row_unusable |
| FNLD-002 | same_day_disclosure_unusable_at_decision |
| FNLD-003 | report_type_excluded |
| FNLD-004 | comp_type_not_general_industrial |
| FNLD-005 | conflicting_values_same_key_unknown |
| FNLD-006 | ambiguous_restatement_chronology |
| FNLD-007 | stale_report_period_beyond_max_age |
| FNLD-008 | partial_debt_components_not_zero_filled |
| FNLD-009 | no_silent_total_revenue_fallback |
| FNLD-010 | negative_numerator_invalid |
| FNLD-011 | nonpositive_denominator_invalid |
| FNLD-012 | future_date_rejected |
| FNLD-013 | oos_2025_plus_rejected |

## Readiness

All flags false. This protocol freezes semantics only.

| Flag | Value |
|------|-------|
| research_only | true |
| ready_for_scoring | false |
| ready_for_backtest | false |
| ready_for_portfolio_construction | false |
| ready_for_trading | false |
| ready_for_data_collection | false |
| auto_apply | false |

Outcome-driven changes are forbidden. Any future change requires a new protocol
version and research trial ledger entry.
