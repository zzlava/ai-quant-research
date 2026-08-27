from __future__ import annotations

import json
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

import app.research.layer_two_financial_negative_list_collection_run_contract as run_contract_module
from app.research.layer_two_financial_negative_list_collection_run_contract import (
    DEFAULT_RUN_CONTRACT_PATH,
    FIXED_STAGING_DIR_V1,
    FIXED_STAGING_DIR_V2,
    FIXED_STAGING_DIR_V3,
    LEGACY_RUN_CONTRACT_PATH,
    PREPARED_AT,
    RUN_CONTRACT_PATH_V2,
    build_prepared_run_contract,
    compute_run_contract_id,
    load_run_contract,
    seal_run_contract,
    verify_run_contract_file,
    write_run_contract,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_run_contract_golden_verification_and_real_bindings() -> None:
    contract, result = verify_run_contract_file(
        run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
        repo_root=REPO_ROOT,
    )
    assert contract.run_contract_id == result.run_contract_id
    assert contract.status == "prepared_not_authorized"
    assert contract.network_authorized is False
    assert contract.requires_fresh_user_authorization is True
    assert result.canonical_symbol_count == 5544
    assert result.expected_partition_count == 22176
    assert result.fixed_staging_dir == FIXED_STAGING_DIR_V3
    assert result.run_contract_version == "financial-negative-list-collection-run-contract-v3"
    assert result.response_boundary_policy_id is not None
    assert result.response_boundary_reason_code == "FNLD-013"


def test_legacy_v1_run_contract_still_verifiable() -> None:
    contract, result = verify_run_contract_file(
        run_contract_path=REPO_ROOT / LEGACY_RUN_CONTRACT_PATH,
        repo_root=REPO_ROOT,
    )
    assert contract.run_contract_version == "financial-negative-list-collection-run-contract-v1"
    assert result.fixed_staging_dir == FIXED_STAGING_DIR_V1
    assert result.response_boundary_policy_id is None


def test_v2_run_contract_still_verifiable_against_v1_policy() -> None:
    contract, result = verify_run_contract_file(
        run_contract_path=REPO_ROOT / RUN_CONTRACT_PATH_V2,
        repo_root=REPO_ROOT,
    )
    assert contract.run_contract_version == "financial-negative-list-collection-run-contract-v2"
    assert result.fixed_staging_dir == FIXED_STAGING_DIR_V2
    assert result.response_boundary_policy_path == (
        "config/research/financial-negative-list-response-boundary-policy-v1.json"
    )


def test_run_contract_rebuild_matches_on_disk_contract() -> None:
    rebuilt = seal_run_contract(build_prepared_run_contract(REPO_ROOT))
    on_disk = load_run_contract(REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH)
    assert rebuilt.run_contract_id == on_disk.run_contract_id
    assert compute_run_contract_id(rebuilt) == str(rebuilt.run_contract_id)


def test_run_contract_verification_cwd_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.chdir(Path(tmp))
        _, result = verify_run_contract_file(
            run_contract_path=DEFAULT_RUN_CONTRACT_PATH,
            repo_root=REPO_ROOT,
        )
    assert result.run_contract_id


def test_run_contract_verification_uses_full_protocol_file_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"full": 0}
    real_full_verifier = run_contract_module.verify_protocol_file

    def _wrapped(repo_root: Path):
        calls["full"] += 1
        return real_full_verifier(repo_root)

    monkeypatch.setattr(run_contract_module, "verify_protocol_file", _wrapped)
    contract, _result = verify_run_contract_file(
        run_contract_path=REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH,
        repo_root=REPO_ROOT,
    )
    assert calls["full"] == 1
    assert contract.prepared_at == PREPARED_AT


def test_run_contract_id_tamper_fails() -> None:
    payload = json.loads((REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH).read_text(encoding="utf-8"))
    payload["run_contract_id"] = "0" * 64
    path = REPO_ROOT / "tmp" / f"run-contract-tamper-{uuid4().hex}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="self-seal"):
            verify_run_contract_file(run_contract_path=path, repo_root=REPO_ROOT)
    finally:
        path.unlink(missing_ok=True)


def test_run_contract_binding_drift_symbol_count_fails() -> None:
    payload = json.loads((REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH).read_text(encoding="utf-8"))
    payload["canonical_symbol_count"] = payload["canonical_symbol_count"] + 1
    payload["expected_partition_count"] = payload["canonical_symbol_count"] * 4
    payload["run_contract_id"] = None
    tmp_source = write_temp_json(payload)
    mutated = seal_run_contract(load_run_contract(tmp_source))
    tmp_source.unlink(missing_ok=True)
    path = REPO_ROOT / "tmp" / f"run-contract-mutated-{uuid4().hex}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(mutated.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(ValueError, match="canonical_symbol_count drift"):
            verify_run_contract_file(run_contract_path=path, repo_root=REPO_ROOT)
    finally:
        path.unlink(missing_ok=True)


def test_run_contract_is_not_authorization_file() -> None:
    contract = load_run_contract(REPO_ROOT / DEFAULT_RUN_CONTRACT_PATH)
    assert contract.not_authorization_file is True
    assert contract.network_authorized is False
    assert contract.requires_fresh_user_authorization is True
    assert contract.fixed_staging_dir == FIXED_STAGING_DIR_V3


def write_temp_json(payload: dict[str, object]) -> Path:
    path = REPO_ROOT / "tmp" / f"run-contract-test-{uuid4().hex}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def test_write_contract_roundtrip() -> None:
    built = build_prepared_run_contract(REPO_ROOT)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "contract.json"
        sealed = write_run_contract(out, built)
        loaded = load_run_contract(out)
    assert sealed.run_contract_id == loaded.run_contract_id


def test_run_contract_relative_escape_path_fails(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="inside repo_root"):
        verify_run_contract_file(
            run_contract_path=Path("../outside.json"),
            repo_root=repo_root,
        )


def test_run_contract_absolute_escape_path_fails(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="inside repo_root"):
        verify_run_contract_file(
            run_contract_path=outside,
            repo_root=REPO_ROOT,
        )


def test_run_contract_parent_symlink_path_fails(tmp_path: Path) -> None:
    real = tmp_path / "real"
    link_parent = tmp_path / "link-parent"
    real.mkdir()
    link_parent.symlink_to(real, target_is_directory=True)
    run_contract_file = real / "contract.json"
    run_contract_file.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="symlink"):
        verify_run_contract_file(
            run_contract_path=Path("link-parent/contract.json"),
            repo_root=tmp_path,
        )
