"""Layer-two allocation implementation interpretation protocol (E10d-0).

Read-only freeze of how the confirmed two-layer economics interpret the
8000 CNY minimum base slot together with size / financial risk multipliers.
Does not construct portfolios, score, backtest, place orders, or trade.

Upstream disk bindings (any drift fails file verification):
- two-layer decision contract
- layer-one index protocol
- tranche evaluation protocol
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_one_index_protocol import (
    DEFAULT_LAYER_ONE_INDEX_PROTOCOL_DRAFT_PATH,
    load_layer_one_index_protocol_draft,
    verify_layer_one_index_protocol_draft,
)
from app.research.tranche_evaluation_protocol import (
    DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH,
    load_tranche_evaluation_protocol_draft,
    verify_tranche_evaluation_protocol_draft,
)
from app.research.two_layer_contract import (
    CONFIRMED_ABSOLUTE_MAX_POSITIONS,
    CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS,
    CONFIRMED_INITIAL_CASH,
    CONFIRMED_MAX_POSITIONS_BY_BUDGET,
    CONTRACT_CONFIRMATION_AS_OF,
    DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH,
    _reject_blank_string,
    _require_exact_float,
    load_two_layer_decision_draft,
    verify_two_layer_decision_draft,
)

LAYER_TWO_ALLOCATION_PROTOCOL_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_ALLOCATION_PROTOCOL_VERSION: Literal["layer-two-allocation-implementation-protocol-v1"] = (
    "layer-two-allocation-implementation-protocol-v1"
)
DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH = Path(
    "config/research/layer-two-allocation-implementation-protocol-v1.json"
)

BOUND_TWO_LAYER_DECISION_CONTRACT_PATH: Literal["config/research/two-layer-strategy-decision-draft-v1.json"] = (
    "config/research/two-layer-strategy-decision-draft-v1.json"
)
BOUND_TWO_LAYER_DECISION_CONTRACT_ID = "27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6"
BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH: Literal["config/research/layer-one-index-development-protocol-draft-v1.json"] = (
    "config/research/layer-one-index-development-protocol-draft-v1.json"
)
BOUND_LAYER_ONE_INDEX_PROTOCOL_ID = "b7aa9de1539cdd791aee5b74ca8ec3f269b6ed809a070caa917686742c4b1b2f"
BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH: Literal["config/research/tranche-evaluation-protocol-draft-v1.json"] = (
    "config/research/tranche-evaluation-protocol-draft-v1.json"
)
BOUND_TRANCHE_EVALUATION_PROTOCOL_ID = "8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a"

ProtocolStatus = Literal["confirmed_for_implementation_but_not_ready"]
ProtocolBlockerCategory = Literal[
    "pending_implementation",
    "pending_development_evidence",
    "future_enhancement",
]
ClusterCapDenominator = Literal["sleeve_budget"]
CashRetentionReason = Literal[
    "zero_risk_budget",
    "insufficient_capital_for_minimum_base_slot",
    "candidate_shortage",
    "financial_hard_exclude",
    "financial_unknown",
    "size_or_critical_input_unknown",
    "cluster_report_unknown_or_incomplete",
    "no_available_slot",
    "unaffordable_board_lot_or_cost_gate",
    "risk_multiplier_released_capital",
]

CONFIRMED_MINIMUM_BASE_SLOT_NOTIONAL_CNY: Literal[8000] = 8000
CONFIRMED_RISK_BUDGET_LEVELS: list[float] = [0.0, 0.3, 0.6, 0.9]
CONFIRMED_MAX_ACTIVE_SLOTS_BY_BUDGET: dict[str, int] = {
    "0.0": 0,
    "0.3": 3,
    "0.6": 6,
    "0.9": 9,
}
CONFIRMED_SIZE_MULTIPLIERS: tuple[float, ...] = (0.5, 0.75, 1.0)
CONFIRMED_FINANCIAL_MULTIPLIERS: tuple[float, ...] = (0.0, 0.5, 1.0)
CONFIRMED_CLUSTER_MAX_SLEEVE_WEIGHT: float = 0.35
CONFIRMED_CLUSTER_MAX_POSITIONS: Literal[2] = 2

REQUIRED_ALLOCATION_EVIDENCE_BLOCKERS: dict[str, ProtocolBlockerCategory] = {
    "layer_two_constrained_allocator": "pending_implementation",
    "execution_board_lot_and_cost_gates": "pending_implementation",
    "alpha_weight_selection": "pending_development_evidence",
    "pit_industry_history": "future_enhancement",
    "ownership_and_event_hard_rules": "future_enhancement",
}

_BUDGET_ABS_TOL = 1e-12
_NOTIONAL_ABS_TOL = 1e-9


def _require_real_number(
    value: object,
    *,
    field_name: str,
    minimum: float | None = 0.0,
    minimum_exclusive: bool = False,
) -> float:
    """Reject bool / non-numeric / NaN / Inf; optionally enforce a lower bound.

    Zero is allowed when ``minimum == 0.0`` and ``minimum_exclusive`` is false
    (e.g. current_account_equity / sleeve_budget). Bool is never accepted even
    when it would coerce to 0.0 / 1.0.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a real number (bool rejected)")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite (NaN/Inf rejected)")
    if minimum is not None:
        if minimum_exclusive:
            if number <= minimum:
                raise ValueError(f"{field_name} must be > {minimum}")
        elif number < minimum:
            raise ValueError(f"{field_name} must be >= {minimum}")
    return number


