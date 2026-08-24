from __future__ import annotations

from datetime import date
from pathlib import Path

from app.backtest.engine import BacktestEngine
from app.errors import PreflightError
from app.models.backtest import BacktestResult
from app.models.scores import ScoreResult
from app.preflight import preflight_research
from app.research_scope import PUBLIC_RECONSTRUCTION_SCOPE
from app.scoring.engine import ScoringEngine
from app.settings import Settings, get_settings
from app.storage.duckdb_store import DuckDBParquetStore
from app.storage.protocol import MarketStore
from app.storage.snapshot_io import load_verified_snapshot
from app.strategies.loader import load_strategy_config
from app.universe.public_replay import PublicReconstructionStore, load_public_reconstruction_pack


def load_store(settings: Settings | None = None) -> MarketStore:
    settings = settings or get_settings()
    snapshot = load_verified_snapshot(settings.parquet_dir)
    return DuckDBParquetStore(settings.parquet_dir, snapshot=snapshot)


def load_research_store(settings: Settings, config_name: str, base_store: MarketStore | None = None) -> MarketStore:
    """Load the normal verified snapshot and only then apply a declared overlay."""
    config = load_strategy_config(config_name, settings.strategies_dir)
    market = base_store or load_store(settings)
    if config.research_scope == PUBLIC_RECONSTRUCTION_SCOPE:
        if config.universe.expected_constituents is None:
            raise PreflightError("public_reconstruction requires universe.expected_constituents")
        if settings.public_reconstruction_dir is None:
            raise PreflightError(
                "public_reconstruction requires AIQ_PUBLIC_RECONSTRUCTION_DIR; "
                "it must point to a verified BigQuant collection directory"
            )
        pack = load_public_reconstruction_pack(
            settings.public_reconstruction_dir,
            expected_constituents=config.universe.expected_constituents,
        )
        market = PublicReconstructionStore(market, pack, universe_id=config.universe.id)
    if config.fundamental is not None:
        from app.storage.fundamental_io import load_verified_fundamental_snapshot
        from app.storage.fundamental_overlay import FundamentalOverlayStore

        if settings.fundamental_dir is None:
            raise PreflightError(
                "fundamental strategy requires AIQ_FUNDAMENTAL_DIR pointing to a verified overlay"
            )
        fundamental_snapshot, tables = load_verified_fundamental_snapshot(settings.fundamental_dir)
        if (
            config.research_scope == "historical_all_a_share"
            and fundamental_snapshot.base_market_snapshot_id is None
        ):
            raise PreflightError(
                "historical_all_a_share requires a fundamental overlay bound to the exact market snapshot"
            )
        market = FundamentalOverlayStore(market, fundamental_snapshot, tables)
    return market


def run_score(
    as_of: date,
    strategy_name: str,
    settings: Settings | None = None,
    store: MarketStore | None = None,
) -> list[ScoreResult]:
    settings = settings or get_settings()
    config = load_strategy_config(strategy_name, settings.strategies_dir)
    market = load_research_store(settings, strategy_name, base_store=store)
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
    market = load_research_store(settings, strategy_name, base_store=store)
    preflight_research(store=market, config=config, start=start, end=end)
    engine = BacktestEngine(market, config)
    return engine.run(start, end)


def require_parquet(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    load_verified_snapshot(settings.parquet_dir)
    return settings.parquet_dir / "daily_bars.parquet"
