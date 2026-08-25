from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median
from time import monotonic, sleep
from typing import Any

import polars as pl

from app.errors import DataQualityError, TushareFetchError
from app.models.events import EVENT_SOURCE_NAMES, EventSourceManifest
from app.providers.tushare_client import TushareQueryClient
from app.providers.tushare_events import (
    EVENT_AVAILABILITY_POLICY,
    EXPRESS_NUMERIC,
    FORECAST_NUMERIC,
    SOURCE_TO_TABLE,
    normalize_audit_opinion,
    normalize_earnings_express,
    normalize_earnings_forecast,
    normalize_event_sources,
    normalize_holder_count,
    normalize_share_unlock,
)
from app.providers.tushare_normalize import require_ts_code, ymd
from app.storage.snapshot_io import load_verified_snapshot

_SCHEMA_VERSION = "1"
_REQUEST_INTERVAL_SECONDS = 0.31
_SHARE_FLOAT_MAX_PAGES = 100
_FORECAST_FIRST_ANN_AFTER_ANN_RULE = "first_ann_date_after_ann_date"
_FORECAST_FIRST_ANN_ANOMALY_POLICY = (
    "Provider first_ann_date after authoritative ann_date is quarantined to null with a "
    "content-addressed anomaly artifact; the row is kept, ann_date/availability are never "
    "changed, and no replacement first_ann_date is invented"
)

SOURCE_FIELDS: dict[str, tuple[str, ...]] = {
    "forecast": (
        "ts_code",
        "ann_date",
        "end_date",
        "type",
        *FORECAST_NUMERIC,
        "first_ann_date",
        "summary",
        "change_reason",
    ),
    "express": (
        "ts_code",
        "ann_date",
        "end_date",
        *EXPRESS_NUMERIC,
        "perf_summary",
    ),
    "stk_holdernumber": ("ts_code", "ann_date", "end_date", "holder_num"),
    "share_float": (
        "ts_code",
        "ann_date",
        "float_date",
        "float_share",
        "float_ratio",
        "holder_name",
        "share_type",
    ),
    "fina_audit": (
        "ts_code",
        "ann_date",
        "end_date",
        "audit_result",
        "audit_fees",
        "audit_agency",
        "audit_sign",
    ),
}

SOURCE_DOCUMENTS = {
    "forecast": "https://tushare.pro/document/2?doc_id=45 ann_date",
    "express": "https://tushare.pro/document/2?doc_id=46 ann_date",
    "stk_holdernumber": "https://tushare.pro/document/2?doc_id=166 ann_date",
    "share_float": "https://tushare.pro/document/2?doc_id=160 ann_date",
    "fina_audit": "https://tushare.pro/document/2?doc_id=80 ann_date",
}

_DOCUMENTED_ROW_LIMITS = {
    "forecast": 3500,
    "stk_holdernumber": 3000,
    "share_float": 6000,
}

_NORMALIZERS: dict[str, Callable[[pl.DataFrame], pl.DataFrame]] = {
    "forecast": normalize_earnings_forecast,
    "express": normalize_earnings_express,
    "stk_holdernumber": normalize_holder_count,
    "share_float": normalize_share_unlock,
    "fina_audit": normalize_audit_opinion,
}

_RAW_SCHEMAS: dict[str, dict[str, Any]] = {
    "forecast": {
        **{name: pl.String for name in SOURCE_FIELDS["forecast"]},
        **{name: pl.Float64 for name in FORECAST_NUMERIC},
    },
    "express": {
        **{name: pl.String for name in SOURCE_FIELDS["express"]},
        **{name: pl.Float64 for name in EXPRESS_NUMERIC},
    },
    "stk_holdernumber": {
        "ts_code": pl.String,
        "ann_date": pl.String,
        "end_date": pl.String,
        "holder_num": pl.Int64,
    },
    "share_float": {
        "ts_code": pl.String,
        "ann_date": pl.String,
        "float_date": pl.String,
        "float_share": pl.Float64,
        "float_ratio": pl.Float64,
        "holder_name": pl.String,
        "share_type": pl.String,
    },
    "fina_audit": {
        "ts_code": pl.String,
        "ann_date": pl.String,
        "end_date": pl.String,
        "audit_result": pl.String,
        "audit_fees": pl.Float64,
        "audit_agency": pl.String,
        "audit_sign": pl.String,
    },
}


@dataclass(frozen=True)
class EventCollectionResult:
    staging_dir: Path
    request_id: str
    base_market_snapshot_id: str
    coverage_start: date
    coverage_end: date
    requested_stocks: int
    completed_partitions: int
    reused_partitions: int
    source_manifest_path: Path
    collection_manifest_path: Path
    quality_report_path: Path


_SharedAllMarketCache = dict[date, tuple[pl.DataFrame, int, list[int]]]


class _EndpointPacer:
    def __init__(self, client: TushareQueryClient) -> None:
        self._enabled = bool(getattr(client, "requires_single_code_rate_limit", False))
        self._next_at: dict[str, float] = {}

    def wait(self, api_name: str) -> None:
        if not self._enabled:
            return
        ready = self._next_at.get(api_name)
        now = monotonic()
        if ready is not None and ready > now:
            sleep(ready - now)
        self._next_at[api_name] = monotonic() + _REQUEST_INTERVAL_SECONDS


