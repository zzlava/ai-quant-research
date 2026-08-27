from __future__ import annotations

import hashlib
import json
import math
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Protocol
from urllib.request import Request, urlopen

import polars as pl

from app.errors import DataQualityError, TushareFetchError
from app.providers.tushare_client import TushareQueryClient
from app.providers.tushare_normalize import ymd
from app.research.csi_all_share_index_identity import (
    DEFAULT_CONTRACT_PATH,
    PRICE_TS_CODE,
    TOTAL_RETURN_TS_CODE,
    CSIAllShareIndexIdentityContract,
    verify_contract_file,
)

_SCHEMA_VERSION = "1"
_COLLECTION_SOURCE = "csi_all_share_long_history_raw_v1"
_SNAPSHOT_SOURCE = "csi_all_share_long_history_snapshot_v1"
_OFFICIAL_RESPONSE_NAME = "raw/csi/H00985-20050101-20241231.json"
_CHUNKS: tuple[tuple[date, date], ...] = (
    (date(2005, 1, 1), date(2009, 12, 31)),
    (date(2010, 1, 1), date(2014, 12, 31)),
    (date(2015, 1, 1), date(2019, 12, 31)),
    (date(2020, 1, 1), date(2024, 12, 31)),
)
_RAW_FAMILIES = ("calendar", "price_index", "total_return_index")
_SNAPSHOT_FILES = ("calendar.parquet", "price_index.parquet", "total_return_index.parquet")


class CSIHistoryBytesClient(Protocol):
    def fetch(self, url: str) -> bytes: ...


class LiveCSIHistoryBytesClient:
    """Narrow official-CSI byte fetcher with no credential surface."""

    def fetch(self, url: str) -> bytes:
        if not url.startswith("https://www.csindex.com.cn/csindex-home/perf/index-perf?"):
            raise TushareFetchError("official CSI history URL is outside the sealed endpoint")
        request = Request(url, headers={"User-Agent": "ai-quant-research/0.1"})
        try:
            with urlopen(request, timeout=60) as response:  # noqa: S310
                payload = response.read(16 * 1024 * 1024 + 1)
        except Exception as exc:
            raise TushareFetchError("official CSI history request failed") from exc
        if len(payload) > 16 * 1024 * 1024:
            raise TushareFetchError("official CSI history response exceeds the sealed size limit")
        return payload


@dataclass(frozen=True)
class CSIAllShareCollectionResult:
    staging_dir: Path
    request_id: str
    collection_id: str
    identity_contract_id: str
    raw_file_count: int
    collection_manifest_path: Path
    source_manifest_path: Path
    quality_report_path: Path


@dataclass(frozen=True)
class CSIAllShareMaterializationResult:
    snapshot_dir: Path
    snapshot_id: str
    collection_id: str
    identity_contract_id: str
    calendar_rows: int
    price_rows: int
    total_return_rows: int
    manifest_path: Path


