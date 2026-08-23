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


def test_gap_down_stop_fills_at_open_not_stop_line() -> None:
    calendar = weekdays(date(2024, 1, 2), 16)
    signal_day, buy_day, exit_day = calendar[0], calendar[1], calendar[2]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.97, "close": 10.0},
        exit_day: {"open": 9.0, "high": 9.20, "low": 8.80, "close": 9.10},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))

    def signals(as_of: date):
        return constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else []

    result = BacktestEngine(store, zero_cost_config(), signal_fn=signals).run(signal_day, exit_day)
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert abs(trade.exit_price - 9.0) < 1e-9
    assert trade.exit_price < 9.75
