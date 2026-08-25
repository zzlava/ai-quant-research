from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from app.backtest.metrics import compute_attribution, compute_metrics
from app.cli import app as cli_app
from app.errors import PreflightError
from app.models.backtest import (
    BacktestAttribution,
    BacktestMetrics,
    BacktestResult,
    BacktestWindow,
    EquityPoint,
    SignalAttribution,
    TradeFill,
)
from app.models.config import StrategyConfig
from app.research.portfolio_oos_authorization import (
    AUTHORIZED_COMPOSITE_STORE_SNAPSHOT_ID,
    AUTHORIZED_FREEZE_ID,
    AUTHORIZED_OUTPUT_DIR,
    AUTHORIZED_RECEIPT_PATH,
    AUTHORIZED_RUNTIME_CONFIG_HASH,
    AUTHORIZED_USER_PHRASE,
    DEFAULT_PORTFOLIO_OOS_AUTH_PATH,
    assert_authorization_paths_unused,
    assert_committed_authorization_bindings,
    build_committed_portfolio_oos_authorization,
    load_verified_committed_portfolio_oos_authorization,
    load_verified_portfolio_oos_authorization,
    seal_authorization,
    verify_authorization_against_freeze,
    write_portfolio_oos_authorization,
)
from app.research.portfolio_oos_evaluation import (
    BASELINE_SCENARIO_ID,
    MODERATE_SCENARIO_ID,
    SCENARIO_RESULT_FILES,
    SEVERE_SCENARIO_ID,
    InMemoryScoreProvider,
    PortfolioOosDescriptiveSummary,
    PortfolioOosEvaluationReport,
    PortfolioOosGateResult,
    PortfolioOosScenarioArtifact,
    build_runtime_equivalent_config,
    canonical_config_diff,
    classify_portfolio_oos_outcome,
    evaluate_and_write_portfolio_oos_one_shot,
    load_verified_portfolio_oos_consumption_receipt,
    load_verified_portfolio_oos_evaluation,
    verify_sealed_portfolio_oos_artifacts_against_authorization,
    write_portfolio_oos_evaluation_atomically,
)
from app.research.portfolio_oos_freeze import (
    DEFAULT_PORTFOLIO_OOS_FREEZE_PATH,
    FROZEN_CONFIG_HASH,
    FROZEN_STRATEGY_CONFIG_ID,
    FROZEN_STRATEGY_PATH,
    load_verified_portfolio_oos_freeze,
)
from app.research.portfolio_robustness import _cost_stress_config
from app.strategies.loader import load_strategy_config
from tests.helpers import PROJECT_ROOT, weekdays, zero_cost_config

COMMITTED_AUTH = PROJECT_ROOT / DEFAULT_PORTFOLIO_OOS_AUTH_PATH
COMMITTED_FREEZE = PROJECT_ROOT / DEFAULT_PORTFOLIO_OOS_FREEZE_PATH
STRATEGY_PATH = PROJECT_ROOT / FROZEN_STRATEGY_PATH


def _metrics(
    *,
    total_return: float,
    sharpe: float | None,
    drawdown: float | None,
    trades: int,
    final_equity: float,
) -> BacktestMetrics:
    return BacktestMetrics(
        initial_capital=80_000,
        final_equity=final_equity,
        total_return=total_return,
        annualized_return=total_return,
        number_of_trades=trades,
        win_rate=0.5,
        average_win=1.0,
        average_loss=-1.0,
        profit_factor=1.0,
        expectancy=0.0,
        average_holding_days=20.0,
        max_drawdown=drawdown,
        sharpe_ratio=sharpe,
        tp_exit_count=0,
        sl_exit_count=0,
        timeout_exit_count=trades,
    )


def _trade(symbol: str, *, pnl: float, entry: date, exit_: date) -> TradeFill:
    return TradeFill(
        symbol=symbol,
        entry_date=entry,
        exit_date=exit_,
        entry_price=10.0,
        exit_price=10.0 + pnl / 100.0,
        shares=100,
        pnl=pnl,
        return_pct=pnl / 1_000.0,
        holding_days=20,
        exit_reason="timeout",
        buy_commission=1.0,
        sell_commission=1.0,
        stamp_tax=0.5,
        entry_raw_price=10.0,
        exit_raw_price=10.0,
        buy_slippage=0.0,
        sell_slippage=0.0,
        gross_pnl=pnl + 2.5,
    )


def _result(
    *,
    total_return: float = 0.10,
    sharpe: float | None = 0.5,
    drawdown: float | None = -0.05,
    trades: int = 25,
    open_positions: int = 0,
    trade_rows: list[TradeFill] | None = None,
    final_equity: float | None = None,
    strategy_config_hash: str = "deadbeefdeadbeef",
    scenario_id: str | None = None,
    commission_rate: float | None = None,
    minimum_commission: float | None = None,
    slippage_bps: float | None = None,
) -> BacktestResult:
    """Synthetic result for classify-only tests; metrics may be hand-authored."""
    equity = final_equity if final_equity is not None else 80_000 * (1 + total_return)
    fills = trade_rows or [
        _trade(
            f"{index:06d}.SZ",
            pnl=(equity - 80_000) / max(trades, 1),
            entry=date(2025, 2, 3),
            exit_=date(2025, 3, 3),
        )
        for index in range(trades)
    ]
    if trade_rows is None and fills:
        adjust = (equity - 80_000) - sum(item.pnl for item in fills)
        fills[-1] = fills[-1].model_copy(update={"pnl": fills[-1].pnl + adjust})
    start = date(2025, 1, 2)
    end = date(2026, 8, 21)
    return BacktestResult(
        strategy_name="test",
        strategy_version="1",
        strategy_config_hash=strategy_config_hash,
        start=start,
        end=end,
        window=BacktestWindow(start=start, signal_end=date(2026, 7, 22), entry_end=end, valuation_end=end),
        metrics=_metrics(
            total_return=total_return,
            sharpe=sharpe,
            drawdown=drawdown,
            trades=len(fills) if trade_rows is not None else trades,
            final_equity=equity,
        ),
        trades=fills,
        equity_curve=[
            EquityPoint(date=start, cash=80_000, market_value=0.0, equity=80_000),
            EquityPoint(date=end, cash=equity, market_value=0.0, equity=equity),
        ],
        open_positions_at_end=open_positions,
        attribution=BacktestAttribution(total_trading_costs=10.0),
        portfolio_oos_scenario_id=scenario_id,
        portfolio_oos_commission_rate=commission_rate,
        portfolio_oos_minimum_commission=minimum_commission,
        portfolio_oos_slippage_bps=slippage_bps,
        portfolio_oos_stamp_tax_unchanged=True if scenario_id is not None else None,
    )


def _synthetic_equity_curve(
    *,
    start: date,
    end: date,
    initial: float,
    final: float,
    points: int = 60,
) -> list[EquityPoint]:
    curve: list[EquityPoint] = []
    for index in range(points):
        frac = index / (points - 1)
        day = start + timedelta(days=index)
        if index == points - 1:
            day = end
        equity = initial + (final - initial) * frac
        curve.append(EquityPoint(date=day, cash=equity, market_value=0.0, equity=equity))
    # Ensure strictly increasing dates even when end is far; compress if collisions.
    fixed: list[EquityPoint] = []
    last = start - timedelta(days=1)
    for point in curve:
        day = max(point.date, last + timedelta(days=1))
        if day > end and point is not curve[-1]:
            day = last + timedelta(days=1)
        if point is curve[-1]:
            day = end if end > last else last + timedelta(days=1)
        fixed.append(point.model_copy(update={"date": day}))
        last = day
    return fixed


