# 30 股票受控样本研究

这套配置用于验证数据导入、warm-up、防未来函数、评分、调仓与前端展示，**不是**沪深 300、全市场或可交易策略的历史业绩证明。

## 样本边界

- 股票池：[controlled_sample_anchor_intersection30_v1.txt](../config/research-universes/controlled_sample_anchor_intersection30_v1.txt)
- 策略：[controlled_sample_anchor_intersection30_v1.yaml](../config/strategies/controlled_sample_anchor_intersection30_v1.yaml)
- 来源和偏差说明：[controlled_sample_anchor_intersection30_v1.provenance.json](../config/research-universes/controlled_sample_anchor_intersection30_v1.provenance.json)

该样本是 2021-07-12 与 2024-12-31 两个直接审计的 CSI300 锚点的交集，再通过固定 SHA-256 顺序取 30 只。这个构造故意保证跨期可获得，却也使用了 2024 信息，因此具有幸存者偏差。它只能用于工程和因子回归测试。

## 安全的本地运行方式

Tushare Token 不写入文件。为避免覆盖默认 `data/` 快照，整个试验使用独立数据目录：

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

read -rs "AIQ_TUSHARE_TOKEN?Tushare Token: "
echo
export AIQ_TUSHARE_TOKEN
export AIQ_DATA_DIR=./data/controlled-sample-anchor-intersection30-v1
export AIQ_DATABASE_URL=sqlite:///data/controlled-sample-anchor-intersection30-v1/app.db

.venv/bin/python -m pip install -e '.[tushare]'

.venv/bin/python -m app.cli fetch-tushare \
  --start 2021-09-01 \
  --end 2024-12-31 \
  --strategy controlled_sample_anchor_intersection30_v1 \
  --symbols-file ./config/research-universes/controlled_sample_anchor_intersection30_v1.txt
```

日线从 2021-09-01 开始是为 60 根日线特征窗口留出 warm-up；正式研究窗口不应早于 2022-01-04。拉取成功后先执行：

```bash
.venv/bin/python -m app.cli preflight-research \
  --strategy controlled_sample_anchor_intersection30_v1 \
  --start 2022-01-04 \
  --end 2024-12-31
```

只有预检通过后才能运行 `score` 或 `backtest`。运行结束应清除环境变量：

```bash
unset AIQ_TUSHARE_TOKEN AIQ_DATA_DIR AIQ_DATABASE_URL
```

## 基线逻辑

当前 `baseline_v1` 的个股 alpha 由 20 日相对强度、20 日均线偏离和 20 日收益组成；市场与海外指数用于仓位闸门；当前行业数据不是点时数据，所以行业分固定为 0。它是可检验的基线，不是对未来收益的承诺。
