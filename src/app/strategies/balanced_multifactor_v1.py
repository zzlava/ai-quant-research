from __future__ import annotations

from app.features.engine import clip, scale
from app.models.features import StockFeatureVector
from app.models.scores import ScoreBreakdown, ScoreResult, StrategyContext
from app.strategies.base import BaseStrategy
from app.strategies.registry import StrategyRegistry


@StrategyRegistry.register("balanced_value_defensive_v3")
@StrategyRegistry.register("balanced_value_reversal_v2")
@StrategyRegistry.register("balanced_multifactor_v1")
class BalancedMultifactorV1Strategy(BaseStrategy):
    """Balanced PIT ranking with explicit size and sponsorship support."""

    def score(self, feature: StockFeatureVector, context: StrategyContext) -> ScoreResult:
        ranking = self.config.balanced_ranking
        if ranking is None:
            raise ValueError("balanced_multifactor_v1 requires balanced_ranking")
        try:
            quality = float(feature.extra["quality_score"])
            improvement = float(feature.extra["improvement_score"])
            value = float(feature.extra["value_score"])
            size = float(feature.extra["size_score"])
        except KeyError as exc:
            raise ValueError(
                "balanced_multifactor_v1 received an incomplete PIT feature"
            ) from exc

        excess_120d = feature.ret_120d - feature.index_ret_120d
        continuation = (
            0.70 * scale(excess_120d, -0.25, 0.25)
            + 0.30 * scale(feature.ma60_distance, -0.15, 0.15)
        )
        momentum = (
            100.0 - continuation
            if ranking.momentum_style == "reversal"
            else continuation
        )
        institutional_raw = feature.extra.get("institutional_score")
        institutional = (
            float(institutional_raw) if institutional_raw is not None else None
        )
        component_weight = (
            ranking.quality_weight
            + ranking.improvement_weight
            + ranking.value_weight
            + ranking.momentum_weight
            + ranking.size_weight
        )
        weighted_alpha = (
            ranking.quality_weight * quality
            + ranking.improvement_weight * improvement
            + ranking.value_weight * value
            + ranking.momentum_weight * momentum
            + ranking.size_weight * size
        )
        if institutional is not None:
            weighted_alpha += ranking.institutional_weight * institutional
            component_weight += ranking.institutional_weight
        alpha = weighted_alpha / component_weight
        passes_floor = (
            quality >= ranking.min_quality_score
            and improvement >= ranking.min_improvement_score
            and continuation >= ranking.min_continuation_score
        )
        final = (
            clip(
                alpha
                - ranking.crowding_penalty * feature.crowding_risk
                - ranking.execution_penalty * feature.execution_risk
                - ranking.attention_penalty * feature.attention_risk,
                0.0,
                100.0,
            )
            if passes_floor
            else 0.0
        )
        regime = self.config.regime_score(context.market_score, context.global_score)
        breakdown = ScoreBreakdown(
            market_score=context.market_score,
            global_score=context.global_score,
            sector_score=0.0,
            alpha_score=alpha,
            crowding_risk=feature.crowding_risk,
            execution_risk=feature.execution_risk,
            attention_risk=feature.attention_risk,
            quality_score=quality,
            improvement_score=improvement,
            value_score=value,
            momentum_score=momentum,
            size_score=size,
            institutional_score=institutional,
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
