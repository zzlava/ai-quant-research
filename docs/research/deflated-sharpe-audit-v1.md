# Deflated Sharpe 审计 v1

仓库现在实现并测试了统一日频的 Bailey–López de Prado 正态最大 Sharpe 近似公式，
但当前真实项目仍严格输出 `not_evaluable`。

已经绑定的真实输入包括：第一层恢复反事实 2013–2021 的日收益样本、观测日频及年化
Sharpe、样本数、偏度、Pearson 峰度，以及研究试验台账中的试验数下界。

仍缺两项不可猜测的输入：

1. 可比较 trial 的日频 Sharpe 横截面标准差；
2. 考虑试验相关性后的有效独立试验数。

当前台账本身标记 `complete=false`，且历史 trial 来自不同数据合同、窗口和端点，不能
把 trial 总数直接假装成独立试验数，也不能从零散文档拼出 Sharpe 离散度。因此本次
完成的是“公式 + 数值来源绑定 + 明确缺口”，而不是伪造一个 DSR p 值。

```bash
PYTHONPATH=src .venv/bin/python -m app.cli audit-deflated-sharpe --repo-root .
PYTHONPATH=src .venv/bin/python -m app.cli verify-deflated-sharpe-audit --repo-root .
```

该审计不使用 OOS，不允许评分、回测或交易。
