# 回测持仓期执行语义（E4）

研究用途 / **非交易就绪**。本文固定 `BacktestEngine` 在持仓期对停牌、跌停、缺失行情的失败关闭契约；不构成实盘可执行性声明。

## 停牌：强制持有

- 持仓日 `is_suspended=True` 且 bar 完整时：**不生成卖出成交**，继续持有。
- 当日市值按该 bar 的 `close` 估值（`close` 须为有限正数）。
- `SignalAttribution.exit_blocked_suspended_days` 仅在「已过 `min_holding_days` 且当日 `_exit_decision` 非 None（若可交易本会退出）」时累加（持仓×日）；**不是**所有停牌持仓日。
- **持有日计数（`exit_eligible_days` / `holding_days`）维持现状**：停牌日仍会递增；本里程碑不擅自改为“停牌不计持有日”。

## 跌停：退出顺延

- 开盘触及跌停（现有 `is_open_at_limit(..., "down")`）时：**阻断卖出**，顺延到后续可成交日。
- 不放松既有限价判断（公布 `down_limit` 优先；否则一字板回退）。
- `exit_blocked_limit_down_days` 仅在「已过 `min_holding_days` 且当日 `_exit_decision` 非 None」时累加（持仓×日）；**不是**所有开盘跌停持仓日。

## 持仓缺 bar：失败关闭

- 已持有股票在某个**市场交易日**缺少 daily bar：回测立即抛出含 `symbol` 与 `day` 的 `ValueError`。
- `_manage_exits` 不得静默 `continue`；`_mark_to_market` 不得回退 `entry_price`。
- 非持仓股票缺 bar 不报错（仅影响未持有标的）。
- `_mark_to_market` 对每个持仓校验 `close`：缺失 / `NaN` / `inf` / `<=0` 均失败关闭，错误含 `symbol`/`day`。

## 退市价值：不猜测

- **不**凭空假设退市回收价、**不**自动现金结算、**不**强平。
- 运行时若无法区分“退市后正常无 bar”与其他数据缺口，一律按**持仓缺失行情失败关闭**处理。
- 这是有意限制：需要未来单独授权与数据契约，才能模拟退市处置路径。

## 诊断字段（向后兼容）

| 字段 | 默认 | 语义 |
| --- | --- | --- |
| `exit_blocked_suspended_days` | `0` | 本会退出但因停牌不可交易而被阻断的持仓×日 |
| `exit_blocked_limit_down_days` | `0` | 本会退出但因开盘跌停不可卖出而被阻断的持仓×日 |

旧 JSON 缺字段仍可解析为 `0`；显式负数在模型层拒绝。CLI `backtest` 诊断区输出上述字段。