def collect_tushare_a_share_events(
    *,
    client: TushareQueryClient,
    market_dir: Path,
    start: date,
    end: date,
    staging_dir: Path,
    source_version: str | None = None,
    progress: Callable[[str, int, int, bool], None] | None = None,
    fallback_progress: Callable[[str, int, int, date], None] | None = None,
) -> EventCollectionResult:
    """Collect five Tushare event endpoints into a resumable, audited source pack."""
    if end < start:
        raise TushareFetchError("end date must be on or after start date")
    market_snapshot = load_verified_snapshot(Path(market_dir))
    if market_snapshot.coverage_end is None or end > market_snapshot.coverage_end:
        raise TushareFetchError("event coverage end exceeds the verified market snapshot")
    stocks = _market_stocks(Path(market_dir))
    listing_dates = _market_stock_listing_dates(Path(market_dir), stocks)
    root = Path(staging_dir)
    root.mkdir(parents=True, exist_ok=True)
    request_payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "coverage_start": start.isoformat(),
        "coverage_end": end.isoformat(),
        "base_market_snapshot_id": market_snapshot.snapshot_id,
        "symbols_sha256": _symbols_sha256(stocks),
        "requested_stocks": len(stocks),
        "source_version": source_version,
        "source_fields": {name: list(SOURCE_FIELDS[name]) for name in EVENT_SOURCE_NAMES},
        "availability_policy": EVENT_AVAILABILITY_POLICY,
        "share_float_query_policy": (
            "query full per-symbol history because start_date/end_date filter float_date; "
            "retain only rows whose ann_date is inside the requested coverage"
        ),
    }
    # request_id hashes only the historical core fields so existing unfinished staging
    # directories remain resumable when additive anomaly-policy metadata is introduced.
    request_id = _json_sha256(request_payload)
    request_path = root / "collection_request.json"
    expected_request = {
        **request_payload,
        "request_id": request_id,
        "forecast_first_ann_anomaly_policy": _FORECAST_FIRST_ANN_ANOMALY_POLICY,
    }
    if request_path.exists():
        _verify_collection_request(request_path, expected_request)
        # Preserve the core request ID for an interrupted pre-policy epoch, but
        # make the active anomaly policy durable before a resumed partition can
        # be collected and sealed.
        existing_request = _read_json(request_path, "collection_request.json")
        if "forecast_first_ann_anomaly_policy" not in existing_request:
            _write_json_atomic(request_path, expected_request)
    else:
        _write_json_atomic(request_path, expected_request)

    manifest_path = root / "collection_manifest.json"
    if manifest_path.exists():
        _verify_collection_manifest(root, request_id=request_id)
        return EventCollectionResult(
            staging_dir=root,
            request_id=request_id,
            base_market_snapshot_id=market_snapshot.snapshot_id,
            coverage_start=start,
            coverage_end=end,
            requested_stocks=len(stocks),
            completed_partitions=0,
            reused_partitions=len(stocks) * len(EVENT_SOURCE_NAMES),
            source_manifest_path=root / "source_manifest.json",
            collection_manifest_path=manifest_path,
            quality_report_path=root / "quality_report.json",
        )

    pacer = _EndpointPacer(client)
    shared_all_market_cache: _SharedAllMarketCache = {}

    def record_forecast_anomaly(item: dict[str, Any]) -> None:
        _write_anomaly_artifact(root, item, request_id=request_id)

    total = len(stocks) * len(EVENT_SOURCE_NAMES)
    done = 0
    completed = 0
    reused = 0
    for source_name in EVENT_SOURCE_NAMES:
        for symbol in stocks:
            path = root / "partitions" / source_name / f"{symbol.replace('.', '_')}.parquet"
            if path.exists():
                frame = pl.read_parquet(path)
                canonical = _prepare_partition(
                    frame,
                    source_name,
                    symbol,
                    start,
                    end,
                    anomaly_recorder=(
                        record_forecast_anomaly if source_name == "forecast" else None
                    ),
                )
                if not frame.equals(canonical, null_equal=True):
                    if not _is_schema_only_partition_upgrade(
                        frame,
                        canonical,
                        source_name,
                    ):
                        raise DataQualityError(
                            f"existing {source_name} partition is not canonical for {symbol}"
                        )
                    _write_parquet_atomic(path, canonical)
                reused += 1
                was_reused = True
            elif listing_dates[symbol] > end:
                # The verified market snapshot proves this instrument did not
                # exist in the requested research window. An empty canonical
                # partition is more accurate and avoids treating a provider's
                # current-universe response as historical evidence.
                frame = _empty_source(source_name)
                _write_parquet_atomic(path, frame)
                completed += 1
                was_reused = False
            else:
                pacer.wait(source_name)
                raw, query_audit = _query_source(
                    client,
                    source_name,
                    symbol,
                    start,
                    end,
                    pacer=pacer,
                    fallback_progress=fallback_progress,
                    listing_date=listing_dates[symbol],
                    shared_all_market_cache=shared_all_market_cache,
                    query_cache_dir=(
                        root
                        / "query-cache"
                        / "share_float"
                        / symbol.replace(".", "_")
                    ),
                )
                frame = _prepare_partition(
                    raw,
                    source_name,
                    symbol,
                    start,
                    end,
                    anomaly_recorder=(
                        record_forecast_anomaly
                        if source_name == "forecast"
                        else None
                    ),
                )
                if query_audit is not None:
                    audit_path = (
                        root
                        / "query-audit"
                        / source_name
                        / f"{symbol.replace('.', '_')}.json"
                    )
                    _write_json_atomic(audit_path, query_audit)
                _write_parquet_atomic(path, frame)
                completed += 1
                was_reused = False
            done += 1
            if progress is not None:
                progress(source_name, done, total, was_reused)

    raw_sources = _aggregate_partitions(root, stocks)
    normalized = normalize_event_sources(raw_sources)
    export_paths = _write_exports(root, raw_sources)
    collected_at = datetime.now(UTC)
    source_manifest = EventSourceManifest.model_validate(
        {
            "schema_version": _SCHEMA_VERSION,
            "source_name": "tushare",
            "source_version": source_version or request_id,
            "fetched_at": collected_at.isoformat(),
            "coverage_start": start.isoformat(),
            "coverage_end": end.isoformat(),
            "files": {
                name: {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _sha256_file(path),
                }
                for name, path in export_paths.items()
            },
            "availability_evidence": SOURCE_DOCUMENTS,
            "notes": (
                "Collection time is provenance only and is never historical available_at. "
                "Each endpoint was queried per eligible stock; stocks listed after coverage_end are "
                "represented by empty partitions from the verified market snapshot. share_float normally "
                "uses full per-symbol history and ann_date filtering; capped or foreign-symbol "
                "responses use an audited "
                "ann_date-only all-market fallback with local symbol filtering and resumable "
                "offset pagination plus resumable per-day caches. The all-market response cache is "
                "shared by announcement date; pre-listing dates are skipped only from the verified "
                "market snapshot listing_date. "
                f"{_FORECAST_FIRST_ANN_ANOMALY_POLICY}."
            ),
        }
    )
    source_manifest_path = root / "source_manifest.json"
    _write_json_atomic(source_manifest_path, source_manifest.model_dump(mode="json"))
    quality = _build_quality_report(
        raw_sources,
        normalized,
        request_id=request_id,
        base_market_snapshot_id=market_snapshot.snapshot_id,
        stocks=stocks,
        start=start,
        end=end,
        query_audits=_read_query_audits(root),
        provider_field_anomalies=_provider_field_anomaly_report(root, request_id=request_id),
    )
    quality_path = root / "quality_report.json"
    _write_json_atomic(quality_path, quality)
    collection_manifest = {
        "schema_version": _SCHEMA_VERSION,
        "request_id": request_id,
        "source_name": "tushare_a_share_events",
        "source_version": source_version or request_id,
        "collected_at": collected_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_market_snapshot_id": market_snapshot.snapshot_id,
        "coverage": {"start": start.isoformat(), "end": end.isoformat()},
        "requested_stocks": len(stocks),
        "partition_count": total,
        "dataset_hashes": _dataset_hashes(root),
        "export_hashes": {name: _sha256_file(path) for name, path in export_paths.items()},
        "source_manifest_sha256": _sha256_file(source_manifest_path),
        "quality_report_sha256": _sha256_file(quality_path),
        "query_audit_hashes": _query_audit_hashes(root),
        "query_cache_hashes": _query_cache_hashes(root),
        "anomaly_hashes": _anomaly_hashes(root),
        "research_boundary": (
            "collection and quality diagnostics only; no event is enabled as a score, gate, or trade"
        ),
    }
    _write_json_atomic(manifest_path, collection_manifest)
    return EventCollectionResult(
        staging_dir=root,
        request_id=request_id,
        base_market_snapshot_id=market_snapshot.snapshot_id,
        coverage_start=start,
        coverage_end=end,
        requested_stocks=len(stocks),
        completed_partitions=completed,
        reused_partitions=reused,
        source_manifest_path=source_manifest_path,
        collection_manifest_path=manifest_path,
        quality_report_path=quality_path,
    )


