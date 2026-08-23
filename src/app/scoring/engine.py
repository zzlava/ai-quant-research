from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.features.engine import FeatureEngine
from app.models.config import StrategyConfig
from app.models.scores import ScoreResult, StrategyContext
from app.ranking.ranker import rank_scores
from app.storage.protocol import MarketStore

# Ensure built-in strategies register themselves.
from app.strategies import baseline_v1 as _baseline_v1  # noqa: F401
from app.strategies.base import BaseStrategy
from app.strategies.registry import StrategyRegistry
from app.universe.filter import UniverseFilter


class ScoringEngine:
    def __init__(self, store: MarketStore, config: StrategyConfig, strategy: BaseStrategy | None = None) -> None:
        self.store = store
        self.config = config
        self.strategy = strategy or StrategyRegistry.create(config.name, config)
        self.features = FeatureEngine(store)
        self.universe = UniverseFilter(config.universe)

    def run(self, as_of: date) -> list[ScoreResult]:
        raw = self.features.compute_all(as_of)
        filtered = self.universe.apply(raw)
        if not filtered:
            return []
        market_score = filtered[0].market_score
        global_score = filtered[0].global_score
        context = StrategyContext(as_of=as_of, market_score=market_score, global_score=global_score)
        results = [self.strategy.score(feat, context) for feat in filtered]
        return rank_scores(results)

    def persist(self, results: list[ScoreResult], dest: Path) -> None:
        if not results:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "symbol": r.symbol,
                "score_date": r.score_date,
                "strategy_name": r.strategy_name,
                "strategy_version": r.strategy_version,
                "strategy_config_hash": r.strategy_config_hash,
                "final_score": r.final_score,
                "market_score": r.breakdown.market_score,
                "global_score": r.breakdown.global_score,
                "sector_score": r.breakdown.sector_score,
                "alpha_score": r.breakdown.alpha_score,
                "crowding_risk": r.breakdown.crowding_risk,
                "execution_risk": r.breakdown.execution_risk,
                "sector": r.sector,
            }
            for r in results
        ]
        frame = pl.DataFrame(rows)
        if dest.exists():
            existing = pl.read_parquet(dest)
            first = results[0]
            keep = existing.filter(
                ~((pl.col("score_date") == first.score_date) & (pl.col("strategy_name") == first.strategy_name))
            )
            frame = pl.concat([keep, frame], how="vertical_relaxed")
        if results:
            frame.write_parquet(dest)
