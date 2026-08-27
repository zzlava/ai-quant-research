"""Two-layer strategy decision contract: v1 (legacy sealed) + v2 (confirmed).

Schema v1 drafts remain verifiable for historical sealed reports. Schema v2 records
user-confirmed economic decisions with categorized evidence blockers and status
confirmed_for_implementation_but_not_ready. Ready flags stay false; this module does
not score, backtest, trade, or invent index symbols.

For schema v2, ``user_decisions_resolved`` may be true while overall ``resolved`` stays
false whenever evidence blockers remain or status/ready gates are not-ready (fail-closed).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal, overload

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.experiment_ledger import verify_research_trial_ledger

TWO_LAYER_DECISION_SCHEMA_VERSION_V1: Literal["1"] = "1"
TWO_LAYER_DECISION_SCHEMA_VERSION_V2: Literal["2"] = "2"
TWO_LAYER_DECISION_SCHEMA_VERSION = TWO_LAYER_DECISION_SCHEMA_VERSION_V2
TWO_LAYER_DECISION_CONTRACT_VERSION_V1: Literal["two-layer-strategy-decision-draft-v1"] = (
    "two-layer-strategy-decision-draft-v1"
)
TWO_LAYER_DECISION_CONTRACT_VERSION_V2: Literal["two-layer-strategy-decision-v2"] = "two-layer-strategy-decision-v2"
TWO_LAYER_DECISION_CONTRACT_VERSION = TWO_LAYER_DECISION_CONTRACT_VERSION_V2
DEFAULT_TWO_LAYER_DECISION_DRAFT_PATH = Path("config/research/two-layer-strategy-decision-draft-v1.json")
DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH = DEFAULT_TWO_LAYER_DECISION_DRAFT_PATH
CONFIRMED_INITIAL_CASH: Literal[80000] = 80000
BOUND_RESEARCH_TRIAL_LEDGER_ID = "1fc944251212da4972a087b4c54263912d621e43ad400b5936d6a492f1f9b9f4"
BOUND_RESEARCH_TRIAL_LEDGER_PATH: Literal["config/research/research-trial-ledger-v1.json"] = (
    "config/research/research-trial-ledger-v1.json"
)
CONTRACT_CONFIRMATION_AS_OF: date = date(2026, 8, 26)

LayerOneObjective = Literal["absolute_return", "benchmark_relative"]
OwnershipProxyRole = Literal["scoring", "diagnostic", "exclusion"]
SuspensionHoldingDayClock = Literal["count_suspended_days", "pause_holding_clock"]
DraftStatusV1 = Literal["blocked_pending_user_decisions"]
ContractStatusV2 = Literal["confirmed_for_implementation_but_not_ready"]
BlockerCategory = Literal[
    "pending_user_decision",
    "pending_factual_source_verification",
    "pending_implementation",
    "pending_development_evidence",
    "future_enhancement",
]
IndexSymbolStatus = Literal["confirmed", "pending_factual_source_verification"]

_BUDGET_ABS_TOL = 1e-12

# Confirmed layer-two position / active-tranche caps (budget fraction keys as strings).
CONFIRMED_MAX_POSITIONS_BY_BUDGET: dict[str, int] = {"0.3": 3, "0.6": 6, "0.9": 9}
CONFIRMED_ABSOLUTE_MAX_POSITIONS: Literal[9] = 9
CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS: Literal[40] = 40

# Legacy v1 null-field blocker paths (stable order). Used only for schema v1.
REQUIRED_DECISION_PATHS: tuple[str, ...] = (
    "layer_one.objective",
    "layer_one.primary_benchmark",
    "layer_one.cash_asset_scope",
    "layer_one.etf_asset_scope",
    "layer_one.max_acceptable_drawdown",
    "layer_one.min_stock_budget",
    "layer_one.max_stock_budget",
    "layer_one.risk_budget_levels",
    "layer_one.trend_lookback",
    "layer_one.volatility_lookback",
    "layer_one.volatility_target",
    "layer_two.pit_industry_source_requirement",
    "layer_two.statistical_risk_cluster_lookback",
    "layer_two.statistical_risk_cluster_correlation_threshold",
    "layer_two.statistical_risk_cluster_max_weight",
    "layer_two.max_positions_per_cluster",
    "layer_two.ownership_proxy_role",
    "layer_two.max_positions",
    "layer_two.holding_period_bars",
    "layer_two.tranche_count",
    "layer_two.rebalance_semantics",
    "layer_two.exit_semantics",
    "execution.suspension_holding_day_clock",
    "execution.delisting_settlement_contract",
    "execution.minimum_commission_lot_handling_policy",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _reject_blank_string(value: object, *, field_name: str) -> object:
    if isinstance(value, str) and value.strip() == "":
        raise ValueError(f"{field_name} must be null when unknown, not empty string")
    return value


def _finite_number(value: float, *, field_name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return value


def _require_exact_float(value: float, expected: float, *, field_name: str) -> float:
    value = _finite_number(value, field_name=field_name)
    if abs(value - expected) > _BUDGET_ABS_TOL:
        raise ValueError(f"{field_name} must equal {expected}")
    return expected


def _stock_budget_may_be_below_one(
    *,
    min_stock_budget: float | None,
    max_stock_budget: float | None,
    risk_budget_levels: list[float] | None,
) -> bool:
    if min_stock_budget is not None and min_stock_budget < 1.0 - _BUDGET_ABS_TOL:
        return True
    if max_stock_budget is not None and max_stock_budget < 1.0 - _BUDGET_ABS_TOL:
        return True
    if risk_budget_levels is not None:
        for level in risk_budget_levels:
            if level < 1.0 - _BUDGET_ABS_TOL:
                return True
    return False


def _assert_asset_scope_items(scope: list[str], *, field_name: str) -> None:
    for item in scope:
        if not isinstance(item, str) or item.strip() == "":
            raise ValueError(f"{field_name} entries must be non-empty strings")
    if len(set(scope)) != len(scope):
        raise ValueError(f"{field_name} entries must be unique")


def _assert_layer_one_cross_field_consistency(
    *,
    cash_asset_scope: list[str] | None,
    etf_asset_scope: list[str] | None,
    min_stock_budget: float | None,
    max_stock_budget: float | None,
    risk_budget_levels: list[float] | None,
) -> None:
    if cash_asset_scope is not None:
        _assert_asset_scope_items(cash_asset_scope, field_name="cash_asset_scope")
    if etf_asset_scope is not None:
        _assert_asset_scope_items(etf_asset_scope, field_name="etf_asset_scope")

    if min_stock_budget is not None and max_stock_budget is not None:
        if min_stock_budget > max_stock_budget:
            raise ValueError("min_stock_budget must be <= max_stock_budget")
        if risk_budget_levels is not None:
            low = min_stock_budget
            high = max_stock_budget
            for level in risk_budget_levels:
                if level < low - _BUDGET_ABS_TOL or level > high + _BUDGET_ABS_TOL:
                    raise ValueError("risk_budget_levels entries must lie within [min_stock_budget, max_stock_budget]")
            if abs(risk_budget_levels[0] - min_stock_budget) > _BUDGET_ABS_TOL:
                raise ValueError("risk_budget_levels first entry must equal min_stock_budget")
            if abs(risk_budget_levels[-1] - max_stock_budget) > _BUDGET_ABS_TOL:
                raise ValueError("risk_budget_levels last entry must equal max_stock_budget")

    if cash_asset_scope is not None and etf_asset_scope is not None:
        if _stock_budget_may_be_below_one(
            min_stock_budget=min_stock_budget,
            max_stock_budget=max_stock_budget,
            risk_budget_levels=risk_budget_levels,
        ):
            if len(cash_asset_scope) == 0 and len(etf_asset_scope) == 0:
                raise ValueError(
                    "cash_asset_scope or etf_asset_scope must be non-empty when stock budget may be below 1"
                )


def _assert_layer_two_cross_field_consistency(
    *,
    max_positions_per_cluster: int | None,
    max_positions: int | None,
) -> None:
    if max_positions_per_cluster is not None and max_positions is not None:
        if max_positions_per_cluster > max_positions:
            raise ValueError("max_positions_per_cluster must be <= max_positions")


def _assert_confirmed_budget_position_map(mapping: dict[str, int], *, field_name: str) -> None:
    if mapping != CONFIRMED_MAX_POSITIONS_BY_BUDGET:
        raise ValueError(
            f"{field_name} must equal {CONFIRMED_MAX_POSITIONS_BY_BUDGET!r} (budget 0.3/0.6/0.9 -> 3/6/9 active slots)"
        )
    if max(mapping.values()) > CONFIRMED_ABSOLUTE_MAX_POSITIONS:
        raise ValueError(f"{field_name} values must be <= absolute max {CONFIRMED_ABSOLUTE_MAX_POSITIONS}")


def _assert_v2_tranche_position_consistency(
    *,
    position_sizing: PositionSizingPolicyConfirmed,
    tranche_hold: TrancheHoldPolicyConfirmed,
) -> None:
    """Active tranche caps must track position caps; 40 is cycle length, not active count."""
    _assert_confirmed_budget_position_map(
        position_sizing.max_positions_by_budget,
        field_name="position_sizing.max_positions_by_budget",
    )
    _assert_confirmed_budget_position_map(
        tranche_hold.max_active_tranches_by_budget,
        field_name="tranche_hold.max_active_tranches_by_budget",
    )
    if tranche_hold.max_active_tranches_by_budget != position_sizing.max_positions_by_budget:
        raise ValueError("max_active_tranches_by_budget must equal max_positions_by_budget")
    if tranche_hold.absolute_max_active_tranches != position_sizing.absolute_max_positions:
        raise ValueError("absolute_max_active_tranches must equal absolute_max_positions")
    if position_sizing.absolute_max_positions != CONFIRMED_ABSOLUTE_MAX_POSITIONS:
        raise ValueError(f"absolute_max_positions must equal {CONFIRMED_ABSOLUTE_MAX_POSITIONS}")
    if tranche_hold.absolute_max_active_tranches != CONFIRMED_ABSOLUTE_MAX_POSITIONS:
        raise ValueError(f"absolute_max_active_tranches must equal {CONFIRMED_ABSOLUTE_MAX_POSITIONS}")
    if tranche_hold.holding_cycle_market_trading_days != tranche_hold.holding_period_market_trading_days:
        raise ValueError(
            "holding_cycle_market_trading_days must equal holding_period_market_trading_days under uniform stagger"
        )
    if tranche_hold.holding_cycle_market_trading_days != CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS:
        raise ValueError(f"holding_cycle_market_trading_days must equal {CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS}")
    # Guard against remapping the cycle length (40) as an active-tranche count.
    for count in tranche_hold.max_active_tranches_by_budget.values():
        if count == tranche_hold.holding_cycle_market_trading_days:
            raise ValueError(
                "active tranche counts must not equal holding_cycle_market_trading_days; "
                "40 is the holding/phase cycle length, not the active tranche count"
            )
        if count > tranche_hold.absolute_max_active_tranches:
            raise ValueError("active tranche count exceeds absolute_max_active_tranches")
    if not tranche_hold.active_tranche_count_equals_active_target_position_count:
        raise ValueError("active tranche count must equal active target position count")
    if not tranche_hold.one_stock_per_tranche:
        raise ValueError("one_stock_per_tranche (one stock per active tranche) must remain true")


def _assert_repo_relative_ledger_path(value: str, *, repo_root: Path) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError("research_trial_ledger_path must be a relative path without parent traversal")
    if value != BOUND_RESEARCH_TRIAL_LEDGER_PATH:
        raise ValueError("research_trial_ledger_path does not match bound research trial ledger path")
    resolved = (repo_root / path).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("research_trial_ledger_path escapes repository root") from exc
    if not resolved.is_file():
        raise ValueError(f"research_trial_ledger_path does not exist: {value}")
    return resolved


def _validate_ledger_path_field(value: object) -> object:
    if value is None:
        raise ValueError("research_trial_ledger_path must be the bound repo-relative path")
    if not isinstance(value, str):
        raise ValueError("research_trial_ledger_path must be a string")
    if value.strip() == "":
        raise ValueError("research_trial_ledger_path must not be empty")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("research_trial_ledger_path must be a relative path without parent traversal")
    if value != BOUND_RESEARCH_TRIAL_LEDGER_PATH:
        raise ValueError("research_trial_ledger_path does not match bound research trial ledger path")
    return value


class CategorizedBlocker(_StrictModel):
    path: str = Field(min_length=1)
    category: BlockerCategory
    detail: str = Field(min_length=1)

    @field_validator("path", "detail", mode="before")
    @classmethod
    def _reject_blank(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)


class ConfirmedTwoLayerDecisions(_StrictModel):
    initial_cash: Literal[80000] = CONFIRMED_INITIAL_CASH
    initial_cash_confirmed: Literal[True] = True
    initial_cash_is_blocker: Literal[False] = False
    note: str = "initial_cash=80000 is confirmed for account-scale research and is not a pending user decision blocker."


class ConsumedOosReusePolicy(_StrictModel):
    reuse_forbidden: Literal[True] = True
    note: str = (
        "Consumed one-shot OOS windows are terminal. They must not be reused to "
        "select parameters, re-run evaluations, or authorize new strategy configs."
    )


# ---------------------------------------------------------------------------
# Schema v1 (legacy sealed drafts)
# ---------------------------------------------------------------------------


class LayerOnePendingDecisions(_StrictModel):
    objective: LayerOneObjective | None = None
    primary_benchmark: str | None = None
    cash_asset_scope: list[str] | None = None
    etf_asset_scope: list[str] | None = None
    max_acceptable_drawdown: float | None = None
    min_stock_budget: float | None = None
    max_stock_budget: float | None = None
    risk_budget_levels: list[float] | None = None
    trend_lookback: int | None = None
    volatility_lookback: int | None = None
    volatility_target: float | None = None

    @field_validator("primary_benchmark", mode="before")
    @classmethod
    def _reject_blank_benchmark(cls, value: object) -> object:
        return _reject_blank_string(value, field_name="primary_benchmark")

    @field_validator("cash_asset_scope", "etf_asset_scope", mode="before")
    @classmethod
    def _reject_unknown_list_masquerades(cls, value: object, info: Any) -> object:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError("asset scope must be a list or null")
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("asset scope entries must be non-empty strings")
            cleaned.append(item)
        _assert_asset_scope_items(cleaned, field_name=info.field_name)
        return cleaned

    @field_validator("max_acceptable_drawdown")
    @classmethod
    def _validate_drawdown(cls, value: float | None) -> float | None:
        if value is None:
            return None
        value = _finite_number(value, field_name="max_acceptable_drawdown")
        if not (-1.0 < value <= 0.0):
            raise ValueError("max_acceptable_drawdown must be in (-1, 0]")
        return value

    @field_validator("min_stock_budget", "max_stock_budget", "volatility_target")
    @classmethod
    def _validate_unit_interval(cls, value: float | None, info: Any) -> float | None:
        if value is None:
            return None
        value = _finite_number(value, field_name=info.field_name)
        if not (0.0 <= value <= 1.0):
            raise ValueError(f"{info.field_name} must be in [0, 1]")
        return value

    @field_validator("trend_lookback", "volatility_lookback")
    @classmethod
    def _validate_positive_lookback(cls, value: int | None, info: Any) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise ValueError(f"{info.field_name} must be >= 1")
        return value

    @field_validator("risk_budget_levels")
    @classmethod
    def _validate_risk_budget_levels(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return None
        if len(value) < 2:
            raise ValueError("risk_budget_levels must contain at least two levels when set")
        cleaned: list[float] = []
        for level in value:
            level = _finite_number(float(level), field_name="risk_budget_levels")
            if not (0.0 <= level <= 1.0):
                raise ValueError("risk_budget_levels entries must be in [0, 1]")
            cleaned.append(level)
        for previous, current in zip(cleaned, cleaned[1:], strict=False):
            if not current > previous:
                raise ValueError("risk_budget_levels must be strictly increasing")
        return cleaned

    @model_validator(mode="after")
    def _validate_budget_bounds(self) -> LayerOnePendingDecisions:
        _assert_layer_one_cross_field_consistency(
            cash_asset_scope=self.cash_asset_scope,
            etf_asset_scope=self.etf_asset_scope,
            min_stock_budget=self.min_stock_budget,
            max_stock_budget=self.max_stock_budget,
            risk_budget_levels=self.risk_budget_levels,
        )
        return self


class LayerTwoPendingDecisions(_StrictModel):
    pit_industry_source_requirement: str | None = None
    statistical_risk_cluster_lookback: int | None = None
    statistical_risk_cluster_correlation_threshold: float | None = None
    statistical_risk_cluster_max_weight: float | None = None
    max_positions_per_cluster: int | None = None
    ownership_proxy_role: OwnershipProxyRole | None = None
    max_positions: int | None = None
    holding_period_bars: int | None = None
    tranche_count: int | None = None
    rebalance_semantics: str | None = None
    exit_semantics: str | None = None

    @field_validator(
        "pit_industry_source_requirement",
        "rebalance_semantics",
        "exit_semantics",
        mode="before",
    )
    @classmethod
    def _reject_blank_strings(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)

    @field_validator("statistical_risk_cluster_lookback")
    @classmethod
    def _validate_cluster_lookback(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 2:
            raise ValueError("statistical_risk_cluster_lookback must be >= 2")
        return value

    @field_validator("statistical_risk_cluster_correlation_threshold")
    @classmethod
    def _validate_correlation(cls, value: float | None) -> float | None:
        if value is None:
            return None
        value = _finite_number(value, field_name="statistical_risk_cluster_correlation_threshold")
        if not (0.0 < value <= 1.0):
            raise ValueError("statistical_risk_cluster_correlation_threshold must be in (0, 1]")
        return value

    @field_validator("statistical_risk_cluster_max_weight")
    @classmethod
    def _validate_cluster_max_weight(cls, value: float | None) -> float | None:
        if value is None:
            return None
        value = _finite_number(value, field_name="statistical_risk_cluster_max_weight")
        if not (0.0 < value <= 1.0):
            raise ValueError("statistical_risk_cluster_max_weight must be in (0, 1]")
        return value

    @field_validator("max_positions_per_cluster", "max_positions", "holding_period_bars", "tranche_count")
    @classmethod
    def _validate_positive_ints(cls, value: int | None, info: Any) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise ValueError(f"{info.field_name} must be >= 1")
        return value

    @model_validator(mode="after")
    def _validate_position_caps(self) -> LayerTwoPendingDecisions:
        _assert_layer_two_cross_field_consistency(
            max_positions_per_cluster=self.max_positions_per_cluster,
            max_positions=self.max_positions,
        )
        return self


class ExecutionPendingDecisions(_StrictModel):
    suspension_holding_day_clock: SuspensionHoldingDayClock | None = None
    delisting_settlement_contract: str | None = None
    minimum_commission_lot_handling_policy: str | None = None

    @field_validator(
        "delisting_settlement_contract",
        "minimum_commission_lot_handling_policy",
        mode="before",
    )
    @classmethod
    def _reject_blank_strings(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)


class TwoLayerStrategyDecisionDraftV1(_StrictModel):
    schema_version: Literal["1"] = TWO_LAYER_DECISION_SCHEMA_VERSION_V1
    contract_version: Literal["two-layer-strategy-decision-draft-v1"] = TWO_LAYER_DECISION_CONTRACT_VERSION_V1
    status: DraftStatusV1 = "blocked_pending_user_decisions"
    research_trial_ledger_id: str = Field(min_length=1)
    research_trial_ledger_path: Literal["config/research/research-trial-ledger-v1.json"] = (
        BOUND_RESEARCH_TRIAL_LEDGER_PATH
    )
    confirmed: ConfirmedTwoLayerDecisions = Field(default_factory=ConfirmedTwoLayerDecisions)
    consumed_oos: ConsumedOosReusePolicy = Field(default_factory=ConsumedOosReusePolicy)
    layer_one: LayerOnePendingDecisions
    layer_two: LayerTwoPendingDecisions
    execution: ExecutionPendingDecisions
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    contract_id: str | None = None

    @field_validator("research_trial_ledger_id", mode="before")
    @classmethod
    def _reject_blank_ledger_id(cls, value: object) -> object:
        return _reject_blank_string(value, field_name="research_trial_ledger_id")

    @field_validator("research_trial_ledger_path", mode="before")
    @classmethod
    def _reject_blank_or_escape_ledger_path(cls, value: object) -> object:
        return _validate_ledger_path_field(value)

    @model_validator(mode="after")
    def _validate_gate_flags(self) -> TwoLayerStrategyDecisionDraftV1:
        if self.status != "blocked_pending_user_decisions":
            raise ValueError("draft status must remain blocked_pending_user_decisions")
        if self.ready_for_scoring or self.ready_for_backtest or self.ready_for_trading or self.auto_deploy:
            raise ValueError("two-layer decision draft cannot authorize scoring, backtest, trading, or deploy")
        if self.confirmed.initial_cash != CONFIRMED_INITIAL_CASH:
            raise ValueError("confirmed initial_cash must remain 80000")
        if not self.consumed_oos.reuse_forbidden:
            raise ValueError("consumed OOS reuse must remain forbidden")
        return self


# Backward-compatible alias used by older imports/tests.
TwoLayerStrategyDecisionDraft = TwoLayerStrategyDecisionDraftV1


# ---------------------------------------------------------------------------
# Schema v2 (confirmed economic decisions)
# ---------------------------------------------------------------------------


class IndexIdentityConfirmed(_StrictModel):
    role: Literal["performance_comparison", "market_risk_state"]
    name: str
    return_definition: Literal["total_return", "price_index"]
    market_data_primary_source: Literal["tushare"] = "tushare"
    identity_cross_check: Literal["csi_index_official_website"] = "csi_index_official_website"
    symbol: str | None = None
    symbol_status: IndexSymbolStatus = "pending_factual_source_verification"
    note: str

    @field_validator("name", "note", mode="before")
    @classmethod
    def _reject_blank(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)

    @field_validator("symbol", mode="before")
    @classmethod
    def _reject_blank_symbol(cls, value: object) -> object:
        return _reject_blank_string(value, field_name="symbol")

    @model_validator(mode="after")
    def _symbol_status_consistency(self) -> IndexIdentityConfirmed:
        if self.symbol_status == "confirmed" and self.symbol is None:
            raise ValueError("confirmed index symbol cannot be null")
        if self.symbol_status == "pending_factual_source_verification" and self.symbol is not None:
            raise ValueError("pending factual index symbol must remain null until verified")
        return self


class TrendPolicyConfirmed(_StrictModel):
    lookback_trading_days: Literal[200] = 200
    neutral_band_pct: float = 0.03
    base_budget_positive_trend: float = 0.9
    base_budget_neutral_trend: float = 0.6
    base_budget_negative_trend: float = 0.3

    @model_validator(mode="after")
    def _freeze(self) -> TrendPolicyConfirmed:
        self.neutral_band_pct = _require_exact_float(self.neutral_band_pct, 0.03, field_name="neutral_band_pct")
        self.base_budget_positive_trend = _require_exact_float(
            self.base_budget_positive_trend, 0.9, field_name="base_budget_positive_trend"
        )
        self.base_budget_neutral_trend = _require_exact_float(
            self.base_budget_neutral_trend, 0.6, field_name="base_budget_neutral_trend"
        )
        self.base_budget_negative_trend = _require_exact_float(
            self.base_budget_negative_trend, 0.3, field_name="base_budget_negative_trend"
        )
        return self


class VolatilityPolicyConfirmed(_StrictModel):
    lookback_trading_days: Literal[60] = 60
    annualization_trading_days_per_year: Literal[242] = 242
    target: float = 0.18
    # Caps map realized annualized vol thresholds 0.18/0.27/0.36 to no-cap / 0.6 / 0.3 / 0.
    no_cap_at_or_below: float = 0.18
    cap_0_6_above_through: float = 0.27
    cap_0_3_above_through: float = 0.36
    force_zero_above: float = 0.36
    budget_when_at_or_below_no_cap_threshold: Literal["no_additional_cap"] = "no_additional_cap"
    budget_when_in_cap_0_6_band: float = 0.6
    budget_when_in_cap_0_3_band: float = 0.3
    budget_when_above_force_zero: float = 0.0

    @model_validator(mode="after")
    def _freeze(self) -> VolatilityPolicyConfirmed:
        self.target = _require_exact_float(self.target, 0.18, field_name="target")
        self.no_cap_at_or_below = _require_exact_float(self.no_cap_at_or_below, 0.18, field_name="no_cap_at_or_below")
        self.cap_0_6_above_through = _require_exact_float(
            self.cap_0_6_above_through, 0.27, field_name="cap_0_6_above_through"
        )
        self.cap_0_3_above_through = _require_exact_float(
            self.cap_0_3_above_through, 0.36, field_name="cap_0_3_above_through"
        )
        self.force_zero_above = _require_exact_float(self.force_zero_above, 0.36, field_name="force_zero_above")
        self.budget_when_in_cap_0_6_band = _require_exact_float(
            self.budget_when_in_cap_0_6_band, 0.6, field_name="budget_when_in_cap_0_6_band"
        )
        self.budget_when_in_cap_0_3_band = _require_exact_float(
            self.budget_when_in_cap_0_3_band, 0.3, field_name="budget_when_in_cap_0_3_band"
        )
        self.budget_when_above_force_zero = _require_exact_float(
            self.budget_when_above_force_zero, 0.0, field_name="budget_when_above_force_zero"
        )
        return self


class IndexDrawdownPolicyConfirmed(_StrictModel):
    lookback_trading_days: Literal[242] = 242
    # Caps map index drawdown -10%/-15%/-20% to stock budget 0.6 / 0.3 / 0.
    no_cap_shallower_than: float = -0.10
    cap_0_6_at_or_beyond: float = -0.10
    cap_0_3_at_or_beyond: float = -0.15
    force_zero_at_or_beyond: float = -0.20
    budget_when_in_cap_0_6_band: float = 0.6
    budget_when_in_cap_0_3_band: float = 0.3
    budget_when_at_or_beyond_force_zero: float = 0.0

    @model_validator(mode="after")
    def _freeze(self) -> IndexDrawdownPolicyConfirmed:
        self.no_cap_shallower_than = _require_exact_float(
            self.no_cap_shallower_than, -0.10, field_name="no_cap_shallower_than"
        )
        self.cap_0_6_at_or_beyond = _require_exact_float(
            self.cap_0_6_at_or_beyond, -0.10, field_name="cap_0_6_at_or_beyond"
        )
        self.cap_0_3_at_or_beyond = _require_exact_float(
            self.cap_0_3_at_or_beyond, -0.15, field_name="cap_0_3_at_or_beyond"
        )
        self.force_zero_at_or_beyond = _require_exact_float(
            self.force_zero_at_or_beyond, -0.20, field_name="force_zero_at_or_beyond"
        )
        self.budget_when_in_cap_0_6_band = _require_exact_float(
            self.budget_when_in_cap_0_6_band, 0.6, field_name="budget_when_in_cap_0_6_band"
        )
        self.budget_when_in_cap_0_3_band = _require_exact_float(
            self.budget_when_in_cap_0_3_band, 0.3, field_name="budget_when_in_cap_0_3_band"
        )
        self.budget_when_at_or_beyond_force_zero = _require_exact_float(
            self.budget_when_at_or_beyond_force_zero, 0.0, field_name="budget_when_at_or_beyond_force_zero"
        )
        return self


class AccountDrawdownPolicyConfirmed(_StrictModel):
    max_acceptable_drawdown: float = -0.20
    cap_0_6_at_or_beyond: float = -0.10
    cap_0_3_at_or_beyond: float = -0.15
    risk_lock_at_or_beyond: float = -0.18
    final_red_line: float = -0.20
    budget_when_in_cap_0_6_band: float = 0.6
    budget_when_in_cap_0_3_band: float = 0.3
    risk_lock_effects: list[str] = Field(
        default_factory=lambda: [
            "prohibit_new_entries",
            "target_stock_budget_zero",
            "prominent_ui_and_output_annotation_required",
            "service_restart_must_not_auto_clear",
        ]
    )
    risk_lock_recovery: dict[str, Any] = Field(
        default_factory=lambda: {
            "min_cooling_trading_days": 20,
            "index_must_not_be_negative_trend": True,
            "realized_vol_60d_annualized_must_be_below": 0.27,
            "requires_explicit_user_confirmation": True,
            "auto_clear_forbidden": True,
        }
    )

    @model_validator(mode="after")
    def _freeze(self) -> AccountDrawdownPolicyConfirmed:
        self.max_acceptable_drawdown = _require_exact_float(
            self.max_acceptable_drawdown, -0.20, field_name="max_acceptable_drawdown"
        )
        self.cap_0_6_at_or_beyond = _require_exact_float(
            self.cap_0_6_at_or_beyond, -0.10, field_name="cap_0_6_at_or_beyond"
        )
        self.cap_0_3_at_or_beyond = _require_exact_float(
            self.cap_0_3_at_or_beyond, -0.15, field_name="cap_0_3_at_or_beyond"
        )
        self.risk_lock_at_or_beyond = _require_exact_float(
            self.risk_lock_at_or_beyond, -0.18, field_name="risk_lock_at_or_beyond"
        )
        self.final_red_line = _require_exact_float(self.final_red_line, -0.20, field_name="final_red_line")
        self.budget_when_in_cap_0_6_band = _require_exact_float(
            self.budget_when_in_cap_0_6_band, 0.6, field_name="budget_when_in_cap_0_6_band"
        )
        self.budget_when_in_cap_0_3_band = _require_exact_float(
            self.budget_when_in_cap_0_3_band, 0.3, field_name="budget_when_in_cap_0_3_band"
        )
        return self


class LayerOneAdjustmentPolicyConfirmed(_StrictModel):
    reduce_allowed_daily: Literal[True] = True
    increase_only_on_first_trading_day_of_week: Literal[True] = True
    increase_uses_prior_trading_day_known_state: Literal[True] = True
    risk_lock_has_priority: Literal[True] = True


class DeploymentUpgradePolicyConfirmed(_StrictModel):
    auto_upgrade_forbidden: Literal[True] = True
    stages: list[dict[str, Any]] = Field(
        default_factory=lambda: [
            {
                "max_stock_budget": 0.3,
                "mode": "human_controlled_trial",
                "requires_historical_validation_pass": True,
                "min_months_before_next": None,
                "requires_user_confirmation": True,
            },
            {
                "max_stock_budget": 0.6,
                "mode": "human_controlled",
                "min_months_at_prior_stage": 3,
                "requires_no_severe_anomaly": True,
                "requires_user_confirmation": True,
                "system_auto_upgrade_forbidden": True,
            },
            {
                "max_stock_budget": 0.9,
                "mode": "human_controlled",
                "min_months_at_prior_stage": 3,
                "requires_no_risk_lock_trigger": True,
                "requires_user_confirmation": True,
                "system_auto_upgrade_forbidden": True,
            },
        ]
    )
    twelve_month_new_oos_continues_and_is_marked: Literal[True] = True
    twelve_month_new_oos_is_not_hard_gate_for_60_or_90_unlock: Literal[True] = True
    unlock_records_must_include: list[str] = Field(
        default_factory=lambda: ["timestamp", "contract_version", "data_snapshot_id", "user_confirmation"]
    )


class CostAssumptionsConfirmed(_StrictModel):
    base_commission_per_side: float = 0.00025
    minimum_commission_cny: float = 5.0
    base_slippage_bps_per_side: Literal[5] = 5
    stress_slippage_bps_per_side: Literal[15] = 15
    stamp_tax: Literal["official_historical_sell_side_schedule"] = "official_historical_sell_side_schedule"
    stamp_tax_schedule_status: Literal["pending_factual_implementation_evidence"] = (
        "pending_factual_implementation_evidence"
    )
    stamp_tax_note: str = (
        "Must not claim completion using a simplified 0.1% since-1900 schedule. "
        "Full official historical sell-side stamp-tax timetable remains pending factual implementation evidence."
    )
    stress_must_not_breach_max_drawdown: float = -0.20

    @model_validator(mode="after")
    def _freeze(self) -> CostAssumptionsConfirmed:
        self.base_commission_per_side = _require_exact_float(
            self.base_commission_per_side, 0.00025, field_name="base_commission_per_side"
        )
        self.minimum_commission_cny = _require_exact_float(
            self.minimum_commission_cny, 5.0, field_name="minimum_commission_cny"
        )
        self.stress_must_not_breach_max_drawdown = _require_exact_float(
            self.stress_must_not_breach_max_drawdown, -0.20, field_name="stress_must_not_breach_max_drawdown"
        )
        return self


class LayerOneConfirmedDecisions(_StrictModel):
    objective: Literal["absolute_return"] = "absolute_return"
    performance_benchmark: IndexIdentityConfirmed
    risk_state_index: IndexIdentityConfirmed
    cash_asset_scope: list[str]
    etf_asset_scope: list[str]
    etf_enhancement_layer: Literal["future_milestone_after_base_strategy_validation"] = (
        "future_milestone_after_base_strategy_validation"
    )
    max_acceptable_drawdown: float = -0.20
    min_stock_budget: float = 0.0
    max_stock_budget: float = 0.9
    risk_budget_levels: list[float]
    trend: TrendPolicyConfirmed = Field(default_factory=TrendPolicyConfirmed)
    volatility: VolatilityPolicyConfirmed = Field(default_factory=VolatilityPolicyConfirmed)
    index_drawdown: IndexDrawdownPolicyConfirmed = Field(default_factory=IndexDrawdownPolicyConfirmed)
    account_drawdown: AccountDrawdownPolicyConfirmed = Field(default_factory=AccountDrawdownPolicyConfirmed)
    adjustment_policy: LayerOneAdjustmentPolicyConfirmed = Field(default_factory=LayerOneAdjustmentPolicyConfirmed)
    deployment_upgrade: DeploymentUpgradePolicyConfirmed = Field(default_factory=DeploymentUpgradePolicyConfirmed)
    cost_assumptions: CostAssumptionsConfirmed = Field(default_factory=CostAssumptionsConfirmed)

    @model_validator(mode="after")
    def _validate(self) -> LayerOneConfirmedDecisions:
        _assert_layer_one_cross_field_consistency(
            cash_asset_scope=self.cash_asset_scope,
            etf_asset_scope=self.etf_asset_scope,
            min_stock_budget=self.min_stock_budget,
            max_stock_budget=self.max_stock_budget,
            risk_budget_levels=self.risk_budget_levels,
        )
        if self.risk_budget_levels != [0.0, 0.3, 0.6, 0.9]:
            raise ValueError("confirmed risk_budget_levels must be [0.0, 0.3, 0.6, 0.9]")
        if self.cash_asset_scope != ["CNY_CASH"]:
            raise ValueError("v1 non-stock funds must be cash-only CNY_CASH")
        if self.etf_asset_scope != []:
            raise ValueError("ETF sleeve must remain empty until future enhancement milestone")
        if self.performance_benchmark.role != "performance_comparison":
            raise ValueError("performance_benchmark.role mismatch")
        if self.performance_benchmark.return_definition != "total_return":
            raise ValueError("performance benchmark must be total_return")
        if self.risk_state_index.role != "market_risk_state":
            raise ValueError("risk_state_index.role mismatch")
        if self.risk_state_index.return_definition != "price_index":
            raise ValueError("risk-state index must be price_index")
        self.max_acceptable_drawdown = _require_exact_float(
            self.max_acceptable_drawdown, -0.20, field_name="max_acceptable_drawdown"
        )
        self.min_stock_budget = _require_exact_float(self.min_stock_budget, 0.0, field_name="min_stock_budget")
        self.max_stock_budget = _require_exact_float(self.max_stock_budget, 0.9, field_name="max_stock_budget")
        return self


class UniversePolicyConfirmed(_StrictModel):
    markets: list[str] = Field(default_factory=lambda: ["SSE", "SZSE"])
    include_bse: Literal[False] = False
    st_or_delist_risk_new_entries_forbidden: Literal[True] = True
    suspended_buy_forbidden: Literal[True] = True
    min_listed_market_trading_days: Literal[180] = 180


class LiquidityPolicyConfirmed(_StrictModel):
    lookback_market_trading_days: Literal[20] = 20
    median_daily_amount_min_cny: Literal[50_000_000] = 50_000_000
    min_tradable_days_in_lookback: Literal[15] = 15
    max_planned_buy_vs_20d_avg_amount: float = 0.001
    missing_fails_closed: Literal[True] = True

    @model_validator(mode="after")
    def _freeze(self) -> LiquidityPolicyConfirmed:
        self.max_planned_buy_vs_20d_avg_amount = _require_exact_float(
            self.max_planned_buy_vs_20d_avg_amount, 0.001, field_name="max_planned_buy_vs_20d_avg_amount"
        )
        return self


class SmallCapSizeBand(_StrictModel):
    min_inclusive: float
    max_exclusive: float | None
    multiplier: float

    @model_validator(mode="after")
    def _validate_band(self) -> SmallCapSizeBand:
        self.min_inclusive = _finite_number(self.min_inclusive, field_name="min_inclusive")
        self.multiplier = _finite_number(self.multiplier, field_name="multiplier")
        if self.max_exclusive is not None:
            self.max_exclusive = _finite_number(self.max_exclusive, field_name="max_exclusive")
            if self.max_exclusive <= self.min_inclusive:
                raise ValueError("size band max_exclusive must be > min_inclusive")
        if not (0.0 < self.multiplier <= 1.0):
            raise ValueError("size band multiplier must be in (0, 1]")
        return self


class SmallCapPolicyConfirmed(_StrictModel):
    metric: Literal["pit_free_float_market_cap_cny"] = "pit_free_float_market_cap_cny"
    exclude_below_cny: Literal[3_000_000_000] = 3_000_000_000
    size_multipliers: list[SmallCapSizeBand] = Field(
        default_factory=lambda: [
            SmallCapSizeBand(min_inclusive=3_000_000_000, max_exclusive=5_000_000_000, multiplier=0.5),
            SmallCapSizeBand(min_inclusive=5_000_000_000, max_exclusive=10_000_000_000, multiplier=0.75),
            SmallCapSizeBand(min_inclusive=10_000_000_000, max_exclusive=None, multiplier=1.0),
        ]
    )
    reduced_capital_may_not_backfill_small_caps: Literal[True] = True
    reduced_capital_may_go_to_other_eligible_or_cash: Literal[True] = True


class StatisticalRiskClusterPolicyConfirmed(_StrictModel):
    lookback_trading_days: Literal[120] = 120
    correlation_threshold: float = 0.65
    max_sleeve_weight_per_cluster: float = 0.35
    max_positions_per_cluster: Literal[2] = 2
    uses_only_data_on_or_before_decision_date: Literal[True] = True
    required_when_pit_industry_history_missing: Literal[True] = True
    must_be_prominently_annotated: Literal[True] = True
    current_industry_backfill_forbidden: Literal[True] = True

    @model_validator(mode="after")
    def _freeze(self) -> StatisticalRiskClusterPolicyConfirmed:
        self.correlation_threshold = _require_exact_float(
            self.correlation_threshold, 0.65, field_name="correlation_threshold"
        )
        self.max_sleeve_weight_per_cluster = _require_exact_float(
            self.max_sleeve_weight_per_cluster, 0.35, field_name="max_sleeve_weight_per_cluster"
        )
        return self


class PositionSizingPolicyConfirmed(_StrictModel):
    min_target_notional_cny: Literal[8000] = 8000
    max_positions_by_budget: dict[str, int] = Field(default_factory=lambda: dict(CONFIRMED_MAX_POSITIONS_BY_BUDGET))
    absolute_max_positions: Literal[9] = CONFIRMED_ABSOLUTE_MAX_POSITIONS
    unaffordable_board_lot_must_not_force_buy: Literal[True] = True
    never_relax_rules_for_affordability: Literal[True] = True

    @model_validator(mode="after")
    def _freeze(self) -> PositionSizingPolicyConfirmed:
        _assert_confirmed_budget_position_map(
            self.max_positions_by_budget,
            field_name="max_positions_by_budget",
        )
        return self


class TrancheHoldPolicyConfirmed(_StrictModel):
    """Fixed-horizon tranche hold with budget-scaled active tranches.

    ``holding_period_market_trading_days`` / ``holding_cycle_market_trading_days`` (=40)
    are the holding period and uniform phase-cycle length. They are **not** the active
    tranche count. Active tranche count equals active target position count, capped by
    budget at 3/6/9 (absolute max 9), with one stock per active tranche.
    """

    holding_period_market_trading_days: Literal[40] = CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS
    holding_cycle_market_trading_days: Literal[40] = CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS
    max_active_tranches_by_budget: dict[str, int] = Field(
        default_factory=lambda: dict(CONFIRMED_MAX_POSITIONS_BY_BUDGET)
    )
    absolute_max_active_tranches: Literal[9] = CONFIRMED_ABSOLUTE_MAX_POSITIONS
    active_tranche_count_equals_active_target_position_count: Literal[True] = True
    one_stock_per_tranche: Literal[True] = True
    uniform_stagger_within_holding_period: Literal[True] = True
    initial_and_upgrade_build_gradually: Literal[True] = True
    same_day_catchup_fill_forbidden: Literal[True] = True
    risk_reduce_not_phase_limited: Literal[True] = True

    @model_validator(mode="after")
    def _freeze(self) -> TrancheHoldPolicyConfirmed:
        _assert_confirmed_budget_position_map(
            self.max_active_tranches_by_budget,
            field_name="max_active_tranches_by_budget",
        )
        if self.holding_cycle_market_trading_days != self.holding_period_market_trading_days:
            raise ValueError(
                "holding_cycle_market_trading_days must equal holding_period_market_trading_days under uniform stagger"
            )
        for count in self.max_active_tranches_by_budget.values():
            if count == self.holding_cycle_market_trading_days:
                raise ValueError(
                    "active tranche counts must not equal holding_cycle_market_trading_days; "
                    "40 is the holding/phase cycle length, not the active tranche count"
                )
            if count > self.absolute_max_active_tranches:
                raise ValueError("active tranche count exceeds absolute_max_active_tranches")
        return self


class EarlyExitPolicyConfirmed(_StrictModel):
    no_fixed_take_profit: Literal[True] = True
    catastrophe_stop_loss_vs_fill: float = -0.15
    catastrophe_stop_next_tradable_open: Literal[True] = True
    suspension_or_limit_down_defers_stop: Literal[True] = True
    portfolio_risk_lock_has_priority: Literal[True] = True
    no_early_exit_for_ordinary_rank_drop_within_holding_period: Literal[True] = True
    allowed_early_exit_reasons: list[str] = Field(
        default_factory=lambda: [
            "catastrophe_stop_loss",
            "st_or_delist_risk",
            "confirmed_financial_hard_exclusion",
            "portfolio_risk_reduce_or_lock",
        ]
    )

    @model_validator(mode="after")
    def _freeze(self) -> EarlyExitPolicyConfirmed:
        self.catastrophe_stop_loss_vs_fill = _require_exact_float(
            self.catastrophe_stop_loss_vs_fill, -0.15, field_name="catastrophe_stop_loss_vs_fill"
        )
        return self


class CandidateShortagePolicyConfirmed(_StrictModel):
    retain_cash_when_candidates_insufficient: Literal[True] = True
    retain_cash_when_critical_input_missing: Literal[True] = True
    retain_cash_when_board_lot_unaffordable: Literal[True] = True
    never_relax_thresholds: Literal[True] = True
    never_reuse_other_date_candidates: Literal[True] = True


class FinancialNegativeListConfirmed(_StrictModel):
    non_standard_audit_single_hit_excludes: Literal[True] = True
    other_known_pit_auditable_warning_hits_ge_2_excludes: Literal[True] = True
    other_known_pit_auditable_warning_hits_eq_1_halves_target: Literal[True] = True
    missing_stays_unknown_and_is_not_a_miss: Literal[True] = True
    exclusion_cannot_be_offset_by_alpha: Literal[True] = True


class EventDataPolicyConfirmed(_StrictModel):
    shareholder_count_role: Literal["diagnostic_until_independent_dev_and_pit_coverage_gates"] = (
        "diagnostic_until_independent_dev_and_pit_coverage_gates"
    )
    earnings_preview_flash_role: Literal["diagnostic_until_independent_dev_and_pit_coverage_gates"] = (
        "diagnostic_until_independent_dev_and_pit_coverage_gates"
    )
    pledge_role: Literal["diagnostic_until_independent_dev_and_pit_coverage_gates"] = (
        "diagnostic_until_independent_dev_and_pit_coverage_gates"
    )
    unlock_role: Literal["diagnostic_until_independent_dev_and_pit_coverage_gates"] = (
        "diagnostic_until_independent_dev_and_pit_coverage_gates"
    )
    silent_scoring_integration_forbidden: Literal[True] = True
    future_unlock_hard_exclude_rule: dict[str, Any] = Field(
        default_factory=lambda: {
            "description": (
                "Future 30 calendar-day unlock amount / free float > 10% may become a hard "
                "exclusion only when coverage, denominator, and available_at are all complete "
                "and auditable; otherwise the rule must not enable at all."
            ),
            "threshold": 0.10,
            "horizon_calendar_days": 30,
            "enable_only_when_coverage_denominator_available_at_complete": True,
        }
    )


class AlphaCandidateRegistryConfirmed(_StrictModel):
    families: list[str] = Field(
        default_factory=lambda: ["quality", "value", "medium_momentum_12_1", "defensive_low_vol"]
    )
    evaluation_methods: list[str] = Field(
        default_factory=lambda: ["full_cross_section_quantile_portfolios", "icir_hac_development"]
    )
    missing_must_not_fill_zero: Literal[True] = True
    ownership_and_events_not_in_alpha: Literal[True] = True
    weight_selection_status: Literal["pending_development_evidence"] = "pending_development_evidence"
    runnable_strategy_yaml_forbidden_now: Literal[True] = True


class LayerTwoResearchWindowsConfirmed(_StrictModel):
    seen_development: dict[str, str] = Field(default_factory=lambda: {"start": "2022-01-01", "end": "2023-12-31"})
    seen_robustness_check_only: dict[str, str] = Field(
        default_factory=lambda: {"start": "2024-01-01", "end": "2024-12-31"}
    )
    consumed_oos_reuse_forbidden_from: Literal["2025-01-01"] = "2025-01-01"
    note: str = (
        "Layer-two seen development is limited to 2022-01-01..2023-12-31; 2024 is "
        "seen robustness only; consumed 2025+ must not be reused. If any local "
        "contract already imposes a stricter bound, the stricter bound wins."
    )


class PitIndustryStatusConfirmed(_StrictModel):
    user_blocker: Literal[False] = False
    current_proxy: Literal["statistical_risk_clusters"] = "statistical_risk_clusters"
    pit_industry_enhancement: Literal["future_enhancement_not_completed"] = "future_enhancement_not_completed"
    current_industry_backfill_forbidden: Literal[True] = True
    research_and_30pct_controlled_trial_allowed_with_clusters_and_annotation: Literal[True] = True


class LayerTwoConfirmedDecisions(_StrictModel):
    universe: UniversePolicyConfirmed = Field(default_factory=UniversePolicyConfirmed)
    liquidity: LiquidityPolicyConfirmed = Field(default_factory=LiquidityPolicyConfirmed)
    small_cap: SmallCapPolicyConfirmed = Field(default_factory=SmallCapPolicyConfirmed)
    ownership_proxy_role: Literal["diagnostic"] = "diagnostic"
    ownership_missing_stays_unknown: Literal[True] = True
    ownership_not_in_scoring_or_exclusion: Literal[True] = True
    pit_industry: PitIndustryStatusConfirmed = Field(default_factory=PitIndustryStatusConfirmed)
    statistical_risk_cluster: StatisticalRiskClusterPolicyConfirmed = Field(
        default_factory=StatisticalRiskClusterPolicyConfirmed
    )
    position_sizing: PositionSizingPolicyConfirmed = Field(default_factory=PositionSizingPolicyConfirmed)
    tranche_hold: TrancheHoldPolicyConfirmed = Field(default_factory=TrancheHoldPolicyConfirmed)
    early_exit: EarlyExitPolicyConfirmed = Field(default_factory=EarlyExitPolicyConfirmed)
    candidate_shortage: CandidateShortagePolicyConfirmed = Field(default_factory=CandidateShortagePolicyConfirmed)
    financial_negative_list: FinancialNegativeListConfirmed = Field(default_factory=FinancialNegativeListConfirmed)
    event_data: EventDataPolicyConfirmed = Field(default_factory=EventDataPolicyConfirmed)
    alpha_candidates: AlphaCandidateRegistryConfirmed = Field(default_factory=AlphaCandidateRegistryConfirmed)
    research_windows: LayerTwoResearchWindowsConfirmed = Field(default_factory=LayerTwoResearchWindowsConfirmed)
    rebalance_semantics: Literal["fixed_40d_tranche_hold_with_risk_overrides"] = (
        "fixed_40d_tranche_hold_with_risk_overrides"
    )
    exit_semantics: Literal["hold_40_market_days_then_next_tradable_open_exit_with_limit_suspend_defer"] = (
        "hold_40_market_days_then_next_tradable_open_exit_with_limit_suspend_defer"
    )

    @model_validator(mode="after")
    def _validate_tranche_position_alignment(self) -> LayerTwoConfirmedDecisions:
        _assert_layer_two_cross_field_consistency(
            max_positions_per_cluster=self.statistical_risk_cluster.max_positions_per_cluster,
            max_positions=self.position_sizing.absolute_max_positions,
        )
        _assert_v2_tranche_position_consistency(
            position_sizing=self.position_sizing,
            tranche_hold=self.tranche_hold,
        )
        return self


class ExecutionConfirmedDecisions(_StrictModel):
    decision_after_close_on_t: Literal[True] = True
    attempt_fill_at_next_open_t_plus_1: Literal[True] = True
    fill_day_is_holding_day_1: Literal[True] = True
    exit_after_40_market_days_at_next_tradable_open: Literal[True] = True
    limit_up_buy_fails: Literal[True] = True
    limit_down_sell_fails: Literal[True] = True
    suspension_defers_and_is_recorded: Literal[True] = True
    no_hindsight_rewrite_of_original_orders: Literal[True] = True
    suspension_holding_day_clock: Literal["count_suspended_days"] = "count_suspended_days"
    if_still_suspended_at_expiry_exit_first_sellable_after_resume: Literal[True] = True
    delisting_settlement_contract: Literal[
        "prefer_exit_when_tradable_else_hold_with_annotation_fail_closed_without_official_final_evidence"
    ] = "prefer_exit_when_tradable_else_hold_with_annotation_fail_closed_without_official_final_evidence"
    delisting_live_becomes_human_event: Literal[True] = True
    minimum_commission_lot_handling_policy: Literal[
        "retain_cash_never_relax_thresholds_or_reuse_other_date_candidates"
    ] = "retain_cash_never_relax_thresholds_or_reuse_other_date_candidates"
    no_guess_prices_for_missing_final_settlement: Literal[True] = True


def default_evidence_blockers() -> list[CategorizedBlocker]:
    return [
        CategorizedBlocker(
            path="layer_one.performance_benchmark.symbol",
            category="pending_factual_source_verification",
            detail=(
                "CSI All-Share total-return index Tushare/CSI official symbol is not verified "
                "from local evidence; must not be guessed."
            ),
        ),
        CategorizedBlocker(
            path="layer_one.risk_state_index.symbol",
            category="pending_factual_source_verification",
            detail=(
                "CSI All-Share price-index Tushare/CSI official symbol is not verified from "
                "local evidence; must not be guessed."
            ),
        ),
        CategorizedBlocker(
            path="layer_one.cost_assumptions.stamp_tax_schedule",
            category="pending_factual_source_verification",
            detail=(
                "Official historical sell-side stamp-tax timetable evidence is incomplete; "
                "a flat 0.1%-since-1900 simplification must not be treated as done."
            ),
        ),
        CategorizedBlocker(
            path="layer_one.regime_budget_engine",
            category="pending_implementation",
            detail="Confirmed trend/vol/drawdown/account-lock mapping is not yet implemented.",
        ),
        CategorizedBlocker(
            path="layer_two.portfolio_construction",
            category="pending_implementation",
            detail=(
                "Universe, liquidity, size, clusters, tranches, negative list, and execution "
                "semantics are confirmed but not implemented as a runnable strategy."
            ),
        ),
        CategorizedBlocker(
            path="execution.risk_lock_ui_and_persistence",
            category="pending_implementation",
            detail=(
                "Prominent risk-lock annotation and non-auto-clear across service restart are "
                "contractual but not implemented."
            ),
        ),
        CategorizedBlocker(
            path="layer_two.alpha_weight_selection",
            category="pending_development_evidence",
            detail=(
                "Alpha families are pre-registered only; weights remain pending development "
                "evidence from quantile/ICIR evaluation. Not a pending user decision."
            ),
        ),
        CategorizedBlocker(
            path="layer_one.validation_hard_gates_evaluation",
            category="pending_development_evidence",
            detail="Layer-one hard gates are confirmed as criteria; segment evaluations are not yet run.",
        ),
        CategorizedBlocker(
            path="layer_one.etf_enhancement_layer",
            category="future_enhancement",
            detail="Cash/money-market ETF enhancement is a future milestone after base-strategy validation.",
        ),
        CategorizedBlocker(
            path="layer_two.pit_industry_history",
            category="future_enhancement",
            detail=(
                "Real PIT industry history is a future enhancement; statistical clusters are "
                "the current required proxy with prominent annotation."
            ),
        ),
        CategorizedBlocker(
            path="layer_two.event_factors_scoring_integration",
            category="future_enhancement",
            detail=(
                "Shareholder count, earnings preview/flash, unlock, and pledge stay diagnostic "
                "until independent development evidence and PIT coverage gates pass."
            ),
        ),
    ]


class TwoLayerStrategyDecisionContractV2(_StrictModel):
    schema_version: Literal["2"] = TWO_LAYER_DECISION_SCHEMA_VERSION_V2
    contract_version: Literal["two-layer-strategy-decision-v2"] = TWO_LAYER_DECISION_CONTRACT_VERSION_V2
    status: ContractStatusV2 = "confirmed_for_implementation_but_not_ready"
    confirmation_as_of: date = CONTRACT_CONFIRMATION_AS_OF
    research_trial_ledger_id: str = Field(min_length=1)
    research_trial_ledger_path: Literal["config/research/research-trial-ledger-v1.json"] = (
        BOUND_RESEARCH_TRIAL_LEDGER_PATH
    )
    confirmed: ConfirmedTwoLayerDecisions = Field(default_factory=ConfirmedTwoLayerDecisions)
    consumed_oos: ConsumedOosReusePolicy = Field(default_factory=ConsumedOosReusePolicy)
    layer_one: LayerOneConfirmedDecisions
    layer_two: LayerTwoConfirmedDecisions
    execution: ExecutionConfirmedDecisions
    evidence_blockers: list[CategorizedBlocker]
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    contract_id: str | None = None

    @field_validator("research_trial_ledger_id", mode="before")
    @classmethod
    def _reject_blank_ledger_id(cls, value: object) -> object:
        return _reject_blank_string(value, field_name="research_trial_ledger_id")

    @field_validator("research_trial_ledger_path", mode="before")
    @classmethod
    def _reject_blank_or_escape_ledger_path(cls, value: object) -> object:
        return _validate_ledger_path_field(value)

    @field_validator("confirmation_as_of", mode="before")
    @classmethod
    def _parse_confirmation_as_of(cls, value: object) -> date:
        if isinstance(value, date):
            return value
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("confirmation_as_of must be an ISO date")
        return date.fromisoformat(value.strip())

    @model_validator(mode="after")
    def _validate_gate_flags(self) -> TwoLayerStrategyDecisionContractV2:
        if self.status != "confirmed_for_implementation_but_not_ready":
            raise ValueError("v2 contract status must be confirmed_for_implementation_but_not_ready")
        if self.ready_for_scoring or self.ready_for_backtest or self.ready_for_trading or self.auto_deploy:
            raise ValueError("confirmed contract cannot authorize scoring, backtest, trading, or deploy")
        if self.confirmed.initial_cash != CONFIRMED_INITIAL_CASH:
            raise ValueError("confirmed initial_cash must remain 80000")
        if not self.consumed_oos.reuse_forbidden:
            raise ValueError("consumed OOS reuse must remain forbidden")
        if any(blocker.category == "pending_user_decision" for blocker in self.evidence_blockers):
            raise ValueError("confirmed contract must not retain pending_user_decision blockers")
        categories = {blocker.category for blocker in self.evidence_blockers}
        required = {
            "pending_factual_source_verification",
            "pending_implementation",
            "pending_development_evidence",
            "future_enhancement",
        }
        missing = required - categories
        if missing:
            raise ValueError(f"evidence_blockers missing required categories: {sorted(missing)}")
        return self


TwoLayerDecisionDocument = TwoLayerStrategyDecisionDraftV1 | TwoLayerStrategyDecisionContractV2


class TwoLayerDecisionVerificationResult(_StrictModel):
    contract_id: str
    schema_version: Literal["1", "2"]
    contract_version: str
    status: str
    research_trial_ledger_id: str
    research_trial_ledger_path: Literal["config/research/research-trial-ledger-v1.json"]
    research_trial_ledger_binding_ok: bool
    resolved: bool
    user_decisions_resolved: bool
    pending_user_decision_count: int
    blockers: list[str]
    evidence_blockers: list[CategorizedBlocker] = Field(default_factory=list)
    confirmed_initial_cash: Literal[80000] = CONFIRMED_INITIAL_CASH
    initial_cash_is_blocker: Literal[False] = False
    consumed_oos_reuse_forbidden: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    does_not_score: Literal[True] = True
    does_not_backtest: Literal[True] = True
    does_not_trade: Literal[True] = True


def canonical_decision_payload(draft: TwoLayerDecisionDocument) -> dict[str, Any]:
    return draft.model_dump(mode="json", exclude={"contract_id"})


def canonical_decision_bytes(draft: TwoLayerDecisionDocument) -> bytes:
    payload = canonical_decision_payload(draft)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_contract_id(draft: TwoLayerDecisionDocument) -> str:
    return hashlib.sha256(canonical_decision_bytes(draft)).hexdigest()


@overload
def seal_two_layer_decision_draft(draft: TwoLayerStrategyDecisionDraftV1) -> TwoLayerStrategyDecisionDraftV1: ...


@overload
def seal_two_layer_decision_draft(draft: TwoLayerStrategyDecisionContractV2) -> TwoLayerStrategyDecisionContractV2: ...


def seal_two_layer_decision_draft(draft: TwoLayerDecisionDocument) -> TwoLayerDecisionDocument:
    return draft.model_copy(update={"contract_id": compute_contract_id(draft)})


def build_unresolved_draft(
    *,
    research_trial_ledger_id: str = BOUND_RESEARCH_TRIAL_LEDGER_ID,
    research_trial_ledger_path: Literal[
        "config/research/research-trial-ledger-v1.json"
    ] = BOUND_RESEARCH_TRIAL_LEDGER_PATH,
) -> TwoLayerStrategyDecisionDraftV1:
    """Factory for legacy unresolved schema-v1 drafts (tests / migration source)."""
    draft = TwoLayerStrategyDecisionDraftV1(
        research_trial_ledger_id=research_trial_ledger_id,
        research_trial_ledger_path=research_trial_ledger_path,
        layer_one=LayerOnePendingDecisions(),
        layer_two=LayerTwoPendingDecisions(),
        execution=ExecutionPendingDecisions(),
    )
    return seal_two_layer_decision_draft(draft)


def build_confirmed_contract_v2(
    *,
    research_trial_ledger_id: str = BOUND_RESEARCH_TRIAL_LEDGER_ID,
    research_trial_ledger_path: Literal[
        "config/research/research-trial-ledger-v1.json"
    ] = BOUND_RESEARCH_TRIAL_LEDGER_PATH,
    confirmation_as_of: date = CONTRACT_CONFIRMATION_AS_OF,
) -> TwoLayerStrategyDecisionContractV2:
    contract = TwoLayerStrategyDecisionContractV2(
        confirmation_as_of=confirmation_as_of,
        research_trial_ledger_id=research_trial_ledger_id,
        research_trial_ledger_path=research_trial_ledger_path,
        layer_one=LayerOneConfirmedDecisions(
            performance_benchmark=IndexIdentityConfirmed(
                role="performance_comparison",
                name="csi_all_share_total_return",
                return_definition="total_return",
                symbol=None,
                symbol_status="pending_factual_source_verification",
                note=(
                    "中证全指全收益 used as performance comparison benchmark. "
                    "Exact Tushare/CSI symbol pending factual source verification."
                ),
            ),
            risk_state_index=IndexIdentityConfirmed(
                role="market_risk_state",
                name="csi_all_share_price_index",
                return_definition="price_index",
                symbol=None,
                symbol_status="pending_factual_source_verification",
                note=(
                    "中证全指价格指数 used for market risk-state features. "
                    "Exact Tushare/CSI symbol pending factual source verification."
                ),
            ),
            cash_asset_scope=["CNY_CASH"],
            etf_asset_scope=[],
            risk_budget_levels=[0.0, 0.3, 0.6, 0.9],
        ),
        layer_two=LayerTwoConfirmedDecisions(),
        execution=ExecutionConfirmedDecisions(),
        evidence_blockers=default_evidence_blockers(),
    )
    return seal_two_layer_decision_draft(contract)


def migrate_decision_contract_v1_to_v2(
    draft_v1: TwoLayerStrategyDecisionDraftV1,
    *,
    confirmation_as_of: date = CONTRACT_CONFIRMATION_AS_OF,
) -> TwoLayerStrategyDecisionContractV2:
    """Explicit migration: unresolved v1 cannot silently invent confirmed economics.

    Unresolved v1 is replaced by the sealed confirmed v2 factory (ledger binding preserved).
    Partially filled v1 is rejected so operators must use an explicit confirmed overlay.
    """
    blockers = collect_decision_blockers_v1(draft_v1)
    if blockers and len(blockers) != len(REQUIRED_DECISION_PATHS):
        raise ValueError(
            "partially filled schema-v1 draft cannot auto-migrate; provide an explicit confirmed v2 overlay"
        )
    if draft_v1.research_trial_ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
        raise ValueError("migration requires bound research trial ledger id")
    if draft_v1.research_trial_ledger_path != BOUND_RESEARCH_TRIAL_LEDGER_PATH:
        raise ValueError("migration requires bound research trial ledger path")
    return build_confirmed_contract_v2(
        research_trial_ledger_id=draft_v1.research_trial_ledger_id,
        research_trial_ledger_path=draft_v1.research_trial_ledger_path,
        confirmation_as_of=confirmation_as_of,
    )


def _decision_value(draft: TwoLayerStrategyDecisionDraftV1, path: str) -> object:
    current: object = draft
    for part in path.split("."):
        current = getattr(current, part)
    return current


def collect_decision_blockers_v1(draft: TwoLayerStrategyDecisionDraftV1) -> list[str]:
    blockers: list[str] = []
    for path in REQUIRED_DECISION_PATHS:
        if _decision_value(draft, path) is None:
            blockers.append(path)
    return blockers


def collect_decision_blockers(draft: TwoLayerDecisionDocument) -> list[str]:
    if isinstance(draft, TwoLayerStrategyDecisionDraftV1):
        return collect_decision_blockers_v1(draft)
    return [
        f"{blocker.category}:{blocker.path}"
        for blocker in draft.evidence_blockers
        if blocker.category == "pending_user_decision"
    ]


def assert_contract_self_hash(draft: TwoLayerDecisionDocument) -> None:
    if draft.contract_id is None:
        raise ValueError("two-layer decision contract_id is missing")
    expected = compute_contract_id(draft)
    if draft.contract_id != expected:
        raise ValueError("two-layer decision contract_id does not match canonical content hash")


def assert_status_ready_consistency(draft: TwoLayerDecisionDocument) -> None:
    if draft.ready_for_scoring or draft.ready_for_backtest or draft.ready_for_trading or draft.auto_deploy:
        raise ValueError("status/ready contradiction: ready flags must remain false")
    if isinstance(draft, TwoLayerStrategyDecisionDraftV1):
        if draft.status != "blocked_pending_user_decisions":
            raise ValueError("status/ready contradiction: v1 status must be blocked_pending_user_decisions")
    elif draft.status != "confirmed_for_implementation_but_not_ready":
        raise ValueError("status/ready contradiction: v2 status must be confirmed_for_implementation_but_not_ready")


def assert_bound_research_trial_ledger_id(draft: TwoLayerDecisionDocument) -> None:
    if draft.research_trial_ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
        raise ValueError("research_trial_ledger_id does not match bound research trial ledger")
    if draft.research_trial_ledger_path != BOUND_RESEARCH_TRIAL_LEDGER_PATH:
        raise ValueError("research_trial_ledger_path does not match bound research trial ledger path")


def assert_cross_field_consistency(draft: TwoLayerDecisionDocument) -> None:
    if isinstance(draft, TwoLayerStrategyDecisionDraftV1):
        _assert_layer_one_cross_field_consistency(
            cash_asset_scope=draft.layer_one.cash_asset_scope,
            etf_asset_scope=draft.layer_one.etf_asset_scope,
            min_stock_budget=draft.layer_one.min_stock_budget,
            max_stock_budget=draft.layer_one.max_stock_budget,
            risk_budget_levels=draft.layer_one.risk_budget_levels,
        )
        _assert_layer_two_cross_field_consistency(
            max_positions_per_cluster=draft.layer_two.max_positions_per_cluster,
            max_positions=draft.layer_two.max_positions,
        )
        return
    _assert_layer_one_cross_field_consistency(
        cash_asset_scope=draft.layer_one.cash_asset_scope,
        etf_asset_scope=draft.layer_one.etf_asset_scope,
        min_stock_budget=draft.layer_one.min_stock_budget,
        max_stock_budget=draft.layer_one.max_stock_budget,
        risk_budget_levels=draft.layer_one.risk_budget_levels,
    )
    _assert_layer_two_cross_field_consistency(
        max_positions_per_cluster=draft.layer_two.statistical_risk_cluster.max_positions_per_cluster,
        max_positions=draft.layer_two.position_sizing.absolute_max_positions,
    )
    _assert_v2_tranche_position_consistency(
        position_sizing=draft.layer_two.position_sizing,
        tranche_hold=draft.layer_two.tranche_hold,
    )


def assert_no_future_seen_windows(
    draft: TwoLayerStrategyDecisionContractV2,
    *,
    reference_date: date | None = None,
) -> None:
    """Reject seen research windows that end after the confirmation/reference date."""
    as_of = reference_date or draft.confirmation_as_of
    for label, window in (
        ("seen_development", draft.layer_two.research_windows.seen_development),
        ("seen_robustness_check_only", draft.layer_two.research_windows.seen_robustness_check_only),
    ):
        end = date.fromisoformat(window["end"])
        if end > as_of:
            raise ValueError(f"{label} window end is after reference_date/confirmation_as_of")


def _status_blocks_overall_resolved(status: str) -> bool:
    """Return True when status alone forbids overall resolved (fail-closed)."""
    normalized = status.strip().lower()
    return normalized == "blocked_pending_user_decisions" or "not_ready" in normalized


def compute_two_layer_v2_overall_resolved(
    *,
    evidence_blockers: Sequence[object],
    status: str,
    ready_for_scoring: bool,
    ready_for_backtest: bool,
    ready_for_trading: bool,
) -> bool:
    """Fail-closed overall resolved for schema-v2 verification results.

    Formula (all must hold):
    - ``len(evidence_blockers) == 0``
    - status is not blocked / not-ready
    - ``ready_for_scoring and ready_for_backtest and ready_for_trading``

    ``user_decisions_resolved`` is reported separately and is never sufficient alone.
    ``auto_deploy`` is intentionally excluded (must never auto-deploy).
    Verification output does not affect ``contract_id`` / canonical hash.
    """
    if len(evidence_blockers) != 0:
        return False
    if _status_blocks_overall_resolved(status):
        return False
    return bool(ready_for_scoring and ready_for_backtest and ready_for_trading)


def verify_two_layer_decision_draft(
    draft: TwoLayerDecisionDocument,
    *,
    reference_date: date | None = None,
) -> TwoLayerDecisionVerificationResult:
    assert_contract_self_hash(draft)
    assert_status_ready_consistency(draft)
    assert_bound_research_trial_ledger_id(draft)
    assert_cross_field_consistency(draft)

    if isinstance(draft, TwoLayerStrategyDecisionDraftV1):
        blockers = collect_decision_blockers_v1(draft)
        resolved = len(blockers) == 0
        if not resolved and draft.status != "blocked_pending_user_decisions":
            raise ValueError("unresolved draft must keep status blocked_pending_user_decisions")
        return TwoLayerDecisionVerificationResult(
            contract_id=draft.contract_id or compute_contract_id(draft),
            schema_version="1",
            contract_version=draft.contract_version,
            status=draft.status,
            research_trial_ledger_id=draft.research_trial_ledger_id,
            research_trial_ledger_path=draft.research_trial_ledger_path,
            research_trial_ledger_binding_ok=False,
            resolved=resolved,
            user_decisions_resolved=resolved,
            pending_user_decision_count=len(blockers),
            blockers=blockers,
            evidence_blockers=[],
        )

    assert_no_future_seen_windows(draft, reference_date=reference_date)
    user_blockers = [b for b in draft.evidence_blockers if b.category == "pending_user_decision"]
    if user_blockers:
        raise ValueError("confirmed contract has pending_user_decision blockers")
    path_blockers = [f"{b.category}:{b.path}" for b in draft.evidence_blockers]
    overall_resolved = compute_two_layer_v2_overall_resolved(
        evidence_blockers=draft.evidence_blockers,
        status=draft.status,
        ready_for_scoring=draft.ready_for_scoring,
        ready_for_backtest=draft.ready_for_backtest,
        ready_for_trading=draft.ready_for_trading,
    )
    return TwoLayerDecisionVerificationResult(
        contract_id=draft.contract_id or compute_contract_id(draft),
        schema_version="2",
        contract_version=draft.contract_version,
        status=draft.status,
        research_trial_ledger_id=draft.research_trial_ledger_id,
        research_trial_ledger_path=draft.research_trial_ledger_path,
        research_trial_ledger_binding_ok=False,
        resolved=overall_resolved,
        user_decisions_resolved=True,
        pending_user_decision_count=0,
        blockers=path_blockers,
        evidence_blockers=list(draft.evidence_blockers),
    )


def load_two_layer_decision_draft(path: Path) -> TwoLayerDecisionDocument:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("two-layer decision draft is missing or invalid") from exc
    version = payload.get("schema_version")
    try:
        if version == "1":
            return TwoLayerStrategyDecisionDraftV1.model_validate(payload)
        if version == "2":
            return TwoLayerStrategyDecisionContractV2.model_validate(payload)
    except Exception as exc:
        raise ValueError("two-layer decision draft is missing or invalid") from exc
    raise ValueError(f"unsupported two-layer decision schema_version: {version!r}")


def verify_two_layer_decision_draft_file(
    *,
    draft_path: Path,
    repo_root: Path | None = None,
    reference_date: date | None = None,
) -> tuple[TwoLayerDecisionDocument, TwoLayerDecisionVerificationResult]:
    root = (repo_root or Path.cwd()).resolve()
    draft = load_two_layer_decision_draft(draft_path)
    result = verify_two_layer_decision_draft(draft, reference_date=reference_date)
    ledger_path = _assert_repo_relative_ledger_path(draft.research_trial_ledger_path, repo_root=root)
    ledger, _summary = verify_research_trial_ledger(ledger_path=ledger_path, repo_root=root)
    if ledger.ledger_id != draft.research_trial_ledger_id:
        raise ValueError("research trial ledger_id does not match draft research_trial_ledger_id")
    if ledger.ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
        raise ValueError("research trial ledger_id does not match bound research trial ledger")
    if draft.research_trial_ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
        raise ValueError("research_trial_ledger_id does not match bound research trial ledger")
    return draft, result.model_copy(update={"research_trial_ledger_binding_ok": True})


def write_two_layer_decision_draft(
    path: Path,
    draft: TwoLayerDecisionDocument,
) -> TwoLayerDecisionDocument:
    sealed = seal_two_layer_decision_draft(draft)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_RESEARCH_TRIAL_LEDGER_ID",
    "BOUND_RESEARCH_TRIAL_LEDGER_PATH",
    "CONFIRMED_ABSOLUTE_MAX_POSITIONS",
    "CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS",
    "CONFIRMED_INITIAL_CASH",
    "CONFIRMED_MAX_POSITIONS_BY_BUDGET",
    "CONTRACT_CONFIRMATION_AS_OF",
    "DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH",
    "DEFAULT_TWO_LAYER_DECISION_DRAFT_PATH",
    "REQUIRED_DECISION_PATHS",
    "TWO_LAYER_DECISION_CONTRACT_VERSION",
    "TWO_LAYER_DECISION_CONTRACT_VERSION_V1",
    "TWO_LAYER_DECISION_CONTRACT_VERSION_V2",
    "TWO_LAYER_DECISION_SCHEMA_VERSION",
    "TWO_LAYER_DECISION_SCHEMA_VERSION_V1",
    "TWO_LAYER_DECISION_SCHEMA_VERSION_V2",
    "CategorizedBlocker",
    "ConfirmedTwoLayerDecisions",
    "ConsumedOosReusePolicy",
    "ExecutionConfirmedDecisions",
    "ExecutionPendingDecisions",
    "LayerOneConfirmedDecisions",
    "LayerOnePendingDecisions",
    "LayerTwoConfirmedDecisions",
    "LayerTwoPendingDecisions",
    "PositionSizingPolicyConfirmed",
    "TrancheHoldPolicyConfirmed",
    "TwoLayerDecisionVerificationResult",
    "TwoLayerStrategyDecisionContractV2",
    "TwoLayerStrategyDecisionDraft",
    "TwoLayerStrategyDecisionDraftV1",
    "assert_bound_research_trial_ledger_id",
    "assert_contract_self_hash",
    "assert_cross_field_consistency",
    "assert_no_future_seen_windows",
    "assert_status_ready_consistency",
    "build_confirmed_contract_v2",
    "build_unresolved_draft",
    "canonical_decision_bytes",
    "canonical_decision_payload",
    "collect_decision_blockers",
    "collect_decision_blockers_v1",
    "compute_contract_id",
    "compute_two_layer_v2_overall_resolved",
    "default_evidence_blockers",
    "load_two_layer_decision_draft",
    "migrate_decision_contract_v1_to_v2",
    "seal_two_layer_decision_draft",
    "verify_two_layer_decision_draft",
    "verify_two_layer_decision_draft_file",
    "write_two_layer_decision_draft",
]
