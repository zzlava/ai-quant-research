from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import polars as pl

from app.errors import DataQualityError, TushareFetchError
from app.providers.tushare_client import TushareQueryClient
from app.providers.tushare_normalize import require_ts_code, ymd
from app.research.layer_two_financial_negative_list_collection_run_contract import (
    DEFAULT_RUN_CONTRACT_PATH,
    RUN_CONTRACT_VERSION_V3,
    verify_run_contract_file,
)
from app.research.layer_two_financial_negative_list_data_protocol import (
    ANNOUNCEMENT_COLLECTION_END,
    ANNOUNCEMENT_COLLECTION_START,
    PROTOCOL_FILE_PATH,
    SOURCE_ENDPOINTS,
    verify_protocol_file,
)
from app.research.layer_two_financial_negative_list_finalization_authorization import (
    FINALIZATION_AUTHORIZATION_PATH,
    FINALIZATION_ORIGINAL_REQUEST_ID,
    FinalizationAuthorizationVerificationResult,
    verify_finalization_authorization_file,
)
from app.research.layer_two_financial_negative_list_response_boundary_policy import (
    POLICY_FILE_PATH as RESPONSE_BOUNDARY_POLICY_FILE_PATH,
)
from app.research.layer_two_financial_negative_list_response_boundary_policy import (
    POLICY_REASON_CODE as RESPONSE_BOUNDARY_REASON_CODE,
)
from app.research.layer_two_financial_negative_list_response_boundary_policy import (
    evaluate_response_boundary,
    verify_policy_file,
)
from app.research.layer_two_financial_negative_list_stock_basic import (
    load_canonical_symbol_listing_dates,
)

_SCHEMA_VERSION = "1"
_REQUEST_INTERVAL_SECONDS = 0.31
_DATE_RE = re.compile(r"^\d{8}$")
_INT_FIELDS = frozenset({"report_type", "comp_type", "end_type"})
_ID_TEXT_FIELDS = frozenset(
    {
        "ts_code",
        "ann_date",
        "f_ann_date",
        "end_date",
        "update_flag",
        "audit_result",
        "audit_agency",
        "audit_sign",
    }
)
_PARTITION_EXTRA_SCHEMA: dict[str, Any] = {
    "effective_disclosure_date": pl.String,
    "available_at": pl.String,
    "availability_status": pl.String,
    "source_row_hash": pl.String,
}
_ROW_LIMITS = {
    "balancesheet": 600,
    "income": 600,
    "fina_indicator": 600,
    "fina_audit": 600,
}
_SHANGHAI_TZ = timezone(timedelta(hours=8))
_AVAILABLE_AT_TIME = time(23, 59, 59)
_AVAILABILITY_POLICY = (
    "effective_disclosure_date=max(valid ann_date,valid f_ann_date when present); "
    "available_at=23:59:59 Asia/Shanghai on effective date; "
    "missing ann_date keeps row with availability_status=missing_ann_date and available_at=null; "
    "collected_at is provenance only and never used as availability"
)
_RAW_PRESERVATION_SEMANTICS = (
    "preserve every retrieved source row version; no silent dedupe; "
    "source_row_hash=sha256(JSON canonical field->normalized value map over all persisted source fields "
    "after strict type/date normalization); no raw field is dropped; "
    "exact duplicates and same semantic key+availability conflicts fail closed"
)
_UTC_TEXT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "collection_authorization_id",
        "protocol_id",
        "protocol_file_path",
        "protocol_file_sha256",
        "candidate_pack_id",
        "candidate_pack_parquet_sha256",
        "raw_collection_request_id",
        "raw_collection_manifest_sha256",
        "raw_quality_report_sha256",
        "verified_run_contract_id",
        "verified_run_contract_version",
        "verified_response_boundary_policy_id",
        "verified_response_boundary_policy_file_sha256",
        "verified_response_boundary_reason_code",
        "announcement_window_start",
        "announcement_window_end",
        "requested_symbols",
        "collected_at",
        "availability_policy",
        "raw_preservation_semantics",
        "response_boundary_policy",
        "response_boundary_receipts",
        "endpoints",
    }
)
_FINALIZATION_MANIFEST_KEY = "response_boundary_finalization"
_QUALITY_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "collection_authorization_id",
        "protocol_id",
        "announcement_window",
        "requested_symbols",
        "partition_count",
        "verified_run_contract_id",
        "verified_run_contract_version",
        "verified_response_boundary_policy_id",
        "verified_response_boundary_policy_file_sha256",
        "verified_response_boundary_reason_code",
        "sources",
        "response_boundary",
        "post_window_listed_symbols",
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_trading",
    }
)
_COLLECTION_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "collection_id",
        "request_id",
        "collection_authorization_id",
        "protocol_id",
        "protocol_file_path",
        "protocol_file_sha256",
        "candidate_pack_id",
        "candidate_pack_parquet_sha256",
        "raw_collection_request_id",
        "raw_collection_manifest_sha256",
        "raw_quality_report_sha256",
        "announcement_window_start",
        "announcement_window_end",
        "requested_symbols",
        "partition_count",
        "symbols_sha256",
        "dataset_hashes",
        "request_sha256",
        "source_manifest_sha256",
        "quality_report_sha256",
        "verified_run_contract_id",
        "verified_run_contract_version",
        "verified_response_boundary_policy_id",
        "verified_response_boundary_policy_file_sha256",
        "verified_response_boundary_reason_code",
        "response_boundary_receipt_hashes",
        "collected_at",
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_trading",
    }
)
_RESPONSE_BOUNDARY_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "endpoint",
        "symbol",
        "ann_date",
        "f_ann_date",
        "end_date",
        "report_type",
        "comp_type",
        "end_type",
        "update_flag",
        "effective_disclosure_date",
        "reason_code",
        "source_row_hash",
    }
)


@dataclass(frozen=True)
class FinancialNegativeListCollectionResult:
    staging_dir: Path
    request_id: str
    collection_authorization_id: str
    protocol_id: str
    requested_symbols: int
    partition_count: int
    completed_partitions: int
    reused_partitions: int
    source_manifest_path: Path
    quality_report_path: Path
    collection_manifest_path: Path


@dataclass(frozen=True)
class _CanonicalPartitionBuild:
    frame: pl.DataFrame
    response_boundary_receipts: list[dict[str, Any]]


@dataclass(frozen=True)
class _VerifiedBindings:
    run_contract_id: str
    run_contract_version: str
    response_boundary_policy_id: str
    response_boundary_policy_file_sha256: str
    response_boundary_reason_code: str


