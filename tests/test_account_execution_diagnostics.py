from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from app.backtest.costs import apply_slippage, buy_cost, commission
from app.cli import app as cli_app
from app.models.backtest import (
    BacktestAttribution,
    BacktestMetrics,
    BacktestResult,
    BacktestWindow,
    SignalAttribution,
    TradeFill,
)
from app.models.config import CostConfig
from app.research.account_execution_diagnostics import (
    CandidatePriceRow,
    assert_account_execution_report_self_hash,
    assert_candidate_lot_report_self_hash,
    compute_candidate_lot_report_id,
    diagnose_account_execution,
    diagnose_candidate_lot_affordability,
    seal_candidate_lot_affordability_report,
    verify_account_execution_diagnostic_report_integrity_only,
    verify_candidate_lot_affordability_report_file,
    write_account_execution_diagnostic_report,
    write_candidate_lot_affordability_report,
)

TOL = 1e-9


def _cost_config(
    *,
    commission_rate: float,
    minimum_commission: float,
    slippage_bps: float,
    stamp_tax_rate: float = 0.0,
) -> CostConfig:
    return CostConfig(
        commission_rate=commission_rate,
        min_commission=minimum_commission,
        slippage_bps=slippage_bps,
        stamp_tax_rate=stamp_tax_rate,
        stamp_tax_schedule=[],
    )


def _trade(
    *,
    symbol: str = "AAA",
    entry_raw: float = 10.0,
    exit_raw: float = 11.0,
    shares: int = 100,
    commission_rate: float = 0.00025,
    minimum_commission: float = 5.0,
    slippage_bps: float = 0.0,
    stamp_tax_rate: float = 0.0,
    entry_date: date = date(2024, 1, 3),
    exit_date: date = date(2024, 1, 10),
) -> TradeFill:
    costs = _cost_config(
        commission_rate=commission_rate,
        minimum_commission=minimum_commission,
        slippage_bps=slippage_bps,
        stamp_tax_rate=stamp_tax_rate,
    )
    entry_price = apply_slippage(entry_raw, costs, "buy")
    exit_price = apply_slippage(exit_raw, costs, "sell")
    buy_comm = commission(entry_price * shares, costs)
    sell_comm = commission(exit_price * shares, costs)
    stamp = exit_price * shares * stamp_tax_rate
    buy_slip = (entry_price - entry_raw) * shares
    sell_slip = (exit_raw - exit_price) * shares
    gross = (exit_raw - entry_raw) * shares
    pnl = gross - buy_comm - sell_comm - stamp - buy_slip - sell_slip
    cost_basis = entry_price * shares + buy_comm
    return TradeFill(
        symbol=symbol,
        entry_date=entry_date,
        exit_date=exit_date,
        entry_price=entry_price,
        exit_price=exit_price,
        shares=shares,
        pnl=pnl,
        return_pct=pnl / cost_basis if cost_basis else 0.0,
        holding_days=5,
        exit_reason="timeout",
        buy_commission=buy_comm,
        sell_commission=sell_comm,
        stamp_tax=stamp,
        entry_raw_price=entry_raw,
        exit_raw_price=exit_raw,
        buy_slippage=buy_slip,
        sell_slippage=sell_slip,
        gross_pnl=gross,
    )


