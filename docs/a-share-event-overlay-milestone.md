# A 股五类事件 overlay 里程碑

本里程碑只建立离线数据契约，不采集网络数据、不修改评分、不执行交易。五类事件必须作为独立 overlay 绑定到精确的六表行情快照，未来只有通过独立因子诊断后才能进入新策略配置。

## 五张规范表

| 规范表 | 原始接口 | 逻辑版本键 |
| --- | --- | --- |
| `earnings_forecast_events` | `forecast` | `symbol, report_period, ann_date` |
| `earnings_express_events` | `express` | `symbol, report_period, ann_date` |
| `holder_count_events` | `stk_holdernumber` | `symbol, end_date, ann_date` |
| `share_unlock_events` | `share_float` | `symbol, float_date, ann_date, holder_name, share_type` |
| `audit_opinion_events` | `fina_audit` | `symbol, report_period, ann_date` |

每一行都包含：

- 规范化股票代码；
- 公告日 `ann_date`；
- `available_at=公告日 23:59 Asia/Shanghai`，即日期型公告最早只能供之后的决策使用；
- 基于规范字段重新计算的 `source_row_hash`；
- 接口对应的报告期、事件日、数值及说明字段。

同一报告期不同公告日是不同版本，必须全部保留。相同逻辑键的重复或冲突行会失败，不能用最后一行覆盖。

## 离线源目录

目录必须包含五个 CSV/Parquet 文件和 `source_manifest.json`。文件名可以自定，但 manifest 的 key 必须固定：

```text
event-source/
├── source_manifest.json
├── forecast.csv
├── express.csv
├── stk_holdernumber.csv
├── share_float.csv
└── fina_audit.csv
```

manifest 示例：

```json
{
  "schema_version": "1",
  "source_name": "tushare",
  "source_version": "a-share-events-2022-2024-v1",
  "fetched_at": "2025-01-10T09:00:00+08:00",
  "coverage_start": "2022-01-01",
  "coverage_end": "2024-12-31",
  "files": {
    "forecast": {"path": "forecast.csv", "sha256": "<64位小写SHA-256>"},
    "express": {"path": "express.csv", "sha256": "<64位小写SHA-256>"},
    "stk_holdernumber": {"path": "stk_holdernumber.csv", "sha256": "<64位小写SHA-256>"},
    "share_float": {"path": "share_float.csv", "sha256": "<64位小写SHA-256>"},
    "fina_audit": {"path": "fina_audit.csv", "sha256": "<64位小写SHA-256>"}
  },
  "availability_evidence": {
    "forecast": "Tushare doc 45 ann_date",
    "express": "Tushare doc 46 ann_date",
    "stk_holdernumber": "Tushare doc 166 ann_date",
    "share_float": "Tushare doc 160 ann_date",
    "fina_audit": "Tushare doc 80 ann_date"
  },
  "notes": "查询参数、权限、分页方式与人工备注"
}
```

`fetched_at` 必须带时区；五个来源文件及五条可得时点证据缺一不可。相对路径不能逃出源目录，SHA-256 不匹配直接失败。

## 离线物化

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

.venv/bin/python -m app.cli materialize-a-share-event-overlay \
  --source-dir ./data/raw/a-share-events-2022-2024-v1 \
  --market-dir ./data/all-a-share-historical-v1/parquet \
  --output-dir ./data/all-a-share-historical-v1/events-v1
```

物化过程会：

1. 校验五个原始文件的字节哈希；
2. 规范化字段并拒绝空表、无效代码、无效日期、非有限数值、负解禁量等异常；
3. 检查公告日没有越出源 manifest，也没有晚于行情快照；
4. 检查事件股票均存在于绑定行情的 `instruments.parquet`；
5. 原子写入五张 Parquet、原始 `source_manifest.json` 和内容寻址 `manifest.json`。

## 离线复核

```bash
.venv/bin/python -m app.cli verify-a-share-event-overlay \
  --event-dir ./data/all-a-share-historical-v1/events-v1 \
  --market-dir ./data/all-a-share-historical-v1/parquet
```

复核会重新计算每行来源哈希、五表内容哈希、源 manifest 字节哈希和总 `event_snapshot_id`，并验证 `base_market_snapshot_id`。任何 Parquet、来源说明或基础行情版本变化都会失败。

## 当前边界

- 事件 overlay 尚未挂载到评分引擎。
- 没有声称股东户数、业绩预告、解禁或审计意见具有正收益。
- 2024 已经被观察，不能再作为新增事件因子的未见留出期。
- 下一里程碑是生成可恢复的 Tushare 五接口采集器与质量报告；采集完成后先检查覆盖率、重复版本和缺失机制，不立刻调权。