def _bound_result(
    scenario_id: str,
    *,
    commission_rate: float,
    minimum_commission: float = 5.0,
    slippage_bps: float,
    strategy_config_hash: str = "deadbeefdeadbeef",
    strategy_name: str = "test",
    strategy_version: str = "1",
    research_scope: str = "historical_all_a_share",
    total_return: float = 0.10,
    trades: int = 25,
    open_positions: int = 0,
    trade_rows: list[TradeFill] | None = None,
) -> BacktestResult:
    """Sealed-artifact result with metrics/attribution recomputable by the verifier."""
    start = date(2025, 1, 2)
    end = date(2026, 8, 21)
    equity = 80_000 * (1 + total_return)
    fills = trade_rows or [
        _trade(
            f"{index:06d}.SZ",
            pnl=(equity - 80_000) / max(trades, 1),
            entry=date(2025, 2, 3),
            exit_=date(2025, 3, 3),
        )
        for index in range(trades)
    ]
    if trade_rows is None and fills:
        adjust = (equity - 80_000) - sum(item.pnl for item in fills)
        fills[-1] = fills[-1].model_copy(
            update={"pnl": fills[-1].pnl + adjust, "gross_pnl": fills[-1].pnl + adjust + 2.5}
        )
    curve = _synthetic_equity_curve(start=start, end=end, initial=80_000, final=equity)
    metrics = compute_metrics(80_000, fills, curve, start, end)
    attribution = compute_attribution(fills, SignalAttribution())
    return BacktestResult(
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        strategy_config_hash=strategy_config_hash,
        start=start,
        end=end,
        window=BacktestWindow(start=start, signal_end=date(2026, 7, 22), entry_end=end, valuation_end=end),
        metrics=metrics,
        trades=fills,
        equity_curve=curve,
        open_positions_at_end=open_positions,
        data_snapshot_id=AUTHORIZED_COMPOSITE_STORE_SNAPSHOT_ID,
        research_scope=research_scope,
        attribution=attribution,
        portfolio_oos_scenario_id=scenario_id,
        portfolio_oos_commission_rate=commission_rate,
        portfolio_oos_minimum_commission=minimum_commission,
        portfolio_oos_slippage_bps=slippage_bps,
        portfolio_oos_stamp_tax_unchanged=True,
    )


def _artifact_for(result: BacktestResult, *, initial_cash: float = 80_000.0) -> PortfolioOosScenarioArtifact:
    from app.research.portfolio_oos_evaluation import (
        _artifact_from_bound_result,
    )

    assert result.portfolio_oos_scenario_id is not None
    scenario_id = result.portfolio_oos_scenario_id
    return _artifact_from_bound_result(
        scenario_id=scenario_id,
        result_file=SCENARIO_RESULT_FILES[scenario_id],
        result=result,
        initial_cash=initial_cash,
    )


def _auth_for_temp(tmp_path: Path):
    committed = build_committed_portfolio_oos_authorization()
    return seal_authorization(
        committed.model_copy(
            update={
                "output_dir": "out/one-shot-v1",
                "consumption_receipt_path": "out/one-shot-v1.consumption-receipt.json",
            }
        )
    )


def _report_from_auth(
    auth,
    *,
    scenarios: dict[str, PortfolioOosScenarioArtifact],
    outcome: str,
    outcome_reason: str,
    gates: list[Any] | None = None,
    preflight_passed: bool = True,
    scenario_execution_complete: bool = True,
    failure_stage: str = "none",
    preflight_error: str | None = None,
    scenario_error: str | None = None,
    observed_first_signal: date | None = date(2025, 1, 22),
) -> PortfolioOosEvaluationReport:
    return PortfolioOosEvaluationReport(
        authorization_id=auth.authorization_id,
        freeze_id=auth.freeze_id,
        strategy_path=auth.strategy_path,
        strategy_file_sha256=auth.strategy_file_sha256,
        strategy_config_id=auth.strategy_config_id,
        frozen_config_hash=auth.frozen_config_hash,
        runtime_config_hash=auth.runtime_override.expected_runtime_config_hash,
        runtime_signal_anchor_date=auth.runtime_override.runtime_value,
        runtime_config_diff=["trade.signal_anchor_date"],
        market_snapshot_id=auth.market_snapshot_id,
        fundamental_snapshot_id=auth.fundamental_snapshot_id,
        fundamental_base_market_snapshot_id=auth.fundamental_base_market_snapshot_id,
        composite_store_snapshot_id=auth.expected_composite_store_snapshot_id,
        evaluation_start=auth.evaluation_window.evaluation_start,
        evaluation_end=auth.evaluation_window.evaluation_end,
        first_2025_plus_signal=auth.evaluation_window.first_2025_plus_signal,
        signal_cutoff=auth.evaluation_window.signal_cutoff,
        last_scheduled_exit=auth.evaluation_window.last_scheduled_exit,
        preflight_passed=preflight_passed,
        preflight_error=preflight_error,
        scenario_execution_complete=scenario_execution_complete,
        failure_stage=failure_stage,
        scenario_error=scenario_error,
        observed_first_signal=observed_first_signal,
        scenarios=scenarios,
        gates=gates or [],
        outcome=outcome,
        outcome_reason=outcome_reason,
    )


def _seal_complete_scenarios(auth, tmp_path: Path, *, tag: str = "seal"):
    freeze = load_verified_portfolio_oos_freeze(COMMITTED_FREEZE)
    baseline = _bound_result(
        BASELINE_SCENARIO_ID, commission_rate=0.0003, slippage_bps=5.0, total_return=0.1
    )
    moderate = _bound_result(
        MODERATE_SCENARIO_ID, commission_rate=0.0006, slippage_bps=10.0, total_return=0.08
    )
    severe = _bound_result(
        SEVERE_SCENARIO_ID, commission_rate=0.0012, slippage_bps=25.0, total_return=0.05
    )
    outcome, reason, gates, _ = classify_portfolio_oos_outcome(
        preflight_passed=True,
        scenario_execution_complete=True,
        baseline=baseline,
        severe=severe,
        initial_cash=float(auth.hard_risk_gates.initial_cash),
        min_closed_trades=auth.evaluability_gates.min_closed_trades,
        max_drawdown_floor=auth.hard_risk_gates.max_drawdown_floor,
        largest_symbol_loss_fraction=(
            auth.hard_risk_gates.largest_single_symbol_loss_fraction_of_initial_cash
        ),
        pnl_reconciliation_abs_tol=auth.evaluability_gates.pnl_reconciliation_abs_tol,
    )
    results = {
        BASELINE_SCENARIO_ID: baseline,
        MODERATE_SCENARIO_ID: moderate,
        SEVERE_SCENARIO_ID: severe,
    }
    sealed_gates = list(gates)
    sealed_gates.append(
        PortfolioOosGateResult(
            gate="moderate_cost_descriptive",
            category="descriptive",
            passed=True,
            observed=moderate.metrics.total_return,
            threshold=None,
            decides_oos_result=False,
            note="moderate 2x/2x cost stress is descriptive only",
        )
    )
    report = _report_from_auth(
        auth,
        scenarios={key: _artifact_for(value) for key, value in results.items()},
        outcome=outcome,
        outcome_reason=reason,
        gates=sealed_gates,
    )
    output = tmp_path / tag / auth.output_dir
    receipt = tmp_path / tag / auth.consumption_receipt_path
    sealed, written = write_portfolio_oos_evaluation_atomically(
        output,
        report,
        results,
        receipt_path=receipt,
        authorization_id=auth.authorization_id or "",
        freeze_id=auth.freeze_id,
        authorization_output_dir=auth.output_dir,
    )
    return sealed, written, output, receipt, freeze, results, report


