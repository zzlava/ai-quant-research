from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from app.models.snapshot import SCHEMA_VERSION, TABLE_NAMES, DataSnapshot

HASH_COLUMNS: dict[str, tuple[str, ...]] = {
    "daily_bars": (
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turnover_rate",
        "is_st",
        "is_suspended",
        "price_limit_pct",
    ),
    "index_bars": (
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turnover_rate",
        "is_st",
        "is_suspended",
        "price_limit_pct",
    ),
    "global_bars": ("symbol", "date", "close", "available_at"),
    "instruments": (
        "symbol",
        "name",
        "sector",
        "listing_date",
        "is_index",
        "is_global",
        "market",
        "timezone",
        "session_close",
    ),
    "calendar": ("date",),
}

SORT_KEYS: dict[str, tuple[str, ...]] = {
    "daily_bars": ("symbol", "date"),
    "index_bars": ("symbol", "date"),
    "global_bars": ("symbol", "date"),
    "instruments": ("symbol",),
    "calendar": ("date",),
}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonicalize_table(frame: pl.DataFrame, table: str) -> str:
    columns = HASH_COLUMNS[table]
    keys = SORT_KEYS[table]
    present = [col for col in columns if col in frame.columns]
    if frame.is_empty() or not present:
        return ""
    work = frame.select(present)
    sort_cols = [col for col in keys if col in work.columns]
    if sort_cols:
        work = work.sort(sort_cols)
    lines: list[str] = []
    for row in work.iter_rows(named=True):
        parts = [_canon_cell(row.get(col), col) for col in columns]
        lines.append("|".join(parts))
    return "\n".join(lines) + "\n"


def hash_table(frame: pl.DataFrame, table: str) -> str:
    return sha256_text(canonicalize_table(frame, table))


def combine_hashes(table_hashes: dict[str, str], adjustment: str, schema_version: str = SCHEMA_VERSION) -> str:
    parts = [f"schema_version={schema_version}", f"adjustment={adjustment}"]
    for name in TABLE_NAMES:
        parts.append(f"{name}={table_hashes.get(name, sha256_text(''))}")
    return sha256_text("\n".join(parts) + "\n")


def build_snapshot(
    tables: dict[str, pl.DataFrame],
    *,
    adjustment: str,
    source_name: str,
    fetched_at: datetime | None = None,
    market_index: str | None = None,
    global_symbol: str | None = None,
    source_version: str | None = None,
) -> DataSnapshot:
    table_hashes = {name: hash_table(tables.get(name, pl.DataFrame()), name) for name in TABLE_NAMES}
    content_hash = combine_hashes(table_hashes, adjustment)
    calendar = tables.get("calendar")
    coverage_start, coverage_end = _coverage(calendar)
    fetched = fetched_at or datetime.now(UTC).replace(tzinfo=None)
    return DataSnapshot(
        snapshot_id=content_hash,
        schema_version=SCHEMA_VERSION,
        table_hashes=table_hashes,
        content_hash=content_hash,
        source_name=source_name,
        fetched_at=fetched.strftime("%Y-%m-%dT%H:%M:%S"),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        adjustment=adjustment,
        row_counts={
            name: int(tables[name].height) if name in tables and tables[name] is not None else 0
            for name in TABLE_NAMES
        },
        market_index=market_index,
        global_symbol=global_symbol,
        source_version=source_version,
    )


def _coverage(calendar: pl.DataFrame | None) -> tuple[date | None, date | None]:
    if calendar is None or calendar.is_empty() or "date" not in calendar.columns:
        return None, None
    values = [v for v in calendar["date"].to_list() if isinstance(v, date)]
    if not values:
        return None, None
    return min(values), max(values)


def _canon_cell(value: Any, column: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if column == "available_at" or isinstance(value, datetime):
        return _canon_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int | float):
        return format(float(value), ".15g")
    return str(value)


def _canon_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone(UTC).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    text = str(value).replace("Z", "")
    if "+" in text[10:]:
        text = text.split("+", 1)[0]
    if "." in text:
        text = text.split(".", 1)[0]
    return text
