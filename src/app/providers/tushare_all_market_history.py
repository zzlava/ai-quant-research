from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import polars as pl

from app.clock import decision_at_utc
from app.errors import DataQualityError, TushareFetchError
from app.models.config import StrategyConfig
from app.models.snapshot import DataSnapshot
from app.providers._frames import UNIVERSE_MEMBERSHIP_SCHEMA
from app.providers.tushare_client import TushareQueryClient
from app.providers.tushare_fetch import write_normalized_tables
from app.providers.tushare_normalize import (
    TushareRaw,
    is_full_day_suspend_timing,
    normalize_tushare,
    open_trading_days,
    parse_ymd,
    require_ts_code,
    split_session_symbols,
    ymd,
)
from app.storage.import_market import import_market_data
from app.storage.quality import validate_universe_membership

_SCHEMA_VERSION = "1"
_LOOKBACK_CALENDAR_DAYS = 400
_REQUEST_INTERVAL_SECONDS = 0.31
_STOCK_BASIC_FIELDS = "ts_code,name,industry,list_date,delist_date,market,exchange,list_status"
_STOCK_BASIC_STATUSES = ("L", "D", "P", "G")
_ALLOWED_EXCHANGES = frozenset({"SSE", "SZSE"})
_ALLOWED_MARKETS = frozenset({"主板", "创业板", "科创板"})

_DAY_APIS: dict[str, dict[str, Any]] = {
    "daily": {"source_history": True, "page_size": 5000},
    "adj_factor": {"source_history": True, "page_size": 5000},
    "daily_basic": {
        "source_history": False,
        "page_size": 5000,
        "fields": "ts_code,trade_date,turnover_rate",
    },
    "stk_limit": {
        "source_history": False,
        "page_size": 5000,
        "fields": "ts_code,trade_date,pre_close,up_limit,down_limit",
    },
    "suspend_d": {
        "source_history": True,
        "page_size": 2000,
        "suspend_type": "S",
    },
}


@dataclass(frozen=True)
class AllMarketCollectionResult:
    staging_dir: Path
    request_id: str
    source_start: date
    coverage_start: date
    coverage_end: date
    trading_days: int
    selected_stocks: int
    completed_partitions: int
    reused_partitions: int
    collection_manifest_path: Path
    quality_report_path: Path


@dataclass(frozen=True)
class AllMarketMaterializeResult:
    snapshot: DataSnapshot
    selected_stocks: int
    membership_rows: int
    min_members: int
    max_members: int