def _query_source(
    client: TushareQueryClient,
    source_name: str,
    symbol: str,
    start: date,
    end: date,
    *,
    pacer: _EndpointPacer,
    fallback_progress: Callable[[str, int, int, date], None] | None,
    listing_date: date,
    shared_all_market_cache: _SharedAllMarketCache,
    query_cache_dir: Path,
) -> tuple[pl.DataFrame, dict[str, Any] | None]:
    params: dict[str, object] = {
        "ts_code": symbol,
        "fields": ",".join(SOURCE_FIELDS[source_name]),
    }
    if source_name != "share_float":
        params.update({"start_date": ymd(start), "end_date": ymd(end)})
    frame = client.query(source_name, **params)
    row_limit = _DOCUMENTED_ROW_LIMITS.get(source_name)
    if source_name == "share_float" and not frame.is_empty():
        if "ts_code" not in frame.columns:
            raise DataQualityError("share_float response is missing ts_code")
        observed = sorted({str(value) for value in frame["ts_code"].drop_nulls().to_list()})
        reasons: list[str] = []
        if row_limit is not None and frame.height >= row_limit:
            reasons.append("primary_response_reached_row_limit")
        if observed != [symbol]:
            reasons.append("primary_response_contains_foreign_symbols")
        if reasons:
            return _query_share_float_by_ann_date(
                client,
                symbol=symbol,
                start=start,
                end=end,
                pacer=pacer,
                primary=frame,
                reasons=reasons,
                progress=fallback_progress,
                cache_dir=query_cache_dir,
                listing_date=listing_date,
                shared_all_market_cache=shared_all_market_cache,
            )
    elif row_limit is not None and frame.height >= row_limit:
        raise DataQualityError(
            f"{source_name} returned {frame.height} rows for {symbol}; response may be truncated"
        )
    return frame, None


def _query_share_float_by_ann_date(
    client: TushareQueryClient,
    *,
    symbol: str,
    start: date,
    end: date,
    pacer: _EndpointPacer,
    primary: pl.DataFrame,
    reasons: list[str],
    progress: Callable[[str, int, int, date], None] | None,
    cache_dir: Path,
    listing_date: date,
    shared_all_market_cache: _SharedAllMarketCache,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    retained: list[pl.DataFrame] = []
    total_returned = 0
    current = start
    fields = ",".join(SOURCE_FIELDS["share_float"])
    row_limit = _DOCUMENTED_ROW_LIMITS["share_float"]
    total_days = (end - start).days + 1
    completed_days = 0
    pre_listing_days_skipped = 0
    all_market_cache_hits = 0
    all_market_network_queries = 0
    all_market_cache_dir = cache_dir.parent / "all-market"
    while current <= end:
        cache_path = cache_dir / f"{ymd(current)}.json"
        if cache_path.exists():
            cache = _read_share_float_day_cache(cache_path, symbol=symbol, ann_date=current)
            selected = _cached_share_float_frame(cache["rows"])
            response_rows = int(str(cache["response_rows"]))
            if cache.get("query_mode") == "pre_listing_skip":
                pre_listing_days_skipped += 1
        elif current < listing_date:
            selected = _empty_source("share_float")
            response_rows = 0
            pre_listing_days_skipped += 1
            _write_json_atomic(
                cache_path,
                {
                    "schema_version": _SCHEMA_VERSION,
                    "source_name": "share_float",
                    "symbol": symbol,
                    "ann_date": current.isoformat(),
                    "response_rows": 0,
                    "query_mode": "pre_listing_skip",
                    "listing_date": listing_date.isoformat(),
                    "rows": [],
                },
            )
        else:
            all_market_cache_path = all_market_cache_dir / f"{ymd(current)}.json"
            used_shared_all_market_cache = False
            cached_all_market = shared_all_market_cache.get(current)
            if cached_all_market is not None:
                all_market, response_rows, page_rows = cached_all_market
                all_market_cache_hits += 1
                used_shared_all_market_cache = True
            elif all_market_cache_path.exists():
                all_market, response_rows, page_rows = _read_share_float_all_market_day_cache(
                    all_market_cache_path,
                    ann_date=current,
                )
                shared_all_market_cache[current] = (all_market, response_rows, page_rows)
                all_market_cache_hits += 1
                used_shared_all_market_cache = True
            else:
                all_market, response_rows, page_rows = _query_share_float_ann_date_pages(
                    client,
                    symbol=symbol,
                    ann_date=current,
                    fields=fields,
                    row_limit=row_limit,
                    pacer=pacer,
                )
                _write_share_float_all_market_day_cache(
                    all_market_cache_path,
                    ann_date=current,
                    response_rows=response_rows,
                    page_rows=page_rows,
                    all_market=all_market,
                )
                shared_all_market_cache[current] = (all_market, response_rows, page_rows)
                all_market_network_queries += 1
            selected = _select_share_float_symbol(
                all_market,
                symbol=symbol,
                ann_date=current,
            )
            if not used_shared_all_market_cache:
                _write_json_atomic(
                    cache_path,
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "source_name": "share_float",
                        "symbol": symbol,
                        "ann_date": current.isoformat(),
                        "response_rows": response_rows,
                        "query_mode": "ann_date_offset_pages",
                        "page_rows": page_rows,
                        "rows": selected.to_dicts(),
                    },
                )
        total_returned += response_rows
        if not selected.is_empty():
            retained.append(selected)
        completed_days += 1
        if progress is not None:
            progress(symbol, completed_days, total_days, current)
        current += timedelta(days=1)
    result = (
        pl.concat(retained, how="diagonal_relaxed")
        if retained
        else _empty_source("share_float")
    )
    audit = {
        "schema_version": _SCHEMA_VERSION,
        "source_name": "share_float",
        "symbol": symbol,
        "coverage_start": start.isoformat(),
        "coverage_end": end.isoformat(),
        "reasons": reasons,
        "primary_response_rows": primary.height,
        "primary_unique_symbols": sorted(
            {str(value) for value in primary["ts_code"].drop_nulls().to_list()}
        ),
        "fallback_policy": (
            "ann_date-only all-market query for every calendar day with offset pagination, "
            "then local symbol filter; reject ignored offsets, repeated full pages, and "
            "safety-limit exhaustion; pre-listing dates are verified empty from the market snapshot"
        ),
        "announcement_dates_examined": total_days,
        "pre_listing_dates_skipped": pre_listing_days_skipped,
        "announcement_dates_queried": total_days - pre_listing_days_skipped,
        "all_market_cache_hits": all_market_cache_hits,
        "all_market_network_queries": all_market_network_queries,
        "local_day_cache_files": len(list(cache_dir.glob("*.json"))),
        "fallback_rows_before_symbol_filter": total_returned,
        "retained_target_rows": result.height,
    }
    return result, audit


