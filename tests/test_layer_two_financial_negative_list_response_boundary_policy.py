from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from app.research.layer_two_financial_negative_list_response_boundary_policy import (
    BOUND_BASE_PROTOCOL_PATH,
    POLICY_FILE_PATH,
    POLICY_FILE_PATH_V1,
    POLICY_FILE_PATH_V3,
    POLICY_REASON_CODE,
    POLICY_VERSION_V1,
    POLICY_VERSION_V3,
    compute_policy_id,
    evaluate_response_boundary,
    verify_policy_file,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_golden_response_boundary_policy_verifies() -> None:
    policy, result = verify_policy_file(repo_root=REPO_ROOT)

    assert result.policy_id == policy.policy_id
    assert result.policy_file_sha256 == _sha256(REPO_ROOT / POLICY_FILE_PATH)
    assert result.quarantine_reason_code == POLICY_REASON_CODE


def test_legacy_v1_response_boundary_policy_still_verifies() -> None:
    policy, result = verify_policy_file(repo_root=REPO_ROOT, policy_path=Path(POLICY_FILE_PATH_V1))

    assert policy.policy_version == POLICY_VERSION_V1
    assert result.policy_file_sha256 == _sha256(REPO_ROOT / POLICY_FILE_PATH_V1)


def test_v3_offline_finalization_policy_verifies() -> None:
    policy, result = verify_policy_file(repo_root=REPO_ROOT, policy_path=Path(POLICY_FILE_PATH_V3))

    assert policy.policy_version == POLICY_VERSION_V3
    assert result.policy_file_sha256 == _sha256(REPO_ROOT / POLICY_FILE_PATH_V3)


def test_response_boundary_decisions_are_narrow_and_fail_closed() -> None:
    allowed = evaluate_response_boundary(
        endpoint="balancesheet",
        ann_date=date(2024, 3, 20),
        f_ann_date=date(2024, 4, 1),
        end_date=date(2023, 12, 31),
    )
    quarantined = evaluate_response_boundary(
        endpoint="balancesheet",
        ann_date=date(2024, 3, 20),
        f_ann_date=date(2026, 4, 29),
        end_date=date(2023, 12, 31),
    )
    rejected_ann = evaluate_response_boundary(
        endpoint="balancesheet",
        ann_date=date(2025, 1, 1),
        f_ann_date=None,
        end_date=date(2024, 12, 31),
    )
    rejected_end = evaluate_response_boundary(
        endpoint="income",
        ann_date=date(2024, 3, 20),
        f_ann_date=None,
        end_date=date(2025, 3, 31),
    )

    assert allowed.action == "allow"
    assert quarantined.action == "quarantine"
    assert quarantined.reason_code == POLICY_REASON_CODE
    assert quarantined.effective_disclosure_date == date(2026, 4, 29)
    assert rejected_ann.action == "reject"
    assert rejected_end.action == "reject"


@pytest.mark.parametrize("endpoint", ["fina_indicator", "fina_audit"])
def test_future_ann_date_report_period_rows_are_metadata_only_quarantine(endpoint: str) -> None:
    decision = evaluate_response_boundary(
        endpoint=endpoint,
        ann_date=date(2025, 3, 15),
        f_ann_date=None,
        end_date=date(2024, 12, 31),
    )
    assert decision.action == "quarantine"
    assert decision.reason_code == POLICY_REASON_CODE
    assert decision.effective_disclosure_date == date(2025, 3, 15)


def test_future_ann_date_shape_remains_rejected_by_v1_or_wrong_endpoint() -> None:
    legacy = evaluate_response_boundary(
        endpoint="fina_indicator",
        ann_date=date(2025, 3, 15),
        f_ann_date=None,
        end_date=date(2024, 12, 31),
        policy_version=POLICY_VERSION_V1,
    )
    wrong_endpoint = evaluate_response_boundary(
        endpoint="balancesheet",
        ann_date=date(2025, 3, 15),
        f_ann_date=None,
        end_date=date(2024, 12, 31),
    )
    assert legacy.action == "reject"
    assert wrong_endpoint.action == "reject"


def test_policy_self_seal_tamper_fails(tmp_path: Path) -> None:
    policy_path = tmp_path / POLICY_FILE_PATH
    policy_path.parent.mkdir(parents=True)
    payload = json.loads((REPO_ROOT / POLICY_FILE_PATH).read_text(encoding="utf-8"))
    payload["purpose"] = "tampered"
    policy_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="self-seal mismatch"):
        verify_policy_file(repo_root=tmp_path)


def test_policy_false_semantic_flag_fails_even_if_resealed(tmp_path: Path) -> None:
    protocol_path = tmp_path / BOUND_BASE_PROTOCOL_PATH
    protocol_path.parent.mkdir(parents=True)
    protocol_path.write_bytes((REPO_ROOT / BOUND_BASE_PROTOCOL_PATH).read_bytes())
    policy_path = tmp_path / POLICY_FILE_PATH
    payload = json.loads((REPO_ROOT / POLICY_FILE_PATH).read_text(encoding="utf-8"))
    payload["do_not_persist_future_payload_values"] = False
    payload["policy_id"] = compute_policy_id(payload)
    policy_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="fail-closed semantics mismatch"):
        verify_policy_file(repo_root=tmp_path)


def test_policy_parent_symlink_is_rejected(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "policy.json").write_bytes((REPO_ROOT / POLICY_FILE_PATH).read_bytes())
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        verify_policy_file(repo_root=tmp_path, policy_path=Path("linked/policy.json"))
