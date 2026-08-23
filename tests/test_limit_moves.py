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


def test_one_word_limit_down_blocks_exit_until_tradable() -> None:
    calendar = weekdays(date(2024, 1, 2), 16)
    signal_day, buy_day, lock_day, free_day = calendar[0], calendar[1], calendar[2], calendar[3]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.98, "close": 10.0},
        lock_day: {"open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0},
        free_day: {"open": 9.90, "high": 10.40, "low": 9.80, "close": 10.20},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))

    def signals(as_of: date):
        return constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else []

    result = BacktestEngine(store, zero_cost_config(), signal_fn=signals).run(signal_day, free_day)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_date == free_day
    assert trade.exit_reason == "take_profit"
