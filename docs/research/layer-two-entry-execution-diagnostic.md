# Layer-two T+1 Entry Execution Diagnostic（E10e-0）

研究 / 只读**仿真标签** only。对一份已封印的 E10d-3 stateful allocator 结果，结合显式 T+1 开盘观察，产出至多一条买入执行诊断。**不得**回灌排名 / 评分，**不得**发单，**不得**声称真实成交。

## 明确不做

- 不接 ScoringEngine / StrategyConfig / BacktestEngine / API / CLI / DB
- 不用未来 close/high/low，不在开盘尝试后推断补成，不复用其他交易日
- `diagnostic_only=true`；`post_decision_execution_label_only=true`；`must_not_feed_ranking_or_scoring=true`
- `ready_for_scoring/backtest/orders/trading/auto_apply` 全部 `false`

## 模块

| 项 | 路径 |
| --- | --- |
| 引擎 | `src/app/research/layer_two_entry_execution_diagnostic.py` |
| 测试 | `tests/test_layer_two_entry_execution_diagnostic.py` |

## 上游绑定

| 上游 | 用途 |
| --- | --- |
| E10d-3 allocator | `proposed_entry` / cash retention；结构路径完整重算 |
| E10d-2 / E10d-1 | 经 E10d-3 **file** verifier（必填落盘 `phase_report_path`） |
| Tranche evaluation protocol v2 | 成本语义磁盘绑定：`protocol_id=8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a` |

成本常量（与磁盘协议一致，禁止漂移复制）：

- 佣金率 `0.00025` / 边；最低佣金 `5` CNY
- 基础滑点 `5` bps；压力滑点 `15` bps
- 整手 `100`（与 `app.backtest.costs.LOT_SIZE` 及 A 股整手合同一致）
- **买入不计印花税**；显式标注 `stamp_tax_irrelevant_for_buy_entry`，不臆造卖出税率表

## T+1 与观察

- `expected_t1_execution_date` = `phase_report.market_calendar` 中 `as_of` 的**下一个**市场交易日（日历须严格递增、无重复；无下一交易日 → 失败关闭）
- 有 `proposed_entry` 时必须提供匹配的 `LayerTwoEntryExecutionObservation`（symbol / execution_date / 同一 market snapshot）
- 观察状态：`unknown` | `known_full_day_suspension` | `tradable`
  - unknown / 停牌：`raw_open` 与 `published_up_limit` 必须为 null（不得用 0 冒充）
  - tradable：二者须为正有限非 bool；若 `raw_open >= published_up_limit` → `blocked_limit_up`

## 结果

| outcome | 含义 |
| --- | --- |
| `not_attempted` | 无 proposed_entry；原样携带 allocator `portfolio_cash_retention_reason`；无股数/价格/费用 |
| `unknown_execution_observation` | 观察未知；不编造拒绝/成交 |
| `blocked_suspension` | 全日停牌 |
| `blocked_limit_up` | 开盘价触达/高于涨停价 |
| `unaffordable_board_lot_or_minimum_commission` | 可交易但目标名义买不起一手（含佣金） |
| `hypothetically_fillable` | 基础 5bps 情景可买至少一手；**仅为假设**，非 fill |

可交易时先 `apply_slippage(raw_open)`，再将假设买价**封顶**到 `published_up_limit`（`legal_limit_cap_applied` 标明是否触发）。随后用封顶后的价格调用 `shares_affordable` / `buy_cost`（零附加滑点配置），以 `target_notional` 为 all-in 现金上限，输出 base(5bps) 与 stress(15bps) 情景行。**假设成交价永不超过 published_up_limit**。主 outcome 看 base；stress 仅诊断。不抬高 target、不超预算。

`raw_open >= published_up_limit` 仍走 `blocked_limit_up`，不进入情景构造。

unknown / 停牌 / 涨停 / not_attempted：**不得**填充数量/价格/费用字段。买不起时可填情景行（可含 0 股与价格算术），仍无订单。

## Verifier

- **结构**：self-hash + E10d-3 结构 verifier（完整重算）+ 本诊断完整重算；四项 binding（allocator / phase / protocol / `execution_observation_binding_ok`）全 false
- **文件**：结构路径 + 真实 E10d-3 file verifier + 磁盘 verify tranche evaluation protocol 并核对 cost 字段与 `protocol_id`；再对 `MarketStore` **逐行**绑定 execution observation（`snapshot_id` 一致；`not_attempted` 允许无 observation；attempted 按 execution_date/symbol 精确日线；unknown / 停牌 / tradable 规则 fail-closed；tradable 比对 **raw `open` / `up_limit`**，禁止 `adj_open`）→ **新构造** 四 binding 全 true
- 状态机：`structural_ok=false` 禁止任何 binding；禁止部分 binding；结构形四 false；文件形四 true
- 结构路径可接受显式 observation label 做纯重算；**仅**逐行 MarketStore 绑定后的 file result 才视为 observation-ready（上游 E10f-1 / E10e-1 必须显式要求 `execution_observation_binding_ok=true`）

法律买价边界（与 E10f-2 一致）：`raw_open >= published_up_limit` 严格 blocked；仅 `raw < up` 可进情景；`slipped > up` 严格 cap；`fill <= up` 严格；金额 tol 不得决定法律价格顺序。
