from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import polars as pl

from app.errors import DataQualityError, TushareFetchError
from app.models.config import StrategyConfig
from app.models.ownership import OwnershipSnapshot
from app.providers.tushare_client import TushareQueryClient
from app.providers.tushare_normalize import require_ts_code, ymd
from app.providers.tushare_ownership import (
    OWNERSHIP_AVAILABILITY_POLICY,
    OWNERSHIP_FIELDS,
    normalize_top10_float_holders,
    validate_top10_float_holders,
)
from app.storage.fundamental_io import load_verified_fundamental_snapshot
from app.storage.ownership_io import (
    build_ownership_snapshot,
    write_ownership_snapshot_atomically,
)
from app.storage.snapshot_io import load_verified_snapshot

_SCHEMA_VERSION = "2"
_REQUEST_INTERVAL_SECONDS = 0.31
_MAX_ROWS_PER_SYMBOL = 1000
_MAX_QUERY_ATTEMPTS = 5
_QUERY_RETRY_BASE_SECONDS = 1.0


@dataclass(frozen=True)
class OwnershipHistoryCollectionResult:
    staging_dir: Path
    request_id: str
    base_market_snapshot_id: str
    fundamental_snapshot_id: str
    requested_stocks: int
    completed_partitions: int
    reused_partitions: int
    collection_manifest_path: Path
    quality_report_path: Path


@dataclass(frozen=True)
class OwnershipHistoryMaterializeResult:
    snapshot: OwnershipSnapshot
    requested_stocks: int
    covered_symbols: int
    rows: int
    complete_groups: int


def collect_tushare_all_a_share_ownership(
    *,
    client: TushareQueryClient,
    market_dir: Path,
    fundamental_dir: Path,
    config: StrategyConfig,
    start: date,
    end: date,
    staging_dir: Path,
    progress: Callable[[int, int, bool], None] | None = None,
) -> OwnershipHistoryCollectionResult:
    """Collect resumable top-ten-float-holder partitions without inferring gaps."""
    _require_config(config)
    if end < start:
        raise TushareFetchError("end date must be on or after start date")
    market = load_verified_snapshot(Path(market_dir))
    fundamental, _ = load_verified_fundamental_snapshot(Path(fundamental_dir))
    if fundamental.base_market_snapshot_id != market.snapshot_id:
        raise TushareFetchError(
            "fundamental overlay is not bound to the exact market snapshot"
        )
    if (
        market.coverage_start is None
        or market.coverage_end is None
        or start < market.coverage_start
        or end > market.coverage_end
    ):
        raise TushareFetchError(
            "ownership request is outside the verified market snapshot coverage"
        )
    ownership = config.ownership
    if ownership is None:
        raise TushareFetchError("ownership strategy has no ownership contract")
    query_start = start - timedelta(days=ownership.max_report_age_days)
    stocks = _market_stocks(Path(market_dir))
    request_payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "query_start": query_start.isoformat(),
        "strategy_config_hash": config.config_hash(),
        "ownership_contract": ownership.model_dump(mode="json"),
        "base_market_snapshot_id": market.snapshot_id,
        "fundamental_snapshot_id": fundamental.snapshot_id,
        "symbols_sha256": _symbols_sha256(stocks),
        "requested_stocks": len(stocks),
        "fields": OWNERSHIP_FIELDS,
        "availability_policy": OWNERSHIP_AVAILABILITY_POLICY,
    }
    request_id = _json_sha256(request_payload)
    root = Path(staging_dir)
    root.mkdir(parents=True, exist_ok=True)
    request_path = root / "collection_request.json"
    expected_request = {**request_payload, "request_id": request_id}
    if request_path.exists():
        if _read_json(request_path, "collection_request.json") != expected_request:
            raise TushareFetchError(
                "staging directory belongs to a different ownership request; "
                "use a new --staging-dir"
            )
    else:
        _write_json_atomic(request_path, expected_request)
    if (root / "collection_manifest.json").exists():
        _verify_collection_manifest(root, request_id=request_id)

    next_request_at = 0.0
    completed = 0
    reused = 0
    for done, symbol in enumerate(stocks, start=1):
        path = root / "partitions" / f"{symbol.replace('.', '_')}.parquet"
        if path.exists():
            _validate_partition(pl.read_parquet(path), symbol, query_start, end)
            reused += 1
            was_reused = True
        else:
            if bool(getattr(client, "requires_single_code_rate_limit", False)):
                now = monotonic()
                if next_request_at > now:
                    sleep(next_request_at - now)
                next_request_at = monotonic() + _REQUEST_INTERVAL_SECONDS
            raw = _query_with_retry(
                client,
                "top10_floatholders",
                {
                    "ts_code": symbol,
                    "start_date": ymd(query_start),
                    "end_date": ymd(end),
                    "fields": OWNERSHIP_FIELDS,
                },
            )
            if raw.height >= _MAX_ROWS_PER_SYMBOL:
                raise DataQualityError(
                    f"top10_floatholders returned {raw.height} rows for {symbol}; "
                    "the response may be truncated"
                )
            frame = (
                normalize_top10_float_holders(raw)
                if not raw.is_empty()
                else _empty_ownership()
            )
            if not frame.is_empty():
                frame = frame.filter(
                    (pl.col("report_period") >= query_start)
                    & (pl.col("report_period") <= end)
                    & (pl.col("ann_date") <= end)
                )
            _validate_partition(frame, symbol, query_start, end)
            _write_parquet_atomic(path, frame)
            completed += 1
            was_reused = False
        if progress is not None:
            progress(done, len(stocks), was_reused)

    quality = _build_quality_report(root, stocks)
    quality_path = root / "quality_report.json"
    _write_json_atomic(quality_path, quality)
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "request_id": request_id,
        "source_name": "tushare_top10_floatholders",
        "collected_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_market_snapshot_id": market.snapshot_id,
        "fundamental_snapshot_id": fundamental.snapshot_id,
        "dataset_sha256": _dataset_sha256(root),
        "quality_report_sha256": _sha256_file(quality_path),
        "normalization": "each per-symbol partition normalized before atomic persistence",
    }
    manifest_path = root / "collection_manifest.json"
    _write_json_atomic(manifest_path, manifest)
    return OwnershipHistoryCollectionResult(
        staging_dir=root,
        request_id=request_id,
        base_market_snapshot_id=market.snapshot_id,
        fundamental_snapshot_id=fundamental.snapshot_id,
        requested_stocks=len(stocks),
        completed_partitions=completed,
        reused_partitions=reused,
        collection_manifest_path=manifest_path,
        quality_report_path=quality_path,
    )


