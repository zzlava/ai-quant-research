from __future__ import annotations

from datetime import date

from app.backtest.costs import buy_cost, commission
from app.backtest.engine import BacktestEngine
from app.models.config import CostConfig
from tests.helpers import (
    constant_signal,
    fill_quiet_bars,
    load_test_config,
    store_from_rows,
    weekdays,
)


def test_min_commission_overrides_rate() -> None:
    costs = CostConfig(commission_rate=0.00025, min_commission=5.0)
    assert 1000 * 0.00025 == 0.25
    assert commission(1000, costs) == 5.0
    _, comm = buy_cost(10.0, 100, costs)
    assert comm == 5.0


def test_backtest_charges_minimum_commission_both_sides() -> None:
    calendar = weekdays(date(2024, 1, 2), 16)
    signal_day, buy_day, exit_day = calendar[0], calendar[1], calendar[2]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.98, "close": 10.0},
        exit_day: {"open": 10.0, "high": 10.40, "low": 9.90, "close": 10.2},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))
    config = load_test_config()
    config.costs.commission_rate = 0.00025
    config.costs.min_commission = 5.0
    config.costs.stamp_tax_rate = 0.0
    config.costs.slippage_bps = 0.0
    config.portfolio.initial_cash = 2_000
    config.portfolio.max_positions = 1

    def signals(as_of: date):
        return constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else []

    result = BacktestEngine(store, config, signal_fn=signals).run(signal_day, exit_day)
    trade = result.trades[0]
    assert trade.shares == 100
    assert trade.buy_commission == 5.0
    assert trade.sell_commission == 5.0
    expected_pnl = (10.3 * 100 - 5.0) - (10.0 * 100 + 5.0)
    assert abs(trade.pnl - expected_pnl) < 1e-9