def _optional_finalization_bindings(
    *,
    repo_root: Path,
    request: dict[str, Any],
) -> FinalizationAuthorizationVerificationResult | None:
    if request.get("request_id") != FINALIZATION_ORIGINAL_REQUEST_ID:
        return None
    candidate = Path(repo_root).resolve() / FINALIZATION_AUTHORIZATION_PATH
    if not candidate.exists() and not candidate.is_symlink():
        return None
    try:
        _authorization, result = verify_finalization_authorization_file(
            repo_root=repo_root,
            request=request,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise TushareFetchError(f"invalid financial-negative-list finalization authorization: {exc}") from exc
    return result


class _EndpointPacer:
    def __init__(self, client: TushareQueryClient) -> None:
        self._enabled = bool(getattr(client, "requires_single_code_rate_limit", False))
        self._next_at: dict[str, float] = {}

    def wait(self, endpoint: str) -> None:
        if not self._enabled:
            return
        now = monotonic()
        ready = self._next_at.get(endpoint)
        if ready is not None and ready > now:
            sleep(ready - now)
        self._next_at[endpoint] = monotonic() + _REQUEST_INTERVAL_SECONDS


CollectionProgressCallback = Callable[[str, int, int, int, int], None]


def _normalize_required_hex64(value: object, *, field_name: str) -> str:
    text = str(value or "").strip()
    if _HEX64_RE.fullmatch(text) is None:
        raise TushareFetchError(f"{field_name} must be 64-char lowercase hex")
    return text


def _verify_required_bindings(
    *,
    repo_root: Path,
    run_contract_id: object,
    run_contract_version: object,
    response_boundary_policy_id: object,
    response_boundary_policy_file_sha256: object,
    response_boundary_reason_code: object,
) -> _VerifiedBindings:
    provided = (
        run_contract_id,
        run_contract_version,
        response_boundary_policy_id,
        response_boundary_policy_file_sha256,
        response_boundary_reason_code,
    )
    if any(value is None for value in provided):
        raise TushareFetchError(
            "verified v3 run-contract/policy bindings are required before collection verification or execution"
        )
    version_text = str(run_contract_version).strip()
    if version_text != RUN_CONTRACT_VERSION_V3:
        raise TushareFetchError(
            "network financial-negative-list collection requires verified v3 run-contract/policy bindings"
        )
    run_contract_hash = _normalize_required_hex64(run_contract_id, field_name="verified_run_contract_id")
    policy_id_hash = _normalize_required_hex64(
        response_boundary_policy_id,
        field_name="verified_response_boundary_policy_id",
    )
    policy_file_hash = _normalize_required_hex64(
        response_boundary_policy_file_sha256,
        field_name="verified_response_boundary_policy_file_sha256",
    )
    reason_code_text = str(response_boundary_reason_code).strip()
    if not reason_code_text:
        raise TushareFetchError("verified_response_boundary_reason_code must be non-empty")

    contract, contract_result = verify_run_contract_file(
        run_contract_path=DEFAULT_RUN_CONTRACT_PATH,
        repo_root=repo_root,
    )
    if contract_result.run_contract_version != RUN_CONTRACT_VERSION_V3:
        raise TushareFetchError("network financial-negative-list collection is retired for non-v3 run contracts")
    if contract_result.run_contract_id != run_contract_hash:
        raise TushareFetchError("verified_run_contract_id mismatch against sealed v3 run contract")
    if contract.run_contract_version != version_text:
        raise TushareFetchError("verified_run_contract_version mismatch against sealed v3 run contract")
    if contract.response_boundary_policy_id != policy_id_hash:
        raise TushareFetchError("verified_response_boundary_policy_id mismatch against sealed v3 run contract")
    if contract.response_boundary_policy_file_sha256 != policy_file_hash:
        raise TushareFetchError("verified_response_boundary_policy_file_sha256 mismatch against sealed v3 run contract")
    if contract.response_boundary_reason_code != reason_code_text:
        raise TushareFetchError("verified_response_boundary_reason_code mismatch against sealed v3 run contract")

    policy, policy_result = verify_policy_file(repo_root=repo_root)
    if policy.policy_id != policy_id_hash:
        raise TushareFetchError("verified_response_boundary_policy_id mismatch against sealed policy file")
    if policy_result.policy_file_sha256 != policy_file_hash:
        raise TushareFetchError("verified_response_boundary_policy_file_sha256 mismatch against sealed policy file")
    if policy_result.quarantine_reason_code != reason_code_text:
        raise TushareFetchError("verified_response_boundary_reason_code mismatch against sealed policy file")

    return _VerifiedBindings(
        run_contract_id=run_contract_hash,
        run_contract_version=version_text,
        response_boundary_policy_id=policy_id_hash,
        response_boundary_policy_file_sha256=policy_file_hash,
        response_boundary_reason_code=reason_code_text,
    )


def collect_tushare_financial_negative_list(
    *,
    client: TushareQueryClient,
    repo_root: Path,
    staging_dir: Path,
    collection_authorization_id: str,
    verified_run_contract_id: str | None = None,
    verified_run_contract_version: str | None = None,
    verified_response_boundary_policy_id: str | None = None,
    verified_response_boundary_policy_file_sha256: str | None = None,
    verified_response_boundary_reason_code: str | None = None,
    progress_callback: CollectionProgressCallback | None = None,
) -> FinancialNegativeListCollectionResult:
    authorization_id = _normalize_authorization_id(collection_authorization_id)
    repo_root_path = Path(repo_root)
    verified_bindings = _verify_required_bindings(
        repo_root=repo_root_path,
        run_contract_id=verified_run_contract_id,
        run_contract_version=verified_run_contract_version,
        response_boundary_policy_id=verified_response_boundary_policy_id,
        response_boundary_policy_file_sha256=verified_response_boundary_policy_file_sha256,
        response_boundary_reason_code=verified_response_boundary_reason_code,
    )
    protocol = verify_protocol_file(repo_root_path)
    _verify_protocol_window(protocol)
    endpoint_fields, endpoint_docs = _protocol_endpoint_bindings(protocol)
    symbols, listing_dates = _load_bound_canonical_symbols(repo_root_path, protocol)
    request_payload = _build_request_payload(
        repo_root=repo_root_path,
        protocol=protocol,
        symbols=symbols,
        endpoint_fields=endpoint_fields,
        endpoint_docs=endpoint_docs,
        collection_authorization_id=authorization_id,
        verified_bindings=verified_bindings,
    )
    request_id = _json_sha256(request_payload)
    expected_request = {**request_payload, "request_id": request_id}
    root = _prepare_staging_root(
        repo_root=repo_root_path,
        staging_dir=Path(staging_dir),
        create=True,
    )
    request_path = root / "collection_request.json"
    if request_path.exists() or request_path.is_symlink():
        _assert_safe_staging_path(
            root=root,
            path=request_path,
            label="collection_request.json",
            expect_file=True,
        )
        existing = _read_json(request_path, "collection_request.json")
        if existing != expected_request:
            raise TushareFetchError(
                "staging directory belongs to a different financial-negative-list request; use a new --staging-dir"
            )
    else:
        _assert_safe_staging_write_path(root=root, path=request_path, label="collection_request.json")
        _write_json_atomic(request_path, expected_request)

    manifest_path = root / "collection_manifest.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        _assert_safe_staging_path(
            root=root,
            path=manifest_path,
            label="collection_manifest.json",
            expect_file=True,
        )
        verified = verify_financial_negative_list_collection(
            repo_root=repo_root_path,
            staging_dir=root,
            expected_collection_authorization_id=authorization_id,
        )
        if verified.request_id != request_id:
            raise TushareFetchError("financial-negative-list collection manifest request ID does not match")
        return verified

    pacer = _EndpointPacer(client)
    total = len(symbols) * len(SOURCE_ENDPOINTS)
    completed = 0
    reused = 0
    quality_counts: dict[str, dict[str, int]] = {
        endpoint: {
            "rows": 0,
            "nonempty_partitions": 0,
            "empty_partitions": 0,
            "missing_ann_date_rows": 0,
            "conflict_rows": 0,
        }
        for endpoint in SOURCE_ENDPOINTS
    }
    post_window_symbols = sorted(
        symbol for symbol, listed_on in listing_dates.items() if listed_on > ANNOUNCEMENT_COLLECTION_END
    )
    for endpoint in SOURCE_ENDPOINTS:
        fields = endpoint_fields[endpoint]
        endpoint_done = 0
        endpoint_total = len(symbols)
        for symbol in symbols:
            partition_path = root / "partitions" / endpoint / f"{symbol.replace('.', '_')}.parquet"
            listed_on = listing_dates[symbol]
            if partition_path.exists() or partition_path.is_symlink():
                _assert_safe_staging_path(
                    root=root,
                    path=partition_path,
                    label=f"{endpoint} partition {partition_path.name}",
                    expect_file=True,
                )
                frame = pl.read_parquet(partition_path)
                _validate_existing_partition(
                    frame=frame,
                    endpoint=endpoint,
                    symbol=symbol,
                    fields=fields,
                    listed_on=listed_on,
                )
                reused += 1
            elif listed_on > ANNOUNCEMENT_COLLECTION_END:
                frame = _empty_partition(fields)
                _assert_safe_staging_write_path(
                    root=root,
                    path=partition_path,
                    label=f"{endpoint} partition {partition_path.name}",
                )
                _write_parquet_atomic(partition_path, frame)
                completed += 1
            else:
                pacer.wait(endpoint)
                raw = client.query(
                    endpoint,
                    ts_code=symbol,
                    start_date=ymd(ANNOUNCEMENT_COLLECTION_START),
                    end_date=ymd(ANNOUNCEMENT_COLLECTION_END),
                    fields=",".join(fields),
                )
                cap = _ROW_LIMITS[endpoint]
                if raw.height >= cap:
                    raise DataQualityError(
                        f"{endpoint} returned {raw.height} rows for {symbol}; "
                        f"response may be truncated at/above cap={cap}"
                    )
                partition = _build_canonical_partition(
                    raw=raw,
                    endpoint=endpoint,
                    symbol=symbol,
                    fields=fields,
                    request_id=request_id,
                )
                _write_response_boundary_receipts(
                    root=root,
                    endpoint=endpoint,
                    symbol=symbol,
                    request_id=request_id,
                    receipts=partition.response_boundary_receipts,
                )
                _assert_safe_staging_write_path(
                    root=root,
                    path=partition_path,
                    label=f"{endpoint} partition {partition_path.name}",
                )
                _write_parquet_atomic(partition_path, partition.frame)
                completed += 1
                frame = partition.frame
            counts = quality_counts[endpoint]
            counts["rows"] += frame.height
            counts["missing_ann_date_rows"] += _count_missing_ann_rows(frame)
            if frame.is_empty():
                counts["empty_partitions"] += 1
            else:
                counts["nonempty_partitions"] += 1
            endpoint_done += 1
            if progress_callback is not None:
                progress_callback(endpoint, completed + reused, total, endpoint_done, endpoint_total)

    finalization_bindings = _optional_finalization_bindings(
        repo_root=repo_root_path,
        request=expected_request,
    )
    dataset_hashes = _dataset_hashes(root)
    receipt_hashes = _response_boundary_receipt_hashes(root)
    receipt_summary = _response_boundary_receipt_summary(
        root,
        request_id=request_id,
        finalization_bindings=finalization_bindings,
    )
    collected_at = _utc_now_text()
    source_manifest = _build_source_manifest(
        root=root,
        request_id=request_id,
        collection_authorization_id=authorization_id,
        request=request_payload,
        protocol=protocol,
        protocol_id=str(protocol.protocol_id),
        endpoint_fields=endpoint_fields,
        endpoint_docs=endpoint_docs,
        symbols=symbols,
        dataset_hashes=dataset_hashes,
        receipt_hashes=receipt_hashes,
        receipt_summary=receipt_summary,
        collected_at=collected_at,
        verified_bindings=verified_bindings,
        finalization_bindings=finalization_bindings,
    )
    source_manifest_path = root / "source_manifest.json"
    _assert_safe_staging_write_path(root=root, path=source_manifest_path, label="source_manifest.json")
    _write_json_atomic(source_manifest_path, source_manifest)

    quality_report = _build_quality_report(
        request_id=request_id,
        collection_authorization_id=authorization_id,
        protocol_id=str(protocol.protocol_id),
        symbols=symbols,
        quality_counts=quality_counts,
        receipt_summary=receipt_summary,
        post_window_symbols=post_window_symbols,
        verified_bindings=verified_bindings,
        finalization_bindings=finalization_bindings,
    )
    quality_report_path = root / "quality_report.json"
    _assert_safe_staging_write_path(root=root, path=quality_report_path, label="quality_report.json")
    _write_json_atomic(quality_report_path, quality_report)

    manifest = _build_collection_manifest(
        request=request_payload,
        protocol=protocol,
        request_id=request_id,
        collection_authorization_id=authorization_id,
        symbols=symbols,
        dataset_hashes=dataset_hashes,
        receipt_hashes=receipt_hashes,
        receipt_summary=receipt_summary,
        source_manifest_path=source_manifest_path,
        quality_report_path=quality_report_path,
        collected_at=collected_at,
        verified_bindings=verified_bindings,
        finalization_bindings=finalization_bindings,
    )
    _assert_safe_staging_write_path(root=root, path=manifest_path, label="collection_manifest.json")
    _write_json_atomic(manifest_path, manifest)

    return FinancialNegativeListCollectionResult(
        staging_dir=root,
        request_id=request_id,
        collection_authorization_id=authorization_id,
        protocol_id=str(protocol.protocol_id),
        requested_symbols=len(symbols),
        partition_count=total,
        completed_partitions=completed,
        reused_partitions=reused,
        source_manifest_path=source_manifest_path,
        quality_report_path=quality_report_path,
        collection_manifest_path=manifest_path,
    )


