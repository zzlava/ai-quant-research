# ai-quant-research

A 股低频、半自动量化**研究**系统 MVP。

当前范围只有：研究、评分、排名、历史回测。

**不会做、也没有做：**

- 券商接口
- 自动下单
- 高频交易
- 前端
- LLM 调用
- 机器学习

## 架构

```
Data Provider
    ↓
Storage (DuckDB + Parquet / 内存)
    ↓
Feature Engine
    ↓
Universe Filter
    ↓
Strategy Engine
    ↓
Scoring Engine
    ↓
Ranking
    ↓
Backtest
```

策略代码不能直接调用 AKShare / Tushare / 网络 API。行情只通过 `MarketDataProvider` → `MarketStore` 进入后续模块。

## 回测窗口语义

`--start` / `--end` 是声明区间：

- `valuation_end`：声明结束日（或之前最近一个交易日）。权益曲线和指标只计算到这一天。
- `entry_end`：最后允许开仓日，等于 `valuation_end`。
- `signal_end`：最后允许发信号日。仅当下一交易日开盘成交日 `<= entry_end` 时才发信号。

因此：结束日产生、却要在结束日之后才成交的信号，不会进入该回测。区间结束时仍持有的仓位按收盘价估值，不在区间外平仓，也不把区间外交易日算进回撤/年化。

## 全球数据可用时点

A 股 `as_of=T` 的决策时点默认是 **T 日 15:00 Asia/Shanghai**。

海外 K 线必须带 `available_at`（该市场收盘时刻，存 UTC）。评分只使用 `available_at <= 决策时点` 的数据。美股 T 日收盘通常晚于 A 股 T 日收盘，因此默认使用海外 **T-1**。

基准代码、海外代码、时区、收盘时刻全部在 YAML `data:` 中，不写死 demo 常量。缺少基准数据会直接失败，不会回退成 50 分。

## 复权、停牌、涨跌停口径

- Demo 快照声明 `adjustment: forward`（合成前复权价格，无真实除权事件）。
- 停牌日仍保留 K 线：`is_suspended=true`，OHLC=前收，成交量=0，不可交易。
- 每条日线带 `price_limit_pct`（可为 `null`）。回测优先用该日幅度，不再仅按 ST / 非 ST 推断。`null` 表示当日不适用普通涨跌停。
- 一字涨停不可买，一字跌停不可卖。开盘跳空优先于盘中 TP/SL；同一根日线同时触发时先止损。
- 开盘跳空跌破止损时，按**开盘价**成交，而不是按止损线。

## 导入真实历史数据

先把任意数据源预处理成标准化目录，字段见 `docs/market-data-contract.md`。不要把 Token 或 Cookie 写进仓库。

```bash
python -m app.cli import-market-data \
  --source-dir /path/to/normalized-data \
  --source-name local \
  --adjustment forward
```

成功后会打印 `data_snapshot_id`。评分、回测和 API 都会带上同一个快照标识。缺少 manifest、manifest 与内容不一致、或缺表时会直接失败，不会回退 demo 数据。

## 从 Tushare 拉取真实历史

Token 只放在环境变量 `AIQ_TUSHARE_TOKEN`，不要提交。步骤见 `docs/tushare.md`。

```bash
# 手工受控股票池。只代表 symbols.txt 里的研究样本，不是全市场或指数历史成分。
python -m app.cli fetch-tushare \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --strategy baseline_real_cn_v1 \
  --symbols-file ./symbols.txt

# 历史点时股票池：成员文件已预先准备好。不要把 API 连通或手工股票池回测说成全市场有效。
python -m app.cli fetch-tushare \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --strategy baseline_csi300_pit_v1 \
  --universe-membership-file ./csi300_membership_daily.csv
```

历史点时成员文件应由可信来源的完整成分快照离线物化，不要把当前成分当成历史成分：

```bash
python -m app.cli build-universe-membership \
  --snapshots-file ./csi300_snapshots.csv \
  --calendar-file ./cn_trade_calendar.csv \
  --start 2024-01-02 \
  --end 2024-12-31 \
  --strategy baseline_csi300_pit_v1 \
  --output ./csi300_membership_daily.csv
```