class _EndpointPacer:
    def __init__(
        self,
        client: TushareQueryClient,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._enabled = bool(getattr(client, "requires_single_code_rate_limit", False))
        self._clock = monotonic_clock
        self._sleep = sleeper
        self._next_at: dict[str, float] = {}

    def wait(self, api_name: str) -> None:
        if not self._enabled:
            return
        next_at = self._next_at.get(api_name)
        if next_at is not None:
            delay = next_at - self._clock()
            if delay > 0:
                self._sleep(delay)
        self._next_at[api_name] = self._clock() + _REQUEST_INTERVAL_SECONDS


def collect_tushare_all_a_share_history(
    *,
    client: TushareQueryClient,
    config: StrategyConfig,
    start: date,
    end: date,
    staging_dir: Path,
    progress: Callable[[str, int, int, bool], None] | None = None,
) -> AllMarketCollectionResult:
    """Collect resumable, date-partitioned raw inputs for a historical all-A snapshot."""
    _require_historical_all_a_config(config)
    if end < start:
        raise TushareFetchError("end date must be on or after start date")
    root = Path(staging_dir)
    root.mkdir(parents=True, exist_ok=True)
    source_start = start - timedelta(days=_LOOKBACK_CALENDAR_DAYS)
    request_payload = {
        "schema_version": _SCHEMA_VERSION,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "source_start": source_start.isoformat(),
        "strategy_config_hash": config.config_hash(),
        "universe_id": config.universe.id,
        "market_index": config.data.market_index,
        "global_symbol": config.data.global_symbol,
        "day_apis": _DAY_APIS,
    }
    request_id = _json_sha256(request_payload)
    request_path = root / "collection_request.json"
    if request_path.exists():
        existing = _read_json(request_path, "collection_request.json")
        if existing != {**request_payload, "request_id": request_id}:
            raise TushareFetchError(
                "staging directory belongs to a different collection request; use a new --staging-dir"
            )
    else:
        _write_json_atomic(request_path, {**request_payload, "request_id": request_id})
    if (root / "collection_manifest.json").exists():
        _verify_collection_manifest(root, request_id=request_id)

    pacer = _EndpointPacer(client)
    trade_cal = _collect_reference(
        root / "reference" / "trade_cal.parquet",
        lambda: _paced_query(
            client,
            pacer,
            "trade_cal",
            exchange="SSE",
            start_date=ymd(source_start),
            end_date=ymd(end),
            is_open="1",
        ),
        required_columns=("cal_date",),
    )
    source_days = open_trading_days(trade_cal, source_start, end)
    target_days = [day for day in source_days if start <= day <= end]
    if not target_days:
        raise TushareFetchError("trade_cal has no open dates in the requested range")

    stock_basic = _collect_stock_basic(client, pacer, root / "reference" / "stock_basic.parquet")
    stocks = select_historical_a_share(stock_basic, start=start, end=end)
    _collect_namechange(client, pacer, root / "reference" / "namechange.parquet", stocks)

    total = sum(
        len(source_days if bool(spec["source_history"]) else target_days)
        for spec in _DAY_APIS.values()
    )
    done = 0
    completed = 0
    reused = 0
    for api_name, spec in _DAY_APIS.items():
        days = source_days if bool(spec["source_history"]) else target_days
        for day in days:
            path = root / "partitions" / api_name / f"{ymd(day)}.parquet"
            if path.exists():
                _validate_day_partition(pl.read_parquet(path), api_name, day)
                reused += 1
                was_reused = True
            else:
                extras = {
                    key: value
                    for key, value in spec.items()
                    if key not in {"source_history", "page_size"}
                }
                frame = _query_paginated_day(
                    client,
                    pacer,
                    api_name,
                    day,
                    page_size=int(spec["page_size"]),
                    **extras,
                )
                if not frame.is_empty() and "ts_code" in frame.columns:
                    frame = frame.filter(pl.col("ts_code").is_in(stocks))
                _validate_day_partition(frame, api_name, day)
                _write_parquet_atomic(path, frame)
                completed += 1
                was_reused = False
            done += 1
            if progress is not None:
                progress(api_name, done, total, was_reused)

    _collect_legacy_suspend_fallback(
        client,
        pacer,
        root,
        stock_basic=stock_basic,
        stocks=stocks,
        target_days=target_days,
    )

    indices, globals_ = split_session_symbols(config, stocks)
    _collect_reference(
        root / "reference" / "index_daily.parquet",
        lambda: _query_symbol_ranges(
            client,
            pacer,
            "index_daily",
            indices,
            start,
            end,
        ),
        required_columns=("ts_code", "trade_date"),
    )
    _collect_reference(
        root / "reference" / "index_global.parquet",
        lambda: _query_symbol_ranges(
            client,
            pacer,
            "index_global",
            globals_,
            start,
            end,
        ),
        required_columns=("ts_code", "trade_date"),
    )

    report = _build_quality_report(root, stocks, source_days, target_days)
    quality_path = root / "quality_report.json"
    _write_json_atomic(quality_path, report)
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "request_id": request_id,
        "source_name": "tushare_all_a_share_history",
        "collected_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage": {"start": start.isoformat(), "end": end.isoformat()},
        "source_start": source_start.isoformat(),
        "selected_stocks": len(stocks),
        "trading_days": len(target_days),
        "dataset_hashes": _dataset_hashes(root),
        "quality_report_sha256": _sha256_file(quality_path),
        "st_definition": "official namechange effective start/end intervals; ST/PT names only",
        "suspension_definition": (
            "official suspend_d daily rows plus legacy suspend intervals for otherwise unexplained active gaps"
        ),
        "membership_definition": (
            "derived later from same-day PIT listing age, ST, suspension, and trailing 20-day amount"
        ),
    }
    manifest_path = root / "collection_manifest.json"
    _write_json_atomic(manifest_path, manifest)
    return AllMarketCollectionResult(
        staging_dir=root,
        request_id=request_id,
        source_start=source_start,
        coverage_start=start,
        coverage_end=end,
        trading_days=len(target_days),
        selected_stocks=len(stocks),
        completed_partitions=completed,
        reused_partitions=reused,
        collection_manifest_path=manifest_path,
        quality_report_path=quality_path,
    )


