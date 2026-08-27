# 第二层合同绑定的统计风险簇约束证据（E10c）

> **醒目标注：这是统计风险代理，不是行业分类。**
> PIT 行业史仍是未来增强层。
> **只有完整报告**（`ready_for_cluster_constraints=true`）才可被后续 **E10d** 约束分配器读取；**当前不自动应用**。

研究 / 实现地基 only。本里程碑把通用 E6b `diagnose_statistical_risk_clusters` 包装成两层策略决策合同绑定的约束证据；**不**接入评分、组合构建、订单、交易或券商。

## 入口

| 项 | 路径 |
| --- | --- |
| 引擎 | `src/app/research/layer_two_statistical_risk_clusters.py` → `diagnose_layer_two_statistical_risk_clusters` |
| 校验 | `verify_layer_two_statistical_risk_cluster_report`（需 `MarketStore` + 磁盘合同重算） |
| 测试 | `tests/test_layer_two_statistical_risk_clusters.py` |
| 通用诊断（只读复用） | `src/app/research/statistical_risk_clusters.py` |

显式参数（**禁止默认经济参数、禁止调用方覆盖**）：

| 参数 | 含义 |
| --- | --- |
| `store` | 已封印 `MarketStore` |
| `as_of` | 决策日历日；必须是市场交易日，且为 121 个价格点窗口的最后一天 |
| `decision_at` | timezone-aware；`decision_at.date() == as_of` |
| `symbols` | 非空、唯一、稳定排序后的规范 A 股代码 `^[0-9]{6}\.(SH\|SZ)$` |
| `repo_root` | 仓库根；用于绑定磁盘合同 |

## 合同绑定

精确绑定：

- 相对路径 `config/research/two-layer-strategy-decision-draft-v1.json`
- `contract_id=27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6`

校验 `layer_two.pit_industry` 与 `layer_two.statistical_risk_cluster` 的全部冻结旗标。合同值或字节篡改 → 绑定失败。

冻结经济参数（仅来自合同）：

| 字段 | 值 |
| --- | --- |
| lookback（收益天数） | 120（需 121 个 `adj_close` 价格点） |
| Pearson 相关阈值 | 0.65（正阈值） |
| 连通方式 | connected-components **chain linkage** |
| 簇 sleeve 最大权重 | 0.35 |
| 簇内最大持仓数 | 2 |

`adj_close` **只**用于封印快照内收益相关性；**不是**未来行业标签，也不是 alpha。

## 点时与失败关闭

1. 只使用 `<= decision date` 的已封印 market snapshot；输出绑定 `data_snapshot_id`。
2. 周末 / 非交易日 `as_of` → **抛错**。
3. 任一候选缺 121 个精确交易日价格、缺列、重复日、非有限/非正价、未来 bar、常数收益或任一 pair 无法计算 → `unresolved`，且整份报告 `ready_for_cluster_constraints=false`。
4. **禁止**把 unresolved 当单例或低风险；**禁止**补零 / 前向填充 / 复用别日。
5. 别名、BJ、空白、重复 symbol → **抛错**。候选输入顺序不影响 `report_id`。

## 输出与门闩

输出保留完整 generic diagnostic，并绑定 `contract_id` / path、决策时点、冻结算法与上限、醒目 `risk_proxy_annotation`。

固定：

| 字段 | 值 |
| --- | --- |
| `is_not_industry_classification` | `true` |
| `current_industry_backfill_forbidden` | `true` |
| `ready_for_scoring` | `false` |
| `ready_for_portfolio_construction` | `false` |
| `ready_for_orders` | `false` |
| `ready_for_trading` | `false` |
| `auto_apply` | `false` |
| `ready_for_cluster_constraints` | 仅 complete（无 unresolved）时为 `true` |

`ready_for_cluster_constraints=true` **只表示约束证据完整**，不表示可组合 / 可交易。

## Verifier

内容寻址 `report_id`。校验顺序：

1. self-hash
2. 磁盘合同绑定
3. 从 `MarketStore`、报告候选、`decision_at` 与磁盘合同完整重跑并逐字段比较

派生字段（簇、cap、annotation 等）被改后即使重封 outer hash 仍失败；store snapshot 改变也失败。禁止注入 current industry / sector / alpha；ready flags 不能被置 true。

## 明确非目标

- 不重写 / 删除 generic E6b 模块、测试或文档
- 不修改冻结合同 JSON / 策略 YAML
- 不运行 score / IC / backtest，不接券商
- 不自动应用簇约束；E10d 尚未交付
