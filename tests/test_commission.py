from __future__ import annotations

from datetime import date

from app.backtest.costs import buy_cost, commission, sell_cost
from app.backtest.engine import BacktestEngine
from app.models.config import CostConfig
from tests.helpers import (
    constant_signal,
    fill_quiet_bars,
    load_test_config,
    store_from_rows,
    weekdays,
)


def test_commission_formula() -> None:
    costs = CostConfig(commission_rate=0.00025, min_commission=5.0, stamp_tax_rate=0.0005)
    assert commission(40_000, costs) == 10.0
    total, comm = buy_cost(10.0, 4000, costs)
    assert comm == 10.0
    assert total == 40_010.0
    net, sell_comm, tax = sell_cost(10.3, 4000, costs)
    assert sell_comm == 10.3
    assert abs(tax - 20.6) < 1e-9
    assert abs(net - (41200 - 10.3 - 20.6)) < 1e-9


def test_backtest_applies_commission_and_stamp_tax() -> None:
    calendar = weekdays(date(2024, 1, 2), 16)
    signal_day, buy_day, exit_day = calendar[0], calendar[1], calendar[2]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.98, "close": 10.0},
        exit_day: {"open": 10.0, "high": 10.40, "low": 9.90, "close": 10.2},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))
    config = load_test_config()
    config.costs.commission_rate = 0.00025
    config.costs.min_commission = 0.0
    config.costs.stamp_tax_rate = 0.0005
    config.costs.slippage_bps = 0.0
    config.portfolio.initial_cash = 40_100
    config.portfolio.max_positions = 1

    def signals(as_of: date):
        return constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else []

    result = BacktestEngine(store, config, signal_fn=signals).run(signal_day, signal_day)
    trade = result.trades[0]
    assert trade.shares == 4000
    assert abs(trade.buy_commission - 10.0) < 1e-9
    assert abs(trade.sell_commission - 10.3) < 1e-9
    assert abs(trade.stamp_tax - 20.6) < 1e-9
    expected_pnl = (10.3 * 4000 - 10.3 - 20.6) - (10.0 * 4000 + 10.0)
    assert abs(trade.pnl - expected_pnl) < 1e-6
