from __future__ import annotations

import hashlib
import json
from datetime import date
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
    attention_risk: float = Field(default=0.0, ge=0)


class UniverseConfig(StrictModel):
    exclude_st: bool = True
    exclude_suspended: bool = True
    min_listing_days: int = Field(default=120, gt=0)
    min_avg_turnover_20d: float = Field(default=100_000_000, ge=0)
    mode: Literal[
        "manual_static",
        "historical_membership",
        "derived_liquid",
        "public_reconstruction",
    ] = "manual_static"
    id: str = Field(default="demo", min_length=1)
    expected_constituents: int | None = Field(default=None, gt=0)


class MarketGateBand(StrictModel):
    min: float
    max: float
    max_new_positions: int = Field(ge=0)

    @model_validator(mode="after")
    def max_gt_min(self) -> MarketGateBand:
        if self.max <= self.min:
            raise ValueError("market_gate band max must be greater than min")
        return self


class RegimeConfig(StrictModel):
    """Common market inputs for exposure control, never cross-sectional rank."""

    market_weight: float = Field(default=1.0, ge=0)
    global_weight: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def has_input_weight(self) -> RegimeConfig:
        if self.market_weight + self.global_weight <= 0:
            raise ValueError("regime requires a positive market_weight or global_weight")
        return self


class RankingConfig(StrictModel):
    """Cross-sectional score. Market-wide inputs are intentionally absent."""

    alpha_style: Literal["momentum_blend", "medium_term_reversal"] = "momentum_blend"
    sector_weight: float = Field(default=0.0, ge=0)
    alpha_weight: float = Field(default=1.0, ge=0)
    crowding_penalty: float = Field(default=0.25, ge=0)
    execution_penalty: float = Field(default=0.25, ge=0)
    attention_penalty: float = Field(default=0.0, ge=0)
    min_score: float = Field(default=0.0, ge=0, le=100)


class FundamentalDataConfig(StrictModel):
    """Point-in-time limits for the separately hashed fundamental overlay."""

    required: bool = True
    revision_policy: Literal[
        "initial_as_announced", "strict_initial_as_announced"
    ] = "initial_as_announced"
    max_report_age_days: int = Field(default=550, gt=0)
    max_valuation_age_days: int = Field(default=10, gt=0)
    min_quality_components: int = Field(default=3, ge=1, le=5)
    min_improvement_components: int = Field(default=1, ge=1, le=3)
    min_value_components: int = Field(default=2, ge=1, le=3)


class FundamentalRankingConfig(StrictModel):
    """Cross-sectional quality, earnings-improvement, and value ranking."""

    quality_weight: float = Field(default=0.45, ge=0)
    improvement_weight: float = Field(default=0.30, ge=0)
    value_weight: float = Field(default=0.25, ge=0)
    crowding_penalty: float = Field(default=0.02, ge=0)
    execution_penalty: float = Field(default=0.05, ge=0)
    attention_penalty: float = Field(default=0.03, ge=0)
    min_quality_score: float = Field(default=0.0, ge=0, le=100)
    min_improvement_score: float = Field(default=0.0, ge=0, le=100)
    min_score: float = Field(default=0.0, ge=0, le=100)

    @model_validator(mode="after")
    def has_fundamental_weight(self) -> FundamentalRankingConfig:
        if self.quality_weight + self.improvement_weight + self.value_weight <= 0:
            raise ValueError("fundamental_ranking requires a positive component weight")
        return self


class OwnershipDataConfig(StrictModel):
    """PIT contract for the separately hashed ownership overlay."""

    required: bool = True
    proxy: Literal["top10_float_non_individual_ratio"] = (
        "top10_float_non_individual_ratio"
    )
    max_report_age_days: int = Field(default=200, gt=0)
    min_complete_holders: int = Field(default=10, ge=1, le=10)
    min_cross_section_coverage: float = Field(default=0.80, gt=0, le=1)
    personal_holder_types: list[str] = Field(
        default_factory=lambda: ["个人", "自然人"], min_length=1
    )

    @model_validator(mode="after")
    def normalized_personal_types(self) -> OwnershipDataConfig:
        values = [item.strip() for item in self.personal_holder_types]
        if any(not item for item in values) or len(values) != len(set(values)):
            raise ValueError("ownership personal_holder_types must be nonblank and unique")
        self.personal_holder_types = values
        return self


