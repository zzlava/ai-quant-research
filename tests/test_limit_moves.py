from __future__ import annotations

from datetime import date

import pytest

from app.backtest.engine import BacktestEngine
from app.models.config import TradeConfig
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


def test_open_at_published_limit_up_blocks_buy_even_when_not_one_word_board() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, buy_day = calendar[0], calendar[1]
    overrides = {
        buy_day: {"open": 11.0, "high": 11.20, "low": 10.80, "close": 11.05},
    }
    rows = fill_quiet_bars("AAA", calendar, overrides)
    for row in rows:
        if row["date"] == buy_day:
            row["up_limit"] = 11.0
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()
    config.portfolio.max_positions = 1

    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else [],
    ).run(signal_day, buy_day)

    assert result.open_positions_at_end == 0
    assert result.attribution.signal.orders_deferred == 0


def test_limit_up_entry_can_be_deferred_until_next_tradable_day() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, locked_day, tradable_day = calendar[:3]
    rows = fill_quiet_bars(
        "AAA",
        calendar,
        {
            locked_day: {"open": 11.0, "high": 11.20, "low": 10.80, "close": 11.05},
            tradable_day: {"open": 10.80, "high": 11.0, "low": 10.7, "close": 10.9},
        },
    )
    for row in rows:
        if row["date"] == locked_day:
            row["up_limit"] = 11.0
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()
    config.portfolio.max_positions = 1
    trade = config.trade.model_dump()
    trade.update({"blocked_entry_policy": "defer", "max_entry_delay_days": 2})
    config.trade = TradeConfig.model_validate(trade)

    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of)
        if as_of == signal_day
        else [],
    ).run(signal_day, tradable_day)

    assert result.open_positions_at_end == 1
    assert result.attribution.signal.orders_generated == 1
    assert result.attribution.signal.orders_deferred == 1
    assert result.attribution.signal.entry_deferral_days == 1
    assert result.attribution.signal.orders_filled_after_deferral == 1
    assert result.attribution.signal.deferred_orders_expired == 0


def test_deferred_entry_expires_after_configured_trading_days() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, first_locked_day, second_locked_day, end_day = calendar[:4]
    rows = fill_quiet_bars("AAA", calendar)
    for row in rows:
        if row["date"] in {first_locked_day, second_locked_day}:
            row.update(
                {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "up_limit": 11.0}
            )
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()
    config.portfolio.max_positions = 1
    trade = config.trade.model_dump()
    trade.update({"blocked_entry_policy": "defer", "max_entry_delay_days": 1})
    config.trade = TradeConfig.model_validate(trade)

    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of)
        if as_of == signal_day
        else [],
    ).run(signal_day, end_day)

    assert result.open_positions_at_end == 0
    assert result.attribution.signal.orders_deferred == 1
    assert result.attribution.signal.entry_deferral_days == 1
    assert result.attribution.signal.orders_filled_after_deferral == 0
    assert result.attribution.signal.deferred_orders_expired == 1


def test_deferred_entry_is_not_replaced_by_a_new_signal_for_same_symbol() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, locked_day, tradable_day = calendar[:3]
    rows = fill_quiet_bars("AAA", calendar)
    for row in rows:
        if row["date"] == locked_day:
            row.update(
                {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "up_limit": 11.0}
            )
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()
    config.portfolio.max_positions = 1
    trade = config.trade.model_dump()
    trade.update({"blocked_entry_policy": "defer", "max_entry_delay_days": 2})
    config.trade = TradeConfig.model_validate(trade)

    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of),
    ).run(signal_day, tradable_day)

    assert result.open_positions_at_end == 1
    assert result.attribution.signal.orders_generated == 1
    assert result.attribution.signal.orders_filled == 1


def test_suspended_entry_can_be_deferred_until_resumption() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, suspended_day, resumed_day = calendar[:3]
    rows = fill_quiet_bars("AAA", calendar)
    for row in rows:
        if row["date"] == suspended_day:
            row.update({"is_suspended": True, "volume": 0.0, "amount": 0.0})
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()
    config.portfolio.max_positions = 1
    trade = config.trade.model_dump()
    trade.update({"blocked_entry_policy": "defer", "max_entry_delay_days": 2})
    config.trade = TradeConfig.model_validate(trade)

    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of)
        if as_of == signal_day
        else [],
    ).run(signal_day, resumed_day)

    assert result.open_positions_at_end == 1
    assert result.attribution.signal.rejected_suspended == 1
    assert result.attribution.signal.orders_filled_after_deferral == 1


def test_missing_daily_bar_for_pending_entry_fails_closed() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, missing_day = calendar[:2]
    rows = [row for row in fill_quiet_bars("AAA", calendar) if row["date"] != missing_day]
    store = store_from_rows(calendar, rows)

    with pytest.raises(ValueError, match=f"pending entry AAA has no daily bar on {missing_day}"):
        BacktestEngine(
            store,
            zero_cost_config(),
            signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of)
            if as_of == signal_day
            else [],
        ).run(signal_day, calendar[2])


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
