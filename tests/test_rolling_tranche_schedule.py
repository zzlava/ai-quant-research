from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.research.rolling_tranche_schedule import (
    RollingTrancheScheduleReport,
    assert_report_self_hash,
    compute_report_id,
    diagnose_rolling_tranche_schedule,
    seal_rolling_tranche_schedule_report,
    validate_market_calendar,
    verify_rolling_tranche_schedule_report_file,
    write_rolling_tranche_schedule_report,
)


def _calendar(start: date, n: int) -> list[date]:
    return [start + timedelta(days=offset) for offset in range(n)]


def test_n_equals_h_20_assigns_each_tranche_once_with_warm_up() -> None:
    calendar = _calendar(date(2024, 1, 1), 20)
    report = diagnose_rolling_tranche_schedule(
        market_calendar=calendar,
        start=calendar[0],
        end=calendar[-1],
        tranche_count=20,
        holding_period_bars=20,
        initial_capital=80_000.0,
    )
    assert report.total_scheduled_decisions == 20
    assert report.decisions_per_tranche == [1] * 20
    assert [row.tranche_id for row in report.schedule_rows] == list(range(20))
    assert report.per_tranche_capital == 4_000.0
    assert report.daily_utilization[0].active_tranche_count == 1
    assert report.daily_utilization[0].theoretical_allocated_fraction == pytest.approx(0.05)
    assert report.daily_utilization[0].theoretical_cash_fraction == pytest.approx(0.95)
    assert report.daily_utilization[0].is_warm_up_day is True
    assert report.daily_utilization[18].active_tranche_count == 19
    assert report.daily_utilization[18].is_warm_up_day is True
    assert report.daily_utilization[19].active_tranche_count == 20
    assert report.daily_utilization[19].theoretical_allocated_fraction == pytest.approx(1.0)
    assert report.daily_utilization[19].is_warm_up_day is False
    assert report.warm_up_day_count == 19
    assert report.peak_active_tranche_count == 20
    assert report.diagnostic_only is True
    assert report.ready_for_scoring is False
    assert report.ready_for_backtest is False
    assert report.ready_for_trading is False
    assert report.auto_apply is False
    payload = report.model_dump(mode="json")
    for forbidden in ("sharpe", "total_return", "pnl", "winner", "best_phase", "ready_for_live"):
        assert forbidden not in json.dumps(payload)


def test_longer_fixture_reuses_tranches_deterministically_after_completion() -> None:
    calendar = _calendar(date(2024, 1, 1), 45)
    report = diagnose_rolling_tranche_schedule(
        market_calendar=calendar,
        start=calendar[0],
        end=calendar[-1],
        tranche_count=20,
        holding_period_bars=20,
        initial_capital=80_000.0,
    )
    assert report.total_scheduled_decisions == 45
    assert report.decisions_per_tranche[0] == 3  # days 0, 20, 40
    assert report.decisions_per_tranche[4] == 3  # days 4, 24, 44
    assert report.decisions_per_tranche[5] == 2  # days 5, 25
    assert report.schedule_rows[20].tranche_id == 0
    assert report.schedule_rows[20].decision_date == calendar[20]
    assert report.schedule_rows[0].next_free_date == calendar[20]
    # Steady-state full utilization after warm-up.
    assert report.daily_utilization[19].active_tranche_count == 20
    assert report.daily_utilization[20].active_tranche_count == 20
    assert report.daily_utilization[20].is_warm_up_day is False
    again = diagnose_rolling_tranche_schedule(
        market_calendar=calendar,
        start=calendar[0],
        end=calendar[-1],
        tranche_count=20,
        holding_period_bars=20,
        initial_capital=80_000.0,
    )
    assert again.report_id == report.report_id


def test_h_greater_than_n_fails_closed() -> None:
    calendar = _calendar(date(2024, 1, 1), 30)
    with pytest.raises(ValueError, match="hidden leverage"):
        diagnose_rolling_tranche_schedule(
            market_calendar=calendar,
            start=calendar[0],
            end=calendar[-1],
            tranche_count=5,
            holding_period_bars=6,
            initial_capital=80_000.0,
        )


