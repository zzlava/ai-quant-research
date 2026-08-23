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


def test_take_profit_fills_at_target() -> None:
    calendar = weekdays(date(2024, 1, 2), 16)
    signal_day, buy_day, exit_day = calendar[0], calendar[1], calendar[2]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.10, "low": 9.95, "close": 10.02},
        exit_day: {"open": 10.05, "high": 10.40, "low": 9.90, "close": 10.20},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))
    config = zero_cost_config()

    def signals(as_of: date):
        return constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else []

    result = BacktestEngine(store, config, signal_fn=signals).run(signal_day, signal_day)
    assert result.trades[0].exit_reason == "take_profit"
    assert result.trades[0].exit_date == exit_day
    assert abs(result.trades[0].exit_price - 10.0 * 1.03) < 1e-9
    assert result.metrics.tp_exit_count == 1
