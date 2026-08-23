from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models.backtest import BacktestResult
from app.models.scores import ScoreResult
from app.persistence.db import init_db
from app.persistence.models import BacktestRun
from app.pipeline import run_backtest, run_score
from app.settings import get_settings
from app.strategies import baseline_v1 as _baseline_v1  # noqa: F401
from app.strategies.loader import load_strategy_config
from app.strategies.registry import StrategyRegistry

app = FastAPI(title="ai-quant-research", version="0.1.0")


class BacktestRequest(BaseModel):
    strategy: str = "baseline_v1"
    start: date
    end: date


class StrategyInfo(BaseModel):
    name: str
    version: str | None = None
    config_hash: str | None = None


class RankingResponse(BaseModel):
    date: date
    strategy: str
    items: list[ScoreResult]


class BacktestCreated(BaseModel):
    id: str
    status: str
    result: BacktestResult | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/strategies", response_model=list[StrategyInfo])
def strategies() -> list[StrategyInfo]:
    settings = get_settings()
    out: list[StrategyInfo] = []
    for name in StrategyRegistry.names():
        try:
            config = load_strategy_config(name, settings.strategies_dir)
            out.append(
                StrategyInfo(name=name, version=config.version, config_hash=config.config_hash())
            )
        except FileNotFoundError:
            out.append(StrategyInfo(name=name))
    return out


@app.get("/ranking", response_model=RankingResponse)
def ranking(
    date_: Annotated[date, Query(alias="date")],
    strategy: str = "baseline_v1",
    top: Annotated[int, Query(ge=1, le=200)] = 20,
) -> RankingResponse:
    try:
        results = run_score(date_, strategy)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RankingResponse(date=date_, strategy=strategy, items=results[:top])


@app.post("/backtests", response_model=BacktestCreated)
def create_backtest(payload: BacktestRequest) -> BacktestCreated:
    engine = init_db()
    run_id = str(uuid.uuid4())
    session = Session(engine)
    row = BacktestRun(
        id=run_id,
        strategy_name=payload.strategy,
        strategy_version="",
        strategy_config_hash="",
        start_date=payload.start,
        end_date=payload.end,
        status="running",
        created_at=datetime.now(UTC).replace(tzinfo=None),
        result_json=None,
    )
    session.add(row)
    session.commit()
    try:
        result = run_backtest(payload.strategy, payload.start, payload.end)
        row.strategy_version = result.strategy_version
        row.strategy_config_hash = result.strategy_config_hash
        row.status = "done"
        row.result_json = result.model_dump_json()
        session.add(row)
        session.commit()
        return BacktestCreated(id=run_id, status="done", result=result)
    except Exception as exc:  # noqa: BLE001
        row.status = "error"
        row.error = str(exc)
        session.add(row)
        session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/backtests/{backtest_id}")
def get_backtest(backtest_id: str) -> dict[str, Any]:
    engine = init_db()
    session = Session(engine)
    try:
        row = session.get(BacktestRun, backtest_id)
        if row is None:
            raise HTTPException(status_code=404, detail="backtest not found")
        result = None
        if row.result_json:
            result = BacktestResult.model_validate_json(row.result_json)
        return {
            "id": row.id,
            "status": row.status,
            "strategy_name": row.strategy_name,
            "strategy_version": row.strategy_version,
            "strategy_config_hash": row.strategy_config_hash,
            "start": row.start_date,
            "end": row.end_date,
            "error": row.error,
            "result": result,
        }
    finally:
        session.close()
