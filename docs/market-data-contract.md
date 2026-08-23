# 标准化行情契约

本系统只接受**已经标准化**的历史行情。真实数据源（交易所、Tushare、AKShare、券商导出）必须先预处理成本文件约定的字段，再通过离线导入进入研究链路。

本阶段不联网拉取。导入只读本地 CSV 或 Parquet，不会改写输入目录。

## 导入命令

```bash
python -m app.cli import-market-data \
  --source-dir /path/to/normalized-data \
  --source-name local \
  --adjustment forward \
  --source-version 2024Q4 \
  --market-index 000300.SH \
  --global-symbol SPX
```

`--adjustment` 只能是 `forward` / `backward` / `none`。它会写入快照并参与 `snapshot_id`，必须与价格口径一致。

`--market-index` / `--global-symbol` 可选；若提供，则必须出现在 `index_bars` / `global_bars` 中，否则导入失败。

成功后写入 `data/parquet/`，并生成内容哈希 `manifest.json`。失败时不会留下半成品，也不会覆盖已有有效快照。

## 输入目录

至少包含下列六个表，每个表为 `.csv` 或 `.parquet`：

| 文件 | 用途 |
| --- | --- |
| `daily_bars` | A 股个股日线 |
| `index_bars` | 指数日线（含策略基准） |
| `global_bars` | 海外/跨市场日线，必须带 `available_at` |
| `instruments` | 证券主数据 |
| `calendar` | A 股交易日历 |
| `universe_membership` | 每个交易日的点时股票池成员，必须带 `available_at` |

## 代码与时间规范

- **symbol**：导入数据、策略 YAML `data.market_index` / `data.global_symbol`、以及 `data.sessions` 的键必须使用同一套代码。系统不改写、不猜测交易所后缀。
- **date**：日历日，`YYYY-MM-DD`。
- **available_at**：该根 K 线在决策时点已经可知的时刻，必须是 **UTC**。只接受 naive UTC（`YYYY-MM-DDTHH:MM:SS`）或 `Z` / `+00:00`。带非零偏移的值（例如 `2024-01-02T16:00:00-05:00`）在导入时会被拒绝，不会被剥掉时区后当成 16:00 UTC。
- A 股 T 日评分的默认决策时点是 **T 日 15:00 Asia/Shanghai**。只使用 `available_at <= 决策时点` 的全球数据。美股 T 日收盘通常晚于该时点，因此 T 日评分通常只能用到美股 T-1。

## 字段

### daily_bars / index_bars

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| symbol | string | 标准化代码 |
| date | date | 交易日 |
| open, high, low, close | float64 | 必须有限且 > 0 |
| volume, amount | float64 | 必须有限且 ≥ 0 |
| turnover_rate | float64 | 换手率；指数可填 0 |
| is_st | bool | 是否 ST |
| is_suspended | bool | 停牌。停牌日仍保留 K 线，回测不可交易 |
| price_limit_pct | float64 \| null | 当日普通涨跌停幅度。`0.10`=±10%，`0.20`=±20%。`null` 表示当日不适用普通涨跌停，回测不得按 10% 拦交易 |

约束：

- 主键 `(symbol, date)` 唯一
- `high >= max(open, close)`
- `low <= min(open, close)`
- 禁止 `NaN` / `Infinity` / `-Infinity`

示例：

```text
symbol,date,open,high,low,close,volume,amount,turnover_rate,is_st,is_suspended,price_limit_pct
000001.SZ,2024-01-02,10.00,10.20,9.90,10.10,12000000,121000000,0.03,false,false,0.10
300001.SZ,2024-01-02,20.00,20.50,19.80,20.20,8000000,162000000,0.04,false,false,0.20
ST0002.SZ,2024-01-02,5.00,5.10,4.90,5.02,3000000,15060000,0.02,true,false,0.05
688001.SH,2024-01-02,30.00,31.00,29.50,30.40,2000000,60800000,0.05,false,false,
```

最后一行 `price_limit_pct` 为空，表示该日不套用普通涨跌停。

### global_bars

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| symbol | string | 标准化代码 |
| date | date | 该市场交易日 |
| close | float64 | 必须有限且 > 0 |
| available_at | datetime UTC | 该收盘价已知时刻 |
| ret_1d | float64 | 可选，缺省 0 |
| market | string | 可选 |
| timezone | string | 可选，仅作元数据，不替代 `available_at` |

### instruments

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| symbol | string | 与行情表一致 |
| name | string | 名称 |
| sector | string | 板块 |
| listing_date | date | 上市日 |
| is_index | bool | 指数 |
| is_global | bool | 海外序列 |
| market | string | 如 `CN` / `US` / `HK` |
| timezone | string | IANA 时区 |
| session_close | string | `HH:MM` 当地收盘 |

### calendar

| 字段 | 类型 |
| --- | --- |
| date | date |