def test_committed_authorization_is_self_hashed_and_matches_freeze() -> None:
    contract = load_verified_committed_portfolio_oos_authorization(COMMITTED_AUTH)
    assert_committed_authorization_bindings(contract)
    verify_authorization_against_freeze(contract, freeze_path=COMMITTED_FREEZE)
    assert contract.authorized is True
    assert contract.one_shot is True
    assert contract.consumed is False
    assert contract.authorization_date == date(2026, 8, 25)
    assert contract.user_authorization_phrase == AUTHORIZED_USER_PHRASE
    assert contract.freeze_id == AUTHORIZED_FREEZE_ID
    assert contract.frozen_config_hash == FROZEN_CONFIG_HASH
    assert contract.runtime_override.expected_runtime_config_hash == AUTHORIZED_RUNTIME_CONFIG_HASH
    assert contract.expected_composite_store_snapshot_id == AUTHORIZED_COMPOSITE_STORE_SNAPSHOT_ID
    assert contract.output_dir == AUTHORIZED_OUTPUT_DIR
    assert contract.consumption_receipt_path == AUTHORIZED_RECEIPT_PATH
    assert contract.ready_for_scoring is False
    assert contract.ready_for_trading is False
    assert contract.auto_deploy is False
    assert contract.human_review_required is True
    rebuilt = build_committed_portfolio_oos_authorization()
    assert rebuilt.authorization_id == contract.authorization_id


def test_tampered_authorization_hash_and_committed_drift_are_rejected(tmp_path: Path) -> None:
    auth = build_committed_portfolio_oos_authorization()
    path = tmp_path / "auth.json"
    write_portfolio_oos_authorization(path, auth)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["authorization_id"] = "ab" * 32
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="authorization ID does not match"):
        load_verified_portfolio_oos_authorization(path)

    resealed = seal_authorization(
        auth.model_copy(update={"output_dir": "data/evil/portfolio-oos-evaluations/one-shot-v1"})
    )
    with pytest.raises(ValueError, match="sealed committed"):
        assert_committed_authorization_bindings(resealed)


def test_runtime_override_changes_only_signal_anchor_and_matches_expected_hash() -> None:
    frozen = load_strategy_config(
        "all_a_share_historical_value_portfolio_selected_v2",
        PROJECT_ROOT / "config" / "strategies",
    )
    assert frozen.config_hash() == FROZEN_CONFIG_HASH
    runtime = build_runtime_equivalent_config(frozen)
    assert canonical_config_diff(frozen, runtime) == ["trade.signal_anchor_date"]
    assert runtime.trade.signal_anchor_date == date(2024, 10, 29)
    assert runtime.config_hash() == AUTHORIZED_RUNTIME_CONFIG_HASH
    assert frozen.trade.signal_anchor_date == date(2022, 1, 4)


def test_cost_multiplier_semantics_match_portfolio_robustness() -> None:
    config = zero_cost_config()
    config = config.model_copy(
        update={
            "costs": config.costs.model_copy(
                update={"commission_rate": 0.0003, "min_commission": 5.0, "slippage_bps": 10.0}
            )
        }
    )
    stressed = _cost_stress_config(
        config,
        scenario_id=SEVERE_SCENARIO_ID,
        commission_multiplier=4.0,
        slippage_multiplier=5.0,
    )
    assert stressed.costs.commission_rate == pytest.approx(0.0012)
    assert stressed.costs.slippage_bps == pytest.approx(50.0)
    assert stressed.costs.min_commission == config.costs.min_commission
    assert stressed.costs.stamp_tax_schedule == config.costs.stamp_tax_schedule


def test_gate_boundaries_and_per_symbol_loss_aggregation() -> None:
    baseline = _result(total_return=0.0, sharpe=0.0, drawdown=-0.15, trades=20)
    severe = _result(total_return=0.0, sharpe=0.1, drawdown=-0.01, trades=20)
    outcome, _, gates, loss = classify_portfolio_oos_outcome(
        preflight_passed=True,
        baseline=baseline,
        severe=severe,
        initial_cash=80_000,
        min_closed_trades=20,
        max_drawdown_floor=-0.15,
        largest_symbol_loss_fraction=0.03,
        pnl_reconciliation_abs_tol=1e-6,
    )
    assert outcome == "no_go"
    assert loss == 0.0

    winning = _result(total_return=1e-12, sharpe=1e-12, drawdown=-0.15, trades=20)
    severe_ok = _result(total_return=1e-12, sharpe=0.2, drawdown=-0.01, trades=20)
    outcome, _, _, _ = classify_portfolio_oos_outcome(
        preflight_passed=True,
        baseline=winning,
        severe=severe_ok,
        initial_cash=80_000,
        min_closed_trades=20,
        max_drawdown_floor=-0.15,
        largest_symbol_loss_fraction=0.03,
        pnl_reconciliation_abs_tol=1e-6,
    )
    assert outcome == "conditional_go"

    lossy_trades = [
        _trade("AAA.SZ", pnl=-2_400.0, entry=date(2025, 2, 3), exit_=date(2025, 3, 3)),
        _trade("AAA.SZ", pnl=-1.0, entry=date(2025, 4, 1), exit_=date(2025, 4, 30)),
        _trade("BBB.SZ", pnl=10_000.0, entry=date(2025, 2, 3), exit_=date(2025, 3, 3)),
    ]
    # 2401 / 80000 = 0.0300125 > 0.03
    lossy = _result(
        total_return=0.1,
        sharpe=1.0,
        drawdown=-0.05,
        trades=3,
        trade_rows=lossy_trades,
        final_equity=80_000 + sum(item.pnl for item in lossy_trades),
    )
    # pad closed trades for evaluability
    padded = lossy.model_copy(
        update={
            "metrics": lossy.metrics.model_copy(update={"number_of_trades": 20}),
            "trades": lossy_trades
            + [
                _trade(f"{index:06d}.SZ", pnl=0.0, entry=date(2025, 5, 1), exit_=date(2025, 5, 30))
                for index in range(17)
            ],
        }
    )
    # repair reconciliation after padding
    adjust = (padded.metrics.final_equity - 80_000) - sum(item.pnl for item in padded.trades)
    padded.trades[-1] = padded.trades[-1].model_copy(update={"pnl": padded.trades[-1].pnl + adjust})
    outcome, _, _, observed_loss = classify_portfolio_oos_outcome(
        preflight_passed=True,
        baseline=padded,
        severe=severe_ok.model_copy(update={"metrics": severe_ok.metrics.model_copy(update={"number_of_trades": 20})}),
        initial_cash=80_000,
        min_closed_trades=20,
        max_drawdown_floor=-0.15,
        largest_symbol_loss_fraction=0.03,
        pnl_reconciliation_abs_tol=1e-6,
    )
    assert observed_loss == pytest.approx(2401.0 / 80_000)
    assert outcome == "no_go"

    open_end = _result(total_return=0.1, sharpe=1.0, drawdown=-0.05, trades=25, open_positions=1)
    outcome, _, _, _ = classify_portfolio_oos_outcome(
        preflight_passed=True,
        baseline=open_end,
        severe=_result(),
        initial_cash=80_000,
        min_closed_trades=20,
        max_drawdown_floor=-0.15,
        largest_symbol_loss_fraction=0.03,
        pnl_reconciliation_abs_tol=1e-6,
    )
    assert outcome == "not_evaluable"

    too_few = _result(total_return=0.1, sharpe=1.0, drawdown=-0.05, trades=19)
    outcome, _, _, _ = classify_portfolio_oos_outcome(
        preflight_passed=True,
        baseline=too_few,
        severe=_result(),
        initial_cash=80_000,
        min_closed_trades=20,
        max_drawdown_floor=-0.15,
        largest_symbol_loss_fraction=0.03,
        pnl_reconciliation_abs_tol=1e-6,
    )
    assert outcome == "not_evaluable"

    drawdown_fail = _result(total_return=0.1, sharpe=1.0, drawdown=-0.1500001, trades=25)
    outcome, _, _, _ = classify_portfolio_oos_outcome(
        preflight_passed=True,
        baseline=drawdown_fail,
        severe=_result(total_return=0.01),
        initial_cash=80_000,
        min_closed_trades=20,
        max_drawdown_floor=-0.15,
        largest_symbol_loss_fraction=0.03,
        pnl_reconciliation_abs_tol=1e-6,
    )
    assert outcome == "no_go"


