from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.research.layer_two_tranche_phase_schedule import (
    BOUND_ALLOCATION_PROTOCOL_ID,
    BOUND_ALLOCATION_PROTOCOL_PATH,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
    HOLDING_CYCLE_MARKET_TRADING_DAYS,
    LayerTwoTranchePhaseScheduleReport,
    compute_baseline_phase_offsets,
    compute_report_id,
    plan_layer_two_tranche_phase_schedule,
    seal_layer_two_tranche_phase_schedule_report,
    verify_layer_two_tranche_phase_schedule_report,
    verify_layer_two_tranche_phase_schedule_report_file,
    write_layer_two_tranche_phase_schedule_report,
)
from tests.helpers import PROJECT_ROOT


def _trading_days(start: date, n: int) -> list[date]:
    out: list[date] = []
    current = start
    while len(out) < n:
        if current.weekday() < 5:
            out.append(current)
        current += timedelta(days=1)
    return out


def _snapshot(seed: str = "aa") -> str:
    return (seed * 32)[:64]


def _plan(
    *,
    calendar: list[date],
    start: date | None = None,
    end: date | None = None,
    anchor: date | None = None,
    equity: float = 80_000.0,
    risk_budget: float = 0.3,
    snapshot: str | None = None,
) -> LayerTwoTranchePhaseScheduleReport:
    return plan_layer_two_tranche_phase_schedule(
        market_calendar=calendar,
        start=start or calendar[0],
        end=end or calendar[-1],
        anchor=anchor or calendar[0],
        current_account_equity=equity,
        risk_budget=risk_budget,
        market_data_snapshot_id=snapshot or _snapshot(),
    )


def test_baseline_offsets_exact_for_n_2_3_6_9() -> None:
    assert compute_baseline_phase_offsets(0) == []
    assert compute_baseline_phase_offsets(2) == [0, 20]
    assert compute_baseline_phase_offsets(3) == [0, 13, 26]
    assert compute_baseline_phase_offsets(6) == [0, 6, 13, 20, 26, 33]
    assert compute_baseline_phase_offsets(9) == [0, 4, 8, 13, 17, 22, 26, 31, 35]
    assert HOLDING_CYCLE_MARKET_TRADING_DAYS == 40


def test_active_counts_via_plan_base_slots_n_0_2_3_6_9() -> None:
    calendar = _trading_days(date(2022, 1, 3), 120)

    n0 = _plan(calendar=calendar, equity=80_000.0, risk_budget=0.0)
    assert n0.active_tranche_count == 0
    assert n0.baseline_phase_offsets == []
    assert n0.selected_schedule.opportunity_count == 0
    assert n0.cash_retention_reason == "zero_risk_budget"

    n0_short = _plan(calendar=calendar, equity=20_000.0, risk_budget=0.3)
    assert n0_short.active_tranche_count == 0
    assert n0_short.cash_retention_reason == "insufficient_capital_for_minimum_base_slot"

    n2 = _plan(calendar=calendar, equity=56_000.0, risk_budget=0.3)
    assert n2.base_slot.sleeve_budget == 16_800.0
    assert n2.active_tranche_count == 2
    assert n2.baseline_phase_offsets == [0, 20]

    n3 = _plan(calendar=calendar, equity=80_000.0, risk_budget=0.3)
    assert n3.active_tranche_count == 3
    assert n3.baseline_phase_offsets == [0, 13, 26]

    n6 = _plan(calendar=calendar, equity=80_000.0, risk_budget=0.6)
    assert n6.active_tranche_count == 6
    assert n6.baseline_phase_offsets == [0, 6, 13, 20, 26, 33]

    n9 = _plan(calendar=calendar, equity=80_000.0, risk_budget=0.9)
    assert n9.active_tranche_count == 9
    assert n9.baseline_phase_offsets == [0, 4, 8, 13, 17, 22, 26, 31, 35]