def collect_csi_all_share_long_history(
    *,
    tushare_client: TushareQueryClient,
    csi_client: CSIHistoryBytesClient,
    repo_root: Path,
    staging_dir: Path,
    identity_contract_path: Path = DEFAULT_CONTRACT_PATH,
    progress: Callable[[str, int, int, bool], None] | None = None,
) -> CSIAllShareCollectionResult:
    """Collect and seal raw 2005-2024 index sources without materializing research data."""
    contract, verification = verify_contract_file(
        repo_root=repo_root,
        contract_path=identity_contract_path,
    )
    if not verification.total_return_series_ready_for_strict_long_history_materialization:
        raise TushareFetchError("identity contract has not cleared total-return source recovery")
    root = _resolve_repo_directory(repo_root, staging_dir, create=True, field_name="staging_dir")
    contract_file = _resolve_repo_file(repo_root, identity_contract_path, "identity_contract_path")
    official_source = _official_history_source(contract)
    request_payload = _request_payload(
        contract=contract,
        contract_path=contract_file.relative_to(Path(repo_root).resolve()).as_posix(),
        contract_file_sha256=_sha256_file(contract_file),
        official_url=official_source["url"],
        official_expected_sha256=official_source["sha256"],
    )
    request_id = _json_sha256(request_payload)
    request = {**request_payload, "request_id": request_id}
    request_path = root / "collection_request.json"
    if request_path.exists():
        if _read_json(request_path, "collection_request.json") != request:
            raise TushareFetchError("staging directory belongs to another CSI index request")
    else:
        _write_json_atomic(request_path, request)
    manifest_path = root / "collection_manifest.json"
    if manifest_path.exists():
        return verify_csi_all_share_long_history_collection(
            repo_root=repo_root,
            staging_dir=root,
            identity_contract_path=identity_contract_path,
        )

    work: list[tuple[str, str, date, date]] = []
    for source_family, index_code in (
        ("price_index", PRICE_TS_CODE),
        ("total_return_index", TOTAL_RETURN_TS_CODE),
    ):
        work.extend((source_family, index_code, start, end) for start, end in _CHUNKS)
    work.extend(("calendar", "SSE", start, end) for start, end in _CHUNKS)
    total_steps = len(work) + 1
    for done, (task_family, task_code, start, end) in enumerate(work, start=1):
        path = root / _raw_partition_name(task_family, start, end)
        if path.exists():
            _require_regular_child(root, path, path.relative_to(root).as_posix())
            frame = pl.read_parquet(path)
            _validate_raw_partition(
                frame,
                family=task_family,
                code=task_code,
                start=start,
                end=end,
            )
            reused = True
        else:
            if task_family == "calendar":
                frame = tushare_client.query(
                    "trade_cal",
                    exchange="SSE",
                    start_date=ymd(start),
                    end_date=ymd(end),
                    is_open="1",
                    fields="exchange,cal_date,is_open",
                )
            else:
                frame = tushare_client.query(
                    "index_daily",
                    ts_code=task_code,
                    start_date=ymd(start),
                    end_date=ymd(end),
                )
            _validate_raw_partition(
                frame,
                family=task_family,
                code=task_code,
                start=start,
                end=end,
            )
            _write_parquet_atomic(path, frame)
            reused = False
        if progress is not None:
            progress(task_family, done, total_steps, reused)

    official_path = root / _OFFICIAL_RESPONSE_NAME
    if official_path.exists():
        _require_regular_child(root, official_path, _OFFICIAL_RESPONSE_NAME)
        official_bytes = official_path.read_bytes()
        reused = True
    else:
        official_bytes = csi_client.fetch(official_source["url"])
        if _sha256_bytes(official_bytes) != official_source["sha256"]:
            raise DataQualityError("official CSI history bytes do not match the sealed evidence hash")
        _parse_official_history(official_bytes)
        _write_bytes_atomic(official_path, official_bytes)
        reused = False
    if _sha256_bytes(official_bytes) != official_source["sha256"]:
        raise DataQualityError("official CSI history bytes do not match the sealed evidence hash")
    if progress is not None:
        progress("official_total_return", total_steps, total_steps, reused)

    quality = _build_quality_report(root, contract)
    quality = {**quality, "quality_report_id": _json_sha256(quality)}
    quality_path = root / "quality_report.json"
    _write_json_atomic(quality_path, quality)
    source = {
        "schema_version": _SCHEMA_VERSION,
        "source_name": _COLLECTION_SOURCE,
        "request_id": request_id,
        "identity_contract_id": contract.contract_id,
        "collected_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "sources": [
            {
                "name": "Tushare index_daily",
                "codes": [PRICE_TS_CODE, TOTAL_RETURN_TS_CODE],
                "availability_policy": "trade-date close; eligible only after that close",
            },
            {
                "name": "Tushare trade_cal",
                "exchange": "SSE",
                "availability_policy": "calendar reference only; never a price source",
            },
            {
                "name": "CSI official index-perf",
                "url": official_source["url"],
                "content_sha256": official_source["sha256"],
                "allowed_override_dates": ["2011-08-02", "2011-08-03"],
            },
        ],
        "no_interpolation_or_forward_fill": True,
        "ready_for_scoring": False,
        "ready_for_backtest": False,
        "ready_for_trading": False,
    }
    source = {**source, "source_manifest_id": _json_sha256(source)}
    source_path = root / "source_manifest.json"
    _write_json_atomic(source_path, source)
    raw_hashes = _raw_file_hashes(root)
    manifest_payload = {
        "schema_version": _SCHEMA_VERSION,
        "source_name": _COLLECTION_SOURCE,
        "request_id": request_id,
        "identity_contract_id": contract.contract_id,
        "identity_contract_file_sha256": _sha256_file(contract_file),
        "raw_file_hashes": raw_hashes,
        "source_manifest_sha256": _sha256_file(source_path),
        "quality_report_sha256": _sha256_file(quality_path),
        "raw_file_count": len(raw_hashes),
        "repair_rule": contract.official_recovery_probe.repair_rule,
        "ready_for_scoring": False,
        "ready_for_backtest": False,
        "ready_for_trading": False,
    }
    manifest = {**manifest_payload, "collection_id": _json_sha256(manifest_payload)}
    _write_json_atomic(manifest_path, manifest)
    return verify_csi_all_share_long_history_collection(
        repo_root=repo_root,
        staging_dir=root,
        identity_contract_path=identity_contract_path,
    )


