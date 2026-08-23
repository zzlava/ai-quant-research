from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.errors import DataQualityError

REQUIRED_OHLCV = ("symbol", "date", "open", "high", "low", "close", "volume", "amount")
REQUIRED_GLOBAL = ("symbol", "date", "close", "available_at")


def validate_ohlcv(frame: pl.DataFrame, name: str, calendar: list | None = None) -> None:
    if frame.is_empty():
        raise DataQualityError(f"{name} has no rows")
    missing = [col for col in REQUIRED_OHLCV if col not in frame.columns]
    if missing:
        raise DataQualityError(f"{name} missing columns: {missing}")
    dups = frame.group_by(["symbol", "date"]).len().filter(pl.col("len") > 1)
    if dups.height:
        raise DataQualityError(f"{name} has duplicate (symbol, date) rows")
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
    bad = frame.filter((pl.col("close") <= 0) | pl.col("available_at").is_null())
    if bad.height:
        raise DataQualityError(f"{name} has non-positive close or missing available_at")


def snapshot_payload(
    daily: pl.DataFrame,
    index: pl.DataFrame,
    global_bars: pl.DataFrame,
    adjustment: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = json.dumps(
        {
            "daily_rows": daily.height,
            "index_rows": index.height,
            "global_rows": global_bars.height,
            "adjustment": adjustment,
            "extra": extra or {},
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return {
        "version": digest[:16],
        "created_at": datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
        "adjustment": adjustment,
        "daily_rows": daily.height,
        "index_rows": index.height,
        "global_rows": global_bars.height,
        **(extra or {}),
    }


def write_snapshot_manifest(parquet_dir: Path, payload: dict[str, Any]) -> Path:
    path = parquet_dir / "manifest.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path