class BalancedRankingConfig(StrictModel):
    """Quality/improvement/value/momentum with size and sponsorship support."""

    quality_weight: float = Field(default=0.25, ge=0)
    improvement_weight: float = Field(default=0.20, ge=0)
    value_weight: float = Field(default=0.15, ge=0)
    momentum_weight: float = Field(default=0.20, ge=0)
    size_weight: float = Field(default=0.10, ge=0)
    institutional_weight: float = Field(default=0.10, ge=0)
    momentum_style: Literal["continuation", "reversal"] = "continuation"
    crowding_penalty: float = Field(default=0.02, ge=0)
    execution_penalty: float = Field(default=0.05, ge=0)
    attention_penalty: float = Field(default=0.03, ge=0)
    min_quality_score: float = Field(default=30.0, ge=0, le=100)
    min_improvement_score: float = Field(default=30.0, ge=0, le=100)
    min_continuation_score: float = Field(default=0.0, ge=0, le=100)
    min_score: float = Field(default=50.0, ge=0, le=100)

    @model_validator(mode="after")
    def has_component_weight(self) -> BalancedRankingConfig:
        total = (
            self.quality_weight
            + self.improvement_weight
            + self.value_weight
            + self.momentum_weight
            + self.size_weight
            + self.institutional_weight
        )
        if total <= 0:
            raise ValueError("balanced_ranking requires a positive component weight")
        return self


class TradeConfig(StrictModel):
    exit_policy: Literal["barrier_or_timeout", "fixed_horizon"] = "barrier_or_timeout"
    take_profit: float = Field(default=0.03, gt=0)
    stop_loss: float = Field(default=-0.025, lt=0)
    take_profit_atr: float | None = Field(default=None, gt=0)
    stop_loss_atr: float | None = Field(default=None, gt=0)
    min_holding_days: int = Field(default=1, gt=0)
    max_holding_days: int = Field(default=10, gt=0)
    cooldown_days: int = Field(default=0, ge=0)
    signal_interval_days: int = Field(default=1, gt=0)
    signal_anchor_date: date | None = None
    blocked_entry_policy: Literal["cancel", "defer"] = "cancel"
    max_entry_delay_days: int = Field(default=0, ge=0)
    require_target_lot_affordability: bool = False
    limit_pct: float = Field(default=0.10, gt=0, le=0.2)
    st_limit_pct: float = Field(default=0.05, gt=0, le=0.2)
    model_limit_moves: bool = True

    @model_validator(mode="after")
    def validate_exit_horizon(self) -> TradeConfig:
        if self.min_holding_days > self.max_holding_days:
            raise ValueError("min_holding_days cannot exceed max_holding_days")
        if (self.take_profit_atr is None) != (self.stop_loss_atr is None):
            raise ValueError("take_profit_atr and stop_loss_atr must be configured together")
        if self.exit_policy == "fixed_horizon" and self.min_holding_days != self.max_holding_days:
            raise ValueError("fixed_horizon requires min_holding_days == max_holding_days")
        if self.signal_interval_days > 1 and self.signal_anchor_date is None:
            raise ValueError("signal_interval_days > 1 requires signal_anchor_date")
        if self.blocked_entry_policy == "cancel" and self.max_entry_delay_days != 0:
            raise ValueError("cancel blocked_entry_policy requires max_entry_delay_days=0")
        if self.blocked_entry_policy == "defer" and self.max_entry_delay_days < 1:
            raise ValueError("defer blocked_entry_policy requires max_entry_delay_days>=1")
        return self


class PortfolioConfig(StrictModel):
    initial_cash: float = Field(default=80_000, gt=0)
    max_positions: int = Field(default=3, gt=0)
    weighting: Literal["equal_weight"] = "equal_weight"
    max_pairwise_correlation: float | None = Field(default=None, gt=-1, lt=1)
    correlation_lookback_days: int = Field(default=120, ge=20)


class StampTaxRateBand(StrictModel):
    effective_from: date
    rate: float = Field(ge=0)


class CostConfig(StrictModel):
    commission_rate: float = Field(default=0.00025, ge=0)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_tax_rate: float = Field(default=0.0005, ge=0)
    stamp_tax_schedule: list[StampTaxRateBand] = Field(default_factory=list)
    slippage_bps: float = Field(default=5.0, ge=0)

    @model_validator(mode="after")
    def validate_stamp_tax_schedule(self) -> CostConfig:
        starts = [item.effective_from for item in self.stamp_tax_schedule]
        if starts != sorted(starts) or len(starts) != len(set(starts)):
            raise ValueError("stamp_tax_schedule effective_from values must be strictly increasing")
        return self


class SessionConfig(StrictModel):
    market: str
    timezone: str
    session_close: str


class DataConfig(StrictModel):
    market_index: str
    global_symbol: str
    adjustment: Literal["forward", "backward", "none"] = "forward"
    require_point_in_time_adjustment: bool = False
    decision_timezone: str = "Asia/Shanghai"
    decision_time: str = "15:00"
    min_history_bars: int = Field(default=21, ge=21)
    sessions: dict[str, SessionConfig]


