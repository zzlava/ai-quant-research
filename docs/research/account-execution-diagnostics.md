# 账户 / 执行结构诊断（E7b）

研究用途 / **非收益证据** / **非交易就绪**。本里程碑把**账户与执行结构问题**从选股 alpha 中拆开：最低佣金、整手可负担性、显式成本与滑点、预算未用满、信号漏斗。只做只读归因，**不**改成本参数、成交规则或策略配置，**不**输出选股/通过失败的 alpha 裁决。

## 背景：毛利为正仍可能净亏

当前历史开发窗上的示例回测（本文**不**重读结果文件）可以出现：归因层 `gross_realized_pnl > 0`，但在买入/卖出佣金、印花税与滑点之后 `net_realized_pnl < 0`。这首先是**账户与成本结构**问题，不能直接解读为“选股因子无效”。本模块只诊断这些结构量，**不**替你改佣金、滑点或选出下一版策略。

## 小资金切片示意（非选定参数）

已确认 `initial_cash=80000`。若想象把资金均分为 20 个 tranche，则每片理论现金为 `80000 / 20 = 4000`。在 A 股 100 股整手与最低佣金下，4000 元切片很容易买不起一手或被最低佣金扭曲有效费率。

**这只是说明，不是选定参数**：`tranche_count`（N）仍待明日用户确认；禁止把 N=20 或每片 4000 写成默认实现。

## 回测结果诊断

入口：`src/app/research/account_execution_diagnostics.py` → `diagnose_account_execution`

显式参数（**禁止默认值**）：

| 参数 | 含义 |
| --- | --- |
| `result` | `BacktestResult`（内存对象；报告**不**内嵌完整结果以免膨胀） |
| `commission_rate` / `minimum_commission` | 用于核对成交佣金与最低佣金绑定 |
| `slippage_bps` | 用于核对成交价相对 raw 价的滑点 |
| `lot_size` | 正整数整手（如 100） |
| `numerical_tolerance` | 归因与恒等核对容差 |

失败关闭要点：

- 经济输入须有限且非负；`lot_size` 为正整数；
- `metrics.number_of_trades == len(trades)`；
- 每笔成交：正有限价格、正股数且整手整除、成本分量有限非负；
- 从成交独立重算并与 attribution 的佣金/印花税/滑点/显式成本/总成本/毛利/净利在容差内一致；
- 当 `gross_pnl` 与 raw 价齐全时，强制 `gross - 全部交易成本 = net`；legacy 缺字段**拒绝猜测**（不把缺省 0/`None` 当成已验证）；
- 零成交或数学上无定义的比率（如 cost/gross 在 `gross_realized_pnl <= 0`）→ `null` + `unavailable_reason`。

报告绑定：`strategy_*` / `strategy_config_hash`、`data_snapshot_id`、结果日期与 window、`source_result_hash`（结果的规范内容哈希）。固定门闩：`diagnostic_only=true`，`ready_for_scoring/backtest/trading=false`，`auto_apply=false`。

文件校验器名为 **integrity-only**（`verify_account_execution_diagnostic_report_integrity_only` / CLI `verify-account-execution-diagnostic-report-integrity`）：只核自哈希与密封字段一致性，**不能**代替对源 `BacktestResult` 的完整经济校验。

## 候选整手可负担性（合成 / 无行情）

入口：`diagnose_candidate_lot_affordability`

显式接受 `(symbol, raw_price)` 行、`cash_per_slice`、佣金率/最低佣金、`slippage_bps`、`lot_size`。符号唯一、价格有限为正；复用 `app.backtest.costs` 的买入语义（印花税与买入无关）。输出每标的可买股数/手数与未用现金；不可用信息**永不**用 0 冒充。报告内嵌输入，文件校验必须重算。

## 只读 CLI

必须显式给出本地文件；不静默打开行情或现有 OOS 结果：

```bash
.venv/bin/python -m app.cli verify-account-execution-diagnostic-report-integrity \
  --report-file /path/to/local-account-execution-report.json

.venv/bin/python -m app.cli verify-candidate-lot-affordability-report \
  --report-file /path/to/local-candidate-lot-report.json
```

## 本里程碑不做的事

- 不连接券商、不读 token、不跑真实/2025+ 预检/评分/IC/相位/回测；
- 不分析仓库内已有 OOS 结果文件；
- 不选定 N/H、不改成本政策、不自动应用组合约束。
