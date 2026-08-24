from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime, time
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast
from zoneinfo import ZoneInfo

import polars as pl

from app.errors import DataQualityError, TushareFetchError
from app.models.fundamentals import FundamentalSnapshot
from app.providers.tushare_client import TushareQueryClient
from app.providers.tushare_normalize import require_ts_code
from app.storage.fundamental_io import (
    build_fundamental_snapshot,
    require_no_conflicting_duplicates,
    source_row_hash,
    write_fundamental_snapshot_atomically,
)

REPORT_FIELDS = (
    "ts_code,ann_date,end_date,update_flag,roe,roic,grossprofit_margin,"
    "debt_to_assets,ocf_to_or,q_sales_yoy,q_netprofit_yoy,dt_netprofit_yoy"
)
VALUATION_FIELDS = "ts_code,trade_date,turnover_rate,pe_ttm,pb,ps_ttm,total_mv,circ_mv"
REPORT_METRICS = (
    "roe",
    "roic",
    "grossprofit_margin",
    "debt_to_assets",
    "ocf_to_or",
    "q_sales_yoy",
    "q_netprofit_yoy",
    "dt_netprofit_yoy",
)
VALUATION_METRICS = ("turnover_rate", "pe_ttm", "pb", "ps_ttm", "total_mv", "circ_mv")
_MIN_REQUEST_INTERVAL_SECONDS = 0.31


