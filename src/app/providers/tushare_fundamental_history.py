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
from app.models.fundamentals import FundamentalSnapshot
from app.providers.tushare_client import TushareQueryClient
from app.providers.tushare_fundamentals import (
    REPORT_FIELDS,
    REPORT_METRICS,
    VALUATION_FIELDS,
    normalize_daily_valuation,
    normalize_fundamental_reports,
)
from app.providers.tushare_normalize import require_ts_code, ymd
from app.storage.fundamental_io import (
    REPORT_AVAILABILITY_POLICY,
    VALUATION_AVAILABILITY_POLICY,
    build_fundamental_snapshot,
    write_fundamental_snapshot_atomically,
)
from app.storage.snapshot_io import load_verified_snapshot

_SCHEMA_VERSION = "1"
_REQUEST_INTERVAL_SECONDS = 0.31
_DAILY_BASIC_PAGE_SIZE = 6000
_FINA_INDICATOR_MAX_ROWS = 100


@dataclass(frozen=True)
class FundamentalHistoryCollectionResult:
    staging_dir: Path
    request_id: str
    base_market_snapshot_id: str
    report_period_start: date
    coverage_start: date
    coverage_end: date
    trading_days: int
    requested_stocks: int
    completed_partitions: int
    reused_partitions: int
    collection_manifest_path: Path
    quality_report_path: Path


@dataclass(frozen=True)
class FundamentalHistoryMaterializeResult:
    snapshot: FundamentalSnapshot
    requested_stocks: int
    covered_report_symbols: int
    covered_valuation_symbols: int
    report_rows: int
    valuation_rows: int


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


