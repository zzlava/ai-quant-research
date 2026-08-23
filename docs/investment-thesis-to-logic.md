# 把文字见解转成可检验的选股逻辑

文字见解本身不能直接回测。先把它写成一个可被证伪的研究假设，再实现为数据、规则和风险约束。不要把事后解释写成入场条件。

## 提交模板

```yaml
thesis_id: short_descriptive_name_v1
claim: |
  用一句话写因果主张，而不是预测。例如：在市场趋势向上时，
  相对强度持续且成交没有异常放大的股票，未来持有期表现更稳健。
universe:
  name: controlled_sample_anchor_intersection30_v1
  known_biases:
    - survivorship_bias
signal:
  observable_inputs:
    - stock_relative_strength_20d
    - ma20_distance
    - volume_ratio_5d
  lookback_trading_days: 20
  entry_rule: "请用明确不等式，例如 relative_strength > 0 and ma20_distance > 0"
  ranking_rule: "按哪个数值降序；并列时如何处理"
exit_rule: "止损、止盈、最长持有期、或信号反转"
risk_controls:
  market_regime: "何时不新开仓"
  liquidity: "最低 20 日平均成交额"
  max_positions: 3
falsification:
  primary_metric: "样本外区间的净值、最大回撤、换手率"
  failure_rule: "例如两个独立区间都落后基准且回撤更大时废弃"
anti_lookahead:
  - "只用决策时点已经可得的数据"
  - "参数冻结后再看测试区间"
```

## 从文本到代码的映射

| 文字中的意思 | 需要明确成 | 当前系统已有输入 |
| --- | --- | --- |
| “强势股会延续” | 相对强度窗口、阈值、持有期 | `stock_relative_strength`、`ret_20d` |
| “站上均线才买” | 均线周期与严格比较符 | `ma20_distance`、`ma60_distance` |
| “放量但不追高” | 成交量比率上下界、拥挤惩罚 | `volume_ratio_5d`、`crowding_risk` |
| “市场不好就少做” | 市场分段与最大新增仓 | `market_gate`、`market_score` |
| “流动性不足不做” | 20 日平均成交额门槛 | `min_avg_turnover_20d` |

当前 YAML 可以调整组合权重、市场闸门、成本、持仓数与交易约束；`baseline_v1` 内部的 alpha 公式仍是代码固定的。若你的见解需要新变量、阈值或不同的排序公式，应新建一个注册策略并为它写单元测试，而不是把自然语言解释事后附在结果上。