def _result_from_trades(
    trades: list[TradeFill],
    *,
    initial_capital: float = 80_000.0,
    signal: SignalAttribution | None = None,
    strategy_config_hash: str = "cfg" + "0" * 61,
    data_snapshot_id: str = "snap" + "0" * 60,
) -> BacktestResult:
    signal = signal or SignalAttribution(
        orders_generated=3,
        entry_attempts=3,
        orders_filled=len(trades),
        rejected_unaffordable=1,
        rejected_insufficient_cash=2,
        target_entry_budget_total=12_000.0,
        actual_entry_cash_used_total=10_000.0,
        unallocated_entry_budget_total=2_500.0,
        overallocated_entry_budget_total=500.0,
    )
    buy_commission = sum(t.buy_commission for t in trades)
    sell_commission = sum(t.sell_commission for t in trades)
    stamp_tax = sum(t.stamp_tax for t in trades)
    slippage = sum(t.buy_slippage + t.sell_slippage for t in trades)
    explicit = buy_commission + sell_commission + stamp_tax
    gross = sum(t.gross_pnl for t in trades if t.gross_pnl is not None)
    net = sum(t.pnl for t in trades)
    start = date(2024, 1, 2)
    end = date(2024, 1, 31)
    return BacktestResult(
        strategy_name="synthetic_diag",
        strategy_version="0.0.1",
        strategy_config_hash=strategy_config_hash,
        start=start,
        end=end,
        window=BacktestWindow(
            start=start,
            signal_end=date(2024, 1, 30),
            entry_end=end,
            valuation_end=end,
        ),
        metrics=BacktestMetrics(
            initial_capital=initial_capital,
            final_equity=initial_capital + net,
            total_return=net / initial_capital if initial_capital else 0.0,
            annualized_return=None,
            number_of_trades=len(trades),
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
            timeout_exit_count=len(trades),
        ),
        trades=trades,
        equity_curve=[],
        open_positions_at_end=0,
        data_snapshot_id=data_snapshot_id,
        attribution=BacktestAttribution(
            gross_realized_pnl=gross,
            net_realized_pnl=net,
            buy_commission=buy_commission,
            sell_commission=sell_commission,
            stamp_tax=stamp_tax,
            estimated_slippage=slippage,
            explicit_costs=explicit,
            total_trading_costs=explicit + slippage,
            signal=signal,
        ),
    )


def test_minimum_commission_binding_vs_rate_commission() -> None:
    rate = 0.00025
    minimum = 5.0
    # Small notional: 10 * 100 = 1000 → rate commission 0.25 → binds to 5.
    small = _trade(
        symbol="SMALL",
        entry_raw=10.0,
        exit_raw=10.5,
        shares=100,
        commission_rate=rate,
        minimum_commission=minimum,
        slippage_bps=0.0,
    )
    # Large notional: 50 * 10000 = 500_000 → rate commission 125 → rate wins.
    large = _trade(
        symbol="LARGE",
        entry_raw=50.0,
        exit_raw=51.0,
        shares=10_000,
        commission_rate=rate,
        minimum_commission=minimum,
        slippage_bps=0.0,
    )
    assert small.buy_commission == pytest.approx(5.0)
    assert large.buy_commission == pytest.approx(50.0 * 10_000 * rate)
    result = _result_from_trades([small, large])
    report = diagnose_account_execution(
        result,
        commission_rate=rate,
        minimum_commission=minimum,
        slippage_bps=0.0,
        lot_size=100,
        numerical_tolerance=TOL,
    )
    assert report.buy_minimum_commission_binding_count == 1
    assert report.sell_minimum_commission_binding_count == 1
    assert report.buy_minimum_commission_binding_fraction.value == pytest.approx(0.5)
    assert "positive_rate_and_minimum" in report.commission_parameter_semantics


def test_effective_bps_arithmetic() -> None:
    rate = 0.00025
    minimum = 5.0
    trade = _trade(
        entry_raw=10.0,
        exit_raw=10.2,
        shares=100,
        commission_rate=rate,
        minimum_commission=minimum,
        slippage_bps=0.0,
    )
    result = _result_from_trades([trade])
    report = diagnose_account_execution(
        result,
        commission_rate=rate,
        minimum_commission=minimum,
        slippage_bps=0.0,
        lot_size=100,
        numerical_tolerance=TOL,
    )
    entry_notional = trade.entry_price * trade.shares
    exit_notional = trade.exit_price * trade.shares
    assert report.buy_effective_commission_bps_notional_weighted.value == pytest.approx(
        (trade.buy_commission / entry_notional) * 10_000.0
    )
    assert report.sell_effective_commission_bps_notional_weighted.value == pytest.approx(
        (trade.sell_commission / exit_notional) * 10_000.0
    )
    assert report.buy_effective_commission_bps_per_side.value == pytest.approx(
        (trade.buy_commission / entry_notional) * 10_000.0
    )