def _require_non_bool_int(value: object, *, field_name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise ValueError(f"{field_name} must be an int (bool rejected)")
    if value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProtocolEvidenceBlocker(_StrictModel):
    path: str = Field(min_length=1)
    category: ProtocolBlockerCategory
    detail: str = Field(min_length=1)

    @field_validator("path", "detail", mode="before")
    @classmethod
    def _reject_blank(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)


class CapitalBudgetInterpretation(_StrictModel):
    initial_cash: Literal[80000] = CONFIRMED_INITIAL_CASH
    initial_cash_is_research_starting_point_only: Literal[True] = True
    allocation_uses_verified_current_account_equity: Literal[True] = True
    allocation_uses_layer_one_current_risk_budget: Literal[True] = True
    allowed_risk_budget_levels: list[float] = Field(default_factory=lambda: list(CONFIRMED_RISK_BUDGET_LEVELS))
    sleeve_budget_formula: Literal["current_account_equity * risk_budget"] = "current_account_equity * risk_budget"
    sleeve_budget_must_not_overspend_equity: Literal[True] = True
    note: str = (
        "initial_cash=80000 is the research starting point only. Live allocation math "
        "must use verified current_account_equity and the current layer-one risk_budget "
        "in {0.0, 0.3, 0.6, 0.9}. sleeve_budget = current_account_equity * risk_budget "
        "and must never overspend equity."
    )

    @model_validator(mode="after")
    def _freeze(self) -> CapitalBudgetInterpretation:
        if self.allowed_risk_budget_levels != CONFIRMED_RISK_BUDGET_LEVELS:
            raise ValueError("allowed_risk_budget_levels must remain [0.0, 0.3, 0.6, 0.9]")
        if self.initial_cash != CONFIRMED_INITIAL_CASH:
            raise ValueError("initial_cash must remain 80000")
        return self


class BaseSlotInterpretation(_StrictModel):
    minimum_base_slot_notional_cny: Literal[8000] = CONFIRMED_MINIMUM_BASE_SLOT_NOTIONAL_CNY
    minimum_applies_before_risk_multipliers: Literal[True] = True
    minimum_is_not_post_multiplier_floor: Literal[True] = True
    max_active_slots_by_budget: dict[str, int] = Field(
        default_factory=lambda: dict(CONFIRMED_MAX_ACTIVE_SLOTS_BY_BUDGET)
    )
    absolute_max_active_slots: Literal[9] = CONFIRMED_ABSOLUTE_MAX_POSITIONS
    base_slot_count_formula: Literal[
        "min(contract_cap_for_budget, floor(sleeve_budget / minimum_base_slot_notional_cny))"
    ] = "min(contract_cap_for_budget, floor(sleeve_budget / minimum_base_slot_notional_cny))"
    zero_base_slot_count_means_no_new_entries: Literal[True] = True
    base_slot_notional_formula: Literal["sleeve_budget / base_slot_count"] = "sleeve_budget / base_slot_count"
    pre_multiplier_each_slot_at_least_minimum: Literal[True] = True
    note: str = (
        "8000 CNY is the minimum_base_slot_notional before size/financial multipliers. "
        "Budget tiers give max active slots 0/3/6/9. For non-zero budget, "
        "base_slot_count = min(contract cap, floor(sleeve_budget/8000)); if 0, open no "
        "slots. base_slot_notional = sleeve_budget / base_slot_count so each pre-multiplier "
        "slot is >= 8000."
    )

    @model_validator(mode="after")
    def _freeze(self) -> BaseSlotInterpretation:
        if self.max_active_slots_by_budget != CONFIRMED_MAX_ACTIVE_SLOTS_BY_BUDGET:
            raise ValueError("max_active_slots_by_budget must map 0.0/0.3/0.6/0.9 -> 0/3/6/9")
        if self.max_active_slots_by_budget["0.3"] != CONFIRMED_MAX_POSITIONS_BY_BUDGET["0.3"]:
            raise ValueError("0.3 budget slot cap must remain 3")
        if self.max_active_slots_by_budget["0.6"] != CONFIRMED_MAX_POSITIONS_BY_BUDGET["0.6"]:
            raise ValueError("0.6 budget slot cap must remain 6")
        if self.max_active_slots_by_budget["0.9"] != CONFIRMED_MAX_POSITIONS_BY_BUDGET["0.9"]:
            raise ValueError("0.9 budget slot cap must remain 9")
        for count in self.max_active_slots_by_budget.values():
            if count == CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS:
                raise ValueError(
                    "active slot counts must not equal holding_cycle_market_trading_days; "
                    "40 is the holding/phase cycle length, not the active count"
                )
        return self


class RiskMultiplierInterpretation(_StrictModel):
    size_multipliers_allowed: list[float] = Field(default_factory=lambda: list(CONFIRMED_SIZE_MULTIPLIERS))
    financial_multipliers_allowed: list[float] = Field(default_factory=lambda: list(CONFIRMED_FINANCIAL_MULTIPLIERS))
    financial_unknown_retains_cash: Literal[True] = True
    financial_zero_is_hard_exclude: Literal[True] = True
    final_target_notional_formula: Literal["base_slot_notional * size_multiplier * financial_multiplier"] = (
        "base_slot_notional * size_multiplier * financial_multiplier"
    )
    post_multiplier_notional_may_be_below_minimum_base_slot: Literal[True] = True
    lift_post_multiplier_notional_back_to_minimum_forbidden: Literal[True] = True
    rounding_compensation_forbidden: Literal[True] = True
    silent_conversion_of_downweight_into_hard_exclude_forbidden: Literal[True] = True
    note: str = (
        "Final target notional = base_slot_notional * size_multiplier * financial_multiplier. "
        "size allows only 0.5/0.75/1.0; financial allows only 0/0.5/1.0/unknown. "
        "financial=0 is hard exclude; financial=unknown retains cash. Post-multiplier "
        "notional may be below 8000. Lifting back to 8000, rounding compensation, and "
        "silently converting a downweight into a hard exclude are forbidden."
    )

    @model_validator(mode="after")
    def _freeze(self) -> RiskMultiplierInterpretation:
        if list(self.size_multipliers_allowed) != list(CONFIRMED_SIZE_MULTIPLIERS):
            raise ValueError("size_multipliers_allowed must equal [0.5, 0.75, 1.0]")
        if list(self.financial_multipliers_allowed) != list(CONFIRMED_FINANCIAL_MULTIPLIERS):
            raise ValueError("financial_multipliers_allowed must equal [0.0, 0.5, 1.0]")
        return self


class ReleasedCapitalPolicy(_StrictModel):
    v1_risk_multiplier_released_capital_stays_cash: Literal[True] = True
    same_day_transfer_to_other_candidates_forbidden: Literal[True] = True
    small_cap_backfill_forbidden: Literal[True] = True
    threshold_relaxation_forbidden: Literal[True] = True
    future_redistribution_requires_new_protocol_and_review: Literal[True] = True
    note: str = (
        "In v1, capital released by size/financial risk multipliers stays cash. It must "
        "not be transferred same-day to other candidates, must not backfill small caps, "
        "and must not relax thresholds. Any future redistribution among other eligible "
        "names requires a new protocol and review."
    )


class ClusterCapInterpretation(_StrictModel):
    cluster_cap_denominator: ClusterCapDenominator = "sleeve_budget"
    cluster_cap_denominator_is_not_invested_notional: Literal[True] = True
    max_sleeve_weight_per_cluster: float = CONFIRMED_CLUSTER_MAX_SLEEVE_WEIGHT
    max_positions_per_cluster: Literal[2] = CONFIRMED_CLUSTER_MAX_POSITIONS
    unknown_or_incomplete_cluster_report_retains_all_cash: Literal[True] = True
    note: str = (
        "Cluster cap denominator is fixed to sleeve_budget, not invested notional. "
        "Per-cluster sum of target notionals <= 0.35 * sleeve_budget and at most 2 names. "
        "Unknown/incomplete cluster reports retain all cash so cash does not mechanically "
        "inflate invested cluster weights."
    )

    @model_validator(mode="after")
    def _freeze(self) -> ClusterCapInterpretation:
        self.max_sleeve_weight_per_cluster = _require_exact_float(
            self.max_sleeve_weight_per_cluster,
            CONFIRMED_CLUSTER_MAX_SLEEVE_WEIGHT,
            field_name="max_sleeve_weight_per_cluster",
        )
        if self.cluster_cap_denominator != "sleeve_budget":
            raise ValueError("cluster_cap_denominator must remain sleeve_budget")
        return self


class ExecutionBoundary(_StrictModel):
    board_lot_shares: Literal[100] = 100
    board_lot_fees_limit_suspend_t_plus_1_open_are_execution_layer: Literal[True] = True
    unaffordable_one_lot_or_cost_gate_failure_retains_cash: Literal[True] = True
    must_not_raise_target_to_force_affordability: Literal[True] = True
    this_protocol_does_not_compute_orders: Literal[True] = True
    note: str = (
        "100-share board lots, fees, limit-up/limit-down/suspension, and T+1 open fills "
        "belong to a later execution layer. If the final target cannot afford one lot or "
        "fails a cost gate, that slot retains cash; targets must not be raised. This "
        "protocol does not compute orders."
    )


class CashRetentionPolicy(_StrictModel):
    retain_cash_on_candidate_shortage: Literal[True] = True
    retain_cash_on_unknown_critical_input: Literal[True] = True
    retain_cash_when_no_available_slot: Literal[True] = True
    retain_cash_when_unaffordable: Literal[True] = True
    never_reuse_other_date_candidate_lists: Literal[True] = True
    never_catch_up_fill: Literal[True] = True
    note: str = (
        "Candidate shortage, unknown critical inputs, no available slot, and unaffordable "
        "board-lot/cost outcomes all retain cash. Other-date candidate lists must not be "
        "reused and catch-up fills are forbidden."
    )


class ActiveCountInterpretation(_StrictModel):
    active_target_count_may_be_below_budget_cap: Literal[True] = True
    active_tranche_count_equals_active_target_count: Literal[True] = True
    holding_cycle_market_trading_days: Literal[40] = CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS
    holding_cycle_is_not_active_tranche_count: Literal[True] = True
    note: str = (
        "active_target_count may be below the budget-tier cap because of capital or "
        "candidate shortage. active_tranche_count = active_target_count. 40 is the "
        "holding/phase cycle length, not the active tranche count."
    )

    @model_validator(mode="after")
    def _freeze(self) -> ActiveCountInterpretation:
        if self.holding_cycle_market_trading_days != CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS:
            raise ValueError("holding_cycle_market_trading_days must equal 40")
        return self


class ReadinessGates(_StrictModel):
    research_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_score: Literal[True] = True
    does_not_backtest: Literal[True] = True
    does_not_construct_portfolio: Literal[True] = True
    does_not_compute_orders: Literal[True] = True
    does_not_trade: Literal[True] = True
    enhancement_layers_may_add_pit_industry_ownership_events_later: Literal[True] = True
    enhancement_layers_must_not_silently_rewrite_this_protocol: Literal[True] = True
    note: str = (
        "This is a read-only implementation interpretation protocol. All ready flags stay "
        "false. Future enhancement layers may add real PIT industry / ownership / events, "
        "but must not silently rewrite this protocol."
    )


class InterpretationInputPolicy(_StrictModel):
    """Fail-closed numeric input rules for interpretation helpers and examples."""

    bool_inputs_fail_closed: Literal[True] = True
    nan_inf_fail_closed: Literal[True] = True
    negative_equity_budget_or_notional_forbidden: Literal[True] = True
    zero_equity_allowed: Literal[True] = True
    note: str = (
        "Interpretation helpers and worked-example numeric fields reject bool "
        "(no coercion of True/False to 1.0/0.0), reject NaN/Inf, and reject negatives. "
        "Zero current_account_equity is allowed and yields no base slots under the "
        "confirmed formulas."
    )


class WorkedExample(_StrictModel):
    """Frozen narrative example; helpers recompute the same numbers in tests."""

    label: str = Field(min_length=1)
    current_account_equity: float
    risk_budget: float
    sleeve_budget: float
    budget_slot_cap: int
    base_slot_count: int
    base_slot_notional: float | None
    size_multiplier: float | None = None
    financial_multiplier: float | Literal["unknown"] | None = None
    final_target_notional: float | None = None
    cash_retention_reason: CashRetentionReason | None = None
    detail: str = Field(min_length=1)

    @field_validator("label", "detail", mode="before")
    @classmethod
    def _reject_blank(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)

    @field_validator(
        "current_account_equity",
        "risk_budget",
        "sleeve_budget",
        mode="before",
    )
    @classmethod
    def _reject_bool_nonnegative(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("base_slot_notional", "final_target_notional", mode="before")
    @classmethod
    def _reject_bool_optional_nonnegative(cls, value: object, info: Any) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("size_multiplier", mode="before")
    @classmethod
    def _reject_bool_size(cls, value: object) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name="size_multiplier", minimum=0.0, minimum_exclusive=True)

    @field_validator("financial_multiplier", mode="before")
    @classmethod
    def _reject_bool_financial(cls, value: object) -> object:
        if value is None or value == "unknown":
            return value
        return _require_real_number(value, field_name="financial_multiplier", minimum=0.0)

    @field_validator("budget_slot_cap", "base_slot_count", mode="before")
    @classmethod
    def _reject_bool_int(cls, value: object, info: Any) -> int:
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=0)


