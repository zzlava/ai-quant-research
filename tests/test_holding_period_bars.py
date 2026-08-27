from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.backtest.engine import BacktestEngine, OpenPosition
from app.models.backtest import SignalAttribution
from tests.helpers import (
    constant_signal,
    fill_quiet_bars,
    store_from_rows,
    weekdays,
    zero_cost_config,
)


def test_missing_daily_bar_for_open_position_fails_closed() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, missing_day = calendar[0], calendar[2]
    rows = [row for row in fill_quiet_bars("AAA", calendar) if row["date"] != missing_day]
    store = store_from_rows(calendar, rows)

    with pytest.raises(ValueError, match=f"open position AAA has no daily bar on {missing_day}"):
        BacktestEngine(
            store,
            zero_cost_config(),
            signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else [],
        ).run(signal_day, calendar[3])


def test_mark_to_market_does_not_fall_back_to_entry_price() -> None:
    calendar = weekdays(date(2024, 1, 2), 4)
    day = calendar[0]
    engine = BacktestEngine(store_from_rows(calendar, fill_quiet_bars("AAA", calendar)), zero_cost_config())
    positions = {
        "AAA": OpenPosition(
            symbol="AAA",
            entry_date=day,
            entry_price=99.0,
            entry_raw_price=99.0,
            shares=100,
            buy_commission=0.0,
        )
    }
    with pytest.raises(ValueError, match=f"open position AAA has no daily bar on {day}"):
        engine._mark_to_market(positions, {}, day)


@pytest.mark.parametrize("bad_close", [float("nan"), float("inf"), 0.0, -1.0])
def test_mark_to_market_rejects_non_finite_or_non_positive_close(bad_close: float) -> None:
    calendar = weekdays(date(2024, 1, 2), 4)
    day = calendar[0]
    engine = BacktestEngine(store_from_rows(calendar, fill_quiet_bars("AAA", calendar)), zero_cost_config())
    positions = {
        "AAA": OpenPosition(
            symbol="AAA",
            entry_date=day,
            entry_price=10.0,
            entry_raw_price=10.0,
            shares=100,
            buy_commission=0.0,
        )
    }
    with pytest.raises(ValueError, match=f"open position AAA has invalid close on {day}"):
        engine._mark_to_market(positions, {"AAA": {"close": bad_close}}, day)


def test_missing_bar_for_non_held_symbol_does_not_fail() -> None:
    calendar = weekdays(date(2024, 1, 2), 6)
    signal_day, buy_day = calendar[0], calendar[1]
    aaa = fill_quiet_bars("AAA", calendar)
    bbb = [row for row in fill_quiet_bars("BBB", calendar) if row["date"] == buy_day]
    store = store_from_rows(calendar, aaa + bbb)
    config = zero_cost_config()
    config.portfolio.max_positions = 1

    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else [],
    ).run(signal_day, calendar[3])

    assert result.open_positions_at_end == 1
    assert all(point.open_positions == 1 for point in result.equity_curve if point.date >= buy_day)


def test_suspended_open_position_is_forced_held_and_marked_at_close() -> None:
    calendar = weekdays(date(2024, 1, 2), 10)
    signal_day, buy_day, halt_day, free_day = calendar[0], calendar[1], calendar[2], calendar[3]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.98, "close": 10.0},
        halt_day: {"open": 10.5, "high": 10.5, "low": 10.5, "close": 10.5},
        free_day: {"open": 10.0, "high": 10.40, "low": 9.90, "close": 10.20},
    }
    rows = fill_quiet_bars("AAA", calendar, overrides)
    for row in rows:
        if row["date"] == halt_day:
            row.update({"is_suspended": True, "volume": 0.0, "amount": 0.0})
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()
    config.portfolio.max_positions = 1

    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else [],
    ).run(signal_day, free_day)

    assert result.attribution.signal.exit_blocked_suspended_days == 1
    assert not any(trade.exit_date == halt_day for trade in result.trades)
    assert len(result.trades) == 1
    assert result.trades[0].exit_date == free_day
    halt_point = next(point for point in result.equity_curve if point.date == halt_day)
    assert halt_point.open_positions == 1
    assert abs(halt_point.market_value - 10.5 * result.trades[0].shares) < 1e-9


def test_limit_down_blocks_exit_increments_counter_then_exits_when_tradable() -> None:
    calendar = weekdays(date(2024, 1, 2), 16)
    signal_day, buy_day, lock_day, free_day = calendar[0], calendar[1], calendar[2], calendar[3]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.98, "close": 10.0},
        lock_day: {"open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0},
        free_day: {"open": 9.90, "high": 10.40, "low": 9.80, "close": 10.20},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))

    result = BacktestEngine(
        store,
        zero_cost_config(),
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else [],
    ).run(signal_day, free_day)

    assert result.attribution.signal.exit_blocked_limit_down_days == 1
    assert result.attribution.signal.exit_blocked_suspended_days == 0
    assert len(result.trades) == 1
    assert result.trades[0].exit_date == free_day
    assert result.trades[0].exit_reason == "take_profit"


