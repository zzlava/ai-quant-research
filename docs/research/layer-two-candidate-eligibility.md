# 第二层 PIT 候选资格（E10a）

研究 / 实现地基 only。**不可交易**。本里程碑交付纯函数 PIT 候选资格评估与严格单测；**不**接策略评分、组合构建、订单、DB；**不**加载行情；**不**修改冻结 research JSON。

## 入口

| 项 | 路径 |
| --- | --- |
| 引擎 | `src/app/research/layer_two_candidate_eligibility.py` → `evaluate_layer_two_candidate_eligibility` |
| 校验 | `verify_layer_two_candidate_eligibility_report` / `verify_layer_two_candidate_eligibility_report_file` |
| 测试 | `tests/test_layer_two_candidate_eligibility.py` |

## 时点契约

- 决策发生在 **T 日收盘后**；`decision_at` 必须为 **timezone-aware**，且 **`decision_at.date() == as_of`**。
- 全部来源 `available_at`（流动性槽、证券状态、PIT 流通市值）必须 `≤ decision_at`；naive 时间戳 **拒绝**（`ValidationError` / `ValueError`）。
- `as_of = T`；20 槽流动性窗口 **最后一槽 `observation_date` 必须恰好等于 `as_of`**（T-1 窗口复用 **拒绝**）。
- 禁止未来观测日、重复/乱序观测日、跨 as_of 来源、重复 symbol。
- **缺失 vs 无效**：仅 `None`/缺失关键值 → `unknown_critical_input`；畸形/损坏输入（晚于 `decision_at` 的 `available_at`、NaN/inf/负值、已知停牌非零量、21 槽、后缀/来源不一致等）→ **`ValueError`/`ValidationError`**，不得吞并为 unknown。
- 流动性校验 **先扫全部 20 槽** 再判定 unknown，避免早退掩盖后续 late/corrupt 槽。
- `data_snapshot_id` 非空；绑定磁盘 two-layer 合约（自哈希 + 固定 `contract_id`）。

## 来源元数据（provenance）

| 字段组 | 必填规则 |
| --- | --- |
| 证券状态 | `security_status_as_of` + `security_status_available_at` 覆盖 `market` / 普通 A / BSE / ST / 停牌 / 上市天数；半对半 → **拒绝** |
| PIT 流通市值 | `pit_free_float_market_cap_cny` + `pit_free_float_market_cap_as_of` + `pit_free_float_market_cap_available_at` 同进同退；半对半 → **拒绝** |

已知值时：`security_status_as_of` / `pit_free_float_market_cap_as_of` **必须等于** report `as_of`；对应 `available_at ≤ decision_at`。缺失元数据但字段已知 → `unknown_critical_input`，且 **全部** 证券派生 pass 标志（`market_scope_pass`、`st_delist_pass`、`tradability_pass`、`listing_history_pass`）保持 `null`，**不得**将未验证原始值显示为 passed；元数据齐全时方可计算各 gate。元数据齐全但单项 status 为 `None` → 仅对应 pass 为 `null` + unknown，其余已验证字段可正常计算。

## 符号后缀

- **拒绝式规范化**：`symbol` 必须 **精确** 匹配 `^[0-9]{6}\.(SH|SZ)$`（六位数字 + 大写后缀）；拒绝小写、空白、非六位、其他后缀（含 `.BJ`）。**不**静默 trim/upper。
- 通过 canonical 校验后：`SSE` ↔ `.SH`，`SZSE` ↔ `.SZ`；阻止 alias 重复（如 `000001.sz` 与 `000001.SZ`）。

## 冻结阈值（来自 `two-layer-strategy-decision-draft-v1.json` v2，经 `bind_two_layer_eligibility_policy` 读取）

### 证券范围

- 仅普通 A 股；`market` 显式 `SSE` 或 `SZSE`；`is_bse=true` 禁止。
- ST / 退市风险禁止新开；决策日停牌禁止买入。
- 上市市场交易日数 **≥ 180**（179 不合格）。
- 关键字段缺失 → `unknown_critical_input`，对应 pass 标志为 `null`，**不得**当作 false/0。

### 流动性（恰好 20 个市场日槽，窗口 **结束于 `as_of`（最后一槽 = T）**）

- 日期严格递增、无重复；每槽显式 `tradable` 或 `known_full_day_suspension`。
- **19 槽**：`unknown_critical_input` + `liquidity_observation_structure_fail`；**21 槽**：结构无效 → **拒绝**。
- `tradability=None` / `amount=None`（其余槽合法、amount 非 `None` 时已先校验有限非负）→ unknown only；`amount_cny` 非 `None` 但 NaN/inf/负值 → **拒绝**（含 `tradability=None`），且须扫完全部槽位以免早退掩盖后续 corrupt 槽。
- 已知停牌日 `amount_cny=0`；可交易日成交额有限且 `≥ 0`。
- 可交易日数 **≥ 15**（14 不合格）。
- 20 槽已知成交额中位数 **≥ CNY 50,000,000**；停牌日计 0。
- 容量：`planned_buy_notional_cny ≤ 0.1% × 20 槽平均成交额`。
- `planned_buy_notional_cny` 为显式正有限输入；引擎**不**静默改单或回填。

### PIT 自由流通市值

- 三组 provenance 齐全；缺任一 → unknown。
- **< 3bn CNY** 硬排除；`3bn` 含边界合格（乘数 0.5）。
- `[3bn, 5bn) → 0.5`；`[5bn, 10bn) → 0.75`；`≥ 10bn → 1.0`。
- `adjusted_planned_notional_cny` 仅诊断；**不**再分配缩减资本。
- `ownership_role=diagnostic_not_used`；**不接受** ownership 输入字段。

## 输出

- 每 symbol 有序 `reason_codes`；全部硬门通过时仅 `eligible_for_new_entry`。
- `requested_symbols` 与 `candidate_inputs` / `evaluations` **一一对应、同序、同 symbol**。
- `report_id` 自哈希；校验器从封印输入 **离线重算**，篡改后重封仍拒绝；磁盘合约 tamper 与 report binding tamper 均拒绝。

## 上游绑定

- `config/research/two-layer-strategy-decision-draft-v1.json`
  `id=27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6`

## 明确非目标

- 不修改 `rolling_tranche_schedule`
- 不跑 score / IC / backtest / 实盘
- 不接入 `StrategyConfig`、scoring、portfolio、trading
- E10a 完成后仍**不能**声称 layer-two 可交易或可组合