def verify_financial_negative_list_collection(
    *,
    repo_root: Path,
    staging_dir: Path,
    expected_collection_authorization_id: str | None = None,
) -> FinancialNegativeListCollectionResult:
    repo_root_path = Path(repo_root)
    protocol = verify_protocol_file(repo_root_path)
    _verify_protocol_window(protocol)
    endpoint_fields, endpoint_docs = _protocol_endpoint_bindings(protocol)
    symbols, listing_dates = _load_bound_canonical_symbols(repo_root_path, protocol)
    root = _prepare_staging_root(
        repo_root=repo_root_path,
        staging_dir=Path(staging_dir),
        create=False,
    )
    request_path = root / "collection_request.json"
    source_manifest_path = root / "source_manifest.json"
    quality_report_path = root / "quality_report.json"
    manifest_path = root / "collection_manifest.json"
    _assert_safe_staging_path(root=root, path=request_path, label="collection_request.json", expect_file=True)
    _assert_safe_staging_path(root=root, path=source_manifest_path, label="source_manifest.json", expect_file=True)
    _assert_safe_staging_path(root=root, path=quality_report_path, label="quality_report.json", expect_file=True)
    _assert_safe_staging_path(root=root, path=manifest_path, label="collection_manifest.json", expect_file=True)
    request = _read_json(request_path, "collection_request.json")
    request_auth_id = _normalize_authorization_id(str(request.get("collection_authorization_id") or ""))
    verified_bindings = _verify_required_bindings(
        repo_root=repo_root_path,
        run_contract_id=request.get("verified_run_contract_id"),
        run_contract_version=request.get("verified_run_contract_version"),
        response_boundary_policy_id=request.get("verified_response_boundary_policy_id"),
        response_boundary_policy_file_sha256=request.get("verified_response_boundary_policy_file_sha256"),
        response_boundary_reason_code=request.get("verified_response_boundary_reason_code"),
    )
    if expected_collection_authorization_id is not None:
        if request_auth_id != _normalize_authorization_id(expected_collection_authorization_id):
            raise TushareFetchError("financial-negative-list collection authorization ID mismatch")
    expected_payload = _build_request_payload(
        repo_root=repo_root_path,
        protocol=protocol,
        symbols=symbols,
        endpoint_fields=endpoint_fields,
        endpoint_docs=endpoint_docs,
        collection_authorization_id=request_auth_id,
        verified_bindings=verified_bindings,
    )
    expected_request = {
        **expected_payload,
        "request_id": _json_sha256(expected_payload),
    }
    if request != expected_request:
        raise TushareFetchError("financial-negative-list collection request drift detected")
    request_id = str(request["request_id"])
    expected_partition_names = {
        endpoint: {f"{symbol.replace('.', '_')}.parquet" for symbol in symbols} for endpoint in SOURCE_ENDPOINTS
    }
    for endpoint in SOURCE_ENDPOINTS:
        family_dir = root / "partitions" / endpoint
        _assert_safe_staging_path(
            root=root,
            path=family_dir,
            label=f"{endpoint} partition family directory",
            expect_dir=True,
        )
        paths = sorted(family_dir.glob("*.parquet"))
        if {p.name for p in paths} != expected_partition_names[endpoint]:
            raise TushareFetchError(f"{endpoint} partition set is incomplete or contains extras")
        for path in paths:
            _assert_safe_staging_path(
                root=root,
                path=path,
                label=f"{endpoint} partition {path.name}",
                expect_file=True,
            )
            symbol = path.stem.replace("_", ".", 1)
            if symbol not in listing_dates:
                raise TushareFetchError(f"{endpoint} partition has unknown symbol: {path.name}")
            _validate_existing_partition(
                frame=pl.read_parquet(path),
                endpoint=endpoint,
                symbol=symbol,
                fields=endpoint_fields[endpoint],
                listed_on=listing_dates[symbol],
            )

    finalization_bindings = _optional_finalization_bindings(
        repo_root=repo_root_path,
        request=expected_request,
    )
    dataset_hashes = _dataset_hashes(root)
    receipt_hashes = _response_boundary_receipt_hashes(root)
    receipt_summary = _response_boundary_receipt_summary(
        root,
        request_id=request_id,
        finalization_bindings=finalization_bindings,
    )
    expected_partition_count = len(symbols) * len(SOURCE_ENDPOINTS)

    source_manifest = _read_json(source_manifest_path, "source_manifest.json")
    source_keys = _SOURCE_MANIFEST_KEYS
    if finalization_bindings is not None:
        source_keys = source_keys | {_FINALIZATION_MANIFEST_KEY}
    _validate_manifest_keys(source_manifest, source_keys, "financial-negative-list source manifest")
    if source_manifest.get("request_id") != request_id:
        raise TushareFetchError("financial-negative-list source manifest request ID does not match")
    collected_at = _validate_collected_at(source_manifest.get("collected_at"), "source manifest collected_at")
    expected_source_manifest = _build_source_manifest(
        root=root,
        request_id=request_id,
        collection_authorization_id=request_auth_id,
        request=expected_payload,
        protocol=protocol,
        protocol_id=str(protocol.protocol_id),
        endpoint_fields=endpoint_fields,
        endpoint_docs=endpoint_docs,
        symbols=symbols,
        dataset_hashes=dataset_hashes,
        receipt_hashes=receipt_hashes,
        receipt_summary=receipt_summary,
        collected_at=collected_at,
        verified_bindings=verified_bindings,
        finalization_bindings=finalization_bindings,
    )
    if source_manifest != expected_source_manifest:
        raise TushareFetchError("financial-negative-list source manifest content drift detected")

    quality = _read_json(quality_report_path, "quality_report.json")
    quality_keys = _QUALITY_REPORT_KEYS
    if finalization_bindings is not None:
        quality_keys = quality_keys | {_FINALIZATION_MANIFEST_KEY}
    _validate_manifest_keys(quality, quality_keys, "financial-negative-list quality report")
    recomputed_quality_counts = _recompute_quality_counts(
        root=root,
        symbols=symbols,
        endpoint_fields=endpoint_fields,
    )
    post_window_symbols = sorted(
        symbol for symbol, listed_on in listing_dates.items() if listed_on > ANNOUNCEMENT_COLLECTION_END
    )
    expected_quality = _build_quality_report(
        request_id=request_id,
        collection_authorization_id=request_auth_id,
        protocol_id=str(protocol.protocol_id),
        symbols=symbols,
        quality_counts=recomputed_quality_counts,
        receipt_summary=receipt_summary,
        post_window_symbols=post_window_symbols,
        verified_bindings=verified_bindings,
        finalization_bindings=finalization_bindings,
    )
    if quality != expected_quality:
        raise TushareFetchError("financial-negative-list quality report content drift detected")

    manifest = _read_json(manifest_path, "collection_manifest.json")
    manifest_keys = _COLLECTION_MANIFEST_KEYS
    if finalization_bindings is not None:
        manifest_keys = manifest_keys | {_FINALIZATION_MANIFEST_KEY}
    _validate_manifest_keys(manifest, manifest_keys, "financial-negative-list collection manifest")
    if manifest.get("request_id") != request_id:
        raise TushareFetchError("financial-negative-list collection manifest request ID does not match")
    if manifest.get("partition_count") != expected_partition_count:
        raise TushareFetchError("financial-negative-list collection partition_count is invalid")
    actual_partition_count = len(list((root / "partitions").glob("*/*.parquet")))
    if actual_partition_count != expected_partition_count:
        raise TushareFetchError("financial-negative-list collection partition set count is invalid")
    manifest_collected_at = _validate_collected_at(
        manifest.get("collected_at"),
        "collection manifest collected_at",
    )
    if manifest_collected_at != collected_at:
        raise TushareFetchError("financial-negative-list collection/source collected_at mismatch")
    expected_collection_manifest = _build_collection_manifest(
        request=expected_payload,
        protocol=protocol,
        request_id=request_id,
        collection_authorization_id=request_auth_id,
        symbols=symbols,
        dataset_hashes=dataset_hashes,
        receipt_hashes=receipt_hashes,
        receipt_summary=receipt_summary,
        source_manifest_path=source_manifest_path,
        quality_report_path=quality_report_path,
        collected_at=collected_at,
        verified_bindings=verified_bindings,
        finalization_bindings=finalization_bindings,
    )
    if manifest != expected_collection_manifest:
        raise TushareFetchError("financial-negative-list collection manifest content drift detected")
    if manifest.get("collection_id") != _collection_id(manifest):
        raise TushareFetchError("financial-negative-list collection_id self-seal mismatch")

    return FinancialNegativeListCollectionResult(
        staging_dir=root,
        request_id=request_id,
        collection_authorization_id=request_auth_id,
        protocol_id=str(protocol.protocol_id),
        requested_symbols=len(symbols),
        partition_count=expected_partition_count,
        completed_partitions=0,
        reused_partitions=expected_partition_count,
        source_manifest_path=source_manifest_path,
        quality_report_path=quality_report_path,
        collection_manifest_path=manifest_path,
    )


def _verify_protocol_window(protocol: Any) -> None:
    window = protocol.source_announcement_collection_window
    if (
        date.fromisoformat(str(window.start)) != ANNOUNCEMENT_COLLECTION_START
        or date.fromisoformat(str(window.end)) != ANNOUNCEMENT_COLLECTION_END
    ):
        raise TushareFetchError("financial-negative-list protocol announcement window binding mismatch")