def collect_tushare_all_a_share_fundamentals(
    *,
    client: TushareQueryClient,
    market_dir: Path,
    config: StrategyConfig,
    start: date,
    end: date,
    staging_dir: Path,
    progress: Callable[[str, int, int, bool], None] | None = None,
) -> FundamentalHistoryCollectionResult:
    """Collect a resumable full-market normalized fundamental staging set."""
    _require_config(config)
    if end < start:
        raise TushareFetchError("end date must be on or after start date")
    market_snapshot = load_verified_snapshot(Path(market_dir))
    if market_snapshot.adjustment != config.data.adjustment:
        raise TushareFetchError("market snapshot adjustment does not match strategy config")
    if (
        market_snapshot.coverage_start is None
        or market_snapshot.coverage_end is None
        or start < market_snapshot.coverage_start
        or end > market_snapshot.coverage_end
    ):
        raise TushareFetchError("fundamental request is outside the verified market snapshot coverage")
    stocks = _market_stocks(Path(market_dir))
    days = _market_days(Path(market_dir), start, end)
    fundamental_config = config.fundamental
    if fundamental_config is None:
        raise TushareFetchError("full-market fundamentals require an enabled fundamental config")
    report_period_start = start - timedelta(days=fundamental_config.max_report_age_days)
    root = Path(staging_dir)
    root.mkdir(parents=True, exist_ok=True)
    request_payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "report_period_start": report_period_start.isoformat(),
        "strategy_config_hash": config.config_hash(),
        "base_market_snapshot_id": market_snapshot.snapshot_id,
        "symbols_sha256": _symbols_sha256(stocks),
        "requested_stocks": len(stocks),
        "trading_days": len(days),
        "report_fields": REPORT_FIELDS,
        "valuation_fields": VALUATION_FIELDS,
        "report_availability_policy": REPORT_AVAILABILITY_POLICY,
        "valuation_availability_policy": VALUATION_AVAILABILITY_POLICY,
    }
    request_id = _json_sha256(request_payload)
    request_path = root / "collection_request.json"
    expected_request = {**request_payload, "request_id": request_id}
    if request_path.exists():
        if _read_json(request_path, "collection_request.json") != expected_request:
            raise TushareFetchError(
                "staging directory belongs to a different fundamental request; use a new --staging-dir"
            )
    else:
        _write_json_atomic(request_path, expected_request)
    if (root / "collection_manifest.json").exists():
        _verify_collection_manifest(root, request_id=request_id)

    pacer = _EndpointPacer(client)
    total = len(stocks) + len(days)
    done = 0
    completed = 0
    reused = 0
    for symbol in stocks:
        path = root / "partitions" / "fundamental_reports" / f"{symbol.replace('.', '_')}.parquet"
        if path.exists():
            _validate_report_partition(pl.read_parquet(path), symbol, report_period_start, end)
            reused += 1
            was_reused = True
        else:
            pacer.wait("fina_indicator")
            raw = client.query(
                "fina_indicator",
                ts_code=symbol,
                start_date=ymd(report_period_start),
                end_date=ymd(end),
                fields=REPORT_FIELDS,
            )
            if raw.height >= _FINA_INDICATOR_MAX_ROWS:
                raise DataQualityError(
                    f"fina_indicator returned {_FINA_INDICATOR_MAX_ROWS} rows for {symbol}; "
                    "the response may be truncated"
                )
            frame = normalize_fundamental_reports(raw) if not raw.is_empty() else _empty_reports()
            _validate_report_partition(frame, symbol, report_period_start, end)
            _write_parquet_atomic(path, frame)
            completed += 1
            was_reused = False
        done += 1
        if progress is not None:
            progress("fina_indicator", done, total, was_reused)

    stock_set = set(stocks)
    for day in days:
        path = root / "partitions" / "daily_valuation" / f"{ymd(day)}.parquet"
        if path.exists():
            _validate_valuation_partition(pl.read_parquet(path), day, stock_set)
            reused += 1
            was_reused = True
        else:
            raw = _query_daily_basic(client, pacer, day)
            if not raw.is_empty():
                raw = raw.filter(pl.col("ts_code").is_in(stocks))
            if raw.is_empty():
                raise DataQualityError(f"daily_basic returned no selected A-share rows on {day}")
            frame = normalize_daily_valuation(raw)
            _validate_valuation_partition(frame, day, stock_set)
            _write_parquet_atomic(path, frame)
            completed += 1
            was_reused = False
        done += 1
        if progress is not None:
            progress("daily_basic", done, total, was_reused)

    quality = _build_quality_report(root, stocks, days)
    quality_path = root / "quality_report.json"
    _write_json_atomic(quality_path, quality)
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "request_id": request_id,
        "source_name": "tushare_all_a_share_fundamentals",
        "collected_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_market_snapshot_id": market_snapshot.snapshot_id,
        "coverage": {"start": start.isoformat(), "end": end.isoformat()},
        "report_period_start": report_period_start.isoformat(),
        "requested_stocks": len(stocks),
        "trading_days": len(days),
        "dataset_hashes": _dataset_hashes(root),
        "quality_report_sha256": _sha256_file(quality_path),
        "normalization": "each partition normalized before atomic persistence",
        "revision_contract": (
            "update_flag=0 is provable initial-as-announced; strict strategy policy excludes "
            "revision-only groups without a revision publication timestamp"
        ),
    }
    manifest_path = root / "collection_manifest.json"
    _write_json_atomic(manifest_path, manifest)
    return FundamentalHistoryCollectionResult(
        staging_dir=root,
        request_id=request_id,
        base_market_snapshot_id=market_snapshot.snapshot_id,
        report_period_start=report_period_start,
        coverage_start=start,
        coverage_end=end,
        trading_days=len(days),
        requested_stocks=len(stocks),
        completed_partitions=completed,
        reused_partitions=reused,
        collection_manifest_path=manifest_path,
        quality_report_path=quality_path,
    )