class StrategyConfig(StrictModel):
    name: str
    config_id: str | None = None
    version: str
    research_scope: Literal[
        "historical_index",
        "controlled_sample",
        "latest_market_snapshot",
        "public_reconstruction",
        "historical_all_a_share",
    ] = "historical_index"
    weights: WeightsConfig | None = None
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    ranking: RankingConfig | None = None
    fundamental: FundamentalDataConfig | None = None
    fundamental_ranking: FundamentalRankingConfig | None = None
    ownership: OwnershipDataConfig | None = None
    balanced_ranking: BalancedRankingConfig | None = None
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
        if self.research_scope == "public_reconstruction" and self.universe.mode != "public_reconstruction":
            raise ValueError("public_reconstruction scope requires universe.mode=public_reconstruction")
        if self.research_scope != "public_reconstruction" and self.universe.mode == "public_reconstruction":
            raise ValueError("universe.mode=public_reconstruction requires public_reconstruction scope")
        if self.research_scope == "historical_all_a_share" and self.universe.mode != "derived_liquid":
            raise ValueError("historical_all_a_share scope requires universe.mode=derived_liquid")
        if self.research_scope != "historical_all_a_share" and self.universe.mode == "derived_liquid":
            raise ValueError("universe.mode=derived_liquid requires historical_all_a_share scope")
        if self.name == "baseline_v1" and self.weights is None:
            raise ValueError("baseline_v1 requires legacy weights")
        if self.name == "baseline_v2" and self.ranking is None:
            raise ValueError("baseline_v2 requires ranking")
        if self.name == "quality_value_v1" and (
            self.fundamental is None or self.fundamental_ranking is None
        ):
            raise ValueError(
                "quality_value_v1 requires fundamental and fundamental_ranking configuration"
            )
        if self.name in {"balanced_multifactor_v1", "balanced_value_reversal_v2"} and (
            self.fundamental is None
            or self.ownership is None
            or self.balanced_ranking is None
        ):
            raise ValueError(
                f"{self.name} requires fundamental, ownership, and "
                "balanced_ranking configuration"
            )
        return self

    def run_id(self) -> str:
        return self.config_id or self.name

    def config_hash(self) -> str:
        payload = self.model_dump(mode="json")
        # Preserve hashes for configurations created before these optional
        # controls existed. Non-default experimental choices remain hashed.
        ranking = payload.get("ranking")
        if isinstance(ranking, dict) and ranking.get("alpha_style") == "momentum_blend":
            ranking.pop("alpha_style")
        trade = payload.get("trade")
        if isinstance(trade, dict):
            if trade.get("exit_policy") == "barrier_or_timeout":
                trade.pop("exit_policy")
            if trade.get("signal_interval_days") == 1:
                trade.pop("signal_interval_days")
            if trade.get("signal_anchor_date") is None:
                trade.pop("signal_anchor_date")
            if trade.get("blocked_entry_policy") == "cancel":
                trade.pop("blocked_entry_policy")
            if trade.get("max_entry_delay_days") == 0:
                trade.pop("max_entry_delay_days")
        if payload.get("fundamental") is None:
            payload.pop("fundamental", None)
        if payload.get("fundamental_ranking") is None:
            payload.pop("fundamental_ranking", None)
        if payload.get("ownership") is None:
            payload.pop("ownership", None)
        balanced = payload.get("balanced_ranking")
        if (
            isinstance(balanced, dict)
            and balanced.get("momentum_style") == "continuation"
        ):
            balanced.pop("momentum_style")
        if (
            isinstance(balanced, dict)
            and balanced.get("min_continuation_score") == 0.0
        ):
            balanced.pop("min_continuation_score")
        if payload.get("balanced_ranking") is None:
            payload.pop("balanced_ranking", None)
        portfolio = payload.get("portfolio")
        if isinstance(portfolio, dict) and portfolio.get("max_pairwise_correlation") is None:
            portfolio.pop("max_pairwise_correlation", None)
            if portfolio.get("correlation_lookback_days") == 120:
                portfolio.pop("correlation_lookback_days", None)
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def gate_max_new_positions(self, market_score: float) -> int:
        for band in self.market_gate:
            if band.min <= market_score < band.max:
                return band.max_new_positions
        return 0

    def regime_score(self, market_score: float, global_score: float) -> float:
        total = self.regime.market_weight + self.regime.global_weight
        return (self.regime.market_weight * market_score + self.regime.global_weight * global_score) / total
