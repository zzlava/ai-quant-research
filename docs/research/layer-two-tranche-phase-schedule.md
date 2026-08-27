# 第二层 40 日分批相位计划器（E10d-1）

研究 / 只读诊断 only。在冻结市场交易日日历与 anchor 上，按确认的 40 日 holding/phase cycle 生成均匀错开的 **scheduled_opportunity**；**不选股、不算价、不生成订单、不成交**。

| 项 | 路径 |
| --- | --- |
| 引擎 | `src/app/research/layer_two_tranche_phase_schedule.py` → `plan_layer_two_tranche_phase_schedule` |
| 结构校验 | `verify_layer_two_tranche_phase_schedule_report`（self-hash + 全量重算；不声称 disk binding） |
| 磁盘校验 | `verify_layer_two_tranche_phase_schedule_report_file`（结构 + 上游 file verify） |
| 测试 | `tests/test_layer_two_tranche_phase_schedule.py` |

## 上游绑定

| 上游 | 路径 | protocol_id |
| --- | --- | --- |
| tranche 评价协议 | `config/research/tranche-evaluation-protocol-draft-v1.json` | `8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a` |
| allocation 实现解释协议 | `config/research/layer-two-allocation-implementation-protocol-v1.json` | `0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2` |

活动 tranche 数经 allocation 协议的 `plan_base_slots` 得出（可因资本不足低于档位 3/6/9；0 预算或不足 8000 → 0）。**40 是 holding/phase cycle，不是 tranche count。**

## 输入（全部显式，禁止默认经济参数）

| 参数 | 规则 |
| --- | --- |
| `market_calendar` | 完整、严格递增、无重复的市场交易日 |
| `anchor` / `start` / `end` | 必须在 calendar；`anchor <= start <= end` |
| `current_account_equity` / `risk_budget` | 拒绝 bool / NaN / Inf / 负值；0 equity 允许 |
| `market_data_snapshot_id` | 64 位小写 hex；测试可用非生产 snapshot |

## 相位几何

- 基准 offset：`floor(k * 40 / N)`，`k=0..N-1`（循环间隔差最多 1；唯一、排序、落在 `[0,39]`）
  - N=2 → `[0,20]`；N=3 → `[0,13,26]`；N=6 → `[0,6,13,20,26,33]`；N=9 → `[0,4,8,13,17,22,26,31,35]`
- 日期 phase：`(calendar_index - anchor_index) % 40`；匹配时仅生成 **scheduled_opportunity**
- 同一 tranche 下一机会严格相隔 **40** 个市场交易日
- 首次 / 预算上调 **渐进建立**；禁止 same-day catch-up
- `risk_reduce_not_phase_limited` 仅为语义字段，本模块不发减仓指令
- 一股一 tranche 语义保留；本模块**不选股票**

## 完整 40-member phase family

同一冻结 calendar+anchor 上生成 `family_shift=0..39`：将基准 offsets 循环平移，并**完整封印**各 shift 的机会日期列表（不只 count）。`selected operational schedule` 固定 `family_shift=0`；其余 39 仅供后续全相位评估。禁止按收益选 best phase；报告不含价格 / 收益 / PnL。

## Verifier

1. self-hash
2. 从报告输入全量重算并逐字段比较（含全部 family 机会日期）
3. file verifier 另读盘 verify 两个上游协议；路径必须为固定 repo-relative，拒绝绝对路径与 `..`

拒绝：外层 reseal 掩盖漂移、calendar/date/offset/tranche/opportunity/family_shift/snapshot/path/ID 篡改、ready 注入。

## 明确非目标

- 不接 CLI / 评分 / 股票池 / 排除 / 组合 / 订单 / 交易
- 不决定持仓与成交；机会 ≠ 候选存在 / 可成交
- 不跑 score / IC / backtest
