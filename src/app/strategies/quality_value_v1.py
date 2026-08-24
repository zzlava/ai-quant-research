from __future__ import annotations

from app.features.engine import clip
from app.models.features import StockFeatureVector
from app.models.scores import ScoreBreakdown, ScoreResult, StrategyContext
from app.strategies.base import BaseStrategy
from app.strategies.registry import StrategyRegistry


@StrategyRegistry.register("quality_value_v1")
class QualityValueV1Strategy(BaseStrategy):
    """Fundamentals rank stocks; technical data only constrains risk/execution."""

    def score(self, feature: StockFeatureVector, context: StrategyContext) -> ScoreResult:
        ranking = self.config.fundamental_ranking
        if ranking is None:
            raise ValueError("quality_value_v1 requires fundamental_ranking")
        try:
            quality = float(feature.extra["quality_score"])
            improvement = float(feature.extra["improvement_score"])
            value = float(feature.extra["value_score"])
        except KeyError as exc:
            raise ValueError("quality_value_v1 received an incomplete fundamental feature") from exc
        passes_fundamental_floor = (
            quality >= ranking.min_quality_score
            and improvement >= ranking.min_improvement_score
        )
        weight = ranking.quality_weight + ranking.improvement_weight + ranking.value_weight
        fundamental_score = (
            ranking.quality_weight * quality
            + ranking.improvement_weight * improvement
            + ranking.value_weight * value
        ) / weight
        final = (
            clip(
                fundamental_score
                - ranking.crowding_penalty * feature.crowding_risk
                - ranking.execution_penalty * feature.execution_risk
                - ranking.attention_penalty * feature.attention_risk,
                0.0,
                100.0,
            )
            if passes_fundamental_floor
            else 0.0
        )
        regime = self.config.regime_score(context.market_score, context.global_score)
        breakdown = ScoreBreakdown(
            market_score=context.market_score,
            global_score=context.global_score,
            sector_score=0.0,
            alpha_score=fundamental_score,
            crowding_risk=feature.crowding_risk,
            execution_risk=feature.execution_risk,
            attention_risk=feature.attention_risk,
            quality_score=quality,
            improvement_score=improvement,
            value_score=value,
            final_score=final,
            regime_score=regime,
        )
        return ScoreResult(
            symbol=feature.symbol,
            score_date=context.as_of,
            strategy_name=self.config.name,
            config_id=self.config.run_id(),
            strategy_version=self.config.version,
            strategy_config_hash=self.config.config_hash(),
            final_score=final,
            breakdown=breakdown,
            sector=feature.sector,
            feature=feature,
            data_snapshot_id=context.data_snapshot_id,
        )
