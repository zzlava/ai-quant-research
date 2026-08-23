from __future__ import annotations

from datetime import date
from pathlib import Path

from app.backtest.engine import BacktestEngine
from app.errors import PreflightError
from app.models.backtest import BacktestResult
from app.models.scores import ScoreResult
from app.preflight import preflight_research
from app.scoring.engine import ScoringEngine
from app.settings import Settings, get_settings
from app.storage.duckdb_store import DuckDBParquetStore
from app.storage.protocol import MarketStore
from app.storage.snapshot_io import load_verified_snapshot
from app.strategies.loader import load_strategy_config


def load_store(settings: Settings | None = None) -> MarketStore:
    settings = settings or get_settings()
    snapshot = load_verified_snapshot(settings.parquet_dir)
    return DuckDBParquetStore(settings.parquet_dir, snapshot=snapshot)


def run_score(
    as_of: date,
    strategy_name: str,
    settings: Settings | None = None,
    store: MarketStore | None = None,
) -> list[ScoreResult]:
    settings = settings or get_settings()
    config = load_strategy_config(strategy_name, settings.strategies_dir)
    market = store or load_store(settings)
    preflight_research(store=market, config=config, start=as_of, end=as_of)
    engine = ScoringEngine(market, config)
    results = engine.run(as_of)
    engine.persist(results, settings.scores_dir / "scores.parquet")
    return results


def run_backtest(
    strategy_name: str,
    start: date,
    end: date,
    settings: Settings | None = None,
    store: MarketStore | None = None,
) -> BacktestResult:
    settings = settings or get_settings()
    config = load_strategy_config(strategy_name, settings.strategies_dir)
    if config.research_scope == "latest_market_snapshot":
        raise PreflightError(
            "latest_market_snapshot is limited to a single current as-of ranking; historical backtests are disabled"
        )
    market = store or load_store(settings)
    preflight_research(store=market, config=config, start=start, end=end)
    engine = BacktestEngine(market, config)
    return engine.run(start, end)


def require_parquet(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    load_verified_snapshot(settings.parquet_dir)
    return settings.parquet_dir / "daily_bars.parquet"
