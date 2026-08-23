from __future__ import annotations

from datetime import date
from pathlib import Path

from app.backtest.engine import BacktestEngine
from app.models.backtest import BacktestResult
from app.models.scores import ScoreResult
from app.providers.demo_provider import DemoProvider
from app.scoring.engine import ScoringEngine
from app.settings import Settings, get_settings
from app.storage.duckdb_store import DuckDBParquetStore
from app.storage.memory import InMemoryStore
from app.storage.protocol import MarketStore
from app.strategies.loader import load_strategy_config


def load_store(settings: Settings | None = None) -> MarketStore:
    settings = settings or get_settings()
    duck = DuckDBParquetStore(settings.parquet_dir)
    if duck.available():
        return duck
    return InMemoryStore.from_provider(DemoProvider())


def run_score(
    as_of: date,
    strategy_name: str,
    settings: Settings | None = None,
    store: MarketStore | None = None,
) -> list[ScoreResult]:
    settings = settings or get_settings()
    config = load_strategy_config(strategy_name, settings.strategies_dir)
    engine = ScoringEngine(store or load_store(settings), config)
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
    engine = BacktestEngine(store or load_store(settings), config)
    return engine.run(start, end)


def require_parquet(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.parquet_dir / "daily_bars.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"missing {path}. Run: python -m app.cli generate-demo"
        )
    return path