def materialize_tushare_all_a_share_fundamentals(
    *,
    staging_dir: Path,
    market_dir: Path,
    config: StrategyConfig,
    dest_dir: Path,
    source_version: str | None = None,
    replace_existing: bool = False,
) -> FundamentalHistoryMaterializeResult:
    """Verify every staged byte and atomically build the bound overlay."""
    _require_config(config)
    root = Path(staging_dir)
    request = _read_json(root / "collection_request.json", "collection_request.json")
    manifest = _read_json(root / "collection_manifest.json", "collection_manifest.json")
    request_id = str(request.get("request_id") or "")
    if not request_id or manifest.get("request_id") != request_id:
        raise TushareFetchError("fundamental collection request and manifest IDs do not match")
    if request.get("strategy_config_hash") != config.config_hash():
        raise TushareFetchError("fundamental collection strategy config hash does not match")
    _verify_collection_manifest(root, request_id=request_id)
    market_snapshot = load_verified_snapshot(Path(market_dir))
    if request.get("base_market_snapshot_id") != market_snapshot.snapshot_id:
        raise TushareFetchError("fundamental collection belongs to a different market snapshot")

    report_paths = sorted((root / "partitions" / "fundamental_reports").glob("*.parquet"))
    valuation_paths = sorted((root / "partitions" / "daily_valuation").glob("*.parquet"))
    if not report_paths or not valuation_paths:
        raise TushareFetchError("fundamental collection has missing partition families")
    reports = pl.scan_parquet([str(path) for path in report_paths]).collect()
    valuation = pl.scan_parquet([str(path) for path in valuation_paths]).collect()
    if reports.is_empty():
        raise DataQualityError("full-market fundamental collection has no report rows")
    if valuation.is_empty():
        raise DataQualityError("full-market fundamental collection has no valuation rows")
    _require_unique(reports, ["symbol", "report_period", "ann_date", "update_flag"], "fundamental_reports")
    _require_unique(valuation, ["symbol", "date"], "daily_valuation")
    requested = int(request["requested_stocks"])
    tables = {"fundamental_reports": reports, "daily_valuation": valuation}
    snapshot = build_fundamental_snapshot(
        tables,
        source_name="tushare_all_a_share_fundamentals",
        source_version=source_version or request_id,
        base_market_snapshot_id=market_snapshot.snapshot_id,
        collection_request_id=request_id,
        requested_symbols=requested,
    )
    write_fundamental_snapshot_atomically(
        Path(dest_dir),
        tables,
        snapshot,
        replace_existing=replace_existing,
    )
    return FundamentalHistoryMaterializeResult(
        snapshot=snapshot,
        requested_stocks=requested,
        covered_report_symbols=int(reports["symbol"].n_unique()),
        covered_valuation_symbols=int(valuation["symbol"].n_unique()),
        report_rows=reports.height,
        valuation_rows=valuation.height,
    )


def _require_config(config: StrategyConfig) -> None:
    if config.research_scope != "historical_all_a_share" or config.universe.mode != "derived_liquid":
        raise TushareFetchError("full-market fundamentals require historical_all_a_share derived_liquid")
    if config.fundamental is None or not config.fundamental.required:
        raise TushareFetchError("full-market fundamentals require an enabled fundamental config")


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


def _market_days(market_dir: Path, start: date, end: date) -> list[date]:
    frame = pl.read_parquet(market_dir / "calendar.parquet")
    if "date" not in frame.columns:
        raise DataQualityError("market calendar missing date")
    days = sorted(
        value for value in frame["date"].to_list() if isinstance(value, date) and start <= value <= end
    )
    if not days or days[0] != start or days[-1] != end:
        raise DataQualityError("requested fundamental bounds must be open days in the market snapshot")
    return days


def _query_daily_basic(
    client: TushareQueryClient,
    pacer: _EndpointPacer,
    day: date,
) -> pl.DataFrame:
    pages: list[pl.DataFrame] = []
    offset = 0
    while True:
        pacer.wait("daily_basic")
        frame = client.query(
            "daily_basic",
            trade_date=ymd(day),
            fields=VALUATION_FIELDS,
            limit=_DAILY_BASIC_PAGE_SIZE,
            offset=offset,
        )
        if frame.is_empty():
            break
        if any(frame.equals(previous, null_equal=True) for previous in pages):
            raise DataQualityError(f"daily_basic pagination ignored offset on {day}")
        pages.append(frame)
        if frame.height < _DAILY_BASIC_PAGE_SIZE:
            break
        offset += frame.height
    return pl.concat(pages, how="diagonal_relaxed") if pages else pl.DataFrame()


def _validate_report_partition(
    frame: pl.DataFrame,
    symbol: str,
    start: date,
    end: date,
) -> None:
    if frame.is_empty():
        return
    required = {"symbol", "report_period", "ann_date", "update_flag", "source_row_hash"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataQualityError(f"fundamental report partition missing columns: {missing}")
    if set(frame["symbol"].to_list()) != {symbol}:
        raise DataQualityError(f"fundamental report partition contains another symbol: {symbol}")
    periods = frame["report_period"].to_list()
    if any(not isinstance(value, date) or value < start or value > end for value in periods):
        raise DataQualityError(f"fundamental report partition is outside requested periods: {symbol}")
    _require_unique(frame, ["symbol", "report_period", "ann_date", "update_flag"], symbol)


def _validate_valuation_partition(frame: pl.DataFrame, day: date, stocks: set[str]) -> None:
    required = {"symbol", "date", "available_at", "source_row_hash"}
    missing = sorted(required - set(frame.columns))
    if frame.is_empty() or missing:
        detail = f" missing columns: {missing}" if missing else ""
        raise DataQualityError(f"daily valuation partition is empty or invalid on {day}.{detail}")
    values = set(frame["date"].to_list())
    if values != {day}:
        raise DataQualityError(f"daily valuation partition {day} contains dates {sorted(values)}")
    foreign = set(frame["symbol"].to_list()) - stocks
    if foreign:
        raise DataQualityError(f"daily valuation partition {day} contains unknown symbol {sorted(foreign)[0]}")
    _require_unique(frame, ["symbol", "date"], f"daily_valuation/{day}")


def _require_unique(frame: pl.DataFrame, keys: list[str], table: str) -> None:
    duplicate = frame.group_by(keys).len().filter(pl.col("len") > 1)
    if duplicate.height:
        raise DataQualityError(f"{table} has duplicate logical keys: {duplicate.head(1).to_dicts()[0]}")


def _empty_reports() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "report_period": pl.Date,
            "ann_date": pl.Date,
            "update_flag": pl.String,
            "available_at": pl.Datetime("us"),
            **{name: pl.Float64 for name in REPORT_METRICS},
            "source_row_hash": pl.String,
        }
    )


