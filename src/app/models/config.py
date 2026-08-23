from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WeightsConfig(StrictModel):
    market_score: float
    global_score: float
    sector_score: float
    alpha_score: float
    crowding_risk: float
    execution_risk: float


class UniverseConfig(StrictModel):
    exclude_st: bool = True
    exclude_suspended: bool = True
    min_listing_days: int = Field(default=120, gt=0)
    min_avg_turnover_20d: float = Field(default=100_000_000, ge=0)


class MarketGateBand(StrictModel):
    min: float
    max: float
    max_new_positions: int = Field(ge=0)

    @model_validator(mode="after")
    def max_gt_min(self) -> MarketGateBand:
        if self.max <= self.min:
            raise ValueError("market_gate band max must be greater than min")
        return self


class TradeConfig(StrictModel):
    take_profit: float = Field(default=0.03, gt=0)
    stop_loss: float = Field(default=-0.025, lt=0)
    max_holding_days: int = Field(default=10, gt=0)
    limit_pct: float = Field(default=0.10, gt=0, le=0.2)
    st_limit_pct: float = Field(default=0.05, gt=0, le=0.2)
    model_limit_moves: bool = True


class PortfolioConfig(StrictModel):
    initial_cash: float = Field(default=80_000, gt=0)
    max_positions: int = Field(default=3, gt=0)
    weighting: Literal["equal_weight"] = "equal_weight"


class CostConfig(StrictModel):
    commission_rate: float = Field(default=0.00025, ge=0)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_tax_rate: float = Field(default=0.0005, ge=0)
    slippage_bps: float = Field(default=5.0, ge=0)


class SessionConfig(StrictModel):
    market: str
    timezone: str
    session_close: str


class DataConfig(StrictModel):
    market_index: str
    global_symbol: str
    adjustment: Literal["forward", "backward", "none"] = "forward"
    decision_timezone: str = "Asia/Shanghai"
    decision_time: str = "15:00"
    min_history_bars: int = Field(default=21, ge=21)
    sessions: dict[str, SessionConfig]


class StrategyConfig(StrictModel):
    name: str
    version: str
    weights: WeightsConfig
    universe: UniverseConfig
    market_gate: list[MarketGateBand]
    trade: TradeConfig
    portfolio: PortfolioConfig
    costs: CostConfig
    data: DataConfig
    source_path: str | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_gate_coverage(self) -> StrategyConfig:
        bands = sorted(self.market_gate, key=lambda item: item.min)
        if not bands:
            raise ValueError("market_gate must contain at least one band")
        if bands[0].min != 0:
            raise ValueError("market_gate must start at min=0")
        for prev, curr in zip(bands, bands[1:], strict=False):
            if curr.min != prev.max:
                raise ValueError("market_gate bands must be contiguous and non-overlapping")
        if bands[-1].max <= 100:
            raise ValueError("market_gate last band max must be greater than 100 so score=100 is included")
        if self.data.market_index not in self.data.sessions:
            raise ValueError(f"data.sessions missing market_index '{self.data.market_index}'")
        if self.data.global_symbol not in self.data.sessions:
            raise ValueError(f"data.sessions missing global_symbol '{self.data.global_symbol}'")
        return self

    def config_hash(self) -> str:
        payload = self.model_dump(mode="json")
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def gate_max_new_positions(self, market_score: float) -> int:
        for band in self.market_gate:
            if band.min <= market_score < band.max:
                return band.max_new_positions
        return 0
