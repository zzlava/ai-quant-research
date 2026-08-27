# 固定换仓间隔全相位敏感性诊断（E3b）

`analyze-signal-phase-sensitivity` 对 `trade.signal_interval_days=N>1` 的策略，基于原 `signal_anchor_date` 生成 **全部** `phase_offset=0..N-1` 个锚点，并在同一 store、同一评价窗、同一只读 `score_fn` 上各跑一次现有 `BacktestEngine`。

本命令是**诊断器**，不是选型器：

- 禁止按收益/Sharpe 自动选择最佳相位；
- 禁止改策略权重、持有期、成本或其他非锚点字段；
- 不得把未来收益或 phase 结果反馈给评分；
- 固定 `diagnostic_only=true`、`parameter_selection_forbidden=true`、`selected_phase=null`、`ready_for_scoring=false`、`ready_for_trading=false`；
- 报告**不提供** `winner` / `best` 字段，相位只按 `phase_offset` 排序。

## 相位构造

对原锚点所在交易日序列 `D[0], D[1], …`：

| offset | 运行时锚点 | 计划信号日（示意） |
| --- | --- | --- |
| 0 | `D[0]`（原锚点） | `D[0], D[N], D[2N], …` |
| 1 | `D[1]` | `D[1], D[1+N], …` |
| … | … | … |
| N-1 | `D[N-1]` | `D[N-1], D[2N-1], …` |

每个候选 config **只能**改变 `trade.signal_anchor_date`；若出现其他字段差异则失败关闭。

日历不足以覆盖 `0..N-1`、原锚点不是交易日、`N<=1`、缺锚点、评价窗无可执行信号日（与 `BacktestEngine._window` 同义：至少两个交易日且存在 `next<=entry_end` 的信号截止日）、任一相位在评价窗内无计划信号日、任一 phase 的 `PositionUtilizationSummary.available=false`（含 legacy 缺 `open_positions`）时，**整份报告失败**，不静默跳过相位。

## 评价窗与计划信号日

`window` JSON 绑定：

- `start`：评价窗内首个交易日
- `end` / `valuation_end`：评价窗估值截止日（末个可 mark-to-market 交易日；二者同值）
- `signal_end`：可执行信号截止日，与 `BacktestEngine._window.signal_end` 完全同义——仅当该日有下一交易日且 `next<=entry_end` 时计入，通常为倒数第二个交易日；`valuation_end` 当日不可生成可成交信号

`planned_signal_dates`：从相位锚点按间隔生成、再截到 `evaluation_start<=day<=signal_end` 的日期列表。不得包含评价窗 `start` 之前的锚点日程，也不得把 `valuation_end` 当日不可成交信号列入。每个相位在评价窗内须至少有一个实际计划信号日，否则整份失败（避免全现金空路径被当成相位结果）。

## 每相位输出

- `phase_offset` / `signal_anchor_date` / `runtime_config_hash`
- `total_return` / `annualized_return` / `sharpe_ratio` / `max_drawdown`
- `trades` / `costs` / `orders_generated` / `orders_filled`
- 仓位利用相关字段（及完整 `utilization`）：平均/峰值开放仓位、投入与现金比例、零仓与未满仓日、`fill_rate`、`budget_utilization`
- `planned_signal_dates`：与引擎实际 `signal_fn` 调用日对齐的计划信号日（便于核对相位差）

## 汇总

仅给出全相位 `min` / `median` / `max` / `range`（`return`、可空的 `sharpe`、`drawdown`、`average_invested_fraction`、`trades`）。

明确声明：这 N 个相位共用同一窗口与同一评分函数，**不是 N 个独立 OOS 样本**（例如 N=20 时也不是 20 个独立样本外试验）。

报告绑定：`base_config_hash`、`data_snapshot_id`、`window`（含 `signal_end` / `valuation_end`）、`signal_interval_days`、`original_anchor`、`phase_count=N`。JSON 写入可重复。

## CLI

```bash
.venv/bin/python -m app.cli analyze-signal-phase-sensitivity \
  --strategy <strategy> \
  --start YYYY-MM-DD \
  --end YYYY-MM-DD \
  --output path/to/report.json
```

- 只读加载研究 store 并 `preflight`；
- 可用缓存 score provider（`fixed_horizon`），**不**调用现有 `score` / `backtest` CLI；
- 终端打印每相位进度（`phase_progress k/N anchor=…`）、warnings 与 report 路径；进度不影响报告内容。

命令实现本身是通用的，但**当前流程只能在开发窗使用**；首次对 2025+ 窗口运行需要新的显式授权，不得把本诊断结果当作 OOS 通过或交易许可。