class LayerTwoAllocationImplementationProtocolV1(_StrictModel):
    schema_version: Literal["1"] = LAYER_TWO_ALLOCATION_PROTOCOL_SCHEMA_VERSION
    protocol_version: Literal["layer-two-allocation-implementation-protocol-v1"] = LAYER_TWO_ALLOCATION_PROTOCOL_VERSION
    status: ProtocolStatus = "confirmed_for_implementation_but_not_ready"
    confirmation_as_of: date = CONTRACT_CONFIRMATION_AS_OF
    two_layer_decision_contract_id: str = Field(min_length=1)
    two_layer_decision_contract_path: Literal["config/research/two-layer-strategy-decision-draft-v1.json"] = (
        BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
    )
    layer_one_index_protocol_id: str = Field(min_length=1)
    layer_one_index_protocol_path: Literal["config/research/layer-one-index-development-protocol-draft-v1.json"] = (
        BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH
    )
    tranche_evaluation_protocol_id: str = Field(min_length=1)
    tranche_evaluation_protocol_path: Literal["config/research/tranche-evaluation-protocol-draft-v1.json"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
    )
    capital_budget: CapitalBudgetInterpretation = Field(default_factory=CapitalBudgetInterpretation)
    base_slot: BaseSlotInterpretation = Field(default_factory=BaseSlotInterpretation)
    risk_multipliers: RiskMultiplierInterpretation = Field(default_factory=RiskMultiplierInterpretation)
    released_capital: ReleasedCapitalPolicy = Field(default_factory=ReleasedCapitalPolicy)
    cluster_cap: ClusterCapInterpretation = Field(default_factory=ClusterCapInterpretation)
    execution_boundary: ExecutionBoundary = Field(default_factory=ExecutionBoundary)
    cash_retention: CashRetentionPolicy = Field(default_factory=CashRetentionPolicy)
    active_counts: ActiveCountInterpretation = Field(default_factory=ActiveCountInterpretation)
    readiness: ReadinessGates = Field(default_factory=ReadinessGates)
    interpretation_inputs: InterpretationInputPolicy = Field(default_factory=InterpretationInputPolicy)
    worked_examples: list[WorkedExample]
    evidence_blockers: list[ProtocolEvidenceBlocker]
    pending_user_decisions: list[str] = Field(default_factory=list)
    protocol_id: str | None = None

    @field_validator(
        "two_layer_decision_contract_id",
        "layer_one_index_protocol_id",
        "tranche_evaluation_protocol_id",
        mode="before",
    )
    @classmethod
    def _reject_blank_ids(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)

    @field_validator("two_layer_decision_contract_path", mode="before")
    @classmethod
    def _reject_contract_path_escape(cls, value: object) -> object:
        return _assert_bound_relative_path(
            value,
            expected=BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
            field_name="two_layer_decision_contract_path",
        )

    @field_validator("layer_one_index_protocol_path", mode="before")
    @classmethod
    def _reject_layer_one_path_escape(cls, value: object) -> object:
        return _assert_bound_relative_path(
            value,
            expected=BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH,
            field_name="layer_one_index_protocol_path",
        )

    @field_validator("tranche_evaluation_protocol_path", mode="before")
    @classmethod
    def _reject_tranche_path_escape(cls, value: object) -> object:
        return _assert_bound_relative_path(
            value,
            expected=BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
            field_name="tranche_evaluation_protocol_path",
        )

    @field_validator("confirmation_as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, value: object) -> date:
        if isinstance(value, date) and type(value) is date:
            return value
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("confirmation_as_of must be an ISO date")
        return date.fromisoformat(value.strip())

    @model_validator(mode="after")
    def _gate_flags(self) -> LayerTwoAllocationImplementationProtocolV1:
        if self.status != "confirmed_for_implementation_but_not_ready":
            raise ValueError("status must be confirmed_for_implementation_but_not_ready")
        readiness = self.readiness
        if (
            readiness.ready_for_scoring
            or readiness.ready_for_backtest
            or readiness.ready_for_portfolio_construction
            or readiness.ready_for_orders
            or readiness.ready_for_trading
            or readiness.auto_apply
            or not readiness.research_only
        ):
            raise ValueError("allocation implementation protocol must remain research_only with all ready flags false")
        if self.pending_user_decisions:
            raise ValueError("confirmed protocol must have empty pending_user_decisions")
        if self.two_layer_decision_contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
            raise ValueError("two_layer_decision_contract_id does not match bound two-layer contract")
        if self.layer_one_index_protocol_id != BOUND_LAYER_ONE_INDEX_PROTOCOL_ID:
            raise ValueError("layer_one_index_protocol_id does not match bound layer-one protocol")
        if self.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
            raise ValueError("tranche_evaluation_protocol_id does not match bound tranche protocol")
        path_to_category: dict[str, str] = {}
        for blocker in self.evidence_blockers:
            if blocker.path in path_to_category:
                raise ValueError(f"evidence_blockers duplicate path: {blocker.path}")
            path_to_category[blocker.path] = blocker.category
        missing = [path for path in REQUIRED_ALLOCATION_EVIDENCE_BLOCKERS if path not in path_to_category]
        if missing:
            raise ValueError(f"evidence_blockers missing required paths: {missing}")
        wrong = [
            f"{path}->{path_to_category[path]} (expected {expected})"
            for path, expected in REQUIRED_ALLOCATION_EVIDENCE_BLOCKERS.items()
            if path_to_category[path] != expected
        ]
        if wrong:
            raise ValueError("evidence_blockers path->category mismatch: " + "; ".join(wrong))
        if not self.worked_examples:
            raise ValueError("worked_examples must document the confirmed interpretation")
        return self


class LayerTwoAllocationProtocolVerificationResult(_StrictModel):
    protocol_id: str
    schema_version: Literal["1"] = "1"
    protocol_version: str
    status: str
    structural_ok: bool
    two_layer_decision_contract_id: str
    two_layer_decision_contract_path: str
    two_layer_decision_contract_binding_ok: bool = False
    layer_one_index_protocol_id: str
    layer_one_index_protocol_path: str
    layer_one_index_protocol_binding_ok: bool = False
    tranche_evaluation_protocol_id: str
    tranche_evaluation_protocol_path: str
    tranche_evaluation_protocol_binding_ok: bool = False
    resolved: bool
    user_decisions_resolved: bool
    pending_user_decision_count: int
    pending_user_decisions: list[str] = Field(default_factory=list)
    blockers: list[str]
    evidence_blockers: list[ProtocolEvidenceBlocker] = Field(default_factory=list)
    research_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_score: Literal[True] = True
    does_not_backtest: Literal[True] = True
    does_not_construct_portfolio: Literal[True] = True
    does_not_compute_orders: Literal[True] = True
    does_not_trade: Literal[True] = True


class BaseSlotPlan(_StrictModel):
    current_account_equity: float
    risk_budget: float
    sleeve_budget: float
    budget_slot_cap: int
    base_slot_count: int
    base_slot_notional: float | None
    cash_retention_reason: CashRetentionReason | None = None

    @field_validator("current_account_equity", "risk_budget", "sleeve_budget", mode="before")
    @classmethod
    def _reject_bool_nonnegative(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("base_slot_notional", mode="before")
    @classmethod
    def _reject_bool_optional_notional(cls, value: object) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name="base_slot_notional", minimum=0.0)

    @field_validator("budget_slot_cap", "base_slot_count", mode="before")
    @classmethod
    def _reject_bool_int(cls, value: object, info: Any) -> int:
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=0)

    @model_validator(mode="after")
    def _consistency(self) -> BaseSlotPlan:
        if self.base_slot_count == 0:
            if self.base_slot_notional is not None:
                raise ValueError("base_slot_notional must be null when base_slot_count is 0")
            if self.cash_retention_reason is None:
                raise ValueError("cash_retention_reason required when base_slot_count is 0")
        else:
            if self.base_slot_notional is None:
                raise ValueError("base_slot_notional required when base_slot_count > 0")
            if self.base_slot_notional + _NOTIONAL_ABS_TOL < CONFIRMED_MINIMUM_BASE_SLOT_NOTIONAL_CNY:
                raise ValueError("pre-multiplier base_slot_notional must be >= 8000")
        return self