def _protocol_endpoint_bindings(protocol: Any) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    source_endpoints = protocol.source_endpoints
    fields: dict[str, tuple[str, ...]] = {}
    docs: dict[str, str] = {}
    for endpoint in SOURCE_ENDPOINTS:
        entry = getattr(source_endpoints, endpoint)
        if str(entry.tushare_api) != endpoint:
            raise TushareFetchError(f"protocol source endpoint drift: {endpoint}")
        endpoint_fields = tuple(str(name) for name in entry.fields)
        if not endpoint_fields:
            raise TushareFetchError(f"protocol source endpoint fields missing: {endpoint}")
        fields[endpoint] = endpoint_fields
        docs[endpoint] = str(entry.official_doc)
    return fields, docs


def _load_bound_canonical_symbols(repo_root: Path, protocol: Any) -> tuple[list[str], dict[str, date]]:
    raw_collection_dir = _resolve_path(repo_root, Path(str(protocol.bindings.raw_collection_dir)))
    stock_basic_path = raw_collection_dir / "reference" / "stock_basic.parquet"
    symbols, listing_dates = load_canonical_symbol_listing_dates(stock_basic_path)
    return symbols, listing_dates


def _build_request_payload(
    *,
    repo_root: Path,
    protocol: Any,
    symbols: list[str],
    endpoint_fields: dict[str, tuple[str, ...]],
    endpoint_docs: dict[str, str],
    collection_authorization_id: str,
    verified_bindings: _VerifiedBindings,
) -> dict[str, Any]:
    protocol_path = repo_root / PROTOCOL_FILE_PATH
    if not protocol_path.is_file():
        raise TushareFetchError(f"missing bound protocol file: {PROTOCOL_FILE_PATH}")
    bindings = protocol.bindings
    return {
        "schema_version": _SCHEMA_VERSION,
        "protocol_id": str(protocol.protocol_id),
        "collection_authorization_id": collection_authorization_id,
        "protocol_file_path": PROTOCOL_FILE_PATH,
        "protocol_file_sha256": _sha256_file(protocol_path),
        "source_name": "tushare_financial_negative_list_collection",
        "announcement_window_start": ANNOUNCEMENT_COLLECTION_START.isoformat(),
        "announcement_window_end": ANNOUNCEMENT_COLLECTION_END.isoformat(),
        "candidate_pack_id": str(bindings.candidate_pack_id),
        "candidate_pack_parquet_sha256": str(bindings.candidate_pack_parquet_sha256),
        "raw_collection_request_id": str(bindings.raw_collection_request_id),
        "raw_collection_manifest_sha256": str(bindings.raw_collection_manifest_sha256),
        "raw_quality_report_sha256": str(bindings.raw_quality_report_sha256),
        "verified_run_contract_id": verified_bindings.run_contract_id,
        "verified_run_contract_version": verified_bindings.run_contract_version,
        "verified_response_boundary_policy_id": verified_bindings.response_boundary_policy_id,
        "verified_response_boundary_policy_file_sha256": verified_bindings.response_boundary_policy_file_sha256,
        "verified_response_boundary_reason_code": verified_bindings.response_boundary_reason_code,
        "symbols_sha256": _symbols_sha256(symbols),
        "requested_symbols": len(symbols),
        "source_fields": {name: list(endpoint_fields[name]) for name in SOURCE_ENDPOINTS},
        "source_documents": {name: endpoint_docs[name] for name in SOURCE_ENDPOINTS},
        "response_boundary_policy_path": RESPONSE_BOUNDARY_POLICY_FILE_PATH,
        "availability_policy": _AVAILABILITY_POLICY,
        "raw_preservation_semantics": _RAW_PRESERVATION_SEMANTICS,
    }


def _build_canonical_partition(
    *,
    raw: pl.DataFrame,
    endpoint: str,
    symbol: str,
    fields: tuple[str, ...],
    request_id: str,
) -> _CanonicalPartitionBuild:
    if raw.is_empty() and not raw.columns:
        return _CanonicalPartitionBuild(frame=_empty_partition(fields), response_boundary_receipts=[])
    missing = sorted(set(fields) - set(raw.columns))
    if missing:
        raise DataQualityError(f"{endpoint} partition missing columns: {missing}")
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    seen_source_hashes: set[str] = set()
    seen_receipt_identity: dict[tuple[Any, ...], str] = {}
    for line, item in enumerate(raw.select(fields).iter_rows(named=True), start=1):
        for field_name in fields:
            if isinstance(item.get(field_name), bool):
                raise DataQualityError(
                    f"{endpoint} contains boolean in field {field_name} for {symbol} at source row {line}"
                )
        observed_symbol = require_ts_code(str(item.get("ts_code") or ""), kind="stock")
        if observed_symbol != symbol:
            raise DataQualityError(f"{endpoint} returned another symbol for {symbol} at source row {line}")
        ann_text, ann_date = _optional_ymd_text(
            item.get("ann_date"),
            "ann_date",
            endpoint,
            symbol,
            line,
            reject_2025=False,
        )
        end_text, end_date = _required_ymd_text(item.get("end_date"), "end_date", endpoint, symbol, line)
        if end_date > ANNOUNCEMENT_COLLECTION_END:
            raise DataQualityError(f"{endpoint} contains 2025+ end_date for {symbol} at source row {line}")
        f_ann_text = None
        f_ann_date: date | None = None
        if "f_ann_date" in fields:
            f_ann_text, f_ann_date = _optional_ymd_text(
                item.get("f_ann_date"),
                "f_ann_date",
                endpoint,
                symbol,
                line,
                reject_2025=False,
            )
        availability_status = "usable"
        effective: date | None
        if ann_date is None:
            if f_ann_date is not None and f_ann_date > ANNOUNCEMENT_COLLECTION_END:
                raise DataQualityError(
                    f"{endpoint} missing ann_date with 2025+ f_ann_date is unsupported "
                    f"for {symbol} at source row {line}"
                )
            availability_status = "missing_ann_date"
            effective = None
            available_at = None
            effective_text = None
        else:
            boundary_decision = evaluate_response_boundary(
                endpoint=endpoint,
                ann_date=ann_date,
                f_ann_date=f_ann_date,
                end_date=end_date,
            )
            if boundary_decision.action == "reject":
                raise DataQualityError(f"{endpoint} response boundary rejects row for {symbol} at source row {line}")
            if boundary_decision.action == "quarantine":
                effective = boundary_decision.effective_disclosure_date
                if effective is None:
                    raise DataQualityError(
                        f"{endpoint} quarantine missing effective disclosure for {symbol} at source row {line}"
                    )
                effective_text = ymd(effective)
                available_at = datetime.combine(effective, _AVAILABLE_AT_TIME, _SHANGHAI_TZ).isoformat()
            else:
                effective = ann_date if f_ann_date is None else max(ann_date, f_ann_date)
                if effective > ANNOUNCEMENT_COLLECTION_END:
                    raise DataQualityError(
                        f"{endpoint} effective disclosure is 2025+ for {symbol} at source row {line}"
                    )
                effective_text = ymd(effective)
                available_at = datetime.combine(effective, _AVAILABLE_AT_TIME, _SHANGHAI_TZ).isoformat()
        row: dict[str, Any] = {}
        for field_name in fields:
            value = item.get(field_name)
            if field_name == "ann_date":
                row[field_name] = ann_text
            elif field_name == "f_ann_date":
                row[field_name] = f_ann_text
            elif field_name == "end_date":
                row[field_name] = end_text
            elif field_name in _INT_FIELDS:
                row[field_name] = _coerce_int(value, endpoint, symbol, line, field_name)
            elif field_name in _ID_TEXT_FIELDS:
                row[field_name] = _coerce_text(value)
            else:
                row[field_name] = _coerce_float(value, endpoint, symbol, line, field_name)
        source_hash_payload = _canonical_source_hash_payload(row=row, fields=fields)
        source_row_hash = _json_sha256(source_hash_payload)
        if source_row_hash in seen_source_hashes:
            raise DataQualityError(f"{endpoint} has exact duplicate source rows for {symbol}")
        seen_source_hashes.add(source_row_hash)
        boundary_decision = evaluate_response_boundary(
            endpoint=endpoint,
            ann_date=ann_date,
            f_ann_date=f_ann_date,
            end_date=end_date,
        )
        if boundary_decision.action == "quarantine":
            if boundary_decision.reason_code != RESPONSE_BOUNDARY_REASON_CODE:
                raise DataQualityError(f"{endpoint} response boundary reason drift for {symbol}")
            receipt = _build_response_boundary_receipt(
                request_id=request_id,
                endpoint=endpoint,
                symbol=symbol,
                ann_date=ann_text,
                f_ann_date=f_ann_text,
                end_date=end_text,
                row=row,
                effective_disclosure_date=ymd(boundary_decision.effective_disclosure_date),  # type: ignore[arg-type]
                source_row_hash=source_row_hash,
            )
            identity = _response_boundary_receipt_identity(receipt)
            prior_hash = seen_receipt_identity.get(identity)
            if prior_hash is not None and prior_hash != receipt["source_row_hash"]:
                raise DataQualityError(
                    f"{endpoint} has conflicting source rows for same response-boundary receipt identity {symbol}"
                )
            seen_receipt_identity[identity] = receipt["source_row_hash"]
            receipts.append(receipt)
            continue
        row.update(
            {
                "effective_disclosure_date": effective_text,
                "available_at": available_at,
                "availability_status": availability_status,
                "source_row_hash": source_row_hash,
            }
        )
        rows.append(row)
    if not rows:
        return _CanonicalPartitionBuild(frame=_empty_partition(fields), response_boundary_receipts=receipts)
    frame = pl.DataFrame(rows, schema=_partition_schema(fields)).select(_partition_columns(fields))
    _validate_duplicate_and_conflict(frame=frame, endpoint=endpoint, symbol=symbol, fields=fields)
    canonical = frame.sort(
        [
            "availability_status",
            "effective_disclosure_date",
            "available_at",
            "source_row_hash",
        ],
        nulls_last=True,
    )
    return _CanonicalPartitionBuild(frame=canonical, response_boundary_receipts=receipts)