def test_commission_slippage_gross_net_identities() -> None:
    rate = 0.0003
    minimum = 5.0
    slip = 10.0
    stamp = 0.0005
    trade = _trade(
        entry_raw=20.0,
        exit_raw=21.0,
        shares=200,
        commission_rate=rate,
        minimum_commission=minimum,
        slippage_bps=slip,
        stamp_tax_rate=stamp,
    )
    trading_costs = (
        trade.buy_commission + trade.sell_commission + trade.stamp_tax + trade.buy_slippage + trade.sell_slippage
    )
    assert trade.gross_pnl is not None
    assert trade.pnl == pytest.approx(trade.gross_pnl - trading_costs)
    result = _result_from_trades([trade])
    report = diagnose_account_execution(
        result,
        commission_rate=rate,
        minimum_commission=minimum,
        slippage_bps=slip,
        lot_size=100,
        numerical_tolerance=TOL,
    )
    assert report.explicit_costs == pytest.approx(trade.buy_commission + trade.sell_commission + trade.stamp_tax)
    assert report.estimated_slippage == pytest.approx(trade.buy_slippage + trade.sell_slippage)
    assert report.total_trading_costs == pytest.approx(report.explicit_costs + report.estimated_slippage)
    assert report.gross_realized_pnl == pytest.approx(trade.gross_pnl)
    assert report.net_realized_pnl == pytest.approx(trade.pnl)
    assert report.per_trade_gross_net_identity_status == "verified"
    assert report.cost_drag_vs_initial_capital.value == pytest.approx(report.total_trading_costs / 80_000.0)
    assert report.cost_to_gross_ratio.value == pytest.approx(report.total_trading_costs / trade.gross_pnl)


def test_attribution_drift_rejected() -> None:
    trade = _trade(slippage_bps=0.0)
    result = _result_from_trades([trade])
    drifted = result.model_copy(
        update={
            "attribution": result.attribution.model_copy(
                update={"buy_commission": result.attribution.buy_commission + 1.0}
            )
        }
    )
    with pytest.raises(ValueError, match="buy_commission drift"):
        diagnose_account_execution(
            drifted,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
            numerical_tolerance=TOL,
        )


def test_odd_lot_trade_rejected() -> None:
    trade = _trade(shares=100, slippage_bps=0.0)
    odd = trade.model_copy(update={"shares": 150})
    # Keep pnl identity roughly consistent is unnecessary; validation fails on lot first.
    result = _result_from_trades([odd])
    result = result.model_copy(update={"metrics": result.metrics.model_copy(update={"number_of_trades": 1})})
    with pytest.raises(ValueError, match="not divisible by lot_size"):
        diagnose_account_execution(
            result,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
            numerical_tolerance=TOL,
        )


def test_zero_trades_undefined_ratios_are_null_with_reason() -> None:
    result = _result_from_trades([])
    report = diagnose_account_execution(
        result,
        commission_rate=0.00025,
        minimum_commission=5.0,
        slippage_bps=5.0,
        lot_size=100,
        numerical_tolerance=TOL,
    )
    assert report.closed_trade_count == 0
    assert report.buy_minimum_commission_binding_fraction.value is None
    assert report.buy_minimum_commission_binding_fraction.unavailable_reason is not None
    assert report.buy_effective_commission_bps_notional_weighted.value is None
    assert report.sell_effective_commission_bps_per_side.value is None
    assert report.cost_to_gross_ratio.value is None
    assert "gross_realized_pnl <= 0" in (report.cost_to_gross_ratio.unavailable_reason or "")
    assert report.cost_drag_vs_initial_capital.value == pytest.approx(0.0)


def test_negative_gross_cost_to_gross_ratio_null() -> None:
    from app.research.account_execution_diagnostics import _cost_to_gross_ratio

    ratio = _cost_to_gross_ratio(100.0, -50.0)
    assert ratio.value is None
    assert "gross_realized_pnl <= 0" in (ratio.unavailable_reason or "")

    losing = _trade(
        entry_raw=20.0,
        exit_raw=18.0,
        shares=100,
        commission_rate=0.00025,
        minimum_commission=5.0,
        slippage_bps=0.0,
    )
    assert losing.gross_pnl is not None and losing.gross_pnl < 0.0
    report = diagnose_account_execution(
        _result_from_trades([losing]),
        commission_rate=0.00025,
        minimum_commission=5.0,
        slippage_bps=0.0,
        lot_size=100,
        numerical_tolerance=TOL,
    )
    assert report.gross_realized_pnl < 0.0
    assert report.cost_to_gross_ratio.value is None
    assert "not economically interpretable" in (report.cost_to_gross_ratio.unavailable_reason or "")