def verify_csi_all_share_long_history_collection(
    *,
    repo_root: Path,
    staging_dir: Path,
    identity_contract_path: Path = DEFAULT_CONTRACT_PATH,
) -> CSIAllShareCollectionResult:
    contract, verification = verify_contract_file(
        repo_root=repo_root,
        contract_path=identity_contract_path,
    )
    if not verification.total_return_series_ready_for_strict_long_history_materialization:
        raise TushareFetchError("identity contract has not cleared total-return source recovery")
    root = _resolve_repo_directory(repo_root, staging_dir, create=False, field_name="staging_dir")
    contract_file = _resolve_repo_file(repo_root, identity_contract_path, "identity_contract_path")
    request = _read_json(root / "collection_request.json", "collection_request.json")
    manifest = _read_json(root / "collection_manifest.json", "collection_manifest.json")
    quality = _read_json(root / "quality_report.json", "quality_report.json")
    source = _read_json(root / "source_manifest.json", "source_manifest.json")
    official_source = _official_history_source(contract)
    expected_request_payload = _request_payload(
        contract=contract,
        contract_path=contract_file.relative_to(Path(repo_root).resolve()).as_posix(),
        contract_file_sha256=_sha256_file(contract_file),
        official_url=official_source["url"],
        official_expected_sha256=official_source["sha256"],
    )
    expected_request = {**expected_request_payload, "request_id": _json_sha256(expected_request_payload)}
    if request != expected_request:
        raise TushareFetchError("CSI index collection request does not match the sealed identity contract")
    raw_hashes = _raw_file_hashes(root)
    if manifest.get("raw_file_hashes") != raw_hashes:
        raise TushareFetchError("CSI index collection raw hashes do not match staged bytes")
    if manifest.get("raw_file_count") != len(raw_hashes):
        raise TushareFetchError("CSI index collection raw file count does not match")
    if manifest.get("source_manifest_sha256") != _sha256_file(root / "source_manifest.json"):
        raise TushareFetchError("CSI index source manifest hash does not match")
    if manifest.get("quality_report_sha256") != _sha256_file(root / "quality_report.json"):
        raise TushareFetchError("CSI index quality report hash does not match")
    manifest_payload = {key: value for key, value in manifest.items() if key != "collection_id"}
    if manifest.get("collection_id") != _json_sha256(manifest_payload):
        raise TushareFetchError("CSI index collection_id does not match canonical manifest content")
    if manifest.get("request_id") != request["request_id"]:
        raise TushareFetchError("CSI index collection request ID does not match")
    if manifest.get("identity_contract_id") != contract.contract_id:
        raise TushareFetchError("CSI index collection identity contract ID does not match")
    if manifest.get("identity_contract_file_sha256") != _sha256_file(contract_file):
        raise TushareFetchError("CSI index identity contract file hash does not match")
    for flag in ("ready_for_scoring", "ready_for_backtest", "ready_for_trading"):
        if manifest.get(flag) is not False or source.get(flag) is not False:
            raise TushareFetchError("CSI index collection violates research-only readiness gates")
    source_payload = {key: value for key, value in source.items() if key != "source_manifest_id"}
    if source.get("source_manifest_id") != _json_sha256(source_payload):
        raise TushareFetchError("CSI index source manifest ID does not match")
    recomputed_quality = _build_quality_report(root, contract)
    expected_quality = {**recomputed_quality, "quality_report_id": _json_sha256(recomputed_quality)}
    if quality != expected_quality:
        raise TushareFetchError("CSI index quality report does not match raw source content")
    return CSIAllShareCollectionResult(
        staging_dir=root,
        request_id=str(request["request_id"]),
        collection_id=str(manifest["collection_id"]),
        identity_contract_id=str(contract.contract_id),
        raw_file_count=len(raw_hashes),
        collection_manifest_path=root / "collection_manifest.json",
        source_manifest_path=root / "source_manifest.json",
        quality_report_path=root / "quality_report.json",
    )


