# 仓位漏斗、执行拒单与整手资金利用诊断（E3a）

本诊断只读汇总回测结果，**不改变**策略配置、分数、门控、选股顺序、持有期、成本参数或成交规则。

## SignalAttribution 漏斗字段

向后兼容：新字段默认 `0`，旧 JSON 可解析。

| 字段 | 语义 |
| --- | --- |
| `scheduled_signal_days` | 实际计划信号日次数（即使 `signal_fn` 返回空也计） |
| `scoring_days` | 有非空 `ranked` 的信号日次数（语义不变） |
| `empty_ranking_days` | 计划信号日但排名为空 |
| `regime_blocked_days` / `rejected_by_regime_gate` | `allowed <= 0` 时按日 / 按名计数 |
| `capacity_blocked_days` / `rejected_by_capacity` | `allowed > 0` 且 `free_slots <= 0` 时按日 / 按名计数 |
| `rejected_already_held_or_pending` | 已持有或已在 pending |
| `rejected_not_in_membership` | 当日非宇宙成员 |
| `not_evaluated_after_order_limit` | 已凑满 `take` 后未再检查的排名名；**不是**排名阈值拒绝 |
| `entry_attempts` | 执行端真正检查的 pending entry 次数 |
| `rejected_insufficient_cash` | 现金不足以买至少一手（与停牌/涨停/目标整手不可负担互斥） |
| `rejected_unaffordable` | `require_target_lot_affordability` 下目标预算买不起一手 |
| `rejected_suspended` / `rejected_at_limit` | 停牌 / 涨停（仍可按既有 defer 语义延期） |

`allowed <= 0` 优先于容量归因；二者分开计数，不混用。

## 成功成交预算恒等

仅对**成功成交**累计（失败尝试不写入预算字段，也不当作 0 摊入利用率）：

- `target_entry_budget_total`：尝试时 `min(cash, equity / max_positions)`
- `actual_entry_cash_used_total`：`shares * fill_price + buy_commission`（`fill_price` 已含买入滑点）
- `unallocated_entry_budget_total`：每笔 `max(target - actual, 0)` 之和（永不为负）
- `overallocated_entry_budget_total`：每笔 `max(actual - target, 0)` 之和（永不为负）

当 `require_target_lot_affordability=false` 且目标预算买不起一手、但总现金买得起时，成交路径仍可用现金 fallback 买一手；此时 `actual` 可大于 `target`，记入 `overallocated`，**不**把 `unallocated` 写成负数。

恒等：`target_entry_budget_total + overallocated_entry_budget_total = actual_entry_cash_used_total + unallocated_entry_budget_total`（容差可核对）。

所有预算字段为有限非负；缺字段默认 `0` 兼容旧 JSON，显式负数 / NaN / Inf 在模型解析时拒绝。

不改 `shares` 或成交路径；卖出印花税不进入买入预算恒等。

## EquityPoint 日终仓位

新引擎写入：

- `open_positions`：日终开放仓位数（回测结束仍持仓的也计入当日）
- `pending_orders`：日终 pending 单数

二者为可空字段。历史 JSON 缺字段解析为 `null`，**不得**把缺省当成真实 0。

## PositionUtilizationSummary

`app.research.position_utilization.summarize_position_utilization(result, max_positions=...)`：

- 若任一 `equity_curve` 点缺少 `open_positions`：`available=false`，并给出 `unavailable_reason`；不伪造日终仓位统计。
- 可用时输出：`trading_days`、`zero_position_days`、`underfilled_days`（相对 `max_positions`）、平均/峰值开放仓位、平均/峰值投入比例、平均现金比例、漏斗计数、`fill_rate = orders_filled / entry_attempts`、`budget_utilization = actual / target`（可大于 1；仅基于成功成交目标预算）。

CLI `backtest` 可打印上述诊断（含 `overallocated_entry_budget_total`），不改变核心 metrics / trades。