必须覆盖评分/回测窗口内的 A 股交易日，日期唯一。

### universe_membership

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| universe_id | string | 股票池标识，与策略 YAML `universe.id` 一致 |
| as_of_date | date | 该日完整成员截面的生效日，不是文件下载日 |
| symbol | string | Tushare `ts_code`，必须已在 `instruments` 中 |
| available_at | datetime UTC | 该成员关系在决策时点已经可知的时刻 |
| weight | float64 \| null | 预留给未来组合构建；当前评分/回测不按权重调仓 |

约束：

- 主键 `(universe_id, as_of_date, symbol)` 唯一
- 每个交易日必须导入完整截面，不能只存增减差异，也不能沿用前一日
- 不得包含快照覆盖期外的日期
- 旧五表快照缺少本表时会被拒绝，不会回退为“所有 instruments 都可交易”

`import-market-data` 可用 `--universe-membership-file` 提供该表。`fetch-tushare` 在 `manual_static` 下按 `--symbols-file` 生成该表；在 `historical_membership` 下只接受离线每日成员历史文件。

原始成分快照 CSV（`universe_id,effective_from,symbol,available_at,weight`）与每日成员 CSV 是两种格式。前者是可信来源的完整截面序列；`build-universe-membership` 只做保守前向物化，不会联网、不会下载指数成分，也不会把“当前成员”写成历史成员。输出必须截断到请求窗口内的交易日历日。`baseline_csi300_pit_v1` 只有在输入完整的 300 成分历史快照时才能用于指数历史研究；两成员或小样本文件只可用于管道验证，不能描述成 CSI300 回测。

来源清单由用户/可信来源提供。`verify-universe-source` 只验证 provenance JSON 与原始快照文件的精确字节 SHA-256、`universe_id`、覆盖区间和完整截面人数；不下载、不生成成员，也不把 `file_obtained_at` 或下载时间写回/推导为 `available_at`。行内 `available_at` 仍只来自 CSV，并走既有严格 UTC 解析。

用户提交的 `membership_source_manifest.json` 必须是下面这个对象（禁止未知字段）。`source_url`、`announcement_id`、`source_note` 至少填写一项，可只留一项并删掉其余两项：

```json
{
  "schema_version": "1",
  "universe_id": "csi300",
  "source_name": "your-source-name",
  "snapshots_file_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "file_obtained_at": "2026-08-23T04:00:00Z",
  "effective_from_coverage": {
    "start": "2024-01-02",
    "end": "2024-12-31"
  },
  "available_at_definition": "How each row's available_at was determined (not the download time).",
  "available_at_evidence": "Where that timestamp can be audited (document, announcement, or notes).",
  "expected_constituents": 300,
  "source_url": "https://example.invalid/source",
  "announcement_id": "optional-announcement-id",
  "source_note": "optional source note"
}
```

`snapshots_file_sha256` 必须换成原始快照文件的精确 bytes SHA-256。`file_obtained_at` 必须是带 `T` 的 UTC 时间戳（可 `Z` / `+00:00`），仅为审计，不能写成裸日期，也不能替代任何行的 `available_at`。`effective_from_coverage.start` / `end` 必须等于该文件全部截面 `effective_from` 的最小/最大日期。

## 快照

导入成功后 `data/parquet/manifest.json` 至少包含：

- `snapshot_id`：六张表内容哈希 + schema + 复权口径的 SHA-256
- `schema_version`
- 每张表的 content hash
- 合并 `content_hash`（与 `snapshot_id` 相同）
- `source_name`、`fetched_at`、覆盖区间、`adjustment`、行数、基准代码

`fetched_at` 和输入文件的修改时间**不进入** `snapshot_id`。同一批逻辑内容再次导入，得到同一个 `snapshot_id`。价格、`available_at`、证券信息或日历任一关键字段变化，`snapshot_id` 必须改变。

缺少 `manifest.json`、manifest 与 Parquet 内容不一致、或缺表时，评分和回测会失败，**不会**回退 demo 数据。

## 真实数据预处理

1. 从任意数据源导出日线、指数、海外指数、证券列表、交易日历。
2. 统一 symbol，不要混用 `000001`、`000001.SZ`、`sz000001`。
3. 按选定复权口径换算 OHLC，并在导入时声明同一口径。
4. **逐日**填写 `price_limit_pct`：主板/ST/创业板/科创板/北交所/上市初期以数据源或官方规则为准，不要让本系统猜测。
5. 海外 K 线按该市场收盘的当地时间换算成 UTC，写入 `available_at`。
6. 停牌日保留 K 线，`is_suspended=true`，成交量为 0。
7. 将六张表放到同一目录，跑导入命令，再用 YAML 里的代码与导入 symbol 对齐后评分/回测。
8. 若数据来自 Tushare，使用 `fetch-tushare`（见 `docs/tushare.md`），不要绕过本契约自行写 Parquet。