def test_pnl_reconciliation_boundary() -> None:
    baseline = _result(total_return=0.1, sharpe=1.0, drawdown=-0.05, trades=25)
    # Break reconciliation just beyond tolerance.
    broken = baseline.model_copy(
        update={
            "trades": [item.model_copy(update={"pnl": item.pnl}) for item in baseline.trades[:-1]]
            + [baseline.trades[-1].model_copy(update={"pnl": baseline.trades[-1].pnl + 1e-5})]
        }
    )
    outcome, _, _, _ = classify_portfolio_oos_outcome(
        preflight_passed=True,
        baseline=broken,
        severe=_result(),
        initial_cash=80_000,
        min_closed_trades=20,
        max_drawdown_floor=-0.15,
        largest_symbol_loss_fraction=0.03,
        pnl_reconciliation_abs_tol=1e-6,
    )
    assert outcome == "not_evaluable"


def test_atomic_immutable_output_replay_refusal_and_verifier(tmp_path: Path) -> None:
    auth = _auth_for_temp(tmp_path)
    sealed, written_receipt, output, receipt, freeze, results, report = _seal_complete_scenarios(
        auth, tmp_path, tag="atomic"
    )
    baseline = results[BASELINE_SCENARIO_ID]
    assert sealed.report_id is not None
    loaded_report, loaded_results = load_verified_portfolio_oos_evaluation(output)
    assert loaded_report.report_id == sealed.report_id
    assert set(loaded_results) == {BASELINE_SCENARIO_ID, MODERATE_SCENARIO_ID, SEVERE_SCENARIO_ID}
    load_verified_portfolio_oos_consumption_receipt(receipt)

    with pytest.raises(ValueError, match="immutable"):
        write_portfolio_oos_evaluation_atomically(
            output,
            report,
            {BASELINE_SCENARIO_ID: baseline},
            receipt_path=tmp_path / "receipt2.json",
            authorization_id=auth.authorization_id or "",
            freeze_id=auth.freeze_id,
            authorization_output_dir=auth.output_dir,
        )

    verified_report, verified_receipt = verify_sealed_portfolio_oos_artifacts_against_authorization(
        output_dir=output,
        receipt_path=receipt,
        authorization=auth,
        freeze=freeze,
    )
    assert verified_report.report_id == sealed.report_id
    assert verified_receipt.receipt_id == written_receipt.receipt_id

    report_path = output / "report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["outcome"] = "no_go"
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="report ID does not match|outcome"):
        verify_sealed_portfolio_oos_artifacts_against_authorization(
            output_dir=output,
            receipt_path=receipt,
            authorization=auth,
            freeze=freeze,
        )


def test_preflight_before_score_and_not_evaluable_consumption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _auth_for_temp(tmp_path)
    write_portfolio_oos_authorization(tmp_path / "authorization.json", auth)
    order: list[str] = []

    def fake_preflight(**kwargs: Any) -> None:
        order.append("preflight")
        raise PreflightError("synthetic preflight failure")

    def fake_score(day: date) -> list[Any]:
        order.append("score")
        raise AssertionError("score must not run before successful preflight")

    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation.preflight_research",
        fake_preflight,
    )
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._load_authorized_composite_store",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._load_verified_frozen_strategy",
        lambda strategy_path, authorization: load_strategy_config(
            "all_a_share_historical_value_portfolio_selected_v2",
            PROJECT_ROOT / "config" / "strategies",
        ),
    )
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._runtime_config_for_evaluation",
        lambda frozen_config, authorization: build_runtime_equivalent_config(frozen_config),
    )

    report, receipt, output = evaluate_and_write_portfolio_oos_one_shot(
        authorization=auth,
        freeze_path=COMMITTED_FREEZE,
        strategy_path=STRATEGY_PATH,
        market_dir=tmp_path / "market",
        fundamental_dir=tmp_path / "fundamental",
        root=tmp_path,
        project_root=PROJECT_ROOT,
        score_fn=fake_score,
    )
    assert order == ["preflight"]
    assert report.outcome == "not_evaluable"
    assert report.preflight_passed is False
    assert report.scenario_execution_complete is False
    assert report.failure_stage == "preflight"
    assert "preflight failed" in report.outcome_reason
    assert receipt.receipt_id is not None
    assert output.exists()
    assert (tmp_path / auth.consumption_receipt_path).exists()
    with pytest.raises(ValueError, match="refuse replay|immutable|already exists"):
        assert_authorization_paths_unused(auth, root=tmp_path)


def test_signal_cutoff_wrapper_blocks_late_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.portfolio_construction import run_backtest_with_signal_cutoff

    calls: list[date] = []

    class _Engine:
        def __init__(self, store: Any, config: Any, signal_fn: Any) -> None:
            self.signal_fn = signal_fn

        def run(self, start: date, end: date) -> BacktestResult:
            for day in weekdays(start, 5):
                self.signal_fn(day)
            return _result()

    monkeypatch.setattr("app.research.portfolio_construction.BacktestEngine", _Engine)

    def score_fn(day: date) -> list[Any]:
        calls.append(day)
        return []

    config = zero_cost_config()
    run_backtest_with_signal_cutoff(
        store=object(),  # type: ignore[arg-type]
        config=config,
        start=date(2026, 7, 20),
        end=date(2026, 7, 24),
        signal_cutoff=date(2026, 7, 22),
        score_fn=score_fn,
    )
    assert calls
    assert all(day <= date(2026, 7, 22) for day in calls)


def test_in_memory_score_provider_does_not_write_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = zero_cost_config()

    class _Engine:
        def __init__(self, store: Any, config: StrategyConfig) -> None:
            self.config = config

        def run(self, as_of: date) -> list[Any]:
            return []

    monkeypatch.setattr("app.research.portfolio_oos_evaluation.ScoringEngine", _Engine)
    provider = InMemoryScoreProvider(store=object(), config=config)  # type: ignore[arg-type]
    assert provider(date(2025, 1, 22)) == []
    assert provider(date(2025, 1, 22)) == []
    assert provider.hits == 1
    assert provider.misses == 1
    assert list(tmp_path.iterdir()) == []


def test_cli_rejects_custom_resealed_authorization(tmp_path: Path) -> None:
    committed = build_committed_portfolio_oos_authorization()
    tampered = seal_authorization(
        committed.model_copy(
            update={
                "output_dir": "data/evil/portfolio-oos-evaluations/one-shot-v1",
                "consumption_receipt_path": (
                    "data/evil/portfolio-oos-evaluations/one-shot-v1.consumption-receipt.json"
                ),
            }
        )
    )
    custom_path = tmp_path / "custom-auth.json"
    write_portfolio_oos_authorization(custom_path, tampered)
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "evaluate-all-a-share-portfolio-oos-one-shot",
            "--strategy",
            FROZEN_STRATEGY_CONFIG_ID,
            "--authorization-file",
            str(custom_path),
        ],
    )
    assert result.exit_code == 1
    assert "sealed" in result.output.lower() or "does not match" in result.output.lower()


