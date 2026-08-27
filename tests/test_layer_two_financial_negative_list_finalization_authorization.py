from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.research.layer_two_financial_negative_list_finalization_authorization import (
    FINALIZATION_AUTHORIZATION_PATH,
    compute_finalization_authorization_id,
    verify_finalization_authorization_file,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _bound_request() -> dict[str, str]:
    return {
        "request_id": "465f7d74a30a463b67746134430b629ec7d6b7d4c181c76c4fad95c8675aa75f",
        "collection_authorization_id": "505c88aa3fee39b2c78c09bf5684ef3bdf6f87bea159f9f59ff4616fdf77af83",
        "verified_run_contract_id": "ca092dafc663ae8b125b1183977e2c42c6874b5342367e212ac5fbbacdb2310b",
        "verified_response_boundary_policy_id": (
            "dd5ad210aab073d8dc5b625535be57df75c7c8c2026b52dc61c2210a3d96598c"
        ),
    }


def test_golden_finalization_authorization_verifies() -> None:
    request = _bound_request()
    authorization, result = verify_finalization_authorization_file(
        repo_root=REPO_ROOT,
        request=request,
    )

    assert result.authorization_id == authorization.authorization_id
    assert result.nullable_end_type_endpoints == ("balancesheet", "income")
    assert authorization.network_access_allowed is False
    assert authorization.future_payload_values_allowed is False


def test_finalization_authorization_wrong_request_fails() -> None:
    request = _bound_request()
    request["request_id"] = "0" * 64

    with pytest.raises(ValueError, match="original_request_id mismatch"):
        verify_finalization_authorization_file(repo_root=REPO_ROOT, request=request)


def test_finalization_authorization_self_seal_tamper_fails(tmp_path: Path) -> None:
    payload = json.loads((REPO_ROOT / FINALIZATION_AUTHORIZATION_PATH).read_text(encoding="utf-8"))
    payload["user_authorization_phrase"] = "tampered"
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    request = _bound_request()

    with pytest.raises(ValueError, match="self-seal mismatch"):
        verify_finalization_authorization_file(
            repo_root=tmp_path,
            request=request,
            authorization_path=Path("authorization.json"),
        )


def test_authorization_id_recompute_matches_golden() -> None:
    payload = json.loads((REPO_ROOT / FINALIZATION_AUTHORIZATION_PATH).read_text(encoding="utf-8"))
    assert compute_finalization_authorization_id(payload) == payload["authorization_id"]