def _query_share_float_ann_date_pages(
    client: TushareQueryClient,
    *,
    symbol: str,
    ann_date: date,
    fields: str,
    row_limit: int,
    pacer: _EndpointPacer,
) -> tuple[pl.DataFrame, int, list[int]]:
    """Fetch all rows for one announcement date and cache-safe pagination metadata."""
    pages: list[pl.DataFrame] = []
    page_rows: list[int] = []
    full_page_hashes: set[str] = set()
    offset = 0
    while True:
        pacer.wait("share_float")
        # Do not pass the problematic ts_code again. Tushare's documented
        # example queries share_float by ann_date alone. Offset pagination is
        # required because one announcement date can exceed the row limit.
        response = client.query(
            "share_float",
            ann_date=ymd(ann_date),
            limit=row_limit,
            offset=offset,
            fields=fields,
        )
        _validate_share_float_ann_date_response(
            response,
            ann_date,
            target_symbol=symbol,
        )
        count = response.height
        page_rows.append(count)
        if count == row_limit:
            digest = _frame_rows_sha256(response, SOURCE_FIELDS["share_float"])
            if digest in full_page_hashes:
                raise DataQualityError(
                    "share_float ann_date pagination repeated a full page on "
                    f"{ann_date.isoformat()} at offset={offset}; refusing an ignored offset"
                )
            full_page_hashes.add(digest)
        if not response.is_empty():
            pages.append(response.select(SOURCE_FIELDS["share_float"]))
        if count < row_limit:
            break
        if len(page_rows) >= _SHARE_FLOAT_MAX_PAGES:
            raise DataQualityError(
                "share_float ann_date pagination exceeded the safety page limit on "
                f"{ann_date.isoformat()}"
            )
        offset += row_limit
    combined = (
        pl.concat(pages, how="diagonal_relaxed")
        if pages
        else _empty_source("share_float")
    )
    return combined, sum(page_rows), page_rows


def _select_share_float_symbol(
    all_market: pl.DataFrame,
    *,
    symbol: str,
    ann_date: date,
) -> pl.DataFrame:
    selected = (
        all_market.filter(pl.col("ts_code").cast(pl.String) == symbol)
        .select(SOURCE_FIELDS["share_float"])
        if not all_market.is_empty()
        else _empty_source("share_float")
    )
    return _prepare_partition(selected, "share_float", symbol, ann_date, ann_date)


def _frame_rows_sha256(frame: pl.DataFrame, fields: tuple[str, ...]) -> str:
    missing = sorted(set(fields) - set(frame.columns))
    if missing:
        raise DataQualityError(f"share_float fallback is missing columns: {missing}")
    raw = json.dumps(
        frame.select(fields).to_dicts(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_share_float_ann_date_response(
    frame: pl.DataFrame,
    ann_date: date,
    *,
    target_symbol: str,
) -> None:
    if frame.is_empty() and not frame.columns:
        return
    missing = sorted(set(SOURCE_FIELDS["share_float"]) - set(frame.columns))
    if missing:
        raise DataQualityError(
            f"share_float ann_date-only fallback is missing columns: {missing}"
        )
    for line, item in enumerate(
        frame.select(["ts_code", "ann_date"]).iter_rows(named=True),
        start=1,
    ):
        observed_symbol = str(item.get("ts_code") or "").strip()
        if observed_symbol == target_symbol:
            require_ts_code(observed_symbol, kind="stock")
        observed = _parse_ymd(
            item.get("ann_date"),
            "ann_date",
            "share_float",
            "all-market",
            line,
        )
        if observed != ann_date:
            raise DataQualityError(
                "share_float ann_date-only fallback ignored the requested announcement date "
                f"{ann_date.isoformat()} at source row {line}"
            )


def _cached_share_float_frame(rows: object) -> pl.DataFrame:
    if not isinstance(rows, list):
        raise TushareFetchError("share_float query cache rows are invalid")
    if not rows:
        return _empty_source("share_float")
    if any(not isinstance(item, dict) for item in rows):
        raise TushareFetchError("share_float query cache rows are invalid")
    try:
        return pl.from_dicts(
            rows,
            schema=_RAW_SCHEMAS["share_float"],
        ).select(SOURCE_FIELDS["share_float"])
    except Exception as exc:
        raise TushareFetchError("share_float query cache rows are invalid") from exc


def _read_share_float_day_cache(
    path: Path,
    *,
    symbol: str,
    ann_date: date,
) -> dict[str, Any]:
    item = _read_json(path, path.as_posix())
    legacy_required = {
        "schema_version",
        "source_name",
        "symbol",
        "ann_date",
        "response_rows",
        "rows",
    }
    paged_required = legacy_required | {"query_mode", "page_rows"}
    pre_listing_required = legacy_required | {"query_mode", "listing_date"}
    if (
        set(item) not in (legacy_required, paged_required, pre_listing_required)
        or item.get("schema_version") != _SCHEMA_VERSION
        or item.get("source_name") != "share_float"
        or item.get("symbol") != symbol
        or item.get("ann_date") != ann_date.isoformat()
    ):
        raise TushareFetchError(f"share_float query cache identity is invalid: {path.name}")
    response_rows = item.get("response_rows")
    if (
        not isinstance(response_rows, int)
        or isinstance(response_rows, bool)
        or response_rows < 0
    ):
        raise TushareFetchError(f"share_float query cache row count is invalid: {path.name}")
    row_limit = _DOCUMENTED_ROW_LIMITS["share_float"]
    if set(item) == legacy_required:
        if response_rows >= row_limit:
            raise TushareFetchError(
                f"legacy share_float query cache row count is invalid: {path.name}"
            )
    elif set(item) == paged_required:
        page_rows = item.get("page_rows")
        if (
            item.get("query_mode") != "ann_date_offset_pages"
            or not isinstance(page_rows, list)
            or not page_rows
            or len(page_rows) > _SHARE_FLOAT_MAX_PAGES
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > row_limit
                for value in page_rows
            )
            or any(value != row_limit for value in page_rows[:-1])
            or page_rows[-1] >= row_limit
            or sum(page_rows) != response_rows
        ):
            raise TushareFetchError(
                f"share_float paged query cache metadata is invalid: {path.name}"
            )
    else:
        try:
            cached_listing_date = _parse_ymd(
                item.get("listing_date"),
                "listing_date",
                "share_float",
                symbol,
                1,
            )
        except DataQualityError as exc:
            raise TushareFetchError(
                f"share_float pre-listing query cache metadata is invalid: {path.name}"
            ) from exc
        if (
            item.get("query_mode") != "pre_listing_skip"
            or response_rows != 0
            or item.get("rows") != []
            or ann_date >= cached_listing_date
        ):
            raise TushareFetchError(
                f"share_float pre-listing query cache metadata is invalid: {path.name}"
            )
    frame = _cached_share_float_frame(item.get("rows"))
    canonical = _prepare_partition(frame, "share_float", symbol, ann_date, ann_date)
    if not frame.equals(canonical, null_equal=True) or frame.height > response_rows:
        raise TushareFetchError(f"share_float query cache rows are not canonical: {path.name}")
    return item


def _write_share_float_all_market_day_cache(
    path: Path,
    *,
    ann_date: date,
    response_rows: int,
    page_rows: list[int],
    all_market: pl.DataFrame,
) -> None:
    _write_json_atomic(
        path,
        {
            "schema_version": _SCHEMA_VERSION,
            "source_name": "share_float_all_market",
            "ann_date": ann_date.isoformat(),
            "response_rows": response_rows,
            "query_mode": "ann_date_offset_pages",
            "page_rows": page_rows,
            "rows": all_market.to_dicts(),
        },
    )


def _read_share_float_all_market_day_cache(
    path: Path,
    *,
    ann_date: date,
) -> tuple[pl.DataFrame, int, list[int]]:
    item = _read_json(path, path.as_posix())
    required = {
        "schema_version",
        "source_name",
        "ann_date",
        "response_rows",
        "query_mode",
        "page_rows",
        "rows",
    }
    if (
        set(item) != required
        or item.get("schema_version") != _SCHEMA_VERSION
        or item.get("source_name") != "share_float_all_market"
        or item.get("ann_date") != ann_date.isoformat()
        or item.get("query_mode") != "ann_date_offset_pages"
    ):
        raise TushareFetchError(
            f"share_float all-market query cache identity is invalid: {path.name}"
        )
    response_rows = item.get("response_rows")
    page_rows = item.get("page_rows")
    row_limit = _DOCUMENTED_ROW_LIMITS["share_float"]
    if (
        not isinstance(response_rows, int)
        or isinstance(response_rows, bool)
        or response_rows < 0
        or not isinstance(page_rows, list)
        or not page_rows
        or len(page_rows) > _SHARE_FLOAT_MAX_PAGES
        or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > row_limit
            for value in page_rows
        )
        or any(value != row_limit for value in page_rows[:-1])
        or page_rows[-1] >= row_limit
        or sum(page_rows) != response_rows
    ):
        raise TushareFetchError(
            f"share_float all-market query cache metadata is invalid: {path.name}"
        )
    frame = _cached_share_float_frame(item.get("rows"))
    if frame.height != response_rows:
        raise TushareFetchError(
            f"share_float all-market query cache row count is invalid: {path.name}"
        )
    try:
        _validate_share_float_ann_date_response(frame, ann_date, target_symbol="")
    except DataQualityError as exc:
        raise TushareFetchError(
            f"share_float all-market query cache rows are invalid: {path.name}"
        ) from exc
    return frame, response_rows, page_rows


