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

数据写入 `data/parquet/`。

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

## 策略配置

权重、股票池、Market Gate、交易参数、成本全部来自：

`config/strategies/baseline_v1.yaml`

不要在策略代码里写死权重。

## 已知限制 / TODO

- `TushareProvider` / `AKShareProvider` 只建立了接口，**没有**实现联网拉取。
- 没有分钟级数据。同一根日线同时触发止盈和止损时，按保守原则视为**先止损**。
- 回测成交默认下一交易日开盘；A 股 T+1，买入当日不可卖。
- 没有组合再平衡、没有融资融券、没有涨跌停板撮合模型。
- API 回测是同步执行，不是任务队列。
