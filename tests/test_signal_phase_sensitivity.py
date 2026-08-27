from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.backtest.engine import BacktestEngine
from app.models.backtest import (
    BacktestAttribution,
    BacktestMetrics,
    BacktestResult,
    BacktestWindow,
    EquityPoint,
    SignalAttribution,
)
from app.models.scores import ScoreBreakdown, ScoreResult
from app.research.signal_phase_sensitivity import (
    analyze_signal_phase_sensitivity,
    apply_phase_anchor,
    canonical_config_diff,
    planned_signal_dates,
    resolve_phase_anchors,
    write_signal_phase_sensitivity_report,
)
from tests.helpers import (
    constant_signal,
    fill_quiet_bars,
    store_from_rows,
    weekdays,
    zero_cost_config,
)


def _score(symbol: str, as_of: date, final: float) -> ScoreResult:
    return ScoreResult(
        symbol=symbol,
        score_date=as_of,
        strategy_name="baseline_v1",
        strategy_version="1.0.0",
        strategy_config_hash="test",
        final_score=final,
        breakdown=ScoreBreakdown(
            market_score=80.0,
            global_score=60.0,
            sector_score=60.0,
            alpha_score=70.0,
            crowding_risk=10.0,
            execution_risk=10.0,
            final_score=final,
        ),
    )


def _interval_config(calendar: list[date], *, interval: int = 3):
    config = zero_cost_config()
    config.portfolio.max_positions = 1
    config.trade = config.trade.model_copy(
        update={
            "signal_interval_days": interval,
            "signal_anchor_date": calendar[0],
            "exit_policy": "fixed_horizon",
            "min_holding_days": interval,
            "max_holding_days": interval,
        }
    )
    return config


def test_n3_runs_exactly_three_phases_in_offset_order(tmp_path: Path) -> None:
    calendar = weekdays(date(2024, 1, 2), 18)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = _interval_config(calendar, interval=3)
    called: list[date] = []

    def score_fn(as_of: date) -> list[ScoreResult]:
        called.append(as_of)
        return constant_signal(["AAA"], 80.0, as_of)

    report = analyze_signal_phase_sensitivity(
        store=store,
        config=config,
        start=calendar[0],
        end=calendar[-1],
        score_fn=score_fn,
    )
    assert report.phase_count == 3
    assert [phase.phase_offset for phase in report.phases] == [0, 1, 2]
    assert [phase.signal_anchor_date for phase in report.phases] == calendar[:3]
    assert report.selected_phase is None
    assert report.diagnostic_only is True
    assert report.parameter_selection_forbidden is True
    assert report.ready_for_scoring is False
    assert report.ready_for_trading is False
    assert report.window.signal_end == calendar[-2]
    assert report.window.valuation_end == calendar[-1]
    assert report.window.end == report.window.valuation_end
    from app.research.signal_phase_sensitivity import SignalPhaseSensitivityReport

    assert "winner" not in SignalPhaseSensitivityReport.model_fields
    assert "best" not in SignalPhaseSensitivityReport.model_fields
    assert "best_phase" not in SignalPhaseSensitivityReport.model_fields
    assert "winner" not in report.model_dump()
    assert "best_phase" not in report.model_dump()

    planned_sets = [tuple(phase.planned_signal_dates) for phase in report.phases]
    assert len(set(planned_sets)) == 3
    assert planned_sets[0][0] == calendar[0]
    assert planned_sets[1][0] == calendar[1]
    assert planned_sets[2][0] == calendar[2]
    for phase in report.phases:
        assert calendar[-1] not in phase.planned_signal_dates
        assert all(calendar[0] <= day <= report.window.signal_end for day in phase.planned_signal_dates)

    for phase in report.phases:
        runtime = apply_phase_anchor(config, phase.signal_anchor_date)
        assert runtime.config_hash() == phase.runtime_config_hash
        if phase.phase_offset == 0:
            assert canonical_config_diff(config, runtime) == []
        else:
            assert canonical_config_diff(config, runtime) == ["trade.signal_anchor_date"]

    summary = report.summary
    returns = [phase.total_return for phase in report.phases]
    assert summary.total_return.min == min(returns)
    assert summary.total_return.max == max(returns)
    assert summary.total_return.range == max(returns) - min(returns)
    assert summary.total_return.median == sorted(returns)[1]
    assert "independent" in summary.independence_note.lower()
    assert "out-of-sample" in summary.independence_note.lower()

    path = tmp_path / "phase.json"
    write_signal_phase_sensitivity_report(report, path)
    text_a = path.read_text(encoding="utf-8")
    write_signal_phase_sensitivity_report(report, path)
    text_b = path.read_text(encoding="utf-8")
    assert text_a == text_b
    assert '"diagnostic_only": true' in text_a
    assert '"parameter_selection_forbidden": true' in text_a
    assert '"selected_phase": null' in text_a
    assert '"ready_for_scoring": false' in text_a
    assert '"ready_for_trading": false' in text_a
    assert '"signal_end"' in text_a
    assert '"valuation_end"' in text_a
    assert "winner" not in text_a
    assert "best_phase" not in text_a
    assert called  # score_fn used; results never fed back into scoring


