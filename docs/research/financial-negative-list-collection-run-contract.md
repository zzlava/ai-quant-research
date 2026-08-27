# Financial Negative List Collection Run Contract (E11b-2c)

默认运行契约为 `config/research/financial-negative-list-collection-run-contract-v3.json`，用于
离线锁定一次历史财务负面清单原始采集任务的绑定输入，不执行网络采集。
历史 v1/v2 契约和 v2 半成品 staging 均保留且可独立校验，不会被覆盖或重写。

## 关键约束

- `status=prepared_not_authorized`，`network_authorized=false`，`requires_fresh_user_authorization=true`。
- 该 run contract **不是授权文件**，不能单独放行联网。
- v3 固定 `prepared_at=2026-08-27`，authorization 的日期必须不早于该值且不晚于当前 Asia/Shanghai 日期。
- 绑定 E11b-2a 协议文件路径、`protocol_id`、文件 SHA-256。
- 绑定公告窗口 `2020-01-01..2024-12-31`。
- 绑定四个 endpoint 的 API 名称、官方文档和字段列表：
  - `balancesheet`
  - `income`
  - `fina_indicator`
  - `fina_audit`
- 绑定 `stock_basic` 来源路径、candidate pack id/sha；v3 使用全新的固定 staging 目录
  `data/raw/a-share-financial-negative-list-20200101-20241231-v3`。
- v3 绑定 response-boundary policy v2（路径、policy_id、文件 SHA、原因码 `FNLD-013`），
  允许将报告期查询返回的未来版本转为不含未来 payload 的密封 receipt。
- 绑定 canonical symbols 统计（当前 5544）及其 SHA-256，以及分区总数（当前 22176）。
- `ready_for_scoring/backtest/trading` 固定为 `false`。

## 为什么需要单独 authorization

- run contract 只描述“允许被授权时应采什么、怎么验”。
- 真正允许网络采集必须提供独立 authorization JSON，并与该 run contract 完整匹配；
  v1/v2 authorization 不能用于 v3 run contract。
- CLI 和启动脚本都会在读取 Keychain token 之前先做离线校验，任一漂移立即 fail-closed。

## 明确边界

- 仅用于 historical data collection。
- `collected_at` 是采集时间元数据，**不是** `available_at`。
- 不涉及 score、IC、backtest、交易授权或下单行为。