def materialize_tushare_all_a_share_history(
    *,
    staging_dir: Path,
    dest_dir: Path,
    config: StrategyConfig,
    source_version: str | None = None,
    replace_existing: bool = False,
) -> AllMarketMaterializeResult:
    """Normalize a completed collection and derive a daily liquid universe."""
    _require_historical_all_a_config(config)
    root = Path(staging_dir)
    request = _read_json(root / "collection_request.json", "collection_request.json")
    manifest = _read_json(root / "collection_manifest.json", "collection_manifest.json")
    if request.get("request_id") != manifest.get("request_id"):
        raise TushareFetchError("collection request and manifest IDs do not match")
    if request.get("strategy_config_hash") != config.config_hash():
        raise TushareFetchError("collection strategy config hash does not match the requested strategy")
    _verify_collection_manifest(root, request_id=str(request["request_id"]))
    start = date.fromisoformat(str(request["start"]))
    end = date.fromisoformat(str(request["end"]))
    destination = Path(dest_dir)
    if destination.exists() and any(destination.iterdir()):
        if not replace_existing:
            raise TushareFetchError(
                "destination already contains a snapshot; use a new AIQ_DATA_DIR or pass --replace-existing"
            )
        if not (destination / "manifest.json").is_file():
            raise TushareFetchError(
                "destination is non-empty but is not a recognized market snapshot; refusing to replace it"
            )

    stock_basic = pl.read_parquet(root / "reference" / "stock_basic.parquet")
    stocks = select_historical_a_share(stock_basic, start=start, end=end)
    daily = _read_partitions(root, "daily")
    seed_daily_path = root / "reference" / "suspend_seed_daily.parquet"
    if seed_daily_path.exists():
        seed_daily = pl.read_parquet(seed_daily_path)
        if not seed_daily.is_empty():
            existing_keys = daily.select(["ts_code", "trade_date"])
            seed_daily = seed_daily.join(
                existing_keys,
                on=["ts_code", "trade_date"],
                how="anti",
            )
            daily = pl.concat([daily, seed_daily], how="diagonal_relaxed")
    interval_path = root / "reference" / "suspend_intervals.parquet"
    raw = TushareRaw(
        trade_cal=pl.read_parquet(root / "reference" / "trade_cal.parquet"),
        stock_basic=stock_basic,
        daily=daily,
        daily_basic=_read_partitions(root, "daily_basic"),
        adj_factor=_read_partitions(root, "adj_factor"),
        stk_limit=_read_partitions(root, "stk_limit"),
        suspend_d=_read_partitions(root, "suspend_d"),
        namechange=pl.read_parquet(root / "reference" / "namechange.parquet"),
        stock_st=None,
        suspend_intervals=pl.read_parquet(interval_path) if interval_path.exists() else None,
        index_daily=pl.read_parquet(root / "reference" / "index_daily.parquet"),
        index_global=pl.read_parquet(root / "reference" / "index_global.parquet"),
    )
    tables = normalize_tushare(raw, config, start, end, stocks)
    amount_history = _liquidity_amount_history(raw, tables, stocks=stocks, start=start)
    membership = build_derived_liquid_membership(
        tables,
        config=config,
        amount_history=amount_history,
    )
    tables["universe_membership"] = membership
    counts = membership.group_by("as_of_date").len().sort("as_of_date")
    min_value = counts["len"].min()
    max_value = counts["len"].max()
    if not isinstance(min_value, int) or not isinstance(max_value, int):
        raise DataQualityError("derived membership has no daily member counts")
    min_members = min_value
    max_members = max_value

    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".tushare-all-a-history-{uuid.uuid4().hex}"
    try:
        write_normalized_tables(tables, temporary)
        snapshot = import_market_data(
            temporary,
            destination,
            source_name="tushare_all_a_share_history",
            adjustment=config.data.adjustment,
            source_version=source_version or str(manifest["request_id"]),
            market_index=config.data.market_index,
            global_symbol=config.data.global_symbol,
        )
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return AllMarketMaterializeResult(
        snapshot=snapshot,
        selected_stocks=len(stocks),
        membership_rows=membership.height,
        min_members=min_members,
        max_members=max_members,
    )


def select_historical_a_share(stock_basic: pl.DataFrame, *, start: date, end: date) -> list[str]:
    required = {"ts_code", "list_date", "delist_date", "market", "exchange", "list_status"}
    missing = sorted(required - set(stock_basic.columns))
    if stock_basic.is_empty() or missing:
        detail = f" missing columns: {missing}" if missing else ""
        raise TushareFetchError(f"stock_basic returned no usable securities.{detail}")
    stocks: list[str] = []
    seen: set[str] = set()
    for item in stock_basic.to_dicts():
        raw_code = str(item.get("ts_code") or "").strip()
        exchange = str(item.get("exchange") or "").strip().upper()
        market = str(item.get("market") or "").strip()
        # Historical stock_basic can contain provider-specific retired codes
        # such as T600018.SH with no recognized market. Filter non-target
        # markets before enforcing the six-digit tradable-stock contract.
        if exchange not in _ALLOWED_EXCHANGES or market not in _ALLOWED_MARKETS:
            continue
        is_b_share = (raw_code.endswith(".SH") and raw_code.startswith("900")) or (
            raw_code.endswith(".SZ") and raw_code.startswith("200")
        )
        if not raw_code.endswith((".SH", ".SZ")) or is_b_share:
            continue
        code = require_ts_code(raw_code, kind="stock")
        if code in seen:
            raise DataQualityError(f"stock_basic has duplicate ts_code {code}")
        seen.add(code)
        list_raw = item.get("list_date")
        if not list_raw:
            raise DataQualityError(f"stock_basic missing list_date for {code}")
        listed = parse_ymd(list_raw)
        delist_raw = item.get("delist_date")
        delisted = parse_ymd(delist_raw) if delist_raw not in (None, "", "None") else None
        if listed <= end and (delisted is None or delisted > start):
            stocks.append(code)
    if not stocks:
        raise TushareFetchError("stock_basic has no SSE/SZSE common A shares overlapping the window")
    return sorted(stocks)


