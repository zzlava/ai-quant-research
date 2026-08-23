from __future__ import annotations

import shutil
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import polars as pl

from app.errors import DataQualityError, SnapshotError
from app.models.snapshot import TABLE_NAMES, DataSnapshot
from app.providers._frames import DAILY_SCHEMA, INSTRUMENT_SCHEMA
from app.storage.hashing import build_snapshot
from app.storage.quality import (
    assert_benchmarks,
    validate_calendar,
    validate_global,
    validate_instruments,
    validate_ohlcv,
)
from app.storage.snapshot_io import write_manifest

Adjustment = Literal["forward", "backward", "none"]
GLOBAL_REQUIRED = {
    "symbol": pl.String,
    "date": pl.Date,
    "close": pl.Float64,
    "available_at": pl.Datetime("us"),
}


def _read_named_table(source_dir: Path, name: str) -> pl.DataFrame:
    csv_path = source_dir / f"{name}.csv"
    parquet_path = source_dir / f"{name}.parquet"
    if csv_path.exists():
        return pl.read_csv(csv_path, try_parse_dates=True)
    if parquet_path.exists():
        return pl.read_parquet(parquet_path)
    raise DataQualityError(f"missing required table {name} (.csv or .parquet)")


def _blank_to_null(frame: pl.DataFrame, columns: tuple[str, ...]) -> pl.DataFrame:
    work = frame
    for col in columns:
        if col not in work.columns:
            continue
        dtype = work[col].dtype
        if dtype in (pl.Utf8, pl.String):
            work = work.with_columns(
                pl.when(pl.col(col).str.strip_chars().is_in(["", "null", "None", "NA"]))
                .then(None)
                .otherwise(pl.col(col))
                .alias(col)
            )
    return work


def _cast_schema(frame: pl.DataFrame, schema: Mapping[str, object], name: str) -> pl.DataFrame:
    missing = [col for col in schema if col not in frame.columns]
    if missing:
        raise DataQualityError(f"{name} missing required columns: {missing}")
    work = _blank_to_null(frame, tuple(schema))
    casts = []
    for col, dtype in schema.items():
        casts.append(pl.col(col).cast(dtype, strict=True))  # type: ignore[arg-type]
    try:
        return work.with_columns(casts)
    except Exception as exc:
        raise DataQualityError(f"{name} failed strict type conversion") from exc


def _prepare_global(frame: pl.DataFrame) -> pl.DataFrame:
    work = _blank_to_null(frame, ("symbol", "date", "close", "available_at", "ret_1d", "market", "timezone"))
    if "ret_1d" not in work.columns:
        work = work.with_columns(pl.lit(0.0).alias("ret_1d"))
    if "market" not in work.columns:
        work = work.with_columns(pl.lit("US").alias("market"))
    if "timezone" not in work.columns:
        work = work.with_columns(pl.lit("America/New_York").alias("timezone"))
    schema = {
        **GLOBAL_REQUIRED,
        "ret_1d": pl.Float64,
        "market": pl.String,
        "timezone": pl.String,
    }
    return _cast_schema(work, schema, "global_bars")


def load_normalized_tables(source_dir: Path) -> dict[str, pl.DataFrame]:
    root = Path(source_dir)
    raw = {name: _read_named_table(root, name) for name in TABLE_NAMES}
    daily = _cast_schema(raw["daily_bars"], DAILY_SCHEMA, "daily_bars")
    index = _cast_schema(raw["index_bars"], DAILY_SCHEMA, "index_bars")
    glob = _prepare_global(raw["global_bars"])
    if glob["available_at"].dtype != pl.Datetime:
        glob = glob.with_columns(pl.col("available_at").cast(pl.Datetime("us"), strict=True))
    instruments = _cast_schema(raw["instruments"], INSTRUMENT_SCHEMA, "instruments")
    calendar = raw["calendar"].with_columns(pl.col("date").cast(pl.Date, strict=True))
    validate_ohlcv(daily, "daily_bars")
    validate_ohlcv(index, "index_bars")
    validate_global(glob)
    validate_instruments(instruments)
    validate_calendar(calendar)
    return {
        "daily_bars": daily,
        "index_bars": index,
        "global_bars": glob,
        "instruments": instruments,
        "calendar": calendar,
    }


def import_market_data(
    source_dir: Path,
    dest_dir: Path,
    *,
    source_name: str,
    adjustment: Adjustment,
    source_version: str | None = None,
    market_index: str | None = None,
    global_symbol: str | None = None,
) -> DataSnapshot:
    tables = load_normalized_tables(source_dir)
    assert_benchmarks(tables["index_bars"], tables["global_bars"], market_index, global_symbol)
    snapshot = build_snapshot(
        tables,
        adjustment=adjustment,
        source_name=source_name,
        fetched_at=datetime.now(UTC).replace(tzinfo=None),
        market_index=market_index,
        global_symbol=global_symbol,
        source_version=source_version,
    )
    write_snapshot_atomically(Path(dest_dir), tables, snapshot)
    return snapshot


def write_snapshot_atomically(dest_dir: Path, tables: dict[str, pl.DataFrame], snapshot: DataSnapshot) -> None:
    dest = Path(dest_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".import-{uuid.uuid4().hex}"
    backup = dest.parent / f".import-bak-{uuid.uuid4().hex}"
    try:
        tmp.mkdir(parents=True)
        for name, frame in tables.items():
            frame.write_parquet(tmp / f"{name}.parquet")
        write_manifest(tmp, snapshot)
        if dest.exists():
            dest.rename(backup)
        tmp.rename(dest)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        if backup.exists() and not dest.exists():
            backup.rename(dest)
        elif backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        raise


def require_existing_snapshot_dir(parquet_dir: Path) -> None:
    if not Path(parquet_dir).exists():
        raise SnapshotError("missing market snapshot directory")