def test_duplicate_unsorted_missing_boundary_calendar_fails() -> None:
    calendar = _calendar(date(2024, 1, 1), 10)
    with pytest.raises(ValueError, match="duplicate"):
        validate_market_calendar(
            [*calendar[:3], calendar[2], *calendar[3:]],
            start=calendar[0],
            end=calendar[-1],
        )
    with pytest.raises(ValueError, match="strictly increasing"):
        validate_market_calendar(
            [calendar[0], calendar[2], calendar[1], *calendar[3:]],
            start=calendar[0],
            end=calendar[-1],
        )
    with pytest.raises(ValueError, match="must be a datetime.date"):
        validate_market_calendar(
            ["2024-01-01", *calendar[1:]],  # type: ignore[list-item]
            start=calendar[0],
            end=calendar[-1],
        )
    with pytest.raises(ValueError, match="outside"):
        validate_market_calendar(
            [calendar[0] - timedelta(days=1), *calendar],
            start=calendar[0],
            end=calendar[-1],
        )
    with pytest.raises(ValueError, match="not in market_calendar"):
        diagnose_rolling_tranche_schedule(
            market_calendar=calendar,
            start=calendar[0],
            end=calendar[-1] + timedelta(days=1),
            tranche_count=2,
            holding_period_bars=2,
            initial_capital=80_000.0,
        )
    with pytest.raises(ValueError, match="start must be on or before end"):
        diagnose_rolling_tranche_schedule(
            market_calendar=calendar,
            start=calendar[-1],
            end=calendar[0],
            tranche_count=2,
            holding_period_bars=2,
            initial_capital=80_000.0,
        )


