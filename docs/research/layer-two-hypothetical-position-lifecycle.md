# Layer-two Hypothetical Position Lifecycle Record（E10f-1）

> **醒目：本模块只产出 E10f-2（固定 20 市场日退出诊断）的输入记录。**
> 它不是订单、不是真实成交、不是下一组合状态，也不冒充 / 修改 E10d-3 `LayerTwoStatefulPortfolioState`。
> 不接评分 / IC / 回测 / API / CLI / DB / 交易。

研究 / 只读假设开放持仓：把一份经过**完整 E10e-0 验证**且 `outcome=hypothetically_fillable` 的 **base_5bps** 情景，提升为一条独立、封印的 `hypothetical_open` 生命周期记录。

## 模块

| 项 | 路径 |
| --- | --- |
| 引擎 | `src/app/research/layer_two_hypothetical_position_lifecycle.py` |
| 测试 | `tests/test_layer_two_hypothetical_position_lifecycle.py` |

## 严格语义

1. **上游**：必须提供 E10e-0 完整输入（allocator / ranking / current_state / constraint / phase / execution observation），并通过真实 E10e-0 **结构** verifier 完整重算。文件路径复用真实 E10e-0 **文件** verifier；要求其返回的 allocator / phase / protocol / **`execution_observation_binding_ok`** 全为 true（`entry_execution_binding_ok=true` 明确包含该 observation 磁盘绑定），且 `report_id` 与 `record.entry_execution_report_id` 一致后，才**新构造**本模块 VerificationResult 的 binding / `ready_for_lifecycle_diagnostic=true`（不信任调用者布尔；记录本体 ready 恒为 false）。
2. **仅接受** `entry_report.outcome == hypothetically_fillable`；`base_scenario` 必须存在、`can_afford_one_lot=true`、`affordable_shares>0` 且为 100 整手、`total_cash_used<=target_notional`、假设买价 `<=published_up_limit`。其他 outcome → `ValueError`，不生成 null / 零股持仓。
3. **金额只从 base scenario 精确继承并重算恒等式**（`entry_cost_basis_total=entry_total_cash_used`，`per_share=total/shares`，`unused_target_cash=target-total`，容差 `1e-9`）。**不使用 stress scenario**；不得接受调用者另行提供的派生金额；不捏造估值 / PnL。模型**不设** exit / mark / PnL 字段。
4. **持仓日计数**：`holding_market_bars_elapsed=1`，且 `entry_market_day_counts_as_holding_bar_one=true`（成交市场日算第 1 日，供 E10f-2 冻结）。
5. **字面量**：`research_only` / `hypothetical_not_fill` / `diagnostic_only` / `post_decision_label_only`；记录本体 `ready_for_lifecycle_diagnostic=false`（结构产物，不得声称 ready）；`ready_for_scoring/backtest/portfolio_construction/orders/trading/auto_apply` 全 false。**仅**文件 verifier 返回的 `VerificationResult.ready_for_lifecycle_diagnostic` 可为 true。

## Verifier

| 路径 | 行为 |
| --- | --- |
| 结构 | record self-hash + 真实 E10e-0 structural verify + 完整重算 open record；四个 binding 全 false，`ready_for_lifecycle_diagnostic=false` |
| 文件 | 结构路径 + 真实 E10e-0 file verifier（含 phase 落盘、tranche protocol、**observation MarketStore 逐行绑定**，并核对 `e10e0.report_id == record.entry_execution_report_id`）→ **新构造** 四 binding 全 true + `ready_for_lifecycle_diagnostic=true` |

`VerificationResult` 状态机只允许两种成功形：结构形（ok + 无 binding + ready false）与文件形（ok + 四 binding + ready true）。部分 binding、ready 无 binding、全 binding 但 ready false、`structural_ok=false` 却带 binding/ready → 拒绝。

## 明确不做

- 不创建真实订单、成交、下一组合状态或收益
- 不修改既有 E10d / E10e 模块或 tranche evaluation protocol JSON
- 不宣称 stamp-tax / cash-occupancy / 任何 tranche blocker 已解除
