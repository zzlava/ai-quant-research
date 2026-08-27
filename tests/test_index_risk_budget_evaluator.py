from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.research.index_research_product_cost_contract import CostScenario
from app.research.index_risk_budget_evaluator import (
    REQUIRED_CONFIRMATION_TEXT,
    compute_authorization_id,
    simulate_arm,
    verify_run_authorization,
)


def _synthetic_inputs() -> tuple[list[date], list[float], list[float], list[float]]:
    start = date(2020, 1, 1)
    dates = [start + timedelta(days=index) for index in range(180)]
    risk = [100.0 * (1.0004**index) * (1.0 + 0.01 * ((index % 7) - 3) / 3) for index in range(180)]
    equity = [100.0 * (1.0005**index) for index in range(180)]
    defensive = [100.0 * (1.0001**index) for index in range(180)]
    return dates, risk, equity, defensive


def test_static_arm_costs_and_dynamic_timing_are_deterministic() -> None:
    dates, risk, equity, defensive = _synthetic_inputs()
    scenario = CostScenario(
        commission_rate_per_side=0.00025,
        minimum_commission_cny_per_leg=5.0,
        slippage_bps_per_side=5.0,
        equity_proxy_annual_drag=0.006,
        defensive_proxy_annual_drag=0.003,
    )
    static_metrics, static_frame = simulate_arm(
        arm_id="static",
        dates=dates,
        risk_levels=risk,
        equity_levels=equity,
        defensive_levels=defensive,
        scenario_label="base",
        scenario=scenario,
        static_weight=0.6,
    )
    dynamic_metrics, dynamic_frame = simulate_arm(
        arm_id="dynamic",
        dates=dates,
        risk_levels=risk,
        equity_levels=equity,
        defensive_levels=defensive,
        scenario_label="base",
        scenario=scenario,
        volatility_lookback=20,
    )
    assert static_metrics.rebalance_count == 1
    assert dynamic_metrics.rebalance_count > 1
    assert static_frame.get_column("date")[0] == dates[61]
    assert dynamic_frame.get_column("date")[0] == dates[61]


def test_arm_rejects_missing_or_misaligned_input() -> None:
    dates, risk, equity, defensive = _synthetic_inputs()
    scenario = CostScenario(
        commission_rate_per_side=0.0,
        minimum_commission_cny_per_leg=0.0,
        slippage_bps_per_side=0.0,
        equity_proxy_annual_drag=0.0,
        defensive_proxy_annual_drag=0.0,
    )
    with pytest.raises(ValueError, match="not aligned"):
        simulate_arm(
            arm_id="bad",
            dates=dates,
            risk_levels=risk[:-1],
            equity_levels=equity,
            defensive_levels=defensive,
            scenario_label="base",
            scenario=scenario,
            static_weight=0.5,
        )


def test_run_authorization_requires_exact_prominent_confirmation(tmp_path: Path) -> None:
    payload = {
        "schema_version": "1",
        "authorization_version": "index-risk-budget-run-authorization-v1",
        "authorization_id": "0" * 64,
        "confirmed_at": "2026-08-27T12:00:00+08:00",
        "confirmation_text": REQUIRED_CONFIRMATION_TEXT + "修改",
        "protocol_id": "1" * 64,
        "trial_ledger_id": "2" * 64,
        "defensive_snapshot_id": "3" * 64,
        "product_cost_contract_id": "4" * 64,
        "power_review_id": "5" * 64,
        "historical_window": "2005-01-04..2024-12-31",
        "one_time_historical_replay_authorized": True,
        "prospective_or_oos_evaluation_authorized": False,
        "scoring_or_stock_selection_authorized": False,
        "portfolio_construction_authorized": False,
        "orders_or_trading_authorized": False,
    }
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="PROMINENT MANUAL CONFIRMATION REQUIRED"):
        verify_run_authorization(repo_root=tmp_path, path=Path("auth.json"))


def test_authorization_id_excludes_only_its_self_hash() -> None:
    from app.research.index_risk_budget_evaluator import IndexRiskBudgetRunAuthorization

    authorization = IndexRiskBudgetRunAuthorization(
        schema_version="1",
        authorization_version="index-risk-budget-run-authorization-v1",
        authorization_id="0" * 64,
        confirmed_at="2026-08-27T12:00:00+08:00",
        confirmation_text=REQUIRED_CONFIRMATION_TEXT,
        protocol_id="1" * 64,
        trial_ledger_id="2" * 64,
        defensive_snapshot_id="3" * 64,
        product_cost_contract_id="4" * 64,
        power_review_id="5" * 64,
        historical_window="2005-01-04..2024-12-31",
        one_time_historical_replay_authorized=True,
        prospective_or_oos_evaluation_authorized=False,
        scoring_or_stock_selection_authorized=False,
        portfolio_construction_authorized=False,
        orders_or_trading_authorized=False,
    )
    assert compute_authorization_id(authorization) != authorization.authorization_id


def test_cli_run_gate_is_prominent_and_fails_closed_without_authorization(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        cli_app,
        ["run-index-risk-budget-historical-replay", "--repo-root", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "PROMINENT MANUAL CONFIRMATION GATE" in result.output
    assert "PROMINENT MANUAL CONFIRMATION REQUIRED" in result.output
