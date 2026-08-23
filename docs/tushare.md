# Tushare 历史行情拉取

本系统只把 Tushare 当作**原始数据来源**。拉取结果必须标准化成现有六表契约（含 `universe_membership`），再经 `import_market_data()` 写入 `data/parquet/`。评分和回测不直接调用 Tushare。

**仅用于历史研究。不接券商，不自动下单。**

## 设置 Token

Token 只从环境变量读取，不要写进 YAML、代码、测试或 Git。

```bash
export AIQ_TUSHARE_TOKEN="你的 token"
pip install -e ".[tushare]"
```

不要通过 CLI 参数传入 Token。未设置时命令会失败，且不会发起网络请求。

## 拉取范围

必须同时给出 `--start`、`--end`，以及互斥的股票池输入之一（不允许默默拉全市场）。

```bash
# 手工受控股票池（manual_static）。只代表文件中的研究样本。
python -m app.cli fetch-tushare \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --strategy baseline_real_cn_v1 \
  --symbols-file ./symbols.txt

# 历史点时股票池（historical_membership）。成员文件必须覆盖每个交易日的完整截面。
python -m app.cli fetch-tushare \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --strategy baseline_csi300_pit_v1 \
  --universe-membership-file ./csi300_membership_daily.csv
```

`--symbols-file` 只对应 `universe.mode: manual_static`，导入时会为这些代码生成标记为该模式的每日成员表，**不代表全市场或指数历史成分**。

`--universe-membership-file` 只对应 `historical_membership`。系统从文件提取历史并集代码去拉行情，但每日候选仍按文件里的 `as_of_date` + `available_at` 过滤，不会把并集当作每天的股票池。当前阶段不从 Tushare `index_weight` 联网拉取成分。来源清单由用户/可信来源提供；`verify-universe-source` 只验证，不下载、不生成、不把下载时间当 `available_at`。

`--index-universe` 已禁用，传入会报未知选项。按结束日成分铺回历史会造成幸存者偏差。

`symbols.txt` 每行一个 **已经带交易所后缀** 的 Tushare `ts_code`，例如 `000001.SZ`。系统不会猜测或改写后缀。成员 CSV 表头为 `universe_id,as_of_date,symbol,available_at,weight`。

策略 YAML 的 `data.market_index`、`data.global_symbol`、`data.sessions` 必须与输出 symbol 完全一致。可用配置：

- 手工受控池：`config/strategies/baseline_real_cn_v1.yaml`
- 沪深 300 点时研究：`config/strategies/baseline_csi300_pit_v1.yaml`

不要把 API 连通、单股票导入成功或手工股票池回测描述成“全市场策略有效”。

## 使用的官方接口

实现只调用 Tushare Pro 已文档化的接口，不臆造字段：

| 接口 | 用途 |
| --- | --- |
| `trade_cal` | A 股交易日历（SSE / `is_open=1`） |
| `stock_basic` | 证券名称、当前行业、上市日、退市日；按官方 `list_status` 分别拉取 `L`/`D`/`P`/`G` 后合并（默认只返回 `L`） |
| `daily` | 未复权个股 OHLCV、昨收 |
| `daily_basic` | `turnover_rate`（百分比，导入时转为小数） |
| `adj_factor` | 前复权 / 后复权 |
| `stk_limit` | `pre_close` / `up_limit` / `down_limit` → 逐日 `price_limit_pct` |
| `suspend_d` | 全日停牌（`suspend_type=S` 且无日内时段） |
| `namechange` | 历史名称中的 ST / \*ST 等，生成历史 `is_st` |
| `index_daily` | A 股指数 |
| `index_global` | 海外指数（如 `SPX`、`HSI`） |

个股 `daily` 在标准化时按请求的 `start`/`end` 截断，不会把窗口外的日线写入快照。

`daily` 官方说明：停牌期间不提供数据。只有 `suspend_d` 明确为全日停牌时，才会补 `OHLC=前收、volume=0、is_suspended=true`。上市日（含）到退市日（不含）与交易日历的交集里，若既无日线也不是全日停牌，导入会失败，不会静默缺行。上市前、退市后的缺口允许。

`stock_basic.industry` 是本次拉取的当前行业，不是点时分类。`baseline_real_cn_v1` 因此将 `sector_score` 设为 0；在引入有日期版本的行业分类之前，不要把行业分当作严格点时结果。

评分持久化按 `score_date` + `strategy_config_hash` + `data_snapshot_id` 替换。真实配置使用 `config_id: baseline_real_cn_v1`，与 demo 的 `baseline_v1` 实现名区分，避免同日互相覆盖。

缺少 `stk_limit` 行时不会静默填 10%。创业板 / 科创板 / 北交所 / 上市初期的幅度以当日涨跌停价和前收计算。涨跌停价与前收都缺省时，`price_limit_pct` 为 `null`。

海外 `available_at` 按策略 YAML 的 session 收盘时间换算成 **naive UTC**。不接受、也不写入 `-05:00` 这类非零偏移。

成功时打印 `source_name`、`universe_id`、`universe_mode`、成员表行数、覆盖区间、证券数量、基准、`adjustment`、`source_version`、`data_snapshot_id`。失败不会破坏已有 `data/parquet/` 快照。