class FinalTargetPlan(_StrictModel):
    base_slot_notional: float
    size_multiplier: float
    financial_multiplier: float | Literal["unknown"]
    final_target_notional: float | None
    hard_excluded: bool = False
    retain_cash: bool = False
    cash_retention_reason: CashRetentionReason | None = None
    released_capital_stays_cash: Literal[True] = True

    @field_validator("base_slot_notional", "size_multiplier", mode="before")
    @classmethod
    def _reject_bool_positive(cls, value: object, info: Any) -> float:
        return _require_real_number(
            value,
            field_name=str(info.field_name),
            minimum=0.0,
            minimum_exclusive=True,
        )

    @field_validator("final_target_notional", mode="before")
    @classmethod
    def _reject_bool_optional_final(cls, value: object) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name="final_target_notional", minimum=0.0)

    @field_validator("financial_multiplier", mode="before")
    @classmethod
    def _reject_bool_financial(cls, value: object) -> object:
        if value == "unknown":
            return value
        return _require_real_number(value, field_name="financial_multiplier", minimum=0.0)

    @model_validator(mode="after")
    def _consistency(self) -> FinalTargetPlan:
        if self.financial_multiplier == "unknown":
            if not self.retain_cash or self.final_target_notional is not None:
                raise ValueError("financial unknown must retain cash with null final target")
            return self
        if self.financial_multiplier == 0.0:
            if not self.hard_excluded or self.final_target_notional is not None:
                raise ValueError("financial zero must hard-exclude with null final target")
            return self
        if self.final_target_notional is None:
            raise ValueError("final_target_notional required for non-zero known financial multiplier")
        expected = self.base_slot_notional * self.size_multiplier * float(self.financial_multiplier)
        if abs(self.final_target_notional - expected) > _NOTIONAL_ABS_TOL:
            raise ValueError("final_target_notional must equal base * size * financial")
        return self


