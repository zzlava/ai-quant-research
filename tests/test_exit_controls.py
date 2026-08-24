from __future__ import annotations

from datetime import date

from app.backtest.engine import BacktestEngine, OpenPosition
from app.models.backtest import TradeFill
from app.models.config import TradeConfig
from app.models.scores import ScoreResult
from tests.helpers import constant_signal, fill_quiet_bars, store_from_rows, weekdays, zero_cost_config


def test_min_holding_days_prevents_early_take_profit_exit() -> None:
    calendar = weekdays(date(2024, 1, 2), 16)
    signal_day, buy_day, first_exit_day, second_exit_day, allowed_exit_day = calendar[:5]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.95, "close": 10.0},
        first_exit_day: {"open": 10.0, "high": 10.50, "low": 9.90, "close": 10.2},
        second_exit_day: {"open": 10.0, "high": 10.50, "low": 9.90, "close": 10.2},
        allowed_exit_day: {"open": 10.0, "high": 10.50, "low": 9.90, "close": 10.2},
    }
    config = zero_cost_config()
    config.portfolio.max_positions = 1
    config.trade.min_holding_days = 3
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))

    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else [],
    ).run(signal_day, allowed_exit_day)

    assert len(result.trades) == 1
    assert result.trades[0].exit_date == allowed_exit_day
    assert result.trades[0].holding_days == 3


def test_atr_exit_uses_signal_volatility_scaled_barriers() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = zero_cost_config()
    config.trade.take_profit_atr = 2.0
    config.trade.stop_loss_atr = 1.5
    engine = BacktestEngine(store, config)
    take_profit, stop_loss = engine._barrier_prices(10.0, 0.02)
    assert take_profit == 10.4
    assert stop_loss == 9.7
    position = OpenPosition(
        symbol="AAA",
        entry_date=calendar[0],
        entry_price=10.0,
        entry_raw_price=10.0,
        shares=100,
        buy_commission=0.0,
        take_profit_price=take_profit,
        stop_loss_price=stop_loss,
    )
    assert engine._exit_decision(position, {"open": 10.0, "high": 10.5, "low": 9.9, "close": 10.2}) == (
        "take_profit",
        10.4,
    )


def test_target_lot_affordability_is_explicit() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, buy_day = calendar[:2]
    config = zero_cost_config()
    config.portfolio.max_positions = 3
    config.trade.require_target_lot_affordability = True
    store = store_from_rows(
        calendar,
        fill_quiet_bars("AAA", calendar, {buy_day: {"open": 300.0, "high": 301.0, "low": 299.0, "close": 300.0}}),
    )
    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda as_of: constant_signal(["AAA"], 80.0, as_of) if as_of == signal_day else [],
    ).run(signal_day, buy_day)
    assert result.open_positions_at_end == 0


def test_cooldown_blocks_configured_number_of_later_trading_days() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    config = zero_cost_config()
    config.trade.cooldown_days = 2
    engine = BacktestEngine(store_from_rows(calendar, fill_quiet_bars("AAA", calendar)), config)
    closed = TradeFill(
        symbol="AAA",
        entry_date=calendar[0],
        exit_date=calendar[1],
        entry_price=10.0,
        exit_price=10.0,
        shares=100,
        pnl=0.0,
        return_pct=0.0,
        holding_days=1,
        exit_reason="timeout",
        buy_commission=0.0,
        sell_commission=0.0,
        stamp_tax=0.0,
    )
    assert engine._cooldown_dates([closed]) == {"AAA": calendar[3]}


def test_fixed_horizon_ignores_barriers_until_timeout() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    overrides = {
        day: {"open": 10.0, "high": 20.0, "low": 5.0, "close": 10.0}
        for day in calendar
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))
    config = zero_cost_config()
    config = config.model_copy(
        update={
            "trade": config.trade.model_copy(
                update={
                    "exit_policy": "fixed_horizon",
                    "min_holding_days": 2,
                    "max_holding_days": 2,
                    "take_profit_atr": None,
                    "stop_loss_atr": None,
                }
            )
        }
    )
    engine = BacktestEngine(
        store,
        config,
        signal_fn=lambda day: constant_signal(["AAA"], 90.0, day),
    )

    result = engine.run(calendar[0], calendar[4])

    assert len(result.trades) == 1
    assert result.trades[0].entry_date == calendar[1]
    assert result.trades[0].exit_date == calendar[3]
    assert result.trades[0].exit_reason == "timeout"


def test_signal_interval_uses_explicit_trading_day_anchor() -> None:
    calendar = weekdays(date(2024, 1, 2), 10)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = zero_cost_config()
    config = config.model_copy(
        update={
            "trade": config.trade.model_copy(
                update={
                    "signal_interval_days": 3,
                    "signal_anchor_date": calendar[0],
                }
            )
        }
    )
    called: list[date] = []

    def signal(day: date) -> list[ScoreResult]:
        called.append(day)
        return constant_signal(["AAA"], 90.0, day)

    BacktestEngine(store, config, signal_fn=signal).run(calendar[0], calendar[8])

    assert called == [calendar[0], calendar[3], calendar[6]]


def test_blocked_entry_policy_requires_a_bounded_delay() -> None:
    payload = zero_cost_config().trade.model_dump()
    payload["blocked_entry_policy"] = "defer"
    payload["max_entry_delay_days"] = 0

    try:
        TradeConfig.model_validate(payload)
    except ValueError as exc:
        assert "max_entry_delay_days>=1" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("deferred entries must have a bounded delay")