def test_signal_attribution_exit_block_fields_default_zero_and_reject_negative() -> None:
    parsed = SignalAttribution.model_validate({})
    assert parsed.exit_blocked_suspended_days == 0
    assert parsed.exit_blocked_limit_down_days == 0
    for field in ("exit_blocked_suspended_days", "exit_blocked_limit_down_days"):
        with pytest.raises(ValidationError):
            SignalAttribution.model_validate({field: -1})


def _fixed_horizon_holding_config(holding_days: int = 3):
    config = zero_cost_config()
    config.portfolio.max_positions = 1
    return config.model_copy(
        update={
            "trade": config.trade.model_copy(
                update={
                    "exit_policy": "fixed_horizon",
                    "min_holding_days": holding_days,
                    "max_holding_days": holding_days,
                    "take_profit_atr": None,
                    "stop_loss_atr": None,
                }
            )
        }
    )


def test_fixed_horizon_pre_timeout_suspension_does_not_increment_blocked_counter() -> None:
    calendar = weekdays(date(2024, 1, 2), 12)
    signal_day, buy_day, early_halt, timeout_day = calendar[0], calendar[1], calendar[2], calendar[4]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.98, "close": 10.0},
        early_halt: {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0},
    }
    rows = fill_quiet_bars("AAA", calendar, overrides)
    for row in rows:
        if row["date"] == early_halt:
            row.update({"is_suspended": True, "volume": 0.0, "amount": 0.0})
    store = store_from_rows(calendar, rows)

    result = BacktestEngine(
        store,
        _fixed_horizon_holding_config(3),
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else [],
    ).run(signal_day, timeout_day)

    assert result.attribution.signal.exit_blocked_suspended_days == 0
    assert len(result.trades) == 1
    assert result.trades[0].exit_date == timeout_day
    assert result.trades[0].exit_reason == "timeout"


def test_fixed_horizon_timeout_suspension_increments_then_exits_on_resume() -> None:
    calendar = weekdays(date(2024, 1, 2), 12)
    signal_day, buy_day, timeout_halt, free_day = calendar[0], calendar[1], calendar[4], calendar[5]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.98, "close": 10.0},
        timeout_halt: {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0},
        free_day: {"open": 10.0, "high": 10.05, "low": 9.95, "close": 10.0},
    }
    rows = fill_quiet_bars("AAA", calendar, overrides)
    for row in rows:
        if row["date"] == timeout_halt:
            row.update({"is_suspended": True, "volume": 0.0, "amount": 0.0})
    store = store_from_rows(calendar, rows)

    result = BacktestEngine(
        store,
        _fixed_horizon_holding_config(3),
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else [],
    ).run(signal_day, free_day)

    assert result.attribution.signal.exit_blocked_suspended_days == 1
    assert not any(trade.exit_date == timeout_halt for trade in result.trades)
    assert len(result.trades) == 1
    assert result.trades[0].exit_date == free_day
    assert result.trades[0].exit_reason == "timeout"


def test_fixed_horizon_pre_timeout_limit_down_does_not_increment_blocked_counter() -> None:
    calendar = weekdays(date(2024, 1, 2), 12)
    signal_day, buy_day, early_lock, timeout_day = calendar[0], calendar[1], calendar[2], calendar[4]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.98, "close": 10.0},
        early_lock: {"open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))

    result = BacktestEngine(
        store,
        _fixed_horizon_holding_config(3),
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else [],
    ).run(signal_day, timeout_day)

    assert result.attribution.signal.exit_blocked_limit_down_days == 0
    assert len(result.trades) == 1
    assert result.trades[0].exit_date == timeout_day
    assert result.trades[0].exit_reason == "timeout"


def test_fixed_horizon_timeout_limit_down_increments_then_exits_when_tradable() -> None:
    calendar = weekdays(date(2024, 1, 2), 12)
    signal_day, buy_day, timeout_lock, free_day = calendar[0], calendar[1], calendar[4], calendar[5]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.98, "close": 10.0},
        timeout_lock: {"open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0},
        free_day: {"open": 9.90, "high": 9.95, "low": 9.85, "close": 9.90},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))

    result = BacktestEngine(
        store,
        _fixed_horizon_holding_config(3),
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else [],
    ).run(signal_day, free_day)

    assert result.attribution.signal.exit_blocked_limit_down_days == 1
    assert result.attribution.signal.exit_blocked_suspended_days == 0
    assert not any(trade.exit_date == timeout_lock for trade in result.trades)
    assert len(result.trades) == 1
    assert result.trades[0].exit_date == free_day
    assert result.trades[0].exit_reason == "timeout"