def build_derived_liquid_membership(
    tables: dict[str, pl.DataFrame],
    *,
    config: StrategyConfig,
    amount_history: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Derive each day's tradable research set using information available that day."""
    daily = tables.get("daily_bars", pl.DataFrame())
    instruments = tables.get("instruments", pl.DataFrame())
    calendar_frame = tables.get("calendar", pl.DataFrame())
    if daily.is_empty() or instruments.is_empty() or calendar_frame.is_empty():
        raise DataQualityError("cannot derive liquid membership from empty normalized tables")
    required_daily = {"symbol", "date", "amount", "is_st", "is_suspended"}
    missing = sorted(required_daily - set(daily.columns))
    if missing:
        raise DataQualityError(f"daily_bars missing derived-membership columns: {missing}")
    stock_instruments = instruments.filter(~pl.col("is_index") & ~pl.col("is_global")).select(
        ["symbol", "listing_date"]
    )
    liquidity = amount_history if amount_history is not None else daily.select(["symbol", "date", "amount"])
    liquidity_required = {"symbol", "date", "amount"}
    liquidity_missing = sorted(liquidity_required - set(liquidity.columns))
    if liquidity.is_empty() or liquidity_missing:
        raise DataQualityError(f"amount_history missing derived-membership columns: {liquidity_missing}")
    rolling = liquidity.sort(["symbol", "date"]).with_columns(
        [
            pl.col("amount").rolling_mean(window_size=20, min_samples=20).over("symbol").alias("avg_amount_20d"),
            pl.col("amount")
            .rolling_sum(window_size=20, min_samples=20)
            .over("symbol")
            .is_not_null()
            .alias("has_20_bars"),
        ]
    )
    work = (
        daily.join(
            rolling.select(["symbol", "date", "avg_amount_20d", "has_20_bars"]),
            on=["symbol", "date"],
            how="left",
            validate="1:1",
        )
        .sort(["symbol", "date"])
        .join(stock_instruments, on="symbol", how="inner")
        .with_columns((pl.col("date") - pl.col("listing_date")).dt.total_days().alias("listing_days"))
    )
    eligible = work.filter(
        pl.col("has_20_bars")
        & (pl.col("listing_days") >= config.universe.min_listing_days)
        & (~pl.col("is_st") if config.universe.exclude_st else pl.lit(True))
        & (~pl.col("is_suspended") if config.universe.exclude_suspended else pl.lit(True))
        & (pl.col("avg_amount_20d") >= config.universe.min_avg_turnover_20d)
    )
    rows = eligible.select(
        [
            pl.lit(config.universe.id).alias("universe_id"),
            pl.col("date").alias("as_of_date"),
            pl.col("symbol"),
        ]
    ).with_columns(
        pl.Series(
            "available_at",
            [decision_at_utc(day, config.data) for day in eligible["date"].to_list()],
            dtype=pl.Datetime("us"),
        ),
        pl.lit(None).cast(pl.Float64).alias("weight"),
    )
    calendar = [day for day in calendar_frame["date"].to_list() if isinstance(day, date)]
    validate_universe_membership(
        rows,
        calendar,
        instruments,
        universe_id=config.universe.id,
        expected_constituents=None,
    )
    return rows.select(list(UNIVERSE_MEMBERSHIP_SCHEMA)).sort(["as_of_date", "symbol"])


def _liquidity_amount_history(
    raw: TushareRaw,
    tables: dict[str, pl.DataFrame],
    *,
    stocks: list[str],
    start: date,
) -> pl.DataFrame:
    calendar = open_trading_days(raw.trade_cal, date.min, start - timedelta(days=1))
    seed_days = calendar[-19:]
    if len(seed_days) < 19:
        raise DataQualityError("trade_cal has fewer than 19 pre-window dates for liquidity warm-up")
    stock_set = set(stocks)
    daily_amount: dict[tuple[str, date], float] = {}
    for item in raw.daily.to_dicts():
        symbol = str(item.get("ts_code") or "").strip()
        if symbol not in stock_set:
            continue
        day = parse_ymd(item.get("trade_date"))
        if day not in seed_days:
            continue
        value = item.get("amount")
        if not isinstance(value, int | float | str):
            raise DataQualityError(f"daily amount is invalid for {symbol} on {day}")
        try:
            amount = float(value) * 1000.0
        except (TypeError, ValueError) as exc:
            raise DataQualityError(f"daily amount is invalid for {symbol} on {day}") from exc
        daily_amount[(symbol, day)] = amount
    suspended: set[tuple[str, date]] = set()
    if raw.suspend_d is None:
        raise DataQualityError("suspend_d is required for liquidity warm-up")
    for item in raw.suspend_d.to_dicts():
        symbol = str(item.get("ts_code") or "").strip()
        if symbol not in stock_set or str(item.get("suspend_type") or "").upper() != "S":
            continue
        if not is_full_day_suspend_timing(item.get("suspend_timing")):
            continue
        day = parse_ymd(item.get("trade_date"))
        if day in seed_days:
            suspended.add((symbol, day))
    if raw.suspend_intervals is not None and not raw.suspend_intervals.is_empty():
        for item in raw.suspend_intervals.to_dicts():
            symbol = str(item.get("ts_code") or "").strip()
            if symbol not in stock_set:
                continue
            start_raw = item.get("suspend_date")
            if start_raw in (None, "", "None"):
                continue
            interval_start = parse_ymd(start_raw)
            resume_raw = item.get("resume_date")
            resume = (
                parse_ymd(resume_raw) if resume_raw not in (None, "", "None") else None
            )
            for day in seed_days:
                if day >= interval_start and (resume is None or day < resume):
                    suspended.add((symbol, day))
    bounds: dict[str, tuple[date, date | None]] = {}
    for item in raw.stock_basic.to_dicts():
        symbol = str(item.get("ts_code") or "").strip()
        if symbol not in stock_set:
            continue
        list_raw = item.get("list_date")
        if not list_raw:
            raise DataQualityError(f"stock_basic missing list_date for {symbol}")
        delist_raw = item.get("delist_date")
        bounds[symbol] = (
            parse_ymd(list_raw),
            parse_ymd(delist_raw) if delist_raw not in (None, "", "None") else None,
        )
    rows: list[dict[str, object]] = []
    for symbol in stocks:
        listed, delisted = bounds[symbol]
        for day in seed_days:
            if day < listed or (delisted is not None and day >= delisted):
                continue
            key = (symbol, day)
            if key in daily_amount:
                amount = daily_amount[key]
            elif key in suspended:
                amount = 0.0
            else:
                raise DataQualityError(
                    f"unknown liquidity warm-up gap for {symbol} on {day}; "
                    "refusing to treat missing data as zero amount"
                )
            rows.append({"symbol": symbol, "date": day, "amount": amount})
    target = tables["daily_bars"].select(["symbol", "date", "amount"])
    seed = (
        pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date), pl.col("amount").cast(pl.Float64))
        if rows
        else target.clear()
    )
    return pl.concat([seed, target], how="vertical_relaxed").sort(["symbol", "date"])


