# A 股事件候选 OOS 冻结协议（development-only）

本协议在 2022-01-01..2023-12-31 开发窗证据上冻结**第一次未来 2025+ 一次性 OOS** 的规则。它不是评分、回测或交易授权。

机器可读合同：[`config/research/a-share-event-candidate-oos-freeze-v1.json`](../config/research/a-share-event-candidate-oos-freeze-v1.json)

## 绑定证据

只绑定已核验的开发窗诊断，不读取 OOS-3，也不使用 2024/2025+ 收益做选择。

| 字段 | 值 |
| --- | --- |
| `report_id` | `782a042d666600a4383cce72ecff27c2599acbde0acd2b0f2100b164d928bd01` |
| 诊断目录 | `data/all-a-share-historical-v1/event-candidate-diagnostics/development-2022-2023-v1` |
| `strategy_config_hash` | `796b793856dcd02a` |
| 窗口 / 标签硬截断 | `2022-01-01..2023-12-31` |
| 基准 | `000300.SH` |
| 预声明假设 | 11 个，全部注册，不按结果删减 |

## 主终点与门控

一次性 OOS 只认 **20 个交易日相对沪深 300 收益**（`fwd_rel_hs300_ret_20d`）。原始收益以及 5/10 日相对收益只作描述，不能让候选晋级。冻结后不得调阈值。

开发窗提名（两年 2022 与 2023 的已封存 summary，主终点）：

- 20d 相对指标两年均已知；
- `candidate_direction_supported_2022_2023_rel_hs300` 为 true；
- 两年 `known_coverage >= 0.90`；
- 两年 labeled 样本 `>= 100`；
- 二元信号两年 treatment/control labeled 均 `>= 20`。

通过门控不等于上线。通过者只锁定为未来授权 2025+ 窗口的一次性评估名单。

当前锁定候选：`forecast_upward_revision`、`audit_non_standard_opinion`。其余 9 个假设仍注册并保留失败原因。

## OOS 政策

- 仅在**用户已授权**的 2025+ 窗口评估上述锁定名单，且只评估一次；
- 授权窗口与绑定见 [`config/research/a-share-event-candidate-oos-one-shot-authorization-v1.json`](../config/research/a-share-event-candidate-oos-one-shot-authorization-v1.json)；
- 公告窗：`2025-01-01..2026-07-23`；完整 20 日标签最晚入场：`2026-07-24`；`label_hard_end=2026-08-21`；
- 2024 已观察，禁止用于选择或调参；
- 缺失保持 unknown；不得改参数、符号、来源、可得性、阈值或候选名单；
- 必须报告多重检验（当前 n=2）；不得自动晋升评分或交易；必须人工审查。

## 授权一次性 OOS 评估（已于 2026-08-25 消费）

机器可读授权合同：[`config/research/a-share-event-candidate-oos-one-shot-authorization-v1.json`](../config/research/a-share-event-candidate-oos-one-shot-authorization-v1.json)

绑定：`authorization_id=efee84f049b6e8590a01bce2f185aacd7d01c5fae6845e9dca5432d8de980439`，`freeze_id=5d5298f0115f883c29d96cf2a1892ce4de7295c2068cabea96f23db393bad92e`，OOS 行情 `snapshot_id=b6f664d31d8ffcdabbb655e888467c75dbfa6a7f8bd863d698febb015f5b0427`，OOS 事件 `snapshot_id=73f1dedf83b0c28d0ba5ae933205e2777b02e27d356d4dd5cf62dcb10155b28f`，基准 `000300.SH`，主终点 `fwd_rel_hs300_ret_20d`。

一次性评估已执行并消费。输出目录 [`data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1`](../data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1) 与消费收据 [`one-shot-v1.consumption-receipt.json`](../data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1.consumption-receipt.json) **不可覆盖、不得重跑**。封存审查说明见 [`docs/a-share-event-candidate-oos-one-shot-review.md`](a-share-event-candidate-oos-one-shot-review.md)。

| 字段 | 值 |
| --- | --- |
| `report_id` | `0db5875c389520443c5249005d420f7bbc949385cd1c94448cf159113a00051d` |
| `receipt_id` | `e931fafca1dcf596bb9b2b345792c0ded327a13e78e2ea99092fc169fdfce6c6` |
| `observation_rows` | 10931 |
| `candidate_multiplicity` | 2 |
| `ready_for_scoring` | false |
| `ready_for_trading` | false |
| `auto_deploy` | false |
| `human_review_required` | true |

主终点统计（20 日相对沪深 300，方向复制诊断；**不构成 alpha/盈利结论**）：

| 假设 | eligible | known | unknown | known_coverage | labeled | signal1 | signal0 | mean_rel_20d (1) | mean_rel_20d (0) | spread (1−0) | outcome |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `audit_non_standard_opinion` | 10652 | 10652 | — | — | 10329 | 383 | 9946 | 0.0012302876155067062 | 0.00884338373488073 | −0.007613096119374024 | `direction_replicated` |
| `forecast_upward_revision` | 279 | 185 | 94 | 0.6630824372759857 | 178 | 23 | 155 | 0.0030925688119775078 | −0.016583190146612838 | 0.019675758958590344 | `not_evaluable` |

`forecast_upward_revision` 因 OOS 窗内 `known_coverage=0.6630824372759857` 低于冻结门槛 `0.90`，标记为 `not_evaluable`；不得据此调参或重跑。

原始执行命令（仅供溯源；**不得再次运行**）：

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

.venv/bin/python -m app.cli evaluate-a-share-event-candidate-oos-one-shot \
  --strategy all_a_share_historical_value_portfolio_selected_v2 \
  --authorization-file ./config/research/a-share-event-candidate-oos-one-shot-authorization-v1.json \
  --freeze-file ./config/research/a-share-event-candidate-oos-freeze-v1.json \
  --market-dir ./data/all-a-share-oos-20241001-20260821-v1/parquet \
  --event-dir ./data/all-a-share-oos-20241001-20260821-v1/events-v1
```

## 校验

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

.venv/bin/python -m app.cli verify-a-share-event-candidate-freeze \
  --freeze-file ./config/research/a-share-event-candidate-oos-freeze-v1.json \
  --diagnostic-dir ./data/all-a-share-historical-v1/event-candidate-diagnostics/development-2022-2023-v1
```

合同、绑定报告、哈希、候选名单或门控任一不一致即失败关闭。该命令不联网、不读 token、不跑 score/IC/回测。
