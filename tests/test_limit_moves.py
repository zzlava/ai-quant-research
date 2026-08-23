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


def test_price_limit_pct_20_blocks_buy_on_limit_up() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, buy_day = calendar[0], calendar[1]
    overrides = {
        buy_day: {"open": 12.0, "high": 12.0, "low": 12.0, "close": 12.0, "price_limit_pct": 0.20},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))

    def signals(as_of: date):
        return constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else []

    result = BacktestEngine(store, zero_cost_config(), signal_fn=signals).run(signal_day, calendar[3])
    assert result.trades == []
    assert result.open_positions_at_end == 0


def test_price_limit_pct_20_blocks_sell_on_limit_down() -> None:
    calendar = weekdays(date(2024, 1, 2), 10)
    signal_day, buy_day, lock_day, free_day = calendar[0], calendar[1], calendar[2], calendar[3]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.98, "close": 10.0},
        lock_day: {"open": 8.0, "high": 8.0, "low": 8.0, "close": 8.0, "price_limit_pct": 0.20},
        free_day: {"open": 9.90, "high": 10.40, "low": 9.80, "close": 10.20},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))

    def signals(as_of: date):
        return constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else []

    result = BacktestEngine(store, zero_cost_config(), signal_fn=signals).run(signal_day, free_day)
    assert len(result.trades) == 1
    assert result.trades[0].exit_date == free_day
    assert result.trades[0].exit_reason == "take_profit"


def test_null_price_limit_pct_does_not_apply_ten_percent_block() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, buy_day, next_day = calendar[0], calendar[1], calendar[2]
    overrides = {
        buy_day: {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "price_limit_pct": None},
        next_day: {"open": 11.0, "high": 11.40, "low": 10.90, "close": 11.20, "price_limit_pct": None},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))

    def signals(as_of: date):
        return constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else []

    result = BacktestEngine(store, zero_cost_config(), signal_fn=signals).run(signal_day, next_day)
    assert result.open_positions_at_end == 1 or result.trades
    if result.trades:
        assert result.trades[0].entry_date == buy_day
    else:
        assert result.open_positions_at_end == 1
