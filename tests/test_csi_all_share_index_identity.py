"""Attack-oriented tests for the sealed CSI All Share identity contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.research.csi_all_share_index_identity import (
    DEFAULT_CONTRACT_PATH,
    EXPECTED_CURRENT_CONTRACT_ID,
    NET_RETURN_TS_CODE,
    PRICE_TS_CODE,
    TOTAL_RETURN_TS_CODE,
    CSIAllShareIndexIdentityContract,
    build_csi_all_share_index_identity_v1,
    compute_contract_id,
    seal_contract,
    verify_contract,
    verify_contract_file,
)
from tests.helpers import PROJECT_ROOT

COMMITTED_PATH = PROJECT_ROOT / DEFAULT_CONTRACT_PATH
MODULE_PATH = PROJECT_ROOT / "src/app/research/csi_all_share_index_identity.py"


def _temp_repo(tmp_path: Path, payload: dict[str, object]) -> Path:
    target = tmp_path / DEFAULT_CONTRACT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return tmp_path


def test_committed_contract_verifies_and_keeps_strict_readiness_gate() -> None:
    contract, result = verify_contract_file(repo_root=PROJECT_ROOT)
    assert contract.contract_id == EXPECTED_CURRENT_CONTRACT_ID
    assert result.contract_id == EXPECTED_CURRENT_CONTRACT_ID
    assert result.structural_ok is True
    assert result.canonical_factory_binding_ok is True
    assert result.disk_binding_ok is True
    assert result.factual_identity_verified is True
    assert result.price_series_ready_for_long_history_materialization is True
    assert result.total_return_series_ready_for_strict_long_history_materialization is True
    assert result.ready_for_scoring is False
    assert result.ready_for_backtest is False
    assert result.ready_for_trading is False
    assert result.auto_apply is False

    identities = {item.return_definition: item for item in contract.identities}
    assert identities["price_index"].tushare_ts_code == PRICE_TS_CODE
    assert identities["total_return"].tushare_ts_code == TOTAL_RETURN_TS_CODE
    assert identities["net_return"].tushare_ts_code == NET_RETURN_TS_CODE
    assert contract.no_token_value_recorded is True
    assert contract.no_market_values_recorded is True
    assert contract.no_consumed_oos_used is True


def test_coverage_probe_records_source_gap_without_synthesis() -> None:
    contract = build_csi_all_share_index_identity_v1()
    coverage = {item.tushare_ts_code: item for item in contract.coverage_probes}
    assert coverage[PRICE_TS_CODE].returned_rows == 4858
    assert coverage[TOTAL_RETURN_TS_CODE].returned_rows == 4857
    assert coverage[PRICE_TS_CODE].duplicate_key_rows == 0
    assert coverage[TOTAL_RETURN_TS_CODE].duplicate_key_rows == 0
    assert coverage[TOTAL_RETURN_TS_CODE].required_field_null_counts == {
        "trade_date": 0,
        "close": 0,
        "pre_close": 0,
    }
    assert coverage[TOTAL_RETURN_TS_CODE].optional_field_null_counts == {
        "open": 4857,
        "high": 4857,
        "low": 4857,
    }
    assert [item.isoformat() for item in contract.coverage_cross_check.price_only_trade_dates] == [
        "2011-08-02"
    ]
    assert contract.coverage_cross_check.total_return_only_trade_dates == []
    assert contract.coverage_cross_check.missing_dates_must_not_be_synthesized is True
    recovery = contract.official_recovery_probe
    assert recovery.returned_rows == 4860
    assert [item.isoformat() for item in recovery.source_dates_outside_sse_open_calendar] == [
        "2005-01-01",
        "2018-06-18",
    ]
    assert [item.isoformat() for item in recovery.official_only_valid_trading_dates] == [
        "2011-08-02"
    ]
    assert [item.isoformat() for item in recovery.fixed_official_override_dates] == [
        "2011-08-02",
        "2011-08-03",
    ]
    assert recovery.no_interpolation_or_forward_fill is True
    assert recovery.official_source_rows_must_be_hash_bound_before_materialization is True


def test_factory_and_committed_json_are_identical() -> None:
    factory = build_csi_all_share_index_identity_v1()
    committed = CSIAllShareIndexIdentityContract.model_validate_json(COMMITTED_PATH.read_text(encoding="utf-8"))
    assert factory.model_dump(mode="json") == committed.model_dump(mode="json")
    assert factory.contract_id == compute_contract_id(factory) == EXPECTED_CURRENT_CONTRACT_ID


def test_plain_self_hash_tamper_is_rejected() -> None:
    contract = build_csi_all_share_index_identity_v1()
    tampered = contract.model_copy(update={"contract_id": "ab" * 32})
    with pytest.raises(ValueError, match="contract_id"):
        verify_contract(tampered)


def test_valid_outer_reseal_cannot_change_symbol_gap_or_source_hash() -> None:
    base = build_csi_all_share_index_identity_v1().model_dump(mode="json")

    changed_symbol = json.loads(json.dumps(base))
    changed_symbol["identities"][0]["tushare_ts_code"] = "000985.SH"
    changed_symbol.pop("contract_id", None)
    resealed_symbol = seal_contract(CSIAllShareIndexIdentityContract.model_validate(changed_symbol))
    assert resealed_symbol.contract_id == compute_contract_id(resealed_symbol)
    with pytest.raises(ValueError, match="canonical factual factory"):
        verify_contract(resealed_symbol)

    removed_gap = json.loads(json.dumps(base))
    removed_gap["coverage_cross_check"]["price_only_trade_dates"] = []
    removed_gap.pop("contract_id", None)
    resealed_gap = seal_contract(CSIAllShareIndexIdentityContract.model_validate(removed_gap))
    with pytest.raises(ValueError, match="canonical factual factory"):
        verify_contract(resealed_gap)

    changed_hash = json.loads(json.dumps(base))
    changed_hash["evidence_sources"][0]["content_sha256_at_access"] = "cd" * 32
    changed_hash.pop("contract_id", None)
    resealed_hash = seal_contract(CSIAllShareIndexIdentityContract.model_validate(changed_hash))
    with pytest.raises(ValueError, match="canonical factual factory"):
        verify_contract(resealed_hash)


def test_extra_fields_naive_timestamps_and_relaxed_gates_are_rejected() -> None:
    base = build_csi_all_share_index_identity_v1().model_dump(mode="json")
    base.pop("contract_id", None)

    extra = json.loads(json.dumps(base))
    extra["secret_ready"] = True
    with pytest.raises(ValidationError):
        CSIAllShareIndexIdentityContract.model_validate(extra)

    naive = json.loads(json.dumps(base))
    naive["observed_at"] = "2026-08-27T03:25:00"
    with pytest.raises(ValidationError, match="timezone-aware"):
        CSIAllShareIndexIdentityContract.model_validate(naive)

    relaxed = json.loads(json.dumps(base))
    relaxed["readiness"]["ready_for_backtest"] = True
    with pytest.raises(ValidationError):
        CSIAllShareIndexIdentityContract.model_validate(relaxed)


def test_disk_binding_rejects_missing_wrong_id_and_symlink(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        verify_contract_file(repo_root=tmp_path)

    payload = build_csi_all_share_index_identity_v1().model_dump(mode="json")
    payload["contract_id"] = "ef" * 32
    bad_root = _temp_repo(tmp_path / "bad", payload)
    with pytest.raises(ValueError, match="contract_id"):
        verify_contract_file(repo_root=bad_root)

    symlink_root = tmp_path / "symlink"
    link = symlink_root / DEFAULT_CONTRACT_PATH
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(COMMITTED_PATH)
    with pytest.raises(ValueError, match="symlink"):
        verify_contract_file(repo_root=symlink_root)


def test_verifier_module_has_no_live_client_or_secret_access() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "LiveTushareClient" not in source
    assert "read_tushare_token" not in source
    assert "security find-generic-password" not in source
    assert "AIQ_TUSHARE_TOKEN" not in source


def test_cli_verifier_is_read_only_and_reports_blocker() -> None:
    result = CliRunner().invoke(
        cli_app,
        [
            "verify-csi-all-share-index-identity",
            "--contract-file",
            str(DEFAULT_CONTRACT_PATH),
            "--repo-root",
            str(PROJECT_ROOT),
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"contract_id={EXPECTED_CURRENT_CONTRACT_ID}" in result.output
    assert f"price_ts_code={PRICE_TS_CODE}" in result.output
    assert f"total_return_ts_code={TOTAL_RETURN_TS_CODE}" in result.output
    assert "factual_identity_verified=true" in result.output
    assert "price_series_ready_for_long_history_materialization=true" in result.output
    assert "total_return_series_ready_for_strict_long_history_materialization=true" in result.output
    assert "offline_long_history_materializer_and_hash_bound_raw_collection_not_yet_implemented" in (
        result.output
    )
    assert "ready_for_scoring=false" in result.output
    assert "ready_for_backtest=false" in result.output
    assert "ready_for_trading=false" in result.output
