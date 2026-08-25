# 全 A 股价值组合 p10_h20 一次性 OOS 授权与结果

授权日期：2026-08-25

用户授权原话：`我授权按照冻结协议执行 p10_h20 的 2025+ 一次性 OOS 评估`

- 冻结合同：[`config/research/all-a-share-portfolio-oos-freeze-v1.json`](../config/research/all-a-share-portfolio-oos-freeze-v1.json)（`freeze_id=e5cdb0ff04e5eb78c331d6e4af77d4f8932a683e3f1558f83945708d48d00cc0`）
- 授权合同：[`config/research/all-a-share-portfolio-oos-one-shot-authorization-v1.json`](../config/research/all-a-share-portfolio-oos-one-shot-authorization-v1.json)
- 策略：`config/strategies/all_a_share_historical_value_portfolio_selected_v2.yaml`（`config_hash=796b793856dcd02a`；运行时仅改锚点后 `b06e86cac8041f84`）

## 评估执行（已于 2026-08-25 消费唯一一次授权）

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

# 历史审计命令；输出和消费回执已存在，禁止再次运行。
.venv/bin/python -m app.cli evaluate-all-a-share-portfolio-oos-one-shot \
  --strategy all_a_share_historical_value_quality_v1_portfolio_p10_h20_selected_v2
```

已封存输出：

- `data/all-a-share-oos-20241001-20260821-v1/portfolio-oos-evaluations/one-shot-v1/`
- `data/all-a-share-oos-20241001-20260821-v1/portfolio-oos-evaluations/one-shot-v1.consumption-receipt.json`

## 只读校验（已通过）

```bash
.venv/bin/python -m app.cli verify-all-a-share-portfolio-oos-one-shot
```

校验器默认加载 committed 授权与冻结合同（含完整日历 schedule proof），拒绝不匹配的自定义 `--authorization-file` / `--freeze-file`，并校验 output/receipt 路径与报告绑定；从落盘 `BacktestResult` 重建场景摘要、指标、归因、描述端点与全量 gates。允许只读重放 preflight，不运行 score / backtest / trade。

## 封存结果

- 结论：`no_go`
- 执行链代码提交：`71b158b`
- `report_id=5e193216c156cf3094f19e3c882f5d68dc8ca4338f3ab8496ff0312e79ace53f`
- `receipt_id=69761230249a6be34c5ea9fbcd97c742e0ac9cfd2be10b34082627f66e694e96`
- `report.json` SHA-256：`7da53f6978987fd7c798c577515f60f16f87623c008c6d3f37d361c9ae189121`
- 消费回执文件 SHA-256：`4092ad5b088f164cd4ca495edac79a01cb2c3ece8a53c0802ea439f41360f12a`
- baseline：总收益 `-0.431997%`，Sharpe `0.003146`，最大回撤 `-13.283784%`，69 笔平仓交易，期末持仓 0，最终权益 `79,654.40`。
- 2×佣金 / 2×滑点成本场景：总收益 `-1.158180%`。
- 4×佣金 / 5×滑点严重成本场景：总收益 `-3.399918%`，期末持仓 0。
- 两个决定门失败：`primary_total_return`、`severe_total_return`；其他可评估性与硬风险门通过。
- 同期沪深 300 价格指数收益 `+20.900979%`，策略相对差值 `-21.332976` 个百分点；该基准不含股息，只作描述。

结果不得用于同一 OOS 窗口重跑、调参、选择替代组合或自动晋级。

结果语义仅允许 `not_evaluable` / `no_go` / `conditional_go`；始终 `ready_for_scoring=false`、`ready_for_trading=false`、`auto_deploy=false`、`human_review_required=true`。无 GO、无自动晋升。
