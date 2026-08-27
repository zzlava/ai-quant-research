# Layer-two Cash-Occupancy Attribution（E10e-1）

研究 / 只读**纵向归因** only。在已封印的 E10e-0 入场执行诊断序列上，把可观测的保留 / 未用目标现金归到 `CONFIRMED_CASH_OCCUPANCY_CAUSES` 冻结集合。**不**宣称解决 tranche 协议 cash-occupancy blocker，**不**修改 allocator / 执行语义，**不**声称账户级利用率或完整 sleeve 现金归因。

## 明确不做

- 不接 ScoringEngine / StrategyConfig / BacktestEngine / API / CLI / DB / broker
- 不改冻结协议 JSON，不宣称 blocker 已解除（`protocol_cash_occupancy_blocker_not_resolved=true`）
- 不把 caller 自报的 ready 布尔当作磁盘绑定证明
- `diagnostic_only=true`；`does_not_modify_allocator_or_execution=true`
- `ready_for_scoring/backtest/portfolio_construction/orders/trading/auto_apply` 全部 `false`

## 模块

| 项 | 路径 |
| --- | --- |
| 引擎 | `src/app/research/layer_two_cash_occupancy_attribution.py` |
| 测试 | `tests/test_layer_two_cash_occupancy_attribution.py` |

## 输入契约

结构行输入（`LayerTwoCashOccupancyStructuralRowInput`）携带一份 E10e-0 报告，以及调用 E10e-0 **结构** verifier 所需的全部精确上游：allocator、constraint、已封印 current state、exact ranking、phase report、execution observation。

文件行绑定（`LayerTwoCashOccupancyFileRowBindings` / `LayerTwoCashOccupancyFileRowInput`）另带 eligibility、financial reports、cluster report、`MarketStore`、`repo_root`、`phase_report_path`，以便对**每一行**调用真实 E10e-0 **文件** verifier。

序列约束：

- 非空；`as_of` 严格递增且唯一
- `entry_execution_report_id` 唯一
- 全体共享同一 `market_data_snapshot_id`、`phase_report_id`、bound tranche evaluation protocol id
- 完整重算每一行（非仅自哈希）

## 原因集合（冻结顺序）

与 `CONFIRMED_CASH_OCCUPANCY_CAUSES` 完全一致：

1. `candidate_shortage`
2. `gates`
3. `unaffordable_board_lot_or_min_commission`
4. `suspension`
5. `limit_up_or_limit_down`
6. `risk_budget`

`unknown` / `no_retained_cash` 单独计数，**永不**并入六因或把 unknown 金额压成 0。

Entry-only 诊断**观察不到跌停**；`limit_up_or_limit_down` 当前仅代表已观察到的买侧涨停分支（`blocked_limit_up`）。

## 逐行分类

| E10e-0 outcome | cause / marker | 金额 |
| --- | --- | --- |
| `unknown_execution_observation` | `unknown` | 全 null；`amount_quantified=false` |
| `blocked_suspension` | `suspension` | target 已知；used=0；retained=target |
| `blocked_limit_up` | `limit_up_or_limit_down` | 同上 |
| `unaffordable_board_lot_or_minimum_commission` | `unaffordable_board_lot_or_min_commission` | used=base scenario total；retained=target−used |
| `hypothetically_fillable` | residual>`tol` → `unaffordable_board_lot_or_min_commission`；否则 `no_retained_cash` | used=base total；retained=unused（近零时记 0）。**stress 仅诊断，不替换 base 归因** |
| `not_attempted` | 见下 | 全 null；永不编造 target |

`not_attempted` 保留 / 拒绝映射：

- `zero_risk_budget` / `insufficient_capital_for_minimum_base_slot` / `preexisting_sleeve_breach` → `risk_budget`
- `upstream_not_ready_for_stateful_allocator_input` / `no_active_tranche` / `no_selected_phase_opportunity` / `selected_tranche_occupied` / `preexisting_cluster_breach` → `gates`
- `no_admissible_candidate`：diagnostics 空或全 `already_held` → `candidate_shortage`；全为 `insufficient_cash` 或 `sleeve_notional_cap` → `risk_budget`；其他 / 混合 → `gates`
- 未知 retention / rejection 字面量按构造拒绝

## 聚合

- `cause_summaries`：**始终**含六因（即使决策数为 0）：decision / quantified / unquantified 计数，以及仅对 quantified 行累加的 known target / used / retained
- 全局：`total_report_count`、`total_attempt_count`、`total_not_attempt_count`、`total_unknown_count`、`total_no_retained_count`
- 恒等式：`global_sum_known_target_cash = known_used + known_retained`（仅 quantified）
- 明确标注：`does_not_claim_account_utilization`、`does_not_claim_full_sleeve_cash_attribution`

## Verifier

- **结构**：self-hash + 每行 E10e-0 结构 verifier + 本归因完整重算；`entry_execution_binding_ok` / `phase_binding_ok` / `tranche_evaluation_protocol_binding_ok` 均为 `false`
- **文件**：结构路径通过后，对每一行调用真实 E10e-0 文件 verifier；要求每行 `structural_ok`、`allocator_binding_ok`、`phase_binding_ok`、`tranche_evaluation_protocol_binding_ok`、**`execution_observation_binding_ok`** 均为 `true`，全部通过后才将外层 binding 置 `true`（不改归因语义）
- Canonical JSON SHA256 `report_id`；extra forbid；严格布尔 / 有限非负金额；`execution_outcome` 为 E10e-0 六值 Literal
- 报告模型独立校验：attempt+not_attempt=row_count；六因 decision + unknown + no_retained = row_count；各 cause summary 与全局 known 金额均从 `rows` 重算
