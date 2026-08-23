from __future__ import annotations

from app.features.engine import clip, scale
from app.models.features import StockFeatureVector
from app.models.scores import ScoreBreakdown, ScoreResult, StrategyContext
from app.strategies.base import BaseStrategy
from app.strategies.registry import StrategyRegistry


@StrategyRegistry.register("baseline_v1")
class BaselineV1Strategy(BaseStrategy):
    def score(self, feature: StockFeatureVector, context: StrategyContext) -> ScoreResult:
        weights = self.config.weights
        market_score = context.market_score
        global_score = context.global_score
        sector_score = scale(feature.sector_relative_strength, -0.08, 0.08)
        alpha_score = (
            0.40 * scale(feature.stock_relative_strength, -0.10, 0.10)
            + 0.30 * scale(feature.ma20_distance, -0.05, 0.05)
            + 0.30 * scale(feature.ret_20d, -0.10, 0.10)
        )
        crowding = feature.crowding_risk
        execution = feature.execution_risk
        raw = (
            weights.market_score * market_score
            + weights.global_score * global_score
            + weights.sector_score * sector_score
            + weights.alpha_score * alpha_score
            - weights.crowding_risk * crowding
            - weights.execution_risk * execution
        )
        final_score = clip(raw, 0.0, 100.0)
        breakdown = ScoreBreakdown(
            market_score=market_score,
            global_score=global_score,
            sector_score=sector_score,
            alpha_score=alpha_score,
            crowding_risk=crowding,
            execution_risk=execution,
            final_score=final_score,
        )
        return ScoreResult(
            symbol=feature.symbol,
            score_date=context.as_of,
            strategy_name=self.config.name,
            strategy_version=self.config.version,
            strategy_config_hash=self.config.config_hash(),
            final_score=final_score,
            breakdown=breakdown,
            sector=feature.sector,
            feature=feature,
            data_snapshot_id=context.data_snapshot_id,
        )