def _require_historical_all_a_config(config: StrategyConfig) -> None:
    if config.research_scope != "historical_all_a_share" or config.universe.mode != "derived_liquid":
        raise TushareFetchError(
            "historical all-A collection requires research_scope=historical_all_a_share "
            "and universe.mode=derived_liquid"
        )
    if config.universe.expected_constituents is not None:
        raise TushareFetchError("derived liquid universe must not set expected_constituents")
    if config.data.adjustment != "backward" or not config.data.require_point_in_time_adjustment:
        raise TushareFetchError("historical all-A research requires backward point-in-time adjustment")


def _collect_stock_basic(
    client: TushareQueryClient,
    pacer: _EndpointPacer,
    path: Path,
) -> pl.DataFrame:
    if path.exists():
        frame = pl.read_parquet(path)
        if frame.is_empty():
            raise TushareFetchError("staged stock_basic is empty")
        return frame
    frames: list[pl.DataFrame] = []
    for status in _STOCK_BASIC_STATUSES:
        frame = _paced_query(
            client,
            pacer,
            "stock_basic",
            list_status=status,
            fields=_STOCK_BASIC_FIELDS,
        )
        if not frame.is_empty():
            frames.append(frame)
    if not frames:
        raise TushareFetchError("stock_basic returned no rows")
    combined = pl.concat(frames, how="diagonal_relaxed")
    if combined.group_by("ts_code").len().filter(pl.col("len") > 1).height:
        raise DataQualityError("stock_basic has duplicate ts_code across list-status queries")
    _write_parquet_atomic(path, combined)
    return combined


def _collect_reference(
    path: Path,
    query: Callable[[], pl.DataFrame],
    *,
    required_columns: tuple[str, ...],
) -> pl.DataFrame:
    frame = pl.read_parquet(path) if path.exists() else query()
    if frame.is_empty():
        raise TushareFetchError(f"{path.stem} returned no rows")
    missing = sorted(set(required_columns) - set(frame.columns))
    if missing:
        raise DataQualityError(f"{path.stem} missing columns: {missing}")
    if not path.exists():
        _write_parquet_atomic(path, frame)
    return frame


