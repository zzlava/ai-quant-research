from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import CONFIG_DIR, PROJECT_ROOT

# These modules intentionally recompute against large, hash-bound artifacts under
# the git-ignored data/ tree.  They remain part of the default local suite, but a
# clean GitHub runner cannot manufacture those artifacts without invalidating the
# very bindings the tests protect.
_LOCAL_DATA_MODULES = {
    "test_defensive_leg_history.py",
    "test_deflated_sharpe_audit.py",
    "test_experiment_ledger.py",
    "test_index_etf_risk_budget_protocol.py",
    "test_index_research_product_cost_contract.py",
    "test_index_risk_budget_closeout.py",
    "test_index_risk_budget_power.py",
    "test_layer_one_historical_validation.py",
    "test_layer_one_index_data_evidence.py",
    "test_layer_one_index_protocol.py",
    "test_layer_one_persistence.py",
    "test_layer_one_recovery_counterfactual.py",
    "test_layer_one_regime.py",
    "test_layer_two_alpha_development_protocol.py",
    "test_layer_two_alpha_input_bundle_v2.py",
    "test_layer_two_alpha_v2_freeze_bundle.py",
    "test_layer_two_cash_occupancy_attribution.py",
    "test_layer_two_constraint_assembler.py",
    "test_layer_two_entry_execution_diagnostic.py",
    "test_layer_two_evaluation_machine.py",
    "test_layer_two_financial_negative_list_collection_authorization.py",
    "test_layer_two_financial_negative_list_collection_run_contract.py",
    "test_layer_two_financial_negative_list_data_protocol.py",
    "test_layer_two_fixed_horizon_exit_diagnostic.py",
    "test_layer_two_hypothetical_position_lifecycle.py",
    "test_layer_two_longitudinal_state_transitions.py",
    "test_layer_two_stateful_allocator.py",
    "test_layer_two_tranche_phase_schedule.py",
    "test_portfolio_oos_evaluation.py",
    "test_portfolio_oos_freeze.py",
    "test_research_plan_stop_rule.py",
    "test_statistical_power_gate.py",
    "test_tranche_evaluation_protocol.py",
    "test_two_layer_contract.py",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    local_data = pytest.mark.local_data
    for item in items:
        if Path(str(item.fspath)).name in _LOCAL_DATA_MODULES:
            item.add_marker(local_data)


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def strategy_config_dir() -> Path:
    return CONFIG_DIR
