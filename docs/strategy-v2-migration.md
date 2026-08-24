# 策略 v2：数据迁移与研究边界

`csi300_bigquant_public_reconstruction_v2` 是一个新的说明性研究变体，不覆盖或重写 v1 的既有结果。它将市场状态门控、股票截面排名和执行成本拆开，且只允许原始 OHLC 用于成交、涨跌停、整手和估值。

## 必须重新抓取价格快照

v2 使用快照 schema 4。旧快照只有前复权 OHLC，无法证明成交价为原始价，因此会被拒绝。先保留 BigQuant 公开重建目录，再以它导出的 373 只去重代码作为**基础行情下载列表**：

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

export AIQ_DATA_DIR=./data/csi300-bigquant-public-replay-v2
export AIQ_DATABASE_URL=sqlite:///data/csi300-bigquant-public-replay-v2/app.db
export AIQ_PUBLIC_RECONSTRUCTION_DIR=./data/public-reconstruction/csi300-bigquant-2022-2024

.venv/bin/python -m app.cli fetch-tushare \
  --start 2021-10-01 \
  --end 2024-12-31 \
  --strategy baseline_real_cn_raw_backward_v2 \
  --symbols-file ./data/csi300-bigquant-public-replay-v1/public-reconstruction-symbols.txt \
  --source-version bigquant-public-reconstruction-base-v2
```

`baseline_real_cn_raw_backward_v2` 仅用于下载和校验基础行情；其 `manual_static` 成员表不是 CSI300 历史成员声明。真正运行时，v2 再叠加 `AIQ_PUBLIC_RECONSTRUCTION_DIR` 中的公开重建成员关系。

若 Tushare 返回频率限制，停止当前命令并等满一个分钟窗口后重试；不要同时启动多个抓取进程。标准导入是原子替换：失败不会产生可用的半成品快照。

## 运行顺序

```bash
.venv/bin/python -m app.cli preflight-research \
  --strategy csi300_bigquant_public_reconstruction_v2 \
  --start 2022-04-01 \
  --end 2024-12-31

.venv/bin/python -m app.cli analyze-ic \
  --strategy csi300_bigquant_public_reconstruction_v2 \
  --start 2022-04-01 \
  --end 2024-12-15 \
  --horizons 1,5,10 \
  --output ./data/csi300-bigquant-public-replay-v2/ic-20220401-20241215.json

.venv/bin/python -m app.cli backtest \
  --strategy csi300_bigquant_public_reconstruction_v2 \
  --start 2022-04-01 \
  --end 2024-12-31
```

先看 IC：它只将未来收益作为研究标签，绝不回灌给当日打分。再看回测输出中的 `gross_realized_pnl`、`explicit_costs`、`estimated_slippage` 和 `signal_orders_*`，区分信号、退出和摩擦的贡献；不要用单一 Sharpe 或总收益调参。

## 仍然不可作出的结论

- 公开 BigQuant 成员快照仍是 `public_reconstruction`，不是严格 PIT。
- 该回测不能用于实盘、不能与正式 PIT 指数策略比较。
- v2 的 ATR 阈值、最短持有、冷却期和最低分数是待检验的预注册起点，不是已证明有效的参数。
- 日线无法识别同一交易日止盈和止损的真实先后顺序；当前仍以保守的止损顺序处理。