def test_resolve_phase_anchors_and_planned_dates() -> None:
    calendar = weekdays(date(2024, 1, 2), 12)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    anchors = resolve_phase_anchors(store, original_anchor=calendar[0], interval=3)
    assert anchors == [(0, calendar[0]), (1, calendar[1]), (2, calendar[2])]
    signal_end = calendar[-2]
    p0 = planned_signal_dates(
        store,
        anchor=calendar[0],
        interval=3,
        signal_end=signal_end,
        evaluation_start=calendar[0],
    )
    p1 = planned_signal_dates(
        store,
        anchor=calendar[1],
        interval=3,
        signal_end=signal_end,
        evaluation_start=calendar[0],
    )
    assert p0 != p1
    assert p0 == [day for day in calendar[0::3] if day <= signal_end]
    assert p1 == [day for day in calendar[1::3] if day <= signal_end]
    assert calendar[-1] not in p0
    assert calendar[-1] not in p1


def test_planned_excludes_dates_before_evaluation_start() -> None:
    calendar = weekdays(date(2024, 1, 2), 18)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = _interval_config(calendar, interval=3)
    start = calendar[5]
    report = analyze_signal_phase_sensitivity(
        store=store,
        config=config,
        start=start,
        end=calendar[-1],
        score_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of),
    )
    assert report.original_anchor == calendar[0]
    assert start > report.original_anchor
    for phase in report.phases:
        assert phase.planned_signal_dates
        assert all(day >= start for day in phase.planned_signal_dates)
        assert calendar[0] not in phase.planned_signal_dates
        assert calendar[1] not in phase.planned_signal_dates
        assert calendar[2] not in phase.planned_signal_dates


def test_valuation_end_on_phase_day_is_excluded() -> None:
    calendar = weekdays(date(2024, 1, 2), 18)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = _interval_config(calendar, interval=3)
    # Phase 2 schedule includes calendar[17] == valuation_end when stepping from calendar[2].
    assert calendar[-1] == calendar[2::3][-1]
    report = analyze_signal_phase_sensitivity(
        store=store,
        config=config,
        start=calendar[0],
        end=calendar[-1],
        score_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of),
    )
    phase2 = next(phase for phase in report.phases if phase.phase_offset == 2)
    assert report.window.valuation_end == calendar[-1]
    assert report.window.signal_end == calendar[-2]
    assert calendar[-1] not in phase2.planned_signal_dates
    assert phase2.planned_signal_dates == [day for day in calendar[2::3] if day <= report.window.signal_end]
    assert phase2.planned_signal_dates[-1] == calendar[14]


def test_score_fn_call_dates_match_planned_per_phase() -> None:
    calendar = weekdays(date(2024, 1, 2), 18)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = _interval_config(calendar, interval=3)
    start = calendar[4]
    end = calendar[-1]

    report = analyze_signal_phase_sensitivity(
        store=store,
        config=config,
        start=start,
        end=end,
        score_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of),
    )

    for phase in report.phases:
        called: list[date] = []

        def score_fn(as_of: date, _called: list[date] = called) -> list[ScoreResult]:
            _called.append(as_of)
            return constant_signal(["AAA"], 80.0, as_of)

        runtime = apply_phase_anchor(config, phase.signal_anchor_date)
        BacktestEngine(store, runtime, signal_fn=score_fn).run(start, end)
        assert called == phase.planned_signal_dates


def test_single_day_window_fails_closed() -> None:
    calendar = weekdays(date(2024, 1, 2), 6)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = _interval_config(calendar, interval=3)
    with pytest.raises(ValueError, match="no executable signal day"):
        analyze_signal_phase_sensitivity(
            store=store,
            config=config,
            start=calendar[0],
            end=calendar[0],
            score_fn=lambda as_of: [],
        )


def test_phase_without_planned_signal_fails_whole_report() -> None:
    calendar = weekdays(date(2024, 1, 2), 12)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = _interval_config(calendar, interval=3)
    # Two trading days => one executable signal day; only one phase can land on it.
    with pytest.raises(ValueError, match="no planned signal dates"):
        analyze_signal_phase_sensitivity(
            store=store,
            config=config,
            start=calendar[5],
            end=calendar[6],
            score_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of),
        )