def _collect_namechange(
    client: TushareQueryClient,
    pacer: _EndpointPacer,
    path: Path,
    stocks: list[str],
) -> pl.DataFrame:
    if path.exists():
        frame = pl.read_parquet(path)
    else:
        frame = _query_paginated_reference(
            client,
            pacer,
            "namechange",
            page_size=5000,
            fields="ts_code,name,start_date,end_date,ann_date,change_reason",
        )
        if not frame.is_empty():
            frame = frame.filter(pl.col("ts_code").is_in(stocks))
        _write_parquet_atomic(path, frame)
    required = {"ts_code", "name", "start_date", "end_date"}
    missing = sorted(required - set(frame.columns))
    if frame.is_empty() or missing:
        detail = f" missing columns: {missing}" if missing else ""
        raise TushareFetchError(f"namechange returned no auditable history.{detail}")
    return frame


def _collect_legacy_suspend_fallback(
    client: TushareQueryClient,
    pacer: _EndpointPacer,
    root: Path,
    *,
    stock_basic: pl.DataFrame,
    stocks: list[str],
    target_days: list[date],
) -> None:
    interval_path = root / "reference" / "suspend_intervals.parquet"
    seed_path = root / "reference" / "suspend_seed_daily.parquet"
    unknown = _unknown_active_gaps(root, stock_basic=stock_basic, stocks=stocks, days=target_days)
    empty_intervals = pl.DataFrame(
        schema={
            "ts_code": pl.String,
            "suspend_date": pl.String,
            "resume_date": pl.String,
            "suspend_reason": pl.String,
        }
    )
    if not unknown:
        # Drop any stale legacy intervals from a prior incomplete attempt. When
        # suspend_d already explains every gap, retained null-resume rows would
        # incorrectly mark later trading days as suspended.
        _write_parquet_atomic(interval_path, empty_intervals)
        return

    if interval_path.exists():
        intervals = pl.read_parquet(interval_path)
    else:
        frames: list[pl.DataFrame] = []
        for symbol in sorted(unknown):
            frame = _paced_query(client, pacer, "suspend", ts_code=symbol)
            if not frame.is_empty():
                frames.append(frame)
        intervals = pl.concat(frames, how="diagonal_relaxed") if frames else empty_intervals
        _write_parquet_atomic(interval_path, intervals)
    _assert_intervals_cover_unknown_gaps(intervals, unknown)

    if seed_path.exists():
        return
    seed_frames: list[pl.DataFrame] = []
    for symbol, missing_days in sorted(unknown.items()):
        starts: list[date] = []
        for item in intervals.filter(pl.col("ts_code") == symbol).to_dicts():
            start_raw = item.get("suspend_date")
            if start_raw in (None, "", "None"):
                continue
            interval_start = parse_ymd(start_raw)
            resume_raw = item.get("resume_date")
            resume = (
                parse_ymd(resume_raw) if resume_raw not in (None, "", "None") else None
            )
            if any(
                interval_start <= day and (resume is None or day < resume)
                for day in missing_days
            ):
                starts.append(interval_start)
        if not starts:
            raise DataQualityError(f"suspend interval has no start date for {symbol}")
        seed_start = min(starts) - timedelta(days=_LOOKBACK_CALENDAR_DAYS)
        seed_end = min(missing_days) - timedelta(days=1)
        frame = _paced_query(
            client,
            pacer,
            "daily",
            ts_code=symbol,
            start_date=ymd(seed_start),
            end_date=ymd(seed_end),
        )
        if frame.is_empty():
            raise DataQualityError(
                f"cannot seed legacy suspension for {symbol}: no preceding daily close"
            )
        seed_frames.append(frame)
    seed = pl.concat(seed_frames, how="diagonal_relaxed")
    _write_parquet_atomic(seed_path, seed)


