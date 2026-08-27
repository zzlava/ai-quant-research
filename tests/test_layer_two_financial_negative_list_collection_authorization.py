from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.research.layer_two_financial_negative_list_collection_authorization import (
    FinancialNegativeListCollectionAuthorization,
    compute_authorization_id,
    verify_collection_authorization_file,
)
from app.research.layer_two_financial_negative_list_collection_run_contract import (
    DEFAULT_RUN_CONTRACT_PATH,
    RunContractVerificationResult,
    load_run_contract,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _base_authorization_payload() -> dict[str, object]:
    run_contract = load_run_contract(REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH)
    return {
        "schema_version": "1",
        "authorization_version": "financial-negative-list-collection-authorization-v1",
        "authorization_scope": "historical_financial_negative_list_collection_only",
        "not_trading_authorization": True,
        "allows_resume_until_collection_manifest_complete": True,
        "run_contract_id": run_contract.run_contract_id,
        "protocol_id": run_contract.e11b_2a_protocol_id,
        "protocol_file_sha256": run_contract.e11b_2a_protocol_file_sha256,
        "staging_dir": run_contract.fixed_staging_dir,
        "canonical_symbol_count": run_contract.canonical_symbol_count,
        "canonical_symbols_sha256": run_contract.canonical_symbols_sha256,
        "expected_partition_count": run_contract.expected_partition_count,
        "source_endpoints": [item.tushare_api for item in run_contract.source_endpoints],
        "user_authorization_phrase": "I explicitly authorize historical financial-negative-list collection only.",
        "authorization_date": "2026-08-27",
        "authorization_time": "00:00:00",
        "authorization_timezone": "Asia/Shanghai",
        "network_collection_allowed": True,
        "ready_for_scoring": False,
        "ready_for_backtest": False,
        "ready_for_trading": False,
        "authorization_id": None,
    }


def _write_payload(payload: dict[str, object]) -> Path:
    path = REPO_ROOT / "tmp" / f"fn-authorization-test-{uuid4().hex}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def _seal_payload(payload: dict[str, object]) -> dict[str, object]:
    model = FinancialNegativeListCollectionAuthorization.model_validate(payload)
    sealed = model.model_copy(update={"authorization_id": compute_authorization_id(model)})
    return sealed.model_dump(mode="json")


def test_authorization_missing_file_fails() -> None:
    with pytest.raises(FileNotFoundError):
        verify_collection_authorization_file(
            authorization_path=REPO_ROOT / "tmp" / "missing-auth.json",
            repo_root=REPO_ROOT,
            run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
        )


def test_authorization_self_hash_mismatch_fails() -> None:
    payload = _seal_payload(_base_authorization_payload())
    payload["authorization_id"] = "0" * 64
    path = _write_payload(payload)
    try:
        with pytest.raises(ValueError, match="self-seal"):
            verify_collection_authorization_file(
                authorization_path=path,
                repo_root=REPO_ROOT,
                run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
            )
    finally:
        path.unlink()


def test_authorization_binding_drift_fails() -> None:
    payload = _base_authorization_payload()
    payload["protocol_file_sha256"] = "1" * 64
    sealed = _seal_payload(payload)
    path = _write_payload(sealed)
    try:
        with pytest.raises(ValueError, match="protocol_file_sha256 mismatch"):
            verify_collection_authorization_file(
                authorization_path=path,
                repo_root=REPO_ROOT,
                run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
            )
    finally:
        path.unlink()


def test_authorization_false_ready_flag_fails_closed() -> None:
    sealed = _seal_payload(_base_authorization_payload())
    sealed["ready_for_backtest"] = True
    path = _write_payload(sealed)
    try:
        with pytest.raises(Exception, match="False|ready_for_backtest"):
            verify_collection_authorization_file(
                authorization_path=path,
                repo_root=REPO_ROOT,
                run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
            )
    finally:
        path.unlink()


def test_authorization_staging_path_mismatch_fails() -> None:
    payload = _base_authorization_payload()
    payload["staging_dir"] = "data/raw/another-dir"
    sealed = _seal_payload(payload)
    path = _write_payload(sealed)
    try:
        with pytest.raises(ValueError, match="staging_dir mismatch"):
            verify_collection_authorization_file(
                authorization_path=path,
                repo_root=REPO_ROOT,
                run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
            )
    finally:
        path.unlink()


def test_authorization_date_before_prepared_at_fails() -> None:
    payload = _base_authorization_payload()
    payload["authorization_date"] = "2026-08-26"
    sealed = _seal_payload(payload)
    path = _write_payload(sealed)
    try:
        with pytest.raises(ValueError, match="must not be earlier than prepared_at"):
            verify_collection_authorization_file(
                authorization_path=path,
                repo_root=REPO_ROOT,
                run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
            )
    finally:
        path.unlink()


def test_authorization_date_in_future_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _base_authorization_payload()
    payload["authorization_date"] = "2026-08-28"
    sealed = _seal_payload(payload)
    path = _write_payload(sealed)
    try:
        fixed_now = datetime(2026, 8, 27, 23, 30, 0)
        monkeypatch.setattr(
            "app.research.layer_two_financial_negative_list_collection_authorization.datetime",
            type(
                "_FixedDateTime",
                (),
                {
                    "now": staticmethod(lambda _tz=None: fixed_now if _tz is None else fixed_now.replace(tzinfo=_tz)),
                    "combine": staticmethod(datetime.combine),
                },
            ),
        )
        with pytest.raises(ValueError, match="must not be later than current Asia/Shanghai date"):
            verify_collection_authorization_file(
                authorization_path=path,
                repo_root=REPO_ROOT,
                run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
            )
    finally:
        path.unlink()


def test_authorization_file_outside_repo_root_rejected(tmp_path: Path) -> None:
    sealed = _seal_payload(_base_authorization_payload())
    auth_path = tmp_path / "authorization.json"
    auth_path.write_text(json.dumps(sealed, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inside repo_root"):
        verify_collection_authorization_file(
            authorization_path=auth_path,
            repo_root=REPO_ROOT,
            run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
        )


def test_authorization_parent_symlink_rejected(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    real_auth_dir = repo_root / "real-auth"
    link_auth_dir = repo_root / "auth-link"
    real_contract_dir = repo_root / "config" / "research"
    real_auth_dir.mkdir(parents=True)
    real_contract_dir.mkdir(parents=True)
    link_auth_dir.symlink_to(real_auth_dir, target_is_directory=True)

    contract = (REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH).read_text(encoding="utf-8")
    (real_contract_dir / DEFAULT_RUN_CONTRACT_PATH.name).write_text(contract, encoding="utf-8")
    payload = _seal_payload(_base_authorization_payload())
    auth_path = real_auth_dir / "auth.json"
    auth_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="symlink"):
        verify_collection_authorization_file(
            authorization_path=Path("auth-link/auth.json"),
            repo_root=repo_root,
            run_contract_path=Path("config/research") / DEFAULT_RUN_CONTRACT_PATH.name,
        )


def test_authorization_rejects_preverified_contract_without_result() -> None:
    sealed = _seal_payload(_base_authorization_payload())
    path = _write_payload(sealed)
    run_contract = load_run_contract(REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH)
    try:
        with pytest.raises(
            ValueError,
            match="must both be provided or both be omitted",
        ):
            verify_collection_authorization_file(
                authorization_path=path,
                repo_root=REPO_ROOT,
                run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
                preverified_run_contract=run_contract,
            )
    finally:
        path.unlink()


def test_authorization_rejects_preverified_result_without_contract() -> None:
    sealed = _seal_payload(_base_authorization_payload())
    path = _write_payload(sealed)
    run_contract = load_run_contract(REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH)
    preverified_result = RunContractVerificationResult(
        run_contract_id=str(run_contract.run_contract_id),
        status=run_contract.status,
        network_authorized=run_contract.network_authorized,
        requires_fresh_user_authorization=run_contract.requires_fresh_user_authorization,
        canonical_symbol_count=run_contract.canonical_symbol_count,
        canonical_symbols_sha256=run_contract.canonical_symbols_sha256,
        expected_partition_count=run_contract.expected_partition_count,
        run_contract_version=run_contract.run_contract_version,
        fixed_staging_dir=run_contract.fixed_staging_dir,
        raw_stock_basic_source_path=run_contract.raw_stock_basic_source_path,
    )
    try:
        with pytest.raises(
            ValueError,
            match="must both be provided or both be omitted",
        ):
            verify_collection_authorization_file(
                authorization_path=path,
                repo_root=REPO_ROOT,
                run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
                preverified_run_contract_result=preverified_result,
            )
    finally:
        path.unlink()


def test_authorization_rejects_preverified_binding_mismatch() -> None:
    sealed = _seal_payload(_base_authorization_payload())
    path = _write_payload(sealed)
    run_contract = load_run_contract(REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH)
    preverified_result = RunContractVerificationResult(
        run_contract_id="f" * 64,
        status=run_contract.status,
        network_authorized=run_contract.network_authorized,
        requires_fresh_user_authorization=run_contract.requires_fresh_user_authorization,
        canonical_symbol_count=run_contract.canonical_symbol_count,
        canonical_symbols_sha256=run_contract.canonical_symbols_sha256,
        expected_partition_count=run_contract.expected_partition_count,
        run_contract_version=run_contract.run_contract_version,
        fixed_staging_dir=run_contract.fixed_staging_dir,
        raw_stock_basic_source_path=run_contract.raw_stock_basic_source_path,
    )
    try:
        with pytest.raises(ValueError, match="preverified run contract result mismatch"):
            verify_collection_authorization_file(
                authorization_path=path,
                repo_root=REPO_ROOT,
                run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
                preverified_run_contract=run_contract,
                preverified_run_contract_result=preverified_result,
            )
    finally:
        path.unlink()


def test_authorization_without_preverified_inputs_runs_full_contract_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed = _seal_payload(_base_authorization_payload())
    path = _write_payload(sealed)
    run_contract = load_run_contract(REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH)
    preverified_result = RunContractVerificationResult(
        run_contract_id=str(run_contract.run_contract_id),
        status=run_contract.status,
        network_authorized=run_contract.network_authorized,
        requires_fresh_user_authorization=run_contract.requires_fresh_user_authorization,
        canonical_symbol_count=run_contract.canonical_symbol_count,
        canonical_symbols_sha256=run_contract.canonical_symbols_sha256,
        expected_partition_count=run_contract.expected_partition_count,
        run_contract_version=run_contract.run_contract_version,
        fixed_staging_dir=run_contract.fixed_staging_dir,
        raw_stock_basic_source_path=run_contract.raw_stock_basic_source_path,
    )
    calls = {"count": 0}

    def _fake_verify_run_contract_file(**_: object) -> tuple[object, object]:
        calls["count"] += 1
        return run_contract, preverified_result

    monkeypatch.setattr(
        "app.research.layer_two_financial_negative_list_collection_authorization.verify_run_contract_file",
        _fake_verify_run_contract_file,
    )
    try:
        verify_collection_authorization_file(
            authorization_path=path,
            repo_root=REPO_ROOT,
            run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
        )
        assert calls["count"] == 1
    finally:
        path.unlink()


@pytest.mark.parametrize(
    "authorization_name",
    [
        "financial-negative-list-collection-authorization-20260826.json",
        "financial-negative-list-collection-authorization-20260827.json",
    ],
)
def test_old_authorization_is_rejected_against_default_v3_run_contract(authorization_name: str) -> None:
    with pytest.raises(ValueError, match="run_contract_id mismatch"):
        verify_collection_authorization_file(
            authorization_path=REPO_ROOT / "config/research" / authorization_name,
            repo_root=REPO_ROOT,
            run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
        )
