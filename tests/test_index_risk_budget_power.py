from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.research.index_risk_budget_power import (
    build_index_risk_budget_power_review,
    verify_index_risk_budget_power_protocol,
    verify_index_risk_budget_power_review,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_committed_power_protocol_and_static_only_review_verify() -> None:
    protocol = verify_index_risk_budget_power_protocol(repo_root=PROJECT_ROOT)
    assert protocol.policies["dynamic_candidate_returns_must_not_be_loaded_for_calibration"] is True
    review = verify_index_risk_budget_power_review(repo_root=PROJECT_ROOT)
    assert review.dynamic_candidate_returns_loaded is False
    assert review.consumes_oos is False
    assert review.family_outcome == "evaluable_for_sealed_mde"
    assert review.sealed_mde_calmar_difference <= 0.1


def test_static_only_power_review_is_deterministic() -> None:
    first = build_index_risk_budget_power_review(repo_root=PROJECT_ROOT)
    second = build_index_risk_budget_power_review(repo_root=PROJECT_ROOT)
    assert first == second


def test_power_protocol_source_hash_drift_fails() -> None:
    source = PROJECT_ROOT / "config/research/index-risk-budget-power-protocol-v1.json"
    payload = json.loads(source.read_text())
    payload["source_bindings"]["trial_ledger"]["sha256"] = "0" * 64
    without_id = {key: value for key, value in payload.items() if key != "protocol_id"}
    payload["protocol_id"] = __import__("hashlib").sha256(
        json.dumps(without_id, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (PROJECT_ROOT / "tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "tmp") as temporary:
        copied = Path(temporary) / "power.json"
        copied.write_text(json.dumps(payload))
        with pytest.raises(ValueError, match="source hash mismatch"):
            verify_index_risk_budget_power_protocol(repo_root=PROJECT_ROOT, path=copied)
