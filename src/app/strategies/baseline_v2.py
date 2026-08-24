from __future__ import annotations

from app.features.engine import clip, scale
from app.models.features import StockFeatureVector
from app.models.scores import ScoreBreakdown, ScoreResult, StrategyContext
from app.strategies.base import BaseStrategy
from app.strategies.registry import StrategyRegistry


@StrategyRegistry.register("baseline_v2")
class BaselineV2Strategy(BaseStrategy):
    """Separate market regime/exposure from cross-sectional stock ranking."""

    def score(self, feature: StockFeatureVector, context: StrategyContext) -> ScoreResult:
        ranking = self.config.ranking
        if ranking is None:  # Defensive; StrategyConfig rejects this at load time.
            raise ValueError("baseline_v2 requires ranking configuration")

        sector_score = scale(feature.sector_relative_strength, -0.08, 0.08)
        momentum_alpha = (
            0.40 * scale(feature.stock_relative_strength, -0.10, 0.10)
            + 0.30 * scale(feature.ma20_distance, -0.05, 0.05)
            + 0.30 * scale(feature.ret_20d, -0.10, 0.10)
        )
        alpha_score = (
            100.0 - momentum_alpha
            if ranking.alpha_style == "medium_term_reversal"
            else momentum_alpha
        )
        crowding = feature.crowding_risk
        execution = feature.execution_risk
        attention = feature.attention_risk
        ranking_score = clip(
            ranking.sector_weight * sector_score
            + ranking.alpha_weight * alpha_score
            - ranking.crowding_penalty * crowding
            - ranking.execution_penalty * execution
            - ranking.attention_penalty * attention,
            0.0,
            100.0,
        )
        regime_score = self.config.regime_score(context.market_score, context.global_score)
        breakdown = ScoreBreakdown(
            market_score=context.market_score,
            global_score=context.global_score,
            sector_score=sector_score,
            alpha_score=alpha_score,
            crowding_risk=crowding,
            execution_risk=execution,
            attention_risk=attention,
            final_score=ranking_score,
            regime_score=regime_score,
        )
        return ScoreResult(
            symbol=feature.symbol,
            score_date=context.as_of,
            strategy_name=self.config.name,
            config_id=self.config.run_id(),
            strategy_version=self.config.version,
            strategy_config_hash=self.config.config_hash(),
            final_score=ranking_score,
            breakdown=breakdown,
            sector=feature.sector,
            feature=feature,
            data_snapshot_id=context.data_snapshot_id,
        )