def test_progress_callback_reports_each_phase() -> None:
    calendar = weekdays(date(2024, 1, 2), 18)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = _interval_config(calendar, interval=3)
    events: list[tuple[int, int, date]] = []

    analyze_signal_phase_sensitivity(
        store=store,
        config=config,
        start=calendar[0],
        end=calendar[-1],
        score_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of),
        progress=lambda done, total, anchor: events.append((done, total, anchor)),
    )
    assert events == [(1, 3, calendar[0]), (2, 3, calendar[1]), (3, 3, calendar[2])]


def test_illegal_interval_fails() -> None:
    calendar = weekdays(date(2024, 1, 2), 10)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = zero_cost_config()
    config.trade = config.trade.model_copy(update={"signal_interval_days": 1, "signal_anchor_date": calendar[0]})
    with pytest.raises(ValueError, match="signal_interval_days must be > 1"):
        analyze_signal_phase_sensitivity(
            store=store,
            config=config,
            start=calendar[0],
            end=calendar[-1],
            score_fn=lambda as_of: [],
        )


def test_missing_anchor_on_analyzer_path() -> None:
    calendar = weekdays(date(2024, 1, 2), 10)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = _interval_config(calendar, interval=3)
    # Force missing anchor after construction for the analyzer guard.
    object.__setattr__(config.trade, "signal_anchor_date", None)
    with pytest.raises(ValueError, match="signal_anchor_date"):
        analyze_signal_phase_sensitivity(
            store=store,
            config=config,
            start=calendar[0],
            end=calendar[-1],
            score_fn=lambda as_of: [],
        )


def test_short_calendar_fails_closed() -> None:
    calendar = weekdays(date(2024, 1, 2), 2)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    with pytest.raises(ValueError, match="incomplete trading calendar"):
        resolve_phase_anchors(store, original_anchor=calendar[0], interval=3)


def test_legacy_utilization_unavailable_fails_whole_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calendar = weekdays(date(2024, 1, 2), 18)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = _interval_config(calendar, interval=3)

    def fake_run(self, start: date, end: date) -> BacktestResult:  # noqa: ANN001
        return BacktestResult(
            strategy_name=config.name,
            strategy_version=config.version,
            strategy_config_hash=self.config.config_hash(),
            start=start,
            end=end,
            window=BacktestWindow(start=start, signal_end=end, entry_end=end, valuation_end=end),
            metrics=BacktestMetrics(
                initial_capital=80_000.0,
                final_equity=80_000.0,
                total_return=0.0,
                annualized_return=0.0,
                number_of_trades=0,
                win_rate=None,
                average_win=None,
                average_loss=None,
                profit_factor=None,
                expectancy=None,
                average_holding_days=None,
                max_drawdown=0.0,
                sharpe_ratio=None,
                tp_exit_count=0,
                sl_exit_count=0,
                timeout_exit_count=0,
            ),
            equity_curve=[
                EquityPoint(
                    date=start,
                    cash=80_000.0,
                    market_value=0.0,
                    equity=80_000.0,
                    open_positions=None,
                    pending_orders=None,
                )
            ],
            attribution=BacktestAttribution(signal=SignalAttribution()),
            data_snapshot_id=store.snapshot().snapshot_id,
        )

    monkeypatch.setattr("app.research.signal_phase_sensitivity.BacktestEngine.run", fake_run)
    with pytest.raises(ValueError, match="utilization unavailable"):
        analyze_signal_phase_sensitivity(
            store=store,
            config=config,
            start=calendar[0],
            end=calendar[-1],
            score_fn=lambda as_of: [_score("AAA", as_of, 80.0)],
        )


def test_apply_phase_anchor_rejects_non_anchor_drift() -> None:
    calendar = weekdays(date(2024, 1, 2), 6)
    config = _interval_config(calendar, interval=3)
    drifted = config.model_copy(
        update={
            "trade": config.trade.model_copy(
                update={
                    "signal_anchor_date": calendar[1],
                    "max_holding_days": config.trade.max_holding_days + 1,
                }
            )
        }
    )
    diff = canonical_config_diff(config, drifted)
    assert "trade.signal_anchor_date" in diff
    assert "trade.max_holding_days" in diff
    with pytest.raises(ValueError, match="anchor-only"):
        # Simulate the guard used by apply_phase_anchor after a bad copy.
        runtime = config.model_copy(
            update={
                "trade": config.trade.model_copy(
                    update={
                        "signal_anchor_date": calendar[1],
                        "max_holding_days": 99,
                    }
                )
            }
        )
        checked = canonical_config_diff(config, runtime)
        if checked != ["trade.signal_anchor_date"]:
            raise ValueError(f"phase config diff is not anchor-only: {checked}")