def _prepare_partition(
    raw: pl.DataFrame,
    source_name: str,
    symbol: str,
    start: date,
    end: date,
    *,
    anomaly_recorder: Callable[[dict[str, Any]], None] | None = None,
) -> pl.DataFrame:
    expected = SOURCE_FIELDS[source_name]
    if raw.is_empty() and not raw.columns:
        return _empty_source(source_name)
    missing = sorted(set(expected) - set(raw.columns))
    if missing:
        raise DataQualityError(f"{source_name} partition missing columns: {missing}")
    frame = raw.select(expected)
    if source_name == "share_float":
        try:
            frame = frame.cast(pl.Schema(_RAW_SCHEMAS["share_float"]), strict=True)
        except Exception as exc:
            raise DataQualityError(
                f"share_float contains values incompatible with its declared schema for {symbol}"
            ) from exc
    if frame.is_empty():
        return _empty_source(source_name)
    keep: list[bool] = []
    for line, item in enumerate(frame.iter_rows(named=True), start=1):
        observed = require_ts_code(str(item.get("ts_code") or ""), kind="stock")
        if observed != symbol:
            raise DataQualityError(
                f"{source_name} returned another symbol for {symbol} at source row {line}"
            )
        ann_date = _partition_ann_date(
            item,
            source_name=source_name,
            symbol=symbol,
            line=line,
        )
        in_window = start <= ann_date <= end
        if source_name != "share_float" and not in_window:
            raise DataQualityError(
                f"{source_name} ignored the requested announcement-date window for {symbol}"
            )
        keep.append(in_window)
    if source_name == "share_float":
        frame = frame.filter(pl.Series("in_window", keep))
    if frame.is_empty():
        return _empty_source(source_name)
    if source_name == "forecast":
        frame, anomalies = _quarantine_forecast_first_ann_after_ann(frame, symbol)
        if anomaly_recorder is not None:
            for anomaly in anomalies:
                anomaly_recorder(anomaly)
    frame = frame.unique(maintain_order=False).sort(list(expected), nulls_last=True)
    _NORMALIZERS[source_name](frame)
    return frame


def _is_schema_only_partition_upgrade(
    frame: pl.DataFrame,
    canonical: pl.DataFrame,
    source_name: str,
) -> bool:
    """Permit a resumable rewrite only when declared type normalization preserves values."""
    try:
        typed = frame.select(SOURCE_FIELDS[source_name]).cast(
            pl.Schema(_RAW_SCHEMAS[source_name]),
            strict=True,
        )
    except Exception:
        return False
    return typed.equals(canonical, null_equal=True)


def _quarantine_forecast_first_ann_after_ann(
    frame: pl.DataFrame,
    symbol: str,
) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """Null contradictory first_ann_date values while preserving the source row."""
    expected = SOURCE_FIELDS["forecast"]
    rows: list[dict[str, object]] = []
    anomalies: list[dict[str, Any]] = []
    for line, item in enumerate(frame.select(expected).iter_rows(named=True), start=1):
        row = {name: item.get(name) for name in expected}
        ann_date = _parse_ymd(row.get("ann_date"), "ann_date", "forecast", symbol, line)
        first_ann = _optional_partition_date(
            row.get("first_ann_date"),
            "first_ann_date",
            "forecast",
            symbol,
            line,
        )
        if first_ann is not None and first_ann > ann_date:
            source_fields = {name: _jsonable_source_value(row[name]) for name in expected}
            original = ymd(first_ann)
            anomaly = {
                "schema_version": _SCHEMA_VERSION,
                "source_name": "forecast",
                "symbol": symbol,
                "rule": _FORECAST_FIRST_ANN_AFTER_ANN_RULE,
                "original_first_ann_date": original,
                "source_fields": source_fields,
                "row_hash": _json_sha256(
                    {
                        "source_name": "forecast",
                        "symbol": symbol,
                        "rule": _FORECAST_FIRST_ANN_AFTER_ANN_RULE,
                        "original_first_ann_date": original,
                        "source_fields": source_fields,
                    }
                ),
            }
            anomalies.append(anomaly)
            row["first_ann_date"] = None
        rows.append(row)
    sanitized = pl.DataFrame(rows, schema=_RAW_SCHEMAS["forecast"]).select(expected)
    return sanitized, anomalies


def _optional_partition_date(
    value: object,
    name: str,
    source_name: str,
    symbol: str,
    line: int,
) -> date | None:
    if value is None or str(value).strip().lower() in {"", "nan", "none", "null"}:
        return None
    return _parse_ymd(value, name, source_name, symbol, line)


def _jsonable_source_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise DataQualityError("forecast anomaly source field is not finite")
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def _write_anomaly_artifact(
    root: Path,
    anomaly: dict[str, Any],
    *,
    request_id: str,
) -> Path:
    payload = {
        **anomaly,
        "request_id": request_id,
    }
    digest = _json_sha256(payload)
    path = root / "anomalies" / str(anomaly["source_name"]) / f"{digest}.json"
    if path.exists():
        existing = _read_json(path, path.relative_to(root).as_posix())
        if existing != payload:
            raise DataQualityError(
                f"anomaly content hash collision for {path.relative_to(root).as_posix()}"
            )
        return path
    _write_json_atomic(path, payload)
    return path


def _anomaly_hashes(root: Path) -> dict[str, str]:
    anomaly_root = root / "anomalies"
    if not anomaly_root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): _sha256_file(path)
        for path in sorted(anomaly_root.rglob("*.json"))
    }


