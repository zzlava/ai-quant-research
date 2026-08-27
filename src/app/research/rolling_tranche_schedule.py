"""Fail-closed, read-only daily rolling-tranche schedule diagnostic.

Models one decision allocation per in-window trading day and round-robin
tranche assignment. Does not select stocks, prices, orders, returns, PnL,
regimes, or choose N / H / thresholds. Never authorizes scoring, backtest,
or trading.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROLLING_TRANCHE_SCHEDULE_SCHEMA_VERSION: Literal["1"] = "1"
ROLLING_TRANCHE_SCHEDULE_DIAGNOSTIC_VERSION: Literal["rolling-tranche-schedule-diagnostic-v1"] = (
    "rolling-tranche-schedule-diagnostic-v1"
)

# Holding interval convention (exact):
# - A decision on absolute calendar index i occupies H consecutive market
#   trading bars calendar[i] .. calendar[i + H - 1] (inclusive of the decision
#   day; length exactly H).
# - The tranche becomes free for a new decision on calendar[i + H] (first
#   trading day after the holding interval). Same-day free-and-reassign on
#   calendar[i + H] does not overlap capital.
# - Under one-decision-per-day round-robin, the next assignment to the same
#   tranche is N trading days later; H > N always implies overlapping capital
#   (hidden leverage) and is rejected.
HOLDING_INTERVAL_CONVENTION: Literal[
    "decision_day_inclusive_H_bars_free_on_index_plus_H"
] = "decision_day_inclusive_H_bars_free_on_index_plus_H"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RollingTrancheScheduleRow(_StrictModel):
    decision_index_in_window: int = Field(ge=0)
    absolute_calendar_index: int = Field(ge=0)
    decision_date: date
    tranche_id: int = Field(ge=0)
    holding_start_date: date
    holding_last_bar_date: date | None
    holding_end_exclusive_index: int = Field(ge=1)
    next_free_date: date | None
    holding_complete_on_calendar: bool
    extends_past_window_end: bool


class RollingTrancheDailyUtilization(_StrictModel):
    date: date
    absolute_calendar_index: int = Field(ge=0)
    active_tranche_count: int = Field(ge=0)
    active_tranche_ids: list[int]
    theoretical_allocated_fraction: float
    theoretical_cash_fraction: float
    is_warm_up_day: bool
    is_tail_effect_day: bool

    @field_validator("theoretical_allocated_fraction", "theoretical_cash_fraction")
    @classmethod
    def _finite_unit_interval(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("utilization fractions must be finite")
        if value < -1e-15 or value > 1.0 + 1e-15:
            raise ValueError("utilization fractions must lie in [0, 1]")
        return float(value)


class RollingTranchePhaseCoverage(_StrictModel):
    tranche_id: int = Field(ge=0)
    decision_count: int = Field(ge=0)
    decision_dates: list[date]


class RollingTrancheScheduleReport(_StrictModel):
    """Sealed schedule diagnostic; never authorizes scoring, backtest, or trading."""

    schema_version: Literal["1"] = ROLLING_TRANCHE_SCHEDULE_SCHEMA_VERSION
    diagnostic_version: Literal["rolling-tranche-schedule-diagnostic-v1"] = (
        ROLLING_TRANCHE_SCHEDULE_DIAGNOSTIC_VERSION
    )
    report_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    market_calendar: list[date]
    start: date
    end: date
    window_dates: list[date]
    tranche_count: int = Field(ge=1)
    holding_period_bars: int = Field(ge=1)
    initial_capital: float
    per_tranche_capital: float
    holding_interval_convention: Literal["decision_day_inclusive_H_bars_free_on_index_plus_H"] = (
        HOLDING_INTERVAL_CONVENTION
    )
    schedule_rows: list[RollingTrancheScheduleRow]
    daily_utilization: list[RollingTrancheDailyUtilization]
    total_scheduled_decisions: int = Field(ge=0)
    decisions_per_tranche: list[int]
    phase_coverage: list[RollingTranchePhaseCoverage]
    warm_up_day_count: int = Field(ge=0)
    tail_effect_day_count: int = Field(ge=0)
    peak_active_tranche_count: int = Field(ge=0)
    min_active_tranche_count: int = Field(ge=0)
    diagnostic_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator("initial_capital", "per_tranche_capital")
    @classmethod
    def _finite_positive_capital(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        if value <= 0.0:
            raise ValueError(f"{info.field_name} must be > 0")
        return float(value)

    @model_validator(mode="after")
    def _gate_flags_and_lengths(self) -> RollingTrancheScheduleReport:
        if self.diagnostic_only is not True:
            raise ValueError("diagnostic_only must remain true")
        if self.ready_for_scoring or self.ready_for_backtest or self.ready_for_trading or self.auto_apply:
            raise ValueError("scoring/backtest/trading/auto_apply must remain false")
        if self.holding_period_bars > self.tranche_count:
            raise ValueError("holding_period_bars > tranche_count implies hidden leverage under daily round-robin")
        if len(self.decisions_per_tranche) != self.tranche_count:
            raise ValueError("decisions_per_tranche length must equal tranche_count")
        if len(self.phase_coverage) != self.tranche_count:
            raise ValueError("phase_coverage length must equal tranche_count")
        if self.total_scheduled_decisions != len(self.schedule_rows):
            raise ValueError("total_scheduled_decisions must equal schedule_rows length")
        if self.total_scheduled_decisions != len(self.window_dates):
            raise ValueError("one decision required per in-window trading day")
        if len(self.daily_utilization) != len(self.window_dates):
            raise ValueError("daily_utilization length must equal window_dates length")
        expected_capital = self.initial_capital / float(self.tranche_count)
        if not math.isclose(self.per_tranche_capital, expected_capital, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("per_tranche_capital must equal initial_capital / tranche_count")
        if self.market_calendar != self.window_dates:
            raise ValueError("market_calendar must equal window_dates for this diagnostic")
        if self.window_dates and (self.window_dates[0] != self.start or self.window_dates[-1] != self.end):
            raise ValueError("window_dates terminals must equal start/end")
        _assert_cheap_schedule_semantics(self)
        return self


def diagnose_rolling_tranche_schedule(
    *,
    market_calendar: Sequence[date],
    start: date,
    end: date,
    tranche_count: int,
    holding_period_bars: int,
    initial_capital: float,
) -> RollingTrancheScheduleReport:
    """Build a sealed rolling-tranche schedule diagnostic.

    All arguments are required; there are no defaults. This function never loads
    market prices, scores, backtests, or trades.
    """
    calendar = validate_market_calendar(market_calendar, start=start, end=end)
    n = _require_positive_int(tranche_count, field_name="tranche_count")
    h = _require_positive_int(holding_period_bars, field_name="holding_period_bars")
    capital = _require_positive_finite(initial_capital, field_name="initial_capital")
    if h > n:
        raise ValueError(
            "holding_period_bars > tranche_count implies hidden leverage under "
            "daily one-decision-per-day round-robin; refusing schedule"
        )

    start_index = calendar.index(start)
    end_index = calendar.index(end)
    window_dates = list(calendar[start_index : end_index + 1])
    if not window_dates:
        raise ValueError("evaluation window has no trading days")
    if window_dates[0] != start or window_dates[-1] != end:
        raise ValueError("window bounds must match calendar slice terminals")

    per_tranche_capital = capital / float(n)
    last_decision_index_by_tranche: dict[int, int] = {}
    schedule_rows: list[RollingTrancheScheduleRow] = []
    decisions_per_tranche = [0 for _ in range(n)]
    phase_dates: list[list[date]] = [[] for _ in range(n)]

    for window_offset, decision_date in enumerate(window_dates):
        absolute_index = start_index + window_offset
        if calendar[absolute_index] != decision_date:
            raise ValueError("calendar index boundary mismatch while assigning decisions")
        tranche_id = window_offset % n
        previous = last_decision_index_by_tranche.get(tranche_id)
        if previous is not None and absolute_index < previous + h:
            raise ValueError(
                f"tranche_id={tranche_id} reassigned on {decision_date.isoformat()} before "
                f"prior H-bar holding completed (prior_index={previous}, H={h}); "
                "refusing hidden leverage"
            )

        holding_end_exclusive = absolute_index + h
        holding_last_index = holding_end_exclusive - 1
        holding_complete = holding_last_index < len(calendar)
        holding_last_bar_date = calendar[holding_last_index] if holding_complete else None
        next_free_date = calendar[holding_end_exclusive] if holding_end_exclusive < len(calendar) else None
        extends_past_window_end = holding_last_index > end_index

        schedule_rows.append(
            RollingTrancheScheduleRow(
                decision_index_in_window=window_offset,
                absolute_calendar_index=absolute_index,
                decision_date=decision_date,
                tranche_id=tranche_id,
                holding_start_date=decision_date,
                holding_last_bar_date=holding_last_bar_date,
                holding_end_exclusive_index=holding_end_exclusive,
                next_free_date=next_free_date,
                holding_complete_on_calendar=holding_complete,
                extends_past_window_end=extends_past_window_end,
            )
        )
        last_decision_index_by_tranche[tranche_id] = absolute_index
        decisions_per_tranche[tranche_id] += 1
        phase_dates[tranche_id].append(decision_date)

    daily_utilization = _build_daily_utilization(
        window_dates=window_dates,
        start_index=start_index,
        end_index=end_index,
        schedule_rows=schedule_rows,
        tranche_count=n,
        holding_period_bars=h,
    )
    phase_coverage = [
        RollingTranchePhaseCoverage(
            tranche_id=tranche_id,
            decision_count=decisions_per_tranche[tranche_id],
            decision_dates=list(phase_dates[tranche_id]),
        )
        for tranche_id in range(n)
    ]
    active_counts = [row.active_tranche_count for row in daily_utilization]
    report = RollingTrancheScheduleReport(
        market_calendar=list(calendar),
        start=start,
        end=end,
        window_dates=window_dates,
        tranche_count=n,
        holding_period_bars=h,
        initial_capital=capital,
        per_tranche_capital=per_tranche_capital,
        schedule_rows=schedule_rows,
        daily_utilization=daily_utilization,
        total_scheduled_decisions=len(schedule_rows),
        decisions_per_tranche=decisions_per_tranche,
        phase_coverage=phase_coverage,
        warm_up_day_count=sum(1 for row in daily_utilization if row.is_warm_up_day),
        tail_effect_day_count=sum(1 for row in daily_utilization if row.is_tail_effect_day),
        peak_active_tranche_count=max(active_counts) if active_counts else 0,
        min_active_tranche_count=min(active_counts) if active_counts else 0,
    )
    return seal_rolling_tranche_schedule_report(report)


def validate_market_calendar(
    market_calendar: Sequence[date],
    *,
    start: date,
    end: date,
) -> list[date]:
    """Fail closed on duplicate / unsorted / non-date / out-of-window / missing bounds."""
    if type(start) is not date:
        raise ValueError("start must be a datetime.date")
    if type(end) is not date:
        raise ValueError("end must be a datetime.date")
    if start > end:
        raise ValueError("start must be on or before end")
    if not isinstance(market_calendar, Sequence) or isinstance(market_calendar, (str, bytes)):
        raise ValueError("market_calendar must be a sequence of dates")
    if len(market_calendar) == 0:
        raise ValueError("market_calendar must be non-empty")

    cleaned: list[date] = []
    for index, value in enumerate(market_calendar):
        if type(value) is not date:
            raise ValueError(f"market_calendar[{index}] must be a datetime.date")
        if value < start or value > end:
            raise ValueError(
                f"market_calendar[{index}]={value.isoformat()} is outside "
                f"[{start.isoformat()}, {end.isoformat()}]; no silent clipping"
            )
        cleaned.append(value)

    for previous, current in zip(cleaned, cleaned[1:], strict=False):
        if current == previous:
            raise ValueError("market_calendar must not contain duplicate dates")
        if current < previous:
            raise ValueError("market_calendar must be strictly increasing")

    if start not in cleaned:
        raise ValueError(f"start {start.isoformat()} is not in market_calendar")
    if end not in cleaned:
        raise ValueError(f"end {end.isoformat()} is not in market_calendar")

    # Contiguous window: calendar must equal inclusive [start, end] with no extras.
    if cleaned[0] != start or cleaned[-1] != end:
        raise ValueError(
            "market_calendar must equal the inclusive [start, end] window with no "
            "leading/trailing out-of-window entries"
        )
    return cleaned


def _build_daily_utilization(
    *,
    window_dates: list[date],
    start_index: int,
    end_index: int,
    schedule_rows: list[RollingTrancheScheduleRow],
    tranche_count: int,
    holding_period_bars: int,
) -> list[RollingTrancheDailyUtilization]:
    steady_peak = min(tranche_count, holding_period_bars)
    rows: list[RollingTrancheDailyUtilization] = []
    for window_offset, day in enumerate(window_dates):
        absolute_index = start_index + window_offset
        active_ids: list[int] = []
        for decision in schedule_rows:
            hold_start = decision.absolute_calendar_index
            hold_end_exclusive = decision.holding_end_exclusive_index
            if hold_start <= absolute_index < hold_end_exclusive:
                if decision.tranche_id in active_ids:
                    raise ValueError(
                        f"tranche_id={decision.tranche_id} has overlapping holdings on {day.isoformat()}"
                    )
                active_ids.append(decision.tranche_id)
        active_ids.sort()
        active_count = len(active_ids)
        if active_count > tranche_count:
            raise ValueError("active_tranche_count cannot exceed tranche_count")
        allocated = active_count / float(tranche_count)
        # Warm-up: active capital below the theoretical steady peak min(N, H).
        is_warm_up = active_count < steady_peak
        # Tail: any active holding whose last bar index is past the window end.
        is_tail = any(
            decision.absolute_calendar_index
            <= absolute_index
            < decision.holding_end_exclusive_index
            and decision.holding_end_exclusive_index - 1 > end_index
            for decision in schedule_rows
        )
        rows.append(
            RollingTrancheDailyUtilization(
                date=day,
                absolute_calendar_index=absolute_index,
                active_tranche_count=active_count,
                active_tranche_ids=active_ids,
                theoretical_allocated_fraction=allocated,
                theoretical_cash_fraction=1.0 - allocated,
                is_warm_up_day=is_warm_up,
                is_tail_effect_day=is_tail,
            )
        )
    return rows


def canonical_report_payload(report: RollingTrancheScheduleReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: RollingTrancheScheduleReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: RollingTrancheScheduleReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_rolling_tranche_schedule_report(
    report: RollingTrancheScheduleReport,
) -> RollingTrancheScheduleReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: RollingTrancheScheduleReport) -> None:
    if report.report_id is None:
        raise ValueError("rolling tranche schedule report_id is missing")
    expected = compute_report_id(report)
    if report.report_id != expected:
        raise ValueError("rolling tranche schedule report_id does not match canonical content hash")


def _assert_cheap_schedule_semantics(report: RollingTrancheScheduleReport) -> None:
    """Fail closed on cheap contradictions; recompute-and-compare remains the final gate."""
    n = report.tranche_count
    h = report.holding_period_bars
    for row in report.schedule_rows:
        if not (0 <= row.tranche_id < n):
            raise ValueError(f"schedule row tranche_id={row.tranche_id} must satisfy 0 <= id < tranche_count")
        if row.decision_index_in_window < 0 or row.decision_index_in_window >= len(report.window_dates):
            raise ValueError("schedule row decision_index_in_window out of window")
        expected_date = report.window_dates[row.decision_index_in_window]
        if row.decision_date != expected_date or row.holding_start_date != expected_date:
            raise ValueError("schedule row decision/holding_start date must match window_dates[index]")
        if row.holding_end_exclusive_index != row.absolute_calendar_index + h:
            raise ValueError("holding_end_exclusive_index must equal absolute_calendar_index + H")
    for index, row in enumerate(report.schedule_rows):
        if row.decision_index_in_window != index:
            raise ValueError("schedule_rows must be ordered by decision_index_in_window 0..W-1")
        if row.decision_date != report.window_dates[index]:
            raise ValueError("schedule_rows decision dates must equal window_dates in order")
    for tranche_id, coverage in enumerate(report.phase_coverage):
        if coverage.tranche_id != tranche_id:
            raise ValueError("phase_coverage must be ordered by tranche_id 0..N-1")
        if coverage.decision_count != report.decisions_per_tranche[tranche_id]:
            raise ValueError("phase_coverage.decision_count must match decisions_per_tranche")
        if coverage.decision_count != len(coverage.decision_dates):
            raise ValueError("phase_coverage.decision_dates length must match decision_count")
        counted = sum(1 for row in report.schedule_rows if row.tranche_id == tranche_id)
        if counted != coverage.decision_count:
            raise ValueError("phase_coverage counts must match schedule_rows tranche assignments")
    if sum(report.decisions_per_tranche) != report.total_scheduled_decisions:
        raise ValueError("sum(decisions_per_tranche) must equal total_scheduled_decisions")
    for day_index, util in enumerate(report.daily_utilization):
        if util.date != report.window_dates[day_index]:
            raise ValueError("daily_utilization dates must equal window_dates in order")
        if util.active_tranche_count != len(util.active_tranche_ids):
            raise ValueError("active_tranche_count must equal len(active_tranche_ids)")
        if len(set(util.active_tranche_ids)) != len(util.active_tranche_ids):
            raise ValueError("active_tranche_ids must be unique")
        if any(not (0 <= tranche_id < n) for tranche_id in util.active_tranche_ids):
            raise ValueError("active_tranche_ids must satisfy 0 <= id < tranche_count")
        expected_alloc = util.active_tranche_count / float(n)
        if not math.isclose(util.theoretical_allocated_fraction, expected_alloc, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("theoretical_allocated_fraction must equal active_tranche_count / N")
        if not math.isclose(
            util.theoretical_cash_fraction,
            1.0 - util.theoretical_allocated_fraction,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("theoretical_cash_fraction must equal 1 - allocated fraction")
    active_counts = [row.active_tranche_count for row in report.daily_utilization]
    if active_counts:
        if report.peak_active_tranche_count != max(active_counts):
            raise ValueError("peak_active_tranche_count must equal max daily active_tranche_count")
        if report.min_active_tranche_count != min(active_counts):
            raise ValueError("min_active_tranche_count must equal min daily active_tranche_count")
    if report.warm_up_day_count != sum(1 for row in report.daily_utilization if row.is_warm_up_day):
        raise ValueError("warm_up_day_count must match daily is_warm_up_day flags")
    if report.tail_effect_day_count != sum(1 for row in report.daily_utilization if row.is_tail_effect_day):
        raise ValueError("tail_effect_day_count must match daily is_tail_effect_day flags")


def assert_report_matches_recomputed_inputs(report: RollingTrancheScheduleReport) -> None:
    """Final gate: every derived field must match diagnose() on the report inputs."""
    expected = diagnose_rolling_tranche_schedule(
        market_calendar=report.market_calendar,
        start=report.start,
        end=report.end,
        tranche_count=report.tranche_count,
        holding_period_bars=report.holding_period_bars,
        initial_capital=report.initial_capital,
    )
    if canonical_report_payload(report) != canonical_report_payload(expected):
        raise ValueError(
            "rolling tranche schedule report canonical payload does not match "
            "recomputed diagnose() output from report inputs"
        )
    if report.report_id != expected.report_id:
        raise ValueError(
            "rolling tranche schedule report_id does not match recomputed diagnose() report_id"
        )


def load_rolling_tranche_schedule_report(path: Path) -> RollingTrancheScheduleReport:
    try:
        return RollingTrancheScheduleReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("rolling tranche schedule report is missing or invalid") from exc


def verify_rolling_tranche_schedule_report_file(path: Path) -> RollingTrancheScheduleReport:
    report = load_rolling_tranche_schedule_report(path)
    assert_report_self_hash(report)
    # Re-validate calendar/window consistency without inventing parameters.
    validate_market_calendar(report.market_calendar, start=report.start, end=report.end)
    if report.diagnostic_only is not True:
        raise ValueError("diagnostic_only must remain true")
    if report.ready_for_scoring or report.ready_for_backtest or report.ready_for_trading or report.auto_apply:
        raise ValueError("ready/auto flags must remain false")
    # Do not trust stored schedule/utilization/counts: recompute from inputs.
    assert_report_matches_recomputed_inputs(report)
    return report


def write_rolling_tranche_schedule_report(report: RollingTrancheScheduleReport, output: Path) -> None:
    sealed = seal_rolling_tranche_schedule_report(report) if report.report_id is None else report
    assert_report_self_hash(sealed)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _require_positive_int(value: int, *, field_name: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an int")
    if value < 1:
        raise ValueError(f"{field_name} must be >= 1")
    return value


def _require_positive_finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    if number <= 0.0:
        raise ValueError(f"{field_name} must be > 0")
    return number


__all__ = [
    "HOLDING_INTERVAL_CONVENTION",
    "ROLLING_TRANCHE_SCHEDULE_DIAGNOSTIC_VERSION",
    "ROLLING_TRANCHE_SCHEDULE_SCHEMA_VERSION",
    "RollingTrancheDailyUtilization",
    "RollingTranchePhaseCoverage",
    "RollingTrancheScheduleReport",
    "RollingTrancheScheduleRow",
    "assert_report_matches_recomputed_inputs",
    "assert_report_self_hash",
    "canonical_report_bytes",
    "canonical_report_payload",
    "compute_report_id",
    "diagnose_rolling_tranche_schedule",
    "load_rolling_tranche_schedule_report",
    "seal_rolling_tranche_schedule_report",
    "validate_market_calendar",
    "verify_rolling_tranche_schedule_report_file",
    "write_rolling_tranche_schedule_report",
]
