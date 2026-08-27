"""Layer-two T+1 entry execution diagnostic (E10e-0).

Research-only simulation label for one sealed E10d-3 allocator result.
Consumes explicit next-day execution observations; never feeds ranking/scoring,
never emits orders, and never claims a live fill.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.backtest.costs import LOT_SIZE, apply_slippage, buy_cost, shares_affordable
from app.models.config import CostConfig
from app.research.layer_two_allocation_protocol import _require_non_bool_int, _require_real_number
from app.research.layer_two_candidate_eligibility import LayerTwoCandidateEligibilityReport
from app.research.layer_two_constraint_assembler import LayerTwoConstraintAssemblerReport
from app.research.layer_two_financial_negative_list import LayerTwoFinancialNegativeListReport
from app.research.layer_two_stateful_allocator import (
    LayerTwoStatefulAllocatorReport,
    LayerTwoStatefulPortfolioState,
    UnvalidatedDevelopmentRankingInput,
    assert_state_self_hash,
    verify_layer_two_stateful_allocator_report,
    verify_layer_two_stateful_allocator_report_file,
)
from app.research.layer_two_stateful_allocator import (
    assert_report_self_hash as assert_allocator_report_self_hash,
)
from app.research.layer_two_statistical_risk_clusters import LayerTwoStatisticalRiskClusterReport
from app.research.layer_two_tranche_phase_schedule import (
    LayerTwoTranchePhaseScheduleReport,
    verify_layer_two_tranche_phase_schedule_report,
)
from app.research.layer_two_tranche_phase_schedule import (
    assert_report_self_hash as assert_phase_report_self_hash,
)
from app.research.tranche_evaluation_protocol import (
    DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH,
    TrancheEvaluationProtocolV2,
    verify_tranche_evaluation_protocol_draft_file,
)
from app.storage.protocol import MarketStore

LAYER_TWO_ENTRY_EXECUTION_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_ENTRY_EXECUTION_ENGINE_VERSION: Literal["layer-two-entry-execution-diagnostic-v1"] = (
    "layer-two-entry-execution-diagnostic-v1"
)

BOUND_TRANCHE_EVALUATION_PROTOCOL_ID: Literal["8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a"] = (
    "8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a"
)
BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH: Literal["config/research/tranche-evaluation-protocol-draft-v1.json"] = (
    "config/research/tranche-evaluation-protocol-draft-v1.json"
)

# A-share board lot; matches app.backtest.costs.LOT_SIZE and frozen execution contracts.
BOUND_BOARD_LOT_SIZE: Literal[100] = 100

BOUND_BASE_COMMISSION_PER_SIDE: float = 0.00025
BOUND_MINIMUM_COMMISSION_CNY: float = 5.0
BOUND_BASE_SLIPPAGE_BPS: Literal[5] = 5
BOUND_STRESS_SLIPPAGE_BPS: Literal[15] = 15

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NOTIONAL_ABS_TOL = 1e-9

ObservationStatus = Literal["unknown", "known_full_day_suspension", "tradable"]
ExecutionOutcome = Literal[
    "not_attempted",
    "unknown_execution_observation",
    "blocked_suspension",
    "blocked_limit_up",
    "unaffordable_board_lot_or_minimum_commission",
    "hypothetically_fillable",
]
ScenarioLabel = Literal["base_5bps", "stress_15bps"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_aware_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _require_date(value: object, *, field_name: str) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str) and value.strip():
        return date.fromisoformat(value.strip())
    raise ValueError(f"{field_name} must be a datetime.date")


def _require_sealed_hex64(value: str | None, *, field_name: str) -> str:
    if value is None or not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sealed 64-char lowercase hex digest")
    return value


def _require_literal_true(value: object, *, field_name: str) -> Literal[True]:
    if value is not True:
        raise ValueError(f"{field_name} must be the boolean True")
    return True


def _require_literal_false(value: object, *, field_name: str) -> Literal[False]:
    if value is not False:
        raise ValueError(f"{field_name} must be the boolean False")
    return False


class LayerTwoEntryExecutionObservation(_StrictModel):
    """Explicit T+1 open observation for the proposed symbol only."""

    symbol: str = Field(min_length=1)
    execution_date: date
    market_data_snapshot_id: str = Field(min_length=1)
    observation_status: ObservationStatus
    raw_open: float | None = None
    published_up_limit: float | None = None
    availability_note: str | None = None

    @field_validator("execution_date", mode="before")
    @classmethod
    def _date(cls, value: object) -> date:
        return _require_date(value, field_name="execution_date")

    @field_validator("symbol", "market_data_snapshot_id", mode="before")
    @classmethod
    def _nonblank(cls, value: object, info: Any) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value.strip()

    @field_validator("raw_open", "published_up_limit", mode="before")
    @classmethod
    def _optional_price(cls, value: object, info: Any) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0, minimum_exclusive=True)

    @model_validator(mode="after")
    def _status_fields(self) -> LayerTwoEntryExecutionObservation:
        if self.observation_status == "unknown":
            if self.raw_open is not None or self.published_up_limit is not None:
                raise ValueError("unknown observation must not populate raw_open/published_up_limit")
            return self
        if self.observation_status == "known_full_day_suspension":
            if self.raw_open is not None or self.published_up_limit is not None:
                raise ValueError("suspension observation must keep raw_open/published_up_limit null")
            return self
        # tradable
        if self.raw_open is None or self.published_up_limit is None:
            raise ValueError("tradable observation requires positive raw_open and published_up_limit")
        return self


class EntryCostScenarioRow(_StrictModel):
    """Hypothetical buy arithmetic for one slippage scenario; not an order."""

    scenario_label: ScenarioLabel
    slippage_bps: int
    hypothetical_fill_price: float
    affordable_shares: int
    affordable_lots: int
    stock_notional: float
    commission: float
    total_cash_used: float
    unused_target_cash: float
    can_afford_one_lot: bool
    legal_limit_cap_applied: bool
    is_hypothetical_only: Literal[True] = True
    is_not_a_fill_or_order: Literal[True] = True

    @field_validator("slippage_bps", "affordable_shares", "affordable_lots", mode="before")
    @classmethod
    def _ints(cls, value: object, info: Any) -> int:
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=0)

    @field_validator(
        "hypothetical_fill_price",
        "stock_notional",
        "commission",
        "total_cash_used",
        "unused_target_cash",
        mode="before",
    )
    @classmethod
    def _nums(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("can_afford_one_lot", "legal_limit_cap_applied", mode="before")
    @classmethod
    def _strict_bool(cls, value: object, info: Any) -> bool:
        if type(value) is not bool:
            raise ValueError(f"{info.field_name} must be a boolean")
        return value

    @field_validator("is_hypothetical_only", "is_not_a_fill_or_order", mode="before")
    @classmethod
    def _true_flags(cls, value: object, info: Any) -> object:
        return _require_literal_true(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _arithmetic(self) -> EntryCostScenarioRow:
        expected_bps = BOUND_BASE_SLIPPAGE_BPS if self.scenario_label == "base_5bps" else BOUND_STRESS_SLIPPAGE_BPS
        if self.scenario_label == "base_5bps" and self.slippage_bps != BOUND_BASE_SLIPPAGE_BPS:
            raise ValueError("base_5bps scenario_label requires slippage_bps=5")
        if self.scenario_label == "stress_15bps" and self.slippage_bps != BOUND_STRESS_SLIPPAGE_BPS:
            raise ValueError("stress_15bps scenario_label requires slippage_bps=15")
        if self.slippage_bps != expected_bps:
            raise ValueError("scenario_label does not match slippage_bps")
        if self.affordable_shares % BOUND_BOARD_LOT_SIZE != 0:
            raise ValueError("affordable_shares must be a multiple of board lot size")
        if self.affordable_lots * BOUND_BOARD_LOT_SIZE != self.affordable_shares:
            raise ValueError("affordable_lots must equal affordable_shares / lot_size")
        if self.can_afford_one_lot is not (self.affordable_shares >= BOUND_BOARD_LOT_SIZE):
            raise ValueError("can_afford_one_lot must equal affordable_shares >= 100")
        expected_notional = self.hypothetical_fill_price * float(self.affordable_shares)
        if abs(self.stock_notional - expected_notional) > _NOTIONAL_ABS_TOL:
            raise ValueError("stock_notional must equal hypothetical_fill_price * affordable_shares")
        if abs(self.total_cash_used - (self.stock_notional + self.commission)) > _NOTIONAL_ABS_TOL:
            raise ValueError("total_cash_used must equal stock_notional + commission")
        if self.affordable_shares == 0:
            if (
                abs(self.stock_notional) > _NOTIONAL_ABS_TOL
                or abs(self.commission) > _NOTIONAL_ABS_TOL
                or abs(self.total_cash_used) > _NOTIONAL_ABS_TOL
            ):
                raise ValueError("zero shares requires zero notional/commission/total_cash_used")
        else:
            expected_comm = max(self.stock_notional * BOUND_BASE_COMMISSION_PER_SIDE, BOUND_MINIMUM_COMMISSION_CNY)
            if abs(self.commission - expected_comm) > _NOTIONAL_ABS_TOL:
                raise ValueError("commission must equal max(stock_notional * 0.00025, 5)")
        return self


class LayerTwoEntryExecutionDiagnosticReport(_StrictModel):
    schema_version: Literal["1"] = LAYER_TWO_ENTRY_EXECUTION_SCHEMA_VERSION
    engine_version: Literal["layer-two-entry-execution-diagnostic-v1"] = LAYER_TWO_ENTRY_EXECUTION_ENGINE_VERSION
    report_id: str | None = Field(default=None, pattern=_HEX64.pattern)
    as_of: date
    decision_at: datetime
    market_data_snapshot_id: str = Field(min_length=1)
    allocator_report_id: str = Field(pattern=_HEX64.pattern)
    current_state_id: str = Field(pattern=_HEX64.pattern)
    constraint_assembler_report_id: str = Field(pattern=_HEX64.pattern)
    phase_report_id: str = Field(pattern=_HEX64.pattern)
    tranche_evaluation_protocol_id: Literal["8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_ID
    )
    tranche_evaluation_protocol_path: Literal["config/research/tranche-evaluation-protocol-draft-v1.json"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
    )
    expected_t1_execution_date: date
    proposed_symbol: str | None = None
    proposed_target_notional: float | None = None
    observation: LayerTwoEntryExecutionObservation | None = None
    outcome: ExecutionOutcome
    portfolio_cash_retention_reason: str | None = None
    base_scenario: EntryCostScenarioRow | None = None
    stress_scenario: EntryCostScenarioRow | None = None
    board_lot_size: Literal[100] = BOUND_BOARD_LOT_SIZE
    base_commission_per_side: float = BOUND_BASE_COMMISSION_PER_SIDE
    minimum_commission_cny: float = BOUND_MINIMUM_COMMISSION_CNY
    base_slippage_bps: Literal[5] = BOUND_BASE_SLIPPAGE_BPS
    stress_slippage_bps: Literal[15] = BOUND_STRESS_SLIPPAGE_BPS
    stamp_tax_irrelevant_for_buy_entry: Literal[True] = True
    stamp_tax_not_invented: Literal[True] = True
    diagnostic_only: Literal[True] = True
    post_decision_execution_label_only: Literal[True] = True
    must_not_feed_ranking_or_scoring: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_emit_orders: Literal[True] = True
    does_not_claim_fill_or_execution: Literal[True] = True
    does_not_use_future_close_high_low: Literal[True] = True

    @field_validator("as_of", "expected_t1_execution_date", mode="before")
    @classmethod
    def _dates(cls, value: object, info: Any) -> date:
        return _require_date(value, field_name=str(info.field_name))

    @field_validator("decision_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, field_name="decision_at")

    @field_validator("proposed_target_notional", mode="before")
    @classmethod
    def _optional_target(cls, value: object) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name="proposed_target_notional", minimum=0.0)

    @field_validator("base_commission_per_side", "minimum_commission_cny", mode="before")
    @classmethod
    def _cost_nums(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator(
        "diagnostic_only",
        "post_decision_execution_label_only",
        "must_not_feed_ranking_or_scoring",
        "stamp_tax_irrelevant_for_buy_entry",
        "stamp_tax_not_invented",
        "does_not_emit_orders",
        "does_not_claim_fill_or_execution",
        "does_not_use_future_close_high_low",
        mode="before",
    )
    @classmethod
    def _true_flags(cls, value: object, info: Any) -> object:
        return _require_literal_true(value, field_name=str(info.field_name))

    @field_validator(
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
        mode="before",
    )
    @classmethod
    def _false_flags(cls, value: object, info: Any) -> object:
        return _require_literal_false(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _gate(self) -> LayerTwoEntryExecutionDiagnosticReport:
        if abs(self.base_commission_per_side - BOUND_BASE_COMMISSION_PER_SIDE) > _NOTIONAL_ABS_TOL:
            raise ValueError("base_commission_per_side must match bound tranche protocol value")
        if abs(self.minimum_commission_cny - BOUND_MINIMUM_COMMISSION_CNY) > _NOTIONAL_ABS_TOL:
            raise ValueError("minimum_commission_cny must match bound tranche protocol value")
        if self.outcome == "not_attempted":
            if self.observation is not None:
                raise ValueError("not_attempted must not carry an execution observation")
            if self.proposed_symbol is not None or self.proposed_target_notional is not None:
                raise ValueError("not_attempted must not carry proposed symbol/target")
            if self.base_scenario is not None or self.stress_scenario is not None:
                raise ValueError("not_attempted must not populate cost scenarios")
            if self.portfolio_cash_retention_reason is None:
                raise ValueError("not_attempted requires allocator portfolio_cash_retention_reason")
            return self
        if self.portfolio_cash_retention_reason is not None:
            raise ValueError("attempted outcomes must not carry portfolio_cash_retention_reason")
        if self.proposed_symbol is None or self.proposed_target_notional is None:
            raise ValueError("attempted outcomes require proposed symbol/target")
        if self.observation is None:
            raise ValueError("attempted outcomes require an execution observation")
        if self.observation.symbol != self.proposed_symbol:
            raise ValueError("observation.symbol must equal proposed_symbol")
        if self.observation.execution_date != self.expected_t1_execution_date:
            raise ValueError("observation.execution_date must equal expected_t1_execution_date")
        if self.observation.market_data_snapshot_id != self.market_data_snapshot_id:
            raise ValueError("observation.market_data_snapshot_id must equal report market snapshot")

        status = self.observation.observation_status
        if status == "unknown":
            if self.outcome != "unknown_execution_observation":
                raise ValueError("unknown observation requires unknown_execution_observation outcome")
        elif status == "known_full_day_suspension":
            if self.outcome != "blocked_suspension":
                raise ValueError("suspension observation requires blocked_suspension outcome")
        else:
            assert self.observation.raw_open is not None
            assert self.observation.published_up_limit is not None
            # Legal price order is exact — cash amount tolerance must not decide limit-up.
            if self.observation.raw_open >= self.observation.published_up_limit:
                if self.outcome != "blocked_limit_up":
                    raise ValueError("raw_open at/above published_up_limit requires blocked_limit_up")
            elif self.outcome not in (
                "unaffordable_board_lot_or_minimum_commission",
                "hypothetically_fillable",
            ):
                raise ValueError("tradable below-limit observation requires cost outcome")

        if self.outcome in (
            "unknown_execution_observation",
            "blocked_suspension",
            "blocked_limit_up",
        ):
            if self.base_scenario is not None or self.stress_scenario is not None:
                raise ValueError(f"{self.outcome} must not populate cost scenarios")
            return self
        if self.outcome in ("unaffordable_board_lot_or_minimum_commission", "hypothetically_fillable"):
            if self.base_scenario is None or self.stress_scenario is None:
                raise ValueError("tradable cost outcomes require base and stress scenarios")
            if self.base_scenario.scenario_label != "base_5bps" or self.base_scenario.slippage_bps != 5:
                raise ValueError("base_scenario must be base_5bps with slippage_bps=5")
            if self.stress_scenario.scenario_label != "stress_15bps" or self.stress_scenario.slippage_bps != 15:
                raise ValueError("stress_scenario must be stress_15bps with slippage_bps=15")
            up_limit = self.observation.published_up_limit
            assert up_limit is not None
            for row in (self.base_scenario, self.stress_scenario):
                if row.hypothetical_fill_price > up_limit:
                    raise ValueError("hypothetical_fill_price must not exceed published_up_limit")
                accounted = row.total_cash_used + row.unused_target_cash
                if abs(accounted - float(self.proposed_target_notional)) > _NOTIONAL_ABS_TOL:
                    raise ValueError("proposed_target_notional must equal total_cash_used + unused_target_cash")
            if self.outcome == "hypothetically_fillable" and not self.base_scenario.can_afford_one_lot:
                raise ValueError("hypothetically_fillable requires base can_afford_one_lot")
            if self.outcome == "unaffordable_board_lot_or_minimum_commission" and self.base_scenario.can_afford_one_lot:
                raise ValueError("unaffordable outcome requires base can_afford_one_lot=false")
        return self


class LayerTwoEntryExecutionVerificationResult(_StrictModel):
    report_id: str
    structural_ok: bool
    allocator_binding_ok: bool = False
    phase_binding_ok: bool = False
    tranche_evaluation_protocol_binding_ok: bool = False
    execution_observation_binding_ok: bool = False
    diagnostic_only: Literal[True] = True
    post_decision_execution_label_only: Literal[True] = True
    must_not_feed_ranking_or_scoring: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator(
        "structural_ok",
        "allocator_binding_ok",
        "phase_binding_ok",
        "tranche_evaluation_protocol_binding_ok",
        "execution_observation_binding_ok",
        mode="before",
    )
    @classmethod
    def _plain_bool(cls, value: object, info: Any) -> bool:
        if type(value) is not bool:
            raise ValueError(f"{info.field_name} must be a boolean")
        return value

    @field_validator(
        "diagnostic_only",
        "post_decision_execution_label_only",
        "must_not_feed_ranking_or_scoring",
        mode="before",
    )
    @classmethod
    def _true_flags(cls, value: object, info: Any) -> object:
        return _require_literal_true(value, field_name=str(info.field_name))

    @field_validator(
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
        mode="before",
    )
    @classmethod
    def _false_flags(cls, value: object, info: Any) -> object:
        return _require_literal_false(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _binding_state_machine(self) -> LayerTwoEntryExecutionVerificationResult:
        bindings = (
            self.allocator_binding_ok,
            self.phase_binding_ok,
            self.tranche_evaluation_protocol_binding_ok,
            self.execution_observation_binding_ok,
        )
        any_bound = any(bindings)
        all_bound = all(bindings)
        if self.structural_ok is not True:
            if any_bound:
                raise ValueError("structural_ok=false forbids any disk binding")
            return self
        if all_bound:
            return self
        if any_bound:
            raise ValueError("partial disk bindings are forbidden")
        return self


def _next_market_trading_day(*, as_of: date, market_calendar: Sequence[date]) -> date:
    calendar = list(market_calendar)
    if not calendar:
        raise ValueError("market_calendar must be non-empty")
    if len(calendar) != len(set(calendar)):
        raise ValueError("market_calendar must not contain duplicates")
    if calendar != sorted(calendar):
        raise ValueError("market_calendar must be strictly increasing")
    try:
        index = calendar.index(as_of)
    except ValueError as exc:
        raise ValueError("as_of must appear in phase market_calendar") from exc
    if index + 1 >= len(calendar):
        raise ValueError("no next market trading day after as_of in phase market_calendar")
    return calendar[index + 1]


def _bound_cost_config(*, slippage_bps: int) -> CostConfig:
    # Stamp tax is sell-side only; buy_cost never applies it. Do not invent a schedule.
    return CostConfig(
        commission_rate=BOUND_BASE_COMMISSION_PER_SIDE,
        min_commission=BOUND_MINIMUM_COMMISSION_CNY,
        slippage_bps=float(slippage_bps),
        stamp_tax_rate=0.0,
        stamp_tax_schedule=[],
    )


def _build_scenario_row(
    *,
    label: ScenarioLabel,
    slippage_bps: int,
    raw_open: float,
    published_up_limit: float,
    target_notional: float,
) -> EntryCostScenarioRow:
    if LOT_SIZE != BOUND_BOARD_LOT_SIZE:
        raise ValueError("app.backtest.costs.LOT_SIZE drifted from bound board lot size")
    if published_up_limit <= 0 or raw_open <= 0:
        raise ValueError("raw_open and published_up_limit must be positive")
    if raw_open >= published_up_limit:
        raise ValueError("scenario construction requires raw_open strictly below published_up_limit")
    config = _bound_cost_config(slippage_bps=slippage_bps)
    slipped = apply_slippage(raw_open, config, "buy")
    # Legal price ordering is exact — cash amount tolerance must not decide cap/fill.
    legal_limit_cap_applied = slipped > published_up_limit
    fill_price = min(slipped, published_up_limit)
    if fill_price > published_up_limit:
        raise ValueError("hypothetical fill price must not exceed published_up_limit")
    # Affordability uses the legal (possibly capped) fill price; zero extra slippage.
    priced = _bound_cost_config(slippage_bps=0)
    shares = shares_affordable(target_notional, fill_price, priced, lot_size=BOUND_BOARD_LOT_SIZE)
    if shares % BOUND_BOARD_LOT_SIZE != 0:
        raise ValueError("shares_affordable returned non-lot multiple")
    lots = shares // BOUND_BOARD_LOT_SIZE
    if shares > 0:
        total, comm = buy_cost(fill_price, shares, priced)
        stock_notional = fill_price * shares
    else:
        total, comm, stock_notional = 0.0, 0.0, 0.0
    if total > target_notional + _NOTIONAL_ABS_TOL:
        raise ValueError("total cash used exceeds target_notional budget")
    unused = target_notional - total
    if unused < -_NOTIONAL_ABS_TOL:
        raise ValueError("unused target cash invariant failed")
    if unused < 0.0:
        unused = 0.0
    return EntryCostScenarioRow(
        scenario_label=label,
        slippage_bps=slippage_bps,
        hypothetical_fill_price=fill_price,
        affordable_shares=shares,
        affordable_lots=lots,
        stock_notional=stock_notional,
        commission=comm,
        total_cash_used=total,
        unused_target_cash=unused,
        can_afford_one_lot=shares >= BOUND_BOARD_LOT_SIZE,
        legal_limit_cap_applied=legal_limit_cap_applied,
    )


def diagnose_layer_two_entry_execution(
    *,
    allocator_report: LayerTwoStatefulAllocatorReport,
    constraint_report: LayerTwoConstraintAssemblerReport,
    current_state: LayerTwoStatefulPortfolioState,
    ranking: UnvalidatedDevelopmentRankingInput,
    phase_report: LayerTwoTranchePhaseScheduleReport,
    execution_observation: LayerTwoEntryExecutionObservation | None,
) -> LayerTwoEntryExecutionDiagnosticReport:
    """Build a sealed T+1 entry execution diagnostic for one allocator result."""
    assert_allocator_report_self_hash(allocator_report)
    assert_state_self_hash(current_state)
    assert_phase_report_self_hash(phase_report)
    if constraint_report.report_id is None:
        raise ValueError("constraint assembler report_id is missing")
    verify_layer_two_stateful_allocator_report(
        allocator_report,
        constraint_report=constraint_report,
        current_state=current_state,
        ranking=ranking,
    )
    verify_layer_two_tranche_phase_schedule_report(phase_report)

    if allocator_report.phase_report_id != phase_report.report_id:
        raise ValueError("allocator phase_report_id must equal phase report_id")
    if constraint_report.phase_report_id != phase_report.report_id:
        raise ValueError("constraint phase_report_id must equal phase report_id")
    if allocator_report.constraint_assembler_report_id != constraint_report.report_id:
        raise ValueError("allocator constraint_assembler_report_id drift")
    if allocator_report.current_state_id != current_state.state_id:
        raise ValueError("allocator current_state_id drift")
    if allocator_report.as_of != constraint_report.as_of:
        raise ValueError("allocator as_of must equal constraint as_of")
    if allocator_report.decision_at != constraint_report.decision_at:
        raise ValueError("allocator decision_at must equal constraint decision_at")
    if allocator_report.market_data_snapshot_id != constraint_report.market_data_snapshot_id:
        raise ValueError("allocator market snapshot must equal constraint snapshot")
    if phase_report.market_data_snapshot_id != allocator_report.market_data_snapshot_id:
        raise ValueError("phase market snapshot must equal allocator snapshot")
    if current_state.as_of != allocator_report.as_of or current_state.decision_at != allocator_report.decision_at:
        raise ValueError("current_state timing must equal allocator timing")
    if current_state.market_data_snapshot_id != allocator_report.market_data_snapshot_id:
        raise ValueError("current_state snapshot must equal allocator snapshot")

    t1 = _next_market_trading_day(as_of=allocator_report.as_of, market_calendar=phase_report.market_calendar)

    common = {
        "as_of": allocator_report.as_of,
        "decision_at": allocator_report.decision_at,
        "market_data_snapshot_id": allocator_report.market_data_snapshot_id,
        "allocator_report_id": _require_sealed_hex64(allocator_report.report_id, field_name="allocator_report_id"),
        "current_state_id": _require_sealed_hex64(current_state.state_id, field_name="current_state_id"),
        "constraint_assembler_report_id": _require_sealed_hex64(
            constraint_report.report_id, field_name="constraint_assembler_report_id"
        ),
        "phase_report_id": _require_sealed_hex64(phase_report.report_id, field_name="phase_report_id"),
        "expected_t1_execution_date": t1,
    }

    if allocator_report.proposed_entry is None:
        if execution_observation is not None:
            raise ValueError("execution_observation forbidden when allocator has no proposed_entry")
        reason = allocator_report.portfolio_cash_retention_reason
        if reason is None:
            raise ValueError("allocator without proposed_entry must carry portfolio_cash_retention_reason")
        assembled = LayerTwoEntryExecutionDiagnosticReport(
            **common,
            proposed_symbol=None,
            proposed_target_notional=None,
            observation=None,
            outcome="not_attempted",
            portfolio_cash_retention_reason=reason,
            base_scenario=None,
            stress_scenario=None,
        )
        return seal_layer_two_entry_execution_diagnostic_report(assembled)

    entry = allocator_report.proposed_entry
    if execution_observation is None:
        raise ValueError("execution_observation required when allocator proposes an entry")
    if execution_observation.symbol != entry.symbol:
        raise ValueError("execution_observation.symbol must equal proposed_entry.symbol")
    if execution_observation.execution_date != t1:
        raise ValueError("execution_observation.execution_date must be exact T+1 from phase calendar")
    if execution_observation.market_data_snapshot_id != allocator_report.market_data_snapshot_id:
        raise ValueError("execution_observation snapshot must equal allocator market snapshot")

    outcome: ExecutionOutcome
    base_row: EntryCostScenarioRow | None = None
    stress_row: EntryCostScenarioRow | None = None

    if execution_observation.observation_status == "unknown":
        outcome = "unknown_execution_observation"
    elif execution_observation.observation_status == "known_full_day_suspension":
        outcome = "blocked_suspension"
    else:
        assert execution_observation.raw_open is not None
        assert execution_observation.published_up_limit is not None
        if execution_observation.raw_open >= execution_observation.published_up_limit:
            outcome = "blocked_limit_up"
        else:
            target = float(entry.target_notional)
            up_limit = float(execution_observation.published_up_limit)
            raw_open = float(execution_observation.raw_open)
            base_row = _build_scenario_row(
                label="base_5bps",
                slippage_bps=BOUND_BASE_SLIPPAGE_BPS,
                raw_open=raw_open,
                published_up_limit=up_limit,
                target_notional=target,
            )
            stress_row = _build_scenario_row(
                label="stress_15bps",
                slippage_bps=BOUND_STRESS_SLIPPAGE_BPS,
                raw_open=raw_open,
                published_up_limit=up_limit,
                target_notional=target,
            )
            if base_row.can_afford_one_lot:
                outcome = "hypothetically_fillable"
            else:
                outcome = "unaffordable_board_lot_or_minimum_commission"

    assembled = LayerTwoEntryExecutionDiagnosticReport(
        **common,
        proposed_symbol=entry.symbol,
        proposed_target_notional=float(entry.target_notional),
        observation=execution_observation,
        outcome=outcome,
        portfolio_cash_retention_reason=None,
        base_scenario=base_row,
        stress_scenario=stress_row,
    )
    return seal_layer_two_entry_execution_diagnostic_report(assembled)


def canonical_report_payload(report: LayerTwoEntryExecutionDiagnosticReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: LayerTwoEntryExecutionDiagnosticReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: LayerTwoEntryExecutionDiagnosticReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_layer_two_entry_execution_diagnostic_report(
    report: LayerTwoEntryExecutionDiagnosticReport,
) -> LayerTwoEntryExecutionDiagnosticReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: LayerTwoEntryExecutionDiagnosticReport) -> None:
    if report.report_id is None:
        raise ValueError("entry execution diagnostic report_id is missing")
    if report.report_id != compute_report_id(report):
        raise ValueError("entry execution diagnostic report_id does not match canonical content hash")


def assert_matches_recomputed_diagnostic(
    report: LayerTwoEntryExecutionDiagnosticReport,
    *,
    allocator_report: LayerTwoStatefulAllocatorReport,
    constraint_report: LayerTwoConstraintAssemblerReport,
    current_state: LayerTwoStatefulPortfolioState,
    ranking: UnvalidatedDevelopmentRankingInput,
    phase_report: LayerTwoTranchePhaseScheduleReport,
    execution_observation: LayerTwoEntryExecutionObservation | None,
) -> None:
    expected = diagnose_layer_two_entry_execution(
        allocator_report=allocator_report,
        constraint_report=constraint_report,
        current_state=current_state,
        ranking=ranking,
        phase_report=phase_report,
        execution_observation=execution_observation,
    )
    if report.report_id != expected.report_id:
        raise ValueError("entry execution diagnostic report_id does not match full recompute")
    if canonical_report_payload(report) != canonical_report_payload(expected):
        raise ValueError("entry execution diagnostic canonical payload does not match full recompute")


def verify_layer_two_entry_execution_diagnostic_report(
    report: LayerTwoEntryExecutionDiagnosticReport,
    *,
    allocator_report: LayerTwoStatefulAllocatorReport,
    constraint_report: LayerTwoConstraintAssemblerReport,
    current_state: LayerTwoStatefulPortfolioState,
    ranking: UnvalidatedDevelopmentRankingInput,
    phase_report: LayerTwoTranchePhaseScheduleReport,
    execution_observation: LayerTwoEntryExecutionObservation | None,
) -> LayerTwoEntryExecutionVerificationResult:
    """Structural verifier: self-hash + E10d-3 structural verify + full recompute.

    Does not claim phase/tranche-protocol disk bindings.
    """
    assert_report_self_hash(report)
    verify_layer_two_stateful_allocator_report(
        allocator_report,
        constraint_report=constraint_report,
        current_state=current_state,
        ranking=ranking,
    )
    verify_layer_two_tranche_phase_schedule_report(phase_report)
    assert_matches_recomputed_diagnostic(
        report,
        allocator_report=allocator_report,
        constraint_report=constraint_report,
        current_state=current_state,
        ranking=ranking,
        phase_report=phase_report,
        execution_observation=execution_observation,
    )
    if report.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("tranche evaluation protocol id drift")
    return LayerTwoEntryExecutionVerificationResult(
        report_id=report.report_id or compute_report_id(report),
        structural_ok=True,
        allocator_binding_ok=False,
        phase_binding_ok=False,
        tranche_evaluation_protocol_binding_ok=False,
        execution_observation_binding_ok=False,
    )


def _coerce_store_date(value: object) -> date:
    if type(value) is date:
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value.strip():
        return date.fromisoformat(value.strip())
    raise ValueError("store daily bar date must be a datetime.date")


def _require_store_finite_positive(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"store {field_name} must be a real number (bool rejected)")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"store {field_name} must be finite (NaN/Inf rejected)")
    if number <= 0.0:
        raise ValueError(f"store {field_name} must be > 0")
    return number


def _exact_symbol_date_daily_rows(
    store: MarketStore,
    *,
    day: date,
    symbol: str,
) -> list[dict[str, Any]]:
    frame = store.get_daily_bars(as_of=day, symbol=symbol, start=day)
    matches: list[dict[str, Any]] = []
    for row in frame.to_dicts():
        try:
            row_day = _coerce_store_date(row.get("date"))
        except ValueError as exc:
            raise ValueError("store daily bar has invalid date") from exc
        if row_day != day:
            continue
        if str(row.get("symbol") or "") != symbol:
            continue
        matches.append(row)
    if len(matches) > 1:
        raise ValueError("duplicate exact symbol/date daily bars in MarketStore")
    return matches


def _store_row_proves_suspension(row: dict[str, Any]) -> bool:
    return row.get("is_suspended") is True


def _store_row_proves_complete_tradable_up(row: dict[str, Any]) -> bool:
    if row.get("is_suspended") is not False:
        return False
    try:
        _require_store_finite_positive(row.get("open"), field_name="open")
        _require_store_finite_positive(row.get("up_limit"), field_name="up_limit")
    except ValueError:
        return False
    return True


def _bind_entry_execution_observation_to_store(
    *,
    report: LayerTwoEntryExecutionDiagnosticReport,
    store: MarketStore,
) -> None:
    """Fail-closed MarketStore binding for the explicit T+1 execution observation."""
    if store.snapshot().snapshot_id != report.market_data_snapshot_id:
        raise ValueError("MarketStore snapshot_id must equal report.market_data_snapshot_id")
    if report.outcome == "not_attempted":
        if report.observation is not None:
            raise ValueError("not_attempted must not carry an execution observation")
        return
    observation = report.observation
    if observation is None:
        raise ValueError("attempted entry diagnostic requires an execution observation")
    rows = _exact_symbol_date_daily_rows(
        store,
        day=observation.execution_date,
        symbol=observation.symbol,
    )
    if observation.observation_status == "unknown":
        if not rows:
            return
        row = rows[0]
        if _store_row_proves_suspension(row) or _store_row_proves_complete_tradable_up(row):
            raise ValueError("unknown execution observation forbidden when MarketStore has a complete determinate row")
        return
    if observation.observation_status == "known_full_day_suspension":
        if len(rows) != 1:
            raise ValueError("suspension observation requires exactly one MarketStore daily row")
        if rows[0].get("is_suspended") is not True:
            raise ValueError("suspension observation requires store is_suspended=true")
        return
    if len(rows) != 1:
        raise ValueError("tradable observation requires exactly one MarketStore daily row")
    row = rows[0]
    if row.get("is_suspended") is not False:
        raise ValueError("tradable observation requires store is_suspended=false")
    open_px = _require_store_finite_positive(row.get("open"), field_name="open")
    up_limit = _require_store_finite_positive(row.get("up_limit"), field_name="up_limit")
    # Never bind against adj_open / adj prices.
    if observation.raw_open != open_px:
        raise ValueError("observation.raw_open must exactly equal store open (not adj_open)")
    if observation.published_up_limit != up_limit:
        raise ValueError("observation.published_up_limit must exactly equal store up_limit")


def verify_layer_two_entry_execution_diagnostic_report_file(
    *,
    report: LayerTwoEntryExecutionDiagnosticReport,
    allocator_report: LayerTwoStatefulAllocatorReport,
    constraint_report: LayerTwoConstraintAssemblerReport,
    current_state: LayerTwoStatefulPortfolioState,
    ranking: UnvalidatedDevelopmentRankingInput,
    phase_report: LayerTwoTranchePhaseScheduleReport,
    execution_observation: LayerTwoEntryExecutionObservation | None,
    eligibility_report: LayerTwoCandidateEligibilityReport,
    financial_reports: Sequence[LayerTwoFinancialNegativeListReport],
    cluster_report: LayerTwoStatisticalRiskClusterReport,
    store: MarketStore,
    repo_root: Path,
    phase_report_path: Path,
) -> LayerTwoEntryExecutionVerificationResult:
    """File verifier: structural path + real E10d-3 file verifier + tranche protocol + observation store bind."""
    root = Path(repo_root).resolve()
    structural = verify_layer_two_entry_execution_diagnostic_report(
        report,
        allocator_report=allocator_report,
        constraint_report=constraint_report,
        current_state=current_state,
        ranking=ranking,
        phase_report=phase_report,
        execution_observation=execution_observation,
    )
    if structural.structural_ok is not True:
        raise ValueError("structural verifier must succeed before file binding")
    if (
        structural.allocator_binding_ok
        or structural.phase_binding_ok
        or structural.tranche_evaluation_protocol_binding_ok
        or structural.execution_observation_binding_ok
    ):
        raise ValueError("structural verifier must not claim disk bindings")
    allocator_file = verify_layer_two_stateful_allocator_report_file(
        report=allocator_report,
        constraint_report=constraint_report,
        current_state=current_state,
        ranking=ranking,
        eligibility_report=eligibility_report,
        financial_reports=financial_reports,
        cluster_report=cluster_report,
        phase_report=phase_report,
        store=store,
        repo_root=root,
        phase_report_path=phase_report_path,
    )
    if not allocator_file.phase_binding_ok:
        raise ValueError("E10d-3 file verifier phase_binding_ok required")
    if not allocator_file.constraint_assembler_binding_ok:
        raise ValueError("E10d-3 file verifier constraint_assembler_binding_ok required")

    protocol_path = root / BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
    if str(DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH) != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
        raise ValueError("tranche evaluation default path drifted")
    _doc, protocol_result = verify_tranche_evaluation_protocol_draft_file(
        protocol_path=protocol_path,
        repo_root=root,
    )
    if protocol_result.protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("tranche evaluation protocol_id on disk does not match bound constant")
    if protocol_result.protocol_id != report.tranche_evaluation_protocol_id:
        raise ValueError("tranche evaluation protocol_id on disk does not match report binding")
    if not isinstance(_doc, TrancheEvaluationProtocolV2):
        raise ValueError("bound tranche evaluation protocol must be schema v2 with cost_assumptions")
    costs = _doc.cost_assumptions
    if abs(float(costs.base_commission_per_side) - BOUND_BASE_COMMISSION_PER_SIDE) > _NOTIONAL_ABS_TOL:
        raise ValueError("disk tranche protocol base_commission_per_side drift")
    if abs(float(costs.minimum_commission_cny) - BOUND_MINIMUM_COMMISSION_CNY) > _NOTIONAL_ABS_TOL:
        raise ValueError("disk tranche protocol minimum_commission_cny drift")
    if int(costs.base_slippage_bps_per_side) != BOUND_BASE_SLIPPAGE_BPS:
        raise ValueError("disk tranche protocol base_slippage_bps drift")
    if int(costs.stress_slippage_bps_per_side) != BOUND_STRESS_SLIPPAGE_BPS:
        raise ValueError("disk tranche protocol stress_slippage_bps drift")

    _bind_entry_execution_observation_to_store(report=report, store=store)

    # Newly constructed — do not model_copy / trust caller binding booleans.
    return LayerTwoEntryExecutionVerificationResult(
        report_id=report.report_id or compute_report_id(report),
        structural_ok=True,
        allocator_binding_ok=True,
        phase_binding_ok=True,
        tranche_evaluation_protocol_binding_ok=True,
        execution_observation_binding_ok=True,
    )


def load_layer_two_entry_execution_diagnostic_report(path: Path) -> LayerTwoEntryExecutionDiagnosticReport:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("entry execution diagnostic report is missing or invalid") from exc
    try:
        return LayerTwoEntryExecutionDiagnosticReport.model_validate(payload)
    except Exception as exc:
        raise ValueError("entry execution diagnostic report is missing or invalid") from exc


def write_layer_two_entry_execution_diagnostic_report(
    path: Path,
    report: LayerTwoEntryExecutionDiagnosticReport,
) -> LayerTwoEntryExecutionDiagnosticReport:
    sealed = seal_layer_two_entry_execution_diagnostic_report(report)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_BASE_COMMISSION_PER_SIDE",
    "BOUND_BASE_SLIPPAGE_BPS",
    "BOUND_BOARD_LOT_SIZE",
    "BOUND_MINIMUM_COMMISSION_CNY",
    "BOUND_STRESS_SLIPPAGE_BPS",
    "BOUND_TRANCHE_EVALUATION_PROTOCOL_ID",
    "BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH",
    "LAYER_TWO_ENTRY_EXECUTION_ENGINE_VERSION",
    "LAYER_TWO_ENTRY_EXECUTION_SCHEMA_VERSION",
    "EntryCostScenarioRow",
    "LayerTwoEntryExecutionDiagnosticReport",
    "LayerTwoEntryExecutionObservation",
    "LayerTwoEntryExecutionVerificationResult",
    "assert_matches_recomputed_diagnostic",
    "assert_report_self_hash",
    "canonical_report_bytes",
    "canonical_report_payload",
    "compute_report_id",
    "diagnose_layer_two_entry_execution",
    "load_layer_two_entry_execution_diagnostic_report",
    "seal_layer_two_entry_execution_diagnostic_report",
    "verify_layer_two_entry_execution_diagnostic_report",
    "verify_layer_two_entry_execution_diagnostic_report_file",
    "write_layer_two_entry_execution_diagnostic_report",
]