def _load_anomaly_artifacts(root: Path) -> list[dict[str, Any]]:
    anomaly_root = root / "anomalies"
    if not anomaly_root.is_dir():
        return []
    artifacts: list[dict[str, Any]] = []
    required = {
        "schema_version",
        "source_name",
        "symbol",
        "rule",
        "original_first_ann_date",
        "source_fields",
        "row_hash",
        "request_id",
    }
    for path in sorted(anomaly_root.rglob("*.json")):
        item = _read_json(path, path.relative_to(root).as_posix())
        if set(item) != required:
            raise TushareFetchError(f"invalid anomaly artifact: {path.name}")
        if (
            item.get("schema_version") != _SCHEMA_VERSION
            or item.get("source_name") != "forecast"
            or item.get("rule") != _FORECAST_FIRST_ANN_AFTER_ANN_RULE
            or not isinstance(item.get("source_fields"), dict)
            or sorted(item["source_fields"]) != sorted(SOURCE_FIELDS["forecast"])
        ):
            raise TushareFetchError(f"invalid anomaly artifact: {path.name}")
        expected_row_hash = _json_sha256(
            {
                "source_name": item["source_name"],
                "symbol": item["symbol"],
                "rule": item["rule"],
                "original_first_ann_date": item["original_first_ann_date"],
                "source_fields": item["source_fields"],
            }
        )
        if item.get("row_hash") != expected_row_hash:
            raise TushareFetchError(f"anomaly row_hash mismatch: {path.name}")
        if _json_sha256(item) != path.stem:
            raise TushareFetchError(f"anomaly content address mismatch: {path.name}")
        artifacts.append(item)
    return artifacts


def _provider_field_anomaly_report(root: Path, *, request_id: str) -> dict[str, Any]:
    artifacts = _load_anomaly_artifacts(root)
    for item in artifacts:
        if item.get("request_id") != request_id:
            raise TushareFetchError(
                f"anomaly artifact is not bound to collection request {request_id}"
            )
    rules = Counter(str(item["rule"]) for item in artifacts)
    return {
        "count": len(artifacts),
        "rule_distribution": dict(sorted(rules.items())),
        "affected_symbols": sorted({str(item["symbol"]) for item in artifacts}),
    }


def _aggregate_partitions(root: Path, stocks: list[str]) -> dict[str, pl.DataFrame]:
    out: dict[str, pl.DataFrame] = {}
    expected_names = {f"{symbol.replace('.', '_')}.parquet" for symbol in stocks}
    for source_name in EVENT_SOURCE_NAMES:
        paths = sorted((root / "partitions" / source_name).glob("*.parquet"))
        if {path.name for path in paths} != expected_names:
            raise TushareFetchError(
                f"{source_name} partition set is incomplete or contains extras"
            )
        frames = [pl.read_parquet(path) for path in paths]
        nonempty = [frame for frame in frames if not frame.is_empty()]
        if not nonempty:
            out[source_name] = _empty_source(source_name)
            continue
        out[source_name] = (
            pl.concat(nonempty, how="diagonal_relaxed")
            .select(SOURCE_FIELDS[source_name])
            .unique(maintain_order=False)
            .sort(list(SOURCE_FIELDS[source_name]), nulls_last=True)
        )
    return out