def _validate_duplicate_and_conflict(
    *,
    frame: pl.DataFrame,
    endpoint: str,
    symbol: str,
    fields: tuple[str, ...],
) -> None:
    if frame["source_row_hash"].n_unique() != frame.height:
        raise DataQualityError(f"{endpoint} has exact duplicate source rows for {symbol}")
    semantic_fields = [
        "ts_code",
        "availability_status",
        "effective_disclosure_date",
        "available_at",
    ]
    for name in ("ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type", "update_flag"):
        if name in fields:
            semantic_fields.append(name)
    duplicates = frame.group_by(semantic_fields).len().filter(pl.col("len") > 1)
    if duplicates.height:
        raise DataQualityError(f"{endpoint} has same semantic key+availability conflict for {symbol}")


def _validate_existing_partition(
    *,
    frame: pl.DataFrame,
    endpoint: str,
    symbol: str,
    fields: tuple[str, ...],
    listed_on: date,
) -> None:
    _validate_partition_schema(frame=frame, endpoint=endpoint, symbol=symbol, fields=fields)
    if listed_on > ANNOUNCEMENT_COLLECTION_END:
        if frame.height != 0:
            raise DataQualityError(f"existing {endpoint} partition must be empty for post-window listing {symbol}")
        return
    _validate_canonical_partition_frame(
        frame=frame,
        endpoint=endpoint,
        symbol=symbol,
        fields=fields,
    )


def _validate_canonical_partition_frame(
    *,
    frame: pl.DataFrame,
    endpoint: str,
    symbol: str,
    fields: tuple[str, ...],
) -> None:
    if frame.is_empty():
        return
    for line, item in enumerate(frame.iter_rows(named=True), start=1):
        observed_symbol = require_ts_code(str(item.get("ts_code") or ""), kind="stock")
        if observed_symbol != symbol:
            raise DataQualityError(f"existing {endpoint} partition has foreign symbol for {symbol}")
        ann_text = item.get("ann_date")
        f_ann_text = item.get("f_ann_date") if "f_ann_date" in fields else None
        end_text = item.get("end_date")
        end_text_norm, end_date = _required_stored_ymd_text(end_text, "end_date", endpoint, symbol, line)
        if end_date > ANNOUNCEMENT_COLLECTION_END:
            raise DataQualityError(f"existing {endpoint} partition has 2025+ end_date for {symbol}")
        ann_text_norm, ann_date = _optional_stored_ymd_text(ann_text, "ann_date", endpoint, symbol, line)
        f_ann_text_norm, f_ann_date = _optional_stored_ymd_text(f_ann_text, "f_ann_date", endpoint, symbol, line)
        status = str(item.get("availability_status") or "")
        if ann_date is None:
            if status != "missing_ann_date":
                raise DataQualityError(f"existing {endpoint} partition availability_status mismatch for {symbol}")
            if item.get("available_at") is not None or item.get("effective_disclosure_date") is not None:
                raise DataQualityError(
                    f"existing {endpoint} partition missing_ann_date row is noncanonical for {symbol}"
                )
        else:
            if status != "usable":
                raise DataQualityError(f"existing {endpoint} partition availability_status mismatch for {symbol}")
            if not (ANNOUNCEMENT_COLLECTION_START <= ann_date <= ANNOUNCEMENT_COLLECTION_END):
                raise DataQualityError(
                    f"existing {endpoint} partition ann_date is outside requested window for {symbol}"
                )
            effective = ann_date if f_ann_date is None else max(ann_date, f_ann_date)
            expected_effective = ymd(effective)
            expected_available_at = datetime.combine(effective, _AVAILABLE_AT_TIME, _SHANGHAI_TZ).isoformat()
            if item.get("effective_disclosure_date") != expected_effective:
                raise DataQualityError(f"existing {endpoint} partition effective_disclosure_date mismatch for {symbol}")
            if item.get("available_at") != expected_available_at:
                raise DataQualityError(f"existing {endpoint} partition available_at mismatch for {symbol}")
        if item.get("end_date") != end_text_norm:
            raise DataQualityError(f"existing {endpoint} partition end_date formatting mismatch for {symbol}")
        if item.get("ann_date") != ann_text_norm:
            raise DataQualityError(f"existing {endpoint} partition ann_date formatting mismatch for {symbol}")
        if "f_ann_date" in fields and item.get("f_ann_date") != f_ann_text_norm:
            raise DataQualityError(f"existing {endpoint} partition f_ann_date formatting mismatch for {symbol}")
        for field_name in fields:
            if field_name in _INT_FIELDS:
                _coerce_int(item.get(field_name), endpoint, symbol, line, field_name)
            elif field_name in _ID_TEXT_FIELDS:
                _coerce_text(item.get(field_name))
            else:
                _coerce_float(item.get(field_name), endpoint, symbol, line, field_name)
        expected_hash = _json_sha256(
            _canonical_source_hash_payload({name: item.get(name) for name in fields}, fields=fields)
        )
        if item.get("source_row_hash") != expected_hash:
            raise DataQualityError(f"existing {endpoint} partition source_row_hash mismatch for {symbol}")
    _validate_duplicate_and_conflict(frame=frame, endpoint=endpoint, symbol=symbol, fields=fields)
    canonical = frame.sort(
        ["availability_status", "effective_disclosure_date", "available_at", "source_row_hash"],
        nulls_last=True,
    )
    if not frame.equals(canonical, null_equal=True):
        raise DataQualityError(f"existing {endpoint} partition is not canonical for {symbol}")


def _build_source_manifest(
    *,
    root: Path,
    request_id: str,
    collection_authorization_id: str,
    request: dict[str, Any],
    protocol: Any,
    protocol_id: str,
    endpoint_fields: dict[str, tuple[str, ...]],
    endpoint_docs: dict[str, str],
    symbols: list[str],
    dataset_hashes: dict[str, str],
    receipt_hashes: dict[str, str],
    receipt_summary: dict[str, Any],
    collected_at: str,
    verified_bindings: _VerifiedBindings,
    finalization_bindings: FinalizationAuthorizationVerificationResult | None,
) -> dict[str, Any]:
    endpoint_stats: dict[str, Any] = {}
    for endpoint in SOURCE_ENDPOINTS:
        stats = _endpoint_partition_stats(
            root=root,
            endpoint=endpoint,
            symbols=symbols,
        )
        endpoint_stats[endpoint] = {
            "fields": list(endpoint_fields[endpoint]),
            "official_doc": endpoint_docs[endpoint],
            "partitions": stats["partitions"],
            "nonempty_partitions": stats["nonempty_partitions"],
            "empty_partitions": stats["empty_partitions"],
            "rows": stats["rows"],
            "dataset_hash": dataset_hashes[endpoint],
        }
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "request_id": request_id,
        "collection_authorization_id": collection_authorization_id,
        "protocol_id": protocol_id,
        "protocol_file_path": request["protocol_file_path"],
        "protocol_file_sha256": request["protocol_file_sha256"],
        "candidate_pack_id": str(protocol.bindings.candidate_pack_id),
        "candidate_pack_parquet_sha256": str(protocol.bindings.candidate_pack_parquet_sha256),
        "raw_collection_request_id": str(protocol.bindings.raw_collection_request_id),
        "raw_collection_manifest_sha256": str(protocol.bindings.raw_collection_manifest_sha256),
        "raw_quality_report_sha256": str(protocol.bindings.raw_quality_report_sha256),
        "verified_run_contract_id": verified_bindings.run_contract_id,
        "verified_run_contract_version": verified_bindings.run_contract_version,
        "verified_response_boundary_policy_id": verified_bindings.response_boundary_policy_id,
        "verified_response_boundary_policy_file_sha256": verified_bindings.response_boundary_policy_file_sha256,
        "verified_response_boundary_reason_code": verified_bindings.response_boundary_reason_code,
        "announcement_window_start": ANNOUNCEMENT_COLLECTION_START.isoformat(),
        "announcement_window_end": ANNOUNCEMENT_COLLECTION_END.isoformat(),
        "requested_symbols": len(symbols),
        "collected_at": collected_at,
        "availability_policy": _AVAILABILITY_POLICY,
        "raw_preservation_semantics": _RAW_PRESERVATION_SEMANTICS,
        "response_boundary_policy": {
            "policy_path": RESPONSE_BOUNDARY_POLICY_FILE_PATH,
            "policy_id": verified_bindings.response_boundary_policy_id,
            "policy_file_sha256": verified_bindings.response_boundary_policy_file_sha256,
            "quarantine_reason_code": verified_bindings.response_boundary_reason_code,
        },
        "response_boundary_receipts": {
            "count": int(receipt_summary["count"]),
            "hashes": receipt_hashes,
            "by_endpoint": dict(receipt_summary["by_endpoint"]),
            "reason_code_distribution": dict(receipt_summary["reason_code_distribution"]),
        },
        "endpoints": endpoint_stats,
    }
    if finalization_bindings is not None:
        manifest[_FINALIZATION_MANIFEST_KEY] = _finalization_manifest_payload(
            finalization_bindings=finalization_bindings,
            receipt_summary=receipt_summary,
        )
    return manifest