def _build_quality_report(root: Path, stocks: list[str], days: list[date]) -> dict[str, Any]:
    report_paths = sorted((root / "partitions" / "fundamental_reports").glob("*.parquet"))
    valuation_paths = sorted((root / "partitions" / "daily_valuation").glob("*.parquet"))
    expected_report_names = {f"{symbol.replace('.', '_')}.parquet" for symbol in stocks}
    expected_valuation_names = {f"{ymd(day)}.parquet" for day in days}
    if {path.name for path in report_paths} != expected_report_names:
        raise TushareFetchError("fundamental report partition set is incomplete or contains extras")
    if {path.name for path in valuation_paths} != expected_valuation_names:
        raise TushareFetchError("daily valuation partition set is incomplete or contains extras")
    reports = pl.scan_parquet([str(path) for path in report_paths]).collect()
    valuation = pl.scan_parquet([str(path) for path in valuation_paths]).collect()
    if valuation.is_empty():
        raise DataQualityError("daily valuation collection is empty")
    report_symbols = int(reports["symbol"].n_unique()) if not reports.is_empty() else 0
    valuation_symbols = int(valuation["symbol"].n_unique())
    strict_initial_symbols = 0
    revision_only_groups = 0
    if not reports.is_empty():
        grouped = reports.group_by(["symbol", "report_period", "ann_date"]).agg(
            pl.col("update_flag").eq("0").any().alias("has_initial"),
            pl.col("update_flag").eq("1").any().alias("has_revision"),
        )
        revision_only_groups = grouped.filter(~pl.col("has_initial") & pl.col("has_revision")).height
        strict_initial_symbols = int(reports.filter(pl.col("update_flag") == "0")["symbol"].n_unique())
    valuation_start = valuation["date"].min()
    valuation_end = valuation["date"].max()
    if not isinstance(valuation_start, date) or not isinstance(valuation_end, date):
        raise DataQualityError("daily valuation coverage dates are invalid")
    return {
        "schema_version": _SCHEMA_VERSION,
        "complete": True,
        "requested_stocks": len(stocks),
        "trading_days": len(days),
        "report_partitions": len(report_paths),
        "valuation_partitions": len(valuation_paths),
        "report_rows": reports.height,
        "valuation_rows": valuation.height,
        "covered_report_symbols": report_symbols,
        "strict_initial_report_symbols": strict_initial_symbols,
        "revision_only_groups_excluded_by_strict_policy": revision_only_groups,
        "covered_valuation_symbols": valuation_symbols,
        "valuation_coverage": {
            "start": valuation_start.isoformat(),
            "end": valuation_end.isoformat(),
        },
    }


def _verify_collection_manifest(root: Path, *, request_id: str) -> None:
    manifest = _read_json(root / "collection_manifest.json", "collection_manifest.json")
    if manifest.get("request_id") != request_id:
        raise TushareFetchError("fundamental collection manifest request ID does not match")
    if manifest.get("dataset_hashes") != _dataset_hashes(root):
        raise TushareFetchError("fundamental collection manifest hashes do not match staged parquet bytes")
    quality_path = root / "quality_report.json"
    if manifest.get("quality_report_sha256") != _sha256_file(quality_path):
        raise TushareFetchError("fundamental collection quality report hash does not match")


def _dataset_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for family in ("fundamental_reports", "daily_valuation"):
        digest = hashlib.sha256()
        for path in sorted((root / "partitions" / family).glob("*.parquet")):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(_sha256_file(path).encode("ascii"))
            digest.update(b"\n")
        out[family] = digest.hexdigest()
    return out


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
