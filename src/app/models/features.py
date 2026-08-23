from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class StockFeatureVector(BaseModel):
    """Point-in-time features. All fields must use data available at as_of only."""

    symbol: str
    as_of: date
    sector: str
    close: float

    ret_1d: float
    ret_5d: float
    ret_20d: float

    ma20_distance: float
    ma60_distance: float

    volume_ratio_5d: float
    turnover_rate: float

    volatility_20d: float
    atr_14: float

    stock_relative_strength: float
    sector_relative_strength: float

    market_score: float
    global_score: float

    crowding_risk: float
    execution_risk: float

    avg_turnover_20d: float
    listing_days: int
    is_st: bool
    is_suspended: bool
    attention_risk: float = 0.0
    index_ret_20d: float = 0.0
    global_ret_20d: float = 0.0

    extra: dict[str, float] = Field(default_factory=dict)
