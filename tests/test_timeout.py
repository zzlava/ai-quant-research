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


def test_timeout_exits_on_tenth_exit_eligible_close() -> None:
    calendar = weekdays(date(2024, 1, 2), 20)
    signal_day, buy_day = calendar[0], calendar[1]
    exit_eligible = calendar[2:12]
    timeout_day = exit_eligible[-1]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.10, "low": 9.95, "close": 10.02},
        timeout_day: {"open": 10.04, "high": 10.12, "low": 9.90, "close": 10.08},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))

    def signals(as_of: date):
        return constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else []

    result = BacktestEngine(store, zero_cost_config(), signal_fn=signals).run(signal_day, timeout_day)
    trade = result.trades[0]
    assert trade.exit_reason == "timeout"
    assert trade.exit_date == timeout_day
    assert trade.holding_days == 10
    assert abs(trade.exit_price - 10.08) < 1e-9
    assert result.metrics.timeout_exit_count == 1