def test_blank_bindings_and_window_contradictions_rejected() -> None:
    trade = _trade(slippage_bps=0.0)
    blank_snap = _result_from_trades([trade], data_snapshot_id="   ")
    with pytest.raises(ValueError, match="data_snapshot_id must be a nonblank"):
        diagnose_account_execution(
            blank_snap,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
            numerical_tolerance=TOL,
        )
    blank_name = _result_from_trades([trade]).model_copy(update={"strategy_name": ""})
    with pytest.raises(ValueError, match="strategy_name must be a nonblank"):
        diagnose_account_execution(
            blank_name,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
            numerical_tolerance=TOL,
        )
    bad_window = _result_from_trades([trade])
    bad_window = bad_window.model_copy(
        update={
            "window": bad_window.window.model_copy(
                update={"signal_end": date(2024, 2, 15)}  # after entry_end
            )
        }
    )
    with pytest.raises(ValueError, match="signal_end"):
        diagnose_account_execution(
            bad_window,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
            numerical_tolerance=TOL,
        )


def test_signal_budget_identity_drift_rejected() -> None:
    trade = _trade(slippage_bps=0.0)
    drifted = SignalAttribution(
        orders_generated=1,
        entry_attempts=1,
        orders_filled=1,
        target_entry_budget_total=12_000.0,
        actual_entry_cash_used_total=10_000.0,
        unallocated_entry_budget_total=1_000.0,  # breaks 12000+500 == 10000+2500
        overallocated_entry_budget_total=500.0,
    )
    result = _result_from_trades([trade], signal=drifted)
    with pytest.raises(ValueError, match="budget identity drift"):
        diagnose_account_execution(
            result,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
            numerical_tolerance=TOL,
        )


def test_duplicate_and_bad_candidate_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate candidate"):
        diagnose_candidate_lot_affordability(
            [("AAA", 10.0), ("AAA", 11.0)],
            cash_per_slice=4_000.0,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
        )
    with pytest.raises(ValueError, match="duplicate candidate"):
        diagnose_candidate_lot_affordability(
            [(" AAA ", 10.0), ("AAA", 11.0)],
            cash_per_slice=4_000.0,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
        )
    with pytest.raises(ValueError, match="raw_price"):
        diagnose_candidate_lot_affordability(
            [("AAA", 0.0)],
            cash_per_slice=4_000.0,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
        )
    with pytest.raises(ValueError, match="bool rejected|finite number"):
        diagnose_candidate_lot_affordability(
            [("AAA", True)],
            cash_per_slice=4_000.0,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
        )
    with pytest.raises(ValueError, match="nonblank"):
        diagnose_candidate_lot_affordability(
            [("   ", 10.0)],
            cash_per_slice=4_000.0,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
        )
    with pytest.raises(ValueError, match="nonblank"):
        CandidatePriceRow(symbol="   ", raw_price=10.0)
    stripped = diagnose_candidate_lot_affordability(
        [("  AAA  ", 10.0)],
        cash_per_slice=4_000.0,
        commission_rate=0.00025,
        minimum_commission=5.0,
        slippage_bps=0.0,
        lot_size=100,
    )
    assert stripped.candidates[0].symbol == "AAA"
    with pytest.raises(ValueError, match="cash_per_slice"):
        diagnose_candidate_lot_affordability(
            [("AAA", 10.0)],
            cash_per_slice=0.0,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
        )


