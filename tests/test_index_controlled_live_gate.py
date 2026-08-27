from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from app.cli import app
from app.research.index_controlled_live_gate import (
    DEFAULT_LIVE_GATE_PATH,
    IndexControlledLiveInputGate,
    verify_index_controlled_live_input_gate,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = CliRunner()


def _payload() -> dict[str, object]:
    return json.loads((PROJECT_ROOT / DEFAULT_LIVE_GATE_PATH).read_text())


def test_committed_controlled_live_gate_is_self_hashed_and_closed() -> None:
    gate = verify_index_controlled_live_input_gate(repo_root=PROJECT_ROOT)
    assert all(value is None for value in gate.missing_manual_inputs.values())
    assert gate.product_review["exact_research_proxy_match"] is False
    assert gate.authorization_boundary["capital_deployment_authorized"] is False
    assert gate.authorization_boundary["broker_connection_authorized"] is False
    assert gate.readiness["manual_inputs_complete"] is False
    assert gate.readiness["ready_for_orders"] is False
    assert gate.readiness["ready_for_trading"] is False


def test_controlled_live_gate_cli_keeps_manual_inputs_and_orders_closed() -> None:
    result = RUNNER.invoke(
        app,
        ["verify-index-controlled-live-input-gate", "--repo-root", str(PROJECT_ROOT)],
    )
    assert result.exit_code == 0, result.output
    assert "manual_inputs_complete=false" in result.output
    assert "capital_deployment_authorized=false" in result.output
    assert "ready_for_orders=false" in result.output


def test_controlled_live_gate_rejects_inferred_manual_input_or_authorization() -> None:
    payload = _payload()
    inputs = payload["missing_manual_inputs"]
    assert isinstance(inputs, dict)
    inputs["broker_legal_name"] = "inferred broker"
    with pytest.raises(ValidationError, match="must remain explicitly missing"):
        IndexControlledLiveInputGate.model_validate(payload)

    payload = _payload()
    boundary = payload["authorization_boundary"]
    assert isinstance(boundary, dict)
    boundary["order_submission_authorized"] = True
    with pytest.raises(ValidationError, match="authorization boundary drifted"):
        IndexControlledLiveInputGate.model_validate(payload)


def test_controlled_live_gate_self_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    payload = _payload()
    binding = payload["shadow_protocol_binding"]
    assert isinstance(binding, dict)
    binding["path"] = "config/research/tampered-protocol.json"
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="self-hash mismatch"):
        verify_index_controlled_live_input_gate(repo_root=tmp_path, path=gate_path)