def materialize_tushare_all_a_share_ownership(
    *,
    staging_dir: Path,
    market_dir: Path,
    fundamental_dir: Path,
    config: StrategyConfig,
    dest_dir: Path,
    source_version: str | None = None,
    replace_existing: bool = False,
) -> OwnershipHistoryMaterializeResult:
    """Verify every staged partition and atomically build the bound overlay."""
    _require_config(config)
    root = Path(staging_dir)
    request = _read_json(root / "collection_request.json", "collection_request.json")
    request_id = str(request.get("request_id") or "")
    if not request_id or request.get("strategy_config_hash") != config.config_hash():
        raise TushareFetchError("ownership collection strategy contract does not match")
    _verify_collection_manifest(root, request_id=request_id)
    market = load_verified_snapshot(Path(market_dir))
    fundamental, _ = load_verified_fundamental_snapshot(Path(fundamental_dir))
    if request.get("base_market_snapshot_id") != market.snapshot_id:
        raise TushareFetchError("ownership collection belongs to another market snapshot")
    if request.get("fundamental_snapshot_id") != fundamental.snapshot_id:
        raise TushareFetchError(
            "ownership collection belongs to another fundamental snapshot"
        )
    if fundamental.base_market_snapshot_id != market.snapshot_id:
        raise TushareFetchError(
            "fundamental overlay is not bound to the exact market snapshot"
        )
    stocks = _market_stocks(Path(market_dir))
    paths = sorted((root / "partitions").glob("*.parquet"))
    expected = {f"{symbol.replace('.', '_')}.parquet" for symbol in stocks}
    if {path.name for path in paths} != expected:
        raise TushareFetchError(
            "ownership partition set is incomplete or contains extras"
        )
    frames = [pl.read_parquet(path) for path in paths]
    table = pl.concat(frames, how="vertical_relaxed")
    if table.is_empty():
        raise DataQualityError("ownership collection contains no disclosed holder rows")
    validate_top10_float_holders(table)
    quality = _build_quality_report(root, stocks)
    complete_groups = int(quality["complete_groups"])
    if complete_groups <= 0:
        raise DataQualityError("ownership collection has no complete top-ten groups")
    snapshot = build_ownership_snapshot(
        table,
        source_name="tushare_top10_floatholders",
        source_version=source_version or request_id,
        base_market_snapshot_id=market.snapshot_id,
        fundamental_snapshot_id=fundamental.snapshot_id,
    )
    write_ownership_snapshot_atomically(
        Path(dest_dir), table, snapshot, replace_existing=replace_existing
    )
    return OwnershipHistoryMaterializeResult(
        snapshot=snapshot,
        requested_stocks=len(stocks),
        covered_symbols=int(table["symbol"].n_unique()),
        rows=table.height,
        complete_groups=complete_groups,
    )


def _require_config(config: StrategyConfig) -> None:
    if config.research_scope != "historical_all_a_share":
        raise TushareFetchError("ownership history requires historical_all_a_share")
    if config.ownership is None:
        raise TushareFetchError("ownership history requires an ownership contract")
    if config.fundamental is None or not config.fundamental.required:
        raise TushareFetchError("ownership history requires the bound fundamental overlay")


