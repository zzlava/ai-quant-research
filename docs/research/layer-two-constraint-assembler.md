# Layer-two Constraint Assembler（E10d-2）

只读约束装配器：把 E10a / E10b / E10c / E10d-0 / E10d-1 的已验证报告装配成封印的 constraint rows，供**后续状态型 allocator**使用。

## 明确不做

- 不按排名选股
- 不构造状态型组合
- 不计算订单、不交易
- 不接 CLI
- 不修改既有协议 / 评分 / 股票池 / 组合 / 订单 / 交易路径
- `diagnostic_only=true`；`ready_for_scoring/backtest/portfolio_construction/orders/trading/auto_apply` 全部为 `false`
- `ready_for_stateful_allocator_input=true` **不等于**可组合 / 可交易

## 模块

- `src/app/research/layer_two_constraint_assembler.py`
- 测试：`tests/test_layer_two_constraint_assembler.py`

## 上游绑定

装配与 verifier **必须**调用真实上游 verifier，不得只信 report ready 字段：

| 上游 | 模块 | 用途 |
|------|------|------|
| E10a | `layer_two_candidate_eligibility` | eligible 有序集合 + size_multiplier + planned_buy |
| E10b | `layer_two_financial_negative_list` | 每名 eligible 恰一份财务裁决 |
| E10c | `layer_two_statistical_risk_clusters` | 统计风险簇（**非行业**）；需 sealed `MarketStore` 完整重算 |
| E10d-0 | `layer_two_allocation_protocol` | `plan_base_slots` / `plan_final_target_notional` / cluster cap |
| E10d-1 | `layer_two_tranche_phase_schedule` | 分档相位机会与 base_slot 封印 |

绑定的 allocation protocol id：`0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2`。

## 绑定规则（fail closed）

1. **时点**：eligibility / cluster / 全部 financial 的 `as_of`、`decision_at` 完全一致；phase 窗口包含 `as_of`；明确输出 selected schedule 在 `as_of` 是否有 opportunity。
2. **行情 snapshot**：`eligibility.data_snapshot_id == cluster.data_snapshot_id == phase.market_data_snapshot_id == store.snapshot_id`；财务 snapshot 独立按 symbol 封印，不得伪称等于 market snapshot。
3. **集合**：从 E10a 提取 `eligible_for_new_entry=true` 的精确有序集合；E10c `candidates` 必须完全相等；每个 eligible 恰一份 E10b；不合格 / unknown 不得进 rows。
4. **资金**：`phase.base_slot == plan_base_slots(equity, risk_budget)`；每个 eligible 的 `planned_buy_notional_cny` 必须等于 pre-multiplier `base_slot_notional`；`N=0` → 空 rows + 明确 cash reason。
5. **目标**：按 size × financial 调用 `plan_final_target_notional`（允许 &lt;8000，不抬高、不重分配）；hard / unknown → target null、retain cash；硬排除优先级不可改写。
6. **簇**：仅当 E10c `ready_for_cluster_constraints=true` 且无 unresolved、symbol 恰映射一次时，rows 才可标为可用于后续 allocator；否则整份 fail closed。
7. **簇 cap**：`0.35 * sleeve`，`max_positions=2`；若单名 target &gt; cap → **不静默裁剪**，`cluster_single_name_admissible=false`，`target_for_later_allocator=null`。不在本模块聚合选股。

## Verifier

- **结构 verifier**（`verify_layer_two_constraint_assembler_report`）：self-hash + 上游真实 verifier + 完整重算装配；此时各 `*_binding_ok` 保持 `false`。
- **文件 verifier**（`verify_layer_two_constraint_assembler_report_file`）：
  - **必填** `phase_report_path`：phase 报告必须落盘；调用 `verify_layer_two_tranche_phase_schedule_report_file`，且落盘报告的 `report_id` 与 canonical payload 必须与传入的 `phase_report` 完全一致，才置 `phase_binding_ok=true`。路径缺失、错误文件、payload 不一致或文件篡改均失败。
  - `eligibility` / `financial` / `cluster` 的 `*_binding_ok`：表示对应内存上游 verifier 完整重算及其磁盘合同绑定（via `repo_root`）成功，**不是**这些报告各自另有一份落盘文件绑定。
  - `allocation_protocol_binding_ok`：仓库内 allocation protocol JSON 磁盘绑定。
- 外层 reseal 篡改上游 ID、集合、snapshot、时点、multiplier、cluster 映射、target、cash reason、phase opportunity、ready 标志 → 拒绝
