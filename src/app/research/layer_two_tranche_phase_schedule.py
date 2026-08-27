"""Layer-two 40-day tranche phase schedule planner (E10d-1).

Read-only staggered phase opportunities on a frozen market calendar.
Uses allocation-protocol ``plan_base_slots`` for active tranche count and binds
the sealed tranche-evaluation + allocation-implementation protocols.

Does not select stocks, prices, returns, PnL, orders, or trades. Scheduled
opportunities are calendar/phase matches only — not evidence of candidates,
fills, or executable orders.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_allocation_protocol import (
    BOUND_TRANCHE_EVALUATION_PROTOCOL_ID as ALLOCATION_BOUND_TRANCHE_ID,
)
from app.research.layer_two_allocation_protocol import (
    BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH as ALLOCATION_BOUND_TRANCHE_PATH,
)
from app.research.layer_two_allocation_protocol import (
    DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH,
    _require_non_bool_int,
    _require_real_number,
    plan_base_slots,
    verify_layer_two_allocation_protocol_file,
)
from app.research.tranche_evaluation_protocol import (
    DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH,
    verify_tranche_evaluation_protocol_draft_file,
)
from app.research.two_layer_contract import CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS

LAYER_TWO_TRANCHE_PHASE_SCHEDULE_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_TRANCHE_PHASE_SCHEDULE_DIAGNOSTIC_VERSION: Literal["layer-two-tranche-phase-schedule-v1"] = (
    "layer-two-tranche-phase-schedule-v1"
)

HOLDING_CYCLE_MARKET_TRADING_DAYS: Literal[40] = CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS

BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH: Literal["config/research/tranche-evaluation-protocol-draft-v1.json"] = (
    "config/research/tranche-evaluation-protocol-draft-v1.json"
)
BOUND_TRANCHE_EVALUATION_PROTOCOL_ID: Literal["8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a"] = (
    "8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a"
)
BOUND_ALLOCATION_PROTOCOL_PATH: Literal["config/research/layer-two-allocation-implementation-protocol-v1.json"] = (
    "config/research/layer-two-allocation-implementation-protocol-v1.json"
)
BOUND_ALLOCATION_PROTOCOL_ID: Literal["0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"] = (
    "0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"
)

SELECTED_OPERATIONAL_FAMILY_SHIFT: Literal[0] = 0
PHASE_FAMILY_SIZE: Literal[40] = 40

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduledOpportunity(_StrictModel):
    """A phase-matched decision opportunity; not a fill or order."""

    tranche_id: int = Field(ge=0)
    decision_date: date
    absolute_calendar_index: int = Field(ge=0)
    phase_offset: int = Field(ge=0, le=39)
    family_shift: int = Field(ge=0, le=39)

    @field_validator("tranche_id", "absolute_calendar_index", "phase_offset", "family_shift", mode="before")
    @classmethod
    def _reject_bool_ints(cls, value: object, info: Any) -> int:
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=0)

    @field_validator("decision_date", mode="before")
    @classmethod
    def _require_date(cls, value: object) -> date:
        if type(value) is date:
            return value
        if isinstance(value, str) and value.strip():
            return date.fromisoformat(value.strip())
        raise ValueError("decision_date must be a datetime.date")


class PhaseFamilyMember(_StrictModel):
    family_shift: int = Field(ge=0, le=39)
    tranche_phase_offsets: list[int]
    opportunity_count: int = Field(ge=0)
    opportunities: list[ScheduledOpportunity]
    is_selected_operational_schedule: bool

    @field_validator("family_shift", "opportunity_count", mode="before")
    @classmethod
    def _reject_bool_ints(cls, value: object, info: Any) -> int:
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=0)

    @field_validator("tranche_phase_offsets", mode="before")
    @classmethod
    def _validate_offsets(cls, value: object) -> list[int]:
        if not isinstance(value, list):
            raise ValueError("tranche_phase_offsets must be a list")
        cleaned: list[int] = []
        for item in value:
            cleaned.append(_require_non_bool_int(item, field_name="tranche_phase_offsets", minimum=0))
            if cleaned[-1] > 39:
                raise ValueError("tranche_phase_offsets entries must be in [0, 39]")
        return cleaned

    @model_validator(mode="after")
    def _counts(self) -> PhaseFamilyMember:
        if self.opportunity_count != len(self.opportunities):
            raise ValueError("opportunity_count must equal opportunities length")
        for row in self.opportunities:
            if row.family_shift != self.family_shift:
                raise ValueError("opportunity family_shift must match member family_shift")
        if self.is_selected_operational_schedule and self.family_shift != SELECTED_OPERATIONAL_FAMILY_SHIFT:
            raise ValueError("only family_shift=0 may be the selected operational schedule")
        if (not self.is_selected_operational_schedule) and self.family_shift == SELECTED_OPERATIONAL_FAMILY_SHIFT:
            raise ValueError("family_shift=0 must be marked as selected operational schedule")
        return self


class BaseSlotSummary(_StrictModel):
    current_account_equity: float
    risk_budget: float
    sleeve_budget: float
    budget_slot_cap: int
    base_slot_count: int
    base_slot_notional: float | None
    cash_retention_reason: str | None = None

    @field_validator("current_account_equity", "risk_budget", "sleeve_budget", mode="before")
    @classmethod
    def _reject_bool_nonneg(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("base_slot_notional", mode="before")
    @classmethod
    def _reject_bool_optional(cls, value: object) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name="base_slot_notional", minimum=0.0)

    @field_validator("budget_slot_cap", "base_slot_count", mode="before")
    @classmethod
    def _reject_bool_int(cls, value: object, info: Any) -> int:
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=0)


class LayerTwoTranchePhaseScheduleReport(_StrictModel):
    schema_version: Literal["1"] = LAYER_TWO_TRANCHE_PHASE_SCHEDULE_SCHEMA_VERSION
    diagnostic_version: Literal["layer-two-tranche-phase-schedule-v1"] = (
        LAYER_TWO_TRANCHE_PHASE_SCHEDULE_DIAGNOSTIC_VERSION
    )
    market_calendar: list[date]
    calendar_sha256: str = Field(pattern=_HEX64.pattern)
    start: date
    end: date
    anchor: date
    current_account_equity: float
    risk_budget: float
    market_data_snapshot_id: str = Field(pattern=_SNAPSHOT_PATTERN.pattern)
    tranche_evaluation_protocol_id: Literal["8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_ID
    )
    tranche_evaluation_protocol_path: Literal["config/research/tranche-evaluation-protocol-draft-v1.json"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
    )
    allocation_implementation_protocol_id: Literal[
        "0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"
    ] = BOUND_ALLOCATION_PROTOCOL_ID
    allocation_implementation_protocol_path: Literal[
        "config/research/layer-two-allocation-implementation-protocol-v1.json"
    ] = BOUND_ALLOCATION_PROTOCOL_PATH
    holding_cycle_market_trading_days: Literal[40] = HOLDING_CYCLE_MARKET_TRADING_DAYS
    holding_cycle_is_not_active_tranche_count: Literal[True] = True
    base_slot: BaseSlotSummary
    active_tranche_count: int = Field(ge=0)
    baseline_phase_offsets: list[int]
    selected_operational_family_shift: Literal[0] = SELECTED_OPERATIONAL_FAMILY_SHIFT
    selected_schedule: PhaseFamilyMember
    phase_family: list[PhaseFamilyMember]
    one_stock_per_tranche_semantics: Literal[True] = True
    does_not_select_stocks: Literal[True] = True
    gradual_build_required: Literal[True] = True
    same_day_catchup_fill_forbidden: Literal[True] = True
    risk_reduce_not_phase_limited: Literal[True] = True
    risk_reduce_does_not_emit_orders_in_this_module: Literal[True] = True
    cash_retention_reason: str | None = None
    diagnostic_only: Literal[True] = True
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
    never_select_phase_by_return: Literal[True] = True
    report_id: str | None = Field(default=None, pattern=_HEX64.pattern)

    @field_validator("current_account_equity", "risk_budget", mode="before")
    @classmethod
    def _reject_bool_nonneg(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("active_tranche_count", mode="before")
    @classmethod
    def _reject_bool_count(cls, value: object) -> int:
        return _require_non_bool_int(value, field_name="active_tranche_count", minimum=0)

    @field_validator("market_data_snapshot_id", "calendar_sha256", mode="before")
    @classmethod
    def _reject_blank_hash(cls, value: object, info: Any) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(f"{info.field_name} must be a non-empty hex digest")
        return value.strip()

    @field_validator(
        "tranche_evaluation_protocol_path",
        "allocation_implementation_protocol_path",
        mode="before",
    )
    @classmethod
    def _reject_path_escape(cls, value: object, info: Any) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(f"{info.field_name} must be a non-empty relative path")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"{info.field_name} must be repo-relative without parent traversal")
        expected = (
            BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
            if info.field_name == "tranche_evaluation_protocol_path"
            else BOUND_ALLOCATION_PROTOCOL_PATH
        )
        if value != expected:
            raise ValueError(f"{info.field_name} does not match bound path")
        return value

    @model_validator(mode="after")
    def _gate(self) -> LayerTwoTranchePhaseScheduleReport:
        if self.ready_for_scoring or self.ready_for_backtest or self.ready_for_portfolio_construction:
            raise ValueError("ready flags must remain false")
        if self.ready_for_orders or self.ready_for_trading or self.auto_apply:
            raise ValueError("orders/trading/auto_apply must remain false")
        if not self.diagnostic_only:
            raise ValueError("diagnostic_only must remain true")
        if self.holding_cycle_market_trading_days != HOLDING_CYCLE_MARKET_TRADING_DAYS:
            raise ValueError("holding_cycle_market_trading_days must equal 40")
        if self.active_tranche_count == self.holding_cycle_market_trading_days:
            raise ValueError("active_tranche_count must not equal 40; 40 is the holding/phase cycle")
        if self.active_tranche_count != self.base_slot.base_slot_count:
            raise ValueError("active_tranche_count must equal base_slot.base_slot_count")
        if self.baseline_phase_offsets != compute_baseline_phase_offsets(self.active_tranche_count):
            raise ValueError("baseline_phase_offsets must match floor(k*40/N) formula")
        if len(self.phase_family) != PHASE_FAMILY_SIZE:
            raise ValueError("phase_family must contain exactly 40 members (family_shift 0..39)")
        for expected_shift, member in enumerate(self.phase_family):
            if member.family_shift != expected_shift:
                raise ValueError("phase_family must be ordered by family_shift 0..39")
        if self.selected_schedule.family_shift != SELECTED_OPERATIONAL_FAMILY_SHIFT:
            raise ValueError("selected_schedule must use family_shift=0")
        if self.selected_schedule != self.phase_family[0]:
            raise ValueError("selected_schedule must equal phase_family[0]")
        if self.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
            raise ValueError("tranche_evaluation_protocol_id mismatch")
        if self.allocation_implementation_protocol_id != BOUND_ALLOCATION_PROTOCOL_ID:
            raise ValueError("allocation_implementation_protocol_id mismatch")
        if compute_calendar_sha256(self.market_calendar) != self.calendar_sha256:
            raise ValueError("calendar_sha256 does not match market_calendar")
        return self


class LayerTwoTranchePhaseScheduleVerificationResult(_StrictModel):
    report_id: str
    structural_ok: bool
    tranche_evaluation_protocol_binding_ok: bool = False
    allocation_implementation_protocol_binding_ok: bool = False
    diagnostic_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_select_stocks: Literal[True] = True
    does_not_score: Literal[True] = True
    does_not_backtest: Literal[True] = True
    does_not_compute_orders: Literal[True] = True
    does_not_trade: Literal[True] = True


def compute_calendar_sha256(market_calendar: Sequence[date]) -> str:
    payload = [d.isoformat() for d in market_calendar]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_phase_schedule_calendar(
    market_calendar: Sequence[date],
    *,
    start: date,
    end: date,
    anchor: date,
) -> list[date]:
    if type(start) is not date or type(end) is not date or type(anchor) is not date:
        raise ValueError("anchor/start/end must be datetime.date values")
    if not isinstance(market_calendar, Sequence) or isinstance(market_calendar, (str, bytes)):
        raise ValueError("market_calendar must be a sequence of dates")
    if len(market_calendar) == 0:
        raise ValueError("market_calendar must be non-empty")
    cleaned: list[date] = []
    for index, value in enumerate(market_calendar):
        if type(value) is not date:
            raise ValueError(f"market_calendar[{index}] must be a datetime.date")
        cleaned.append(value)
    for previous, current in zip(cleaned, cleaned[1:], strict=False):
        if current == previous:
            raise ValueError("market_calendar must not contain duplicate dates")
        if current < previous:
            raise ValueError("market_calendar must be strictly increasing")
    if anchor not in cleaned:
        raise ValueError(f"anchor {anchor.isoformat()} is not in market_calendar")
    if start not in cleaned:
        raise ValueError(f"start {start.isoformat()} is not in market_calendar")
    if end not in cleaned:
        raise ValueError(f"end {end.isoformat()} is not in market_calendar")
    if anchor > start:
        raise ValueError("anchor must be on or before start")
    if start > end:
        raise ValueError("start must be on or before end")
    return cleaned


def compute_baseline_phase_offsets(active_tranche_count: object) -> list[int]:
    """Uniform stagger offsets: floor(k * 40 / N) for k=0..N-1."""
    n = _require_non_bool_int(active_tranche_count, field_name="active_tranche_count", minimum=0)
    if n == 0:
        return []
    if n > HOLDING_CYCLE_MARKET_TRADING_DAYS:
        raise ValueError("active_tranche_count must be <= holding cycle length 40")
    offsets = [int(math.floor(k * HOLDING_CYCLE_MARKET_TRADING_DAYS / n)) for k in range(n)]
    if sorted(offsets) != offsets:
        raise ValueError("baseline phase offsets must be sorted")
    if len(set(offsets)) != len(offsets):
        raise ValueError("baseline phase offsets must be unique")
    if offsets[0] < 0 or offsets[-1] > HOLDING_CYCLE_MARKET_TRADING_DAYS - 1:
        raise ValueError("baseline phase offsets must lie in [0, 39]")
    # Circular adjacent spacings differ by at most 1.
    spacings = [offsets[i + 1] - offsets[i] for i in range(len(offsets) - 1)]
    spacings.append(HOLDING_CYCLE_MARKET_TRADING_DAYS - offsets[-1] + offsets[0])
    if max(spacings) - min(spacings) > 1:
        raise ValueError("baseline phase offset circular spacings must differ by at most 1")
    return offsets


def shifted_tranche_phase_offsets(*, baseline_offsets: Sequence[int], family_shift: int) -> list[int]:
    shift = _require_non_bool_int(family_shift, field_name="family_shift", minimum=0)
    if shift > 39:
        raise ValueError("family_shift must be in [0, 39]")
    return [
        (_require_non_bool_int(offset, field_name="baseline_offset", minimum=0) + shift)
        % HOLDING_CYCLE_MARKET_TRADING_DAYS
        for offset in baseline_offsets
    ]


def _build_opportunities_for_shift(
    *,
    calendar: Sequence[date],
    start_index: int,
    end_index: int,
    anchor_index: int,
    tranche_offsets: Sequence[int],
    family_shift: int,
) -> list[ScheduledOpportunity]:
    opportunities: list[ScheduledOpportunity] = []
    offset_to_tranches: dict[int, list[int]] = {}
    for tranche_id, offset in enumerate(tranche_offsets):
        offset_to_tranches.setdefault(int(offset), []).append(tranche_id)
    for absolute_index in range(start_index, end_index + 1):
        phase = (absolute_index - anchor_index) % HOLDING_CYCLE_MARKET_TRADING_DAYS
        tranche_ids = offset_to_tranches.get(phase)
        if not tranche_ids:
            continue
        # Unique offsets guarantee one tranche; still fail closed if violated.
        if len(tranche_ids) != 1:
            raise ValueError("same-day multi-tranche catch-up is forbidden under unique phase offsets")
        tranche_id = tranche_ids[0]
        opportunities.append(
            ScheduledOpportunity(
                tranche_id=tranche_id,
                decision_date=calendar[absolute_index],
                absolute_calendar_index=absolute_index,
                phase_offset=phase,
                family_shift=family_shift,
            )
        )
    # Per-tranche opportunities must be exactly 40 market days apart when both present.
    by_tranche: dict[int, list[ScheduledOpportunity]] = {}
    for row in opportunities:
        by_tranche.setdefault(row.tranche_id, []).append(row)
    for tranche_id, rows in by_tranche.items():
        rows_sorted = sorted(rows, key=lambda item: item.absolute_calendar_index)
        for previous, current in zip(rows_sorted, rows_sorted[1:], strict=False):
            gap = current.absolute_calendar_index - previous.absolute_calendar_index
            if gap != HOLDING_CYCLE_MARKET_TRADING_DAYS:
                raise ValueError(
                    f"tranche_id={tranche_id} opportunity gap {gap} != "
                    f"{HOLDING_CYCLE_MARKET_TRADING_DAYS} market trading days"
                )
    return opportunities


def plan_layer_two_tranche_phase_schedule(
    *,
    market_calendar: Sequence[date],
    start: date,
    end: date,
    anchor: date,
    current_account_equity: object,
    risk_budget: object,
    market_data_snapshot_id: str,
) -> LayerTwoTranchePhaseScheduleReport:
    """Build a sealed read-only phase schedule diagnostic.

    All arguments are required. Does not select stocks or emit orders.
    """
    if not isinstance(market_data_snapshot_id, str) or not _SNAPSHOT_PATTERN.fullmatch(market_data_snapshot_id):
        raise ValueError("market_data_snapshot_id must be a 64-char lowercase hex digest")
    calendar = validate_phase_schedule_calendar(
        market_calendar,
        start=start,
        end=end,
        anchor=anchor,
    )
    equity = _require_real_number(current_account_equity, field_name="current_account_equity", minimum=0.0)
    budget = _require_real_number(risk_budget, field_name="risk_budget", minimum=0.0)
    slot_plan = plan_base_slots(current_account_equity=equity, risk_budget=budget)
    active_n = slot_plan.base_slot_count
    baseline = compute_baseline_phase_offsets(active_n)

    anchor_index = calendar.index(anchor)
    start_index = calendar.index(start)
    end_index = calendar.index(end)

    phase_family: list[PhaseFamilyMember] = []
    for family_shift in range(PHASE_FAMILY_SIZE):
        offsets = shifted_tranche_phase_offsets(baseline_offsets=baseline, family_shift=family_shift)
        opportunities = _build_opportunities_for_shift(
            calendar=calendar,
            start_index=start_index,
            end_index=end_index,
            anchor_index=anchor_index,
            tranche_offsets=offsets,
            family_shift=family_shift,
        )
        phase_family.append(
            PhaseFamilyMember(
                family_shift=family_shift,
                tranche_phase_offsets=list(offsets),
                opportunity_count=len(opportunities),
                opportunities=opportunities,
                is_selected_operational_schedule=(family_shift == SELECTED_OPERATIONAL_FAMILY_SHIFT),
            )
        )

    report = LayerTwoTranchePhaseScheduleReport(
        market_calendar=list(calendar),
        calendar_sha256=compute_calendar_sha256(calendar),
        start=start,
        end=end,
        anchor=anchor,
        current_account_equity=equity,
        risk_budget=budget,
        market_data_snapshot_id=market_data_snapshot_id,
        base_slot=BaseSlotSummary(
            current_account_equity=slot_plan.current_account_equity,
            risk_budget=slot_plan.risk_budget,
            sleeve_budget=slot_plan.sleeve_budget,
            budget_slot_cap=slot_plan.budget_slot_cap,
            base_slot_count=slot_plan.base_slot_count,
            base_slot_notional=slot_plan.base_slot_notional,
            cash_retention_reason=slot_plan.cash_retention_reason,
        ),
        active_tranche_count=active_n,
        baseline_phase_offsets=baseline,
        selected_schedule=phase_family[0],
        phase_family=phase_family,
        cash_retention_reason=slot_plan.cash_retention_reason,
    )
    return seal_layer_two_tranche_phase_schedule_report(report)


def canonical_report_payload(report: LayerTwoTranchePhaseScheduleReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: LayerTwoTranchePhaseScheduleReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: LayerTwoTranchePhaseScheduleReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_layer_two_tranche_phase_schedule_report(
    report: LayerTwoTranchePhaseScheduleReport,
) -> LayerTwoTranchePhaseScheduleReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: LayerTwoTranchePhaseScheduleReport) -> None:
    if report.report_id is None:
        raise ValueError("layer-two tranche phase schedule report_id is missing")
    expected = compute_report_id(report)
    if report.report_id != expected:
        raise ValueError("layer-two tranche phase schedule report_id does not match canonical content hash")


def assert_matches_recomputed_schedule(report: LayerTwoTranchePhaseScheduleReport) -> None:
    expected = plan_layer_two_tranche_phase_schedule(
        market_calendar=report.market_calendar,
        start=report.start,
        end=report.end,
        anchor=report.anchor,
        current_account_equity=report.current_account_equity,
        risk_budget=report.risk_budget,
        market_data_snapshot_id=report.market_data_snapshot_id,
    )
    if report.report_id != expected.report_id:
        raise ValueError("layer-two tranche phase schedule report_id does not match full recompute")
    if canonical_report_payload(report) != canonical_report_payload(expected):
        raise ValueError("layer-two tranche phase schedule canonical payload does not match full recompute")


def verify_layer_two_tranche_phase_schedule_report(
    report: LayerTwoTranchePhaseScheduleReport,
) -> LayerTwoTranchePhaseScheduleVerificationResult:
    """Structural verifier: self-hash + full recompute. Does not claim disk bindings."""
    assert_report_self_hash(report)
    assert_matches_recomputed_schedule(report)
    if report.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("tranche protocol id constant drift")
    if report.allocation_implementation_protocol_id != BOUND_ALLOCATION_PROTOCOL_ID:
        raise ValueError("allocation protocol id constant drift")
    # Guard against module-constant / import drift vs allocation binding.
    if ALLOCATION_BOUND_TRANCHE_ID != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("allocation module tranche binding id drifted")
    if ALLOCATION_BOUND_TRANCHE_PATH != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
        raise ValueError("allocation module tranche binding path drifted")
    return LayerTwoTranchePhaseScheduleVerificationResult(
        report_id=report.report_id or compute_report_id(report),
        structural_ok=True,
        tranche_evaluation_protocol_binding_ok=False,
        allocation_implementation_protocol_binding_ok=False,
    )


def load_layer_two_tranche_phase_schedule_report(path: Path) -> LayerTwoTranchePhaseScheduleReport:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("layer-two tranche phase schedule report is missing or invalid") from exc
    try:
        return LayerTwoTranchePhaseScheduleReport.model_validate(payload)
    except Exception as exc:
        raise ValueError("layer-two tranche phase schedule report is missing or invalid") from exc


def write_layer_two_tranche_phase_schedule_report(
    path: Path,
    report: LayerTwoTranchePhaseScheduleReport,
) -> LayerTwoTranchePhaseScheduleReport:
    sealed = seal_layer_two_tranche_phase_schedule_report(report)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


def verify_layer_two_tranche_phase_schedule_report_file(
    *,
    report_path: Path,
    repo_root: Path,
) -> tuple[LayerTwoTranchePhaseScheduleReport, LayerTwoTranchePhaseScheduleVerificationResult]:
    """Disk-bound verifier: structural recompute + upstream protocol file verifies."""
    root = Path(repo_root).resolve()
    report = load_layer_two_tranche_phase_schedule_report(report_path)
    structural = verify_layer_two_tranche_phase_schedule_report(report)

    tranche_rel = report.tranche_evaluation_protocol_path
    allocation_rel = report.allocation_implementation_protocol_path
    for rel, expected in (
        (tranche_rel, BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH),
        (allocation_rel, BOUND_ALLOCATION_PROTOCOL_PATH),
    ):
        path = Path(rel)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("upstream protocol path must be repo-relative without parent traversal")
        if rel != expected:
            raise ValueError("upstream protocol path does not match bound constant")
        resolved = (root / path).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("upstream protocol path escapes repository root") from exc

    tranche_path = root / BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
    _tranche_doc, tranche_result = verify_tranche_evaluation_protocol_draft_file(
        protocol_path=tranche_path,
        repo_root=root,
    )
    if tranche_result.protocol_id != report.tranche_evaluation_protocol_id:
        raise ValueError("tranche protocol_id on disk does not match report binding")
    if tranche_result.protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("tranche protocol_id on disk does not match bound constant")
    if str(DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH) != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
        raise ValueError("tranche protocol default path drifted")

    allocation_path = root / BOUND_ALLOCATION_PROTOCOL_PATH
    _allocation_doc, allocation_result = verify_layer_two_allocation_protocol_file(
        protocol_path=allocation_path,
        repo_root=root,
    )
    if allocation_result.protocol_id != report.allocation_implementation_protocol_id:
        raise ValueError("allocation protocol_id on disk does not match report binding")
    if allocation_result.protocol_id != BOUND_ALLOCATION_PROTOCOL_ID:
        raise ValueError("allocation protocol_id on disk does not match bound constant")
    if str(DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH) != BOUND_ALLOCATION_PROTOCOL_PATH:
        raise ValueError("allocation protocol default path drifted")

    result = structural.model_copy(
        update={
            "tranche_evaluation_protocol_binding_ok": True,
            "allocation_implementation_protocol_binding_ok": True,
        }
    )
    return report, result


__all__ = [
    "BOUND_ALLOCATION_PROTOCOL_ID",
    "BOUND_ALLOCATION_PROTOCOL_PATH",
    "BOUND_TRANCHE_EVALUATION_PROTOCOL_ID",
    "BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH",
    "HOLDING_CYCLE_MARKET_TRADING_DAYS",
    "LAYER_TWO_TRANCHE_PHASE_SCHEDULE_DIAGNOSTIC_VERSION",
    "LAYER_TWO_TRANCHE_PHASE_SCHEDULE_SCHEMA_VERSION",
    "PHASE_FAMILY_SIZE",
    "SELECTED_OPERATIONAL_FAMILY_SHIFT",
    "BaseSlotSummary",
    "LayerTwoTranchePhaseScheduleReport",
    "LayerTwoTranchePhaseScheduleVerificationResult",
    "PhaseFamilyMember",
    "ScheduledOpportunity",
    "assert_matches_recomputed_schedule",
    "assert_report_self_hash",
    "canonical_report_bytes",
    "canonical_report_payload",
    "compute_baseline_phase_offsets",
    "compute_calendar_sha256",
    "compute_report_id",
    "load_layer_two_tranche_phase_schedule_report",
    "plan_layer_two_tranche_phase_schedule",
    "seal_layer_two_tranche_phase_schedule_report",
    "shifted_tranche_phase_offsets",
    "validate_phase_schedule_calendar",
    "verify_layer_two_tranche_phase_schedule_report",
    "verify_layer_two_tranche_phase_schedule_report_file",
    "write_layer_two_tranche_phase_schedule_report",
]
