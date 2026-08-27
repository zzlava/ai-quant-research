# 第二层 PIT 财务负面清单判定器（E10b）

研究 / 实现地基 only。**不可交易**。本里程碑交付严格 PIT 证据组合器与攻击型单测；**不**从现有 fundamental overlay 物化资产负债表字段；**不**接策略评分、组合构建、订单、DB；**不**修改冻结 research JSON。

## 入口

| 项 | 路径 |
| --- | --- |
| 引擎 | `src/app/research/layer_two_financial_negative_list.py` → `evaluate_layer_two_financial_negative_list` |
| 校验 | `verify_layer_two_financial_negative_list_report` / `verify_layer_two_financial_negative_list_report_file` |
| 测试 | `tests/test_layer_two_financial_negative_list.py` |

## 为何只做证据组合器

现有 `fundamental_reports` 快照哈希列仅含质量/利润率/杠杆等已用于评分的字段（如 `roe`、`debt_to_assets`、`ocf_to_or` 等），**不含**下列预警规则所需的原始资产负债表明细（大额货币资金与有息负债对照、应收/存货相对营收连续两期异常、其他应收款/总资产、商誉/净资产等）。事件层虽有 `audit_opinion_events`，本里程碑也**不**自动从事件表推导 `non_standard_audit`。

因此 E10b **只**接受调用方显式提供的三态 PIT 证据并做封闭 registry 组合；**不宣称**已有全市场数据覆盖。后续增强层采集并物化证据后，方可批量喂入本判定器。

## 时点契约

- 决策发生在 **T 日收盘后**；`decision_at` 必须 **timezone-aware**；`as_of = decision_at.date()`。
- 每条证据必须含：`symbol`、`rule_code`、`hit_state`（`true`/`false`/`unknown`）、`observation_as_of`、`report_period`、`available_at`、`source`、`evidence_id`。
- `available_at ≤ decision_at`；`observation_as_of` / `report_period` 不得晚于 decision calendar date。
- 晚到、未来观测/报告期、同一 `rule_code` 重复、别名 symbol、非法枚举、NaN/inf 冒充 `hit_state` → **抛错**，不得静默挑选。
- 单次调用仅允许 **一只股票**、**一个 `decision_at`**；全部证据 `symbol` 必须等于裁决 `symbol`。

## 封闭规则 registry

| 规则 | 角色 |
| --- | --- |
| `non_standard_audit` | 单独硬排除：已知 `true` → `target_multiplier=0` |
| `large_cash_and_interest_bearing_debt` | 预警（计入命中数） |
| `receivables_inventory_growth_vs_revenue_two_periods` | 预警 |
| `other_receivables_to_assets_over_5pct` | 预警 |
| `goodwill_to_net_assets_over_30pct` | 预警 |

非法 `rule_code`（质押、解禁、ownership、事件候选、alpha 等）一律拒绝。`extra=forbid` 阻止注入 ownership / pledge / unlock / alpha 字段。

## 裁决语义（绑定 two-layer 合同）

合同路径与 `contract_id` 与 E10a 相同：

- `config/research/two-layer-strategy-decision-draft-v1.json`
- `id=27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6`

读取并校验 `layer_two.financial_negative_list` 冻结旗标与 `execution.decision_after_close_on_t`、候选短缺留现金语义。篡改磁盘合同字节 → 绑定失败。

每条证据仍须唯一、及时；裁决优先级如下（**已成立的硬排除不被其他 unknown/missing 掩盖**）：

1. 已知 `non_standard_audit=true` → `hard_excluded`，`target_multiplier=0`，`eligible_for_new_entry=false`，**即使**其他规则 missing/unknown；`unknown_codes` 原样保留；`known_warning_hit_count` 为当前已知预警 `true` 数。
2. 否则已知预警 `true` 数 `≥2` → 同上硬排除（`warning_hits_ge_2_exclude`），**即使**其他规则 missing/unknown；`unknown_codes` 保留；`known_warning_hit_count` 为已知命中数。
3. 否则只要还有 missing/unknown → `insufficient_evidence`，`target_multiplier=None`，`eligible_for_new_entry=false`，`known_warning_hit_count=None`。**不得**给 `clean` / `1.0` / `0.5`。unknown **绝不**计为未命中（miss）。
4. 全部规则均为已知 `true`/`false` 后，才允许：预警命中 `1 → 0.5`，`0 → 1.0`。

alpha 无法抵消排除（API 无 alpha 输入）。

## 输出

- `reason_codes`、`known_hit_codes`、`unknown_codes`、`input_evidence_hashes`、`two_layer_decision_contract_id`
- 内容寻址 `report_id`；校验器从语义输入与磁盘合同重算；篡改派生字段后即使重封外层 hash 也会因语义漂移被拒
- 固定 `ready_for_scoring=false`、`ready_for_portfolio_construction=false`、`ready_for_trading=false`
- 输入证据顺序不影响 `report_id`（内部按规则/证据 id 等稳定排序）

## 明确非目标

- 不修改冻结合同 / 策略 YAML / scoring / universe / portfolio / trading
- 不运行 score / IC / backtest
- 不从 fundamental / event overlay 自动计算四项预警或非标审计
- 不接入 `StrategyConfig` 或实盘路径
- E10b 完成后仍**不能**声称 layer-two 可交易或财务排雷数据已全市场覆盖

## E11b-2e 离线 verdict overlay（2026-08-27）

v3 财务采集封印通过后，已将冻结规则 A–E 离线物化为独立研究 overlay。该产物按
`decision_date` 分区，逐行绑定候选资格包、财务 collection ID 与规则证据哈希；不修改候选包，
不接入评分、股票池、组合或交易。

| 项 | 值 |
| --- | --- |
| 路径 | `data/all-a-share-historical-v1/research/financial-negative-list-verdict-overlay-v1` |
| overlay ID | `3bfefbce0f32d842a5b77904d01e65c30f4a8c7c72ce312837ae7a9a473daa0d` |
| 财务 collection ID | `f83789dfdb26367fa16e935b0a0348dceb90883e2e9d5db56ec0a120c123b6bc` |
| 覆盖 | `2022-01-04..2024-12-31` |
| 行数 / 分区数 | `3,597,408 / 726` |
| dataset hash | `383d455dcc71fc20efa5ece09ef5f398c88fefc289496d3f2d90f1fd6b10b238` |

只读复验：

```bash
PYTHONPATH=src .venv/bin/python -m app.cli \
  verify-financial-negative-list-verdict-overlay \
  --overlay-dir data/all-a-share-historical-v1/research/financial-negative-list-verdict-overlay-v1
```

覆盖审查的裁决计数为：`clean=199,281`、`halved=48,430`、
`hard_excluded=164,950`、`insufficient_evidence=3,184,747`。约 88.5% 的行因至少一个
必需规则仍为 unknown 而不可形成完整裁决；这反映原始字段覆盖不足，不能把 unknown 当作未命中，
也不能据此宣称财务负面清单已可用于评分或交易。所有 readiness 旗标继续固定为 false。
