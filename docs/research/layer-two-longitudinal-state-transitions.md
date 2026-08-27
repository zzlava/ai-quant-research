# Layer-two Longitudinal Cash / Tranche State Transitions（E10f-3b）

> **醒目：本模块不是收益评估、不是净值/回测、不是 mark-to-market。**
> 它只把已封印且可 file-bind 的 **E10f-1 假设开仓**与 **E10f-2 固定 40 交易日退出诊断**按事件日串成可审计的**现金流 + tranche 占用**研究状态，并强制绑定一份显式封印的 **E10e-1 cash-occupancy attribution**（含全部 `not_attempted` 决策行）。
> **禁止**从 lifecycle entries 反推/合成 occupancy 报告（会漏掉未成交决策）。
> 不计算 return / PnL / equity curve / 基准 / 年化 / Sharpe；不生成订单或交易。
> 不重新解释已消费的 `p10_h20`；持有期语义完全由 E10f-2 file verifier 绑定的 40 日协议给出。

## 模块

| 项 | 路径 |
| --- | --- |
| 引擎 | `src/app/research/layer_two_longitudinal_state_transitions.py` |
| 测试 | `tests/test_layer_two_longitudinal_state_transitions.py` |
| Schema / engine | **`2` / `layer-two-longitudinal-state-transitions-v2`**（相对 E10f-3a v1 显式升版） |

## 冻结语义

1. **初始现金** = `80000`，经 allocation implementation protocol file verifier / 常量真实绑定；caller 不可改。
2. **E10e-1 绑定（强制）**：structural / file 输入必须携带一份封印的 `LayerTwoCashOccupancyAttributionReport` 及其**完整** row 输入。`diagnose` 调用真实 E10e-1 structural verifier（self-hash + 全量重算）；file verifier 对每一行调用真实 E10e-1 file verifier，并要求 `structural_ok` / `entry_execution_binding_ok` / `phase_binding_ok` / `tranche_evaluation_protocol_binding_ok` 全 true，且 `report_id` 精确匹配。报告封印 `cash_occupancy_attribution_report_id` 与完整有序 `cash_occupancy_input_entry_execution_report_ids`（含 `not_attempted`）。E10e-1 的 snapshot / `phase_report_id` / tranche protocol 必须等于纵向链。
3. **Entry**：仅在 E10f-1 structural（或六项 file ready）通过后处理；扣减 `entry_total_cash_used`；`entry_opened` 必须记录 `entry_execution_report_id`（exit 行必须为 null）。该 ID 必须是 E10e-1 输入 ID 的**唯一子集**；对应 E10e-1 行必须 `execution_outcome=hypothetically_fillable`，且 `known_base_cash_used` 在声明容差内等于 lifecycle `entry_total_cash_used`。映射到 `not_attempted` / blocked / unknown / 缺失行 → fail-closed。不要求每个 fillable E10e-1 行都开仓（子集是刻意的）。
4. **Entry `current_state` 绑定（开仓前）**：等于当日 start-of-day 纵向状态；首笔 `80000`+空仓；`current_market_notional` = 携带 entry `stock_notional`（**carried cost-notional**，非 mark）；equity = cash + carried notionals。
5. **Exit**：E10f-2 通过后处理；`hypothetically_exitable` 回款释放；`still_open` 不释放；`unknown_exit_observation` 必须为整条 run 最后一次 transition（含同日后续 exit 禁止）。
6. **同日**：最多一个 entry；entry 先于 exits；当日 exit 释放的现金/tranche 不得资助当日 entry。
7. **现金流恒等式**（仅命名 cash-flow identity）：
   `ending_cash = 80000 - cumulative_entry_total_cash_used + cumulative_base_exit_net_cash_received`
8. **declared_window**：`2022-01-01..2024-12-31`；禁止 2025+。除纵向 `event_date` 外，**E10e-1** 的 `coverage_as_of_start/end`、报告内每一行 `as_of`、以及完整 structural 输入中每条 `entry_execution_report.as_of` / `expected_t1_execution_date`（若有 observation 则含 `execution_date`）均须落在该窗口。额外 occupancy 决策可以落在纵向 transition start/end 之外（子集语义），但不得越过 2022–2024 边界。

## Verifier

| 路径 | 行为 |
| --- | --- |
| 结构 | self-hash + 完整重算；`lifecycle` / `exit` / `allocation` / **`cash_occupancy_attribution`** bindings 全 false；`ready_for_longitudinal_diagnostic=false` |
| 文件 | 真实 E10f-1 / E10f-2 / **E10e-1** / allocation protocol file verifier；嵌套 E10f-1、E10f-2 lifecycle/stamp-tax、**E10e-1 各行** `repo_root` 必须 resolve 到同一顶层 `file_input.repo_root`；四项 binding 全 true 才可 `ready_for_longitudinal_diagnostic=true`（禁止部分 binding） |

报告本体一切 `ready_for_*=false`；仅 file VerificationResult 可为 longitudinal-ready。