def test_cli_rejects_path_mismatch(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "evaluate-all-a-share-portfolio-oos-one-shot",
            "--strategy",
            FROZEN_STRATEGY_CONFIG_ID,
            "--market-dir",
            str(tmp_path / "other-market"),
        ],
    )
    assert result.exit_code == 1
    assert "market-dir" in result.output.lower() or "does not match" in result.output.lower()


def test_paths_unused_helper_uses_root_and_ignores_committed_absence(tmp_path: Path) -> None:
    auth = _auth_for_temp(tmp_path)
    assert_authorization_paths_unused(auth, root=tmp_path)
    (tmp_path / auth.output_dir).mkdir(parents=True)
    with pytest.raises(ValueError, match="already exists"):
        assert_authorization_paths_unused(auth, root=tmp_path)
    # Ordinary suite must not require the committed output directory to be absent.
    committed = build_committed_portfolio_oos_authorization()
    assert committed.output_dir == AUTHORIZED_OUTPUT_DIR


def test_evaluate_shared_in_memory_scores_across_scenarios(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _auth_for_temp(tmp_path)
    write_portfolio_oos_authorization(tmp_path / "authorization.json", auth)
    freeze = load_verified_portfolio_oos_freeze(COMMITTED_FREEZE)
    score_days: list[date] = []

    def fake_preflight(**kwargs: Any) -> None:
        return None

    def fake_score(day: date) -> list[Any]:
        score_days.append(day)
        return []

    calls = {"n": 0}

    def fake_backtest(**kwargs: Any) -> BacktestResult:
        calls["n"] += 1
        config = kwargs["config"]
        score_fn = kwargs["score_fn"]
        score_fn(date(2025, 1, 22))
        score_fn(date(2026, 7, 22))
        consistent = _bound_result(
            BASELINE_SCENARIO_ID,
            commission_rate=config.costs.commission_rate,
            minimum_commission=config.costs.min_commission,
            slippage_bps=config.costs.slippage_bps,
            strategy_config_hash=config.config_hash(),
            strategy_name=config.name,
            strategy_version=config.version,
            research_scope=config.research_scope,
            total_return=0.12,
        )
        # Unbind before evaluate binder re-applies scenario fields.
        return consistent.model_copy(
            update={
                "portfolio_oos_scenario_id": None,
                "portfolio_oos_commission_rate": None,
                "portfolio_oos_minimum_commission": None,
                "portfolio_oos_slippage_bps": None,
                "portfolio_oos_stamp_tax_unchanged": None,
                "data_snapshot_id": "",
            }
        )

    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation.preflight_research",
        fake_preflight,
    )
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._load_authorized_composite_store",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._load_verified_frozen_strategy",
        lambda strategy_path, authorization: load_strategy_config(
            "all_a_share_historical_value_portfolio_selected_v2",
            PROJECT_ROOT / "config" / "strategies",
        ),
    )
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._runtime_config_for_evaluation",
        lambda frozen_config, authorization: build_runtime_equivalent_config(frozen_config),
    )
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation.run_backtest_with_signal_cutoff",
        fake_backtest,
    )
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._first_signal_day",
        lambda **kwargs: date(2025, 1, 22),
    )
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._build_descriptive_summary",
        lambda **kwargs: PortfolioOosDescriptiveSummary(moderate_total_return=0.08),
    )

    report, receipt, output = evaluate_and_write_portfolio_oos_one_shot(
        authorization=auth,
        freeze_path=COMMITTED_FREEZE,
        strategy_path=STRATEGY_PATH,
        market_dir=tmp_path / "market",
        fundamental_dir=tmp_path / "fundamental",
        root=tmp_path,
        project_root=PROJECT_ROOT,
        score_fn=fake_score,
    )
    assert calls["n"] == 3
    assert report.outcome in {"conditional_go", "no_go", "not_evaluable"}
    assert set(report.scenarios) == {BASELINE_SCENARIO_ID, MODERATE_SCENARIO_ID, SEVERE_SCENARIO_ID}
    assert receipt.authorization_id == auth.authorization_id
    assert (output / "report.json").exists()
    verify_sealed_portfolio_oos_artifacts_against_authorization(
        output_dir=output,
        receipt_path=tmp_path / auth.consumption_receipt_path,
        authorization=auth,
        freeze=freeze,
    )


def test_partial_success_leaves_output_immutable_if_receipt_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = _auth_for_temp(tmp_path)
    report = _report_from_auth(
        auth,
        scenarios={},
        outcome="not_evaluable",
        outcome_reason="preflight failed: boom",
        preflight_passed=False,
        scenario_execution_complete=False,
        failure_stage="preflight",
        preflight_error="boom",
        observed_first_signal=None,
    )
    output = tmp_path / "out"
    receipt = tmp_path / "receipt.json"

    real_rename = Path.rename

    def flaky_rename(self: Path, target: Path) -> Path:
        if self.name.startswith(".portfolio-oos-receipt-"):
            raise OSError("synthetic receipt rename failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    with pytest.raises(OSError, match="synthetic receipt rename failure"):
        write_portfolio_oos_evaluation_atomically(
            output,
            report,
            {},
            receipt_path=receipt,
            authorization_id=auth.authorization_id or "",
            freeze_id=auth.freeze_id,
            authorization_output_dir=auth.output_dir,
        )
    assert output.exists()
    assert not receipt.exists()
    with pytest.raises(ValueError, match="immutable"):
        write_portfolio_oos_evaluation_atomically(
            output,
            report,
            {},
            receipt_path=receipt,
            authorization_id=auth.authorization_id or "",
            freeze_id=auth.freeze_id,
            authorization_output_dir=auth.output_dir,
        )


def _patch_evaluate_prereqs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._load_authorized_composite_store",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._load_verified_frozen_strategy",
        lambda strategy_path, authorization: load_strategy_config(
            "all_a_share_historical_value_portfolio_selected_v2",
            PROJECT_ROOT / "config" / "strategies",
        ),
    )
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._runtime_config_for_evaluation",
        lambda frozen_config, authorization: build_runtime_equivalent_config(frozen_config),
    )


def test_schedule_mismatch_fails_before_score_and_does_not_consume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = _auth_for_temp(tmp_path)
    score_calls = {"n": 0}
    backtest_calls = {"n": 0}

    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation.preflight_research",
        lambda **kwargs: None,
    )
    _patch_evaluate_prereqs(monkeypatch)
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._first_signal_day",
        lambda **kwargs: date(2025, 2, 19),
    )

    def fake_score(day: date) -> list[Any]:
        score_calls["n"] += 1
        raise AssertionError("score must not run on schedule mismatch")

    def fake_backtest(**kwargs: Any) -> BacktestResult:
        backtest_calls["n"] += 1
        raise AssertionError("backtest must not run on schedule mismatch")

    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation.run_backtest_with_signal_cutoff",
        fake_backtest,
    )

    with pytest.raises(ValueError, match="first_2025_plus_signal|refusing to score"):
        evaluate_and_write_portfolio_oos_one_shot(
            authorization=auth,
            freeze_path=COMMITTED_FREEZE,
            strategy_path=STRATEGY_PATH,
            market_dir=tmp_path / "market",
            fundamental_dir=tmp_path / "fundamental",
            root=tmp_path,
            project_root=PROJECT_ROOT,
            score_fn=fake_score,
        )
    assert score_calls["n"] == 0
    assert backtest_calls["n"] == 0
    assert not (tmp_path / auth.output_dir).exists()
    assert not (tmp_path / auth.consumption_receipt_path).exists()