def test_40_day_cycle_and_gradual_build_no_same_day_catchup() -> None:
    calendar = _trading_days(date(2022, 1, 3), 100)
    report = _plan(calendar=calendar, equity=80_000.0, risk_budget=0.3)
    assert report.active_tranche_count == 3
    assert report.gradual_build_required is True
    assert report.same_day_catchup_fill_forbidden is True
    assert report.does_not_select_stocks is True
    assert report.risk_reduce_not_phase_limited is True
    assert report.risk_reduce_does_not_emit_orders_in_this_module is True

    selected = report.selected_schedule
    # First opportunities for tranches 0/1/2 are staggered, not same day.
    first_by_tranche = {}
    for row in selected.opportunities:
        first_by_tranche.setdefault(row.tranche_id, row.decision_date)
    assert len(first_by_tranche) == 3
    assert len(set(first_by_tranche.values())) == 3

    # Same calendar day never schedules two tranches.
    dates = [row.decision_date for row in selected.opportunities]
    assert len(dates) == len(set(dates))

    # Next opportunity per tranche is exactly 40 market days later.
    by_tranche: dict[int, list[int]] = {}
    for row in selected.opportunities:
        by_tranche.setdefault(row.tranche_id, []).append(row.absolute_calendar_index)
    for indices in by_tranche.values():
        for a, b in zip(indices, indices[1:], strict=False):
            assert b - a == 40


def test_window_truncation_and_full_40_shift_family() -> None:
    calendar = _trading_days(date(2022, 1, 3), 50)
    # Short window: fewer than one full cycle of all tranches.
    report = _plan(
        calendar=calendar,
        start=calendar[5],
        end=calendar[20],
        anchor=calendar[0],
        equity=80_000.0,
        risk_budget=0.3,
    )
    assert len(report.phase_family) == 40
    assert report.selected_operational_family_shift == 0
    assert report.selected_schedule == report.phase_family[0]
    assert report.phase_family[0].is_selected_operational_schedule is True
    assert all(not m.is_selected_operational_schedule for m in report.phase_family[1:])
    # Family members differ by cyclic shift of offsets.
    base = report.baseline_phase_offsets
    for shift, member in enumerate(report.phase_family):
        assert member.family_shift == shift
        assert member.tranche_phase_offsets == [(o + shift) % 40 for o in base]
        assert member.opportunity_count == len(member.opportunities)
    # Truncation: every opportunity date stays inside [start, end].
    for member in report.phase_family:
        for row in member.opportunities:
            assert report.start <= row.decision_date <= report.end


def test_weekend_missing_from_calendar_fails() -> None:
    calendar = _trading_days(date(2022, 1, 3), 40)
    saturday = calendar[0] + timedelta(days=(5 - calendar[0].weekday()) % 7)
    assert saturday.weekday() == 5
    assert saturday not in calendar
    with pytest.raises(ValueError, match="not in market_calendar"):
        _plan(calendar=calendar, start=saturday, end=calendar[-1], anchor=calendar[0])


def test_anchor_start_end_missing_or_unordered_or_duplicate_calendar() -> None:
    calendar = _trading_days(date(2022, 1, 3), 40)
    with pytest.raises(ValueError, match="anchor"):
        _plan(calendar=calendar, anchor=date(2099, 1, 1))
    with pytest.raises(ValueError, match="start"):
        _plan(calendar=calendar, start=date(2099, 1, 1))
    with pytest.raises(ValueError, match="end"):
        _plan(calendar=calendar, end=date(2099, 1, 1))
    with pytest.raises(ValueError, match="anchor must be on or before start"):
        _plan(calendar=calendar, start=calendar[5], end=calendar[-1], anchor=calendar[10])
    with pytest.raises(ValueError, match="start must be on or before end"):
        _plan(calendar=calendar, start=calendar[10], end=calendar[5], anchor=calendar[0])
    dup = list(calendar)
    dup[3] = dup[2]
    with pytest.raises(ValueError, match="duplicate|strictly increasing"):
        _plan(calendar=dup)
    unsorted = list(calendar)
    unsorted[5], unsorted[6] = unsorted[6], unsorted[5]
    with pytest.raises(ValueError, match="strictly increasing"):
        _plan(calendar=unsorted)


