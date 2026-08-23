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


def test_cannot_sell_on_buy_day() -> None:
    calendar = weekdays(date(2024, 1, 2), 16)
    signal_day, buy_day, next_day = calendar[0], calendar[1], calendar[2]
    overrides = {
        buy_day: {"open": 10.0, "high": 20.0, "low": 1.0, "close": 10.0},
        next_day: {"open": 10.0, "high": 20.0, "low": 1.0, "close": 9.0},
    }
    rows = fill_quiet_bars("AAA", calendar, overrides)
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()

    def signals(as_of: date):
        if as_of == signal_day:
            return constant_signal(["AAA"], market_score=80.0, as_of=as_of)
        return []

    result = BacktestEngine(store, config, signal_fn=signals).run(signal_day, signal_day)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_date == buy_day
    assert trade.exit_date == next_day
    assert trade.exit_reason == "stop_loss"
    assert trade.entry_date != trade.exit_date
