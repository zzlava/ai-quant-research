from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.research.index_risk_budget_closeout import (
    IndexRiskBudgetCloseoutProtocol,
    verify_index_risk_budget_closeout,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _payload() -> dict[str, object]:
    path = _root() / "config/research/index-risk-budget-closeout-protocol-v1.json"
    return json.loads(path.read_text())


def test_real_closeout_is_source_bound_and_fail_closed() -> None:
    protocol = verify_index_risk_budget_closeout(repo_root=_root())
    assert protocol.closeout_id == (
        "d14149395f0061b3b6b9295339653a2af62c746b5844c55a94956821bf155328"
    )
    assert protocol.family_closure["outcome"] == (
        "effect_real_but_insufficient_and_too_costly"
    )
    assert protocol.allocation_policy["maximum_drawdown_utility_budget"] == -0.30
    assert protocol.allocation_policy["equity_policy_starting_weight"] == 0.30
    assert protocol.rebalance_policy["rule"] == "annual_calendar"
    assert not any(protocol.authorization_boundary.values())
    assert not any(protocol.readiness.values())


def test_parameter_rescue_and_consumed_history_reuse_are_rejected() -> None:
    payload = _payload()
    closure = payload["family_closure"]
    assert isinstance(closure, dict)
    closure["parameter_rescue_forbidden"] = False
    with pytest.raises(ValidationError, match="family closeout rule drifted"):
        IndexRiskBudgetCloseoutProtocol.model_validate(payload)

    payload = _payload()
    boundaries = payload["consumed_data_boundaries"]
    assert isinstance(boundaries, dict)
    boundaries["reuse_for_new_dynamic_rule_development_forbidden"] = False
    with pytest.raises(ValidationError, match="reuse must remain forbidden"):
        IndexRiskBudgetCloseoutProtocol.model_validate(payload)


def test_allocation_or_authorization_drift_is_rejected() -> None:
    payload = _payload()
    allocation = payload["allocation_policy"]
    assert isinstance(allocation, dict)
    allocation["maximum_drawdown_utility_budget"] = -0.20
    with pytest.raises(ValidationError, match="utility budget drifted"):
        IndexRiskBudgetCloseoutProtocol.model_validate(payload)

    payload = _payload()
    authorization = payload["authorization_boundary"]
    assert isinstance(authorization, dict)
    authorization["trading_authorized"] = True
    with pytest.raises(ValidationError, match="cannot authorize downstream action"):
        IndexRiskBudgetCloseoutProtocol.model_validate(payload)


def test_self_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    payload = _payload()
    bindings = payload["file_bindings"]
    assert isinstance(bindings, dict)
    engine = bindings["historical_replay_engine"]
    assert isinstance(engine, dict)
    engine["sha256"] = "0" * 64
    path = tmp_path / "closeout.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="self-hash mismatch"):
        verify_index_risk_budget_closeout(repo_root=tmp_path, path=path)


def test_cli_verifier_is_read_only_and_explicit() -> None:
    result = CliRunner().invoke(
        cli_app,
        ["verify-index-risk-budget-closeout", "--repo-root", str(_root())],
    )
    assert result.exit_code == 0, result.output
    assert "Read-only closeout verification" in result.output
    assert "maximum_drawdown_utility_budget=-0.30" in result.output
    assert "consumed_history_reuse_forbidden=true" in result.output
    assert "capital_deployment_authorized=false" in result.output
    assert "ready_for_trading=false" in result.output
