# A 股数据与可执行性缺口审计 v1

本文件把策略讨论中的经验判断转换成可审计的工程要求。它不是因子有效性声明，也不授权自动交易。冻结的 `p10_h20` 配置仍保持不变；任何新增数据或执行规则都必须使用新的配置哈希，并重新做训练期、验证期和未来样本外检验。

## 当前结论

| 主题 | 当前状态 | 决定 |
| --- | --- | --- |
| 财务公告点时性 | 已实现 | `fina_indicator` 以公告日 23:59（Asia/Shanghai）为保守 `available_at`；严格模式只使用 `update_flag=0`，无独立修订发布时间的重述不回填 |
| 停牌卖出 | 已实现 | 持仓继续保留并按最后可见价格估值，恢复交易后再判断退出 |
| 跌停卖出 | 已实现且偏保守 | 开盘位于官方跌停价时不假设可以成交，持仓顺延；使用逐日 `stk_limit`，不按固定 10% 猜测 |
| 停牌/涨停买入 | 本里程碑补齐能力 | 新增有界顺延选项；默认仍取消，以保持冻结配置和既有哈希不变 |
| ST、停牌、新股、流动性过滤 | 已实现 | 当前冻结策略为上市至少 120 个自然日、排除 ST/当日停牌、20 日均成交额门槛；“6-12 个月”不是已验证结论，不能用看过的 2024 结果调参 |
| 交易费用 | 部分实现 | 佣金、最低佣金、分时点印花税、固定滑点已计入；成交额占比冲击模型和排队成交概率仍缺失 |
| 分年度与基准比较 | 已实现诊断 | 冻结组合稳健性报告已按年比较；不等于严格 PIT 或未来收益证明 |
| 换仓日期敏感性 | 未实现 | 应在冻结逻辑上平移锚点，输出月初/月中/月末或多个交易日偏移；不得挑最好日期再报单一结果 |
| 行业/市值中性化 | 数据不完备 | 当前行业字段不能证明历史点时性，只能诊断暴露，不能据此做严格历史中性化 |

## 买入顺延契约

`TradeConfig` 新增两个可选字段：

```yaml
trade:
  blocked_entry_policy: defer
  max_entry_delay_days: 5
```

- `cancel` 是默认值，要求 `max_entry_delay_days=0`，保持旧配置行为和配置哈希。
- `defer` 只处理有明确当日行情证据的全日停牌和开盘位于官方涨停价；不得把缺失行情当成停牌。
- `max_entry_delay_days` 按后续交易日计数。超过上限、研究窗口结束或失去组合席位时必须记录为过期，不能静默删除。
- 顺延订单保留原信号日、原始排序和原信号 ATR；同一股票的新信号不能替换或重复创建该订单。
- 实际持有天数从真实成交日开始，不从信号日开始。
- 输出必须区分 `orders_deferred`、累计 `entry_deferral_days`、`orders_filled_after_deferral` 和 `deferred_orders_expired`。
- 任一待成交股票在应有交易日缺少日线时直接失败；禁止用前收或另一日行情假装可成交。

这项能力暂不写入已冻结的 `all_a_share_historical_value_portfolio_selected_v2`。启用后属于新实验，必须产生新配置哈希，并使用没有参与规则选择的未来样本评估。

## 候选事件数据层

所有事件表都必须保存原始行、规范化行、来源行哈希、采集 manifest、源接口与版本。日期字段只证明“哪一天公告”，不能证明盘中具体时刻；只有日期而没有时刻时，统一保守设为公告日 23:59（Asia/Shanghai），因此最早只能用于下一交易日决策。

### 第一批：字段可支持点时契约

1. `earnings_forecast_events`（业绩预告）
   - 来源：Tushare `forecast` / `forecast_vip`。
   - 必需字段：`symbol`、`ann_date`、`report_period`、`type`、`p_change_min/max`、`net_profit_min/max`、`last_parent_net`、`first_ann_date`、`summary`、`change_reason`。
   - 同一报告期的多次公告不得覆盖；按公告日逐版本保存。

