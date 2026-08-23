from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field


class WeightsConfig(BaseModel):
    market_score: float
    global_score: float
    sector_score: float
    alpha_score: float
    crowding_risk: float
    execution_risk: float


class UniverseConfig(BaseModel):
    exclude_st: bool = True
    exclude_suspended: bool = True
    min_listing_days: int = 120
    min_avg_turnover_20d: float = 100_000_000


class MarketGateBand(BaseModel):
    min: float
    max: float
    max_new_positions: int


class TradeConfig(BaseModel):
    take_profit: float = 0.03
    stop_loss: float = -0.025
    max_holding_days: int = 10


class PortfolioConfig(BaseModel):
    initial_cash: float = 80_000
    max_positions: int = 3
    weighting: Literal["equal_weight"] = "equal_weight"


class CostConfig(BaseModel):
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    slippage_bps: float = 5.0


class StrategyConfig(BaseModel):
    name: str
    version: str
    weights: WeightsConfig
    universe: UniverseConfig
    market_gate: list[MarketGateBand]
    trade: TradeConfig
    portfolio: PortfolioConfig
    costs: CostConfig
    source_path: str | None = Field(default=None, exclude=True)

    def config_hash(self) -> str:
        payload = self.model_dump(mode="json")
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def gate_max_new_positions(self, market_score: float) -> int:
        for band in self.market_gate:
            if band.min <= market_score < band.max:
                return band.max_new_positions
        return 0