def _assert_bound_relative_path(value: object, *, expected: str, field_name: str) -> object:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be relative without parent traversal")
    if value != expected:
        raise ValueError(f"{field_name} does not match bound path")
    return value


def _budget_key(risk_budget: float) -> str:
    for level in CONFIRMED_RISK_BUDGET_LEVELS:
        if abs(risk_budget - level) <= _BUDGET_ABS_TOL:
            return f"{level}"
    raise ValueError("risk_budget must be one of 0.0/0.3/0.6/0.9")


def assert_allowed_risk_budget(risk_budget: object) -> float:
    number = _require_real_number(risk_budget, field_name="risk_budget", minimum=0.0)
    key = _budget_key(number)
    return float(key)


def max_active_slots_for_budget(risk_budget: object) -> int:
    key = _budget_key(assert_allowed_risk_budget(risk_budget))
    return CONFIRMED_MAX_ACTIVE_SLOTS_BY_BUDGET[key]


def compute_sleeve_budget(*, current_account_equity: object, risk_budget: object) -> float:
    equity = _require_real_number(current_account_equity, field_name="current_account_equity", minimum=0.0)
    budget = assert_allowed_risk_budget(risk_budget)
    sleeve = equity * budget
    if sleeve - equity > _NOTIONAL_ABS_TOL:
        raise ValueError("sleeve_budget must not overspend current_account_equity")
    return sleeve


def plan_base_slots(*, current_account_equity: object, risk_budget: object) -> BaseSlotPlan:
    """Deterministic pre-multiplier slot plan (interpretation helper only)."""
    equity = _require_real_number(current_account_equity, field_name="current_account_equity", minimum=0.0)
    budget = assert_allowed_risk_budget(risk_budget)
    sleeve = compute_sleeve_budget(current_account_equity=equity, risk_budget=budget)
    cap = max_active_slots_for_budget(budget)
    if abs(budget) <= _BUDGET_ABS_TOL or cap == 0:
        return BaseSlotPlan(
            current_account_equity=equity,
            risk_budget=budget,
            sleeve_budget=sleeve,
            budget_slot_cap=cap,
            base_slot_count=0,
            base_slot_notional=None,
            cash_retention_reason="zero_risk_budget",
        )
    affordable = int(math.floor(sleeve / CONFIRMED_MINIMUM_BASE_SLOT_NOTIONAL_CNY + 1e-15))
    count = min(cap, affordable)
    if count == 0:
        return BaseSlotPlan(
            current_account_equity=equity,
            risk_budget=budget,
            sleeve_budget=sleeve,
            budget_slot_cap=cap,
            base_slot_count=0,
            base_slot_notional=None,
            cash_retention_reason="insufficient_capital_for_minimum_base_slot",
        )
    notional = sleeve / count
    return BaseSlotPlan(
        current_account_equity=equity,
        risk_budget=budget,
        sleeve_budget=sleeve,
        budget_slot_cap=cap,
        base_slot_count=count,
        base_slot_notional=notional,
        cash_retention_reason=None,
    )


def assert_allowed_size_multiplier(size_multiplier: object) -> float:
    number = _require_real_number(
        size_multiplier,
        field_name="size_multiplier",
        minimum=0.0,
        minimum_exclusive=True,
    )
    for allowed in CONFIRMED_SIZE_MULTIPLIERS:
        if abs(number - allowed) <= _BUDGET_ABS_TOL:
            return allowed
    raise ValueError("size_multiplier must be one of 0.5/0.75/1.0")


