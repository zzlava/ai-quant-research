from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.backtest.costs import apply_slippage, buy_cost, shares_affordable
from app.backtest.engine import BacktestEngine
from app.models.backtest import (
    BacktestAttribution,
    BacktestMetrics,
    BacktestResult,
    BacktestWindow,
    EquityPoint,
    SignalAttribution,
)
from app.models.config import TradeConfig
from app.models.scores import ScoreBreakdown, ScoreResult
from app.research.position_utilization import summarize_position_utilization
from app.universe.membership import build_manual_static_membership
from tests.helpers import (
    constant_signal,
    fill_quiet_bars,
    store_from_rows,
    weekdays,
    zero_cost_config,
)


def _score(symbol: str, as_of: date, final: float, market: float = 80.0) -> ScoreResult:
    return ScoreResult(
        symbol=symbol,
        score_date=as_of,
        strategy_name="baseline_v1",
        strategy_version="1.0.0",
        strategy_config_hash="test",
        final_score=final,
        breakdown=ScoreBreakdown(
            market_score=market,
            global_score=60.0,
            sector_score=60.0,
            alpha_score=70.0,
            crowding_risk=10.0,
            execution_risk=10.0,
            final_score=final,
        ),
    )


def test_empty_ranking_day_counts_scheduled_but_not_scoring() -> None:
    calendar = weekdays(date(2024, 1, 2), 6)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = zero_cost_config()
    config = config.model_copy(
        update={
            "trade": config.trade.model_copy(
                update={"signal_interval_days": 3, "signal_anchor_date": calendar[0]}
            )
        }
    )
    called: list[date] = []

    def signals(as_of: date) -> list[ScoreResult]:
        called.append(as_of)
        return []

    result = BacktestEngine(store, config, signal_fn=signals).run(calendar[0], calendar[5])
    signal = result.attribution.signal
    assert called == [calendar[0], calendar[3]]
    assert signal.scheduled_signal_days == 2
    assert signal.empty_ranking_days == 2
    assert signal.scoring_days == 0
    assert signal.orders_generated == 0


def test_regime_gate_and_capacity_are_attributed_separately() -> None:
    calendar = weekdays(date(2024, 1, 2), 10)
    rows: list[dict[str, object]] = []
    for symbol in ("AAA", "BBB", "CCC"):
        rows.extend(fill_quiet_bars(symbol, calendar))
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()
    config.portfolio.max_positions = 1

    def regime_block(as_of: date) -> list[ScoreResult]:
        if as_of != calendar[0]:
            return []
        return constant_signal(["AAA", "BBB"], 30.0, as_of)

    blocked = BacktestEngine(store, config, signal_fn=regime_block).run(calendar[0], calendar[4])
    assert blocked.attribution.signal.regime_blocked_days == 1
    assert blocked.attribution.signal.rejected_by_regime_gate == 2
    assert blocked.attribution.signal.capacity_blocked_days == 0
    assert blocked.attribution.signal.rejected_by_capacity == 0

    def fill_then_capacity(as_of: date) -> list[ScoreResult]:
        if as_of == calendar[0]:
            return constant_signal(["AAA"], 80.0, as_of)
        if as_of == calendar[2]:
            return constant_signal(["BBB", "CCC"], 80.0, as_of)
        return []

    full = BacktestEngine(store, config, signal_fn=fill_then_capacity).run(
        calendar[0], calendar[5]
    )
    assert full.open_positions_at_end == 1
    assert full.attribution.signal.capacity_blocked_days == 1
    assert full.attribution.signal.rejected_by_capacity == 2
    assert full.attribution.signal.regime_blocked_days == 0


