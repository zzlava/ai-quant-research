from __future__ import annotations

from pathlib import Path

import pytest

from app.research.deflated_sharpe_audit import (
    DsrFormulaInputs,
    build_deflated_sharpe_audit,
    calculate_deflated_sharpe,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_formula_is_numeric_only_with_complete_consistent_inputs() -> None:
    result = calculate_deflated_sharpe(
        DsrFormulaInputs(
            observed_daily_sharpe=0.08,
            trial_daily_sharpe_stddev=0.02,
            n_return_observations=500,
            return_skewness=0.1,
            return_pearson_kurtosis=3.2,
            n_effective_independent_trials=10,
        )
    )
    assert 0 <= result.deflated_sharpe_probability <= 1
    assert result.one_sided_p_value == pytest.approx(1 - result.deflated_sharpe_probability)


def test_current_audit_binds_available_numbers_but_stays_not_evaluable() -> None:
    report = build_deflated_sharpe_audit(repo_root=_root())
    assert report.n_return_observations > 1000
    assert report.registered_trial_count_lower_bound > 1
    assert report.ledger_complete is False
    assert report.status == "not_evaluable"
    assert report.numeric_dsr is None
    assert set(report.missing_bindings) == {
        "trial_daily_sharpe_stddev",
        "n_effective_independent_trials",
    }
    assert report.ready_for_trading is False