该命令不联网、不读 Token。原始快照 CSV 与每日成员 CSV 格式不同；工具只把已经生效且已知的完整截面前向延续到下一份可用快照。`baseline_csi300_pit_v1` 需要完整的 300 成分历史快照才谈得上指数研究；小样本文件只能验证管道。

来源清单由用户/可信来源提供。`verify-universe-source` 只核对清单与原始快照文件的精确字节，不下载、不生成成分，也不把 `file_obtained_at` 或下载时间当成行内 `available_at`。公开重建数据还必须使用 schema v2 的逐事件证据账本：它绑定每个调样生效日、可用时间、来源文件及其 SHA-256，且明确标识为非许可级 PIT。JSON/CSV 模板见 `docs/market-data-contract.md`：

```bash
python -m app.cli verify-universe-source \
  --snapshots-file ./csi300_snapshots.csv \
  --provenance-file ./membership_source_manifest.json \
  --strategy baseline_csi300_pit_v1
```

该命令只做历史研究数据导入，不执行交易。`--symbols-file` 对应 `universe.mode: manual_static`；`--universe-membership-file` 对应 `historical_membership`，两者互斥。`--index-universe` 已禁用，避免把结束日成分股铺回历史。API 连通、单股票导入成功或手工股票池回测，都不代表全市场策略有效。

评分和回测前先做只读预检。命令复用已校验快照，拒绝覆盖范围外、点时成员不完整、或仍处于 warm-up 的区间，不会把 ready 日之前当成零信号。通过预检不能证明策略收益有效：

```bash
python -m app.cli preflight-research \
  --strategy baseline_csi300_pit_v1 \
  --start 2022-01-01 \
  --end 2024-12-31
```

把 `data:` 里的代码保持与导入数据完全一致，再：

```bash
python -m app.cli score --date 2024-01-15 --strategy baseline_real_cn_v1
python -m app.cli backtest --strategy baseline_real_cn_v1 --start 2024-01-02 --end 2024-06-28
```

## 要求

- Python 3.12

## 安装

```bash
cd ai-quant-research
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Docker

```bash
docker compose build
docker compose run --rm api python -m app.cli generate-demo
docker compose up
```

API：`http://localhost:8000/health`

## 生成 Demo 数据

离线、确定性（`seed=42`）：50 只虚拟股票、3 个板块、2 个 A 股指数、2 年交易日、全球市场序列。

```bash
python -m app.cli generate-demo
```

数据写入 `data/parquet/`，并带基于内容哈希的 `manifest.json`。CLI 会打印 `data_snapshot_id`。

## 运行评分

```bash
python -m app.cli score --date 2024-01-15 --strategy baseline_v1
```

## 运行回测

```bash
python -m app.cli backtest --strategy baseline_v1 --start 2024-01-02 --end 2024-06-28
```

## 启动 API

```bash
uvicorn app.api.main:app --reload --port 8000
```

- `GET /health`
- `GET /strategies`
- `GET /ranking?date=2024-01-15&strategy=baseline_v1`
- `POST /backtests`
- `GET /backtests/{id}`

## 运行测试

全部离线，不访问外网：

```bash
pytest
ruff check src tests
mypy src
```

GitHub Actions 会在 push / PR 时自动跑上述三项。

## 策略配置

权重、股票池、Market Gate、交易参数、成本、基准代码全部来自：

`config/strategies/baseline_v1.yaml`

未知字段、止损为正、持有天数 ≤ 0、Gate 区间重叠/断裂都会校验失败。

## 已知限制 / TODO

- `TushareProvider` 已实现离线可测的官方接口拉取，必须经标准化六表（含 `universe_membership`）和 `import_market_data()` 入库。`AKShareProvider` 仍是 TODO。
- 没有分钟级数据。同一根日线盘中同时触发止盈和止损时，按保守原则视为**先止损**；开盘跳空则按开盘价。
- 涨跌停按逐日 `price_limit_pct` + 日线一字板近似，不是逐笔排队模型。
- 没有组合再平衡、没有融资融券。
- API 回测是同步执行，不是任务队列。