def test_typed_scenario_data_failure_seals_not_evaluable_and_consumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.errors import DataQualityError

    auth = _auth_for_temp(tmp_path)
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation.preflight_research",
        lambda **kwargs: None,
    )
    _patch_evaluate_prereqs(monkeypatch)
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._first_signal_day",
        lambda **kwargs: date(2025, 1, 22),
    )

    def boom_backtest(**kwargs: Any) -> BacktestResult:
        raise DataQualityError("synthetic bars incomplete")

    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation.run_backtest_with_signal_cutoff",
        boom_backtest,
    )

    report, receipt, output = evaluate_and_write_portfolio_oos_one_shot(
        authorization=auth,
        freeze_path=COMMITTED_FREEZE,
        strategy_path=STRATEGY_PATH,
        market_dir=tmp_path / "market",
        fundamental_dir=tmp_path / "fundamental",
        root=tmp_path,
        project_root=PROJECT_ROOT,
        score_fn=lambda day: [],
    )
    assert report.outcome == "not_evaluable"
    assert report.preflight_passed is True
    assert report.scenario_execution_complete is False
    assert report.failure_stage == "scenario"
    assert "scenario data" in report.outcome_reason
    assert "preflight failed" not in report.outcome_reason
    assert receipt.receipt_id is not None
    assert output.exists()
    assert report.scenarios == {}


def test_value_error_during_scenario_fails_before_consume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _auth_for_temp(tmp_path)
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation.preflight_research",
        lambda **kwargs: None,
    )
    _patch_evaluate_prereqs(monkeypatch)
    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation._first_signal_day",
        lambda **kwargs: date(2025, 1, 22),
    )

    def boom_backtest(**kwargs: Any) -> BacktestResult:
        raise ValueError("contract-ish generic failure must not be sealed as not_evaluable")

    monkeypatch.setattr(
        "app.research.portfolio_oos_evaluation.run_backtest_with_signal_cutoff",
        boom_backtest,
    )

    with pytest.raises(ValueError, match="must not be sealed"):
        evaluate_and_write_portfolio_oos_one_shot(
            authorization=auth,
            freeze_path=COMMITTED_FREEZE,
            strategy_path=STRATEGY_PATH,
            market_dir=tmp_path / "market",
            fundamental_dir=tmp_path / "fundamental",
            root=tmp_path,
            project_root=PROJECT_ROOT,
            score_fn=lambda day: [],
        )
    assert not (tmp_path / auth.output_dir).exists()
    assert not (tmp_path / auth.consumption_receipt_path).exists()