def plan_final_target_notional(
    *,
    base_slot_notional: object,
    size_multiplier: object,
    financial_multiplier: object,
) -> FinalTargetPlan:
    """Apply size/financial multipliers without lifting below-minimum results."""
    base = _require_real_number(
        base_slot_notional,
        field_name="base_slot_notional",
        minimum=0.0,
        minimum_exclusive=True,
    )
    if base + _NOTIONAL_ABS_TOL < CONFIRMED_MINIMUM_BASE_SLOT_NOTIONAL_CNY:
        raise ValueError("base_slot_notional must be >= minimum_base_slot_notional before multipliers")
    size = assert_allowed_size_multiplier(size_multiplier)
    if financial_multiplier == "unknown":
        return FinalTargetPlan(
            base_slot_notional=base,
            size_multiplier=size,
            financial_multiplier="unknown",
            final_target_notional=None,
            hard_excluded=False,
            retain_cash=True,
            cash_retention_reason="financial_unknown",
        )
    financial = _require_real_number(financial_multiplier, field_name="financial_multiplier", minimum=0.0)
    matched: float | None = None
    for allowed in CONFIRMED_FINANCIAL_MULTIPLIERS:
        if abs(financial - allowed) <= _BUDGET_ABS_TOL:
            matched = allowed
            break
    if matched is None:
        raise ValueError("financial_multiplier must be one of 0.0/0.5/1.0/unknown")
    if matched == 0.0:
        return FinalTargetPlan(
            base_slot_notional=base,
            size_multiplier=size,
            financial_multiplier=0.0,
            final_target_notional=None,
            hard_excluded=True,
            retain_cash=True,
            cash_retention_reason="financial_hard_exclude",
        )
    final = base * size * matched
    # Explicitly do not lift final back to 8000.
    released = final + _NOTIONAL_ABS_TOL < base
    return FinalTargetPlan(
        base_slot_notional=base,
        size_multiplier=size,
        financial_multiplier=matched,
        final_target_notional=final,
        hard_excluded=False,
        retain_cash=False,
        cash_retention_reason="risk_multiplier_released_capital" if released else None,
    )


def cluster_notional_cap(*, sleeve_budget: object) -> float:
    sleeve = _require_real_number(sleeve_budget, field_name="sleeve_budget", minimum=0.0)
    return sleeve * CONFIRMED_CLUSTER_MAX_SLEEVE_WEIGHT


