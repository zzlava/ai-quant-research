# Layer-two Stateful Single-Opportunity Allocator（E10d-3）

研究 / 只读诊断 only。在已验证的 `LayerTwoConstraintAssemblerReport`（E10d-2）之上，结合**显式**未验证开发排名与**显式封印**的当前组合状态，对当日 selected phase tranche 产出至多一条**诊断入场意图**。

## 明确不做

- 不推导评分 / 权重，不自行排名
- 不裁剪 target、不重分配释放资本、不填其他 tranche
- 不生成订单、不成交、不假设 fill / PnL
- 不接 ScoringEngine / StrategyConfig / BacktestEngine / API / CLI / DB / 交易
- `diagnostic_only=true`；`ready_for_scoring/backtest/portfolio_construction/orders/trading/auto_apply` 全部为 `false`
- `ready_for_allocation_diagnostic=true`（固定字面量）**不等于**生产可组合 / 可交易

## 模块

| 项 | 路径 |
| --- | --- |
| 引擎 | `src/app/research/layer_two_stateful_allocator.py` |
| 测试 | `tests/test_layer_two_stateful_allocator.py` |
| 本文档 | `docs/research/layer-two-stateful-allocator.md` |

## 上游

| 上游 | 用途 |
| --- | --- |
| E10d-2 constraint assembler | 已封印 constraint rows / eligible 集合 / phase opportunity / ready 标志 / `sleeve_budget` |
| E10d-0 allocation protocol | 绑定 protocol id；`cluster_notional_cap(sleeve_budget)`（35% sleeve）、`max_positions=2` |
| E10d-1 phase schedule | 经 E10d-2 绑定的 `phase_report_id`；file verifier 必填落盘 phase |

绑定的 allocation protocol id：`0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2`。

## 输入

1. **已封印 constraint report**（self-hash；调用方应先跑 E10d-2 verifier）
2. **`UnvalidatedDevelopmentRankingInput`**：`ranking_label=unvalidated_development_ranking_input`；`ranked_symbols` 必须是 `eligible_symbols` 的**精确无重复置换**
3. **`LayerTwoStatefulPortfolioState`**（调用方**必须事先 seal**，`state_id` 不得为 `None`；allocate **不**自动封印）：
   - 同一 `as_of` / `decision_at` / market snapshot / equity
   - `cash` + 持仓名义 = equity（绝对容差 `STATE_EQUITY_ABS_TOL=1e-6`）
   - 唯一 `symbol` / `tranche_id`；`positions` 必须按 `tranche_id` **严格递增**排序（否则拒绝，避免重排产生歧义 `state_id`）
   - `tranche_id ∈ [0, active_tranche_count)`；持仓数 ≤ active tranche 数
   - 每仓带显式 `cluster_id`；不需要账户身份

既有持仓符号可以不在当前 eligible 集合中；其 `cluster_id` 是状态证据，**不得静默替换**。若同一符号同时出现在 constraint row 且 row.cluster_id 与状态不一致 → **失败关闭**。

## 组合级留现金（不扫描或扫描后全拒）

稳定原因（互斥于 `proposed_entry`）：

| 原因 | 条件 |
| --- | --- |
| `upstream_not_ready_for_stateful_allocator_input` | upstream ready 标志为 false |
| `zero_risk_budget` / `insufficient_capital_for_minimum_base_slot` / `no_active_tranche` | base slots / active tranche = 0 |
| `no_selected_phase_opportunity` | as_of 无 selected opportunity |
| `preexisting_cluster_breach` | **任一**当前簇（含 eligible 外持仓）count &gt; 2，或簇名义合计 &gt; `cluster_notional_cap(sleeve_budget)`；阻断**全部**新开，不发明强制平仓 |
| `preexisting_sleeve_breach` | 当前 gross notional 已 &gt; `constraint_report.sleeve_budget`；阻断全部新开 |
| `selected_tranche_occupied` | selected tranche 已被占用（不借用其他相位 / tranche） |
| `no_admissible_candidate` | 扫描排名后无一可入选 |

不放松约束，不借用其他相位 / tranche。账户现金不得用来绕过 sleeve 风险预算。

## 单机会扫描

在组合门通过后，按排名顺序扫描：

1. 已持有 → `already_held`
2. hard / unknown / 不可用 row / null target → 对应拒绝原因
3. `target > current cash` → `insufficient_cash`（**不缩放**）
4. `current_gross + target > sleeve_budget` → `sleeve_notional_cap`（继续排名；**不裁剪**）
5. 同簇当前持仓数 / 名义（含 eligible 外持仓）相对**全局** `cluster_notional_cap(sleeve_budget)` 与最多 2 名：已满 2 名 → `cluster_position_cap`；加入后超簇 cap → `cluster_notional_cap`
6. 取**第一个**可入选候选；要求 `current_gross <= sleeve_budget` 且 `proposed_gross <= sleeve_budget`

`proposed_entry` 仅为诊断意图：`tranche_id` / `symbol` / `target_notional` / `cluster_id` / `ranking_position`。含 before / proposed-after 的 cash 与 gross notional 会计。

## Verifier

- **结构 verifier**：allocator self-hash + 完整重算分配；**不**声称 E10d-2 磁盘绑定（`*_binding_ok=false`）
- **文件 verifier**：先跑结构路径，再调用真实 E10d-2 **file** verifier（**必填** `phase_report_path`）；成功后置 `constraint_assembler_binding_ok` / `phase_binding_ok` / `allocation_protocol_binding_ok`
- 输出绑定：`constraint_assembler_report_id`、`current_state_id`、`phase_report_id`、allocation protocol id

## 歧义与失败关闭

若未封印状态、状态会计、时点 / snapshot / equity 漂移、排名非精确置换、tranche 越界 / 重复 / 非严格递增、selected_phase_opportunity.tranche_id 越界、constraint rows 符号重复、簇映射冲突：直接 `ValueError`。固定布尔字段（含 state / ranking / report）拒绝 `1` / 非布尔强制转换。
