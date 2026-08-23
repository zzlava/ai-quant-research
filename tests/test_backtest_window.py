from __future__ import annotations

from datetime import date

from app.backtest.engine import BacktestEngine
from tests.helpers import (
    constant_signal,
    fill_quiet_bars,
    store_from_rows,
    weekdays,
    zero_cost_config,
)


def test_metrics_stop_at_declared_end_and_drop_unfilled_end_signal() -> None:
    calendar = weekdays(date(2024, 1, 2), 20)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))

    def signals(as_of: date):
        return constant_signal(["AAA"], 80.0, as_of)

    result = BacktestEngine(store, zero_cost_config(), signal_fn=signals).run(calendar[0], calendar[0])
    assert result.window.valuation_end == calendar[0]
    assert result.window.entry_end == calendar[0]
    assert result.window.signal_end is None
    assert result.equity_curve[-1].date == calendar[0]
    assert all(point.date <= calendar[0] for point in result.equity_curve)
    assert result.metrics.number_of_trades == 0
    assert result.open_positions_at_end == 0


def test_signal_is_kept_only_if_entry_is_on_or_before_end() -> None:
    calendar = weekdays(date(2024, 1, 2), 20)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))

    def signals(as_of: date):
        return constant_signal(["AAA"], 80.0, as_of) if as_of == calendar[0] else []

    result = BacktestEngine(store, zero_cost_config(), signal_fn=signals).run(calendar[0], calendar[1])
    assert result.window.signal_end == calendar[0]
    assert result.window.entry_end == calendar[1]
    assert result.window.valuation_end == calendar[1]
    assert result.open_positions_at_end == 1
    assert result.metrics.number_of_trades == 0
    assert result.equity_curve[-1].date == calendar[1]
    assert all(point.date <= calendar[1] for point in result.equity_curve)