def _unknown_active_gaps(
    root: Path,
    *,
    stock_basic: pl.DataFrame,
    stocks: list[str],
    days: list[date],
) -> dict[str, list[date]]:
    date_names = {ymd(day) for day in days}
    daily_paths = [
        path
        for path in (root / "partitions" / "daily").glob("*.parquet")
        if path.stem in date_names
    ]
    daily = pl.scan_parquet([str(path) for path in sorted(daily_paths)]).select(
        ["ts_code", "trade_date"]
    ).collect()
    present = {
        (str(symbol), parse_ymd(day))
        for symbol, day in daily.iter_rows()
    }
    suspend_paths = [
        path
        for path in (root / "partitions" / "suspend_d").glob("*.parquet")
        if path.stem in date_names
    ]
    suspend = pl.scan_parquet([str(path) for path in sorted(suspend_paths)]).collect()
    known_suspended: set[tuple[str, date]] = set()
    if not suspend.is_empty():
        for item in suspend.to_dicts():
            if str(item.get("suspend_type") or "").upper() != "S":
                continue
            if not is_full_day_suspend_timing(item.get("suspend_timing")):
                continue
            known_suspended.add((str(item["ts_code"]), parse_ymd(item["trade_date"])))
    stock_set = set(stocks)
    by_code = {
        str(item["ts_code"]): item
        for item in stock_basic.to_dicts()
        if str(item.get("ts_code") or "") in stock_set
    }
    unknown: dict[str, list[date]] = {}
    for symbol in stocks:
        item = by_code[symbol]
        listed = parse_ymd(item["list_date"])
        raw_delisted = item.get("delist_date")
        delisted = (
            parse_ymd(raw_delisted) if raw_delisted not in (None, "", "None") else None
        )
        missing = [
            day
            for day in days
            if day >= listed
            and (delisted is None or day < delisted)
            and (symbol, day) not in present
            and (symbol, day) not in known_suspended
        ]
        if missing:
            unknown[symbol] = missing
    return unknown


def _assert_intervals_cover_unknown_gaps(
    intervals: pl.DataFrame,
    unknown: dict[str, list[date]],
) -> None:
    required = {"ts_code", "suspend_date", "resume_date"}
    missing_columns = sorted(required - set(intervals.columns))
    if intervals.is_empty() or missing_columns:
        detail = f" missing columns: {missing_columns}" if missing_columns else ""
        raise DataQualityError(f"legacy suspend returned no auditable intervals.{detail}")
    by_symbol: dict[str, list[tuple[date, date | None]]] = {}
    for item in intervals.to_dicts():
        start_raw = item.get("suspend_date")
        if start_raw in (None, "", "None"):
            continue
        resume_raw = item.get("resume_date")
        by_symbol.setdefault(str(item["ts_code"]), []).append(
            (
                parse_ymd(start_raw),
                parse_ymd(resume_raw) if resume_raw not in (None, "", "None") else None,
            )
        )
    for symbol, days in sorted(unknown.items()):
        periods = by_symbol.get(symbol, [])
        uncovered = [
            day
            for day in days
            if not any(start <= day and (resume is None or day < resume) for start, resume in periods)
        ]
        if uncovered:
            raise DataQualityError(
                f"unknown daily gap for {symbol} on {uncovered[0]} is not covered by official suspend interval"
            )


def _query_symbol_ranges(
    client: TushareQueryClient,
    pacer: _EndpointPacer,
    api_name: str,
    symbols: list[str],
    start: date,
    end: date,
) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for symbol in symbols:
        frame = _paced_query(
            client,
            pacer,
            api_name,
            ts_code=symbol,
            start_date=ymd(start),
            end_date=ymd(end),
        )
        if not frame.is_empty():
            frames.append(frame)
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


def _query_paginated_day(
    client: TushareQueryClient,
    pacer: _EndpointPacer,
    api_name: str,
    day: date,
    *,
    page_size: int,
    **extra: Any,
) -> pl.DataFrame:
    pages: list[pl.DataFrame] = []
    empty_result: pl.DataFrame | None = None
    offset = 0
    while True:
        frame = _paced_query(
            client,
            pacer,
            api_name,
            trade_date=ymd(day),
            limit=page_size,
            offset=offset,
            **extra,
        )
        if frame.is_empty():
            empty_result = frame
            break
        pages.append(frame)
        if frame.height < page_size:
            break
        offset += frame.height
    if not pages:
        if empty_result is not None and empty_result.width:
            return empty_result
        return _empty_day_frame(api_name)
    combined = pl.concat(pages, how="diagonal_relaxed")
    if "ts_code" in combined.columns:
        keys = ["ts_code"] + (["trade_date"] if "trade_date" in combined.columns else [])
        if combined.group_by(keys).len().filter(pl.col("len") > 1).height:
            raise DataQualityError(
                f"{api_name} pagination repeated rows on {day}; endpoint may have ignored offset"
            )
    return combined


def _query_paginated_reference(
    client: TushareQueryClient,
    pacer: _EndpointPacer,
    api_name: str,
    *,
    page_size: int,
    **extra: Any,
) -> pl.DataFrame:
    pages: list[pl.DataFrame] = []
    offset = 0
    while True:
        frame = _paced_query(
            client,
            pacer,
            api_name,
            limit=page_size,
            offset=offset,
            **extra,
        )
        if frame.is_empty():
            break
        if any(frame.equals(previous, null_equal=True) for previous in pages):
            raise DataQualityError(f"{api_name} pagination repeated a complete page; offset was ignored")
        pages.append(frame)
        if frame.height < page_size:
            break
        offset += frame.height
    if not pages:
        return pl.DataFrame()
    return pl.concat(pages, how="diagonal_relaxed")