def fetch_tushare_fundamentals(
    *,
    client: TushareQueryClient,
    symbols: list[str],
    start: date,
    end: date,
    dest_dir: Path,
    source_version: str | None = None,
    replace_existing: bool = False,
    pace_requests: bool | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> FundamentalSnapshot:
    """Fetch and atomically persist a separately hashed PIT overlay."""
    if end < start:
        raise TushareFetchError("end date must be on or after start date")
    codes = list(dict.fromkeys(require_ts_code(code, kind="stock") for code in symbols))
    if not codes:
        raise TushareFetchError("fundamental symbol universe is empty")
    pace = (
        bool(getattr(client, "requires_single_code_rate_limit", False))
        if pace_requests is None
        else pace_requests
    )
    report_frames: list[pl.DataFrame] = []
    valuation_frames: list[pl.DataFrame] = []
    next_request_at: dict[str, float] = {}
    for index, symbol in enumerate(codes, start=1):
        report_frames.append(
            _query_one(
                client,
                "fina_indicator",
                next_request_at,
                pace,
                ts_code=symbol,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                fields=REPORT_FIELDS,
            )
        )
        valuation_frames.append(
            _query_one(
                client,
                "daily_basic",
                next_request_at,
                pace,
                ts_code=symbol,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                fields=VALUATION_FIELDS,
            )
        )
        if progress is not None:
            progress(index, len(codes))
    reports = normalize_fundamental_reports(_concat_nonempty(report_frames))
    valuation = normalize_daily_valuation(_concat_nonempty(valuation_frames))
    tables = {"fundamental_reports": reports, "daily_valuation": valuation}
    snapshot = build_fundamental_snapshot(
        tables,
        source_name="tushare",
        source_version=source_version,
    )
    write_fundamental_snapshot_atomically(
        dest_dir,
        tables,
        snapshot,
        replace_existing=replace_existing,
    )
    return snapshot


def normalize_fundamental_reports(raw: pl.DataFrame) -> pl.DataFrame:
    if raw.is_empty():
        raise DataQualityError("fundamental_reports returned no rows")
    required = ("ts_code", "ann_date", "end_date")
    missing = [name for name in required if name not in raw.columns]
    if missing:
        raise DataQualityError(f"fundamental_reports missing required columns: {missing}")
    rows: list[dict[str, object]] = []
    for line, item in enumerate(raw.iter_rows(named=True), start=1):
        row: dict[str, object] = {
            "symbol": require_ts_code(str(item.get("ts_code") or ""), kind="stock"),
            "report_period": _parse_ymd(item.get("end_date"), "end_date", line),
            "ann_date": _parse_ymd(item.get("ann_date"), "ann_date", line),
            "update_flag": str(item.get("update_flag") or "0").strip(),
        }
        row["available_at"] = _available_at(row["ann_date"], time(23, 59))
        for metric in REPORT_METRICS:
            row[metric] = _optional_number(item.get(metric), metric, line)
        row["source_row_hash"] = source_row_hash(
            row,
            (
                "symbol",
                "report_period",
                "ann_date",
                "update_flag",
                *REPORT_METRICS,
            ),
        )
        rows.append(row)
    frame = pl.DataFrame(rows).with_columns(
        [
            pl.col("report_period").cast(pl.Date),
            pl.col("ann_date").cast(pl.Date),
            pl.col("available_at").cast(pl.Datetime("us")),
            *[pl.col(name).cast(pl.Float64) for name in REPORT_METRICS],
        ]
    )
    return require_no_conflicting_duplicates(
        frame,
        key=["symbol", "report_period", "ann_date", "update_flag"],
        table="fundamental_reports",
    )


def normalize_daily_valuation(raw: pl.DataFrame) -> pl.DataFrame:
    if raw.is_empty():
        raise DataQualityError("daily_valuation returned no rows")
    required = ("ts_code", "trade_date")
    missing = [name for name in required if name not in raw.columns]
    if missing:
        raise DataQualityError(f"daily_valuation missing required columns: {missing}")
    rows: list[dict[str, object]] = []
    for line, item in enumerate(raw.iter_rows(named=True), start=1):
        row: dict[str, object] = {
            "symbol": require_ts_code(str(item.get("ts_code") or ""), kind="stock"),
            "date": _parse_ymd(item.get("trade_date"), "trade_date", line),
        }
        row["available_at"] = _available_at(row["date"], time(17, 0))
        for metric in VALUATION_METRICS:
            row[metric] = _optional_number(item.get(metric), metric, line)
        row["source_row_hash"] = source_row_hash(
            row,
            ("symbol", "date", *VALUATION_METRICS),
        )
        rows.append(row)
    frame = pl.DataFrame(rows).with_columns(
        [
            pl.col("date").cast(pl.Date),
            pl.col("available_at").cast(pl.Datetime("us")),
            *[pl.col(name).cast(pl.Float64) for name in VALUATION_METRICS],
        ]
    )
    return require_no_conflicting_duplicates(
        frame,
        key=["symbol", "date"],
        table="daily_valuation",
    )


def _query_one(
    client: TushareQueryClient,
    api_name: str,
    next_request_at: dict[str, float],
    pace: bool,
    **params: Any,
) -> pl.DataFrame:
    if pace:
        ready = next_request_at.get(api_name)
        if ready is not None:
            delay = ready - monotonic()
            if delay > 0:
                sleep(delay)
        next_request_at[api_name] = monotonic() + _MIN_REQUEST_INTERVAL_SECONDS
    return client.query(api_name, **params)


def _concat_nonempty(frames: list[pl.DataFrame]) -> pl.DataFrame:
    values = [frame for frame in frames if not frame.is_empty()]
    return pl.concat(values, how="diagonal_relaxed") if values else pl.DataFrame()


def _parse_ymd(value: object, name: str, line: int) -> date:
    text = str(value or "").strip().replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise DataQualityError(f"{name} is invalid at source row {line}") from exc


def _available_at(value: object, at: time) -> datetime:
    if not isinstance(value, date):
        raise DataQualityError("availability date is invalid")
    local = datetime.combine(value, at, tzinfo=ZoneInfo("Asia/Shanghai"))
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _optional_number(value: object, name: str, line: int) -> float | None:
    if value is None or str(value).strip() in {"", "nan", "None", "null"}:
        return None
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"{name} is not numeric at source row {line}") from exc
    if not math.isfinite(number):
        raise DataQualityError(f"{name} is not finite at source row {line}")
    return number