def test_expensive_candidate_cannot_buy_one_lot_cheaper_exact() -> None:
    cash = 4_000.0
    report = diagnose_candidate_lot_affordability(
        [("EXPENSIVE", 80.0), ("CHEAP", 10.0)],
        cash_per_slice=cash,
        commission_rate=0.00025,
        minimum_commission=5.0,
        slippage_bps=0.0,
        lot_size=100,
    )
    by_symbol = {row.symbol: row for row in report.rows}
    assert by_symbol["EXPENSIVE"].shares_affordable == 0
    assert by_symbol["EXPENSIVE"].lots_affordable == 0
    assert by_symbol["EXPENSIVE"].can_afford_one_lot is False
    assert by_symbol["EXPENSIVE"].unused_cash == pytest.approx(cash)

    costs = _cost_config(commission_rate=0.00025, minimum_commission=5.0, slippage_bps=0.0)
    expected_shares = 300  # 3 lots * 100 at 10.0 raw with room for min commission
    fill = apply_slippage(10.0, costs, "buy")
    total, _ = buy_cost(fill, expected_shares, costs)
    assert total <= cash + 1e-9
    total_4, _ = buy_cost(fill, 400, costs)
    assert total_4 > cash
    assert by_symbol["CHEAP"].shares_affordable == expected_shares
    assert by_symbol["CHEAP"].lots_affordable == 3
    assert by_symbol["CHEAP"].unused_cash == pytest.approx(cash - total)
    assert by_symbol["CHEAP"].can_afford_one_lot is True


def test_true_reseal_semantic_tamper_of_candidate_report_rejected(tmp_path: Path) -> None:
    report = diagnose_candidate_lot_affordability(
        [("AAA", 10.0), ("BBB", 12.0)],
        cash_per_slice=4_000.0,
        commission_rate=0.00025,
        minimum_commission=5.0,
        slippage_bps=0.0,
        lot_size=100,
    )
    path = tmp_path / "candidate.json"
    write_candidate_lot_affordability_report(report, path)
    verify_candidate_lot_affordability_report_file(path)

    resealed = report.model_copy(deep=True)
    rows = list(resealed.rows)
    rows[0] = rows[0].model_copy(
        update={
            "shares_affordable": rows[0].shares_affordable + 100,
            "lots_affordable": rows[0].lots_affordable + 1,
            "can_afford_one_lot": True,
            "unused_cash": max(rows[0].unused_cash - 1.0, 0.0),
        }
    )
    resealed = resealed.model_copy(update={"rows": rows, "report_id": None})
    resealed = seal_candidate_lot_affordability_report(resealed)
    assert resealed.report_id == compute_candidate_lot_report_id(resealed)
    bad = tmp_path / "resealed-wrong-shares.json"
    write_candidate_lot_affordability_report(resealed, bad)
    with pytest.raises(ValueError, match="does not match recomputed diagnose"):
        verify_candidate_lot_affordability_report_file(bad)


def test_deterministic_hashes_and_readiness_flags_false(tmp_path: Path) -> None:
    trade = _trade(slippage_bps=5.0, stamp_tax_rate=0.0005)
    result = _result_from_trades([trade])
    first = diagnose_account_execution(
        result,
        commission_rate=0.00025,
        minimum_commission=5.0,
        slippage_bps=5.0,
        lot_size=100,
        numerical_tolerance=TOL,
    )
    second = diagnose_account_execution(
        result,
        commission_rate=0.00025,
        minimum_commission=5.0,
        slippage_bps=5.0,
        lot_size=100,
        numerical_tolerance=TOL,
    )
    assert first.report_id == second.report_id
    assert_account_execution_report_self_hash(first)
    assert first.diagnostic_only is True
    assert first.ready_for_scoring is False
    assert first.ready_for_backtest is False
    assert first.ready_for_trading is False
    assert first.auto_apply is False
    assert first.file_verifier_scope == "integrity_only"
    assert first.rejected_unaffordable == 1
    assert first.rejected_insufficient_cash == 2
    assert first.orders_generated == 3
    assert first.target_entry_budget_total == pytest.approx(12_000.0)

    path = tmp_path / "account.json"
    write_account_execution_diagnostic_report(first, path)
    verify_account_execution_diagnostic_report_integrity_only(path)

    # Integrity-only verifier catches stale hash, but cannot catch a true reseal of
    # descriptive fields without the source BacktestResult (by design).
    stale: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    stale["closed_trade_count"] = first.closed_trade_count + 1
    stale["buy_side_count"] = stale["closed_trade_count"]
    stale["sell_side_count"] = stale["closed_trade_count"]
    stale["lot_compliant_trade_count"] = stale["closed_trade_count"]
    stale_path = tmp_path / "stale.json"
    stale_path.write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="report_id does not match"):
        verify_account_execution_diagnostic_report_integrity_only(stale_path)

    cand_a = diagnose_candidate_lot_affordability(
        [("AAA", 10.0)],
        cash_per_slice=4_000.0,
        commission_rate=0.00025,
        minimum_commission=5.0,
        slippage_bps=0.0,
        lot_size=100,
    )
    cand_b = diagnose_candidate_lot_affordability(
        [("AAA", 10.0)],
        cash_per_slice=4_000.0,
        commission_rate=0.00025,
        minimum_commission=5.0,
        slippage_bps=0.0,
        lot_size=100,
    )
    assert cand_a.report_id == cand_b.report_id
    assert_candidate_lot_report_self_hash(cand_a)
    assert cand_a.ready_for_scoring is False
    assert cand_a.ready_for_backtest is False
    assert cand_a.ready_for_trading is False
    assert cand_a.auto_apply is False