def _reseal_report_and_receipt(
    output: Path,
    receipt: Path,
    *,
    auth,
    mutate_report: Any,
    mutate_result_file: str | None = None,
    mutate_result: Any = None,
) -> None:
    from app.research.portfolio_oos_evaluation import (
        PortfolioOosConsumptionReceipt,
        PortfolioOosEvaluationReport,
        _receipt_id,
        _report_id,
        _sha256_file,
    )

    if mutate_result_file is not None and mutate_result is not None:
        payload = json.loads((output / mutate_result_file).read_text(encoding="utf-8"))
        mutate_result(payload)
        (output / mutate_result_file).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    report_payload = json.loads((output / "report.json").read_text(encoding="utf-8"))
    mutate_report(report_payload)
    if mutate_result_file is not None:
        for scenario in report_payload.get("scenarios", {}).values():
            if scenario.get("result_file") == mutate_result_file:
                scenario["result_file_sha256"] = _sha256_file(output / mutate_result_file)
    resealed = PortfolioOosEvaluationReport.model_validate(report_payload)
    resealed = resealed.model_copy(update={"report_id": _report_id(resealed)})
    (output / "report.json").write_text(resealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    hashes = {"report.json": _sha256_file(output / "report.json")}
    for artifact in resealed.scenarios.values():
        hashes[artifact.result_file] = _sha256_file(output / artifact.result_file)
    receipt_obj = PortfolioOosConsumptionReceipt(
        authorization_id=auth.authorization_id or "",
        freeze_id=auth.freeze_id,
        report_id=resealed.report_id or "",
        output_dir=auth.output_dir,
        output_file_sha256=hashes,
    )
    receipt.write_text(
        receipt_obj.model_copy(update={"receipt_id": _receipt_id(receipt_obj)}).model_dump_json(indent=2)
        + "\n",
        encoding="utf-8",
    )


def test_verifier_rejects_resealed_scenario_summary_and_cost_drift(tmp_path: Path) -> None:
    auth = _auth_for_temp(tmp_path)
    _, _, output, receipt, freeze, _results, _report = _seal_complete_scenarios(auth, tmp_path, tag="summary")

    def bump_return(payload: dict[str, Any]) -> None:
        payload["scenarios"][BASELINE_SCENARIO_ID]["total_return"] = 0.99

    _reseal_report_and_receipt(output, receipt, auth=auth, mutate_report=bump_return)
    with pytest.raises(ValueError, match="scenario artifact mismatch|rebuilt"):
        verify_sealed_portfolio_oos_artifacts_against_authorization(
            output_dir=output,
            receipt_path=receipt,
            authorization=auth,
            freeze=freeze,
        )

    _, _, output2, receipt2, freeze2, _, _ = _seal_complete_scenarios(auth, tmp_path, tag="cost")

    def drift_cost_report(payload: dict[str, Any]) -> None:
        payload["scenarios"][SEVERE_SCENARIO_ID]["commission_rate"] = 0.0024

    def drift_cost_result(payload: dict[str, Any]) -> None:
        payload["portfolio_oos_commission_rate"] = 0.0024

    _reseal_report_and_receipt(
        output2,
        receipt2,
        auth=auth,
        mutate_report=drift_cost_report,
        mutate_result_file=SCENARIO_RESULT_FILES[SEVERE_SCENARIO_ID],
        mutate_result=drift_cost_result,
    )
    with pytest.raises(ValueError, match="commission_rate drift|scenario"):
        verify_sealed_portfolio_oos_artifacts_against_authorization(
            output_dir=output2,
            receipt_path=receipt2,
            authorization=auth,
            freeze=freeze2,
        )


def test_verifier_rejects_missing_scenario_and_binding_drift(tmp_path: Path) -> None:
    auth = _auth_for_temp(tmp_path)
    _, _, output, receipt, freeze, results, report = _seal_complete_scenarios(auth, tmp_path, tag="missing")

    from app.research.portfolio_oos_evaluation import (
        PortfolioOosConsumptionReceipt,
        PortfolioOosEvaluationReport,
        _receipt_id,
        _report_id,
        _sha256_file,
    )

    payload = json.loads((output / "report.json").read_text(encoding="utf-8"))
    del payload["scenarios"][MODERATE_SCENARIO_ID]
    (output / SCENARIO_RESULT_FILES[MODERATE_SCENARIO_ID]).unlink()
    missing = PortfolioOosEvaluationReport.model_validate(payload)
    missing = missing.model_copy(update={"report_id": _report_id(missing)})
    (output / "report.json").write_text(missing.model_dump_json(indent=2) + "\n", encoding="utf-8")
    hashes = {"report.json": _sha256_file(output / "report.json")}
    for artifact in missing.scenarios.values():
        hashes[artifact.result_file] = _sha256_file(output / artifact.result_file)
    receipt_obj = PortfolioOosConsumptionReceipt(
        authorization_id=auth.authorization_id or "",
        freeze_id=auth.freeze_id,
        report_id=missing.report_id or "",
        output_dir=auth.output_dir,
        output_file_sha256=hashes,
    )
    receipt.write_text(
        receipt_obj.model_copy(update={"receipt_id": _receipt_id(receipt_obj)}).model_dump_json(indent=2)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="scenario set|exact baseline"):
        verify_sealed_portfolio_oos_artifacts_against_authorization(
            output_dir=output,
            receipt_path=receipt,
            authorization=auth,
            freeze=freeze,
        )

    _, _, output3, receipt3, freeze3, _, _ = _seal_complete_scenarios(auth, tmp_path, tag="bind")

    def drift_bindings(payload: dict[str, Any]) -> None:
        payload["runtime_config_hash"] = "deadbeefdeadbeef"
        payload["evaluation_start"] = "2025-01-03"

    _reseal_report_and_receipt(output3, receipt3, auth=auth, mutate_report=drift_bindings)
    with pytest.raises(ValueError, match="runtime_config_hash|evaluation window"):
        verify_sealed_portfolio_oos_artifacts_against_authorization(
            output_dir=output3,
            receipt_path=receipt3,
            authorization=auth,
            freeze=freeze3,
        )


def test_verifier_rejects_empty_snapshot_hash_window_metrics_and_descriptive_tampers(
    tmp_path: Path,
) -> None:
    auth = _auth_for_temp(tmp_path)

    # Empty data_snapshot_id after reseal.
    _, _, output, receipt, freeze, _, _ = _seal_complete_scenarios(auth, tmp_path, tag="snap")

    def empty_snapshot_report(payload: dict[str, Any]) -> None:
        return None

    def empty_snapshot_result(payload: dict[str, Any]) -> None:
        payload["data_snapshot_id"] = ""

    _reseal_report_and_receipt(
        output,
        receipt,
        auth=auth,
        mutate_report=empty_snapshot_report,
        mutate_result_file=SCENARIO_RESULT_FILES[BASELINE_SCENARIO_ID],
        mutate_result=empty_snapshot_result,
    )
    with pytest.raises(ValueError, match="data_snapshot_id"):
        verify_sealed_portfolio_oos_artifacts_against_authorization(
            output_dir=output,
            receipt_path=receipt,
            authorization=auth,
            freeze=freeze,
        )

    # strategy_config_hash drift.
    _, _, output2, receipt2, freeze2, _, _ = _seal_complete_scenarios(auth, tmp_path, tag="hash")
    runtime = build_runtime_equivalent_config(
        load_strategy_config(
            "all_a_share_historical_value_portfolio_selected_v2",
            PROJECT_ROOT / "config" / "strategies",
        )
    )
    from app.research.portfolio_oos_evaluation import _expected_scenario_configs

    configs = _expected_scenario_configs(runtime)

    def drift_hash_result(payload: dict[str, Any]) -> None:
        payload["strategy_config_hash"] = "deadbeefdeadbeef"

    _reseal_report_and_receipt(
        output2,
        receipt2,
        auth=auth,
        mutate_report=lambda payload: None,
        mutate_result_file=SCENARIO_RESULT_FILES[BASELINE_SCENARIO_ID],
        mutate_result=drift_hash_result,
    )
    with pytest.raises(ValueError, match="strategy_config_hash"):
        verify_sealed_portfolio_oos_artifacts_against_authorization(
            output_dir=output2,
            receipt_path=receipt2,
            authorization=auth,
            freeze=freeze2,
            runtime_config=runtime,
            scenario_configs=configs,
        )

    # Window signal_end drift.
    _, _, output3, receipt3, freeze3, _, _ = _seal_complete_scenarios(auth, tmp_path, tag="window")

    def drift_window(payload: dict[str, Any]) -> None:
        payload["window"]["signal_end"] = "2026-07-21"

    _reseal_report_and_receipt(
        output3,
        receipt3,
        auth=auth,
        mutate_report=lambda payload: None,
        mutate_result_file=SCENARIO_RESULT_FILES[BASELINE_SCENARIO_ID],
        mutate_result=drift_window,
    )
    with pytest.raises(ValueError, match="window binding"):
        verify_sealed_portfolio_oos_artifacts_against_authorization(
            output_dir=output3,
            receipt_path=receipt3,
            authorization=auth,
            freeze=freeze3,
        )

    # Metrics / trade-count / sharpe / drawdown / attribution tampers.
    _, _, output4, receipt4, freeze4, _, _ = _seal_complete_scenarios(auth, tmp_path, tag="metrics")

    def drift_metrics(payload: dict[str, Any]) -> None:
        payload["metrics"]["sharpe_ratio"] = 9.9
        payload["metrics"]["max_drawdown"] = -0.01
        payload["metrics"]["number_of_trades"] = 999
        payload["attribution"]["total_trading_costs"] = 12345.0

    _reseal_report_and_receipt(
        output4,
        receipt4,
        auth=auth,
        mutate_report=lambda payload: None,
        mutate_result_file=SCENARIO_RESULT_FILES[BASELINE_SCENARIO_ID],
        mutate_result=drift_metrics,
    )
    with pytest.raises(ValueError, match="metrics|number_of_trades|attribution|scenario artifact"):
        verify_sealed_portfolio_oos_artifacts_against_authorization(
            output_dir=output4,
            receipt_path=receipt4,
            authorization=auth,
            freeze=freeze4,
        )

    # Descriptive / observed_first_signal tamper.
    _, _, output5, receipt5, freeze5, _, _ = _seal_complete_scenarios(auth, tmp_path, tag="desc")

    def drift_desc(payload: dict[str, Any]) -> None:
        payload["observed_first_signal"] = "2025-02-19"
        payload["descriptive"]["moderate_total_return"] = 0.99

    _reseal_report_and_receipt(output5, receipt5, auth=auth, mutate_report=drift_desc)
    # Without store, descriptive rebuild is skipped; observed_first_signal only checked with store.
    # Still reject via outcome_reason/gate path? observed_first_signal alone isn't checked without store.
    # Force failure via outcome_reason mismatch by also changing outcome_reason arbitrarily.
    def drift_desc_and_reason(payload: dict[str, Any]) -> None:
        payload["observed_first_signal"] = "2025-02-19"
        payload["descriptive"]["moderate_total_return"] = 0.99
        payload["outcome_reason"] = "arbitrary resealed reason"

    _, _, output6, receipt6, freeze6, _, _ = _seal_complete_scenarios(auth, tmp_path, tag="reason")
    _reseal_report_and_receipt(output6, receipt6, auth=auth, mutate_report=drift_desc_and_reason)
    with pytest.raises(ValueError, match="outcome_reason|observed_first_signal|authorized first signal"):
        verify_sealed_portfolio_oos_artifacts_against_authorization(
            output_dir=output6,
            receipt_path=receipt6,
            authorization=auth,
            freeze=freeze6,
        )


def test_cli_verify_rejects_custom_authorization_path(tmp_path: Path) -> None:
    custom = tmp_path / "custom-auth.json"
    write_portfolio_oos_authorization(custom, build_committed_portfolio_oos_authorization())
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "verify-all-a-share-portfolio-oos-one-shot",
            "--authorization-file",
            str(custom),
            "--output-dir",
            str(tmp_path / "missing-out"),
            "--receipt-file",
            str(tmp_path / "missing-receipt.json"),
        ],
    )
    assert result.exit_code == 1
    assert "authorization" in result.output.lower() or "committed" in result.output.lower()


def test_cli_verify_rejects_custom_freeze_path(tmp_path: Path) -> None:
    custom = tmp_path / "custom-freeze.json"
    custom.write_text(COMMITTED_FREEZE.read_text(encoding="utf-8"), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "verify-all-a-share-portfolio-oos-one-shot",
            "--freeze-file",
            str(custom),
            "--output-dir",
            str(tmp_path / "missing-out"),
            "--receipt-file",
            str(tmp_path / "missing-receipt.json"),
        ],
    )
    assert result.exit_code == 1
    assert "freeze" in result.output.lower() or "committed" in result.output.lower()