def _empty_day_frame(api_name: str) -> pl.DataFrame:
    if api_name == "suspend_d":
        return pl.DataFrame(
            schema={
                "ts_code": pl.String,
                "trade_date": pl.String,
                "suspend_type": pl.String,
                "suspend_timing": pl.String,
            }
        )
    return pl.DataFrame()


def _paced_query(
    client: TushareQueryClient,
    pacer: _EndpointPacer,
    api_name: str,
    **params: Any,
) -> pl.DataFrame:
    pacer.wait(api_name)
    return client.query(api_name, **params)


def _validate_day_partition(frame: pl.DataFrame, api_name: str, day: date) -> None:
    # An empty daily/supplementary response can be valid for event-only APIs,
    # but daily, adj_factor, daily_basic and stk_limit cannot be empty on an
    # open market day after all-A filtering.
    if frame.is_empty():
        if api_name in {"daily", "adj_factor", "daily_basic", "stk_limit"}:
            raise DataQualityError(f"{api_name} returned no selected A-share rows on open day {day}")
        return
    if "trade_date" not in frame.columns:
        raise DataQualityError(f"{api_name} missing trade_date on {day}")
    values = {parse_ymd(value) for value in frame["trade_date"].to_list()}
    if values != {day}:
        raise DataQualityError(f"{api_name} partition {day} contains dates {sorted(values)}")
    if "ts_code" in frame.columns:
        dups = frame.group_by(["ts_code", "trade_date"]).len().filter(pl.col("len") > 1)
        if dups.height:
            raise DataQualityError(f"{api_name} has duplicate (ts_code, trade_date) rows on {day}")


def _read_partitions(root: Path, api_name: str) -> pl.DataFrame:
    paths = sorted((root / "partitions" / api_name).glob("*.parquet"))
    if not paths:
        raise TushareFetchError(f"collection has no {api_name} partitions")
    try:
        scans: list[pl.LazyFrame] = []
        for path in paths:
            scan = pl.scan_parquet(path)
            if api_name == "stk_limit":
                # Tushare/Pandas inferred pre_close as object/string on one
                # real date (2024-07-23) even though every non-null value was
                # numeric. Cast strictly while reading; malformed text still
                # fails instead of being converted to null.
                scan = scan.with_columns(
                    [
                        pl.col("pre_close").cast(pl.Float64, strict=True),
                        pl.col("up_limit").cast(pl.Float64, strict=True),
                        pl.col("down_limit").cast(pl.Float64, strict=True),
                    ]
                )
            scans.append(scan)
        return pl.concat(scans, how="vertical").collect()
    except Exception as exc:
        raise DataQualityError(f"cannot read consistent {api_name} partitions") from exc


def _build_quality_report(
    root: Path,
    stocks: list[str],
    source_days: list[date],
    target_days: list[date],
) -> dict[str, Any]:
    expected = {
        api: len(source_days if bool(spec["source_history"]) else target_days)
        for api, spec in _DAY_APIS.items()
    }
    actual = {
        api: len(list((root / "partitions" / api).glob("*.parquet"))) for api in _DAY_APIS
    }
    missing = {api: expected[api] - actual[api] for api in _DAY_APIS if expected[api] != actual[api]}
    if missing:
        raise TushareFetchError(f"collection is incomplete: {missing}")
    return {
        "schema_version": _SCHEMA_VERSION,
        "complete": True,
        "selected_stocks": len(stocks),
        "source_dates": len(source_days),
        "trading_days": len(target_days),
        "expected_partitions": expected,
        "actual_partitions": actual,
    }


def _verify_collection_manifest(root: Path, *, request_id: str) -> None:
    manifest = _read_json(root / "collection_manifest.json", "collection_manifest.json")
    if manifest.get("request_id") != request_id:
        raise TushareFetchError("collection manifest request ID does not match")
    if manifest.get("dataset_hashes") != _dataset_hashes(root):
        raise TushareFetchError("collection manifest hashes do not match staged parquet bytes")
    quality_path = root / "quality_report.json"
    if manifest.get("quality_report_sha256") != _sha256_file(quality_path):
        raise TushareFetchError("collection quality report hash does not match")


def _dataset_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    paths = sorted((root / "reference").glob("*.parquet"))
    paths.extend(sorted((root / "partitions").glob("*/*.parquet")))
    grouped: dict[str, list[Path]] = {}
    for path in paths:
        key = path.parent.name if path.parent.name != "reference" else f"reference/{path.stem}"
        grouped.setdefault(key, []).append(path)
    for key, files in sorted(grouped.items()):
        digest = hashlib.sha256()
        for path in sorted(files):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(_sha256_file(path).encode("ascii"))
            digest.update(b"\n")
        out[key] = digest.hexdigest()
    return out


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