def test_held_membership_cooldown_and_order_limit_attribution() -> None:
    calendar = weekdays(date(2024, 1, 2), 12)
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    membership = build_manual_static_membership(
        ["AAA", "BBB", "CCC", "DDD"], calendar, universe_id="demo"
    )
    signal_day = calendar[0]
    second_signal = calendar[5]
    overrides = {
        calendar[1]: {"open": 10.0, "high": 10.05, "low": 9.95, "close": 10.0},
        calendar[2]: {"open": 10.0, "high": 10.50, "low": 9.90, "close": 10.2},
    }
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        rows.extend(fill_quiet_bars(symbol, calendar, overrides if symbol == "AAA" else None))
    store = store_from_rows(calendar, rows, membership=membership)
    config = zero_cost_config()
    config.portfolio.max_positions = 1
    config.trade.cooldown_days = 5
    config.trade.min_holding_days = 1

    def signals(as_of: date) -> list[ScoreResult]:
        if as_of == signal_day:
            return [_score("AAA", as_of, 90.0), _score("BBB", as_of, 89.0)]
        if as_of == second_signal:
            return [
                _score("AAA", as_of, 95.0),
                _score("EEE", as_of, 94.0),
                _score("BBB", as_of, 93.0),
                _score("CCC", as_of, 92.0),
                _score("DDD", as_of, 91.0),
            ]
        return []

    result = BacktestEngine(store, config, signal_fn=signals).run(calendar[0], calendar[8])
    signal = result.attribution.signal
    assert signal.rejected_by_cooldown >= 1
    assert signal.rejected_not_in_membership >= 1
    assert signal.not_evaluated_after_order_limit >= 1
    assert signal.rejected_by_ranking_threshold == 0


def test_already_held_or_pending_is_counted() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    rows: list[dict[str, object]] = []
    for symbol in ("AAA", "BBB", "CCC"):
        rows.extend(fill_quiet_bars(symbol, calendar))
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()
    config.portfolio.max_positions = 3
    signal_day = calendar[0]
    second_day = calendar[2]

    def signals(as_of: date) -> list[ScoreResult]:
        if as_of == signal_day:
            return constant_signal(["AAA", "BBB"], 80.0, as_of)
        if as_of == second_day:
            # AAA/BBB already held; free slot remains so capacity does not short-circuit.
            return constant_signal(["AAA", "BBB", "CCC"], 80.0, as_of)
        return []

    result = BacktestEngine(store, config, signal_fn=signals).run(calendar[0], calendar[4])
    signal = result.attribution.signal
    assert signal.rejected_already_held_or_pending >= 2
    assert signal.orders_generated >= 3


def test_not_evaluated_after_order_limit_is_not_ranking_reject() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    rows: list[dict[str, object]] = []
    for symbol in ("AAA", "BBB", "CCC", "DDD"):
        rows.extend(fill_quiet_bars(symbol, calendar))
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()
    config.portfolio.max_positions = 1

    def signals(as_of: date) -> list[ScoreResult]:
        if as_of != calendar[0]:
            return []
        return [
            _score("AAA", as_of, 90.0),
            _score("BBB", as_of, 80.0),
            _score("CCC", as_of, 70.0),
            _score("DDD", as_of, 60.0),
        ]

    result = BacktestEngine(store, config, signal_fn=signals).run(calendar[0], calendar[3])
    signal = result.attribution.signal
    assert signal.orders_generated == 1
    assert signal.not_evaluated_after_order_limit == 3
    assert signal.rejected_by_ranking_threshold == 0


def test_correlation_cap_still_counted_when_evaluated() -> None:
    calendar = weekdays(date(2024, 1, 2), 40)
    rows: list[dict[str, object]] = []
    for symbol in ("AAA", "BBB"):
        for day in calendar:
            rows.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "open": 10.0,
                    "high": 10.05,
                    "low": 9.95,
                    "close": 10.0,
                    "adj_close": 10.0,
                    "volume": 12_000_000,
                    "amount": 200_000_000,
                    "turnover_rate": 0.03,
                    "is_st": False,
                    "is_suspended": False,
                    "price_limit_pct": 0.10,
                }
            )
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()
    config.portfolio.max_positions = 2
    config.portfolio.max_pairwise_correlation = 0.5
    config.portfolio.correlation_lookback_days = 20
    signal_day = calendar[25]

    def signals(as_of: date) -> list[ScoreResult]:
        if as_of != signal_day:
            return []
        return [_score("AAA", as_of, 90.0), _score("BBB", as_of, 89.0)]

    result = BacktestEngine(store, config, signal_fn=signals).run(calendar[20], calendar[30])
    assert result.attribution.signal.rejected_by_correlation_cap >= 1


