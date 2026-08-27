# 两层策略用户确认记录与证据门（E8a）

研究用途。本文记录用户已确认的经济决策，以及仍阻止 scoring / backtest / trading / auto_deploy 的**分类证据门**。
**不声称实现完成，不生成可运行策略 YAML，不授权实盘。**

机器可读合同：[`config/research/two-layer-strategy-decision-draft-v1.json`](../../config/research/two-layer-strategy-decision-draft-v1.json)（`schema_version=2`，`contract_version=two-layer-strategy-decision-v2`）

| 字段 | 当前值 |
| --- | --- |
| `status` | `confirmed_for_implementation_but_not_ready` |
| `user_decisions_resolved` | `true`（用户经济决策已冻结；与总体 `resolved` 分离） |
| `pending_user_decision_count` | `0` |
| `resolved`（总体） | `false`（仍有分类证据门，且 status/ready 为 not-ready） |
| `ready_for_scoring` / `ready_for_backtest` / `ready_for_trading` / `auto_deploy` | 全部 `false` |
| 绑定 ledger | `config/research/research-trial-ledger-v1.json`（自哈希绑定） |
| 已消费 OOS | `reuse_forbidden=true`（2025+ 不可再作未见样本） |

## 已确认（用户决策，非 blocker）

### 账户与边界

| 项目 | 确认值 |
| --- | --- |
| `initial_cash` | `80000`（非 blocker） |
| 个股开发窗 | `2022-01-01`..`2023-12-31`（seen development only） |
| 2024 | seen robustness check only |
| 2025+ | consumed OOS，禁止复用 |
| Alpha | 仅预登记 `quality` / `value` / `medium_momentum_12_1` / `defensive_low_vol`；权重属 `pending_development_evidence`；机构/事件不入 alpha；禁止现写可运行 YAML |

### 第一层（市场状态与资产配置）

| 项目 | 确认值 |
| --- | --- |
| `objective` | `absolute_return` |
| 业绩比较基准 | 中证全指**全收益**（名称已确认；Tushare 主行情 + 中证官网核对；**具体代码无本地证据不得猜**） |
| 风险状态指数 | 中证全指**价格指数**（同上，代码 pending factual verification） |
| 非股票资金 | 仅 `CNY_CASH`；ETF 为 future enhancement |
| `max_acceptable_drawdown` | `-0.20` |
| 股票预算 | `0.0`..`0.9`；档位 `0 / 0.3 / 0.6 / 0.9` |
| 趋势 | lookback `200`，中性带 `±0.03`，基础预算 `0.9 / 0.6 / 0.3` |
| 波动 | lookback `60`，年化 `242`，目标 `0.18`；阈值 `18/27/36` → no cap / `0.6` / `0.3` / `0` |
| 指数回撤 | lookback `242`；`-10/-15/-20` → `0.6 / 0.3 / 0` |
| 账户回撤 | `-10/-15/-18/-20`；`-18` 风险锁定；冷静期 `20` 交易日 + 指数非负趋势 + 60 日年化波动 `<27%` + **显式人工恢复**；重启不可解除；UI 必须醒目 |
| 调仓节奏 | 每日可降；仅每周首个交易日可加 |

### 第二层（个股袖套）