def test_zero_rate_and_minimum_semantics() -> None:
    trade = _trade(
        commission_rate=0.0,
        minimum_commission=0.0,
        slippage_bps=0.0,
    )
    assert trade.buy_commission == 0.0
    result = _result_from_trades([trade])
    report = diagnose_account_execution(
        result,
        commission_rate=0.0,
        minimum_commission=0.0,
        slippage_bps=0.0,
        lot_size=100,
        numerical_tolerance=TOL,
    )
    assert report.buy_minimum_commission_binding_count == 0
    assert "zero_rate_and_zero_minimum" in report.commission_parameter_semantics


def test_legacy_missing_gross_refuses_guess() -> None:
    trade = _trade(slippage_bps=0.0)
    legacy = trade.model_copy(update={"gross_pnl": None, "entry_raw_price": None, "exit_raw_price": None})
    result = _result_from_trades([legacy])
    # Attribution still has a gross number; diagnosis must not invent trade gross to match it.
    with pytest.raises(ValueError, match="refusing to guess legacy"):
        diagnose_account_execution(
            result,
            commission_rate=0.00025,
            minimum_commission=5.0,
            slippage_bps=0.0,
            lot_size=100,
            numerical_tolerance=TOL,
        )


def test_cli_verify_commands(tmp_path: Path) -> None:
    trade = _trade(slippage_bps=0.0)
    account = diagnose_account_execution(
        _result_from_trades([trade]),
        commission_rate=0.00025,
        minimum_commission=5.0,
        slippage_bps=0.0,
        lot_size=100,
        numerical_tolerance=TOL,
    )
    account_path = tmp_path / "account.json"
    write_account_execution_diagnostic_report(account, account_path)
    runner = CliRunner()
    ok_account = runner.invoke(
        cli_app,
        ["verify-account-execution-diagnostic-report-integrity", "--report-file", str(account_path)],
    )
    assert ok_account.exit_code == 0
    assert "file_verifier_scope=integrity_only" in ok_account.output
    assert "diagnostic_only=true" in ok_account.output

    candidate = diagnose_candidate_lot_affordability(
        [("AAA", 10.0)],
        cash_per_slice=4_000.0,
        commission_rate=0.00025,
        minimum_commission=5.0,
        slippage_bps=0.0,
        lot_size=100,
    )
    candidate_path = tmp_path / "candidate.json"
    write_candidate_lot_affordability_report(candidate, candidate_path)
    ok_candidate = runner.invoke(
        cli_app,
        ["verify-candidate-lot-affordability-report", "--report-file", str(candidate_path)],
    )
    assert ok_candidate.exit_code == 0
    assert "diagnostic_only=true" in ok_candidate.output

    # True reseal with wrong report_id on account integrity path.
    wrong = account.model_copy(update={"report_id": "0" * 64})
    wrong_path = tmp_path / "wrong-id.json"
    wrong_path.write_text(wrong.model_dump_json(indent=2) + "\n", encoding="utf-8")
    bad = runner.invoke(
        cli_app,
        ["verify-account-execution-diagnostic-report-integrity", "--report-file", str(wrong_path)],
    )
    assert bad.exit_code == 1
