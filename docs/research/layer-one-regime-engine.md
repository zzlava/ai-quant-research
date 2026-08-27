# 第一层指数 Regime / 风险预算状态机（E9a）

研究 / 实现地基 only。**不可交易**。本里程碑交付纯函数状态机与严格单测；**不**接策略评分、股票池、订单、DB 写入；**不**加载行情；**不**声称 layer-one 可交易。

## 入口

| 项 | 路径 |
| --- | --- |
| 引擎 | `src/app/research/layer_one_regime.py` → `evaluate_layer_one_regime` |
| 校验 | `verify_layer_one_regime_decision_file`（本地 sealed JSON + 内嵌 feature report 绑定 + 上游读盘绑定 + **离线重算全部派生逻辑**） |
| CLI | `verify-layer-one-regime-decision` |
| 测试 | `tests/test_layer_one_regime.py`（含攻击回归） |

## 时点契约

- 为目标交易日 **D** 计算预算。
- 全部市场 / 账户输入 `as_of = P`，`P` 为严格小于 `D` 的最近市场交易日。
- 完整市场日历必须证明：`P` 与 `D` 在日历中相邻；以及 `D` 是否为当周（ISO week）第一个**市场**交易日。
- **禁止**「周一=周首」假设；节假日周首日可为周二等。
- `evaluated_at` / unlock `requested_at` 必须为 **timezone-aware**；`requested_at ≤ evaluated_at`，且请求日 `≥ risk_lock_triggered_as_of`、`≤ D`。
- `risk_lock_triggered_as_of` 必须出现在 `market_calendar`（周末/非交易日失败关闭）。

## 冻结映射（端点按有序比较；账户回撤用 Decimal）

1. **趋势（SMA200，`close/SMA`）**
   `>1.03 → 0.9`；`0.97≤ratio≤1.03 → 0.6`；`<0.97 → 0.3`
2. **60 日年化波动（×√242）**
   `≤0.18 → cap 0.9`；`(0.18,0.27] → 0.6`；`(0.27,0.36] → 0.3`；`>0.36 → 0`
3. **指数相对 242 日峰值回撤**
   `≤-0.20 → 0`；`elif ≤-0.15 → 0.3`；`elif ≤-0.10 → 0.6`；`else → 0.9`
4. **账户回撤**
   `Decimal(str(current))/Decimal(str(peak))-1`；`current>peak` 失败关闭；禁止 float round 模糊端点。
   `≤-0.18 → risk lock + 预算 0`；`≤-0.15 且 >-0.18 → 0.3`；`≤-0.10 且 >-0.15 → 0.6`；`>-0.10 → 0.9`
   `≤-0.20` 另标 `red_line_breached=true`（预算仍为 0）
5. **原始目标**
   `min(趋势基准, 波动cap, 指数回撤cap, 账户回撤cap, 人工开放上限)`
   人工上限显式输入，仅 `0/0.3/0.6/0.9`；系统不得自动提高；须绑定 `manual_ceiling_authorization_id`（64 hex）
6. **周频调整**
   降：任意 `D`；升：仅当周首个市场交易日；相等保持。
   **当前观测仍触发 risk lock 时 trigger 优先**，绝不可被 unlock 或周频升仓覆盖
7. **Lock 持久**
   触发后即使指标恢复仍保持预算 0；须显式 **已封印** `prior_state`（`state_id` 必填且自哈希匹配；缺/陈旧失败）；本模块只输出已封印 `new_state`，不写 DB；重启不得用默认 unlocked 清锁
8. **解锁**
   须显式请求，且同时：锁定后 ≥20 个市场交易日；趋势非 negative（`ratio≥0.97`）；`vol<0.27`；用户确认；非空 `operator/reason/request_id`；aware `requested_at` 时序合法。任一失败仍锁定并列出拒绝原因。解锁审计字段与 `unlock_request_evidence_id` 写入 report。解锁后仍受人工上限与周频升仓限制

## 封印 / 证据绑定

- `index_risk_feature_report`：决策内嵌完整已封印 `IndexRiskFeatureReport`；`index_risk_feature_report_id` 必须等于其 `report_id`
- Verifier 校验：feature 自哈希 / 日期窗结构 / lookback / gate flags；`data_snapshot_id`、`index_symbol_input`、顶部三个 feature scalars 与 embedded report 相等；三类 window ⊆ `market_calendar` 且 `as_of=P`；**全部 caps 仅从已验证 embedded 值重算**（禁止只改顶部 scalars 后与派生字段一致重封绕过）
- `market_calendar_id`：完整日期序列 canonical SHA-256；feature 三类 window 日期必须 ⊆ 日历
- `account_equity_evidence_id` / `manual_ceiling_authorization_id`：必填 64 位小写 hex
- `prior_state_id` / `new_state.state_id` / `decision_id` /（若有）`unlock_request_evidence_id`：内容自哈希
- Verifier：自哈希 + 上游 disk 绑定 + **离线重算**账户回撤、全部 caps、raw min、weekly applied、lock/trigger/unlock/redline、`new_state`；逻辑篡改后重封印仍拒绝

## 上游绑定（读盘 / 自哈希 / ID）

不得修改上游文件：

- `config/research/two-layer-strategy-decision-draft-v1.json`
  `id=27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6`
- `config/research/layer-one-index-development-protocol-draft-v1.json`
  `id=b7aa9de1539cdd791aee5b74ca8ec3f269b6ed809a070caa917686742c4b1b2f`
- `config/research/layer-one-index-data-evidence-v1.json`
  `id=6d7cdbb7ba25191f9d4718ec94b61acf6a18e0ca4ffa6a0984c1abbdc6e42e77`

漂移 → 失败关闭。

## 与 `index_risk_features` 的边界

E9a **消费并内嵌**已封印的 `IndexRiskFeatureReport`（lookback 必须严格 200/60/242，年化 242，`as_of=P`，自哈希正确）。
**不**修改其特征诊断语义为交易就绪。输出固定：

- `exact_symbol_identity_verified=true`，且必须绑定已完整重算的 `000985.CSI` 快照证据；symbol / snapshot 任一漂移即失败
- `snapshot_full_raw_recomputation_verified=true`
- `ready_for_historical_evaluation=true`（仅第一层历史评价，不授权个股评分或交易）
- `ready_for_trading=false` / `ready_for_orders=false` / `does_not_trade=true`
- `research_only=true` / `implementation_only=true`

输入 `index_symbol` 仅作非空证据字符串，**不**认定官方身份正确。

## 明确非目标

- 不跑 preflight / score / IC / phase / backtest
- 不接股票池 / 订单 / 实盘
- 不抓取行情、不读 Token
- E9a 完成后仍**不能**声称 layer-one 可交易
