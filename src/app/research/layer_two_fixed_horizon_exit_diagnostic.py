"""Layer-two fixed 40-market-bar exit availability / sell-cost diagnostic (E10f-2).

Research-only post-decision label over one sealed E10f-1 hypothetical open record.
Aligns with the frozen two-layer / tranche-evaluation protocol holding period (40),
not the consumed p10_h20 trial. Does not emit orders, mutate lifecycle, invent
PnL/returns, or claim a live fill.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.a_share_stamp_tax_schedule import (
    BOUND_A_SHARE_STAMP_TAX_SCHEDULE_PATH,
    DECLARED_WINDOW_END,
    DECLARED_WINDOW_START,
    EXPECTED_CURRENT_CONTRACT_ID,
    AShareStampTaxScheduleContract,
    assert_contract_self_hash,
    stamp_tax_amount,
    stamp_tax_rate_for,
    verify_a_share_stamp_tax_schedule,
    verify_a_share_stamp_tax_schedule_file,
)
from app.research.layer_two_allocation_protocol import _require_non_bool_int, _require_real_number
from app.research.layer_two_entry_execution_diagnostic import (
    BOUND_BASE_COMMISSION_PER_SIDE,
    BOUND_BASE_SLIPPAGE_BPS,
    BOUND_MINIMUM_COMMISSION_CNY,
    BOUND_STRESS_SLIPPAGE_BPS,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
)
from app.research.layer_two_hypothetical_position_lifecycle import (
    LayerTwoHypotheticalLifecycleFileInput,
    LayerTwoHypotheticalLifecycleStructuralInput,
    LayerTwoHypotheticalPositionLifecycleRecord,
    assert_record_self_hash,
    verify_layer_two_hypothetical_position_lifecycle_record,
    verify_layer_two_hypothetical_position_lifecycle_record_file,
)
from app.research.tranche_evaluation_protocol import (
    DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH,
    TrancheEvaluationProtocolV2,
    verify_tranche_evaluation_protocol_draft_file,
)
from app.research.two_layer_contract import CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS
from app.storage.protocol import MarketStore

LAYER_TWO_FIXED_HORIZON_EXIT_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_FIXED_HORIZON_EXIT_ENGINE_VERSION: Literal["layer-two-fixed-horizon-exit-diagnostic-v1"] = (
    "layer-two-fixed-horizon-exit-diagnostic-v1"
)

# Frozen two-layer / tranche protocol holding period (not consumed p10_h20).
BOUND_HOLDING_PERIOD_MARKET_BARS: Literal[40] = 40
BOUND_EXIT_ATTEMPT_OFFSET_FROM_ENTRY_INDEX: Literal[40] = 40

if BOUND_HOLDING_PERIOD_MARKET_BARS != CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS:
    raise RuntimeError("E10f-2 holding bars drifted from confirmed two-layer holding cycle")
if BOUND_EXIT_ATTEMPT_OFFSET_FROM_ENTRY_INDEX != BOUND_HOLDING_PERIOD_MARKET_BARS:
    raise RuntimeError("E10f-2 exit attempt offset must equal holding period market bars")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CASH_ABS_TOL = 1e-9

ExitObservationStatus = Literal["unknown", "known_full_day_suspension", "tradable"]
ExitAttemptOutcome = Literal[
    "blocked_suspension",
    "blocked_limit_down",
    "unknown_exit_observation",
    "hypothetically_exitable",
]
ExitFinalOutcome = Literal[
    "unknown_exit_observation",
    "hypothetically_exitable",
    "still_open_after_observed_blocks",
]
ExitScenarioLabel = Literal["base_5bps", "stress_15bps"]


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


def _require_strict_bool(value: object, *, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a strict bool")
    return value


def _amounts_equal(left: float, right: float, *, abs_tol: float = _CASH_ABS_TOL) -> bool:
    return abs(float(left) - float(right)) <= abs_tol


def _require_declared_window(day: date, *, field_name: str) -> date:
    if day < DECLARED_WINDOW_START or day > DECLARED_WINDOW_END:
        raise ValueError(
            f"{field_name} must lie within stamp-tax declared_window "
            f"{DECLARED_WINDOW_START.isoformat()}..{DECLARED_WINDOW_END.isoformat()} "
            "(verified_through must not authorize 2025+ exit diagnostics)"
        )
    return day


def _assert_strict_calendar(calendar: Sequence[date]) -> list[date]:
    days = [_require_date(day, field_name="market_calendar") for day in calendar]
    if not days:
        raise ValueError("market_calendar must be non-empty")
    if days != sorted(days) or len(days) != len(set(days)):
        raise ValueError("market_calendar must be strictly increasing with unique dates")
    return days


def compute_market_calendar_sha256(calendar: Sequence[date]) -> str:
    payload = [day.isoformat() for day in _assert_strict_calendar(calendar)]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class LayerTwoFixedHorizonExitStructuralInput:
    lifecycle_record: LayerTwoHypotheticalPositionLifecycleRecord
    lifecycle_structural: LayerTwoHypotheticalLifecycleStructuralInput
    stamp_tax_contract: AShareStampTaxScheduleContract
    market_calendar: tuple[date, ...]
    exit_observations: tuple[LayerTwoFixedHorizonExitObservation, ...]


@dataclass(frozen=True, slots=True)
class LayerTwoFixedHorizonExitFileInput:
    structural: LayerTwoFixedHorizonExitStructuralInput
    lifecycle_file: LayerTwoHypotheticalLifecycleFileInput
    stamp_tax_repo_root: Path


class LayerTwoFixedHorizonExitObservation(_StrictModel):
    """Explicit exit-day open observation; no close/high/low or future-derived fields."""

    symbol: str = Field(min_length=1)
    observation_date: date
    market_data_snapshot_id: str = Field(min_length=1)
    observation_status: ExitObservationStatus
    raw_open: float | None = None
    published_down_limit: float | None = None

    @field_validator("observation_date", mode="before")
    @classmethod
    def _date(cls, value: object) -> date:
        return _require_date(value, field_name="observation_date")

    @field_validator("symbol", "market_data_snapshot_id", mode="before")
    @classmethod
    def _nonblank(cls, value: object, info: Any) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value.strip()

    @field_validator("raw_open", "published_down_limit", mode="before")
    @classmethod
    def _optional_price(cls, value: object, info: Any) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0, minimum_exclusive=True)

    @model_validator(mode="after")
    def _status_fields(self) -> LayerTwoFixedHorizonExitObservation:
        if self.observation_status in ("unknown", "known_full_day_suspension"):
            if self.raw_open is not None or self.published_down_limit is not None:
                raise ValueError("unknown/suspension exit observation must keep raw_open/published_down_limit null")
            return self
        if self.raw_open is None or self.published_down_limit is None:
            raise ValueError("tradable exit observation requires positive raw_open and published_down_limit")
        return self


class ExitAttemptRow(_StrictModel):
    observation_date: date
    holding_market_bars_elapsed_before_open: int
    observation_status: ExitObservationStatus
    attempt_outcome: ExitAttemptOutcome

    @field_validator("observation_date", mode="before")
    @classmethod
    def _date(cls, value: object) -> date:
        return _require_date(value, field_name="observation_date")

    @field_validator("holding_market_bars_elapsed_before_open", mode="before")
    @classmethod
    def _bars(cls, value: object) -> int:
        return _require_non_bool_int(value, field_name="holding_market_bars_elapsed_before_open", minimum=40)


class ExitSellCostScenarioRow(_StrictModel):
    scenario_label: ExitScenarioLabel
    slippage_bps: int
    hypothetical_fill_price: float
    shares: int
    gross_notional: float
    commission: float
    stamp_tax: float
    stamp_tax_rate: float
    net_sale_cash: float
    legal_limit_floor_applied: bool
    is_hypothetical_only: Literal[True] = True
    is_not_a_fill_or_order: Literal[True] = True

    @field_validator("slippage_bps", "shares", mode="before")
    @classmethod
    def _ints(cls, value: object, info: Any) -> int:
        minimum = 1 if info.field_name == "shares" else 0
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=minimum)

    @field_validator(
        "hypothetical_fill_price",
        "gross_notional",
        "commission",
        "stamp_tax",
        "stamp_tax_rate",
        "net_sale_cash",
        mode="before",
    )
    @classmethod
    def _amounts(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("legal_limit_floor_applied", mode="before")
    @classmethod
    def _bool(cls, value: object) -> bool:
        return _require_strict_bool(value, field_name="legal_limit_floor_applied")

    @field_validator("is_hypothetical_only", "is_not_a_fill_or_order", mode="before")
    @classmethod
    def _true(cls, value: object, info: Any) -> object:
        return _require_literal_true(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _identities(self) -> ExitSellCostScenarioRow:
        expected_bps = BOUND_BASE_SLIPPAGE_BPS if self.scenario_label == "base_5bps" else BOUND_STRESS_SLIPPAGE_BPS
        if self.slippage_bps != expected_bps:
            raise ValueError("exit scenario slippage_bps must match sealed label")
        if not _amounts_equal(self.gross_notional, self.hypothetical_fill_price * float(self.shares)):
            raise ValueError("gross_notional must equal fill_price * shares")
        expected_comm = max(self.gross_notional * BOUND_BASE_COMMISSION_PER_SIDE, BOUND_MINIMUM_COMMISSION_CNY)
        if not _amounts_equal(self.commission, expected_comm):
            raise ValueError("commission must equal max(rate*gross, minimum)")
        if not _amounts_equal(self.stamp_tax, self.gross_notional * self.stamp_tax_rate):
            raise ValueError("stamp_tax must equal gross_notional * stamp_tax_rate")
        if not _amounts_equal(self.net_sale_cash, self.gross_notional - self.commission - self.stamp_tax):
            raise ValueError("net_sale_cash must equal gross - commission - stamp_tax")
        return self


class LayerTwoFixedHorizonExitDiagnosticReport(_StrictModel):
    schema_version: Literal["1"] = LAYER_TWO_FIXED_HORIZON_EXIT_SCHEMA_VERSION
    engine_version: Literal["layer-two-fixed-horizon-exit-diagnostic-v1"] = LAYER_TWO_FIXED_HORIZON_EXIT_ENGINE_VERSION
    report_id: str | None = Field(default=None, pattern=_HEX64.pattern)

    lifecycle_record_id: str = Field(pattern=_HEX64.pattern)
    entry_execution_report_id: str = Field(pattern=_HEX64.pattern)
    allocator_report_id: str = Field(pattern=_HEX64.pattern)
    constraint_assembler_report_id: str = Field(pattern=_HEX64.pattern)
    phase_report_id: str = Field(pattern=_HEX64.pattern)
    current_state_id: str = Field(pattern=_HEX64.pattern)
    stamp_tax_contract_id: str = Field(pattern=_HEX64.pattern)
    market_calendar_sha256: str = Field(pattern=_HEX64.pattern)
    market_data_snapshot_id: str = Field(min_length=1)
    tranche_evaluation_protocol_id: Literal["8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_ID
    )
    tranche_evaluation_protocol_path: Literal["config/research/tranche-evaluation-protocol-draft-v1.json"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
    )

    symbol: str = Field(min_length=1)
    shares: int
    entry_trade_date: date
    holding_period_market_bars: Literal[40] = BOUND_HOLDING_PERIOD_MARKET_BARS
    scheduled_exit_attempt_date: date
    final_outcome: ExitFinalOutcome
    blocked_suspension_days: int
    blocked_limit_down_days: int
    attempt_rows: list[ExitAttemptRow]
    exit_observation_date: date | None = None
    base_scenario: ExitSellCostScenarioRow | None = None
    stress_scenario: ExitSellCostScenarioRow | None = None

    research_only: Literal[True] = True
    diagnostic_only: Literal[True] = True
    post_decision_label_only: Literal[True] = True
    hypothetical_not_fill: Literal[True] = True
    does_not_claim_order_or_live_fill: Literal[True] = True
    does_not_mutate_lifecycle_record: Literal[True] = True
    does_not_invent_return_pnl_or_alpha: Literal[True] = True
    stamp_tax_from_e10f0_contract_only: Literal[True] = True
    declared_window_only_no_verified_through_extrapolation: Literal[True] = True
    holds_frozen_40_bar_two_layer_protocol_not_consumed_p10_h20: Literal[True] = True
    ready_for_exit_diagnostic: Literal[False] = False
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator(
        "entry_trade_date",
        "scheduled_exit_attempt_date",
        "exit_observation_date",
        mode="before",
    )
    @classmethod
    def _dates(cls, value: object, info: Any) -> object:
        if value is None:
            return None
        return _require_date(value, field_name=str(info.field_name))

    @field_validator("shares", "blocked_suspension_days", "blocked_limit_down_days", mode="before")
    @classmethod
    def _ints(cls, value: object, info: Any) -> int:
        minimum = 1 if info.field_name == "shares" else 0
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=minimum)

    @field_validator("symbol", "market_data_snapshot_id", mode="before")
    @classmethod
    def _nonblank(cls, value: object, info: Any) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value.strip()

    @field_validator(
        "research_only",
        "diagnostic_only",
        "post_decision_label_only",
        "hypothetical_not_fill",
        "does_not_claim_order_or_live_fill",
        "does_not_mutate_lifecycle_record",
        "does_not_invent_return_pnl_or_alpha",
        "stamp_tax_from_e10f0_contract_only",
        "declared_window_only_no_verified_through_extrapolation",
        "holds_frozen_40_bar_two_layer_protocol_not_consumed_p10_h20",
        mode="before",
    )
    @classmethod
    def _true(cls, value: object, info: Any) -> object:
        return _require_literal_true(value, field_name=str(info.field_name))

    @field_validator(
        "ready_for_exit_diagnostic",
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_portfolio_construction",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
        mode="before",
    )
    @classmethod
    def _false(cls, value: object, info: Any) -> object:
        return _require_literal_false(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _identities(self) -> LayerTwoFixedHorizonExitDiagnosticReport:
        if self.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
            raise ValueError("tranche_evaluation_protocol_id must equal sealed tranche protocol id")
        if self.tranche_evaluation_protocol_path != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
            raise ValueError("tranche_evaluation_protocol_path must equal sealed tranche protocol path")
        if self.holding_period_market_bars != BOUND_HOLDING_PERIOD_MARKET_BARS:
            raise ValueError("holding_period_market_bars must be 40")
        if not self.attempt_rows:
            raise ValueError("attempt_rows must be non-empty")
        if self.attempt_rows[0].observation_date != self.scheduled_exit_attempt_date:
            raise ValueError("first attempt row must be the scheduled_exit_attempt_date")
        if self.attempt_rows[0].holding_market_bars_elapsed_before_open != BOUND_HOLDING_PERIOD_MARKET_BARS:
            raise ValueError("scheduled attempt must have holding_market_bars_elapsed_before_open=40")
        for index in range(1, len(self.attempt_rows)):
            prev = self.attempt_rows[index - 1]
            cur = self.attempt_rows[index]
            if cur.holding_market_bars_elapsed_before_open != prev.holding_market_bars_elapsed_before_open + 1:
                raise ValueError("holding_market_bars_elapsed_before_open must increase by 1 each attempt")
        susp = sum(1 for row in self.attempt_rows if row.attempt_outcome == "blocked_suspension")
        lim = sum(1 for row in self.attempt_rows if row.attempt_outcome == "blocked_limit_down")
        if susp != self.blocked_suspension_days or lim != self.blocked_limit_down_days:
            raise ValueError("blocked_*_days must equal attempt_rows outcome counts")
        if self.final_outcome == "hypothetically_exitable":
            if self.exit_observation_date is None or self.base_scenario is None or self.stress_scenario is None:
                raise ValueError("hypothetically_exitable requires exit date and base/stress scenarios")
            if self.attempt_rows[-1].attempt_outcome != "hypothetically_exitable":
                raise ValueError("final hypothetically_exitable requires last attempt_outcome match")
            if (
                self.base_scenario.scenario_label != "base_5bps"
                or self.stress_scenario.scenario_label != "stress_15bps"
            ):
                raise ValueError("base/stress scenario labels must be sealed")
            if self.base_scenario.shares != self.shares or self.stress_scenario.shares != self.shares:
                raise ValueError("sell scenarios must use lifecycle shares unchanged")
        else:
            if self.base_scenario is not None or self.stress_scenario is not None:
                raise ValueError("non-exitable outcomes must keep sell scenarios null")
            if self.final_outcome == "unknown_exit_observation":
                if self.exit_observation_date is None:
                    raise ValueError("unknown_exit_observation requires exit_observation_date of the unknown day")
                if self.attempt_rows[-1].attempt_outcome != "unknown_exit_observation":
                    raise ValueError("unknown final outcome requires last attempt unknown")
            else:
                if self.exit_observation_date is not None:
                    raise ValueError("still_open_after_observed_blocks must keep exit_observation_date null")
                if any(
                    row.attempt_outcome in ("hypothetically_exitable", "unknown_exit_observation")
                    for row in self.attempt_rows
                ):
                    raise ValueError("still_open rows must only contain suspension/limit-down attempts")
        return self


class LayerTwoFixedHorizonExitVerificationResult(_StrictModel):
    report_id: str = Field(pattern=_HEX64.pattern)
    structural_ok: bool
    lifecycle_binding_ok: bool
    stamp_tax_binding_ok: bool
    tranche_evaluation_protocol_binding_ok: bool
    exit_observation_binding_ok: bool
    ready_for_exit_diagnostic: bool
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator(
        "structural_ok",
        "lifecycle_binding_ok",
        "stamp_tax_binding_ok",
        "tranche_evaluation_protocol_binding_ok",
        "exit_observation_binding_ok",
        "ready_for_exit_diagnostic",
        mode="before",
    )
    @classmethod
    def _bools(cls, value: object, info: Any) -> bool:
        return _require_strict_bool(value, field_name=str(info.field_name))

    @field_validator(
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_portfolio_construction",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
        mode="before",
    )
    @classmethod
    def _false(cls, value: object, info: Any) -> object:
        return _require_literal_false(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _state_machine(self) -> LayerTwoFixedHorizonExitVerificationResult:
        bindings = (
            self.lifecycle_binding_ok,
            self.stamp_tax_binding_ok,
            self.tranche_evaluation_protocol_binding_ok,
            self.exit_observation_binding_ok,
        )
        any_bound = any(bindings)
        all_bound = all(bindings)
        if self.structural_ok is not True:
            if any_bound or self.ready_for_exit_diagnostic:
                raise ValueError("structural_ok=false forbids any binding or ready_for_exit_diagnostic")
            return self
        if self.ready_for_exit_diagnostic is True:
            if not all_bound:
                raise ValueError("ready_for_exit_diagnostic=true requires structural_ok and all bindings true")
            return self
        if any_bound:
            if not all_bound:
                raise ValueError("partial bindings are forbidden")
            raise ValueError("all bindings true requires ready_for_exit_diagnostic=true (file-path shape)")
        return self


def build_exit_sell_cost_scenario(
    *,
    label: ExitScenarioLabel,
    slippage_bps: int,
    raw_open: float,
    published_down_limit: float,
    shares: int,
    trade_date: date,
    stamp_tax_contract: AShareStampTaxScheduleContract,
) -> ExitSellCostScenarioRow:
    """Build one hypothetical sell-cost scenario using E10e-0 constants + E10f-0 stamp tax."""
    _require_declared_window(trade_date, field_name="trade_date")
    raw = _require_real_number(raw_open, field_name="raw_open", minimum=0.0, minimum_exclusive=True)
    down = _require_real_number(
        published_down_limit, field_name="published_down_limit", minimum=0.0, minimum_exclusive=True
    )
    qty = _require_non_bool_int(shares, field_name="shares", minimum=1)
    if raw <= down:
        raise ValueError("sell scenario requires raw_open strictly above published_down_limit")
    slipped = raw * (1.0 - float(slippage_bps) / 10_000.0)
    # Legal price ordering is exact — cash amount tolerance must not decide floor/fill.
    legal_limit_floor_applied = slipped < down
    fill = max(slipped, down)
    if fill < down:
        raise ValueError("hypothetical fill price must not be below published_down_limit")
    if fill > raw:
        raise ValueError("hypothetical fill price must not exceed raw_open")
    gross = fill * float(qty)
    commission = max(gross * BOUND_BASE_COMMISSION_PER_SIDE, BOUND_MINIMUM_COMMISSION_CNY)
    # Full E10f-0 verify inside stamp_tax_rate_for / stamp_tax_amount — no legacy CostConfig tax.
    rate = stamp_tax_rate_for(trade_date, "sell", contract=stamp_tax_contract)
    tax = stamp_tax_amount(
        transaction_amount=gross,
        trade_date=trade_date,
        side="sell",
        contract=stamp_tax_contract,
    )
    if not _amounts_equal(tax, gross * rate):
        raise ValueError("stamp_tax_amount must equal gross * verified rate")
    net = gross - commission - tax
    if net < -_CASH_ABS_TOL:
        raise ValueError("net_sale_cash must be non-negative within tolerance")
    return ExitSellCostScenarioRow(
        scenario_label=label,
        slippage_bps=slippage_bps,
        hypothetical_fill_price=fill,
        shares=qty,
        gross_notional=gross,
        commission=commission,
        stamp_tax=tax,
        stamp_tax_rate=rate,
        net_sale_cash=max(net, 0.0),
        legal_limit_floor_applied=legal_limit_floor_applied,
    )


def _scheduled_exit_date(*, entry_trade_date: date, calendar: Sequence[date]) -> date:
    days = _assert_strict_calendar(calendar)
    try:
        entry_index = days.index(entry_trade_date)
    except ValueError as exc:
        raise ValueError("entry_trade_date missing from market_calendar") from exc
    scheduled_index = entry_index + BOUND_EXIT_ATTEMPT_OFFSET_FROM_ENTRY_INDEX
    if scheduled_index >= len(days):
        raise ValueError(
            "market_calendar too short for fixed 40-bar exit "
            f"(need index entry+{BOUND_EXIT_ATTEMPT_OFFSET_FROM_ENTRY_INDEX})"
        )
    return days[scheduled_index]


def _scan_exit_attempts(
    *,
    observations: Sequence[LayerTwoFixedHorizonExitObservation],
    calendar: Sequence[date],
    scheduled: date,
    symbol: str,
    snapshot_id: str,
) -> tuple[ExitFinalOutcome, list[ExitAttemptRow], date | None, LayerTwoFixedHorizonExitObservation | None]:
    days = _assert_strict_calendar(calendar)
    scheduled_index = days.index(scheduled)
    if not observations:
        raise ValueError("exit_observations must include at least the scheduled_exit_attempt_date")
    obs_dates = [row.observation_date for row in observations]
    if obs_dates[0] != scheduled:
        raise ValueError("exit_observations must start at scheduled_exit_attempt_date")
    if len(obs_dates) != len(set(obs_dates)):
        raise ValueError("exit_observations must not contain duplicate dates")
    expected_slice = days[scheduled_index : scheduled_index + len(observations)]
    if list(obs_dates) != list(expected_slice):
        raise ValueError(
            "exit_observations must be contiguous calendar days starting at scheduled date (no gaps/skips)"
        )

    rows: list[ExitAttemptRow] = []
    final: ExitFinalOutcome | None = None
    exit_day: date | None = None
    exit_obs: LayerTwoFixedHorizonExitObservation | None = None

    for offset, observation in enumerate(observations):
        if observation.symbol != symbol:
            raise ValueError("exit observation symbol must equal lifecycle symbol")
        if observation.market_data_snapshot_id != snapshot_id:
            raise ValueError("exit observation snapshot must equal lifecycle market snapshot")
        _require_declared_window(observation.observation_date, field_name="observation_date")
        bars_before = BOUND_HOLDING_PERIOD_MARKET_BARS + offset
        if observation.observation_status == "unknown":
            rows.append(
                ExitAttemptRow(
                    observation_date=observation.observation_date,
                    holding_market_bars_elapsed_before_open=bars_before,
                    observation_status="unknown",
                    attempt_outcome="unknown_exit_observation",
                )
            )
            final = "unknown_exit_observation"
            exit_day = observation.observation_date
            exit_obs = observation
            if offset != len(observations) - 1:
                raise ValueError("observations after unknown_exit_observation are forbidden")
            break
        if observation.observation_status == "known_full_day_suspension":
            rows.append(
                ExitAttemptRow(
                    observation_date=observation.observation_date,
                    holding_market_bars_elapsed_before_open=bars_before,
                    observation_status="known_full_day_suspension",
                    attempt_outcome="blocked_suspension",
                )
            )
            continue
        assert observation.raw_open is not None and observation.published_down_limit is not None
        # Legal price order is exact: cash amount tolerance must not decide limit-down.
        if observation.raw_open <= observation.published_down_limit:
            rows.append(
                ExitAttemptRow(
                    observation_date=observation.observation_date,
                    holding_market_bars_elapsed_before_open=bars_before,
                    observation_status="tradable",
                    attempt_outcome="blocked_limit_down",
                )
            )
            continue
        rows.append(
            ExitAttemptRow(
                observation_date=observation.observation_date,
                holding_market_bars_elapsed_before_open=bars_before,
                observation_status="tradable",
                attempt_outcome="hypothetically_exitable",
            )
        )
        final = "hypothetically_exitable"
        exit_day = observation.observation_date
        exit_obs = observation
        if offset != len(observations) - 1:
            raise ValueError("observations after first hypothetically_exitable day are forbidden")
        break

    if final is None:
        final = "still_open_after_observed_blocks"
        exit_day = None
        exit_obs = None
    return final, rows, exit_day, exit_obs


def diagnose_layer_two_fixed_horizon_exit(
    *,
    lifecycle_record: LayerTwoHypotheticalPositionLifecycleRecord,
    lifecycle_structural: LayerTwoHypotheticalLifecycleStructuralInput,
    stamp_tax_contract: AShareStampTaxScheduleContract,
    market_calendar: Sequence[date],
    exit_observations: Sequence[LayerTwoFixedHorizonExitObservation],
) -> LayerTwoFixedHorizonExitDiagnosticReport:
    """Build a sealed fixed-horizon exit availability / sell-cost diagnostic."""
    assert_record_self_hash(lifecycle_record)
    verify_layer_two_hypothetical_position_lifecycle_record(
        lifecycle_record,
        structural=lifecycle_structural,
    )
    assert_contract_self_hash(stamp_tax_contract)
    verify_a_share_stamp_tax_schedule(stamp_tax_contract)

    phase_calendar = list(lifecycle_structural.phase_report.market_calendar)
    calendar = _assert_strict_calendar(market_calendar)
    if calendar != _assert_strict_calendar(phase_calendar):
        raise ValueError("market_calendar must exactly equal phase_report.market_calendar")

    entry = lifecycle_record.entry_trade_date
    _require_declared_window(entry, field_name="entry_trade_date")
    scheduled = _scheduled_exit_date(entry_trade_date=entry, calendar=calendar)
    _require_declared_window(scheduled, field_name="scheduled_exit_attempt_date")

    final, rows, exit_day, exit_obs = _scan_exit_attempts(
        observations=exit_observations,
        calendar=calendar,
        scheduled=scheduled,
        symbol=lifecycle_record.symbol,
        snapshot_id=lifecycle_record.market_data_snapshot_id,
    )

    base_row: ExitSellCostScenarioRow | None = None
    stress_row: ExitSellCostScenarioRow | None = None
    if final == "hypothetically_exitable":
        assert exit_obs is not None and exit_day is not None
        assert exit_obs.raw_open is not None and exit_obs.published_down_limit is not None
        base_row = build_exit_sell_cost_scenario(
            label="base_5bps",
            slippage_bps=BOUND_BASE_SLIPPAGE_BPS,
            raw_open=float(exit_obs.raw_open),
            published_down_limit=float(exit_obs.published_down_limit),
            shares=lifecycle_record.shares,
            trade_date=exit_day,
            stamp_tax_contract=stamp_tax_contract,
        )
        stress_row = build_exit_sell_cost_scenario(
            label="stress_15bps",
            slippage_bps=BOUND_STRESS_SLIPPAGE_BPS,
            raw_open=float(exit_obs.raw_open),
            published_down_limit=float(exit_obs.published_down_limit),
            shares=lifecycle_record.shares,
            trade_date=exit_day,
            stamp_tax_contract=stamp_tax_contract,
        )

    susp = sum(1 for row in rows if row.attempt_outcome == "blocked_suspension")
    lim = sum(1 for row in rows if row.attempt_outcome == "blocked_limit_down")
    if lifecycle_record.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("lifecycle tranche_evaluation_protocol_id must equal sealed tranche protocol id")
    if lifecycle_record.tranche_evaluation_protocol_path != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
        raise ValueError("lifecycle tranche_evaluation_protocol_path must equal sealed tranche protocol path")
    assembled = LayerTwoFixedHorizonExitDiagnosticReport(
        lifecycle_record_id=_require_sealed_hex64(lifecycle_record.record_id, field_name="lifecycle_record_id"),
        entry_execution_report_id=lifecycle_record.entry_execution_report_id,
        allocator_report_id=lifecycle_record.allocator_report_id,
        constraint_assembler_report_id=lifecycle_record.constraint_assembler_report_id,
        phase_report_id=lifecycle_record.phase_report_id,
        current_state_id=lifecycle_record.current_state_id,
        stamp_tax_contract_id=_require_sealed_hex64(stamp_tax_contract.contract_id, field_name="stamp_tax_contract_id"),
        market_calendar_sha256=compute_market_calendar_sha256(calendar),
        market_data_snapshot_id=lifecycle_record.market_data_snapshot_id,
        tranche_evaluation_protocol_id=lifecycle_record.tranche_evaluation_protocol_id,
        tranche_evaluation_protocol_path=lifecycle_record.tranche_evaluation_protocol_path,
        symbol=lifecycle_record.symbol,
        shares=lifecycle_record.shares,
        entry_trade_date=entry,
        scheduled_exit_attempt_date=scheduled,
        final_outcome=final,
        blocked_suspension_days=susp,
        blocked_limit_down_days=lim,
        attempt_rows=rows,
        exit_observation_date=exit_day,
        base_scenario=base_row,
        stress_scenario=stress_row,
    )
    return seal_layer_two_fixed_horizon_exit_diagnostic_report(assembled)


def canonical_report_payload(report: LayerTwoFixedHorizonExitDiagnosticReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: LayerTwoFixedHorizonExitDiagnosticReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: LayerTwoFixedHorizonExitDiagnosticReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_layer_two_fixed_horizon_exit_diagnostic_report(
    report: LayerTwoFixedHorizonExitDiagnosticReport,
) -> LayerTwoFixedHorizonExitDiagnosticReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: LayerTwoFixedHorizonExitDiagnosticReport) -> None:
    if report.report_id is None:
        raise ValueError("fixed-horizon exit diagnostic report_id is missing")
    if report.report_id != compute_report_id(report):
        raise ValueError("fixed-horizon exit diagnostic report_id does not match canonical content hash")


def assert_matches_recomputed_exit_diagnostic(
    report: LayerTwoFixedHorizonExitDiagnosticReport,
    *,
    structural: LayerTwoFixedHorizonExitStructuralInput,
) -> None:
    expected = diagnose_layer_two_fixed_horizon_exit(
        lifecycle_record=structural.lifecycle_record,
        lifecycle_structural=structural.lifecycle_structural,
        stamp_tax_contract=structural.stamp_tax_contract,
        market_calendar=structural.market_calendar,
        exit_observations=structural.exit_observations,
    )
    if report.report_id != expected.report_id:
        raise ValueError("fixed-horizon exit diagnostic report_id does not match full recompute")
    if canonical_report_payload(report) != canonical_report_payload(expected):
        raise ValueError("fixed-horizon exit diagnostic canonical payload does not match full recompute")


def verify_layer_two_fixed_horizon_exit_diagnostic_report(
    report: LayerTwoFixedHorizonExitDiagnosticReport,
    *,
    structural: LayerTwoFixedHorizonExitStructuralInput,
) -> LayerTwoFixedHorizonExitVerificationResult:
    """Structural verifier: self-hash + E10f-1/E10f-0 structural verify + full recompute."""
    assert_report_self_hash(report)
    verify_layer_two_hypothetical_position_lifecycle_record(
        structural.lifecycle_record,
        structural=structural.lifecycle_structural,
    )
    verify_a_share_stamp_tax_schedule(structural.stamp_tax_contract)
    assert_matches_recomputed_exit_diagnostic(report, structural=structural)
    return LayerTwoFixedHorizonExitVerificationResult(
        report_id=report.report_id or compute_report_id(report),
        structural_ok=True,
        lifecycle_binding_ok=False,
        stamp_tax_binding_ok=False,
        tranche_evaluation_protocol_binding_ok=False,
        exit_observation_binding_ok=False,
        ready_for_exit_diagnostic=False,
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


def _store_row_proves_complete_tradable_down(row: dict[str, Any]) -> bool:
    if row.get("is_suspended") is not False:
        return False
    try:
        _require_store_finite_positive(row.get("open"), field_name="open")
        _require_store_finite_positive(row.get("down_limit"), field_name="down_limit")
    except ValueError:
        return False
    return True


def _bind_exit_observations_to_store(
    *,
    report: LayerTwoFixedHorizonExitDiagnosticReport,
    observations: Sequence[LayerTwoFixedHorizonExitObservation],
    store: MarketStore,
    market_calendar: Sequence[date],
) -> None:
    if store.snapshot().snapshot_id != report.market_data_snapshot_id:
        raise ValueError("MarketStore snapshot_id must equal report.market_data_snapshot_id")
    calendar = _assert_strict_calendar(market_calendar)
    store_calendar = list(store.get_calendar(calendar[0], calendar[-1]))
    for day in calendar:
        if day not in store_calendar:
            raise ValueError("report market_calendar day missing from MarketStore calendar")
    for observation in observations:
        if observation.observation_date not in store_calendar:
            raise ValueError("exit observation_date missing from MarketStore calendar")
        rows = _exact_symbol_date_daily_rows(
            store,
            day=observation.observation_date,
            symbol=observation.symbol,
        )
        if observation.observation_status == "unknown":
            if not rows:
                continue
            row = rows[0]
            if _store_row_proves_suspension(row) or _store_row_proves_complete_tradable_down(row):
                raise ValueError("unknown exit observation forbidden when MarketStore has a complete determinate row")
            continue
        if observation.observation_status == "known_full_day_suspension":
            if len(rows) != 1:
                raise ValueError("suspension exit observation requires exactly one MarketStore daily row")
            if rows[0].get("is_suspended") is not True:
                raise ValueError("suspension exit observation requires store is_suspended=true")
            continue
        if len(rows) != 1:
            raise ValueError("tradable exit observation requires exactly one MarketStore daily row")
        row = rows[0]
        if row.get("is_suspended") is not False:
            raise ValueError("tradable exit observation requires store is_suspended=false")
        open_px = _require_store_finite_positive(row.get("open"), field_name="open")
        down = _require_store_finite_positive(row.get("down_limit"), field_name="down_limit")
        if observation.raw_open != open_px:
            raise ValueError("exit observation.raw_open must exactly equal store open (not adj_open)")
        if observation.published_down_limit != down:
            raise ValueError("exit observation.published_down_limit must exactly equal store down_limit")


def verify_layer_two_fixed_horizon_exit_diagnostic_report_file(
    *,
    report: LayerTwoFixedHorizonExitDiagnosticReport,
    file_input: LayerTwoFixedHorizonExitFileInput,
) -> LayerTwoFixedHorizonExitVerificationResult:
    """File verifier: structural path + real E10f-1/E10f-0 file verifiers + exit observation store bind."""
    structural_result = verify_layer_two_fixed_horizon_exit_diagnostic_report(
        report,
        structural=file_input.structural,
    )
    if structural_result.structural_ok is not True:
        raise ValueError("structural verifier must succeed before file binding")
    if (
        structural_result.lifecycle_binding_ok
        or structural_result.stamp_tax_binding_ok
        or structural_result.tranche_evaluation_protocol_binding_ok
        or structural_result.exit_observation_binding_ok
        or structural_result.ready_for_exit_diagnostic
    ):
        raise ValueError("structural verifier must not claim disk binding or exit readiness")

    lifecycle_file = verify_layer_two_hypothetical_position_lifecycle_record_file(
        record=file_input.structural.lifecycle_record,
        file_input=file_input.lifecycle_file,
    )
    if (
        lifecycle_file.structural_ok is not True
        or lifecycle_file.entry_execution_binding_ok is not True
        or lifecycle_file.allocator_binding_ok is not True
        or lifecycle_file.phase_binding_ok is not True
        or lifecycle_file.tranche_evaluation_protocol_binding_ok is not True
        or lifecycle_file.ready_for_lifecycle_diagnostic is not True
    ):
        raise ValueError("E10f-1 file verifier full bindings/ready required for exit file path")
    if lifecycle_file.record_id != report.lifecycle_record_id:
        raise ValueError("E10f-1 file verifier record_id must equal report.lifecycle_record_id")

    root = Path(file_input.stamp_tax_repo_root).resolve()
    lifecycle_root = Path(file_input.lifecycle_file.file_bindings.repo_root).resolve()
    if root != lifecycle_root:
        raise ValueError("stamp_tax_repo_root must equal lifecycle file_bindings.repo_root")
    disk_contract, stamp_file = verify_a_share_stamp_tax_schedule_file(repo_root=root)
    if stamp_file.structural_ok is not True or stamp_file.disk_binding_ok is not True:
        raise ValueError("E10f-0 file verifier disk_binding_ok required for exit file path")
    if stamp_file.ready_for_exit_diagnostic is not True:
        raise ValueError("E10f-0 file verifier ready_for_exit_diagnostic required")
    if disk_contract.contract_id != EXPECTED_CURRENT_CONTRACT_ID:
        raise ValueError("on-disk stamp-tax contract_id must equal EXPECTED_CURRENT_CONTRACT_ID")
    if disk_contract.contract_id != report.stamp_tax_contract_id:
        raise ValueError("on-disk stamp-tax contract_id must equal report.stamp_tax_contract_id")
    if str(BOUND_A_SHARE_STAMP_TAX_SCHEDULE_PATH) != "config/research/a-share-stamp-tax-schedule-v1.json":
        raise ValueError("bound stamp-tax path drifted")

    protocol_path = root / BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
    if str(DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH) != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
        raise ValueError("tranche evaluation default path drifted")
    if report.tranche_evaluation_protocol_path != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
        raise ValueError("report tranche_evaluation_protocol_path must equal bound path")
    if report.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("report tranche_evaluation_protocol_id must equal bound protocol id")
    lifecycle_record = file_input.structural.lifecycle_record
    if lifecycle_record.tranche_evaluation_protocol_id != report.tranche_evaluation_protocol_id:
        raise ValueError("report tranche_evaluation_protocol_id must equal lifecycle binding")
    if lifecycle_record.tranche_evaluation_protocol_path != report.tranche_evaluation_protocol_path:
        raise ValueError("report tranche_evaluation_protocol_path must equal lifecycle binding")
    protocol_doc, protocol_result = verify_tranche_evaluation_protocol_draft_file(
        protocol_path=protocol_path,
        repo_root=root,
    )
    if protocol_result.schema_version != "2":
        raise ValueError("bound tranche evaluation protocol must be schema v2")
    if not isinstance(protocol_doc, TrancheEvaluationProtocolV2):
        raise ValueError("bound tranche evaluation protocol must be schema v2 document")
    if protocol_result.protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("tranche evaluation protocol_id on disk does not match bound constant")
    if protocol_result.protocol_id != report.tranche_evaluation_protocol_id:
        raise ValueError("tranche evaluation protocol_id on disk does not match report binding")
    if protocol_doc.protocol_id != report.tranche_evaluation_protocol_id:
        raise ValueError("loaded tranche protocol_id does not match report binding")
    hold = protocol_doc.tranche_hold
    if int(hold.holding_period_market_trading_days) != BOUND_HOLDING_PERIOD_MARKET_BARS:
        raise ValueError("disk tranche_hold.holding_period_market_trading_days must equal 40")
    if int(hold.holding_cycle_market_trading_days) != BOUND_HOLDING_PERIOD_MARKET_BARS:
        raise ValueError("disk tranche_hold.holding_cycle_market_trading_days must equal 40")
    timing = protocol_doc.decision_timing
    if timing.fill_day_is_holding_day_1 is not True:
        raise ValueError("disk decision_timing.fill_day_is_holding_day_1 must be true")
    if timing.exit_after_holding_period_at_next_tradable_open is not True:
        raise ValueError("disk decision_timing.exit_after_holding_period_at_next_tradable_open must be true")

    store = file_input.lifecycle_file.file_bindings.store
    _bind_exit_observations_to_store(
        report=report,
        observations=file_input.structural.exit_observations,
        store=store,
        market_calendar=file_input.structural.market_calendar,
    )

    return LayerTwoFixedHorizonExitVerificationResult(
        report_id=report.report_id or compute_report_id(report),
        structural_ok=True,
        lifecycle_binding_ok=True,
        stamp_tax_binding_ok=True,
        tranche_evaluation_protocol_binding_ok=True,
        exit_observation_binding_ok=True,
        ready_for_exit_diagnostic=True,
    )


def load_layer_two_fixed_horizon_exit_diagnostic_report(
    path: Path,
) -> LayerTwoFixedHorizonExitDiagnosticReport:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("fixed-horizon exit diagnostic report is missing or invalid") from exc
    try:
        return LayerTwoFixedHorizonExitDiagnosticReport.model_validate(payload)
    except Exception as exc:
        raise ValueError("fixed-horizon exit diagnostic report is missing or invalid") from exc


def write_layer_two_fixed_horizon_exit_diagnostic_report(
    path: Path,
    report: LayerTwoFixedHorizonExitDiagnosticReport,
) -> LayerTwoFixedHorizonExitDiagnosticReport:
    sealed = seal_layer_two_fixed_horizon_exit_diagnostic_report(report)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_EXIT_ATTEMPT_OFFSET_FROM_ENTRY_INDEX",
    "BOUND_HOLDING_PERIOD_MARKET_BARS",
    "BOUND_TRANCHE_EVALUATION_PROTOCOL_ID",
    "BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH",
    "LAYER_TWO_FIXED_HORIZON_EXIT_ENGINE_VERSION",
    "LAYER_TWO_FIXED_HORIZON_EXIT_SCHEMA_VERSION",
    "ExitAttemptRow",
    "ExitSellCostScenarioRow",
    "LayerTwoFixedHorizonExitDiagnosticReport",
    "LayerTwoFixedHorizonExitFileInput",
    "LayerTwoFixedHorizonExitObservation",
    "LayerTwoFixedHorizonExitStructuralInput",
    "LayerTwoFixedHorizonExitVerificationResult",
    "assert_matches_recomputed_exit_diagnostic",
    "assert_report_self_hash",
    "build_exit_sell_cost_scenario",
    "canonical_report_bytes",
    "canonical_report_payload",
    "compute_market_calendar_sha256",
    "compute_report_id",
    "diagnose_layer_two_fixed_horizon_exit",
    "load_layer_two_fixed_horizon_exit_diagnostic_report",
    "seal_layer_two_fixed_horizon_exit_diagnostic_report",
    "verify_layer_two_fixed_horizon_exit_diagnostic_report",
    "verify_layer_two_fixed_horizon_exit_diagnostic_report_file",
    "write_layer_two_fixed_horizon_exit_diagnostic_report",
]
