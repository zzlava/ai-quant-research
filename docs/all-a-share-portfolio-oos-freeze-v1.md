# 全 A 股价值组合 p10_h20 OOS 冻结协议（development-only）

本协议在已封存的开发证据上冻结**第一次未来 2025+ 一次性 OOS** 评估规则。它不是授权、不是评估运行、也不是评分/交易许可。

机器可读合同：[`config/research/all-a-share-portfolio-oos-freeze-v1.json`](../config/research/all-a-share-portfolio-oos-freeze-v1.json)

## 冻结对象

| 字段 | 值 |
| --- | --- |
| 策略 | `config/strategies/all_a_share_historical_value_portfolio_selected_v2.yaml` |
| `config_id` | `all_a_share_historical_value_quality_v1_portfolio_p10_h20_selected_v2` |
| `config_hash` | `796b793856dcd02a` |
| 策略文件 SHA-256 | `ba39935d0329f7c2354990f3875e55945a0c348f4eaa829f4e0fe6ae50597e26` |
| `candidate_id` | `p10_h20` |
| 资金 / 仓位 | `initial_cash=80000`，`max_positions=10`，`equal_weight` |
| 交易 | `fixed_horizon=20`，`signal_interval_days=20`，原锚点 `2022-01-04` |
| `market_gate` | `max_new_positions=[0,3,7,10]` |

选型报告：`data/all-a-share-historical-v1/portfolio-construction-v2.json`（SHA `9442a71b…6621`），`selected_config_hash` 必须与策略哈希一致。

稳健性报告：`data/all-a-share-historical-v1/frozen-portfolio-robustness-v2.json`（SHA `c0fa17a2…6512`），`status=CONDITIONAL_GO`，`data_snapshot_id=cf3a6e5b…9a11`。

开发行情 / 财务 snapshot：`de546fbb…6857` / `6a3406cb…25d3`。

## OOS 数据绑定（仅 manifest + 交易日历）

| 绑定 | 路径 | snapshot | coverage |
| --- | --- | --- | --- |
| OOS 行情 | `data/all-a-share-oos-20241001-20260821-v1/parquet` | `b6f664d3…0427` | `2024-10-08..2026-08-21` |
| OOS 财务 | `…/fundamentals-value-quality-v1` | `6ae37b22…4bd9` | 同上；`base_market` 必须等于 OOS 行情 |

校验只读四个 `manifest.json` 与两张 `calendar.parquet`，**不读** daily/index/global bars、财务 parquet，也不计算收益或交易。

## 日历等价证明

OOS 快照不含原锚点 `2022-01-04`。协议要求：

1. 开发与 OOS 日历在 `2024-10-08..2024-12-31` 完全相同，且共 **61** 个交易日；
2. 合并两张已校验日历后，从原锚点每 20 个交易日取一次信号；
3. OOS 首个等价锚点 = `2024-10-29`；首个 2025+ 信号 = `2025-01-22`；最后完整信号 = `2026-07-22`；+20 交易日退出 = `2026-08-19`。

合同记录 `runtime_equivalent_anchor=2024-10-29`，**仅允许等价排期**；其余策略配置逐项不得变化。任一日历或日期不符即失败关闭。

## 评估窗口与主终点

- 窗口：`evaluation_start=2025-01-02`，`evaluation_end=2026-08-21`，`signal_cutoff=2026-07-22`，`last_scheduled_exit=2026-08-19`
- **唯一主终点**：声明成本与已实现交易成本后的 `total_return > 0`
- 可评估性门：完整 preflight（仅未来授权后才运行）、`closed_trades>=20`、`open_positions_at_end=0`、全部指标有限、P&L reconciliation `abs<=1e-6`
- 硬风险门：baseline Sharpe `>0`、max drawdown `>=-0.15`、`largest_single_symbol_loss/80000<=0.03`
- 成本门：`severe_4x_commission_5x_slippage` 的 `total_return>0` 且期末无持仓；`moderate_2x/2x` 只作描述
- 沪深 300 **价格指数** total return / Sharpe / drawdown 与 strategy−benchmark 只作描述（非含息基准）
- 行业归因与投入率只作描述；静态行业非 PIT，不能作门槛

## 结果语义（预声明）

| 结果 | 含义 |
| --- | --- |
| `not_evaluable` | 数据 / 预检 / 完整性 / 交易数 / 指标有限性 / P&L 闭合失败 |
| `no_go` | 可评估，但主终点或任一硬风险 / 严重成本门失败 |
| `conditional_go` | 所有预声明门通过，但仍 `human_review_required`；**绝不**自动进入评分 / 模拟盘 / 交易 |

固定：`ready_for_scoring=false`、`ready_for_trading=false`、`auto_deploy=false`、`authorized=false`、`one_shot_required=true`。

禁止：p 值、IC、参数搜索、调锚点、重用 2024 调参、事件候选接入。

## 本里程碑明确不做

- 不运行、不读取、不计算任何 2025+ preflight / score / IC / backtest / 收益 / 交易 / 组合结果
- 不创建授权文件、评估输出或消费收据
- 不修改现有策略配置、评分、股票池、行业约束、交易引擎或事件逻辑

## 校验

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

.venv/bin/python -m app.cli verify-all-a-share-portfolio-oos-freeze \
  --freeze-file ./config/research/all-a-share-portfolio-oos-freeze-v1.json
```

合同自哈希、策略 YAML、开发证据 JSON、四个 manifest、两张 calendar 或任一门槛不一致即失败关闭。该命令不联网、不读 token、不跑 score/IC/回测。
