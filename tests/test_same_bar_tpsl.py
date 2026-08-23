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


def test_same_bar_tp_and_sl_uses_stop_loss_first() -> None:
    calendar = weekdays(date(2024, 1, 2), 16)
    signal_day, buy_day, exit_day = calendar[0], calendar[1], calendar[2]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.97, "close": 10.01},
        exit_day: {"open": 10.0, "high": 10.50, "low": 9.60, "close": 10.20},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))

    def signals(as_of: date):
        return constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else []

    result = BacktestEngine(store, zero_cost_config(), signal_fn=signals).run(signal_day, signal_day)
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert abs(trade.exit_price - 9.75) < 1e-9
    assert result.metrics.tp_exit_count == 0
    assert result.metrics.sl_exit_count == 1