def materialize_csi_all_share_long_history(
    *,
    repo_root: Path,
    staging_dir: Path,
    output_dir: Path,
    identity_contract_path: Path = DEFAULT_CONTRACT_PATH,
) -> CSIAllShareMaterializationResult:
    collection = verify_csi_all_share_long_history_collection(
        repo_root=repo_root,
        staging_dir=staging_dir,
        identity_contract_path=identity_contract_path,
    )
    contract, _ = verify_contract_file(repo_root=repo_root, contract_path=identity_contract_path)
    calendar, price, total = _build_materialized_tables(collection.staging_dir, contract)
    destination = _resolve_repo_output_path(repo_root, output_dir, field_name="output_dir")
    if destination.exists():
        raise FileExistsError("CSI index snapshot output already exists; use a new output directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".csi-index-snapshot-{uuid.uuid4().hex}"
    try:
        temporary.mkdir(parents=True)
        tables = {
            "calendar.parquet": calendar,
            "price_index.parquet": price,
            "total_return_index.parquet": total,
        }
        for name, frame in tables.items():
            frame.write_parquet(temporary / name)
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        manifest = _snapshot_manifest(
            root=temporary,
            collection=collection,
            contract=contract,
            created_at=created_at,
        )
        _write_json_atomic(temporary / "manifest.json", manifest)
        temporary.rename(destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    return verify_csi_all_share_long_history_snapshot(
        repo_root=repo_root,
        staging_dir=staging_dir,
        snapshot_dir=destination,
        identity_contract_path=identity_contract_path,
    )


def verify_csi_all_share_long_history_snapshot(
    *,
    repo_root: Path,
    staging_dir: Path,
    snapshot_dir: Path,
    identity_contract_path: Path = DEFAULT_CONTRACT_PATH,
) -> CSIAllShareMaterializationResult:
    collection = verify_csi_all_share_long_history_collection(
        repo_root=repo_root,
        staging_dir=staging_dir,
        identity_contract_path=identity_contract_path,
    )
    contract, _ = verify_contract_file(repo_root=repo_root, contract_path=identity_contract_path)
    root = _resolve_repo_directory(repo_root, snapshot_dir, create=False, field_name="snapshot_dir")
    names = {path.name for path in root.iterdir()}
    if names != {*_SNAPSHOT_FILES, "manifest.json"}:
        raise TushareFetchError("CSI index snapshot contains missing or extra files")
    for name in (*_SNAPSHOT_FILES, "manifest.json"):
        _require_regular_child(root, root / name, name)
    manifest = _read_json(root / "manifest.json", "manifest.json")
    expected_calendar, expected_price, expected_total = _build_materialized_tables(
        collection.staging_dir,
        contract,
    )
    actual = {
        "calendar.parquet": pl.read_parquet(root / "calendar.parquet"),
        "price_index.parquet": pl.read_parquet(root / "price_index.parquet"),
        "total_return_index.parquet": pl.read_parquet(root / "total_return_index.parquet"),
    }
    expected = {
        "calendar.parquet": expected_calendar,
        "price_index.parquet": expected_price,
        "total_return_index.parquet": expected_total,
    }
    for name in _SNAPSHOT_FILES:
        if not actual[name].equals(expected[name], null_equal=True):
            raise TushareFetchError(f"CSI index snapshot {name} does not match raw-source recomputation")
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise TushareFetchError("CSI index snapshot created_at is invalid")
    expected_manifest = _snapshot_manifest(
        root=root,
        collection=collection,
        contract=contract,
        created_at=created_at,
    )
    if manifest != expected_manifest:
        raise TushareFetchError("CSI index snapshot manifest does not match files and source bindings")
    return CSIAllShareMaterializationResult(
        snapshot_dir=root,
        snapshot_id=str(manifest["snapshot_id"]),
        collection_id=collection.collection_id,
        identity_contract_id=collection.identity_contract_id,
        calendar_rows=actual["calendar.parquet"].height,
        price_rows=actual["price_index.parquet"].height,
        total_return_rows=actual["total_return_index.parquet"].height,
        manifest_path=root / "manifest.json",
    )


def _request_payload(
    *,
    contract: CSIAllShareIndexIdentityContract,
    contract_path: str,
    contract_file_sha256: str,
    official_url: str,
    official_expected_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "source_name": _COLLECTION_SOURCE,
        "identity_contract_id": contract.contract_id,
        "identity_contract_path": contract_path,
        "identity_contract_file_sha256": contract_file_sha256,
        "coverage": {"start": "2005-01-01", "end": "2024-12-31"},
        "price_ts_code": PRICE_TS_CODE,
        "total_return_ts_code": TOTAL_RETURN_TS_CODE,
        "calendar_exchange": "SSE",
        "chunks": [{"start": start.isoformat(), "end": end.isoformat()} for start, end in _CHUNKS],
        "official_url": official_url,
        "official_expected_sha256": official_expected_sha256,
        "repair_rule": contract.official_recovery_probe.repair_rule,
        "no_interpolation_or_forward_fill": True,
    }


def _official_history_source(contract: CSIAllShareIndexIdentityContract) -> dict[str, str]:
    matches = [
        source
        for source in contract.evidence_sources
        if source.evidence_role == "official_total_return_history_recovery"
    ]
    if len(matches) != 1:
        raise TushareFetchError("identity contract must bind exactly one official recovery source")
    return {"url": matches[0].url, "sha256": matches[0].content_sha256_at_access}


def _raw_partition_name(family: str, start: date, end: date) -> str:
    return f"raw/tushare/{family}/{ymd(start)}-{ymd(end)}.parquet"


def _validate_raw_partition(
    frame: pl.DataFrame,
    *,
    family: str,
    code: str,
    start: date,
    end: date,
) -> None:
    if frame.is_empty():
        raise DataQualityError(f"{family} returned an empty partition for {start}..{end}")
    if family == "calendar":
        required = {"exchange", "cal_date", "is_open"}
        date_column = "cal_date"
        missing = sorted(required - set(frame.columns))
        if missing:
            raise DataQualityError(f"{family} partition missing columns: {missing}")
        if set(frame["exchange"].cast(pl.String).to_list()) != {"SSE"}:
            raise DataQualityError("calendar partition contains a non-SSE exchange")
        if set(frame["is_open"].cast(pl.String).to_list()) != {"1"}:
            raise DataQualityError("calendar partition contains a closed date")
    else:
        required = {"ts_code", "trade_date", "close"}
        date_column = "trade_date"
        missing = sorted(required - set(frame.columns))
        if missing:
            raise DataQualityError(f"{family} partition missing columns: {missing}")
        if set(frame["ts_code"].cast(pl.String).to_list()) != {code}:
            raise DataQualityError(f"{family} partition contains another index code")
        values = frame["close"].cast(pl.Float64, strict=True).to_list()
        if any(value is None or not math.isfinite(value) or value <= 0 for value in values):
            raise DataQualityError(f"{family} partition contains invalid close values")
    parsed = [_parse_source_date(value, field=date_column) for value in frame[date_column].to_list()]
    if any(value < start or value > end for value in parsed):
        raise DataQualityError(f"{family} partition contains a date outside its request chunk")
    if len(parsed) != len(set(parsed)):
        raise DataQualityError(f"{family} partition contains duplicate dates")


def _build_quality_report(root: Path, contract: CSIAllShareIndexIdentityContract) -> dict[str, Any]:
    calendar_rows = _raw_rows_by_date(root, "calendar", code="SSE")
    price_rows = _raw_rows_by_date(root, "price_index", code=PRICE_TS_CODE)
    total_rows = _raw_rows_by_date(root, "total_return_index", code=TOTAL_RETURN_TS_CODE)
    calendar_dates = set(calendar_rows)
    price_dates = set(price_rows)
    total_dates = set(total_rows)
    expected_price = next(item for item in contract.coverage_probes if item.tushare_ts_code == PRICE_TS_CODE)
    expected_total = next(item for item in contract.coverage_probes if item.tushare_ts_code == TOTAL_RETURN_TS_CODE)
    if len(calendar_dates) != expected_price.returned_rows or price_dates != calendar_dates:
        raise DataQualityError("price index dates do not exactly match the sealed SSE open calendar")
    expected_missing = set(contract.coverage_cross_check.price_only_trade_dates)
    if calendar_dates - total_dates != expected_missing or total_dates - calendar_dates:
        raise DataQualityError("Tushare total-return dates do not match the sealed one-day source gap")
    if len(total_dates) != expected_total.returned_rows:
        raise DataQualityError("Tushare total-return row count does not match the sealed probe")
    official_path = root / _OFFICIAL_RESPONSE_NAME
    official_bytes = _read_regular_bytes(root, official_path, _OFFICIAL_RESPONSE_NAME)
    official_source = _official_history_source(contract)
    if _sha256_bytes(official_bytes) != official_source["sha256"]:
        raise DataQualityError("official CSI response hash does not match the identity contract")
    official_rows = _parse_official_history(official_bytes)
    official_dates = set(official_rows)
    recovery = contract.official_recovery_probe
    if len(official_dates) != recovery.returned_rows:
        raise DataQualityError("official CSI response row count does not match the sealed probe")
    outside = sorted(official_dates - calendar_dates)
    if outside != recovery.source_dates_outside_sse_open_calendar:
        raise DataQualityError("official CSI response outside-calendar dates changed")
    valid_official_only = sorted((official_dates & calendar_dates) - total_dates)
    if valid_official_only != recovery.official_only_valid_trading_dates:
        raise DataQualityError("official CSI valid recovery dates changed")
    common = sorted(official_dates & total_dates)
    differences: list[float] = []
    for day in common:
        official_close = float(official_rows[day]["close"])
        tushare_close = float(total_rows[day]["close"])
        if official_close != tushare_close:
            differences.append(abs(official_close / tushare_close - 1.0) * 10000.0)
    maximum = max(differences, default=0.0)
    if len(common) != recovery.common_dates:
        raise DataQualityError("official/Tushare common-date count changed")
    if len(differences) != recovery.common_close_difference_rows:
        raise DataQualityError("official/Tushare close-difference count changed")
    if not math.isclose(maximum, recovery.maximum_common_close_difference_bps, rel_tol=0, abs_tol=1e-10):
        raise DataQualityError("official/Tushare maximum close difference changed")
    for day in recovery.fixed_official_override_dates:
        if day not in official_rows or day not in calendar_dates:
            raise DataQualityError("fixed official override date is unavailable")
    return {
        "schema_version": _SCHEMA_VERSION,
        "complete": True,
        "calendar_rows": len(calendar_dates),
        "price_rows": len(price_dates),
        "tushare_total_return_rows": len(total_dates),
        "official_total_return_rows": len(official_dates),
        "calendar_start": min(calendar_dates).isoformat(),
        "calendar_end": max(calendar_dates).isoformat(),
        "tushare_total_return_missing_dates": [item.isoformat() for item in sorted(calendar_dates - total_dates)],
        "official_outside_calendar_dates": [item.isoformat() for item in outside],
        "official_override_dates": [item.isoformat() for item in recovery.fixed_official_override_dates],
        "common_close_difference_rows": len(differences),
        "maximum_common_close_difference_bps": maximum,
        "no_interpolation_or_forward_fill": True,
        "ready_for_scoring": False,
        "ready_for_backtest": False,
        "ready_for_trading": False,
    }


def _build_materialized_tables(
    root: Path,
    contract: CSIAllShareIndexIdentityContract,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    _build_quality_report(root, contract)
    calendar_rows = _raw_rows_by_date(root, "calendar", code="SSE")
    price_rows = _raw_rows_by_date(root, "price_index", code=PRICE_TS_CODE)
    total_rows = _raw_rows_by_date(root, "total_return_index", code=TOTAL_RETURN_TS_CODE)
    official_path = root / _OFFICIAL_RESPONSE_NAME
    official_bytes = _read_regular_bytes(root, official_path, _OFFICIAL_RESPONSE_NAME)
    official_rows = _parse_official_history(official_bytes)
    official_hash = _sha256_bytes(official_bytes)
    days = sorted(calendar_rows)
    calendar = pl.DataFrame({"date": days}, schema={"date": pl.Date})
    price_output: list[dict[str, Any]] = []
    total_output: list[dict[str, Any]] = []
    override_dates = set(contract.official_recovery_probe.fixed_official_override_dates)
    for day in days:
        price_row = price_rows[day]
        price_output.append(
            {
                "symbol": PRICE_TS_CODE,
                "date": day,
                "open": _positive_float(price_row, "open"),
                "high": _positive_float(price_row, "high"),
                "low": _positive_float(price_row, "low"),
                "close": _positive_float(price_row, "close"),
                "pre_close": _positive_float(price_row, "pre_close"),
                "available_at": datetime.combine(day, time(7, 0), tzinfo=UTC),
                "source": "tushare_index_daily",
                "source_row_hash": _canonical_row_hash(price_row),
                "source_file": str(price_row["_source_file"]),
                "source_file_sha256": str(price_row["_source_file_sha256"]),
            }
        )
        if day in override_dates:
            source_row = official_rows[day]
            close = _positive_float(source_row, "close")
            source = "csi_official_index_perf_override"
            source_file = _OFFICIAL_RESPONSE_NAME
            source_file_hash = official_hash
        else:
            source_row = total_rows[day]
            close = _positive_float(source_row, "close")
            source = "tushare_index_daily"
            source_file = str(source_row["_source_file"])
            source_file_hash = str(source_row["_source_file_sha256"])
        total_output.append(
            {
                "symbol": TOTAL_RETURN_TS_CODE,
                "date": day,
                "close": close,
                "available_at": datetime.combine(day, time(7, 0), tzinfo=UTC),
                "source": source,
                "source_row_hash": _canonical_row_hash(source_row),
                "source_file": source_file,
                "source_file_sha256": source_file_hash,
            }
        )
    price = pl.DataFrame(price_output).sort("date")
    total = pl.DataFrame(total_output).sort("date")
    if price.height != len(days) or total.height != len(days):
        raise DataQualityError("materialized index rows do not cover the exact calendar")
    actual_override = set(total.filter(pl.col("source") == "csi_official_index_perf_override")["date"].to_list())
    if actual_override != override_dates:
        raise DataQualityError("materialized official override dates do not match the sealed rule")
    return calendar, price, total


def _snapshot_manifest(
    *,
    root: Path,
    collection: CSIAllShareCollectionResult,
    contract: CSIAllShareIndexIdentityContract,
    created_at: str,
) -> dict[str, Any]:
    table_hashes = {name: _sha256_file(root / name) for name in _SNAPSHOT_FILES}
    calendar = pl.read_parquet(root / "calendar.parquet")
    calendar_dates = calendar["date"].to_list()
    if not calendar_dates or any(not isinstance(value, date) for value in calendar_dates):
        raise DataQualityError("CSI index snapshot calendar dates are invalid")
    first_date = min(value for value in calendar_dates if isinstance(value, date))
    last_date = max(value for value in calendar_dates if isinstance(value, date))
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "source_name": _SNAPSHOT_SOURCE,
        "created_at": created_at,
        "identity_contract_id": contract.contract_id,
        "raw_collection_id": collection.collection_id,
        "raw_collection_manifest_sha256": _sha256_file(collection.collection_manifest_path),
        "coverage": {
            "start": first_date.isoformat(),
            "end": last_date.isoformat(),
        },
        "table_hashes": table_hashes,
        "calendar_rows": calendar.height,
        "price_rows": pl.read_parquet(root / "price_index.parquet").height,
        "total_return_rows": pl.read_parquet(root / "total_return_index.parquet").height,
        "official_override_dates": [
            item.isoformat() for item in contract.official_recovery_probe.fixed_official_override_dates
        ],
        "availability_policy": "trade-date 15:00 Asia/Shanghai encoded as 07:00Z; T+1 action only",
        "no_interpolation_or_forward_fill": True,
        "ready_for_scoring": False,
        "ready_for_backtest": False,
        "ready_for_trading": False,
        "auto_apply": False,
    }
    return {**payload, "snapshot_id": _json_sha256(payload)}


def _raw_rows_by_date(root: Path, family: str, *, code: str) -> dict[date, dict[str, Any]]:
    if family not in _RAW_FAMILIES:
        raise ValueError(f"unsupported raw family: {family}")
    out: dict[date, dict[str, Any]] = {}
    for start, end in _CHUNKS:
        path = root / _raw_partition_name(family, start, end)
        relative = path.relative_to(root).as_posix()
        _require_regular_child(root, path, relative)
        frame = pl.read_parquet(path)
        _validate_raw_partition(frame, family=family, code=code, start=start, end=end)
        file_hash = _sha256_file(path)
        date_field = "cal_date" if family == "calendar" else "trade_date"
        for raw in frame.to_dicts():
            day = _parse_source_date(raw[date_field], field=date_field)
            if day in out:
                raise DataQualityError(f"{family} has a duplicate date across chunks: {day}")
            out[day] = {**raw, "_source_file": relative, "_source_file_sha256": file_hash}
    return out


def _parse_official_history(payload: bytes) -> dict[date, dict[str, Any]]:
    try:
        body = json.loads(payload)
    except Exception as exc:
        raise DataQualityError("official CSI history response is not valid JSON") from exc
    if not isinstance(body, dict) or body.get("success") is not True or not isinstance(body.get("data"), list):
        raise DataQualityError("official CSI history response envelope is invalid")
    out: dict[date, dict[str, Any]] = {}
    for raw in body["data"]:
        if not isinstance(raw, dict):
            raise DataQualityError("official CSI history row is invalid")
        if str(raw.get("indexCode")) != "H00985":
            raise DataQualityError("official CSI history contains another index code")
        day = _parse_source_date(raw.get("tradeDate"), field="tradeDate")
        _positive_float(raw, "close")
        if day in out:
            raise DataQualityError(f"official CSI history contains duplicate date {day}")
        out[day] = raw
    if not out:
        raise DataQualityError("official CSI history contains no rows")
    return out


def _parse_source_date(value: object, *, field: str) -> date:
    text = str(value or "").strip()
    try:
        if len(text) >= 10 and text[4] == "-":
            return date.fromisoformat(text[:10])
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise DataQualityError(f"invalid {field} date") from exc
    raise DataQualityError(f"invalid {field} date")


def _positive_float(row: dict[str, Any], field: str) -> float:
    try:
        value = float(row[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise DataQualityError(f"source row has invalid {field}") from exc
    if not math.isfinite(value) or value <= 0:
        raise DataQualityError(f"source row has invalid {field}")
    return value


def _canonical_row_hash(row: dict[str, Any]) -> str:
    clean = {key: value for key, value in row.items() if not key.startswith("_source_")}
    canonical: dict[str, Any] = {}
    for key, value in clean.items():
        if isinstance(value, (date, datetime)):
            canonical[key] = value.isoformat()
        elif value is None or isinstance(value, (str, int, float, bool)):
            canonical[key] = value
        else:
            canonical[key] = str(value)
    return _json_sha256(canonical)


def _raw_file_hashes(root: Path) -> dict[str, str]:
    expected = {_OFFICIAL_RESPONSE_NAME}
    for family in _RAW_FAMILIES:
        expected.update(_raw_partition_name(family, start, end) for start, end in _CHUNKS)
    actual: set[str] = set()
    raw_root = root / "raw"
    if raw_root.is_symlink():
        raise TushareFetchError("CSI index raw collection contains a symlink")
    if raw_root.exists():
        for path in raw_root.rglob("*"):
            if path.is_symlink():
                raise TushareFetchError("CSI index raw collection contains a symlink")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
    if actual != expected:
        raise TushareFetchError("CSI index raw collection contains missing or extra files")
    hashes: dict[str, str] = {}
    for name in sorted(expected):
        path = root / name
        _require_regular_child(root, path, name)
        hashes[name] = _sha256_file(path)
    return hashes


def _resolve_repo_directory(repo_root: Path, path: Path, *, create: bool, field_name: str) -> Path:
    root = Path(repo_root).resolve(strict=True)
    candidate = Path(path) if Path(path).is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be inside repo_root") from exc
    if any(part in {".", ".."} for part in relative.parts):
        raise ValueError(f"{field_name} must be inside repo_root")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise ValueError(f"{field_name} must not contain symlink components")
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    if not candidate.is_dir():
        raise FileNotFoundError(f"{field_name} directory not found")
    resolved = candidate.resolve(strict=True)
    resolved.relative_to(root)
    return resolved


def _resolve_repo_output_path(repo_root: Path, path: Path, *, field_name: str) -> Path:
    root = Path(repo_root).resolve(strict=True)
    candidate = Path(path) if Path(path).is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be inside repo_root") from exc
    if any(part in {".", ".."} for part in relative.parts):
        raise ValueError(f"{field_name} must be inside repo_root")
    cursor = root
    for part in relative.parts[:-1]:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise ValueError(f"{field_name} must not contain symlink components")
    if candidate.exists() and candidate.is_symlink():
        raise ValueError(f"{field_name} must not be a symlink")
    parent = candidate.parent.resolve(strict=False)
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be inside repo_root") from exc
    return candidate


def _resolve_repo_file(repo_root: Path, path: Path, field_name: str) -> Path:
    root = Path(repo_root).resolve(strict=True)
    candidate = Path(path) if Path(path).is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be inside repo_root") from exc
    if any(part in {".", ".."} for part in relative.parts):
        raise ValueError(f"{field_name} must be inside repo_root")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise ValueError(f"{field_name} must not contain symlink components")
    if not candidate.is_file() or candidate.is_symlink():
        raise FileNotFoundError(f"{field_name} file not found")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be inside repo_root") from exc
    return resolved


def _require_regular_child(root: Path, path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise TushareFetchError(f"missing or unsafe {label}")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise TushareFetchError(f"unsafe {label}") from exc


def _read_regular_bytes(root: Path, path: Path, label: str) -> bytes:
    _require_regular_child(root, path, label)
    return path.read_bytes()


def _write_parquet_atomic(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.write_parquet(temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise TushareFetchError(f"missing or unsafe {label}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TushareFetchError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise TushareFetchError(f"invalid {label}")
    return value


def _json_sha256(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CSIAllShareCollectionResult",
    "CSIAllShareMaterializationResult",
    "CSIHistoryBytesClient",
    "LiveCSIHistoryBytesClient",
    "collect_csi_all_share_long_history",
    "materialize_csi_all_share_long_history",
    "verify_csi_all_share_long_history_collection",
    "verify_csi_all_share_long_history_snapshot",
]