def _query_with_retry(
    client: TushareQueryClient,
    api_name: str,
    params: dict[str, Any],
) -> pl.DataFrame:
    """Retry transient provider failures without skipping or fabricating a partition."""
    for attempt in range(1, _MAX_QUERY_ATTEMPTS + 1):
        try:
            return client.query(api_name, **params)
        except TushareFetchError:
            if attempt == _MAX_QUERY_ATTEMPTS:
                raise
            sleep(_QUERY_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    raise AssertionError("ownership query retry loop exhausted unexpectedly")


def _market_stocks(market_dir: Path) -> list[str]:
    frame = pl.read_parquet(market_dir / "instruments.parquet")
    required = {"symbol", "is_index", "is_global"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataQualityError(f"market instruments missing columns: {missing}")
    values = frame.filter(~pl.col("is_index") & ~pl.col("is_global"))["symbol"].to_list()
    stocks = sorted({require_ts_code(str(value), kind="stock") for value in values})
    if not stocks:
        raise DataQualityError("verified market snapshot has no stock instruments")
    return stocks


def _validate_partition(
    frame: pl.DataFrame, symbol: str, query_start: date, end: date
) -> None:
    if frame.is_empty():
        if set(frame.columns) != set(_empty_ownership().columns):
            raise DataQualityError(f"empty ownership partition has invalid schema: {symbol}")
        return
    validate_top10_float_holders(frame)
    if set(frame["symbol"].to_list()) != {symbol}:
        raise DataQualityError(f"ownership partition contains another symbol: {symbol}")
    announcements = frame["ann_date"].to_list()
    periods = frame["report_period"].to_list()
    if any(
        not isinstance(value, date) or value < query_start or value > end
        for value in periods
    ):
        raise DataQualityError(f"ownership partition is outside request bounds: {symbol}")
    if any(not isinstance(value, date) or value > end for value in announcements):
        raise DataQualityError(
            f"ownership partition contains an announcement after request end: {symbol}"
        )


def _build_quality_report(root: Path, stocks: list[str]) -> dict[str, Any]:
    paths = sorted((root / "partitions").glob("*.parquet"))
    expected = {f"{symbol.replace('.', '_')}.parquet" for symbol in stocks}
    if {path.name for path in paths} != expected:
        raise TushareFetchError(
            "ownership partition set is incomplete or contains extras"
        )
    table = pl.concat([pl.read_parquet(path) for path in paths], how="vertical_relaxed")
    complete_groups = 0
    incomplete_groups = 0
    if not table.is_empty():
        grouped = table.group_by(["symbol", "report_period", "ann_date"]).agg(
            pl.len().alias("rows"),
            pl.col("holder_name").n_unique().alias("holders"),
            pl.col("hold_float_ratio").sum().alias("ratio_sum"),
            pl.col("hold_float_ratio").is_null().any().alias("ratio_unknown"),
            (
                (pl.col("hold_float_ratio") < 0)
                | (pl.col("hold_float_ratio") > 100)
            )
            .any()
            .alias("ratio_invalid"),
            (
                pl.col("holder_type").is_null()
                | (pl.col("holder_type").str.strip_chars() == "")
            )
            .any()
            .alias("type_unknown"),
        )
        complete_groups = grouped.filter(
            (pl.col("holders") == 10)
            & (pl.col("rows") == 10)
            & (pl.col("ratio_sum") <= 100.000001)
            & ~pl.col("ratio_unknown")
            & ~pl.col("ratio_invalid")
            & ~pl.col("type_unknown")
        ).height
        incomplete_groups = grouped.height - complete_groups
    return {
        "schema_version": _SCHEMA_VERSION,
        "complete": True,
        "requested_stocks": len(stocks),
        "partitions": len(paths),
        "rows": table.height,
        "covered_symbols": int(table["symbol"].n_unique()) if not table.is_empty() else 0,
        "complete_groups": complete_groups,
        "incomplete_groups": incomplete_groups,
    }


def _empty_ownership() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "report_period": pl.Date,
            "ann_date": pl.Date,
            "available_at": pl.Datetime("us"),
            "holder_name": pl.String,
            "holder_type": pl.String,
            "hold_amount": pl.Float64,
            "hold_ratio": pl.Float64,
            "hold_float_ratio": pl.Float64,
            "hold_change": pl.Float64,
            "source_row_hash": pl.String,
        }
    )


def _verify_collection_manifest(root: Path, *, request_id: str) -> None:
    manifest = _read_json(root / "collection_manifest.json", "collection_manifest.json")
    if manifest.get("request_id") != request_id:
        raise TushareFetchError("ownership collection manifest request ID does not match")
    if manifest.get("dataset_sha256") != _dataset_sha256(root):
        raise TushareFetchError(
            "ownership collection manifest hash does not match staged parquet bytes"
        )
    quality_path = root / "quality_report.json"
    if manifest.get("quality_report_sha256") != _sha256_file(quality_path):
        raise TushareFetchError("ownership quality report hash does not match")


def _dataset_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / "partitions").glob("*.parquet")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _symbols_sha256(stocks: list[str]) -> str:
    return hashlib.sha256(("\n".join(stocks) + "\n").encode("utf-8")).hexdigest()


def _json_sha256(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
