# 第一层指数风险特征与开发协议（E6c 地基 / E8b 确认升级）

研究用途 / **非择时策略** / **非交易就绪**。E6c 交付只读指数风险特征与密封开发协议校验器；E8b 把用户已确认的第一层长历史开发决策升级为 schema v2（`confirmed_for_implementation_but_not_ready`）。**不**接入评分、组合构建、BacktestEngine、成员过滤或交易；**不**实现 regime engine；**不**取行情、不跑真实评价。

本里程碑**未**对真实行情跑预检、评分、IC、事件收益、相位分析或回测，也**未**触碰任何已消费 OOS。

## 为什么第一层需要长而干净的指数历史

第一层回答的是：**在可见市场状态下，账户股票风险预算应是多少**。这依赖指数层面的趋势、波动与回撤等**状态描述**，而不是个股截面打分。

长历史、日历对齐、失败关闭的指数序列现已物化为快照 `9dbc0032539be62518bbc7f64e67cf9deb64e0564dcaca8aecc65bdc1d3890d0`。它有助于：

1. 在足够多样的市场阶段上描述状态分布；
2. 把开发窗、历史验证段、seen-robustness、已消费 OOS 与新冻结 OOS 切开；
3. 避免用个股噪声冒充市场状态。

但这**不等于**已经验证的择时策略。特征可以计算；门槛与档位现已确认，但实现与开发证据仍是 blocker。

## 特征入口（描述性连续量）

`src/app/research/index_risk_features.py` → `diagnose_index_risk_features`

显式参数（**禁止默认值**）：

| 参数 | 含义 |
| --- | --- |
| `store` | `MarketStore` 或仅提供 `get_calendar` / `get_index_bars` / `snapshot` 的窄接口 |
| `index_symbol` | 指数代码 |
| `as_of` | 决策日；只用 `<= as_of` 的市场日历与收盘价 |
| `trend_lookback_bars` | SMA 窗口长度（交易日） |
| `volatility_lookback_bars` | 简单日收益样本数（须 `>= 2`） |
| `drawdown_lookback_bars` | 滚动峰值/回撤窗口长度 |

点时与失败关闭：

1. 原始市场日历必须是真实 `date` 序列、严格递增、无重复，且全部 `<= as_of`，然后才取末段窗口；
2. 任一结果日期 `> as_of` → 立即失败；
3. 所需交易日上必须各有一条有限、严格为正的 `close`，且无重复；
4. **禁止**前向填充、补零、用开盘/入场价回退；
5. 密封报告的三个日期窗口必须严格递增、互不重复、全部 `<= as_of`，并共享同一终端交易日（终端不必等于 `as_of`，因 `as_of` 可为非交易日）；文件校验除自哈希外也强制检查该结构。

输出仅含连续特征：最新收盘、SMA、`close/SMA`、年化已实现波动、滚动峰值、回撤、观测计数与精确窗口日期。报告绑定 `data_snapshot_id`、指数、`as_of`、参数，并自哈希 `report_id`。

固定门闩：`diagnostic_only=true`，`ready_for_scoring/backtest/trading=false`，`auto_apply=false`。**无** regime 标签，**无**风险预算输出。

## 统计定义（须与报告字段一致）

| 量 | 定义 |
| --- | --- |
| 价格字段 | 指数 `close`（非前向填充） |
| 简单收益 | \(r_t = close_t / close_{t-1} - 1\) |
| SMA | 趋势窗口内收盘价算术平均 |
| close/SMA | `latest_close / SMA` |
| 已实现波动 | 波动窗口内简单收益的**样本标准差**（ddof=1），再乘 \(\sqrt{242}\) |
| 年化约定 | `sample_std_simple_daily_returns_times_sqrt_242`；协议已确认年化交易日 `242` |
| 滚动峰值 | 回撤窗口内最大 `close` |
| 回撤 | `latest_close / rolling_peak - 1` |

## 开发协议 schema v2（E8b：已确认 / not-ready）

机器可读协议：[`config/research/layer-one-index-development-protocol-draft-v1.json`](../../config/research/layer-one-index-development-protocol-draft-v1.json)

校验器：`src/app/research/layer_one_index_protocol.py`

- `status=confirmed_for_implementation_but_not_ready`
- `pending_user_decision_count=0` / `user_decisions_resolved=true`
- 只要事实 / 实现 / 开发证据 / 未来 OOS 观察 blocker 存在，总体 `resolved=false`
- `ready_for_scoring/backtest/trading=false`，`auto_apply=false`
- 磁盘绑定（file verifier **读盘**核对，不是口头声明）：
  - research trial ledger：`config/research/research-trial-ledger-v1.json` 当前 `ledger_id`
  - two-layer 合同：`config/research/two-layer-strategy-decision-draft-v1.json` 当前 schema v2 `contract_id`
- schema v1 封印草稿仍可校验（见 `tests/fixtures/research/layer-one-index-development-protocol-draft-v1-sealed.json`）