def test_execution_rejects_are_mutually_exclusive() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day = calendar[0]
    buy_day = calendar[1]

    rows = fill_quiet_bars("AAA", calendar)
    for row in rows:
        if row["date"] == buy_day:
            row.update({"is_suspended": True, "volume": 0.0, "amount": 0.0})
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()
    config.portfolio.max_positions = 1
    trade = config.trade.model_dump()
    trade.update({"blocked_entry_policy": "defer", "max_entry_delay_days": 2})
    config.trade = TradeConfig.model_validate(trade)
    suspended = BacktestEngine(
        store,
        config,
        signal_fn=lambda d: constant_signal(["AAA"], 80.0, d) if d == signal_day else [],
    ).run(signal_day, buy_day)
    assert suspended.attribution.signal.entry_attempts == 1
    assert suspended.attribution.signal.rejected_suspended == 1
    assert suspended.attribution.signal.rejected_at_limit == 0
    assert suspended.attribution.signal.rejected_unaffordable == 0
    assert suspended.attribution.signal.rejected_insufficient_cash == 0

    rows = fill_quiet_bars("AAA", calendar)
    for row in rows:
        if row["date"] == buy_day:
            row.update({"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "up_limit": 11.0})
    store = store_from_rows(calendar, rows)
    limited = BacktestEngine(
        store,
        config,
        signal_fn=lambda d: constant_signal(["AAA"], 80.0, d) if d == signal_day else [],
    ).run(signal_day, buy_day)
    assert limited.attribution.signal.entry_attempts == 1
    assert limited.attribution.signal.rejected_at_limit == 1
    assert limited.attribution.signal.rejected_suspended == 0

    config2 = zero_cost_config()
    config2.portfolio.max_positions = 3
    config2.trade.require_target_lot_affordability = True
    store = store_from_rows(
        calendar,
        fill_quiet_bars(
            "AAA", calendar, {buy_day: {"open": 300.0, "high": 301.0, "low": 299.0, "close": 300.0}}
        ),
    )
    unaffordable = BacktestEngine(
        store,
        config2,
        signal_fn=lambda d: constant_signal(["AAA"], 80.0, d) if d == signal_day else [],
    ).run(signal_day, buy_day)
    assert unaffordable.attribution.signal.entry_attempts == 1
    assert unaffordable.attribution.signal.rejected_unaffordable == 1
    assert unaffordable.attribution.signal.rejected_insufficient_cash == 0

    config3 = zero_cost_config()
    config3.portfolio.initial_cash = 500.0
    config3.portfolio.max_positions = 1
    store = store_from_rows(
        calendar,
        fill_quiet_bars(
            "AAA", calendar, {buy_day: {"open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0}}
        ),
    )
    cashless = BacktestEngine(
        store,
        config3,
        signal_fn=lambda d: constant_signal(["AAA"], 80.0, d) if d == signal_day else [],
    ).run(signal_day, buy_day)
    assert cashless.attribution.signal.entry_attempts == 1
    assert cashless.attribution.signal.rejected_insufficient_cash == 1
    assert cashless.attribution.signal.rejected_unaffordable == 0
    assert cashless.open_positions_at_end == 0


def test_successful_fill_budget_identity_includes_commission_and_slippage() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, buy_day = calendar[:2]
    config = zero_cost_config()
    config.portfolio.max_positions = 1
    config.portfolio.initial_cash = 80_000
    config.costs.commission_rate = 0.00025
    config.costs.min_commission = 5.0
    config.costs.slippage_bps = 10.0
    store = store_from_rows(
        calendar,
        fill_quiet_bars(
            "AAA", calendar, {buy_day: {"open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0}}
        ),
    )
    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda d: constant_signal(["AAA"], 80.0, d) if d == signal_day else [],
    ).run(signal_day, buy_day)
    signal = result.attribution.signal
    assert signal.orders_filled == 1
    assert signal.entry_attempts == 1
    assert abs(
        signal.target_entry_budget_total
        + signal.overallocated_entry_budget_total
        - signal.actual_entry_cash_used_total
        - signal.unallocated_entry_budget_total
    ) < 1e-9
    assert signal.unallocated_entry_budget_total >= 0.0
    assert signal.overallocated_entry_budget_total == 0.0
    raw_open = 10.0
    fill_price = apply_slippage(raw_open, config.costs, "buy")
    equity = config.portfolio.initial_cash
    allocation = min(equity, equity / config.portfolio.max_positions)
    expected_shares = shares_affordable(allocation, raw_open, config.costs)
    expected_total, _ = buy_cost(fill_price, expected_shares, config.costs)
    assert abs(signal.actual_entry_cash_used_total - expected_total) < 1e-9
    assert abs(signal.target_entry_budget_total - allocation) < 1e-9


def test_whole_lot_remainder_keeps_unallocated_non_negative() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, buy_day = calendar[:2]
    config = zero_cost_config()
    config.portfolio.max_positions = 3
    config.portfolio.initial_cash = 80_000
    assert config.trade.require_target_lot_affordability is False
    store = store_from_rows(
        calendar,
        fill_quiet_bars(
            "AAA", calendar, {buy_day: {"open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0}}
        ),
    )
    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda d: constant_signal(["AAA"], 80.0, d) if d == signal_day else [],
    ).run(signal_day, buy_day)
    signal = result.attribution.signal
    assert signal.orders_filled == 1
    raw_open = 10.0
    allocation = min(80_000.0, 80_000.0 / 3)
    expected_shares = shares_affordable(allocation, raw_open, config.costs)
    expected_total, _ = buy_cost(raw_open, expected_shares, config.costs)
    assert expected_shares > 0
    assert expected_total < allocation
    assert abs(signal.target_entry_budget_total - allocation) < 1e-9
    assert abs(signal.actual_entry_cash_used_total - expected_total) < 1e-9
    assert signal.unallocated_entry_budget_total >= 0.0
    assert abs(signal.unallocated_entry_budget_total - (allocation - expected_total)) < 1e-9
    assert signal.overallocated_entry_budget_total == 0.0
    assert abs(
        signal.target_entry_budget_total
        + signal.overallocated_entry_budget_total
        - signal.actual_entry_cash_used_total
        - signal.unallocated_entry_budget_total
    ) < 1e-9
    summary = summarize_position_utilization(result, max_positions=3)
    assert summary.overallocated_entry_budget_total == 0.0
    assert summary.budget_utilization is not None
    assert summary.budget_utilization < 1.0


def test_high_price_cash_fallback_records_overallocated_not_negative_unallocated() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    signal_day, buy_day = calendar[:2]
    config = zero_cost_config()
    config.portfolio.max_positions = 2
    config.portfolio.initial_cash = 30_000
    assert config.trade.require_target_lot_affordability is False
    raw_open = 200.0
    store = store_from_rows(
        calendar,
        fill_quiet_bars(
            "AAA",
            calendar,
            {buy_day: {"open": raw_open, "high": 201.0, "low": 199.0, "close": raw_open}},
        ),
    )
    allocation = min(30_000.0, 30_000.0 / 2)
    assert shares_affordable(allocation, raw_open, config.costs) == 0
    expected_shares = shares_affordable(30_000.0, raw_open, config.costs)
    assert expected_shares == 100
    expected_total, expected_comm = buy_cost(raw_open, expected_shares, config.costs)
    assert expected_total > allocation

    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda d: constant_signal(["AAA"], 80.0, d) if d == signal_day else [],
    ).run(signal_day, buy_day)
    signal = result.attribution.signal
    assert signal.orders_filled == 1
    assert signal.rejected_unaffordable == 0
    assert signal.rejected_insufficient_cash == 0
    assert abs(signal.target_entry_budget_total - allocation) < 1e-9
    assert abs(signal.actual_entry_cash_used_total - expected_total) < 1e-9
    assert signal.unallocated_entry_budget_total == 0.0
    assert signal.overallocated_entry_budget_total > 0.0
    assert abs(signal.overallocated_entry_budget_total - (expected_total - allocation)) < 1e-9
    assert abs(
        signal.target_entry_budget_total
        + signal.overallocated_entry_budget_total
        - signal.actual_entry_cash_used_total
        - signal.unallocated_entry_budget_total
    ) < 1e-9
    assert result.open_positions_at_end == 1
    buy_point = next(p for p in result.equity_curve if p.date == buy_day)
    assert abs(buy_point.cash - (30_000.0 - expected_total)) < 1e-9
    assert abs(buy_point.market_value - raw_open * expected_shares) < 1e-9
    assert expected_comm == 0.0
    summary = summarize_position_utilization(result, max_positions=2)
    assert summary.overallocated_entry_budget_total > 0.0
    assert summary.budget_utilization is not None
    assert summary.budget_utilization > 1.0


def test_signal_budget_fields_reject_negative_or_non_finite() -> None:
    SignalAttribution.model_validate({})
    SignalAttribution.model_validate(
        {
            "target_entry_budget_total": 1.0,
            "actual_entry_cash_used_total": 0.5,
            "unallocated_entry_budget_total": 0.5,
            "overallocated_entry_budget_total": 0.0,
        }
    )
    for field in (
        "target_entry_budget_total",
        "actual_entry_cash_used_total",
        "unallocated_entry_budget_total",
        "overallocated_entry_budget_total",
    ):
        for bad in (-0.01, float("nan"), float("inf")):
            with pytest.raises(ValidationError):
                SignalAttribution.model_validate({field: bad})


def test_open_positions_count_includes_still_open_at_end() -> None:
    calendar = weekdays(date(2024, 1, 2), 6)
    signal_day = calendar[0]
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = zero_cost_config()
    config.portfolio.max_positions = 1
    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda d: constant_signal(["AAA"], 80.0, d) if d == signal_day else [],
    ).run(signal_day, calendar[4])
    assert result.open_positions_at_end == 1
    assert all(point.open_positions is not None for point in result.equity_curve)
    assert result.equity_curve[-1].open_positions == 1
    assert result.equity_curve[0].open_positions == 0
    buy_day = calendar[1]
    buy_index = next(i for i, p in enumerate(result.equity_curve) if p.date == buy_day)
    assert all(p.open_positions == 1 for p in result.equity_curve[buy_index:])


def test_legacy_equity_json_without_position_counts_is_unavailable() -> None:
    curve = [
        EquityPoint(date=date(2024, 1, 2), cash=80_000, market_value=0.0, equity=80_000),
        EquityPoint(date=date(2024, 1, 3), cash=40_000, market_value=40_000, equity=80_000),
    ]
    assert curve[0].open_positions is None
    result = BacktestResult(
        strategy_name="x",
        strategy_version="1",
        strategy_config_hash="h",
        start=date(2024, 1, 2),
        end=date(2024, 1, 3),
        window=BacktestWindow(
            start=date(2024, 1, 2),
            signal_end=date(2024, 1, 2),
            entry_end=date(2024, 1, 3),
            valuation_end=date(2024, 1, 3),
        ),
        metrics=BacktestMetrics(
            initial_capital=80_000,
            final_equity=80_000,
            total_return=0.0,
            annualized_return=None,
            number_of_trades=0,
            win_rate=None,
            average_win=None,
            average_loss=None,
            profit_factor=None,
            expectancy=None,
            average_holding_days=None,
            max_drawdown=None,
            sharpe_ratio=None,
            tp_exit_count=0,
            sl_exit_count=0,
            timeout_exit_count=0,
        ),
        equity_curve=curve,
        attribution=BacktestAttribution(
            signal=SignalAttribution(orders_filled=1, entry_attempts=2)
        ),
    )
    summary = summarize_position_utilization(result, max_positions=3)
    assert summary.available is False
    assert summary.unavailable_reason is not None
    assert "open_positions unavailable" in summary.unavailable_reason
    assert summary.zero_position_days is None
    assert summary.peak_open_positions is None
    assert summary.fill_rate == 0.5


def test_utilization_summary_from_engine_result() -> None:
    calendar = weekdays(date(2024, 1, 2), 6)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = zero_cost_config()
    config.portfolio.max_positions = 3
    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda d: constant_signal(["AAA"], 80.0, d) if d == calendar[0] else [],
    ).run(calendar[0], calendar[4])
    summary = summarize_position_utilization(
        result, max_positions=config.portfolio.max_positions
    )
    assert summary.available is True
    assert summary.trading_days == len(result.equity_curve)
    assert summary.peak_open_positions == 1
    assert summary.underfilled_days == summary.trading_days
    assert summary.zero_position_days >= 1
    signal = result.attribution.signal
    assert abs(
        signal.target_entry_budget_total
        + signal.overallocated_entry_budget_total
        - signal.actual_entry_cash_used_total
        - signal.unallocated_entry_budget_total
    ) < 1e-9
    assert signal.unallocated_entry_budget_total >= 0.0
    assert signal.overallocated_entry_budget_total == 0.0


def test_existing_trade_and_fee_semantics_unchanged_smoke() -> None:
    calendar = weekdays(date(2024, 1, 2), 10)
    signal_day, buy_day, exit_day = calendar[0], calendar[1], calendar[2]
    overrides = {
        buy_day: {"open": 10.0, "high": 10.05, "low": 9.98, "close": 10.0},
        exit_day: {"open": 10.0, "high": 10.40, "low": 9.90, "close": 10.2},
    }
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar, overrides))
    config = zero_cost_config()
    config.portfolio.max_positions = 1
    result = BacktestEngine(
        store,
        config,
        signal_fn=lambda d: constant_signal(["AAA"], 80.0, d) if d == signal_day else [],
    ).run(signal_day, exit_day)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.shares == 100 * (80_000 // (10.0 * 100))
    assert trade.buy_commission == 0.0
    assert trade.sell_commission == 0.0
    assert trade.exit_reason == "take_profit"
