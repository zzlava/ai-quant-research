from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import polars as pl

from app.errors import DataQualityError, MissingBenchmarkError

REQUIRED_OHLCV = ("symbol", "date", "open", "high", "low", "close", "volume", "amount")
REQUIRED_GLOBAL = ("symbol", "date", "close", "available_at")
PRICE_COLS = ("open", "high", "low", "close", "volume", "amount")


def _non_finite_mask(frame: pl.DataFrame, columns: tuple[str, ...]) -> pl.Expr | None:
    masks: list[pl.Expr] = []
    for col in columns:
        if col not in frame.columns:
            continue
        expr = pl.col(col)
        masks.append(expr.is_null() | expr.is_nan() | expr.is_infinite())
    if not masks:
        return None
    out = masks[0]
    for extra in masks[1:]:
        out = out | extra
    return out


def validate_ohlcv(frame: pl.DataFrame, name: str, calendar: list | None = None) -> None:
    if frame.is_empty():
        raise DataQualityError(f"{name} has no rows")
    missing = [col for col in REQUIRED_OHLCV if col not in frame.columns]
    if missing:
        raise DataQualityError(f"{name} missing columns: {missing}")
    dups = frame.group_by(["symbol", "date"]).len().filter(pl.col("len") > 1)
    if dups.height:
        raise DataQualityError(f"{name} has duplicate (symbol, date) rows")
    non_finite = _non_finite_mask(frame, PRICE_COLS)
    finite_bad = frame.filter(non_finite) if non_finite is not None else frame.clear()
    if finite_bad.height:
        raise DataQualityError(f"{name} has non-finite price, volume, or amount values")
    bad = frame.filter(
        (pl.col("open") <= 0)
        | (pl.col("high") <= 0)
        | (pl.col("low") <= 0)
        | (pl.col("close") <= 0)
        | (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("low") > pl.min_horizontal("open", "close"))
        | (pl.col("volume") < 0)
        | (pl.col("amount") < 0)
    )
    if bad.height:
        sample = bad.select(["symbol", "date"]).head(3).to_dicts()
        raise DataQualityError(f"{name} has invalid OHLC rows, e.g. {sample}")
    if "price_limit_pct" in frame.columns:
        limit = pl.col("price_limit_pct")
        bad_limit = frame.filter(limit.is_not_null() & (limit.is_nan() | limit.is_infinite() | (limit < 0)))
        if bad_limit.height:
            raise DataQualityError(f"{name} has invalid price_limit_pct")
    if calendar:
        cal = set(calendar)
        present = set(frame["date"].unique().to_list())
        missing_days = sorted(cal - present)
        if missing_days:
            raise DataQualityError(f"{name} missing calendar dates, first={missing_days[0]}")


def validate_global(frame: pl.DataFrame, name: str = "global_bars") -> None:
    if frame.is_empty():
        raise DataQualityError(f"{name} has no rows")
    missing = [col for col in REQUIRED_GLOBAL if col not in frame.columns]
    if missing:
        raise DataQualityError(f"{name} missing columns: {missing}")
    dups = frame.group_by(["symbol", "date"]).len().filter(pl.col("len") > 1)
    if dups.height:
        raise DataQualityError(f"{name} has duplicate (symbol, date) rows")
    close = pl.col("close")
    bad_close = frame.filter(close.is_null() | close.is_nan() | close.is_infinite() | (close <= 0))
    if bad_close.height:
        raise DataQualityError(f"{name} has missing or non-finite close")
    if frame["available_at"].null_count() > 0:
        raise DataQualityError(f"{name} has missing available_at")
    _assert_available_at_utc(frame, name)


def parse_available_at_utc(value: Any, name: str = "global_bars") -> datetime:
    """Accept naive UTC or Z/+00:00. Reject missing values and non-zero offsets."""
    if value is None:
        raise DataQualityError(f"{name} has missing available_at")
    if isinstance(value, datetime):
        return _require_utc_datetime(value, name)
    text = str(value).strip()
    if not text or text in {"null", "None", "NA"}:
        raise DataQualityError(f"{name} has missing available_at")
    normalized = text.replace("z", "Z")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DataQualityError(f"{name} available_at is not a comparable UTC datetime") from exc
    return _require_utc_datetime(parsed, name)


def normalize_available_at(frame: pl.DataFrame, name: str = "global_bars") -> pl.DataFrame:
    """Parse available_at before any naive Datetime cast can drop a timezone offset."""
    if "available_at" not in frame.columns:
        raise DataQualityError(f"{name} missing available_at")
    dtype = frame["available_at"].dtype
    if isinstance(dtype, pl.Datetime):
        tz = dtype.time_zone
        if tz not in (None, "UTC", "utc"):
            raise DataQualityError(f"{name} available_at must be UTC; non-zero offsets are rejected")
        if tz in ("UTC", "utc"):
            frame = frame.with_columns(pl.col("available_at").dt.replace_time_zone(None))
    parsed = [parse_available_at_utc(value, name=name) for value in frame["available_at"].to_list()]
    return frame.with_columns(pl.Series("available_at", parsed, dtype=pl.Datetime("us")))


def _require_utc_datetime(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        return value
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise DataQualityError(f"{name} available_at must be UTC; non-zero offsets are rejected")
    return value.astimezone(UTC).replace(tzinfo=None)


def _assert_available_at_utc(frame: pl.DataFrame, name: str) -> None:
    dtype = frame["available_at"].dtype
    if isinstance(dtype, pl.Datetime) and dtype.time_zone not in (None, "UTC", "utc"):
        raise DataQualityError(f"{name} available_at must be UTC; non-zero offsets are rejected")
    values = frame["available_at"].to_list()
    for value in values:
        parse_available_at_utc(value, name=name)


def validate_instruments(frame: pl.DataFrame, name: str = "instruments") -> None:
    if frame.is_empty():
        raise DataQualityError(f"{name} has no rows")
    if "symbol" not in frame.columns:
        raise DataQualityError(f"{name} missing symbol")
    dups = frame.group_by("symbol").len().filter(pl.col("len") > 1)
    if dups.height:
        raise DataQualityError(f"{name} has duplicate symbols")


def validate_calendar(frame: pl.DataFrame, name: str = "calendar") -> None:
    if frame.is_empty():
        raise DataQualityError(f"{name} has no rows")
    if "date" not in frame.columns:
        raise DataQualityError(f"{name} missing date")
    dups = frame.group_by("date").len().filter(pl.col("len") > 1)
    if dups.height:
        raise DataQualityError(f"{name} has duplicate dates")


def assert_benchmarks(
    index: pl.DataFrame,
    global_bars: pl.DataFrame,
    market_index: str | None,
    global_symbol: str | None,
) -> None:
    if market_index:
        symbols = set(index["symbol"].to_list()) if "symbol" in index.columns else set()
        if market_index not in symbols:
            raise MissingBenchmarkError(f"market index '{market_index}' is not in index_bars")
    if global_symbol:
        symbols = set(global_bars["symbol"].to_list()) if "symbol" in global_bars.columns else set()
        if global_symbol not in symbols:
            raise MissingBenchmarkError(f"global series '{global_symbol}' is not in global_bars")
