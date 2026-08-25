# A 股事件候选一次性 OOS 封存审查说明

本文档记录 2026-08-25 已消费完毕的**第一次 2025+ 一次性 OOS 方向复制诊断**封存审查结论。它不是评分、IC、回测或交易授权；结果**不构成 alpha 或盈利结论**。

相关合同：

- 冻结协议：[`config/research/a-share-event-candidate-oos-freeze-v1.json`](../config/research/a-share-event-candidate-oos-freeze-v1.json)（[`docs/a-share-event-candidate-oos-freeze.md`](a-share-event-candidate-oos-freeze.md)）
- 一次性授权：[`config/research/a-share-event-candidate-oos-one-shot-authorization-v1.json`](../config/research/a-share-event-candidate-oos-one-shot-authorization-v1.json)

## 封存身份

| 字段 | 值 |
| --- | --- |
| `authorization_id` | `efee84f049b6e8590a01bce2f185aacd7d01c5fae6845e9dca5432d8de980439` |
| `freeze_id` | `5d5298f0115f883c29d96cf2a1892ce4de7295c2068cabea96f23db393bad92e` |
| `report_id` | `0db5875c389520443c5249005d420f7bbc949385cd1c94448cf159113a00051d` |
| `receipt_id` | `e931fafca1dcf596bb9b2b345792c0ded327a13e78e2ea99092fc169fdfce6c6` |
| 消费日期 | 2026-08-25 |

## 评估窗口与快照绑定

| 字段 | 值 |
| --- | --- |
| 公告窗 | `2025-01-01..2026-07-23` |
| 完整 20 日标签最晚入场 | `2026-07-24` |
| `label_hard_end` | `2026-08-21` |
| OOS 行情目录 | `data/all-a-share-oos-20241001-20260821-v1/parquet` |
| OOS 行情 `snapshot_id` | `b6f664d31d8ffcdabbb655e888467c75dbfa6a7f8bd863d698febb015f5b0427` |
| OOS 事件目录 | `data/all-a-share-oos-20241001-20260821-v1/events-v1` |
| OOS 事件 `snapshot_id` | `73f1dedf83b0c28d0ba5ae933205e2777b02e27d356d4dd5cf62dcb10155b28f` |
| 策略 | `all_a_share_historical_value_portfolio_selected_v2` |
| `strategy_config_hash` | `796b793856dcd02a` |
| 基准 | `000300.SH` |

## 封存产物（不可覆盖、不得重跑）

输出目录：

```text
data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1/
├── report.json
├── observations.parquet
└── candidate_summary.parquet
```

消费收据：

```text
data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1.consumption-receipt.json
```

自校验报告：[`data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1/report.json`](../data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1/report.json)

消费收据：[`data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1.consumption-receipt.json`](../data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1.consumption-receipt.json)

上述目录与收据一经写入即视为授权消费完毕；**禁止覆盖、禁止重跑** `evaluate-a-share-event-candidate-oos-one-shot`。

## 研究边界

| 字段 | 值 |
| --- | --- |
| `candidate_multiplicity` | 2 |
| 主终点 | 20 个交易日相对沪深 300 收益（`fwd_rel_hs300_ret_20d`）；**唯一**决定 OOS 结果的终点 |
| 描述性窗口 | 5/10 日相对收益及原始收益仅作描述，不得晋级候选 |
| `ready_for_scoring` | false |
| `ready_for_trading` | false |
| `auto_deploy` | false |
| `human_review_required` | true |

提名候选（冻结名单，n=2）：

1. `forecast_upward_revision` — 同一报告期后续公告上调变动中点，预期正向反应
2. `audit_non_standard_opinion` — 审计意见非精确标准无保留意见，预期负向反应

总 `observation_rows=10931`（两假设事件级观测合计写入 `observations.parquet`）。

## 主终点结果

统计量：`mean_rel_hs300_return_spread_1_minus_0`（signal1 组 20d 相对沪深 300 均值减 signal0 组均值）。无 p 值；不得解读为 alpha 或盈利保证。

### `audit_non_standard_opinion`

| 指标 | 值 |
| --- | ---: |
| eligible | 10652 |
| known | 10652 |
| labeled | 10329 |
| labeled signal1 | 383 |
| labeled signal0 | 9946 |
| mean relative 20d (signal1) | 0.0012302876155067062 |
| mean relative 20d (signal0) | 0.00884338373488073 |
| spread (1−0) | −0.007613096119374024 |
| **outcome** | **`direction_replicated`** |

预声明方向为负向；signal1（非标准意见）20d 相对均值低于 signal0，spread 为负，与开发窗方向一致。

### `forecast_upward_revision`

| 指标 | 值 |
| --- | ---: |
| eligible | 279 |
| known | 185 |
| unknown | 94 |
| known_coverage | 0.6630824372759857 |
| labeled | 178 |
| labeled signal1 | 23 |
| labeled signal0 | 155 |
| mean relative 20d (signal1) | 0.0030925688119775078 |
| mean relative 20d (signal0) | −0.016583190146612838 |
| spread (1−0) | 0.019675758958590344 |
| **outcome** | **`not_evaluable`** |

OOS 窗内 `known_coverage=0.6630824372759857` **低于**冻结授权门槛 `min_known_coverage=0.90`，故不进入方向复制判定。即使 spread 符号与预声明正向一致，亦**不得**据此宣称复制成功、调参或重跑。

## 审查结论

1. **一次性授权已消费**：`authorization_id` 对应评估已于 2026-08-25 完成；消费收据 `receipt_id` 已写入，授权不可再次使用。
2. **多重检验**：`candidate_multiplicity=2`；两候选均须人工审查，不得因单一 `direction_replicated` 自动晋升。
3. **不构成上线依据**：全部产物固定 `ready_for_scoring=false`、`ready_for_trading=false`、`auto_deploy=false`；不得接入 score、IC、成员过滤、排除、组合、下单或回测。
4. **封存完整性**：以 [`report.json`](../data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1/report.json) 自校验哈希为准；任何对输出目录或消费收据的覆盖均视为协议违规。
5. **下一步**：仅允许人工审查上述封存产物与开发窗证据；禁止以 OOS 结果回写阈值、候选名单或冻结门控。