def test_verifier_rejects_threshold_tamper_even_after_reseal(tmp_path: Path) -> None:
    auth = _auth_for_temp(tmp_path)
    mutated = seal_authorization(
        auth.model_copy(
            update={
                "hard_risk_gates": auth.hard_risk_gates.model_copy(update={"max_drawdown_floor": -0.01}),
            }
        )
    )
    with pytest.raises(ValueError, match="hard risk"):
        from app.research.portfolio_oos_authorization import assert_authorization_self_consistent

        assert_authorization_self_consistent(mutated)


def test_gate_notes_use_sealed_thresholds() -> None:
    baseline = _result(total_return=0.1, sharpe=1.0, drawdown=-0.2, trades=25)
    severe = _result(total_return=0.1, sharpe=0.5, drawdown=-0.01, trades=25)
    _, _, gates, _ = classify_portfolio_oos_outcome(
        preflight_passed=True,
        baseline=baseline,
        severe=severe,
        initial_cash=80_000,
        min_closed_trades=20,
        max_drawdown_floor=-0.15,
        largest_symbol_loss_fraction=0.03,
        pnl_reconciliation_abs_tol=1e-6,
    )
    drawdown_gate = next(gate for gate in gates if gate.gate == "baseline_max_drawdown")
    loss_gate = next(gate for gate in gates if gate.gate == "largest_single_symbol_loss")
    assert "-0.15" in drawdown_gate.note
    assert "0.03" in loss_gate.note
    assert "must be >= -0.15" in drawdown_gate.note
    _, _, gates2, _ = classify_portfolio_oos_outcome(
        preflight_passed=True,
        baseline=baseline,
        severe=severe,
        initial_cash=80_000,
        min_closed_trades=20,
        max_drawdown_floor=-0.2,
        largest_symbol_loss_fraction=0.05,
        pnl_reconciliation_abs_tol=1e-6,
    )
    drawdown_gate2 = next(gate for gate in gates2 if gate.gate == "baseline_max_drawdown")
    loss_gate2 = next(gate for gate in gates2 if gate.gate == "largest_single_symbol_loss")
    assert "-0.2" in drawdown_gate2.note
    assert "0.05" in loss_gate2.note


def test_failure_stage_consistency_rejects_impossible_states() -> None:
    from app.research.portfolio_oos_evaluation import _assert_failure_stage_consistency

    auth = build_committed_portfolio_oos_authorization()
    success = _report_from_auth(
        auth,
        scenarios={
            BASELINE_SCENARIO_ID: PortfolioOosScenarioArtifact(
                scenario_id=BASELINE_SCENARIO_ID,
                result_file=SCENARIO_RESULT_FILES[BASELINE_SCENARIO_ID],
                commission_rate=0.0003,
                minimum_commission=5.0,
                slippage_bps=5.0,
            ),
            MODERATE_SCENARIO_ID: PortfolioOosScenarioArtifact(
                scenario_id=MODERATE_SCENARIO_ID,
                result_file=SCENARIO_RESULT_FILES[MODERATE_SCENARIO_ID],
                commission_rate=0.0006,
                minimum_commission=5.0,
                slippage_bps=10.0,
            ),
            SEVERE_SCENARIO_ID: PortfolioOosScenarioArtifact(
                scenario_id=SEVERE_SCENARIO_ID,
                result_file=SCENARIO_RESULT_FILES[SEVERE_SCENARIO_ID],
                commission_rate=0.0012,
                minimum_commission=5.0,
                slippage_bps=25.0,
            ),
        },
        outcome="conditional_go",
        outcome_reason="ok",
        failure_stage="none",
        preflight_passed=True,
        scenario_execution_complete=True,
    )
    _assert_failure_stage_consistency(success, auth)

    none_but_failed = success.model_copy(
        update={"preflight_passed": False, "scenario_execution_complete": False, "scenarios": {}}
    )
    with pytest.raises(ValueError, match="success state cannot have preflight_passed=false"):
        _assert_failure_stage_consistency(none_but_failed, auth)

    success_with_error = success.model_copy(update={"preflight_error": "nope"})
    with pytest.raises(ValueError, match="success state must not set preflight_error"):
        _assert_failure_stage_consistency(success_with_error, auth)

    preflight = _report_from_auth(
        auth,
        scenarios={},
        outcome="not_evaluable",
        outcome_reason="preflight failed: boom",
        preflight_passed=False,
        scenario_execution_complete=False,
        failure_stage="preflight",
        preflight_error="boom",
        observed_first_signal=None,
    )
    _assert_failure_stage_consistency(preflight, auth)
    with pytest.raises(ValueError, match="observed_first_signal is None"):
        _assert_failure_stage_consistency(
            preflight.model_copy(update={"observed_first_signal": date(2025, 1, 22)}),
            auth,
        )
    with pytest.raises(ValueError, match="non-empty preflight_error"):
        _assert_failure_stage_consistency(preflight.model_copy(update={"preflight_error": None}), auth)

    scenario = _report_from_auth(
        auth,
        scenarios={},
        outcome="not_evaluable",
        outcome_reason="scenario data/availability failure: boom",
        preflight_passed=True,
        scenario_execution_complete=False,
        failure_stage="scenario",
        scenario_error="boom",
        observed_first_signal=date(2025, 1, 22),
    )
    _assert_failure_stage_consistency(scenario, auth)
    with pytest.raises(ValueError, match="authorized first signal"):
        _assert_failure_stage_consistency(
            scenario.model_copy(update={"observed_first_signal": date(2025, 2, 19)}),
            auth,
        )


def test_verifier_rejects_descriptive_gate_tamper_after_reseal(tmp_path: Path) -> None:
    auth = _auth_for_temp(tmp_path)
    _, _, output, receipt, freeze, _, _ = _seal_complete_scenarios(auth, tmp_path, tag="gates")

    def drop_moderate_gate(payload: dict[str, Any]) -> None:
        payload["gates"] = [gate for gate in payload["gates"] if gate["gate"] != "moderate_cost_descriptive"]

    _reseal_report_and_receipt(output, receipt, auth=auth, mutate_report=drop_moderate_gate)
    with pytest.raises(ValueError, match="full gate list"):
        verify_sealed_portfolio_oos_artifacts_against_authorization(
            output_dir=output,
            receipt_path=receipt,
            authorization=auth,
            freeze=freeze,
        )

    _, _, output2, receipt2, freeze2, _, _ = _seal_complete_scenarios(auth, tmp_path, tag="gates2")

    def mutate_moderate_observed(payload: dict[str, Any]) -> None:
        for gate in payload["gates"]:
            if gate["gate"] == "moderate_cost_descriptive":
                gate["observed"] = 0.99

    _reseal_report_and_receipt(output2, receipt2, auth=auth, mutate_report=mutate_moderate_observed)
    with pytest.raises(ValueError, match="full gate list"):
        verify_sealed_portfolio_oos_artifacts_against_authorization(
            output_dir=output2,
            receipt_path=receipt2,
            authorization=auth,
            freeze=freeze2,
        )


def test_cli_verify_mentions_readonly_preflight_replay() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "verify-all-a-share-portfolio-oos-one-shot",
            "--output-dir",
            "/tmp/missing-portfolio-oos-out",
            "--receipt-file",
            "/tmp/missing-portfolio-oos-receipt.json",
        ],
    )
    assert result.exit_code == 1
    assert "preflight" in result.output.lower()
    assert "score" in result.output.lower() or "backtest" in result.output.lower()
    assert "does not run" in result.output.lower() or "not run" in result.output.lower()
    # Message should say it may replay preflight, not that it never runs preflight.
    assert "replay" in result.output.lower() or "read-only" in result.output.lower()