### 已确认经济选择（摘要）

| 项 | 确认值 |
| --- | --- |
| 风险状态指数 | 中证全指**价格指数**（price returns）；独立事实契约已确认 `000985.CSI`，长历史快照已物化，但本冻结协议尚未迁移该 overlay |
| 业绩对照 | 中证全指**全收益**（total return）；独立事实契约已确认 `H00985.CSI`，长历史快照仅对 `2011-08-02` / `2011-08-03` 使用哈希绑定的中证官方原始行；本冻结协议尚未迁移该 overlay |
| 开发窗 | 2005-01-01..2012-12-31 |
| 历史验证段（**禁止称 OOS**、禁止调参） | 2013-01-01..2016-12-31；2017-01-01..2019-12-31；2020-01-01..2021-12-31 |
| seen robustness only | 2022-01-01..2024-12-31 |
| 已消费 OOS（禁止复用） | 2025-01-01..2026-08-21 |
| 新冻结 OOS | 自 2026-08-22 起计划连续记录 12 个月；**不是** 60/90 人工解锁硬前提，但满期前不得称完整 OOS 通过；**勿与已消费窗混淆** |
| lookbacks | trend 200 / vol 60 / drawdown 242；年化 242 |
| 趋势 | deadband ±3%；base 正/中/负 = 0.9 / 0.6 / 0.3 |
| 波动 | 目标 18%；≤18 无额外 cap；18–27 → 0.6；27–36 → 0.3；>36 → 0 |
| 指数回撤（相对 242 高点） | >-10 无 cap；≤-10 → 0.6；≤-15 → 0.3；≤-20 → 0 |
| 账户回撤 | ≤-10 → 0.6；≤-15 → 0.3；≤-18 risk lock/0；-20 红线；恢复需 20 交易日冷静 + 指数非负趋势 + vol<27% + 显式用户确认；重启不清除 |
| 最终预算 | `min(trend base, vol cap, index DD cap, account DD cap)`，只映射 `[0, 0.3, 0.6, 0.9]`；risk lock 优先 |
| 调整 | 降仓可每日；升仓仅每周首个交易日，且用前收已知状态 |
| 防守资产 | 仅现金；`max_stock_budget=0.9` |
| 基准基线 | 90% 中证全指全收益 + 10% 现金 |
| 成本 | 佣金 0.00025/边、最低 5、滑点 5bps、压力 15bps；印花税 = 官方历史卖边表（**pending factual**；禁止用旧 flat 表冒充完成） |
| 决策时点 | 仅用收盘后可得数据；T+1 行动；缺/晚 bar 失败关闭 |
| 部署升级 | 历史验证通过后最多 30% 人工试验；30%≥3 个月无严重异常 + 用户确认 → 60%；60%≥3 个月且无 risk lock + 用户确认 → 90%；永不自动升级；解锁审计含 timestamp / version / snapshot / user confirmation |

### Hard gates（准则已确认；结果仍 pending_development_evidence）

- 每个验证段与 combined MDD ≥ -0.20
- combined 扣费后年化收益 > 0
- Calmar ≥ 0.5
- 相对 90/10 基线：回撤幅度改善 ≥ 25%；若基线 CAGR>0 则保留 ≥ 60%
- 压力 MDD 不得突破 -20%
- budget occupancy **仅诊断**，无事后阈值

### Evidence blockers（分类）

- `pending_factual_source_verification`：冻结协议尚未迁移已完成的指数身份和印花税事实 overlay；全收益来源恢复规则已封印，不再是事实缺口
- `pending_implementation`：regime engine；risk lock 持久化/UI
- `completed_downstream_data_foundation`：长历史原始采集与 materializer 已完成；严格快照尚待迁移进本冻结协议
- `pending_development_evidence`：hard-gate 段/合并结果
- `future_oos_observation`：新冻结 OOS 未满期（与已消费窗分离）

## 只读 CLI

```bash
.venv/bin/python -m app.cli verify-layer-one-index-protocol \
  --protocol-file ./config/research/layer-one-index-development-protocol-draft-v1.json \
  --repo-root .

.venv/bin/python -m app.cli verify-index-risk-feature-report \
  --report-file /path/to/local-sealed-report.json
```

与两层合同门闩的关系见 [`two-layer-strategy-implementation-gate.md`](./two-layer-strategy-implementation-gate.md)。

## 关闭门（当前仍禁止）

- **禁止**实现/接入 regime engine 或把协议写成可运行策略默认值；
- **禁止**绕过 [`csi-all-share-index-identity.md`](./csi-all-share-index-identity.md) 的固定两日官方覆盖规则、哈希绑定与严格开市日历，或把事实 overlay 未迁移的旧协议字段直接改成 ready；
- **禁止**把历史验证段称作 OOS，或复用已消费 OOS；
- **禁止**在新冻结 OOS 满期前声称完整 OOS 通过；
- **禁止**真实评分 / 回测 / 交易 / 自动升级。
