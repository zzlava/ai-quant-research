# 第二层 Alpha 诊断数学内核（E11b-0a）

Pure deterministic math kernels for the sealed **E11a** alpha development evidence protocol.
This milestone is **not** a real-data engine: it does not assemble PIT inputs, bind market /
eligibility / financial / snapshot / cluster files, seal content-addressed reports, run data,
score, backtest, or trade.

| 项 | 值 |
| --- | --- |
| Engine version | `layer-two-alpha-diagnostic-engine-v0a` |
| 模块 | `src/app/research/layer_two_alpha_diagnostic_engine.py` |
| 测试 | `tests/test_layer_two_alpha_diagnostic_engine.py` |
| Bound E11a path | `config/research/layer-two-alpha-development-protocol-v1.json` |
| Bound E11a id | `fa91f0e260beb59a7f639dd3650a3842c817e470e9c3614abf2583dd691d2f86` |

Bound E11a path/id are audit constants only; this module **never reads disk**.

## Next milestone（尚未实现）

- **Report sealing** — content-addressed diagnostic report / self-hash / verification state machine
- **PIT input assembler** — eligibility / financial / market snapshot / cluster assignment file binding

Until those land, kernels remain synthetic-input only. Do not treat any caller boolean as proof of PIT readiness.

## Protocol-locked entries（协议锁定入口）

The following entries are **sealed by E11a protocol** — their constituent keys, directions,
thresholds, and minimum-known counts are not caller-configurable. Any deviation raises
`ValueError` at call time or model construction time.

| 入口 | 锁定内容 |
| --- | --- |
| `quality_family_composite` | 恰好五个 key: `roe` high, `roic` high, `grossprofit_margin` high, `debt_to_assets` low, `ocf_to_or` high; min known = 3 |
| `value_family_composite` | 恰好三个 key: `pe_ttm`, `pb`, `ps_ttm`; 全部 `positive_only_inverted`; min known = 2 |
| `holm_step_down_four_factors` | family-wise alpha 固定 0.05; `HolmStepDownResult.alpha` 也强制 == 0.05 |
| `size_band` | 负市值 → `ValueError`（不返回 `below_lowest`） |
| `QUALITY_SEALED_RULES` | `MappingProxyType` 只读映射，运行时不可原地修改（item assignment/deletion → `TypeError`；`clear`/`pop`/`update` → `AttributeError`） |
| `HolmStepDownResult` / 所有 `_StrictModel` 子类 | `frozen=True`，构造后不可赋值；`HolmStepDownResult.results` 为不可变 `tuple`，不可 `append` |

## Symbol validation（证券代码校验）

All public cross-sectional entries (`average_rank_percentiles`, `multi_component_family_composite`,
`quality_family_composite`, `value_family_composite`, `within_cluster_percentiles`) and
models (`SymbolCloseObservation`, `FamilyCompositeEntry`) enforce the project-standard A-share
symbol format: **exactly 6 digits followed by `.SH` or `.SZ`**. Blank, padded, `.BJ`, lowercase
suffixes, and free-text symbols are rejected with `ValueError`.

## Kernels

1. **`average_rank_percentiles`** — ties averaged; `(rank-1)/(n-1)*100`; `n=1` unknown; bool/NaN/Inf rejected; optional low/value inversion via `100-p`.
2. **`quality_family_composite`** — **protocol-locked** five-component composite (see table above). Per-component CS percentiles (high/low), min 3 known, mean of known percentiles as **raw composite**, then final CS rerank as **final percentile**.
3. **`value_family_composite`** — **protocol-locked** three-metric composite (see table above). All `positive_only_inverted`; min 2 known.
4. **`medium_momentum_12_1` / `defensive_low_vol`** — exact expected date list equal to observations (243 / 61 bars). Momentum uses fixed indices `t-242` (0) and `t-21` (221). Low-vol: 60 simple returns, sample stdev `ddof=1`, `*sqrt(242)`, negative sign.
5. **`exact_forward_return`** — same-symbol `t` and exact expected `t+h` endpoint; verified positive finite closes; horizon never shifts.
6. **`paired_spearman` / `quintile_top_minus_bottom_spread`** — average-rank ties; quintile bucket `min(floor((rank-1)/n*5),4)`; ties never split; all-equal / empty extremes → unknown.
7. **`coverage_gate`** — `known >= 500` and `known/eligible >= 0.60` with positive denominator (499 / 0.599 fail; 500 / 0.60 pass).
8. **`newey_west_bartlett_inference`** — ordered finite series; gamma divisor `n`; Bartlett weights; `var(mean)=LRV/n`; `n<=L` or nonpositive/nonfinite variance → statistic/p `None`; positive p=`1-Phi`, negative p=`Phi`.
9. **`holm_step_down_four_factors`** — **protocol-locked** alpha=0.05 (see table above); exactly four frozen IDs; effective p = raw or 1; sort `(p, frozen order)`; threshold `alpha/(4-i+1)`; stop after first failure.
10. **`size_band`** — `[3e9,5e9)`, `[5e9,1e10)`, `[1e10,+inf)`; below / unknown outside; negative cap raises `ValueError`; no bool/nonfinite.
11. **`within_cluster_percentiles`** — consumes prevalidated `symbol → cluster_id | None`; singleton / unassigned → unknown; rerank within non-singleton clusters only. **Does not create clusters or claim PIT.**

## Market window defect rule

- **Raise** on structural/type defects: bool, nonfinite close, duplicate dates, length/order/extra/missing vs the exact expected calendar list.
- **Return `None` (unknown)** when the exact typed window is present but any bar is unverified or non-positive.
- Gaps are **never** skip-compressed.

## Non-goals

No scoring / backtest / strategy / pipeline / broker imports. No ledger update. No 2024/2025 evaluation. No strategy YAML generation.
