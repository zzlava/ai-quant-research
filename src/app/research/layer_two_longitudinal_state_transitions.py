"""Layer-two longitudinal cash / tranche state-transition diagnostic (E10f-3b).

Research-only sequencing of sealed E10f-1 open records and E10f-2 fixed-40-bar
exit diagnostics into an auditable cash-flow + tranche-occupancy state, bound to
one explicit sealed E10e-1 cash-occupancy attribution report (including
not_attempted decisions). Does not compute return, PnL, equity, marks, or
benchmarks; does not emit orders.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.a_share_stamp_tax_schedule import DECLARED_WINDOW_END, DECLARED_WINDOW_START
from app.research.layer_two_allocation_protocol import (
    CONFIRMED_INITIAL_CASH,
    DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH,
    verify_layer_two_allocation_protocol_file,
)
from app.research.layer_two_cash_occupancy_attribution import (
    LayerTwoCashOccupancyAttributionReport,
    LayerTwoCashOccupancyFileRowInput,
    LayerTwoCashOccupancyStructuralRowInput,
    verify_layer_two_cash_occupancy_attribution_report,
    verify_layer_two_cash_occupancy_attribution_report_file,
)
from app.research.layer_two_cash_occupancy_attribution import (
    assert_report_self_hash as assert_cash_occupancy_self_hash,
)
from app.research.layer_two_entry_execution_diagnostic import (
    BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
)
from app.research.layer_two_fixed_horizon_exit_diagnostic import (
    LayerTwoFixedHorizonExitDiagnosticReport,
    LayerTwoFixedHorizonExitFileInput,
    LayerTwoFixedHorizonExitStructuralInput,
    verify_layer_two_fixed_horizon_exit_diagnostic_report,
    verify_layer_two_fixed_horizon_exit_diagnostic_report_file,
)
from app.research.layer_two_fixed_horizon_exit_diagnostic import (
    assert_report_self_hash as assert_exit_report_self_hash,
)
from app.research.layer_two_hypothetical_position_lifecycle import (
    LayerTwoHypotheticalLifecycleFileInput,
    LayerTwoHypotheticalLifecycleStructuralInput,
    LayerTwoHypotheticalPositionLifecycleRecord,
    verify_layer_two_hypothetical_position_lifecycle_record,
    verify_layer_two_hypothetical_position_lifecycle_record_file,
)
from app.research.layer_two_hypothetical_position_lifecycle import (
    assert_record_self_hash as assert_lifecycle_self_hash,
)
from app.research.layer_two_stateful_allocator import LayerTwoStatefulPortfolioState
from app.research.two_layer_contract import CONFIRMED_INITIAL_CASH as _CONTRACT_INITIAL_CASH

LAYER_TWO_LONGITUDINAL_SCHEMA_VERSION: Literal["2"] = "2"
LAYER_TWO_LONGITUDINAL_ENGINE_VERSION: Literal["layer-two-longitudinal-state-transitions-v2"] = (
    "layer-two-longitudinal-state-transitions-v2"
)

BOUND_INITIAL_CASH: Literal[80000] = CONFIRMED_INITIAL_CASH
BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH: Literal[
    "config/research/layer-two-allocation-implementation-protocol-v1.json"
] = "config/research/layer-two-allocation-implementation-protocol-v1.json"
BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID: Literal[
    "0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"
] = "0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"

if BOUND_INITIAL_CASH != _CONTRACT_INITIAL_CASH:
    raise RuntimeError("E10f-3a initial cash drifted from confirmed two-layer initial cash")
if str(DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH) != BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH:
    raise RuntimeError("E10f-3a allocation protocol path drifted from default constant")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CASH_ABS_TOL = 1e-9

TransitionKind = Literal["entry_opened", "exit_closed", "exit_deferred", "exit_unknown_halt"]
PositionStatus = Literal["open", "deferred_still_open", "closed", "unknown_halted"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


def _require_non_bool_int(value: object, *, field_name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a non-bool int")
    if value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value


def _require_real_number(
    value: object,
    *,
    field_name: str,
    minimum: float | None = None,
    minimum_exclusive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a real number (bool rejected)")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{field_name} must be finite (NaN/Inf rejected)")
    if minimum is not None:
        if minimum_exclusive:
            if number <= minimum:
                raise ValueError(f"{field_name} must be > {minimum}")
        elif number < minimum:
            raise ValueError(f"{field_name} must be >= {minimum}")
    return number


def _amounts_equal(left: float, right: float, *, abs_tol: float = _CASH_ABS_TOL) -> bool:
    return abs(float(left) - float(right)) <= abs_tol


def _require_declared_window(day: date, *, field_name: str) -> date:
    if day < DECLARED_WINDOW_START or day > DECLARED_WINDOW_END:
        raise ValueError(
            f"{field_name} must lie within declared_window "
            f"{DECLARED_WINDOW_START.isoformat()}..{DECLARED_WINDOW_END.isoformat()} "
            "(2025+ longitudinal evaluation forbidden)"
        )
    return day


def _assert_e10e1_inputs_within_declared_window(
    *,
    occupancy_report: LayerTwoCashOccupancyAttributionReport,
    occupancy_rows: tuple[LayerTwoCashOccupancyStructuralRowInput, ...],
) -> None:
    """Fail closed if any E10e-1 coverage/row/entry timing spills outside 2022..2024.

    Extra occupancy decisions may fall outside the longitudinal transition start/end
    (intentional subset), but must still lie inside the declared research window.
    """
    _require_declared_window(occupancy_report.coverage_as_of_start, field_name="cash_occupancy.coverage_as_of_start")
    _require_declared_window(occupancy_report.coverage_as_of_end, field_name="cash_occupancy.coverage_as_of_end")
    for index, row in enumerate(occupancy_report.rows):
        _require_declared_window(row.as_of, field_name=f"cash_occupancy.rows[{index}].as_of")
    for index, structural_row in enumerate(occupancy_rows):
        entry = structural_row.entry_execution_report
        _require_declared_window(entry.as_of, field_name=f"cash_occupancy_rows[{index}].entry_execution_report.as_of")
        _require_declared_window(
            entry.expected_t1_execution_date,
            field_name=f"cash_occupancy_rows[{index}].entry_execution_report.expected_t1_execution_date",
        )
        observation = structural_row.execution_observation
        if observation is not None:
            _require_declared_window(
                observation.execution_date,
                field_name=f"cash_occupancy_rows[{index}].execution_observation.execution_date",
            )


def _exit_event_date(report: LayerTwoFixedHorizonExitDiagnosticReport) -> date:
    if report.final_outcome == "still_open_after_observed_blocks":
        if not report.attempt_rows:
            raise ValueError("still_open exit report must have attempt_rows")
        return report.attempt_rows[-1].observation_date
    if report.exit_observation_date is None:
        raise ValueError("exitable/unknown exit report requires exit_observation_date")
    return report.exit_observation_date


def _tracked_open_or_deferred(
    positions: dict[str, LongitudinalActivePosition],
) -> list[LongitudinalActivePosition]:
    """Positions that still occupy cash/tranche in this no-mark diagnostic."""
    return [row for row in positions.values() if row.status in ("open", "deferred_still_open")]


def assert_lifecycle_current_state_matches_longitudinal_start_of_day(
    *,
    current_state: LayerTwoStatefulPortfolioState,
    sod_cash: float,
    sod_positions: dict[str, LongitudinalActivePosition],
    longitudinal_market_data_snapshot_id: str | None,
) -> None:
    """Bind E10f-1 current_state to longitudinal start-of-day cash/tranche occupancy.

    ``current_market_notional`` on each held row must equal the carried entry
    ``stock_notional`` (cost-notional carry). This diagnostic does not mark to market.
    """
    if not _amounts_equal(float(current_state.cash), float(sod_cash)):
        raise ValueError(
            "lifecycle current_state.cash must equal longitudinal start-of-day cash "
            "(first entry requires 80000 with zero positions)"
        )

    tracked = _tracked_open_or_deferred(sod_positions)
    expected_keys = {(int(row.tranche_id), row.symbol, row.cluster_id): float(row.stock_notional) for row in tracked}
    actual_rows = list(current_state.positions)
    actual_keys = {(int(row.tranche_id), row.symbol, row.cluster_id): row for row in actual_rows}
    if set(actual_keys) != set(expected_keys):
        raise ValueError(
            "lifecycle current_state.positions must exactly match longitudinal "
            "open/deferred positions by tranche_id/symbol/cluster_id"
        )
    for key, expected_notional in expected_keys.items():
        actual = actual_keys[key]
        if not _amounts_equal(float(actual.current_market_notional), expected_notional):
            raise ValueError(
                "current_market_notional must equal carried entry stock_notional "
                "(cost-notional carry; not mark-to-market)"
            )

    carried_gross = sum(float(row.current_market_notional) for row in actual_rows)
    accounted = float(current_state.cash) + carried_gross
    tol = float(current_state.equity_accounting_abs_tol)
    if abs(accounted - float(current_state.current_account_equity)) > tol:
        raise ValueError(
            "current_account_equity must equal cash + carried notionals under the upstream portfolio state identity"
        )

    if longitudinal_market_data_snapshot_id is not None:
        if current_state.market_data_snapshot_id != longitudinal_market_data_snapshot_id:
            raise ValueError(
                "current_state.market_data_snapshot_id must equal longitudinal "
                "market_data_snapshot_id when the run snapshot is already bound"
            )


@dataclass(frozen=True, slots=True)
class LayerTwoLongitudinalEntryStructuralInput:
    lifecycle_record: LayerTwoHypotheticalPositionLifecycleRecord
    lifecycle_structural: LayerTwoHypotheticalLifecycleStructuralInput


@dataclass(frozen=True, slots=True)
class LayerTwoLongitudinalExitStructuralInput:
    exit_report: LayerTwoFixedHorizonExitDiagnosticReport
    exit_structural: LayerTwoFixedHorizonExitStructuralInput


@dataclass(frozen=True, slots=True)
class LayerTwoLongitudinalDayStructuralInput:
    event_date: date
    entry: LayerTwoLongitudinalEntryStructuralInput | None
    exits: tuple[LayerTwoLongitudinalExitStructuralInput, ...]


@dataclass(frozen=True, slots=True)
class LayerTwoLongitudinalStructuralInput:
    days: tuple[LayerTwoLongitudinalDayStructuralInput, ...]
    cash_occupancy_report: LayerTwoCashOccupancyAttributionReport
    cash_occupancy_rows: tuple[LayerTwoCashOccupancyStructuralRowInput, ...]


@dataclass(frozen=True, slots=True)
class LayerTwoLongitudinalEntryFileInput:
    structural: LayerTwoLongitudinalEntryStructuralInput
    lifecycle_file: LayerTwoHypotheticalLifecycleFileInput


@dataclass(frozen=True, slots=True)
class LayerTwoLongitudinalExitFileInput:
    structural: LayerTwoLongitudinalExitStructuralInput
    exit_file: LayerTwoFixedHorizonExitFileInput


@dataclass(frozen=True, slots=True)
class LayerTwoLongitudinalDayFileInput:
    event_date: date
    entry: LayerTwoLongitudinalEntryFileInput | None
    exits: tuple[LayerTwoLongitudinalExitFileInput, ...]


@dataclass(frozen=True, slots=True)
class LayerTwoLongitudinalFileInput:
    days: tuple[LayerTwoLongitudinalDayFileInput, ...]
    repo_root: Path
    cash_occupancy_report: LayerTwoCashOccupancyAttributionReport
    cash_occupancy_file_rows: tuple[LayerTwoCashOccupancyFileRowInput, ...]


class LongitudinalActivePosition(_StrictModel):
    lifecycle_record_id: str = Field(pattern=_HEX64.pattern)
    symbol: str = Field(min_length=1)
    tranche_id: int
    cluster_id: str = Field(min_length=1)
    shares: int
    entry_trade_date: date
    stock_notional: float
    buy_commission: float
    entry_total_cash_used: float
    status: PositionStatus

    @field_validator("entry_trade_date", mode="before")
    @classmethod
    def _date(cls, value: object) -> date:
        return _require_date(value, field_name="entry_trade_date")

    @field_validator("tranche_id", "shares", mode="before")
    @classmethod
    def _ints(cls, value: object, info: Any) -> int:
        minimum = 0 if info.field_name == "tranche_id" else 1
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=minimum)

    @field_validator("stock_notional", "buy_commission", "entry_total_cash_used", mode="before")
    @classmethod
    def _amounts(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("symbol", "cluster_id", mode="before")
    @classmethod
    def _nonblank(cls, value: object, info: Any) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value.strip()


class LongitudinalTransitionRow(_StrictModel):
    event_date: date
    transition_kind: TransitionKind
    lifecycle_record_id: str = Field(pattern=_HEX64.pattern)
    symbol: str = Field(min_length=1)
    tranche_id: int
    shares: int
    cash_before: float
    cash_after: float
    occupied_tranche_ids_before: list[int]
    occupied_tranche_ids_after: list[int]
    entry_total_cash_used: float | None = None
    entry_execution_report_id: str | None = Field(default=None, pattern=_HEX64.pattern)
    exit_net_sale_cash: float | None = None
    exit_report_id: str | None = Field(default=None, pattern=_HEX64.pattern)
    exit_final_outcome: str | None = None

    @field_validator("event_date", mode="before")
    @classmethod
    def _date(cls, value: object) -> date:
        return _require_date(value, field_name="event_date")

    @field_validator("tranche_id", "shares", mode="before")
    @classmethod
    def _ints(cls, value: object, info: Any) -> int:
        minimum = 0 if info.field_name == "tranche_id" else 1
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=minimum)

    @field_validator("cash_before", "cash_after", mode="before")
    @classmethod
    def _cash(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("entry_total_cash_used", "exit_net_sale_cash", mode="before")
    @classmethod
    def _optional_amount(cls, value: object, info: Any) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("occupied_tranche_ids_before", "occupied_tranche_ids_after", mode="before")
    @classmethod
    def _tranche_lists(cls, value: object, info: Any) -> list[int]:
        if not isinstance(value, list | tuple):
            raise ValueError(f"{info.field_name} must be a list of ints")
        out: list[int] = []
        for item in value:
            out.append(_require_non_bool_int(item, field_name=str(info.field_name), minimum=0))
        if out != sorted(out) or len(out) != len(set(out)):
            raise ValueError(f"{info.field_name} must be strictly sorted unique tranche ids")
        return out

    @field_validator("symbol", mode="before")
    @classmethod
    def _nonblank(cls, value: object) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("symbol must be a non-empty string")
        return value.strip()

    @model_validator(mode="after")
    def _kind_fields(self) -> LongitudinalTransitionRow:
        if self.transition_kind == "entry_opened":
            if self.entry_total_cash_used is None or self.exit_net_sale_cash is not None:
                raise ValueError("entry_opened requires entry_total_cash_used and null exit_net_sale_cash")
            if self.entry_execution_report_id is None:
                raise ValueError("entry_opened requires entry_execution_report_id")
            if self.exit_report_id is not None or self.exit_final_outcome is not None:
                raise ValueError("entry_opened must keep exit_report_id/outcome null")
            if not _amounts_equal(self.cash_after, self.cash_before - float(self.entry_total_cash_used)):
                raise ValueError("entry_opened cash_after must equal cash_before - entry_total_cash_used")
        elif self.transition_kind == "exit_closed":
            if self.exit_net_sale_cash is None or self.entry_total_cash_used is not None:
                raise ValueError("exit_closed requires exit_net_sale_cash and null entry_total_cash_used")
            if self.entry_execution_report_id is not None:
                raise ValueError("exit transitions must keep entry_execution_report_id null")
            if self.exit_report_id is None or self.exit_final_outcome != "hypothetically_exitable":
                raise ValueError("exit_closed requires exit_report_id and hypothetically_exitable outcome")
            if not _amounts_equal(self.cash_after, self.cash_before + float(self.exit_net_sale_cash)):
                raise ValueError("exit_closed cash_after must equal cash_before + exit_net_sale_cash")
        else:
            if self.entry_total_cash_used is not None or self.exit_net_sale_cash is not None:
                raise ValueError("deferred/unknown transitions must keep cash delta amounts null")
            if self.entry_execution_report_id is not None:
                raise ValueError("exit transitions must keep entry_execution_report_id null")
            if self.exit_report_id is None:
                raise ValueError("deferred/unknown transitions require exit_report_id")
            if self.cash_after != self.cash_before and not _amounts_equal(self.cash_after, self.cash_before):
                raise ValueError("deferred/unknown transitions must not change cash")
            if self.occupied_tranche_ids_after != self.occupied_tranche_ids_before:
                raise ValueError("deferred/unknown transitions must not release occupied tranches")
            if self.transition_kind == "exit_deferred":
                if self.exit_final_outcome != "still_open_after_observed_blocks":
                    raise ValueError("exit_deferred requires still_open_after_observed_blocks")
            elif self.exit_final_outcome != "unknown_exit_observation":
                raise ValueError("exit_unknown_halt requires unknown_exit_observation")
        return self


class LayerTwoLongitudinalStateTransitionReport(_StrictModel):
    schema_version: Literal["2"] = LAYER_TWO_LONGITUDINAL_SCHEMA_VERSION
    engine_version: Literal["layer-two-longitudinal-state-transitions-v2"] = LAYER_TWO_LONGITUDINAL_ENGINE_VERSION
    report_id: str | None = Field(default=None, pattern=_HEX64.pattern)

    start_date: date
    end_date: date
    market_data_snapshot_id: str = Field(min_length=1)
    phase_report_id: str = Field(pattern=_HEX64.pattern)
    cash_occupancy_attribution_report_id: str = Field(pattern=_HEX64.pattern)
    cash_occupancy_input_entry_execution_report_ids: list[str]
    allocation_implementation_protocol_id: Literal[
        "0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"
    ] = BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID
    allocation_implementation_protocol_path: Literal[
        "config/research/layer-two-allocation-implementation-protocol-v1.json"
    ] = BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH
    tranche_evaluation_protocol_id: Literal["8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_ID
    )
    tranche_evaluation_protocol_path: Literal["config/research/tranche-evaluation-protocol-draft-v1.json"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
    )

    initial_cash: Literal[80000] = BOUND_INITIAL_CASH
    cumulative_entry_total_cash_used: float
    cumulative_base_exit_net_cash_received: float
    ending_cash: float
    open_position_count: int
    closed_position_count: int
    deferred_position_count: int
    unknown_halt_position_count: int
    terminal_unknown_halt: bool
    transition_rows: list[LongitudinalTransitionRow]
    ending_positions: list[LongitudinalActivePosition]

    research_only: Literal[True] = True
    diagnostic_only: Literal[True] = True
    post_decision_label_only: Literal[True] = True
    does_not_compute_return_pnl_equity_or_mark: Literal[True] = True
    does_not_emit_orders_or_trades: Literal[True] = True
    cash_flow_identity_only: Literal[True] = True
    same_day_exit_cash_and_tranche_not_reusable_for_entry: Literal[True] = True
    fixed_40_bar_exit_semantics_from_e10f2_only: Literal[True] = True
    does_not_reinterpret_consumed_p10_h20: Literal[True] = True
    ready_for_longitudinal_diagnostic: Literal[False] = False
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _dates(cls, value: object, info: Any) -> date:
        return _require_date(value, field_name=str(info.field_name))

    @field_validator(
        "cumulative_entry_total_cash_used",
        "cumulative_base_exit_net_cash_received",
        "ending_cash",
        mode="before",
    )
    @classmethod
    def _cash_amounts(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator(
        "open_position_count",
        "closed_position_count",
        "deferred_position_count",
        "unknown_halt_position_count",
        mode="before",
    )
    @classmethod
    def _counts(cls, value: object, info: Any) -> int:
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=0)

    @field_validator("terminal_unknown_halt", mode="before")
    @classmethod
    def _bool(cls, value: object) -> bool:
        return _require_strict_bool(value, field_name="terminal_unknown_halt")

    @field_validator("market_data_snapshot_id", mode="before")
    @classmethod
    def _nonblank(cls, value: object) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("market_data_snapshot_id must be a non-empty string")
        return value.strip()

    @field_validator(
        "research_only",
        "diagnostic_only",
        "post_decision_label_only",
        "does_not_compute_return_pnl_equity_or_mark",
        "does_not_emit_orders_or_trades",
        "cash_flow_identity_only",
        "same_day_exit_cash_and_tranche_not_reusable_for_entry",
        "fixed_40_bar_exit_semantics_from_e10f2_only",
        "does_not_reinterpret_consumed_p10_h20",
        mode="before",
    )
    @classmethod
    def _true(cls, value: object, info: Any) -> object:
        return _require_literal_true(value, field_name=str(info.field_name))

    @field_validator(
        "ready_for_longitudinal_diagnostic",
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
    def _identities(self) -> LayerTwoLongitudinalStateTransitionReport:
        if self.initial_cash != BOUND_INITIAL_CASH:
            raise ValueError("initial_cash must equal 80000")
        if self.end_date < self.start_date:
            raise ValueError("end_date must be >= start_date")
        expected_ending = (
            float(self.initial_cash)
            - float(self.cumulative_entry_total_cash_used)
            + float(self.cumulative_base_exit_net_cash_received)
        )
        if not _amounts_equal(self.ending_cash, expected_ending):
            raise ValueError(
                "ending_cash must equal initial_cash - cumulative_entry_total_cash_used "
                "+ cumulative_base_exit_net_cash_received (cash-flow identity)"
            )
        if self.ending_cash < -_CASH_ABS_TOL:
            raise ValueError("ending_cash must be non-negative")
        open_n = sum(1 for p in self.ending_positions if p.status == "open")
        deferred_n = sum(1 for p in self.ending_positions if p.status == "deferred_still_open")
        unknown_n = sum(1 for p in self.ending_positions if p.status == "unknown_halted")
        if open_n != self.open_position_count:
            raise ValueError("open_position_count must match ending_positions")
        if deferred_n != self.deferred_position_count:
            raise ValueError("deferred_position_count must match ending_positions")
        if unknown_n != self.unknown_halt_position_count:
            raise ValueError("unknown_halt_position_count must match ending_positions")
        if any(p.status == "closed" for p in self.ending_positions):
            raise ValueError("ending_positions must not contain closed positions")
        if self.terminal_unknown_halt is True and self.unknown_halt_position_count < 1:
            raise ValueError("terminal_unknown_halt requires at least one unknown_halted position")
        if self.terminal_unknown_halt is False and self.unknown_halt_position_count != 0:
            raise ValueError("unknown_halt positions require terminal_unknown_halt=true")
        closed_from_rows = sum(1 for row in self.transition_rows if row.transition_kind == "exit_closed")
        if closed_from_rows != self.closed_position_count:
            raise ValueError("closed_position_count must match exit_closed transitions")
        occ_ids = list(self.cash_occupancy_input_entry_execution_report_ids)
        if not occ_ids:
            raise ValueError("cash_occupancy_input_entry_execution_report_ids must be non-empty")
        for item in occ_ids:
            _require_sealed_hex64(item, field_name="cash_occupancy_input_entry_execution_report_id")
        if len(occ_ids) != len(set(occ_ids)):
            raise ValueError("cash_occupancy_input_entry_execution_report_ids must be unique")
        opened_entry_ids = [
            row.entry_execution_report_id for row in self.transition_rows if row.transition_kind == "entry_opened"
        ]
        if any(item is None for item in opened_entry_ids):
            raise ValueError("entry_opened rows require entry_execution_report_id")
        opened_entry_ids_s = [str(item) for item in opened_entry_ids]
        if len(opened_entry_ids_s) != len(set(opened_entry_ids_s)):
            raise ValueError("opened entry_execution_report_id values must be unique")
        if not set(opened_entry_ids_s).issubset(set(occ_ids)):
            raise ValueError("opened entry_execution_report_id values must be a subset of E10e-1 input IDs")
        return self


class LayerTwoLongitudinalVerificationResult(_StrictModel):
    report_id: str = Field(pattern=_HEX64.pattern)
    structural_ok: bool
    lifecycle_bindings_ok: bool
    exit_bindings_ok: bool
    allocation_protocol_binding_ok: bool
    cash_occupancy_attribution_binding_ok: bool
    ready_for_longitudinal_diagnostic: bool
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator(
        "structural_ok",
        "lifecycle_bindings_ok",
        "exit_bindings_ok",
        "allocation_protocol_binding_ok",
        "cash_occupancy_attribution_binding_ok",
        "ready_for_longitudinal_diagnostic",
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
    def _state_machine(self) -> LayerTwoLongitudinalVerificationResult:
        bindings = (
            self.lifecycle_bindings_ok,
            self.exit_bindings_ok,
            self.allocation_protocol_binding_ok,
            self.cash_occupancy_attribution_binding_ok,
        )
        any_bound = any(bindings)
        all_bound = all(bindings)
        if self.structural_ok is not True:
            if any_bound or self.ready_for_longitudinal_diagnostic:
                raise ValueError("structural_ok=false forbids any binding or ready_for_longitudinal_diagnostic")
            return self
        if self.ready_for_longitudinal_diagnostic is True:
            if not all_bound:
                raise ValueError("ready_for_longitudinal_diagnostic=true requires structural_ok and all bindings true")
            return self
        if any_bound:
            if not all_bound:
                raise ValueError("partial bindings are forbidden")
            raise ValueError("all bindings true requires ready_for_longitudinal_diagnostic=true (file-path shape)")
        return self


def _sorted_tranches(values: set[int]) -> list[int]:
    return sorted(values)


def _assert_lifecycle_protocol_bindings(record: LayerTwoHypotheticalPositionLifecycleRecord) -> None:
    if record.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("lifecycle tranche_evaluation_protocol_id mismatch")
    if record.tranche_evaluation_protocol_path != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
        raise ValueError("lifecycle tranche_evaluation_protocol_path mismatch")
    if record.allocation_implementation_protocol_id != BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID:
        raise ValueError("lifecycle allocation_implementation_protocol_id mismatch")


def _assert_exit_protocol_bindings(report: LayerTwoFixedHorizonExitDiagnosticReport) -> None:
    if report.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("exit tranche_evaluation_protocol_id mismatch")
    if report.tranche_evaluation_protocol_path != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
        raise ValueError("exit tranche_evaluation_protocol_path mismatch")
    if report.holding_period_market_bars != 40:
        raise ValueError("exit holding_period_market_bars must be 40 (p10_h20 forbidden)")


def diagnose_layer_two_longitudinal_state_transitions(
    *,
    structural: LayerTwoLongitudinalStructuralInput,
) -> LayerTwoLongitudinalStateTransitionReport:
    """Build a sealed longitudinal cash/tranche transition report from sealed day inputs."""
    days = list(structural.days)
    if not days:
        raise ValueError("longitudinal days must be non-empty")
    event_dates = [day.event_date for day in days]
    for event_date in event_dates:
        _require_declared_window(event_date, field_name="event_date")
    if event_dates != sorted(event_dates) or len(event_dates) != len(set(event_dates)):
        raise ValueError("longitudinal event_date values must be strictly increasing and unique")

    occupancy_report = structural.cash_occupancy_report
    occupancy_rows = tuple(structural.cash_occupancy_rows)
    assert_cash_occupancy_self_hash(occupancy_report)
    occupancy_structural = verify_layer_two_cash_occupancy_attribution_report(
        occupancy_report,
        rows=occupancy_rows,
    )
    if occupancy_structural.structural_ok is not True:
        raise ValueError("E10e-1 structural verifier structural_ok required")
    if (
        occupancy_structural.entry_execution_binding_ok
        or occupancy_structural.phase_binding_ok
        or occupancy_structural.tranche_evaluation_protocol_binding_ok
    ):
        raise ValueError("E10e-1 structural verifier must not claim disk bindings")
    if occupancy_structural.report_id != occupancy_report.report_id:
        raise ValueError("E10e-1 structural verifier report_id must equal occupancy report_id")
    if occupancy_report.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("E10e-1 tranche_evaluation_protocol_id mismatch")
    _assert_e10e1_inputs_within_declared_window(
        occupancy_report=occupancy_report,
        occupancy_rows=occupancy_rows,
    )
    occupancy_input_ids = list(occupancy_report.input_entry_execution_report_ids)
    occupancy_rows_by_id = {row.entry_execution_report_id: row for row in occupancy_report.rows}
    if set(occupancy_rows_by_id) != set(occupancy_input_ids):
        raise ValueError("E10e-1 row IDs must match input_entry_execution_report_ids")

    cash = float(BOUND_INITIAL_CASH)
    cum_entry = 0.0
    cum_exit = 0.0
    positions: dict[str, LongitudinalActivePosition] = {}
    occupied_tranches: set[int] = set()
    occupied_symbols: set[str] = set()
    opened_lifecycle_ids: set[str] = set()
    opened_entry_execution_ids: set[str] = set()
    exit_consumed_lifecycle_ids: set[str] = set()
    transitions: list[LongitudinalTransitionRow] = []
    closed_count = 0
    terminal_unknown_halt = False
    snapshot_id = occupancy_report.market_data_snapshot_id.strip()
    if not snapshot_id:
        raise ValueError("E10e-1 market_data_snapshot_id must be non-empty")
    phase_report_id = _require_sealed_hex64(
        occupancy_report.phase_report_id, field_name="cash_occupancy.phase_report_id"
    )
    occupancy_tol = float(occupancy_report.amount_abs_tol)

    for day in days:
        if terminal_unknown_halt:
            raise ValueError("terminal_unknown_halt forbids further longitudinal events")
        event_date = day.event_date
        if day.entry is not None and not isinstance(day.entry, LayerTwoLongitudinalEntryStructuralInput):
            raise ValueError("day.entry must be LayerTwoLongitudinalEntryStructuralInput or null")
        exits = tuple(day.exits)
        if len(exits) != len({id(item) for item in exits}):
            raise ValueError("duplicate exit input objects forbidden")

        # Same-day: entry judged on start-of-day cash/tranches, before exits.
        if day.entry is not None:
            entry_bundle = day.entry
            record = entry_bundle.lifecycle_record
            assert_lifecycle_current_state_matches_longitudinal_start_of_day(
                current_state=entry_bundle.lifecycle_structural.current_state,
                sod_cash=cash,
                sod_positions=positions,
                longitudinal_market_data_snapshot_id=snapshot_id,
            )
            assert_lifecycle_self_hash(record)
            verify_layer_two_hypothetical_position_lifecycle_record(
                record,
                structural=entry_bundle.lifecycle_structural,
            )
            _assert_lifecycle_protocol_bindings(record)
            if record.entry_trade_date != event_date:
                raise ValueError("entry event_date must equal lifecycle.entry_trade_date")
            record_id = _require_sealed_hex64(record.record_id, field_name="lifecycle_record_id")
            if record_id in opened_lifecycle_ids:
                raise ValueError("lifecycle_record_id may be opened only once")
            if record.market_data_snapshot_id != snapshot_id:
                raise ValueError("lifecycle market_data_snapshot_id must equal E10e-1/longitudinal snapshot")
            if record.phase_report_id != phase_report_id:
                raise ValueError("lifecycle phase_report_id must equal E10e-1/longitudinal phase_report_id")
            entry_execution_id = _require_sealed_hex64(
                record.entry_execution_report_id, field_name="lifecycle.entry_execution_report_id"
            )
            if entry_execution_id not in occupancy_rows_by_id:
                raise ValueError("lifecycle entry_execution_report_id missing from E10e-1 input IDs")
            if entry_execution_id in opened_entry_execution_ids:
                raise ValueError("lifecycle entry_execution_report_id may open only once")
            occ_row = occupancy_rows_by_id[entry_execution_id]
            if occ_row.execution_outcome != "hypothetically_fillable":
                raise ValueError(
                    f"lifecycle entry must map to E10e-1 hypothetically_fillable row (got {occ_row.execution_outcome})"
                )
            if occ_row.known_base_cash_used is None:
                raise ValueError("E10e-1 fillable row requires known_base_cash_used")
            if not _amounts_equal(
                float(occ_row.known_base_cash_used),
                float(record.entry_total_cash_used),
                abs_tol=max(_CASH_ABS_TOL, occupancy_tol),
            ):
                raise ValueError(
                    "E10e-1 known_base_cash_used must equal lifecycle entry_total_cash_used within declared tolerance"
                )
            if record.symbol in occupied_symbols:
                raise ValueError("symbol already occupies an active/deferred position")
            if int(record.tranche_id) in occupied_tranches:
                raise ValueError("tranche_id already occupied; same-day catch-up/reuse forbidden")
            used = float(record.entry_total_cash_used)
            if used < 0:
                raise ValueError("entry_total_cash_used must be non-negative")
            if cash + _CASH_ABS_TOL < used:
                raise ValueError("insufficient cash for entry (negative cash / financing forbidden)")
            before_cash = cash
            before_tranches = _sorted_tranches(occupied_tranches)
            cash = cash - used
            if cash < -_CASH_ABS_TOL:
                raise ValueError("cash must remain non-negative")
            cash = max(cash, 0.0)
            cum_entry += used
            occupied_tranches.add(int(record.tranche_id))
            occupied_symbols.add(record.symbol)
            opened_lifecycle_ids.add(record_id)
            opened_entry_execution_ids.add(entry_execution_id)
            positions[record_id] = LongitudinalActivePosition(
                lifecycle_record_id=record_id,
                symbol=record.symbol,
                tranche_id=int(record.tranche_id),
                cluster_id=record.cluster_id,
                shares=int(record.shares),
                entry_trade_date=record.entry_trade_date,
                stock_notional=float(record.stock_notional),
                buy_commission=float(record.buy_commission),
                entry_total_cash_used=used,
                status="open",
            )
            transitions.append(
                LongitudinalTransitionRow(
                    event_date=event_date,
                    transition_kind="entry_opened",
                    lifecycle_record_id=record_id,
                    symbol=record.symbol,
                    tranche_id=int(record.tranche_id),
                    shares=int(record.shares),
                    cash_before=before_cash,
                    cash_after=cash,
                    occupied_tranche_ids_before=before_tranches,
                    occupied_tranche_ids_after=_sorted_tranches(occupied_tranches),
                    entry_total_cash_used=used,
                    entry_execution_report_id=entry_execution_id,
                )
            )
            expected = float(BOUND_INITIAL_CASH) - cum_entry + cum_exit
            if not _amounts_equal(cash, expected):
                raise ValueError("cash-flow identity failed after entry")

        exit_lifecycle_ids_today: set[str] = set()
        exit_symbols_today: set[str] = set()
        exit_tranches_today: set[int] = set()
        for exit_bundle in exits:
            if terminal_unknown_halt:
                raise ValueError(
                    "terminal_unknown_halt forbids further longitudinal events "
                    "(unknown_exit_observation must be the final transition, including same-day exits)"
                )
            report = exit_bundle.exit_report
            assert_exit_report_self_hash(report)
            verify_layer_two_fixed_horizon_exit_diagnostic_report(
                report,
                structural=exit_bundle.exit_structural,
            )
            _assert_exit_protocol_bindings(report)
            exit_day = _exit_event_date(report)
            if exit_day != event_date:
                raise ValueError("exit event_date must equal exit diagnostic final observation/event date")
            if exit_day < report.entry_trade_date:
                raise ValueError("exit event date must not precede entry_trade_date")
            lifecycle_id = report.lifecycle_record_id
            if lifecycle_id in exit_lifecycle_ids_today:
                raise ValueError("duplicate lifecycle_record_id exits on the same day forbidden")
            if report.symbol in exit_symbols_today:
                raise ValueError("duplicate symbol exits on the same day forbidden")
            exit_lifecycle_ids_today.add(lifecycle_id)
            exit_symbols_today.add(report.symbol)
            if lifecycle_id not in positions:
                raise ValueError("exit requires a currently tracked open/deferred position")
            if lifecycle_id in exit_consumed_lifecycle_ids:
                raise ValueError("position already consumed by a prior exit diagnostic")
            position = positions[lifecycle_id]
            if position.status != "open":
                raise ValueError("only open positions may receive an exit diagnostic")
            if position.symbol != report.symbol:
                raise ValueError("exit symbol must equal open position symbol")
            if position.shares != report.shares:
                raise ValueError("exit shares must equal open position shares")
            if position.entry_trade_date != report.entry_trade_date:
                raise ValueError("exit entry_trade_date must equal open position entry_trade_date")
            if position.tranche_id in exit_tranches_today:
                raise ValueError("duplicate tranche exits on the same day forbidden")
            exit_tranches_today.add(position.tranche_id)
            if report.market_data_snapshot_id != snapshot_id:
                raise ValueError("exit market_data_snapshot_id must equal E10e-1/longitudinal snapshot")
            if report.phase_report_id != phase_report_id:
                raise ValueError("exit phase_report_id must equal E10e-1/longitudinal phase_report_id")

            before_cash = cash
            before_tranches = _sorted_tranches(occupied_tranches)
            exit_report_id = _require_sealed_hex64(report.report_id, field_name="exit_report_id")

            if report.final_outcome == "hypothetically_exitable":
                assert report.base_scenario is not None
                net = float(report.base_scenario.net_sale_cash)
                cash = cash + net
                cum_exit += net
                occupied_tranches.discard(position.tranche_id)
                occupied_symbols.discard(position.symbol)
                del positions[lifecycle_id]
                exit_consumed_lifecycle_ids.add(lifecycle_id)
                closed_count += 1
                transitions.append(
                    LongitudinalTransitionRow(
                        event_date=event_date,
                        transition_kind="exit_closed",
                        lifecycle_record_id=lifecycle_id,
                        symbol=position.symbol,
                        tranche_id=position.tranche_id,
                        shares=position.shares,
                        cash_before=before_cash,
                        cash_after=cash,
                        occupied_tranche_ids_before=before_tranches,
                        occupied_tranche_ids_after=_sorted_tranches(occupied_tranches),
                        exit_net_sale_cash=net,
                        exit_report_id=exit_report_id,
                        exit_final_outcome="hypothetically_exitable",
                    )
                )
            elif report.final_outcome == "still_open_after_observed_blocks":
                positions[lifecycle_id] = position.model_copy(update={"status": "deferred_still_open"})
                exit_consumed_lifecycle_ids.add(lifecycle_id)
                transitions.append(
                    LongitudinalTransitionRow(
                        event_date=event_date,
                        transition_kind="exit_deferred",
                        lifecycle_record_id=lifecycle_id,
                        symbol=position.symbol,
                        tranche_id=position.tranche_id,
                        shares=position.shares,
                        cash_before=before_cash,
                        cash_after=cash,
                        occupied_tranche_ids_before=before_tranches,
                        occupied_tranche_ids_after=_sorted_tranches(occupied_tranches),
                        exit_report_id=exit_report_id,
                        exit_final_outcome="still_open_after_observed_blocks",
                    )
                )
            elif report.final_outcome == "unknown_exit_observation":
                positions[lifecycle_id] = position.model_copy(update={"status": "unknown_halted"})
                exit_consumed_lifecycle_ids.add(lifecycle_id)
                terminal_unknown_halt = True
                transitions.append(
                    LongitudinalTransitionRow(
                        event_date=event_date,
                        transition_kind="exit_unknown_halt",
                        lifecycle_record_id=lifecycle_id,
                        symbol=position.symbol,
                        tranche_id=position.tranche_id,
                        shares=position.shares,
                        cash_before=before_cash,
                        cash_after=cash,
                        occupied_tranche_ids_before=before_tranches,
                        occupied_tranche_ids_after=_sorted_tranches(occupied_tranches),
                        exit_report_id=exit_report_id,
                        exit_final_outcome="unknown_exit_observation",
                    )
                )
            else:
                raise ValueError(f"unsupported exit final_outcome: {report.final_outcome!r}")

            expected = float(BOUND_INITIAL_CASH) - cum_entry + cum_exit
            if not _amounts_equal(cash, expected):
                raise ValueError("cash-flow identity failed after exit")

    if not transitions and not any(day.entry is not None or day.exits for day in days):
        raise ValueError("longitudinal run requires at least one entry or exit event")

    ending_positions = sorted(positions.values(), key=lambda p: (p.entry_trade_date.isoformat(), p.lifecycle_record_id))
    open_count = sum(1 for p in ending_positions if p.status == "open")
    deferred_count = sum(1 for p in ending_positions if p.status == "deferred_still_open")
    unknown_count = sum(1 for p in ending_positions if p.status == "unknown_halted")
    assembled = LayerTwoLongitudinalStateTransitionReport(
        start_date=event_dates[0],
        end_date=event_dates[-1],
        market_data_snapshot_id=snapshot_id,
        phase_report_id=phase_report_id,
        cash_occupancy_attribution_report_id=_require_sealed_hex64(
            occupancy_report.report_id, field_name="cash_occupancy_attribution_report_id"
        ),
        cash_occupancy_input_entry_execution_report_ids=list(occupancy_input_ids),
        cumulative_entry_total_cash_used=cum_entry,
        cumulative_base_exit_net_cash_received=cum_exit,
        ending_cash=cash,
        open_position_count=open_count,
        closed_position_count=closed_count,
        deferred_position_count=deferred_count,
        unknown_halt_position_count=unknown_count,
        terminal_unknown_halt=terminal_unknown_halt,
        transition_rows=transitions,
        ending_positions=list(ending_positions),
    )
    return seal_layer_two_longitudinal_state_transition_report(assembled)


def canonical_report_payload(report: LayerTwoLongitudinalStateTransitionReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: LayerTwoLongitudinalStateTransitionReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: LayerTwoLongitudinalStateTransitionReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_layer_two_longitudinal_state_transition_report(
    report: LayerTwoLongitudinalStateTransitionReport,
) -> LayerTwoLongitudinalStateTransitionReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: LayerTwoLongitudinalStateTransitionReport) -> None:
    if report.report_id is None:
        raise ValueError("longitudinal state transition report_id is missing")
    if report.report_id != compute_report_id(report):
        raise ValueError("longitudinal state transition report_id does not match canonical content hash")


def assert_matches_recomputed_longitudinal_report(
    report: LayerTwoLongitudinalStateTransitionReport,
    *,
    structural: LayerTwoLongitudinalStructuralInput,
) -> None:
    expected = diagnose_layer_two_longitudinal_state_transitions(structural=structural)
    if report.report_id != expected.report_id:
        raise ValueError("longitudinal report_id does not match full recompute")
    if canonical_report_payload(report) != canonical_report_payload(expected):
        raise ValueError("longitudinal canonical payload does not match full recompute")


def verify_layer_two_longitudinal_state_transition_report(
    report: LayerTwoLongitudinalStateTransitionReport,
    *,
    structural: LayerTwoLongitudinalStructuralInput,
) -> LayerTwoLongitudinalVerificationResult:
    """Structural verifier: self-hash + full recompute; all upstream bindings false."""
    assert_report_self_hash(report)
    assert_matches_recomputed_longitudinal_report(report, structural=structural)
    return LayerTwoLongitudinalVerificationResult(
        report_id=report.report_id or compute_report_id(report),
        structural_ok=True,
        lifecycle_bindings_ok=False,
        exit_bindings_ok=False,
        allocation_protocol_binding_ok=False,
        cash_occupancy_attribution_binding_ok=False,
        ready_for_longitudinal_diagnostic=False,
    )


def _structural_from_file(file_input: LayerTwoLongitudinalFileInput) -> LayerTwoLongitudinalStructuralInput:
    days: list[LayerTwoLongitudinalDayStructuralInput] = []
    for day in file_input.days:
        entry = None if day.entry is None else day.entry.structural
        exits = tuple(item.structural for item in day.exits)
        days.append(
            LayerTwoLongitudinalDayStructuralInput(
                event_date=day.event_date,
                entry=entry,
                exits=exits,
            )
        )
    return LayerTwoLongitudinalStructuralInput(
        days=tuple(days),
        cash_occupancy_report=file_input.cash_occupancy_report,
        cash_occupancy_rows=tuple(item.structural for item in file_input.cash_occupancy_file_rows),
    )


def _require_e10f1_file_ready(result: Any, *, record_id: str) -> None:
    if (
        result.structural_ok is not True
        or result.entry_execution_binding_ok is not True
        or result.allocator_binding_ok is not True
        or result.phase_binding_ok is not True
        or result.tranche_evaluation_protocol_binding_ok is not True
        or result.ready_for_lifecycle_diagnostic is not True
    ):
        raise ValueError("E10f-1 file verifier six-field ready required for longitudinal entry")
    if result.record_id != record_id:
        raise ValueError("E10f-1 file verifier record_id must equal lifecycle_record_id")


def _require_e10f2_file_ready(result: Any, *, report_id: str) -> None:
    if (
        result.structural_ok is not True
        or result.lifecycle_binding_ok is not True
        or result.stamp_tax_binding_ok is not True
        or result.tranche_evaluation_protocol_binding_ok is not True
        or result.exit_observation_binding_ok is not True
        or result.ready_for_exit_diagnostic is not True
    ):
        raise ValueError("E10f-2 file verifier full bindings/ready required for longitudinal exit")
    if result.report_id != report_id:
        raise ValueError("E10f-2 file verifier report_id must equal exit report_id")


def verify_layer_two_longitudinal_state_transition_report_file(
    *,
    report: LayerTwoLongitudinalStateTransitionReport,
    file_input: LayerTwoLongitudinalFileInput,
) -> LayerTwoLongitudinalVerificationResult:
    """File verifier: structural path + real E10f-1/E10f-2/E10e-1/allocation protocol file verifiers."""
    top_root = Path(file_input.repo_root).resolve()
    for day in file_input.days:
        if day.entry is not None:
            life_root = Path(day.entry.lifecycle_file.file_bindings.repo_root).resolve()
            if life_root != top_root:
                raise ValueError("E10f-1 lifecycle file_bindings.repo_root must resolve to file_input.repo_root")
        for exit_item in day.exits:
            nested_life_root = Path(exit_item.exit_file.lifecycle_file.file_bindings.repo_root).resolve()
            stamp_root = Path(exit_item.exit_file.stamp_tax_repo_root).resolve()
            if nested_life_root != top_root or stamp_root != top_root:
                raise ValueError("E10f-2 nested lifecycle/stamp-tax repo_root must resolve to file_input.repo_root")
    for occ_row in file_input.cash_occupancy_file_rows:
        occ_root = Path(occ_row.file_bindings.repo_root).resolve()
        if occ_root != top_root:
            raise ValueError("E10e-1 cash occupancy file row repo_root must resolve to file_input.repo_root")

    structural = _structural_from_file(file_input)
    structural_result = verify_layer_two_longitudinal_state_transition_report(report, structural=structural)
    if structural_result.structural_ok is not True:
        raise ValueError("structural verifier must succeed before file binding")
    if (
        structural_result.lifecycle_bindings_ok
        or structural_result.exit_bindings_ok
        or structural_result.allocation_protocol_binding_ok
        or structural_result.cash_occupancy_attribution_binding_ok
        or structural_result.ready_for_longitudinal_diagnostic
    ):
        raise ValueError("structural verifier must not claim disk binding or longitudinal readiness")

    e10e1 = verify_layer_two_cash_occupancy_attribution_report_file(
        report=file_input.cash_occupancy_report,
        rows=file_input.cash_occupancy_file_rows,
    )
    if (
        e10e1.structural_ok is not True
        or e10e1.entry_execution_binding_ok is not True
        or e10e1.phase_binding_ok is not True
        or e10e1.tranche_evaluation_protocol_binding_ok is not True
    ):
        raise ValueError("E10e-1 file verifier structural_ok/entry_execution/phase/tranche bindings required")
    if e10e1.report_id != report.cash_occupancy_attribution_report_id:
        raise ValueError("E10e-1 file verifier report_id must equal sealed longitudinal occupancy report_id")
    if e10e1.report_id != file_input.cash_occupancy_report.report_id:
        raise ValueError("E10e-1 file verifier report_id must equal occupancy report_id")

    saw_entry = False
    for day in file_input.days:
        if day.entry is not None:
            saw_entry = True
            record = day.entry.structural.lifecycle_record
            record_id = _require_sealed_hex64(record.record_id, field_name="lifecycle_record_id")
            e10f1 = verify_layer_two_hypothetical_position_lifecycle_record_file(
                record=record,
                file_input=day.entry.lifecycle_file,
            )
            _require_e10f1_file_ready(e10f1, record_id=record_id)
        for exit_item in day.exits:
            exit_report = exit_item.structural.exit_report
            exit_id = _require_sealed_hex64(exit_report.report_id, field_name="exit_report_id")
            e10f2 = verify_layer_two_fixed_horizon_exit_diagnostic_report_file(
                report=exit_report,
                file_input=exit_item.exit_file,
            )
            _require_e10f2_file_ready(e10f2, report_id=exit_id)

    if not saw_entry and not any(day.exits for day in file_input.days):
        raise ValueError("file path requires at least one entry or exit bundle")

    # No exits is allowed for open-only runs; exit_bindings_ok still true when none present.
    root = top_root
    protocol_path = root / BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH
    if str(DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH) != BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH:
        raise ValueError("allocation protocol default path drifted")
    alloc_doc, alloc_result = verify_layer_two_allocation_protocol_file(
        protocol_path=protocol_path,
        repo_root=root,
    )
    if alloc_result.structural_ok is not True:
        raise ValueError("allocation protocol file verifier structural_ok required")
    if (
        alloc_result.two_layer_decision_contract_binding_ok is not True
        or alloc_result.layer_one_index_protocol_binding_ok is not True
        or alloc_result.tranche_evaluation_protocol_binding_ok is not True
    ):
        raise ValueError("allocation protocol upstream disk bindings required")
    if int(alloc_doc.capital_budget.initial_cash) != BOUND_INITIAL_CASH:
        raise ValueError("allocation protocol initial_cash must equal 80000")
    if alloc_doc.protocol_id != BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID:
        raise ValueError("allocation protocol_id on disk must equal bound constant")
    if alloc_doc.protocol_id != report.allocation_implementation_protocol_id:
        raise ValueError("allocation protocol_id on disk must equal report binding")
    if alloc_result.protocol_id != report.allocation_implementation_protocol_id:
        raise ValueError("allocation verification protocol_id must equal report binding")

    return LayerTwoLongitudinalVerificationResult(
        report_id=report.report_id or compute_report_id(report),
        structural_ok=True,
        lifecycle_bindings_ok=True,
        exit_bindings_ok=True,
        allocation_protocol_binding_ok=True,
        cash_occupancy_attribution_binding_ok=True,
        ready_for_longitudinal_diagnostic=True,
    )


def load_layer_two_longitudinal_state_transition_report(
    path: Path,
) -> LayerTwoLongitudinalStateTransitionReport:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("longitudinal state transition report is missing or invalid") from exc
    try:
        return LayerTwoLongitudinalStateTransitionReport.model_validate(payload)
    except Exception as exc:
        raise ValueError("longitudinal state transition report is missing or invalid") from exc


def write_layer_two_longitudinal_state_transition_report(
    path: Path,
    report: LayerTwoLongitudinalStateTransitionReport,
) -> LayerTwoLongitudinalStateTransitionReport:
    sealed = seal_layer_two_longitudinal_state_transition_report(report)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID",
    "BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH",
    "BOUND_INITIAL_CASH",
    "LAYER_TWO_LONGITUDINAL_ENGINE_VERSION",
    "LAYER_TWO_LONGITUDINAL_SCHEMA_VERSION",
    "LayerTwoLongitudinalDayFileInput",
    "LayerTwoLongitudinalDayStructuralInput",
    "LayerTwoLongitudinalEntryFileInput",
    "LayerTwoLongitudinalEntryStructuralInput",
    "LayerTwoLongitudinalExitFileInput",
    "LayerTwoLongitudinalExitStructuralInput",
    "LayerTwoLongitudinalFileInput",
    "LayerTwoLongitudinalStateTransitionReport",
    "LayerTwoLongitudinalStructuralInput",
    "LayerTwoLongitudinalVerificationResult",
    "LongitudinalActivePosition",
    "LongitudinalTransitionRow",
    "assert_lifecycle_current_state_matches_longitudinal_start_of_day",
    "assert_matches_recomputed_longitudinal_report",
    "assert_report_self_hash",
    "canonical_report_bytes",
    "canonical_report_payload",
    "compute_report_id",
    "diagnose_layer_two_longitudinal_state_transitions",
    "load_layer_two_longitudinal_state_transition_report",
    "seal_layer_two_longitudinal_state_transition_report",
    "verify_layer_two_longitudinal_state_transition_report",
    "verify_layer_two_longitudinal_state_transition_report_file",
    "write_layer_two_longitudinal_state_transition_report",
]