def test_bool_nan_inf_negative_rejected() -> None:
    calendar = _trading_days(date(2022, 1, 3), 40)
    with pytest.raises(ValueError, match="bool rejected"):
        _plan(calendar=calendar, equity=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="bool rejected"):
        _plan(calendar=calendar, risk_budget=False)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="NaN/Inf rejected"):
        _plan(calendar=calendar, equity=float("nan"))
    with pytest.raises(ValueError, match="NaN/Inf rejected"):
        _plan(calendar=calendar, risk_budget=float("inf"))
    with pytest.raises(ValueError, match="must be >= 0"):
        _plan(calendar=calendar, equity=-1.0)
    with pytest.raises(ValueError, match="bool rejected"):
        compute_baseline_phase_offsets(True)  # type: ignore[arg-type]


def test_path_escape_and_upstream_id_drift_rejected() -> None:
    calendar = _trading_days(date(2022, 1, 3), 40)
    report = _plan(calendar=calendar)
    payload = report.model_dump(mode="json")
    payload["tranche_evaluation_protocol_path"] = "/etc/passwd"
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoTranchePhaseScheduleReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    payload["allocation_implementation_protocol_path"] = "../secrets/protocol.json"
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoTranchePhaseScheduleReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    payload["tranche_evaluation_protocol_id"] = "a" * 64
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoTranchePhaseScheduleReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    payload["allocation_implementation_protocol_id"] = "b" * 64
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoTranchePhaseScheduleReport.model_validate(payload)


def test_structural_and_file_verifiers_and_reseal_attacks(tmp_path: Path) -> None:
    calendar = _trading_days(date(2022, 1, 3), 80)
    report = _plan(calendar=calendar, equity=80_000.0, risk_budget=0.3, snapshot=_snapshot("ab"))
    structural = verify_layer_two_tranche_phase_schedule_report(report)
    assert structural.structural_ok is True
    assert structural.tranche_evaluation_protocol_binding_ok is False
    assert structural.allocation_implementation_protocol_binding_ok is False
    assert structural.ready_for_scoring is False
    assert structural.does_not_select_stocks is True

    path = tmp_path / "phase-schedule.json"
    write_layer_two_tranche_phase_schedule_report(path, report)
    loaded, file_result = verify_layer_two_tranche_phase_schedule_report_file(
        report_path=path,
        repo_root=PROJECT_ROOT,
    )
    assert loaded.report_id == report.report_id
    assert file_result.tranche_evaluation_protocol_binding_ok is True
    assert file_result.allocation_implementation_protocol_binding_ok is True
    assert loaded.tranche_evaluation_protocol_id == BOUND_TRANCHE_EVALUATION_PROTOCOL_ID
    assert loaded.allocation_implementation_protocol_id == BOUND_ALLOCATION_PROTOCOL_ID
    assert loaded.tranche_evaluation_protocol_path == BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
    assert loaded.allocation_implementation_protocol_path == BOUND_ALLOCATION_PROTOCOL_PATH

    # Field modify without reseal → self-hash fails.
    tampered = report.model_copy(update={"market_data_snapshot_id": _snapshot("cd")})
    with pytest.raises(ValueError, match="report_id does not match"):
        verify_layer_two_tranche_phase_schedule_report(tampered)

    # Modify opportunity / offset then outer reseal → recompute fails.
    payload = report.model_dump(mode="json")
    if payload["selected_schedule"]["opportunities"]:
        payload["selected_schedule"]["opportunities"][0]["tranche_id"] = 2
        payload["phase_family"][0] = payload["selected_schedule"]
    payload.pop("report_id", None)
    drifted = LayerTwoTranchePhaseScheduleReport.model_validate(payload)
    resealed = seal_layer_two_tranche_phase_schedule_report(drifted)
    assert resealed.report_id == compute_report_id(resealed)
    with pytest.raises(ValueError, match="does not match full recompute"):
        verify_layer_two_tranche_phase_schedule_report(resealed)

    # Calendar date drift + reseal.
    payload = report.model_dump(mode="json")
    payload["start"] = calendar[1].isoformat()
    payload.pop("report_id", None)
    # May fail model validation or recompute; either is fail-closed.
    try:
        bad = LayerTwoTranchePhaseScheduleReport.model_validate(payload)
        bad = seal_layer_two_tranche_phase_schedule_report(bad)
        with pytest.raises(ValueError):
            verify_layer_two_tranche_phase_schedule_report(bad)
    except ValidationError:
        pass


