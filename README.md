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
- 回测按日线建模涨跌停：一字涨停不可买，一字跌停不可卖。
- 开盘跳空跌破止损时，按**开盘价**成交，而不是按止损线。

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

数据写入 `data/parquet/`，并带 `manifest.json` 快照版本。

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

- `TushareProvider` / `AKShareProvider` 只建立了接口，**没有**实现联网拉取。接入时必须带 `available_at`、复权口径和数据质量检查。
- 没有分钟级数据。同一根日线盘中同时触发止盈和止损时，按保守原则视为**先止损**；开盘跳空则按开盘价。
- 涨跌停只按日线一字板规则近似，不是逐笔排队模型。
- 没有组合再平衡、没有融资融券。
- API 回测是同步执行，不是任务队列。
