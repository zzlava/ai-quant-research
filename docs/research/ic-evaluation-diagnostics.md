# IC / 分位组合评价统计诊断说明

`analyze-ic` 输出的截面 Spearman IC 与全截面因子分位数组合 spread 都是**诊断指标**，不是组合收益证明，也不能授权评分或交易。

## 报告顶层标志

固定输出：

- `diagnostic_only=true`
- `tradable_long_short=false`
- `ready_for_scoring=false`
- `ready_for_trading=false`

A 股卖空腿通常不可执行；分位 top-minus-bottom spread **只用于因子评价**，不是可交易多空组合。

## IC 字段语义

- `t_stat`：历史兼容的 naive IID t 统计量（样本标准差 / √n）。JSON 仍保留该字段，语义不变。
- `icir`：非年化 ICIR，即 `mean_spearman_ic / std_spearman_ic`。
- `hac_t_stat` / `hac_lag`：用确定性 Newey-West（Bartlett 核）长期方差，修正重叠前瞻收益带来的序列相关；报告的是 cap 到 `observations-1` 后的实际 lag。

## 分位组合字段语义

- 默认 `quantile_count=5`（CLI `--quantiles`，允许 2..10）。
- `spread_definition=highest_factor_quantile_return_minus_lowest_factor_quantile_return`。
- 每个 decision date / horizon / factor：用当日 as-of 因子与未来 adjusted-close return 标签，计算最高因子分位等权收益、最低因子分位等权收益与 spread。
- 未来收益只作研究标签，绝不流入 scorer。
- 缺失/非有限因子或未来价格由 `analyze_ic` 保持 unknown 并排除，绝不填 0；`quantile_day_observation` 若仍收到非有限 pair 则失败关闭。
- 单日观测 `QuantileDayObservation`：`names` 必须为正整数；三收益字段必须有限；`spread` 必须与 `highest_quantile_return - lowest_quantile_return` 在严格浮点容差内一致。
- 汇总入口按 `decision_day` 确定性排序后再算 HAC；重复 `decision_day` 失败关闭（显著性单位是日期，不可静默去重）。
- 汇总至少含 `scoring_days`、`average_names`、`minimum_names`；截面不足或最高/最低分位为空计入 `skipped_insufficient_cross_section`，不伪造收益。
- Tie：average-rank quantile，相同因子值不会被拆到两端制造 spread；全相等截面跳过。输出对输入顺序不敏感。
- Spread 时间序列：`mean_spread` / `std_spread`、兼容 naive `t_stat`、非年化 `spread_ir`、与 IC 相同规则的 `hac_t_stat` / `hac_lag`。
- JSON 可含 `quantile_summaries`、`annual_quantile_periods`、`rolling_quantile_periods`；不含无界逐股明细。既有 IC 字段保持兼容。

## 重叠 lag

- `all_trading_days`：目标 lag = `horizon_days - 1`
- `strategy_signal_schedule`（`--scheduled-only`）：目标 lag = `max(ceil(horizon_days / signal_interval_days) - 1, 0)`

IC 与分位 spread、年度与 rolling 汇总使用同一规则。显著性样本单位是 scoring day，不要把单日截面股票数当自由度。

## 不可误读之处

- 不要把某一天的截面 IC 或分位收益当作独立股票样本去堆自由度。
- HAC 只缓解重叠标签导致的时间序列相关低估标准误；它不把诊断变成可交易 alpha，也不替代含成本回测。
- `n < 2`、零方差、或长期方差非正/非有限时，相关显著性字段为 `null`，不会伪造 0。
- CLI 分别打印 `ic_table` 与 `quantile_spread_table`，勿混读。
