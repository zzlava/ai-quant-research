# 统计风险簇只读诊断（E6b）

研究用途 / **非行业分类** / **非交易就绪**。本诊断用显式阈值上的 Pearson 相关边构造确定性连通分量，作为**过渡性风险代理**，在合格 PIT 行业历史到位前用于分散度诊断。它**不是**行业 alpha，也不得自动接入评分、组合约束或交易引擎。

## 入口

`src/app/research/statistical_risk_clusters.py` → `diagnose_statistical_risk_clusters`

显式参数（**禁止默认值**）：

| 参数 | 含义 |
| --- | --- |
| `store` | `MarketStore` |
| `as_of` | 决策日；只允许该日及以前的 `adj_close` |
| `symbols` | 唯一候选集 |
| `lookback_bars` | 收益窗口长度（需要 `lookback_bars+1` 个交易日价格） |
| `correlation_threshold` | 建边阈值，范围 `(0, 1]` |

阈值与 lookback **不是**本仓库的冻结经济参数；测试中的数值仅作 fixture。正式取值仍由两层策略决策合同中的用户字段决定（当前仍为 `null`）。

## 点时与失败关闭

1. 交易日窗口取 `<= as_of` 的末 `lookback_bars+1` 个市场交易日。
2. 任何日历/行情结果出现 `> as_of` 的日期 → **立即失败**，不静默截断。
3. 每个 symbol 必须在这些交易日上各有一条有限、严格为正的 `adj_close`，且无重复日期，才能得到 `lookback_bars` 条收益。
4. 缺历史、缺交易日、重复日期、非有限/非正价格 → `unresolved`；**禁止**补零、前向填充，也不得把 unresolved 当成“单例低风险”。

## 相关与连通分量

- 仅对双方均可评估的 pair 计算 Pearson 相关。
- 常数收益序列或不足观测 → `unresolved` pair。
- `correlation >= threshold` 的边进入确定性 Union-Find；cluster id / 顺序由排序后的成员稳定生成。
- **链式连通**：A–B、B–C 均可过阈值时，A/B/C 同簇，即使 A–C 相关偏低。这只是风险代理拓扑，不是行业标签。

## 报告绑定与门闩

报告绑定：`data_snapshot_id`、`as_of`、参数、`candidates_hash`、pairs、clusters、unresolved reasons；`report_id` 自哈希，序列化键排序稳定。

固定门闩：

| 字段 | 值 |
| --- | --- |
| `diagnostic_only` | `true` |
| `ready_for_scoring` | `false` |
| `ready_for_trading` | `false` |
| `auto_apply` | `false` |
| `is_not_industry_classification` | `true` |
| `ready_for_portfolio_constraints` | 仅当无任何 unresolved symbol/pair 时为 `true`；否则 `false` |

即使 `ready_for_portfolio_constraints=true`，本里程碑也**不得**接入引擎或自动改组合。

## 与 PIT 行业的关系

统计簇不能替代行业中性。PIT 行业历史契约见 [`pit-industry-history-contract.md`](./pit-industry-history-contract.md)。在可审计 PIT 行业来源提供并验证通过之前，两层策略第二层的行业中性路径保持 **blocked**。