def _build_quality_report(
    *,
    request_id: str,
    collection_authorization_id: str,
    protocol_id: str,
    symbols: list[str],
    quality_counts: dict[str, dict[str, int]],
    receipt_summary: dict[str, Any],
    post_window_symbols: list[str],
    verified_bindings: _VerifiedBindings,
    finalization_bindings: FinalizationAuthorizationVerificationResult | None,
) -> dict[str, Any]:
    report = {
        "schema_version": _SCHEMA_VERSION,
        "request_id": request_id,
        "collection_authorization_id": collection_authorization_id,
        "protocol_id": protocol_id,
        "announcement_window": {
            "start": ANNOUNCEMENT_COLLECTION_START.isoformat(),
            "end": ANNOUNCEMENT_COLLECTION_END.isoformat(),
        },
        "requested_symbols": len(symbols),
        "partition_count": len(symbols) * len(SOURCE_ENDPOINTS),
        "verified_run_contract_id": verified_bindings.run_contract_id,
        "verified_run_contract_version": verified_bindings.run_contract_version,
        "verified_response_boundary_policy_id": verified_bindings.response_boundary_policy_id,
        "verified_response_boundary_policy_file_sha256": verified_bindings.response_boundary_policy_file_sha256,
        "verified_response_boundary_reason_code": verified_bindings.response_boundary_reason_code,
        "sources": {
            endpoint: {
                **quality_counts[endpoint],
                "response_boundary_receipt_count": int(receipt_summary["by_endpoint"][endpoint]),
                "empty_partition_reason_post_window_listing_count": len(post_window_symbols),
            }
            for endpoint in SOURCE_ENDPOINTS
        },
        "response_boundary": {
            "policy_path": RESPONSE_BOUNDARY_POLICY_FILE_PATH,
            "policy_id": verified_bindings.response_boundary_policy_id,
            "policy_file_sha256": verified_bindings.response_boundary_policy_file_sha256,
            "quarantine_reason_code": verified_bindings.response_boundary_reason_code,
            "receipt_count": int(receipt_summary["count"]),
            "receipt_count_by_endpoint": dict(receipt_summary["by_endpoint"]),
            "reason_code_distribution": dict(receipt_summary["reason_code_distribution"]),
        },
        "post_window_listed_symbols": post_window_symbols,
        "ready_for_scoring": False,
        "ready_for_backtest": False,
        "ready_for_trading": False,
    }
    if finalization_bindings is not None:
        report[_FINALIZATION_MANIFEST_KEY] = _finalization_manifest_payload(
            finalization_bindings=finalization_bindings,
            receipt_summary=receipt_summary,
        )
    return report


def _build_collection_manifest(
    *,
    request: dict[str, Any],
    protocol: Any,
    request_id: str,
    collection_authorization_id: str,
    symbols: list[str],
    dataset_hashes: dict[str, str],
    receipt_hashes: dict[str, str],
    receipt_summary: dict[str, Any],
    source_manifest_path: Path,
    quality_report_path: Path,
    collected_at: str,
    verified_bindings: _VerifiedBindings,
    finalization_bindings: FinalizationAuthorizationVerificationResult | None,
) -> dict[str, Any]:
    request_path = source_manifest_path.parent / "collection_request.json"
    manifest: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "request_id": request_id,
        "collection_authorization_id": collection_authorization_id,
        "protocol_id": str(protocol.protocol_id),
        "protocol_file_path": request["protocol_file_path"],
        "protocol_file_sha256": request["protocol_file_sha256"],
        "candidate_pack_id": str(protocol.bindings.candidate_pack_id),
        "candidate_pack_parquet_sha256": str(protocol.bindings.candidate_pack_parquet_sha256),
        "raw_collection_request_id": str(protocol.bindings.raw_collection_request_id),
        "raw_collection_manifest_sha256": str(protocol.bindings.raw_collection_manifest_sha256),
        "raw_quality_report_sha256": str(protocol.bindings.raw_quality_report_sha256),
        "verified_run_contract_id": verified_bindings.run_contract_id,
        "verified_run_contract_version": verified_bindings.run_contract_version,
        "verified_response_boundary_policy_id": verified_bindings.response_boundary_policy_id,
        "verified_response_boundary_policy_file_sha256": verified_bindings.response_boundary_policy_file_sha256,
        "verified_response_boundary_reason_code": verified_bindings.response_boundary_reason_code,
        "announcement_window_start": ANNOUNCEMENT_COLLECTION_START.isoformat(),
        "announcement_window_end": ANNOUNCEMENT_COLLECTION_END.isoformat(),
        "requested_symbols": len(symbols),
        "partition_count": len(symbols) * len(SOURCE_ENDPOINTS),
        "symbols_sha256": _symbols_sha256(symbols),
        "dataset_hashes": dataset_hashes,
        "request_sha256": _sha256_file(request_path),
        "source_manifest_sha256": _sha256_file(source_manifest_path),
        "quality_report_sha256": _sha256_file(quality_report_path),
        "response_boundary_receipt_hashes": receipt_hashes,
        "collected_at": collected_at,
        "ready_for_scoring": False,
        "ready_for_backtest": False,
        "ready_for_trading": False,
    }
    if finalization_bindings is not None:
        manifest[_FINALIZATION_MANIFEST_KEY] = _finalization_manifest_payload(
            finalization_bindings=finalization_bindings,
            receipt_summary=receipt_summary,
        )
    manifest["collection_id"] = _collection_id(manifest)
    return manifest


def _finalization_manifest_payload(
    *,
    finalization_bindings: FinalizationAuthorizationVerificationResult,
    receipt_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "authorization_path": FINALIZATION_AUTHORIZATION_PATH.as_posix(),
        "authorization_id": finalization_bindings.authorization_id,
        "authorization_file_sha256": finalization_bindings.authorization_file_sha256,
        "policy_path": finalization_bindings.policy_path,
        "policy_id": finalization_bindings.policy_id,
        "policy_file_sha256": finalization_bindings.policy_file_sha256,
        "nullable_end_type_endpoints": list(finalization_bindings.nullable_end_type_endpoints),
        "null_end_type_receipt_count_by_endpoint": {
            endpoint: int(receipt_summary["null_end_type_receipt_count_by_endpoint"][endpoint])
            for endpoint in finalization_bindings.nullable_end_type_endpoints
        },
        "network_access_allowed": False,
        "future_payload_values_allowed": False,
        "ready_for_scoring": False,
        "ready_for_backtest": False,
        "ready_for_trading": False,
    }


def _empty_partition(fields: tuple[str, ...]) -> pl.DataFrame:
    return pl.DataFrame(schema=_partition_schema(fields)).select(_partition_columns(fields))


def _partition_columns(fields: tuple[str, ...]) -> list[str]:
    return [*fields, *_PARTITION_EXTRA_SCHEMA.keys()]


def _partition_schema(fields: tuple[str, ...]) -> dict[str, Any]:
    schema = {name: _field_dtype(name) for name in fields}
    schema.update(_PARTITION_EXTRA_SCHEMA)
    return schema


def _validate_partition_schema(
    *,
    frame: pl.DataFrame,
    endpoint: str,
    symbol: str,
    fields: tuple[str, ...],
) -> None:
    expected_columns = _partition_columns(fields)
    if frame.columns != expected_columns:
        raise DataQualityError(f"existing {endpoint} partition columns are noncanonical for {symbol}")
    expected_schema = _partition_schema(fields)
    if frame.schema != expected_schema:
        raise DataQualityError(f"existing {endpoint} partition schema is noncanonical for {symbol}")


def _field_dtype(field_name: str) -> Any:
    if field_name in _INT_FIELDS:
        return pl.Int64
    if field_name in _ID_TEXT_FIELDS:
        return pl.String
    return pl.Float64


def _count_missing_ann_rows(frame: pl.DataFrame) -> int:
    if frame.is_empty() or "availability_status" not in frame.columns:
        return 0
    return int(frame.filter(pl.col("availability_status") == "missing_ann_date").height)


def _canonical_source_hash_payload(row: dict[str, Any], *, fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: row.get(name) for name in fields}


def _build_response_boundary_receipt(
    *,
    request_id: str,
    endpoint: str,
    symbol: str,
    ann_date: str | None,
    f_ann_date: str | None,
    end_date: str,
    row: dict[str, Any],
    effective_disclosure_date: str,
    source_row_hash: str,
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "request_id": request_id,
        "endpoint": endpoint,
        "symbol": symbol,
        "ann_date": ann_date,
        "f_ann_date": f_ann_date,
        "end_date": end_date,
        "report_type": row.get("report_type"),
        "comp_type": row.get("comp_type"),
        "end_type": row.get("end_type"),
        "update_flag": row.get("update_flag"),
        "effective_disclosure_date": effective_disclosure_date,
        "reason_code": RESPONSE_BOUNDARY_REASON_CODE,
        "source_row_hash": source_row_hash,
    }


def _response_boundary_receipt_identity(receipt: dict[str, Any]) -> tuple[Any, ...]:
    return (
        receipt.get("endpoint"),
        receipt.get("symbol"),
        receipt.get("ann_date"),
        receipt.get("f_ann_date"),
        receipt.get("end_date"),
        receipt.get("report_type"),
        receipt.get("comp_type"),
        receipt.get("end_type"),
        receipt.get("update_flag"),
        receipt.get("effective_disclosure_date"),
        receipt.get("reason_code"),
    )