2. `earnings_express_events`（业绩快报）
   - 来源：Tushare `express` / `express_vip`。
   - 必需字段：`symbol`、`ann_date`、`report_period`、收入、营业利润、利润总额、净利润、资产、净资产、同比指标和摘要。
   - 快报与正式财报是不同事件，后续正式财报不得回写到快报公告日。

3. `holder_count_events`（股东户数）
   - 来源：Tushare `stk_holdernumber`。
   - 必需字段：`symbol`、`ann_date`、`end_date`、`holder_num`。
   - 只能在 `ann_date` 之后计算户数变化；禁止按统计截止日提前使用。

4. `share_unlock_events`（限售解禁）
   - 来源：Tushare `share_float`。
   - 必需字段：`symbol`、`ann_date`、`float_date`、`float_share`、`float_ratio`、股东和股份类型。
   - 研究某决策日的未来解禁压力时，只能使用当时已经公告的记录。

5. `audit_opinion_events`（审计意见）
   - 来源：Tushare `fina_audit`。
   - 必需字段：`symbol`、`ann_date`、`report_period`、`audit_result`、审计费用和会计师事务所。
   - 非标审计意见先作为排雷候选，不预设收益权重。

### 第二批：必须先解决可得时点或跨表口径

- 股权质押：优先使用带 `ann_date` 的质押明细；聚合统计若只有截止日而没有公告/发布时间，不能直接进入历史策略。
- “大存大贷”、应收账款增速远超营收、其他应收款异常：当前 `fina_indicator` 不含完整资产负债表和利润表原始科目，需要新增 `balancesheet` 与 `income` 点时版本层，并按公司类型统一口径。
- 年报拖延：Tushare `disclosure_date` 的当前行含预披露、修改和实际日期，但若不能取得每次历史修改版本，就不能重建某个过去决策日看到的计划日期。
- 行业与同行：需要带历史生效区间和 `available_at` 的行业分类；当前静态行业名不能用于严格 PIT 中性化或海外同行映射。
- 指数调仓效应：必须有公告全文/附件、公告日与生效日、当时完整成员或可审计变更链。当前公开重建 CSI300 的 `source_date` 不是已证明的历史 `available_at`，不能升级成正式因子回测。

## 暂不进入主策略

- “股民盲目性”“量化影响”“政策管制更强”等宏观叙述没有可复现的逐日观测定义，不能直接转成主观加分。
- 小市值、股东户数下降、拖延披露等只作为候选假设。先做覆盖率、缺失机制、截面 IC、分年稳定性和成本后组合检验，再决定是否采用。
- 打新增强依赖账户市值、申购资格、中签率与发行日历，是独立收益模块，不应混入股票选择 alpha。

## 后续顺序

1. 保持冻结策略不动，先完成并验证有界买入顺延能力。
2. 建立上述五张第一批事件表的独立 overlay schema、manifest 和离线校验器。
3. 先采集业绩预告/快报和审计意见；检查全市场历史覆盖与修订重复，不立刻加权。
4. 在开发样本做单因子方向、覆盖率和分年度 IC；2024 已被用于多轮观察，不能再称为未见样本。
5. 获取 2025 年以后同口径行情和事件数据，把冻结的新候选逻辑放到真正未参与选择的时间段验证。

## 官方字段参考

- 上交所交易规则：<http://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml>
- 证监会《上市公司信息披露管理办法》：<https://www.csrc.gov.cn/csrc/c101953/c7547359/content.shtml>
- Tushare 业绩预告：<https://tushare.pro/document/2?doc_id=45>
- Tushare 业绩快报：<https://tushare.pro/document/2?doc_id=46>
- Tushare 股东户数：<https://tushare.pro/document/2?doc_id=166>
- Tushare 限售解禁：<https://tushare.pro/document/2?doc_id=160>
- Tushare 股权质押统计/明细：<https://tushare.pro/document/2?doc_id=110>、<https://tushare.pro/document/2?doc_id=111>
- Tushare 财务审计意见：<https://tushare.pro/document/2?doc_id=80>
- Tushare 财报披露计划：<https://tushare.pro/document/2?doc_id=162>
