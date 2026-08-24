# 全 A 股点时财务与估值 overlay

本里程碑把财务/估值数据作为独立、内容寻址的 overlay，绑定到一个已经验证的六表市场快照。它不会修改基础行情，也不会连接券商。

## 数据契约

- `fina_indicator` 按股票分区；请求覆盖首个研究日之前 `max_report_age_days` 的报告期。
- 财报 `available_at` 保守设为公告日 23:59（Asia/Shanghai）。
- 全市场严格策略只允许 `update_flag=0` 的初始记录。`update_flag=1` 若没有独立修订发布时间，一律不回填到原公告日。
- `daily_basic` 按交易日查询全市场，单页上限 6,000；代码保留分页和重复页拒绝检查。
- 每日估值 `available_at` 为交易日 17:00（Asia/Shanghai）。
- 每个分区先规范化，再原子写入；重跑会验证并复用已有分区。
- 完成后生成 `collection_request.json`、`quality_report.json`、`collection_manifest.json`，manifest 覆盖所有 Parquet 字节哈希。
- 最终 overlay manifest 记录基础市场 `snapshot_id` 和采集 `request_id`；运行时不匹配即拒绝。

接口官方说明：

- Tushare `daily_basic`：<https://tushare.pro/document/2?doc_id=32>
- Tushare `fina_indicator`：<https://tushare.pro/document/2?doc_id=79>

## 采集

Token 只注入当前终端，不写入命令历史、配置或 manifest：

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

read -rs "AIQ_TUSHARE_TOKEN?Tushare Token: "
echo
export AIQ_TUSHARE_TOKEN

.venv/bin/python -m app.cli collect-tushare-all-a-share-fundamentals \
  --start 2021-10-08 \
  --end 2024-12-31 \
  --strategy all_a_share_historical_value_quality_v1 \
  --market-dir ./data/all-a-share-historical-v1/parquet \
  --staging-dir ./data/raw/all-a-share-fundamentals-20211008-20241231-v1

unset AIQ_TUSHARE_TOKEN
```

网络或频率错误后，原命令重跑即可；已经验证的股票/日期分区不会重新请求。

## 离线物化

```bash
.venv/bin/python -m app.cli materialize-tushare-all-a-share-fundamentals \
  --staging-dir ./data/raw/all-a-share-fundamentals-20211008-20241231-v1 \
  --market-dir ./data/all-a-share-historical-v1/parquet \
  --strategy all_a_share_historical_value_quality_v1 \
  --output-dir ./data/all-a-share-historical-v1/fundamentals-value-quality-v1 \
  --source-version all-a-share-fundamentals-20211008-20241231-v1
```

## 研究挂载

```bash
export AIQ_DATA_DIR=./data/all-a-share-historical-v1
export AIQ_DATABASE_URL=sqlite:///data/all-a-share-historical-v1/app.db
export AIQ_FUNDAMENTAL_DIR=./data/all-a-share-historical-v1/fundamentals-value-quality-v1

.venv/bin/python -m app.cli preflight-research \
  --strategy all_a_share_historical_value_quality_v1 \
  --start 2022-04-01 \
  --end 2024-12-31
```

预检通过只说明数据和点时契约满足运行条件，不证明策略收益有效。必须继续做 IC、分年/分市场状态诊断和含成本回测。