| 项目 | 确认值 |
| --- | --- |
| 股票池 | 沪深普通 A，不含北交所 |
| 新开仓禁止 | ST/退市风险；停牌禁止买 |
| 上市天数 | `>=180` 交易日 |
| 流动性 | ADV20 中位成交额 `>=5000` 万；20 日内 `>=15` 可交易；订单 `<=ADV20` 的 `0.1%` |
| 流通市值 | `<30` 亿排除；`30–50` 亿仓位乘子 `0.5`；`50–100` 亿 `0.75`；`>=100` 亿 `1.0` |
| ownership | 仅 `diagnostic`；missing 保持 unknown |
| 无 PIT 行业 | 允许 30% 试运行，但**必须**统计风险簇且醒目标注；当前行业回填禁止 |
| 统计簇 | lookback `120`，corr `0.65`，簇袖套权重上限 `0.35`，最多 `2` 只 |
| 仓位规模 | 每仓 `>=8000`；预算 `30/60/90` 最多 `3/6/9`，绝对上限 `9` |
| 持有 / tranche | hold / holding_cycle `40` 市场交易日（均匀错开的 phase cycle 长度，**不是**活动 tranche 数）；活动 tranche 数 = 活动目标仓位数，预算 `30/60/90` → `3/6/9`，总上限 `9`；一股一活动 tranche；均匀错开；不补仓 |
| 时序 | T 收盘后决策；T+1 open 尝试成交；成交日为持有第 1 天；满 40 市场日后下一可交易 open 退出 |
| 停牌 | 计入持有日；到期仍停则复牌后首个可卖日出 |
| 退市 | 无官方最终结算证据则失败关闭；实盘变人工事件 |
| 排雷 | 非标审计单次直接排除；其他可审计 PIT 预警 `>=2` 排除、`1` 项仓位减半；missing 保持 unknown 且不可被 alpha 抵消 |
| 退出 | 无固定止盈；相对成交价亏损 `15%` 下一可交易 open 灾难止损；普通排名下降不提前退出；ST/退市、财务硬排雷、组合降仓/锁定可提前退出 |
| 候选不足 / missing / 整手不可负担 | 留现金，不放松阈值，不复用其他日期候选 |
| 事件（户数、预告/快报、解禁、质押） | PIT 覆盖与开发证据前均为 diagnostic；仅当未来 30 日解禁/流通盘 `>10%` 且覆盖/分母/`available_at` 完整可审计时，才允许未来硬排除，否则规则整体不启用 |

### 部署升级（永不自动）

| 阶段 | 条件 |
| --- | --- |
| 30% | 历史验证通过后，人工试运行 |
| 60% | 30% 至少 3 个月后，**仅用户确认** |
| 90% | 60% 至少 3 个月且无风险锁后，**仅用户确认** |
| 12 个月新 OOS | 继续记录并标记，但**不是** 60%/90% 人工升级硬门 |
| 解锁记录 | 必须含时间、合同版本、数据 snapshot、用户确认 |

## 分类证据门（仍阻止 ready）

`pending_user_decision` 已清零，故 `user_decisions_resolved=true`。
总体 `resolved` 仍为 `false`：只要 `evidence_blockers` 非空，或 status 含 not-ready，或任一 ready flag 为 false，校验器 fail-closed 不报总体已解决。

剩余 blocker 只能落在下列类别：

| 类别 | 含义（例） |
| --- | --- |
| `pending_factual_source_verification` | 中证全指全收益/价格指数的 Tushare+官网代码未本地核实；印花税官方历史时间表证据未齐 |
| `pending_implementation` | regime 预算引擎、组合构建、风险锁 UI/持久化等尚未实现 |
| `pending_development_evidence` | alpha 权重选择、第一层硬门分段评价尚未跑出开发证据 |
| `future_enhancement` | 现金货币/短债 ETF；真实 PIT 行业历史；机构/事件覆盖提升后再验证 |

## 明确未完成 / 禁止事项

1. 本文与合同**不等于**实现完成；`confirmed_for_implementation_but_not_ready` 只表示用户决策已冻结。
2. 不得新增可运行策略配置 / YAML。
3. 不得启动真实评分、回测、交易或 auto-deploy。
4. 不得复用已消费 OOS；不得把 missing 填成 0/false；不得猜测未核实的指数代码。
5. layer-one / tranche 协议文件不在本任务范围内，保持其既有封印状态。

校验：

```bash
.venv/bin/python -m app.cli verify-two-layer-decision-contract \
  --draft-file ./config/research/two-layer-strategy-decision-draft-v1.json \
  --repo-root .
```
