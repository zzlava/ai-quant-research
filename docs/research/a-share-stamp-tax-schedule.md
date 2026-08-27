# A-share Stamp-Tax Factual Schedule（E10f-0）

离线、已封印的 A 股证券交易印花税**事实成本合同**，覆盖声明研究窗口 `2022-01-01..2024-12-31`。不接评分 / IC / 回测 / 交易，不修改既有 tranche evaluation protocol JSON，也不宣称其 stamp-tax / cash-occupancy blocker 已解除。

## 模块

| 项 | 路径 |
| --- | --- |
| 封印合同 | `config/research/a-share-stamp-tax-schedule-v1.json` |
| 引擎 | `src/app/research/a_share_stamp_tax_schedule.py` |
| 测试 | `tests/test_a_share_stamp_tax_schedule.py` |

## 费率带（法律事实）

| 区间 | seller_rate | buyer_rate |
| --- | --- | --- |
| `2008-09-19`..`2023-08-27`（含） | `0.001` | `0.0` |
| `2023-08-28`..open-ended（官方未公布终止日） | `0.0005` | `0.0` |

`2022-07-01`《印花税法》生效是**再确认证据**，不是新费率带。

**法律 open-ended ≠ 可外推。** 合同另有操作核验截止日 `verified_through=2026-08-26`（与 `confirmation_as_of` 同值）。`stamp_tax_rate_for` / `stamp_tax_amount` 对 `trade_date > verified_through` **失败关闭**（例如 `2026-08-27`、`2035-01-01`）。`declared_window` 必须完全落在 `schedule_coverage_start..verified_through` 内。

计税基础：成交金额；卖方征收、买方为零。`2008-09-19` 之前不做任何主张；证据之外不猜测。

## 证据边界（离线复核）

四条官方来源的 `accessed_at` 统一为离线契约证据复核时间 **`2026-08-26T06:54:52Z`**（不是实时抓取时间），且结构验证强制四项时间精确一致、等于该封印值、不晚于 `confirmation_as_of` 次日 UTC 零点。

1. 财政部新闻（2008-09-19 起单边征收）：http://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/200809/t20080919_76432.htm
2. 《中华人民共和国印花税法》（2022-07-01 生效，再确认转让方纳税 / 表列 0.001）：https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193058/content.html
3. 财政部/税务总局公告 2023 年第 39 号（2023-08-28 起减半）：https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html
4. 可选有效性镜像（全文有效）：https://shanghai.chinatax.gov.cn/zcfw/zcfgk/yhs/202308/t468451.html

本里程碑**不访问网络**；运行时只读本地封印 JSON。结构 verifier 除 hash 外还封印四个 `source_id` / URL / `document_identifier` / `evidence_role` / 日期与来源数量。

## API

- `stamp_tax_rate_for(date, side, contract=…)` / `stamp_tax_amount(…)`：使用任何合同（含默认 factory）前必须完整 `verify_a_share_stamp_tax_schedule`（canonical/factory 比对），**不能**仅靠自哈希；覆盖前 / `> verified_through` / 非法 side / 伪造 reseal **失败关闭**
- 覆盖内买侧税额精确为 `0`；`verify` 不调用 rate helper（无递归）
- 结构 verifier：`disk_binding_ok=false`，`ready_for_exit_diagnostic=false`
- 文件 verifier（固定默认路径 + `EXPECTED_CURRENT_CONTRACT_ID`）：完整通过后**新构造** ready 结果；不信任调用者布尔
- 合同本体**没有**可变的 `ready_for_exit_diagnostic` 字段
- `factual_cost_contract_only=true`；`ready_for_scoring/backtest/trading/orders/auto_apply=false`
- `existing_tranche_protocol_blocker_not_modified=true`
