from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.models.features import StockFeatureVector


class ScoreBreakdown(BaseModel):
    market_score: float
    global_score: float
    sector_score: float
    alpha_score: float
    crowding_risk: float
    execution_risk: float
    final_score: float
    attention_risk: float = 0.0
    regime_score: float | None = None
    quality_score: float = 0.0
    improvement_score: float = 0.0
    value_score: float = 0.0


class ScoreResult(BaseModel):
    symbol: str
    score_date: date
    strategy_name: str
    config_id: str = ""
    strategy_version: str
    strategy_config_hash: str
    final_score: float
    breakdown: ScoreBreakdown
    sector: str | None = None
    feature: StockFeatureVector | None = None
    data_snapshot_id: str = ""
    research_scope: str = "historical_index"
    research_notice: str | None = None
    reconstruction_data_id: str | None = None


class StrategyContext(BaseModel):
    as_of: date
    market_score: float
    global_score: float
    data_snapshot_id: str = ""
    extras: dict[str, float] = Field(default_factory=dict)