def test_tampered_and_resealed_invalid_report_rejected(tmp_path: Path) -> None:
    calendar = _calendar(date(2024, 1, 1), 8)
    report = diagnose_rolling_tranche_schedule(
        market_calendar=calendar,
        start=calendar[0],
        end=calendar[-1],
        tranche_count=4,
        holding_period_bars=4,
        initial_capital=80_000.0,
    )
    path = tmp_path / "schedule.json"
    write_rolling_tranche_schedule_report(report, path)
    verify_rolling_tranche_schedule_report_file(path)

    # Stale hash: derived field changed, old report_id left behind (passes cheap checks).
    stale = tmp_path / "stale-hash.json"
    stale_payload: dict[str, Any] = json.loads(report.model_dump_json())
    stale_payload["schedule_rows"][0]["extends_past_window_end"] = not stale_payload["schedule_rows"][0][
        "extends_past_window_end"
    ]
    stale.write_text(json.dumps(stale_payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="report_id does not match"):
        verify_rolling_tranche_schedule_report_file(stale)

    wrong_id = tmp_path / "wrong-report-id.json"
    wrong_id_payload: dict[str, Any] = json.loads(report.model_dump_json())
    wrong_id_payload["report_id"] = "0" * 64
    wrong_id.write_text(json.dumps(wrong_id_payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="report_id does not match"):
        verify_rolling_tranche_schedule_report_file(wrong_id)

    # True reseal after semantic tranche reassignment (counts kept internally consistent).
    resealed_tranche = report.model_copy(deep=True)
    rows = list(resealed_tranche.schedule_rows)
    rows[0] = rows[0].model_copy(update={"tranche_id": 3})
    decisions = list(resealed_tranche.decisions_per_tranche)
    decisions[0] -= 1
    decisions[3] += 1
    phase = list(resealed_tranche.phase_coverage)
    phase[0] = phase[0].model_copy(
        update={
            "decision_count": decisions[0],
            "decision_dates": [d for d in phase[0].decision_dates if d != rows[0].decision_date],
        }
    )
    phase[3] = phase[3].model_copy(
        update={
            "decision_count": decisions[3],
            "decision_dates": sorted([*phase[3].decision_dates, rows[0].decision_date]),
        }
    )
    resealed_tranche = resealed_tranche.model_copy(
        update={
            "schedule_rows": rows,
            "decisions_per_tranche": decisions,
            "phase_coverage": phase,
            "report_id": None,
        }
    )
    resealed_tranche = seal_rolling_tranche_schedule_report(resealed_tranche)
    assert resealed_tranche.report_id == compute_report_id(resealed_tranche)
    tranche_path = tmp_path / "resealed-wrong-tranche.json"
    write_rolling_tranche_schedule_report(resealed_tranche, tranche_path)
    with pytest.raises(ValueError, match="does not match recomputed diagnose"):
        verify_rolling_tranche_schedule_report_file(tranche_path)

    # True reseal after wrong utilization/count while schedule rows stay round-robin.
    resealed_util = report.model_copy(deep=True)
    util_rows = list(resealed_util.daily_utilization)
    util_rows[0] = util_rows[0].model_copy(
        update={
            "active_tranche_count": 2,
            "active_tranche_ids": [0, 1],
            "theoretical_allocated_fraction": 0.5,
            "theoretical_cash_fraction": 0.5,
            "is_warm_up_day": True,
        }
    )
    resealed_util = resealed_util.model_copy(
        update={
            "daily_utilization": util_rows,
            "warm_up_day_count": sum(1 for row in util_rows if row.is_warm_up_day),
            "tail_effect_day_count": sum(1 for row in util_rows if row.is_tail_effect_day),
            "peak_active_tranche_count": max(row.active_tranche_count for row in util_rows),
            "min_active_tranche_count": min(row.active_tranche_count for row in util_rows),
            "report_id": None,
        }
    )
    resealed_util = seal_rolling_tranche_schedule_report(resealed_util)
    assert resealed_util.report_id == compute_report_id(resealed_util)
    util_path = tmp_path / "resealed-wrong-utilization.json"
    write_rolling_tranche_schedule_report(resealed_util, util_path)
    with pytest.raises(ValueError, match="does not match recomputed diagnose"):
        verify_rolling_tranche_schedule_report_file(util_path)


def test_deterministic_hash() -> None:
    calendar = _calendar(date(2024, 2, 1), 12)
    first = diagnose_rolling_tranche_schedule(
        market_calendar=calendar,
        start=calendar[0],
        end=calendar[-1],
        tranche_count=3,
        holding_period_bars=3,
        initial_capital=80_000.0,
    )
    second = diagnose_rolling_tranche_schedule(
        market_calendar=calendar,
        start=calendar[0],
        end=calendar[-1],
        tranche_count=3,
        holding_period_bars=3,
        initial_capital=80_000.0,
    )
    assert first.report_id == second.report_id
    assert first.report_id == compute_report_id(first)
    assert_report_self_hash(first)


def test_ready_flags_literal_false() -> None:
    calendar = _calendar(date(2024, 3, 1), 6)
    report = diagnose_rolling_tranche_schedule(
        market_calendar=calendar,
        start=calendar[0],
        end=calendar[-1],
        tranche_count=2,
        holding_period_bars=2,
        initial_capital=80_000.0,
    )
    payload = report.model_dump(mode="json")
    payload["ready_for_scoring"] = True
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        RollingTrancheScheduleReport.model_validate(payload)


def test_cli_verify_schedule_report(tmp_path: Path) -> None:
    calendar = _calendar(date(2024, 4, 1), 6)
    report = diagnose_rolling_tranche_schedule(
        market_calendar=calendar,
        start=calendar[0],
        end=calendar[-1],
        tranche_count=2,
        holding_period_bars=2,
        initial_capital=80_000.0,
    )
    path = tmp_path / "ok.json"
    write_rolling_tranche_schedule_report(report, path)
    runner = CliRunner()
    missing = runner.invoke(cli_app, ["verify-rolling-tranche-schedule-report"])
    assert missing.exit_code != 0
    ok = runner.invoke(
        cli_app,
        ["verify-rolling-tranche-schedule-report", "--report-file", str(path)],
    )
    assert ok.exit_code == 0, ok.output
    assert f"report_id={report.report_id}" in ok.output
    assert "diagnostic_only=true" in ok.output
    assert "ready_for_scoring=false" in ok.output
    assert "auto_apply=false" in ok.output