def _response_boundary_receipt_identity_hash(receipt: dict[str, Any]) -> str:
    identity_raw = json.dumps(
        {
            "endpoint": receipt["endpoint"],
            "symbol": receipt["symbol"],
            "ann_date": receipt["ann_date"],
            "f_ann_date": receipt["f_ann_date"],
            "end_date": receipt["end_date"],
            "report_type": receipt["report_type"],
            "comp_type": receipt["comp_type"],
            "end_type": receipt["end_type"],
            "update_flag": receipt["update_flag"],
            "effective_disclosure_date": receipt["effective_disclosure_date"],
            "reason_code": receipt["reason_code"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity_raw.encode("utf-8")).hexdigest()


def _write_response_boundary_receipts(
    *,
    root: Path,
    endpoint: str,
    symbol: str,
    request_id: str,
    receipts: list[dict[str, Any]],
) -> None:
    for receipt in receipts:
        if (
            receipt.get("request_id") != request_id
            or receipt.get("endpoint") != endpoint
            or receipt.get("symbol") != symbol
        ):
            raise DataQualityError("response-boundary receipt binding mismatch")
        identity_hash = _response_boundary_receipt_identity_hash(receipt)
        path = root / "response-boundary-receipts" / endpoint / f"{identity_hash}.json"
        if path.exists():
            existing = _read_json(path, path.relative_to(root).as_posix())
            if existing != receipt:
                raise DataQualityError(
                    f"response-boundary receipt identity collision for {path.relative_to(root).as_posix()}"
                )
            continue
        _assert_safe_staging_write_path(root=root, path=path, label=path.relative_to(root).as_posix())
        _write_json_atomic(path, receipt)


def _response_boundary_receipt_hashes(root: Path) -> dict[str, str]:
    receipt_root = root / "response-boundary-receipts"
    if not receipt_root.exists():
        return {}
    _assert_safe_staging_path(
        root=root,
        path=receipt_root,
        label="response-boundary-receipts directory",
        expect_dir=True,
    )
    hashes: dict[str, str] = {}
    for path in sorted(receipt_root.rglob("*")):
        _assert_safe_staging_path(
            root=root,
            path=path,
            label=f"response-boundary receipt path {path.name}",
            expect_dir=path.is_dir(),
            expect_file=not path.is_dir(),
        )
        relative = path.relative_to(receipt_root)
        if path.is_dir():
            if len(relative.parts) != 1 or relative.parts[0] not in SOURCE_ENDPOINTS:
                raise TushareFetchError("response-boundary-receipts contains unsupported directories")
            continue
        if len(relative.parts) != 2 or relative.parts[0] not in SOURCE_ENDPOINTS or path.suffix != ".json":
            raise TushareFetchError("response-boundary-receipts contains unsupported extra files")
        hashes[path.relative_to(root).as_posix()] = _sha256_file(path)
    return hashes


def _response_boundary_receipt_summary(
    root: Path,
    *,
    request_id: str,
    finalization_bindings: FinalizationAuthorizationVerificationResult | None = None,
) -> dict[str, Any]:
    receipt_root = root / "response-boundary-receipts"
    if not receipt_root.exists():
        return {
            "count": 0,
            "by_endpoint": {endpoint: 0 for endpoint in SOURCE_ENDPOINTS},
            "reason_code_distribution": {},
            "null_end_type_receipt_count_by_endpoint": {
                endpoint: 0 for endpoint in SOURCE_ENDPOINTS
            },
        }
    receipts: list[dict[str, Any]] = []
    seen_identity: dict[tuple[Any, ...], str] = {}
    known_paths = set(_response_boundary_receipt_hashes(root))
    for path in sorted(receipt_root.rglob("*.json")):
        rel = path.relative_to(root).as_posix()
        if rel not in known_paths:
            raise TushareFetchError("undeclared response-boundary receipt file")
        payload = _read_json(path, rel)
        if set(payload) != _RESPONSE_BOUNDARY_RECEIPT_KEYS:
            raise TushareFetchError(f"invalid response-boundary receipt schema: {path.name}")
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise TushareFetchError(f"invalid response-boundary receipt schema_version: {path.name}")
        if payload.get("request_id") != request_id:
            raise TushareFetchError(f"response-boundary receipt request_id mismatch: {path.name}")
        if payload.get("reason_code") != RESPONSE_BOUNDARY_REASON_CODE:
            raise TushareFetchError(f"response-boundary receipt reason_code mismatch: {path.name}")
        for key in ("endpoint", "symbol", "ann_date", "end_date", "effective_disclosure_date"):
            if not isinstance(payload.get(key), str) or not str(payload.get(key)).strip():
                raise TushareFetchError(f"invalid response-boundary receipt {key}: {path.name}")
        endpoint = str(payload["endpoint"])
        if endpoint not in SOURCE_ENDPOINTS:
            raise TushareFetchError(f"unsupported response-boundary receipt endpoint: {path.name}")
        expected_endpoint_dir = receipt_root / endpoint
        if path.parent != expected_endpoint_dir:
            raise TushareFetchError(f"response-boundary receipt endpoint directory mismatch: {path.name}")
        symbol_text = str(payload["symbol"])
        require_ts_code(symbol_text, kind="stock")
        if re.fullmatch(r"^\d{6}\.(SH|SZ)$", symbol_text) is None:
            raise TushareFetchError(f"invalid response-boundary receipt symbol: {path.name}")
        ann_date = _parse_ymd_strict(
            payload["ann_date"],
            "ann_date",
            endpoint,
            symbol_text,
            line=1,
            allow_hyphen=False,
        )
        end_date = _parse_ymd_strict(
            payload["end_date"],
            "end_date",
            endpoint,
            symbol_text,
            line=1,
            allow_hyphen=False,
        )
        f_ann_value = payload.get("f_ann_date")
        f_ann_date = None
        if f_ann_value is not None:
            if not isinstance(f_ann_value, str) or not f_ann_value.strip():
                raise TushareFetchError(f"invalid response-boundary receipt f_ann_date: {path.name}")
            f_ann_date = _parse_ymd_strict(
                f_ann_value,
                "f_ann_date",
                endpoint,
                symbol_text,
                line=1,
                allow_hyphen=False,
            )
        effective_disclosure_date = _parse_ymd_strict(
            payload["effective_disclosure_date"],
            "effective_disclosure_date",
            endpoint,
            symbol_text,
            line=1,
            allow_hyphen=False,
        )
        if end_date > ANNOUNCEMENT_COLLECTION_END:
            raise TushareFetchError(f"response-boundary receipt end_date is outside allowed window: {path.name}")
        if endpoint in {"balancesheet", "income"}:
            if not (ANNOUNCEMENT_COLLECTION_START <= ann_date <= ANNOUNCEMENT_COLLECTION_END):
                raise TushareFetchError(f"response-boundary receipt ann_date is outside 2020-2024: {path.name}")
            if f_ann_date is None or f_ann_date <= ANNOUNCEMENT_COLLECTION_END:
                raise TushareFetchError(f"response-boundary receipt f_ann_date is not future: {path.name}")
            if effective_disclosure_date != f_ann_date:
                raise TushareFetchError(f"response-boundary receipt effective_disclosure_date mismatch: {path.name}")
            for numeric_key in ("report_type", "comp_type"):
                value = payload.get(numeric_key)
                if not isinstance(value, int) or isinstance(value, bool):
                    raise TushareFetchError(f"invalid response-boundary receipt {numeric_key}: {path.name}")
            end_type = payload.get("end_type")
            allow_null_end_type = (
                finalization_bindings is not None
                and endpoint in finalization_bindings.nullable_end_type_endpoints
            )
            if end_type is not None and (not isinstance(end_type, int) or isinstance(end_type, bool)):
                raise TushareFetchError(f"invalid response-boundary receipt end_type: {path.name}")
            if end_type is None and not allow_null_end_type:
                raise TushareFetchError(f"invalid response-boundary receipt end_type: {path.name}")
            update_flag = payload.get("update_flag")
            if not isinstance(update_flag, str) or update_flag not in {"0", "1"}:
                raise TushareFetchError(f"invalid response-boundary receipt update_flag: {path.name}")
        else:
            if ann_date <= ANNOUNCEMENT_COLLECTION_END:
                raise TushareFetchError(f"response-boundary receipt ann_date is not future: {path.name}")
            if f_ann_date is not None:
                raise TushareFetchError(f"response-boundary receipt f_ann_date must be null: {path.name}")
            if effective_disclosure_date != ann_date:
                raise TushareFetchError(f"response-boundary receipt effective_disclosure_date mismatch: {path.name}")
            for numeric_key in ("report_type", "comp_type", "end_type"):
                if payload.get(numeric_key) is not None:
                    raise TushareFetchError(f"invalid response-boundary receipt {numeric_key}: {path.name}")
            update_flag = payload.get("update_flag")
            if endpoint == "fina_indicator":
                if not isinstance(update_flag, str) or update_flag not in {"0", "1"}:
                    raise TushareFetchError(f"invalid response-boundary receipt update_flag: {path.name}")
            elif update_flag is not None:
                raise TushareFetchError(f"invalid response-boundary receipt update_flag: {path.name}")
        expected_filename = f"{_response_boundary_receipt_identity_hash(payload)}.json"
        if path.name != expected_filename:
            raise TushareFetchError(f"response-boundary receipt filename hash mismatch: {path.name}")
        source_row_hash = payload.get("source_row_hash")
        if not isinstance(source_row_hash, str) or _HEX64_RE.fullmatch(source_row_hash) is None:
            raise TushareFetchError(f"invalid response-boundary receipt source_row_hash: {path.name}")
        identity = _response_boundary_receipt_identity(payload)
        prior_hash = seen_identity.get(identity)
        if prior_hash is not None and prior_hash != payload["source_row_hash"]:
            raise TushareFetchError(f"response-boundary receipt identity conflict: {path.name}")
        seen_identity[identity] = str(payload["source_row_hash"])
        receipts.append(payload)
    by_endpoint = {endpoint: 0 for endpoint in SOURCE_ENDPOINTS}
    by_reason: dict[str, int] = {}
    null_end_type_by_endpoint = {endpoint: 0 for endpoint in SOURCE_ENDPOINTS}
    for item in receipts:
        endpoint = str(item["endpoint"])
        if endpoint not in by_endpoint:
            raise TushareFetchError(f"invalid response-boundary receipt endpoint: {endpoint}")
        by_endpoint[endpoint] += 1
        if item.get("end_type") is None:
            null_end_type_by_endpoint[endpoint] += 1
        reason_code = str(item["reason_code"])
        by_reason[reason_code] = by_reason.get(reason_code, 0) + 1
    return {
        "count": len(receipts),
        "by_endpoint": by_endpoint,
        "reason_code_distribution": dict(sorted(by_reason.items())),
        "null_end_type_receipt_count_by_endpoint": null_end_type_by_endpoint,
    }


def _optional_ymd_text(
    value: object,
    field_name: str,
    endpoint: str,
    symbol: str,
    line: int,
    *,
    reject_2025: bool = True,
) -> tuple[str | None, date | None]:
    text = _coerce_text(value)
    if text is None:
        return None, None
    parsed = _parse_ymd_strict(text, field_name, endpoint, symbol, line, allow_hyphen=True)
    if reject_2025 and parsed.year >= 2025:
        raise DataQualityError(f"{endpoint} {field_name} is 2025+ for {symbol} at source row {line}")
    return ymd(parsed), parsed


def _required_ymd_text(
    value: object,
    field_name: str,
    endpoint: str,
    symbol: str,
    line: int,
) -> tuple[str, date]:
    text = _coerce_text(value)
    if text is None:
        raise DataQualityError(f"{endpoint} {field_name} is missing for {symbol} at source row {line}")
    parsed = _parse_ymd_strict(text, field_name, endpoint, symbol, line, allow_hyphen=True)
    if parsed.year >= 2025:
        raise DataQualityError(f"{endpoint} {field_name} is 2025+ for {symbol} at source row {line}")
    return ymd(parsed), parsed


def _optional_stored_ymd_text(
    value: object,
    field_name: str,
    endpoint: str,
    symbol: str,
    line: int,
) -> tuple[str | None, date | None]:
    text = _coerce_text(value)
    if text is None:
        return None, None
    parsed = _parse_ymd_strict(text, field_name, endpoint, symbol, line, allow_hyphen=False)
    if parsed.year >= 2025:
        raise DataQualityError(f"existing {endpoint} {field_name} is 2025+ for {symbol}")
    return ymd(parsed), parsed


def _required_stored_ymd_text(
    value: object,
    field_name: str,
    endpoint: str,
    symbol: str,
    line: int,
) -> tuple[str, date]:
    text = _coerce_text(value)
    if text is None:
        raise DataQualityError(f"existing {endpoint} {field_name} is missing for {symbol}")
    parsed = _parse_ymd_strict(text, field_name, endpoint, symbol, line, allow_hyphen=False)
    if parsed.year >= 2025:
        raise DataQualityError(f"existing {endpoint} {field_name} is 2025+ for {symbol}")
    return ymd(parsed), parsed


def _parse_ymd_strict(
    value: object,
    field_name: str,
    endpoint: str,
    symbol: str,
    line: int,
    *,
    allow_hyphen: bool,
) -> date:
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        text = str(value).strip()
        if allow_hyphen:
            text = text.replace("-", "")
        if not _DATE_RE.fullmatch(text):
            raise DataQualityError(f"{endpoint} {field_name} is invalid for {symbol} at source row {line}")
        try:
            parsed = date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError as exc:
            raise DataQualityError(f"{endpoint} {field_name} is invalid for {symbol} at source row {line}") from exc
    return parsed


def _coerce_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def _coerce_float(
    value: object,
    endpoint: str,
    symbol: str,
    line: int,
    field_name: str,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise DataQualityError(f"{endpoint} field {field_name} must not be bool for {symbol} at source row {line}")
    if isinstance(value, int | float):
        number = float(value)
    else:
        text = str(value).strip()
        if not text or text.lower() in {"nan", "none", "null"}:
            return None
        try:
            number = float(text)
        except ValueError as exc:
            raise DataQualityError(
                f"{endpoint} field {field_name} is not numeric for {symbol} at source row {line}"
            ) from exc
    if not math.isfinite(number):
        raise DataQualityError(f"{endpoint} field {field_name} is not finite for {symbol} at source row {line}")
    return number


def _coerce_int(
    value: object,
    endpoint: str,
    symbol: str,
    line: int,
    field_name: str,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise DataQualityError(f"{endpoint} field {field_name} must not be bool for {symbol} at source row {line}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise DataQualityError(f"{endpoint} field {field_name} is not an integer for {symbol} at source row {line}")
        return int(value)
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise DataQualityError(
            f"{endpoint} field {field_name} is not an integer for {symbol} at source row {line}"
        ) from exc
    if not math.isfinite(number) or not number.is_integer():
        raise DataQualityError(f"{endpoint} field {field_name} is not an integer for {symbol} at source row {line}")
    return int(number)


def _endpoint_partition_stats(
    *,
    root: Path,
    endpoint: str,
    symbols: list[str],
) -> dict[str, int]:
    paths = sorted((root / "partitions" / endpoint).glob("*.parquet"))
    if len(paths) != len(symbols):
        raise TushareFetchError(f"{endpoint} partition set is incomplete")
    expected_names = [f"{symbol.replace('.', '_')}.parquet" for symbol in symbols]
    if [path.name for path in paths] != expected_names:
        raise TushareFetchError(f"{endpoint} partition filenames are noncanonical")
    rows = 0
    nonempty = 0
    for path in paths:
        frame = pl.read_parquet(path)
        rows += frame.height
        if frame.height > 0:
            nonempty += 1
    return {
        "partitions": len(paths),
        "nonempty_partitions": nonempty,
        "empty_partitions": len(paths) - nonempty,
        "rows": rows,
    }


def _recompute_quality_counts(
    *,
    root: Path,
    symbols: list[str],
    endpoint_fields: dict[str, tuple[str, ...]],
) -> dict[str, dict[str, int]]:
    quality_counts: dict[str, dict[str, int]] = {}
    expected_names = [f"{symbol.replace('.', '_')}.parquet" for symbol in symbols]
    for endpoint in SOURCE_ENDPOINTS:
        paths = sorted((root / "partitions" / endpoint).glob("*.parquet"))
        if [path.name for path in paths] != expected_names:
            raise TushareFetchError(f"{endpoint} partition set is incomplete or contains extras")
        rows = 0
        nonempty_partitions = 0
        empty_partitions = 0
        missing_ann_date_rows = 0
        for path in paths:
            frame = pl.read_parquet(path)
            _validate_partition_schema(
                frame=frame,
                endpoint=endpoint,
                symbol=path.stem.replace("_", ".", 1),
                fields=endpoint_fields[endpoint],
            )
            rows += frame.height
            missing_ann_date_rows += _count_missing_ann_rows(frame)
            if frame.height == 0:
                empty_partitions += 1
            else:
                nonempty_partitions += 1
        quality_counts[endpoint] = {
            "rows": rows,
            "nonempty_partitions": nonempty_partitions,
            "empty_partitions": empty_partitions,
            "missing_ann_date_rows": missing_ann_date_rows,
            "conflict_rows": 0,
        }
    return quality_counts


def _validate_manifest_keys(payload: dict[str, Any], expected: frozenset[str], name: str) -> None:
    keys = set(payload)
    if keys != expected:
        raise TushareFetchError(f"{name} keys are invalid")


def _validate_collected_at(value: object, label: str) -> str:
    text = str(value or "")
    if not _UTC_TEXT_RE.fullmatch(text):
        raise TushareFetchError(f"financial-negative-list {label} is invalid")
    return text


def _utc_now_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _collection_id(manifest: dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "collection_id"}
    return _json_sha256(payload)


def _resolve_path(root: Path, path: Path) -> Path:
    if path.is_absolute():
        return path
    return root / path


def _prepare_staging_root(repo_root: Path, staging_dir: Path, *, create: bool) -> Path:
    repo_root_abs = repo_root.resolve(strict=True)
    staging_abs = staging_dir if staging_dir.is_absolute() else repo_root_abs / staging_dir
    try:
        relative = staging_abs.relative_to(repo_root_abs)
    except ValueError as exc:
        raise TushareFetchError("staging directory must stay within repo_root") from exc
    if any(part in {".", ".."} for part in relative.parts):
        raise TushareFetchError("staging directory must stay within repo_root")
    cursor = repo_root_abs
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise TushareFetchError("staging directory path must not contain symlink")
    if create:
        staging_abs.mkdir(parents=True, exist_ok=True)
    if staging_abs.exists() and not staging_abs.is_dir():
        raise TushareFetchError("staging path must be a directory")
    _assert_resolved_within(repo_root_abs, staging_abs.resolve(strict=False), "staging directory")
    return staging_abs


def _assert_safe_staging_path(
    *,
    root: Path,
    path: Path,
    label: str,
    expect_file: bool = False,
    expect_dir: bool = False,
) -> None:
    if expect_file and expect_dir:
        raise ValueError("expect_file and expect_dir are mutually exclusive")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise TushareFetchError(f"{label} escapes staging directory") from exc
    if any(part in {".", ".."} for part in relative.parts):
        raise TushareFetchError(f"{label} escapes staging directory")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise TushareFetchError(f"{label} must not be a symlink path")
    root_abs = root.resolve(strict=True)
    _assert_resolved_within(root_abs, path.resolve(strict=False), label)
    if not path.exists():
        raise TushareFetchError(f"missing {label}")
    if expect_file and not path.is_file():
        raise TushareFetchError(f"invalid {label}")
    if expect_dir and not path.is_dir():
        raise TushareFetchError(f"invalid {label}")
    _assert_resolved_within(root_abs, path.resolve(strict=True), label)


def _assert_safe_staging_write_path(*, root: Path, path: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise TushareFetchError(f"{label} escapes staging directory") from exc
    if any(part in {".", ".."} for part in relative.parts):
        raise TushareFetchError(f"{label} escapes staging directory")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise TushareFetchError(f"{label} must not be a symlink path")
    root_abs = root.resolve(strict=True)
    _assert_resolved_within(root_abs, path.resolve(strict=False), label)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists():
            _assert_resolved_within(root_abs, cursor.resolve(strict=True), label)


def _assert_resolved_within(root: Path, candidate: Path, label: str) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise TushareFetchError(f"{label} escapes staging directory") from exc


def _dataset_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for endpoint in SOURCE_ENDPOINTS:
        paths = sorted((root / "partitions" / endpoint).glob("*.parquet"))
        out[endpoint] = _dataset_hash_for_paths(root=root, paths=paths)
    return out


def _dataset_hash_for_paths(*, root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _symbols_sha256(symbols: list[str]) -> str:
    return hashlib.sha256(("\n".join(symbols) + "\n").encode("utf-8")).hexdigest()


def _write_parquet_atomic(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.write_parquet(temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path, name: str) -> dict[str, Any]:
    if not path.is_file():
        raise TushareFetchError(f"missing {name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TushareFetchError(f"invalid {name}") from exc
    if not isinstance(value, dict):
        raise TushareFetchError(f"invalid {name}")
    return value


def _json_sha256(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_authorization_id(raw: str) -> str:
    text = str(raw).strip()
    if _HEX64_RE.fullmatch(text) is None:
        raise TushareFetchError("collection_authorization_id must be 64-char lowercase hex")
    return text


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
