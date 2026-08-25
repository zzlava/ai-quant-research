# 全 A 股价值组合 p10_h20 一次性 OOS 授权（实现期说明）

授权日期：2026-08-25

用户授权原话：`我授权按照冻结协议执行 p10_h20 的 2025+ 一次性 OOS 评估`

- 冻结合同：[`config/research/all-a-share-portfolio-oos-freeze-v1.json`](../config/research/all-a-share-portfolio-oos-freeze-v1.json)（`freeze_id=e5cdb0ff04e5eb78c331d6e4af77d4f8932a683e3f1558f83945708d48d00cc0`）
- 授权合同：[`config/research/all-a-share-portfolio-oos-one-shot-authorization-v1.json`](../config/research/all-a-share-portfolio-oos-one-shot-authorization-v1.json)
- 策略：`config/strategies/all_a_share_historical_value_portfolio_selected_v2.yaml`（`config_hash=796b793856dcd02a`；运行时仅改锚点后 `b06e86cac8041f84`）

## 评估命令（实现期 NOT RUN）

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

# NOT RUN during implementation
.venv/bin/python -m app.cli evaluate-all-a-share-portfolio-oos-one-shot \
  --strategy all_a_share_historical_value_quality_v1_portfolio_p10_h20_selected_v2
```

预声明输出（尚未创建）：

- `data/all-a-share-oos-20241001-20260821-v1/portfolio-oos-evaluations/one-shot-v1/`
- `data/all-a-share-oos-20241001-20260821-v1/portfolio-oos-evaluations/one-shot-v1.consumption-receipt.json`

## 只读校验（实现期 NOT RUN）

```bash
# NOT RUN during implementation
.venv/bin/python -m app.cli verify-all-a-share-portfolio-oos-one-shot
```

校验器默认加载 committed 授权与冻结合同（含完整日历 schedule proof），拒绝不匹配的自定义 `--authorization-file` / `--freeze-file`，并校验 output/receipt 路径与报告绑定；从落盘 `BacktestResult` 重建 scenario 摘要后与 `report.scenarios` 对账（允许 `result_file` / `result_file_sha256` 除外），再按授权门槛重算 deciding gates。不跑 preflight / score / backtest。

结果语义仅允许 `not_evaluable` / `no_go` / `conditional_go`；始终 `ready_for_scoring=false`、`ready_for_trading=false`、`auto_deploy=false`、`human_review_required=true`。无 GO、无自动晋升。
