from __future__ import annotations

from pathlib import Path

import pytest

from app.research.layer_one_risk_lock_recovery_policy import (
    EXPECTED_POLICY_ID,
    LayerOneRiskLockRecoveryPolicy,
    build_policy,
    compute_policy_id,
    verify_policy,
    verify_policy_file,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_factory_and_disk_policy_match_exactly() -> None:
    factory = verify_policy(build_policy())
    disk = verify_policy_file(repo_root=_repo_root())
    assert factory.policy_id == EXPECTED_POLICY_ID
    assert disk.model_dump(mode="json") == factory.model_dump(mode="json")
    assert disk.live_recovery.explicit_user_confirmation_required is True
    assert disk.live_recovery.first_reentry_budget_cap == 0.3
    assert disk.historical_counterfactual.may_not_be_used_for_oos_claim is True


def test_resealed_relaxations_fail_factory_verification() -> None:
    policy = build_policy()
    tampered = policy.model_copy(
        update={
            "live_recovery": policy.live_recovery.model_copy(
                update={"auto_clear_forbidden": False}
            )
        }
    )
    tampered = tampered.model_copy(update={"policy_id": compute_policy_id(tampered)})
    with pytest.raises(ValueError, match="sealed factory"):
        verify_policy(tampered)


def test_policy_rejects_ready_escalation_and_binding_drift() -> None:
    payload = build_policy().model_dump(mode="json", exclude={"policy_id"})
    payload["ready_for_trading"] = True
    with pytest.raises(ValueError):
        LayerOneRiskLockRecoveryPolicy.model_validate(payload)
    payload = build_policy().model_dump(mode="json", exclude={"policy_id"})
    payload["layer_one_index_protocol_id"] = "0" * 64
    with pytest.raises(ValueError, match="protocol binding"):
        LayerOneRiskLockRecoveryPolicy.model_validate(payload)