def test_ready_flag_injection_rejected() -> None:
    calendar = _trading_days(date(2022, 1, 3), 40)
    report = _plan(calendar=calendar)
    payload = report.model_dump(mode="json")
    for flag in (
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_portfolio_construction",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
    ):
        bad = json.loads(json.dumps(payload))
        bad[flag] = True
        bad.pop("report_id", None)
        with pytest.raises(ValidationError):
            LayerTwoTranchePhaseScheduleReport.model_validate(bad)


def test_no_price_return_or_position_selection_claims() -> None:
    calendar = _trading_days(date(2022, 1, 3), 60)
    report = _plan(calendar=calendar, equity=80_000.0, risk_budget=0.6)
    blob = json.dumps(report.model_dump(mode="json"))
    for forbidden in ("sharpe", "pnl", "total_return", "best_phase", "selected_stock", "broker"):
        assert forbidden not in blob
    assert report.does_not_select_stocks is True
    assert report.never_select_phase_by_return is True
    assert report.diagnostic_only is True


def test_tmp_report_may_use_non_production_snapshot(tmp_path: Path) -> None:
    calendar = _trading_days(date(2022, 1, 3), 45)
    custom = _snapshot("11")
    report = _plan(calendar=calendar, snapshot=custom)
    assert report.market_data_snapshot_id == custom
    path = tmp_path / "custom-snapshot.json"
    write_layer_two_tranche_phase_schedule_report(path, report)
    loaded, result = verify_layer_two_tranche_phase_schedule_report_file(
        report_path=path,
        repo_root=PROJECT_ROOT,
    )
    assert loaded.market_data_snapshot_id == custom
    assert result.allocation_implementation_protocol_binding_ok is True


def test_phase_family_opportunity_dates_fully_sealed_not_counts_only() -> None:
    calendar = _trading_days(date(2022, 1, 3), 90)
    report = _plan(calendar=calendar, equity=80_000.0, risk_budget=0.3)
    # Corrupt a nested opportunity date count-preserving way then reseal.
    payload = report.model_dump(mode="json")
    member = payload["phase_family"][7]
    assert member["opportunity_count"] == len(member["opportunities"])
    if member["opportunities"]:
        # Shift a date forward inside calendar if possible.
        idx = member["opportunities"][0]["absolute_calendar_index"]
        if idx + 1 <= calendar.index(report.end):
            member["opportunities"][0]["absolute_calendar_index"] = idx + 1
            member["opportunities"][0]["decision_date"] = calendar[idx + 1].isoformat()
            payload["phase_family"][7] = member
            payload.pop("report_id", None)
            drifted = seal_layer_two_tranche_phase_schedule_report(
                LayerTwoTranchePhaseScheduleReport.model_validate(payload)
            )
            with pytest.raises(ValueError, match="does not match full recompute"):
                verify_layer_two_tranche_phase_schedule_report(drifted)