def _write_exports(root: Path, raw_sources: dict[str, pl.DataFrame]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for source_name in EVENT_SOURCE_NAMES:
        path = root / "exports" / f"{source_name}.parquet"
        _write_parquet_atomic(path, raw_sources[source_name])
        paths[source_name] = path
    return paths


def _build_quality_report(
    raw_sources: dict[str, pl.DataFrame],
    normalized: dict[str, pl.DataFrame],
    *,
    request_id: str,
    base_market_snapshot_id: str,
    stocks: list[str],
    start: date,
    end: date,
    query_audits: list[dict[str, Any]],
    provider_field_anomalies: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    for source_name in EVENT_SOURCE_NAMES:
        raw = raw_sources[source_name]
        table = normalized[SOURCE_TO_TABLE[source_name]]
        ann_dates = [value for value in table["ann_date"].to_list() if isinstance(value, date)]
        years = Counter(value.year for value in ann_dates)
        sources[source_name] = {
            "partitions": len(stocks),
            "raw_rows": raw.height,
            "normalized_rows": table.height,
            "exact_duplicate_rows_removed": 0,
            "unusable_rows_excluded_from_canonical_overlay": raw.height - table.height,
            "covered_symbols": int(table["symbol"].n_unique()),
            "announcement_coverage": {
                "start": min(ann_dates).isoformat() if ann_dates else None,
                "end": max(ann_dates).isoformat() if ann_dates else None,
            },
            "announcement_rows_by_year": {
                str(year): count for year, count in sorted(years.items())
            },
            "field_missing_counts": _missing_counts(raw, SOURCE_FIELDS[source_name]),
            "revision_diagnostics": _revision_diagnostics(source_name, table),
            "timing_diagnostics": _timing_diagnostics(source_name, table),
        }
    forecast = normalized["earnings_forecast_events"]
    audit = normalized["audit_opinion_events"]
    unlock = normalized["share_unlock_events"]
    audit_distribution = Counter(str(value) for value in audit["audit_result"].to_list())
    non_exact_standard = sorted(
        value for value in audit_distribution if value != "标准无保留意见"
    )
    anomalies = provider_field_anomalies or {
        "count": 0,
        "rule_distribution": {},
        "affected_symbols": [],
    }
    return {
        "schema_version": _SCHEMA_VERSION,
        "complete": True,
        "request_id": request_id,
        "base_market_snapshot_id": base_market_snapshot_id,
        "coverage": {"start": start.isoformat(), "end": end.isoformat()},
        "requested_stocks": len(stocks),
        "expected_partitions": len(stocks) * len(EVENT_SOURCE_NAMES),
        "availability_policy": EVENT_AVAILABILITY_POLICY,
        "forecast_first_ann_anomaly_policy": _FORECAST_FIRST_ANN_ANOMALY_POLICY,
        "sources": sources,
        "forecast_type_transition_counts": _forecast_transitions(forecast),
        "audit_result_distribution": dict(sorted(audit_distribution.items())),
        "audit_results_requiring_manual_classification": non_exact_standard,
        "share_unlock": {
            "float_share_unit": "shares, per Tushare share_float documentation",
            "float_ratio_unit": "percent of total shares, per Tushare share_float documentation",
            "float_ratio_non_null_rows": unlock["float_ratio"].drop_nulls().len(),
            "float_ratio_null_rows": unlock["float_ratio"].null_count(),
        },
        "provider_field_anomalies": anomalies,
        "query_fallbacks": {
            "count": len(query_audits),
            "symbols": sorted(str(item["symbol"]) for item in query_audits),
            "announcement_dates_examined": sum(
                int(str(item.get("announcement_dates_examined", item["announcement_dates_queried"])))
                for item in query_audits
            ),
            "pre_listing_dates_skipped": sum(
                int(str(item.get("pre_listing_dates_skipped", 0))) for item in query_audits
            ),
            "all_market_cache_hits": sum(
                int(str(item.get("all_market_cache_hits", 0))) for item in query_audits
            ),
            "all_market_network_queries": sum(
                int(str(item.get("all_market_network_queries", item["announcement_dates_queried"])))
                for item in query_audits
            ),
            "local_day_cache_files": sum(
                int(
                    str(
                        item.get(
                            "local_day_cache_files",
                            item.get(
                                "announcement_dates_examined",
                                item["announcement_dates_queried"],
                            ),
                        )
                    )
                )
                for item in query_audits
            ),
            "announcement_dates_queried": sum(
                int(str(item["announcement_dates_queried"])) for item in query_audits
            ),
            "retained_target_rows": sum(
                int(str(item["retained_target_rows"])) for item in query_audits
            ),
        },
        "research_boundary": {
            "ready_for_materialization": True,
            "ready_for_scoring": False,
            "ready_for_trading": False,
            "statement": (
                "Coverage and revision diagnostics do not establish alpha or authorize a hard exclusion rule."
            ),
        },
    }


def _revision_diagnostics(source_name: str, frame: pl.DataFrame) -> dict[str, int]:
    keys = {
        "forecast": ["symbol", "report_period"],
        "express": ["symbol", "report_period"],
        "stk_holdernumber": ["symbol", "end_date"],
        "share_float": [
            "symbol",
            "float_date",
            "holder_name",
            "share_type",
            "float_share",
            "float_ratio",
        ],
        "fina_audit": ["symbol", "report_period"],
    }[source_name]
    groups = frame.group_by(keys).agg(pl.col("ann_date").n_unique().alias("versions"))
    revised = groups.filter(pl.col("versions") > 1)
    maximum = groups["versions"].max()
    return {
        "logical_groups": groups.height,
        "groups_with_multiple_announcement_dates": revised.height,
        "max_announcement_versions": int(str(maximum)) if maximum is not None else 0,
    }


def _timing_diagnostics(
    source_name: str, frame: pl.DataFrame
) -> dict[str, float | int | str | None]:
    other = {
        "forecast": "report_period",
        "express": "report_period",
        "stk_holdernumber": "end_date",
        "share_float": "float_date",
        "fina_audit": "report_period",
    }[source_name]
    values: list[int] = []
    for item in frame.select(["ann_date", other]).iter_rows(named=True):
        ann = item["ann_date"]
        comparison = item[other]
        if not isinstance(ann, date) or not isinstance(comparison, date):
            raise DataQualityError(f"{source_name} timing columns are invalid")
        delta = comparison - ann if source_name == "share_float" else ann - comparison
        values.append(delta.days)
    return {
        "metric": (
            "days_from_announcement_to_unlock"
            if source_name == "share_float"
            else "days_from_period_end_to_announcement"
        ),
        "observations": len(values),
        "minimum": min(values) if values else None,
        "median": float(median(values)) if values else None,
        "maximum": max(values) if values else None,
    }


def _forecast_transitions(frame: pl.DataFrame) -> dict[str, int]:
    grouped: dict[tuple[str, date], list[tuple[date, str]]] = defaultdict(list)
    for item in frame.select(
        ["symbol", "report_period", "ann_date", "forecast_type"]
    ).iter_rows(named=True):
        grouped[(str(item["symbol"]), item["report_period"])].append(
            (item["ann_date"], str(item["forecast_type"]))
        )
    transitions: Counter[str] = Counter()
    for values in grouped.values():
        ordered = sorted(values)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            transitions[f"{previous[1]} -> {current[1]}"] += 1
    return dict(sorted(transitions.items()))


def _missing_counts(frame: pl.DataFrame, fields: tuple[str, ...]) -> dict[str, int]:
    result: dict[str, int] = {}
    for name in fields:
        count = 0
        for value in frame[name].to_list():
            if value is None or (isinstance(value, str) and not value.strip()):
                count += 1
        result[name] = count
    return result


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


def _market_stock_listing_dates(market_dir: Path, stocks: list[str]) -> dict[str, date]:
    """Return verified listing dates for exactly the ordinary-market stock universe."""
    frame = pl.read_parquet(market_dir / "instruments.parquet")
    required = {"symbol", "listing_date", "is_index", "is_global"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataQualityError(f"market instruments missing columns: {missing}")
    expected = set(stocks)
    result: dict[str, date] = {}
    eligible = frame.filter(~pl.col("is_index") & ~pl.col("is_global"))
    for line, item in enumerate(
        eligible.select(["symbol", "listing_date"]).iter_rows(named=True), start=1
    ):
        symbol = require_ts_code(str(item["symbol"]), kind="stock")
        if symbol not in expected:
            continue
        listing_date = item["listing_date"]
        if isinstance(listing_date, datetime):
            listing_date = listing_date.date()
        if not isinstance(listing_date, date):
            raise DataQualityError(
                f"market listing_date is invalid for {symbol} at source row {line}"
            )
        previous = result.get(symbol)
        if previous is not None and previous != listing_date:
            raise DataQualityError(f"market has conflicting listing_date values for {symbol}")
        result[symbol] = listing_date
    missing_symbols = sorted(expected - set(result))
    if missing_symbols:
        raise DataQualityError(
            "market listing_date is missing for stock instruments: "
            f"{', '.join(missing_symbols[:5])}"
        )
    return result


def _empty_source(source_name: str) -> pl.DataFrame:
    return pl.DataFrame(schema=_RAW_SCHEMAS[source_name]).select(SOURCE_FIELDS[source_name])


def _partition_ann_date(
    item: dict[str, Any],
    *,
    source_name: str,
    symbol: str,
    line: int,
) -> date:
    """Read a source row date without promoting unusable holder rows to events."""
    value = item.get("ann_date")
    try:
        return _parse_ymd(value, "ann_date", source_name, symbol, line)
    except DataQualityError:
        # Tushare occasionally returns a provider timestamp in ann_date on a
        # holder-count row whose holder_num is blank. The row must remain in
        # the raw export as missingness evidence, but normalize_holder_count
        # excludes it before constructing available_at. Accept only this exact
        # timestamp shape for window filtering; a usable holder-count event
        # remains subject to the documented YYYYMMDD contract.
        holder_value = item.get("holder_num")
        holder_missing = holder_value is None or str(holder_value).strip().lower() in {
            "",
            "nan",
            "none",
            "null",
        }
        if source_name != "stk_holdernumber" or not holder_missing:
            raise
        text = str(value or "").strip()
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").date()
        except ValueError:
            raise


def _parse_ymd(
    value: object,
    name: str,
    source_name: str,
    symbol: str,
    line: int,
) -> date:
    text = str(value or "").strip().replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise DataQualityError(
            f"{source_name} {name} is invalid for {symbol} at source row {line}"
        ) from exc


def _verify_collection_request(path: Path, expected_request: dict[str, Any]) -> None:
    existing = _read_json(path, "collection_request.json")
    # Existing unfinished staging epochs may predate the additive anomaly policy key.
    # Core request identity (including request_id) must still match exactly.
    optional_keys = {"forecast_first_ann_anomaly_policy"}
    expected_core = {key: value for key, value in expected_request.items() if key not in optional_keys}
    existing_core = {key: value for key, value in existing.items() if key not in optional_keys}
    if existing_core != expected_core:
        raise TushareFetchError(
            "staging directory belongs to a different event request; use a new --staging-dir"
        )
    if (
        "forecast_first_ann_anomaly_policy" in existing
        and existing["forecast_first_ann_anomaly_policy"]
        != expected_request["forecast_first_ann_anomaly_policy"]
    ):
        raise TushareFetchError(
            "staging directory belongs to a different event request; use a new --staging-dir"
        )


def _verify_collection_manifest(root: Path, *, request_id: str) -> None:
    manifest = _read_json(root / "collection_manifest.json", "collection_manifest.json")
    request = _read_json(root / "collection_request.json", "collection_request.json")
    if manifest.get("request_id") != request_id:
        raise TushareFetchError("event collection manifest request ID does not match")
    requested_stocks = int(str(request.get("requested_stocks")))
    expected_partitions = requested_stocks * len(EVENT_SOURCE_NAMES)
    actual_partitions = len(list((root / "partitions").glob("*/*.parquet")))
    if (
        manifest.get("partition_count") != expected_partitions
        or actual_partitions != expected_partitions
    ):
        raise TushareFetchError("event collection partition set is incomplete or contains extras")
    if manifest.get("dataset_hashes") != _dataset_hashes(root):
        raise TushareFetchError(
            "event collection manifest hashes do not match staged parquet bytes"
        )
    export_hashes = {
        name: _sha256_file(root / "exports" / f"{name}.parquet")
        for name in EVENT_SOURCE_NAMES
    }
    if manifest.get("export_hashes") != export_hashes:
        raise TushareFetchError("event collection export hashes do not match")
    source_manifest_path = root / "source_manifest.json"
    if manifest.get("source_manifest_sha256") != _sha256_file(source_manifest_path):
        raise TushareFetchError("event collection source manifest hash does not match")
    try:
        source_manifest = EventSourceManifest.model_validate_json(
            source_manifest_path.read_bytes()
        )
    except Exception as exc:
        raise TushareFetchError("event collection source manifest is invalid") from exc
    for name in EVENT_SOURCE_NAMES:
        expected_path = f"exports/{name}.parquet"
        item = source_manifest.files[name]
        if item.path != expected_path or item.sha256 != export_hashes[name]:
            raise TushareFetchError(
                f"event collection source manifest does not match {name} export"
            )
    quality_path = root / "quality_report.json"
    if manifest.get("quality_report_sha256") != _sha256_file(quality_path):
        raise TushareFetchError("event collection quality report hash does not match")
    if manifest.get("query_audit_hashes") != _query_audit_hashes(root):
        raise TushareFetchError("event collection query-audit hashes do not match")
    if manifest.get("query_cache_hashes") != _query_cache_hashes(root):
        raise TushareFetchError("event collection query-cache hashes do not match")
    disk_anomaly_hashes = _anomaly_hashes(root)
    quality = _read_json(quality_path, "quality_report.json")
    has_manifest_anomalies = "anomaly_hashes" in manifest
    has_quality_anomalies = "provider_field_anomalies" in quality
    # schema_version=1 collections stamped before anomaly evidence existed omit both
    # fields and have an empty anomalies/ tree. Accept only that exact legacy shape as
    # zero-anomaly; any disk evidence or partial/new field presence stays fail-closed.
    legacy_zero_anomaly = not has_manifest_anomalies and not has_quality_anomalies and not disk_anomaly_hashes
    if not legacy_zero_anomaly:
        if (
            not has_manifest_anomalies
            or not has_quality_anomalies
            or manifest.get("anomaly_hashes") != disk_anomaly_hashes
        ):
            raise TushareFetchError("event collection anomaly hashes do not match")
        _provider_field_anomaly_report(root, request_id=request_id)
        anomalies = quality.get("provider_field_anomalies")
        if not isinstance(anomalies, dict) or anomalies.get("count") != len(disk_anomaly_hashes):
            raise TushareFetchError("event collection quality report violates research boundaries")
    boundary = quality.get("research_boundary")
    if (
        quality.get("request_id") != request_id
        or not isinstance(boundary, dict)
        or boundary.get("ready_for_scoring") is not False
        or boundary.get("ready_for_trading") is not False
    ):
        raise TushareFetchError("event collection quality report violates research boundaries")


def _dataset_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for source_name in EVENT_SOURCE_NAMES:
        digest = hashlib.sha256()
        for path in sorted((root / "partitions" / source_name).glob("*.parquet")):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(_sha256_file(path).encode("ascii"))
            digest.update(b"\n")
        out[source_name] = digest.hexdigest()
    return out


def _query_audit_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256_file(path)
        for path in sorted((root / "query-audit").glob("*/*.json"))
    }


def _query_cache_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256_file(path)
        for path in sorted((root / "query-cache").rglob("*.json"))
    }


def _read_query_audits(root: Path) -> list[dict[str, Any]]:
    audits: list[dict[str, Any]] = []
    for path in sorted((root / "query-audit").glob("*/*.json")):
        item = _read_json(path, path.relative_to(root).as_posix())
        legacy_required = {
            "schema_version",
            "source_name",
            "symbol",
            "coverage_start",
            "coverage_end",
            "reasons",
            "primary_response_rows",
            "primary_unique_symbols",
            "fallback_policy",
            "announcement_dates_queried",
            "fallback_rows_before_symbol_filter",
            "retained_target_rows",
        }
        listing_aware_required = legacy_required | {
            "announcement_dates_examined",
            "pre_listing_dates_skipped",
        }
        shared_cache_required = listing_aware_required | {
            "all_market_cache_hits",
            "all_market_network_queries",
        }
        compact_shared_cache_required = shared_cache_required | {"local_day_cache_files"}
        if (
            set(item)
            not in (
                legacy_required,
                listing_aware_required,
                shared_cache_required,
                compact_shared_cache_required,
            )
            or item.get("source_name") != "share_float"
        ):
            raise TushareFetchError(f"invalid query audit: {path.name}")
        if set(item) in (
            listing_aware_required,
            shared_cache_required,
            compact_shared_cache_required,
        ):
            try:
                examined = int(str(item["announcement_dates_examined"]))
                skipped = int(str(item["pre_listing_dates_skipped"]))
                queried = int(str(item["announcement_dates_queried"]))
            except (TypeError, ValueError) as exc:
                raise TushareFetchError(f"invalid query audit: {path.name}") from exc
            if min(examined, skipped, queried) < 0 or examined != skipped + queried:
                raise TushareFetchError(f"invalid query audit: {path.name}")
        if set(item) in (shared_cache_required, compact_shared_cache_required):
            try:
                cache_hits = int(str(item["all_market_cache_hits"]))
                network_queries = int(str(item["all_market_network_queries"]))
            except (TypeError, ValueError) as exc:
                raise TushareFetchError(f"invalid query audit: {path.name}") from exc
            if (
                min(cache_hits, network_queries) < 0
                or cache_hits + network_queries > queried
            ):
                raise TushareFetchError(f"invalid query audit: {path.name}")
        if set(item) == compact_shared_cache_required:
            local_day_cache_files = item.get("local_day_cache_files")
            if (
                not isinstance(local_day_cache_files, int)
                or isinstance(local_day_cache_files, bool)
                or local_day_cache_files < 0
                or local_day_cache_files > examined
            ):
                raise TushareFetchError(f"invalid query audit: {path.name}")
        symbol = str(item.get("symbol"))
        expected_name = f"{symbol.replace('.', '_')}.json"
        partition = root / "partitions" / "share_float" / f"{symbol.replace('.', '_')}.parquet"
        if path.name != expected_name or not partition.is_file():
            raise TushareFetchError(
                f"query audit is not bound to a completed share_float partition: {path.name}"
            )
        cache_dir = root / "query-cache" / "share_float" / symbol.replace(".", "_")
        cache_files = sorted(cache_dir.glob("*.json"))
        expected_days = int(
            str(
                item.get(
                    "local_day_cache_files",
                    item.get("announcement_dates_examined", item["announcement_dates_queried"]),
                )
            )
        )
        if len(cache_files) != expected_days:
            raise TushareFetchError(
                f"query audit cache set is incomplete for share_float {symbol}"
            )
        audits.append(item)
    return audits


def _symbols_sha256(stocks: list[str]) -> str:
    return hashlib.sha256(("\n".join(stocks) + "\n").encode("utf-8")).hexdigest()


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