def default_allocation_evidence_blockers() -> list[ProtocolEvidenceBlocker]:
    return [
        ProtocolEvidenceBlocker(
            path="layer_two_constrained_allocator",
            category="pending_implementation",
            detail=(
                "Confirmed allocation interpretation is frozen but the constrained "
                "layer-two allocator that applies these rules is not implemented."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="execution_board_lot_and_cost_gates",
            category="pending_implementation",
            detail=(
                "Board-lot / fee / limit / suspension / T+1 open execution gates remain "
                "outside this protocol and are not implemented here."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="alpha_weight_selection",
            category="pending_development_evidence",
            detail=(
                "Alpha family weights remain pending development evidence; this protocol "
                "does not invent weights or wire scoring."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="pit_industry_history",
            category="future_enhancement",
            detail=(
                "Real PIT industry history remains a future enhancement; statistical "
                "clusters stay the current proxy. Enhancement must not silently rewrite "
                "this protocol."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="ownership_and_event_hard_rules",
            category="future_enhancement",
            detail=(
                "Ownership / event hard rules remain future enhancement after coverage "
                "gates; they must not silently rewrite this protocol."
            ),
        ),
    ]


def default_worked_examples() -> list[WorkedExample]:
    """Canonical examples sealed into the protocol document."""
    eighty_thirty = plan_base_slots(current_account_equity=80_000.0, risk_budget=0.3)
    assert eighty_thirty.base_slot_notional is not None
    halved = plan_final_target_notional(
        base_slot_notional=eighty_thirty.base_slot_notional,
        size_multiplier=0.5,
        financial_multiplier=0.5,
    )
    seventy_thirty = plan_base_slots(current_account_equity=70_000.0, risk_budget=0.3)
    hundred_thirty = plan_base_slots(current_account_equity=100_000.0, risk_budget=0.3)
    return [
        WorkedExample(
            label="equity_80k_budget_30pct_three_base_slots",
            current_account_equity=80_000.0,
            risk_budget=0.3,
            sleeve_budget=eighty_thirty.sleeve_budget,
            budget_slot_cap=eighty_thirty.budget_slot_cap,
            base_slot_count=eighty_thirty.base_slot_count,
            base_slot_notional=eighty_thirty.base_slot_notional,
            detail=("80k equity at 30% risk budget -> sleeve_budget=24000 -> 3 base slots x 8000 before multipliers."),
        ),
        WorkedExample(
            label="size_0_5_financial_0_5_post_multiplier_2k",
            current_account_equity=80_000.0,
            risk_budget=0.3,
            sleeve_budget=eighty_thirty.sleeve_budget,
            budget_slot_cap=eighty_thirty.budget_slot_cap,
            base_slot_count=eighty_thirty.base_slot_count,
            base_slot_notional=eighty_thirty.base_slot_notional,
            size_multiplier=0.5,
            financial_multiplier=0.5,
            final_target_notional=halved.final_target_notional,
            cash_retention_reason="risk_multiplier_released_capital",
            detail=(
                "3-5bn free-float size band (x0.5) plus one financial warning (x0.5) "
                "yields final target 2000 per slot. Post-multiplier may be below 8000; "
                "must not lift back to 8000. If execution cannot afford one board lot, "
                "retain cash without raising the target."
            ),
        ),
        WorkedExample(
            label="equity_70k_budget_30pct_two_slots",
            current_account_equity=70_000.0,
            risk_budget=0.3,
            sleeve_budget=seventy_thirty.sleeve_budget,
            budget_slot_cap=seventy_thirty.budget_slot_cap,
            base_slot_count=seventy_thirty.base_slot_count,
            base_slot_notional=seventy_thirty.base_slot_notional,
            detail="70k * 0.3 = 21000 -> floor(21000/8000)=2 -> two base slots of 10500.",
        ),
        WorkedExample(
            label="equity_100k_budget_30pct_three_slots",
            current_account_equity=100_000.0,
            risk_budget=0.3,
            sleeve_budget=hundred_thirty.sleeve_budget,
            budget_slot_cap=hundred_thirty.budget_slot_cap,
            base_slot_count=hundred_thirty.base_slot_count,
            base_slot_notional=hundred_thirty.base_slot_notional,
            detail="100k * 0.3 = 30000 -> three base slots of 10000 (cap still 3).",
        ),
    ]


def canonical_protocol_payload(draft: LayerTwoAllocationImplementationProtocolV1) -> dict[str, Any]:
    return draft.model_dump(mode="json", exclude={"protocol_id"})


def canonical_protocol_bytes(draft: LayerTwoAllocationImplementationProtocolV1) -> bytes:
    payload = canonical_protocol_payload(draft)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_protocol_id(draft: LayerTwoAllocationImplementationProtocolV1) -> str:
    return hashlib.sha256(canonical_protocol_bytes(draft)).hexdigest()


def seal_layer_two_allocation_protocol(
    draft: LayerTwoAllocationImplementationProtocolV1,
) -> LayerTwoAllocationImplementationProtocolV1:
    return draft.model_copy(update={"protocol_id": compute_protocol_id(draft)})


def build_confirmed_layer_two_allocation_protocol_v1(
    *,
    two_layer_decision_contract_id: str = BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    two_layer_decision_contract_path: Literal[
        "config/research/two-layer-strategy-decision-draft-v1.json"
    ] = BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
    layer_one_index_protocol_id: str = BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
    layer_one_index_protocol_path: Literal[
        "config/research/layer-one-index-development-protocol-draft-v1.json"
    ] = BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH,
    tranche_evaluation_protocol_id: str = BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
    tranche_evaluation_protocol_path: Literal[
        "config/research/tranche-evaluation-protocol-draft-v1.json"
    ] = BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
    confirmation_as_of: date = CONTRACT_CONFIRMATION_AS_OF,
) -> LayerTwoAllocationImplementationProtocolV1:
    draft = LayerTwoAllocationImplementationProtocolV1(
        confirmation_as_of=confirmation_as_of,
        two_layer_decision_contract_id=two_layer_decision_contract_id,
        two_layer_decision_contract_path=two_layer_decision_contract_path,
        layer_one_index_protocol_id=layer_one_index_protocol_id,
        layer_one_index_protocol_path=layer_one_index_protocol_path,
        tranche_evaluation_protocol_id=tranche_evaluation_protocol_id,
        tranche_evaluation_protocol_path=tranche_evaluation_protocol_path,
        worked_examples=default_worked_examples(),
        evidence_blockers=default_allocation_evidence_blockers(),
        pending_user_decisions=[],
    )
    return seal_layer_two_allocation_protocol(draft)


def assert_protocol_self_hash(draft: LayerTwoAllocationImplementationProtocolV1) -> None:
    if draft.protocol_id is None:
        raise ValueError("layer-two allocation protocol_id is missing")
    expected = compute_protocol_id(draft)
    if draft.protocol_id != expected:
        raise ValueError("layer-two allocation protocol_id does not match canonical content hash")


def assert_status_ready_consistency(draft: LayerTwoAllocationImplementationProtocolV1) -> None:
    readiness = draft.readiness
    if (
        readiness.ready_for_scoring
        or readiness.ready_for_backtest
        or readiness.ready_for_portfolio_construction
        or readiness.ready_for_orders
        or readiness.ready_for_trading
        or readiness.auto_apply
    ):
        raise ValueError("status/ready contradiction: ready flags must remain false")
    if draft.status != "confirmed_for_implementation_but_not_ready":
        raise ValueError("status/ready contradiction: status must be confirmed_for_implementation_but_not_ready")
    if not readiness.research_only:
        raise ValueError("status/ready contradiction: research_only must remain true")


def assert_bound_upstream_ids(draft: LayerTwoAllocationImplementationProtocolV1) -> None:
    if draft.two_layer_decision_contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
        raise ValueError("two_layer_decision_contract_id does not match bound two-layer contract")
    if draft.two_layer_decision_contract_path != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two_layer_decision_contract_path does not match bound two-layer contract path")
    if draft.layer_one_index_protocol_id != BOUND_LAYER_ONE_INDEX_PROTOCOL_ID:
        raise ValueError("layer_one_index_protocol_id does not match bound layer-one protocol")
    if draft.layer_one_index_protocol_path != BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH:
        raise ValueError("layer_one_index_protocol_path does not match bound layer-one protocol path")
    if draft.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("tranche_evaluation_protocol_id does not match bound tranche protocol")
    if draft.tranche_evaluation_protocol_path != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
        raise ValueError("tranche_evaluation_protocol_path does not match bound tranche protocol path")


def assert_matches_canonical_factory(draft: LayerTwoAllocationImplementationProtocolV1) -> None:
    """Reject outer reseal of drifted semantics/notes/blockers/examples.

    A confirmed protocol must match the unique factory canonical payload and
    protocol_id. Self-hash alone is insufficient against resealed semantic drift.
    """
    canonical = build_confirmed_layer_two_allocation_protocol_v1()
    if draft.protocol_id != canonical.protocol_id:
        raise ValueError("layer-two allocation protocol_id does not match sealed factory canonical protocol_id")
    if canonical_protocol_payload(draft) != canonical_protocol_payload(canonical):
        raise ValueError("layer-two allocation protocol canonical payload does not match sealed factory")


def verify_layer_two_allocation_protocol(
    draft: LayerTwoAllocationImplementationProtocolV1,
) -> LayerTwoAllocationProtocolVerificationResult:
    assert_protocol_self_hash(draft)
    assert_status_ready_consistency(draft)
    assert_bound_upstream_ids(draft)
    assert_matches_canonical_factory(draft)
    path_blockers = [f"{b.category}:{b.path}" for b in draft.evidence_blockers]
    return LayerTwoAllocationProtocolVerificationResult(
        protocol_id=draft.protocol_id or compute_protocol_id(draft),
        schema_version="1",
        protocol_version=draft.protocol_version,
        status=draft.status,
        structural_ok=True,
        two_layer_decision_contract_id=draft.two_layer_decision_contract_id,
        two_layer_decision_contract_path=draft.two_layer_decision_contract_path,
        two_layer_decision_contract_binding_ok=False,
        layer_one_index_protocol_id=draft.layer_one_index_protocol_id,
        layer_one_index_protocol_path=draft.layer_one_index_protocol_path,
        layer_one_index_protocol_binding_ok=False,
        tranche_evaluation_protocol_id=draft.tranche_evaluation_protocol_id,
        tranche_evaluation_protocol_path=draft.tranche_evaluation_protocol_path,
        tranche_evaluation_protocol_binding_ok=False,
        resolved=False,
        user_decisions_resolved=True,
        pending_user_decision_count=0,
        pending_user_decisions=[],
        blockers=path_blockers,
        evidence_blockers=list(draft.evidence_blockers),
    )


def _assert_repo_relative_path(
    value: str,
    *,
    repo_root: Path,
    expected: str,
    field_name: str,
) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError(f"{field_name} must be a relative path without parent traversal")
    if value != expected:
        raise ValueError(f"{field_name} does not match bound path")
    resolved = (repo_root / path).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"{field_name} escapes repository root") from exc
    if not resolved.is_file():
        raise ValueError(f"{field_name} does not exist: {value}")
    return resolved


def load_layer_two_allocation_protocol(path: Path) -> LayerTwoAllocationImplementationProtocolV1:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("layer-two allocation protocol is missing or invalid") from exc
    if isinstance(payload, dict) and "tranche_count" in payload:
        raise ValueError(
            "allocation protocol rejects tranche_count; 40 is the holding/phase cycle length, "
            "not an active tranche count"
        )
    cluster_cap = payload.get("cluster_cap") if isinstance(payload, dict) else None
    if isinstance(cluster_cap, dict) and cluster_cap.get("cluster_cap_denominator") == "invested_notional":
        raise ValueError("cluster_cap_denominator must be sleeve_budget, not invested_notional")
    try:
        return LayerTwoAllocationImplementationProtocolV1.model_validate(payload)
    except Exception as exc:
        raise ValueError("layer-two allocation protocol is missing or invalid") from exc


def verify_layer_two_allocation_protocol_file(
    *,
    protocol_path: Path,
    repo_root: Path,
    reference_date: date | None = None,
) -> tuple[LayerTwoAllocationImplementationProtocolV1, LayerTwoAllocationProtocolVerificationResult]:
    root = Path(repo_root).resolve()
    draft = load_layer_two_allocation_protocol(protocol_path)
    structural = verify_layer_two_allocation_protocol(draft)

    contract_path = _assert_repo_relative_path(
        draft.two_layer_decision_contract_path,
        repo_root=root,
        expected=BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
        field_name="two_layer_decision_contract_path",
    )
    contract = load_two_layer_decision_draft(contract_path)
    contract_result = verify_two_layer_decision_draft(contract, reference_date=reference_date)
    if contract_result.schema_version != "2":
        raise ValueError("bound two-layer decision contract must be schema version 2")
    if contract_result.contract_id != draft.two_layer_decision_contract_id:
        raise ValueError(
            "two-layer decision contract_id on disk does not match protocol two_layer_decision_contract_id"
        )
    if contract_result.contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
        raise ValueError("two-layer decision contract_id on disk does not match bound constant")
    if str(DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH) != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two-layer decision contract default path drifted from protocol binding")

    layer_one_path = _assert_repo_relative_path(
        draft.layer_one_index_protocol_path,
        repo_root=root,
        expected=BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH,
        field_name="layer_one_index_protocol_path",
    )
    layer_one = load_layer_one_index_protocol_draft(layer_one_path)
    layer_one_result = verify_layer_one_index_protocol_draft(layer_one, reference_date=reference_date)
    if layer_one_result.schema_version != "2":
        raise ValueError("bound layer-one index protocol must be schema version 2")
    if layer_one_result.protocol_id != draft.layer_one_index_protocol_id:
        raise ValueError("layer-one index protocol_id on disk does not match protocol layer_one_index_protocol_id")
    if layer_one_result.protocol_id != BOUND_LAYER_ONE_INDEX_PROTOCOL_ID:
        raise ValueError("layer-one index protocol_id on disk does not match bound constant")
    if str(DEFAULT_LAYER_ONE_INDEX_PROTOCOL_DRAFT_PATH) != BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH:
        raise ValueError("layer-one index protocol default path drifted from protocol binding")

    tranche_path = _assert_repo_relative_path(
        draft.tranche_evaluation_protocol_path,
        repo_root=root,
        expected=BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
        field_name="tranche_evaluation_protocol_path",
    )
    tranche = load_tranche_evaluation_protocol_draft(tranche_path)
    tranche_result = verify_tranche_evaluation_protocol_draft(tranche, reference_date=reference_date)
    if tranche_result.schema_version != "2":
        raise ValueError("bound tranche evaluation protocol must be schema version 2")
    if tranche_result.protocol_id != draft.tranche_evaluation_protocol_id:
        raise ValueError(
            "tranche evaluation protocol_id on disk does not match protocol tranche_evaluation_protocol_id"
        )
    if tranche_result.protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("tranche evaluation protocol_id on disk does not match bound constant")
    if str(DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH) != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
        raise ValueError("tranche evaluation protocol default path drifted from protocol binding")

    result = structural.model_copy(
        update={
            "two_layer_decision_contract_binding_ok": True,
            "layer_one_index_protocol_binding_ok": True,
            "tranche_evaluation_protocol_binding_ok": True,
        }
    )
    return draft, result


def write_layer_two_allocation_protocol(
    path: Path,
    draft: LayerTwoAllocationImplementationProtocolV1,
) -> LayerTwoAllocationImplementationProtocolV1:
    sealed = seal_layer_two_allocation_protocol(draft)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_LAYER_ONE_INDEX_PROTOCOL_ID",
    "BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH",
    "BOUND_TRANCHE_EVALUATION_PROTOCOL_ID",
    "BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_ID",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_PATH",
    "CONFIRMED_CLUSTER_MAX_POSITIONS",
    "CONFIRMED_CLUSTER_MAX_SLEEVE_WEIGHT",
    "CONFIRMED_FINANCIAL_MULTIPLIERS",
    "CONFIRMED_INITIAL_CASH",
    "CONFIRMED_MAX_ACTIVE_SLOTS_BY_BUDGET",
    "CONFIRMED_MINIMUM_BASE_SLOT_NOTIONAL_CNY",
    "CONFIRMED_RISK_BUDGET_LEVELS",
    "CONFIRMED_SIZE_MULTIPLIERS",
    "DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH",
    "LAYER_TWO_ALLOCATION_PROTOCOL_SCHEMA_VERSION",
    "LAYER_TWO_ALLOCATION_PROTOCOL_VERSION",
    "REQUIRED_ALLOCATION_EVIDENCE_BLOCKERS",
    "BaseSlotPlan",
    "FinalTargetPlan",
    "InterpretationInputPolicy",
    "LayerTwoAllocationImplementationProtocolV1",
    "LayerTwoAllocationProtocolVerificationResult",
    "ProtocolEvidenceBlocker",
    "WorkedExample",
    "assert_allowed_risk_budget",
    "assert_allowed_size_multiplier",
    "assert_bound_upstream_ids",
    "assert_matches_canonical_factory",
    "assert_protocol_self_hash",
    "assert_status_ready_consistency",
    "build_confirmed_layer_two_allocation_protocol_v1",
    "canonical_protocol_bytes",
    "canonical_protocol_payload",
    "cluster_notional_cap",
    "compute_protocol_id",
    "compute_sleeve_budget",
    "default_allocation_evidence_blockers",
    "default_worked_examples",
    "load_layer_two_allocation_protocol",
    "max_active_slots_for_budget",
    "plan_base_slots",
    "plan_final_target_notional",
    "seal_layer_two_allocation_protocol",
    "verify_layer_two_allocation_protocol",
    "verify_layer_two_allocation_protocol_file",
    "write_layer_two_allocation_protocol",
]
