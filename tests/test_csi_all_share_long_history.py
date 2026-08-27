from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

import app.providers.csi_all_share_long_history as history
from app.errors import DataQualityError, TushareFetchError
from app.providers.csi_all_share_long_history import (
    CSIAllShareCollectionResult,
    collect_csi_all_share_long_history,
    materialize_csi_all_share_long_history,
    verify_csi_all_share_long_history_collection,
    verify_csi_all_share_long_history_snapshot,
)
from app.research.csi_all_share_index_identity import (
    PRICE_TS_CODE,
    TOTAL_RETURN_TS_CODE,
    CSIAllShareIndexIdentityVerificationResult,
    build_csi_all_share_index_identity_v1,
    seal_contract,
)
from tests.tushare_fakes import FakeTushareClient


class FakeCSIClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        return self.payload


def _official_bytes() -> bytes:
    rows = [
        {"indexCode": "H00985", "tradeDate": "2011-07-31", "close": 99.0},
        {"indexCode": "H00985", "tradeDate": "2011-08-01", "close": 100.0},
        {"indexCode": "H00985", "tradeDate": "2011-08-02", "close": 101.0},
        {"indexCode": "H00985", "tradeDate": "2011-08-03", "close": 102.2},
        {"indexCode": "H00985", "tradeDate": "2011-08-04", "close": 103.0},
    ]
    return json.dumps(
        {"code": "200", "data": rows, "msg": "", "success": True},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _synthetic_contract(payload: bytes):
    base = build_csi_all_share_index_identity_v1()
    official_hash = history._sha256_bytes(payload)
    sources = [
        source.model_copy(update={"content_sha256_at_access": official_hash})
        if source.evidence_role == "official_total_return_history_recovery"
        else source
        for source in base.evidence_sources
    ]
    coverage = [
        item.model_copy(
            update={
                "returned_rows": 4,
                "first_trade_date": date(2011, 8, 1),
                "last_trade_date": date(2011, 8, 4),
            }
        )
        if item.tushare_ts_code == PRICE_TS_CODE
        else item.model_copy(
            update={
                "returned_rows": 3,
                "first_trade_date": date(2011, 8, 1),
                "last_trade_date": date(2011, 8, 4),
                "optional_field_null_counts": {"open": 3, "high": 3, "low": 3},
            }
        )
        for item in base.coverage_probes
    ]
    maximum = abs(102.2 / 102.0 - 1.0) * 10000.0
    contract = base.model_copy(
        update={
            "contract_id": None,
            "evidence_sources": sources,
            "coverage_probes": coverage,
            "coverage_cross_check": base.coverage_cross_check.model_copy(update={"common_trade_dates": 3}),
            "official_recovery_probe": base.official_recovery_probe.model_copy(
                update={
                    "requested_start": date(2011, 7, 31),
                    "requested_end": date(2011, 8, 4),
                    "returned_rows": 5,
                    "first_source_date": date(2011, 7, 31),
                    "last_source_date": date(2011, 8, 4),
                    "source_dates_outside_sse_open_calendar": [date(2011, 7, 31)],
                    "common_dates": 3,
                    "common_close_difference_rows": 1,
                    "maximum_common_close_difference_bps": maximum,
                }
            ),
        }
    )
    return seal_contract(contract)


def _tables() -> dict[str, pl.DataFrame]:
    days = ["20110801", "20110802", "20110803", "20110804"]
    price_rows = [
        {
            "ts_code": PRICE_TS_CODE,
            "trade_date": day,
            "open": float(10 + index),
            "high": float(11 + index),
            "low": float(9 + index),
            "close": float(10 + index),
            "pre_close": float(9 + index),
        }
        for index, day in enumerate(days)
    ]
    total_rows = [
        {
            "ts_code": TOTAL_RETURN_TS_CODE,
            "trade_date": "20110801",
            "open": None,
            "high": None,
            "low": None,
            "close": 100.0,
            "pre_close": 99.0,
        },
        {
            "ts_code": TOTAL_RETURN_TS_CODE,
            "trade_date": "20110803",
            "open": None,
            "high": None,
            "low": None,
            "close": 102.0,
            "pre_close": 100.0,
        },
        {
            "ts_code": TOTAL_RETURN_TS_CODE,
            "trade_date": "20110804",
            "open": None,
            "high": None,
            "low": None,
            "close": 103.0,
            "pre_close": 102.0,
        },
    ]
    return {
        "trade_cal": pl.DataFrame({"exchange": ["SSE"] * 4, "cal_date": days, "is_open": ["1"] * 4}),
        "index_daily": pl.DataFrame([*price_rows, *total_rows]),
    }


def _setup(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> object:
    contract = _synthetic_contract(payload)
    result = CSIAllShareIndexIdentityVerificationResult(
        contract_id=str(contract.contract_id),
        disk_binding_ok=True,
        blocker="test_fixture",
    )

    def fake_verify_contract_file(*, repo_root: Path, contract_path: Path):
        del repo_root, contract_path
        return contract, result

    monkeypatch.setattr(history, "verify_contract_file", fake_verify_contract_file)
    monkeypatch.setattr(
        history,
        "_CHUNKS",
        ((date(2011, 8, 1), date(2011, 8, 4)),),
    )
    return contract


def _contract_file(repo_root: Path) -> Path:
    path = repo_root / "config/research/csi-all-share-index-identity-v1.json"
    path.parent.mkdir(parents=True)
    path.write_text("fixture\n", encoding="utf-8")
    return path


def _collect(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CSIAllShareCollectionResult, FakeTushareClient, FakeCSIClient]:
    payload = _official_bytes()
    _setup(monkeypatch, payload)
    _contract_file(repo_root)
    tushare = FakeTushareClient(_tables())
    csi = FakeCSIClient(payload)
    result = collect_csi_all_share_long_history(
        tushare_client=tushare,
        csi_client=csi,
        repo_root=repo_root,
        staging_dir=Path("data/raw/csi"),
    )
    return result, tushare, csi


def test_collect_verify_materialize_and_recompute_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection, tushare, csi = _collect(tmp_path, monkeypatch)
    assert len(tushare.calls) == 3
    assert len(csi.calls) == 1
    assert collection.raw_file_count == 4
    verified = verify_csi_all_share_long_history_collection(
        repo_root=tmp_path,
        staging_dir=collection.staging_dir,
    )
    assert verified.collection_id == collection.collection_id
    materialized = materialize_csi_all_share_long_history(
        repo_root=tmp_path,
        staging_dir=collection.staging_dir,
        output_dir=Path("data/research/csi"),
    )
    assert materialized.calendar_rows == materialized.price_rows == materialized.total_return_rows == 4
    total = pl.read_parquet(materialized.snapshot_dir / "total_return_index.parquet")
    overrides = total.filter(pl.col("source") == "csi_official_index_perf_override")
    assert overrides["date"].to_list() == [date(2011, 8, 2), date(2011, 8, 3)]
    assert overrides["close"].to_list() == [101.0, 102.2]
    assert total.filter(pl.col("date") == date(2011, 8, 4))["close"].item() == 103.0
    verified_snapshot = verify_csi_all_share_long_history_snapshot(
        repo_root=tmp_path,
        staging_dir=collection.staging_dir,
        snapshot_dir=materialized.snapshot_dir,
    )
    assert verified_snapshot.snapshot_id == materialized.snapshot_id


def test_complete_collection_is_reused_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection, _, _ = _collect(tmp_path, monkeypatch)
    tushare = FakeTushareClient({})
    csi = FakeCSIClient(b"must not be read")
    second = collect_csi_all_share_long_history(
        tushare_client=tushare,
        csi_client=csi,
        repo_root=tmp_path,
        staging_dir=collection.staging_dir,
    )
    assert second.collection_id == collection.collection_id
    assert tushare.calls == []
    assert csi.calls == []


def test_raw_parquet_tamper_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    collection, _, _ = _collect(tmp_path, monkeypatch)
    path = next((collection.staging_dir / "raw/tushare/price_index").glob("*.parquet"))
    frame = pl.read_parquet(path).with_columns((pl.col("close") + 1).alias("close"))
    frame.write_parquet(path)
    with pytest.raises(TushareFetchError, match="raw hashes"):
        verify_csi_all_share_long_history_collection(
            repo_root=tmp_path,
            staging_dir=collection.staging_dir,
        )


def test_official_hash_mismatch_is_rejected_before_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _official_bytes()
    _setup(monkeypatch, payload)
    _contract_file(tmp_path)
    with pytest.raises(DataQualityError, match="sealed evidence hash"):
        collect_csi_all_share_long_history(
            tushare_client=FakeTushareClient(_tables()),
            csi_client=FakeCSIClient(payload + b" "),
            repo_root=tmp_path,
            staging_dir=Path("data/raw/csi"),
        )
    assert not (tmp_path / "data/raw/csi/collection_manifest.json").exists()


def test_extra_raw_file_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    collection, _, _ = _collect(tmp_path, monkeypatch)
    extra = collection.staging_dir / "raw/extra.txt"
    extra.write_text("unexpected", encoding="utf-8")
    with pytest.raises(TushareFetchError, match="missing or extra"):
        verify_csi_all_share_long_history_collection(
            repo_root=tmp_path,
            staging_dir=collection.staging_dir,
        )


def test_materializer_refuses_existing_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    collection, _, _ = _collect(tmp_path, monkeypatch)
    output = tmp_path / "data/research/existing"
    output.mkdir(parents=True)
    with pytest.raises(FileExistsError, match="already exists"):
        materialize_csi_all_share_long_history(
            repo_root=tmp_path,
            staging_dir=collection.staging_dir,
            output_dir=output,
        )
    with pytest.raises(ValueError, match="inside repo_root"):
        materialize_csi_all_share_long_history(
            repo_root=tmp_path,
            staging_dir=collection.staging_dir,
            output_dir=Path("../escape"),
        )


def test_snapshot_tamper_fails_full_recomputation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection, _, _ = _collect(tmp_path, monkeypatch)
    materialized = materialize_csi_all_share_long_history(
        repo_root=tmp_path,
        staging_dir=collection.staging_dir,
        output_dir=Path("data/research/csi"),
    )
    path = materialized.snapshot_dir / "total_return_index.parquet"
    pl.read_parquet(path).with_columns((pl.col("close") + 1).alias("close")).write_parquet(path)
    with pytest.raises(TushareFetchError, match="recomputation"):
        verify_csi_all_share_long_history_snapshot(
            repo_root=tmp_path,
            staging_dir=collection.staging_dir,
            snapshot_dir=materialized.snapshot_dir,
        )


def test_staging_symlink_and_outside_repo_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _official_bytes()
    _setup(monkeypatch, payload)
    _contract_file(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "data/raw/csi"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        collect_csi_all_share_long_history(
            tushare_client=FakeTushareClient(_tables()),
            csi_client=FakeCSIClient(payload),
            repo_root=tmp_path,
            staging_dir=link,
        )
    with pytest.raises(ValueError, match="inside repo_root"):
        collect_csi_all_share_long_history(
            tushare_client=FakeTushareClient(_tables()),
            csi_client=FakeCSIClient(payload),
            repo_root=tmp_path,
            staging_dir=outside,
        )


def test_provider_has_no_secret_reader_or_strategy_runtime() -> None:
    source = Path(history.__file__).read_text(encoding="utf-8")
    assert "read_tushare_token" not in source
    assert "AIQ_TUSHARE_TOKEN" not in source
    assert "run_score" not in source
    assert "run_backtest" not in source
    assert "broker" not in source.lower()


def test_keychain_runner_keeps_credential_out_of_arguments() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts/run_csi_all_share_index_collection.sh"
    ).read_text(encoding="utf-8")
    assert 'keychain_service="aiq-tushare-token"' in script
    assert "security find-generic-password" in script
    assert "collect-csi-all-share-long-history" in script
    assert "--token" not in script
    assert "817235" not in script
