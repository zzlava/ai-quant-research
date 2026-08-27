from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.research.index_etf_risk_budget_protocol import (
    IndexEtfRiskBudgetProtocol,
    verify_index_etf_risk_budget_protocol,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _payload() -> dict[str, object]:
    path = _root() / "config/research/index-etf-risk-budget-research-protocol-v1.json"
    return json.loads(path.read_text())


def test_real_protocol_is_source_bound_and_implementation_ready_but_not_authorized() -> None:
    protocol = verify_index_etf_risk_budget_protocol(repo_root=_root())
    assert protocol.status == "implementation_complete_pending_prominent_manual_run_confirmation"
    assert protocol.research_family["family_id"] == "index_time_series_risk_budget_v1"
    assert protocol.design_authorization["historical_replay_authorized"] is False
    assert protocol.design_authorization["prospective_evaluation_authorized"] is False
    assert protocol.readiness["implementation_complete"] is True
    assert protocol.readiness["ready_for_authorized_historical_replay"] is True
    assert protocol.readiness["manual_confirmation_present"] is False
    assert protocol.readiness["ready_for_backtest"] is False
    assert protocol.readiness["ready_for_trading"] is False
    assert [item["realized_volatility_lookback_trading_days"] for item in protocol.dynamic_candidates] == [
        20,
        60,
    ]


def test_ex_post_matched_exposure_cannot_be_confirmatory() -> None:
    payload = _payload()
    static_controls = payload["static_controls"]
    assert isinstance(static_controls, dict)
    matched = static_controls["ex_post_average_exposure_matched_control"]
    assert isinstance(matched, dict)
    matched["confirmatory_baseline"] = True
    with pytest.raises(ValidationError, match="cannot be confirmatory"):
        IndexEtfRiskBudgetProtocol.model_validate(payload)


def test_parameter_scan_or_third_candidate_is_rejected() -> None:
    payload = _payload()
    family = payload["research_family"]
    assert isinstance(family, dict)
    family["parameter_scanning_forbidden"] = False
    with pytest.raises(ValidationError, match="research-family fail-closed"):
        IndexEtfRiskBudgetProtocol.model_validate(payload)

    payload = _payload()
    candidates = payload["dynamic_candidates"]
    assert isinstance(candidates, list)
    candidates.append(dict(candidates[0]))
    with pytest.raises(ValidationError, match="exactly two dynamic candidates"):
        IndexEtfRiskBudgetProtocol.model_validate(payload)


def test_primary_endpoint_mde_and_drawdown_gate_cannot_drift() -> None:
    payload = _payload()
    endpoints = payload["endpoints"]
    assert isinstance(endpoints, dict)
    primary = endpoints["primary"]
    assert isinstance(primary, dict)
    primary["metric"] = "net_sharpe"
    with pytest.raises(ValidationError, match="primary endpoint drifted"):
        IndexEtfRiskBudgetProtocol.model_validate(payload)

    payload = _payload()
    endpoints = payload["endpoints"]
    assert isinstance(endpoints, dict)
    hard_gates = endpoints["hard_gates"]
    assert isinstance(hard_gates, dict)
    hard_gates["maximum_drawdown_floor"] = -0.25
    with pytest.raises(ValidationError, match="drawdown utility constraint"):
        IndexEtfRiskBudgetProtocol.model_validate(payload)


def test_readiness_and_oos_boundaries_fail_closed() -> None:
    payload = _payload()
    readiness = payload["readiness"]
    assert isinstance(readiness, dict)
    readiness["ready_for_backtest"] = True
    with pytest.raises(ValidationError, match="readiness boundary"):
        IndexEtfRiskBudgetProtocol.model_validate(payload)

    payload = _payload()
    windows = payload["evaluation_windows"]
    assert isinstance(windows, dict)
    consumed = windows["consumed_oos"]
    assert isinstance(consumed, dict)
    consumed["reuse_forbidden"] = False
    with pytest.raises(ValidationError, match="reuse must remain forbidden"):
        IndexEtfRiskBudgetProtocol.model_validate(payload)


def test_self_hash_mismatch_fails_before_any_run(tmp_path: Path) -> None:
    payload = _payload()
    scope = payload["scope"]
    assert isinstance(scope, dict)
    scope["question"] = "changed after seal"
    copied = tmp_path / "protocol.json"
    copied.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="self-hash mismatch"):
        verify_index_etf_risk_budget_protocol(repo_root=tmp_path, path=copied)
