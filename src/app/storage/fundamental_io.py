from __future__ import annotations

import hashlib
import shutil
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from app.errors import DataQualityError, SnapshotError
from app.models.fundamentals import (
    FUNDAMENTAL_SCHEMA_VERSION,
    FUNDAMENTAL_TABLE_NAMES,
    FundamentalSnapshot,
)
from app.storage.hashing import hash_table, sha256_text

REPORT_AVAILABILITY_POLICY = (
    "Tushare fina_indicator ann_date at 23:59 Asia/Shanghai; conservative date-only publication clock"
)
VALUATION_AVAILABILITY_POLICY = (
    "Tushare daily_basic trade_date at 17:00 Asia/Shanghai; same-day 15:00 decisions use prior data"
)


def build_fundamental_snapshot(
    tables: dict[str, pl.DataFrame],
    *,
    source_name: str,
    source_version: str | None = None,
    fetched_at: datetime | None = None,
    base_market_snapshot_id: str | None = None,
    collection_request_id: str | None = None,
    requested_symbols: int | None = None,
) -> FundamentalSnapshot:
    hashes = {
        name: hash_table(tables.get(name, pl.DataFrame()), name)
        for name in FUNDAMENTAL_TABLE_NAMES
    }
    content = _combine_fundamental_hashes(
        hashes,
        base_market_snapshot_id=base_market_snapshot_id,
        collection_request_id=collection_request_id,
    )
    valuation = tables.get("daily_valuation", pl.DataFrame())
    coverage_start, coverage_end = _coverage(valuation)
    fetched = fetched_at or datetime.now(UTC).replace(tzinfo=None)
    return FundamentalSnapshot(
        snapshot_id=content,
        schema_version=FUNDAMENTAL_SCHEMA_VERSION,
        table_hashes=hashes,
        content_hash=content,
        source_name=source_name,
        fetched_at=fetched.strftime("%Y-%m-%dT%H:%M:%S"),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        row_counts={name: int(tables.get(name, pl.DataFrame()).height) for name in FUNDAMENTAL_TABLE_NAMES},
        source_version=source_version,
        base_market_snapshot_id=base_market_snapshot_id,
        collection_request_id=collection_request_id,
        requested_symbols=requested_symbols,
        covered_report_symbols=_covered_symbols(tables.get("fundamental_reports", pl.DataFrame())),
        covered_valuation_symbols=_covered_symbols(tables.get("daily_valuation", pl.DataFrame())),
        report_availability_policy=REPORT_AVAILABILITY_POLICY,
        valuation_availability_policy=VALUATION_AVAILABILITY_POLICY,
    )


def write_fundamental_snapshot_atomically(
    dest_dir: Path,
    tables: dict[str, pl.DataFrame],
    snapshot: FundamentalSnapshot,
    *,
    replace_existing: bool = False,
) -> None:
    dest = Path(dest_dir)
    if dest.exists() and not replace_existing:
        raise SnapshotError("fundamental snapshot directory already exists; pass --replace-existing explicitly")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".fundamental-import-{uuid.uuid4().hex}"
    backup = dest.parent / f".fundamental-import-bak-{uuid.uuid4().hex}"
    try:
        tmp.mkdir(parents=True)
        for name in FUNDAMENTAL_TABLE_NAMES:
            if name not in tables:
                raise SnapshotError(f"fundamental snapshot missing table '{name}'")
            tables[name].write_parquet(tmp / f"{name}.parquet")
        (tmp / "manifest.json").write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
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


def load_verified_fundamental_snapshot(
    directory: Path,
) -> tuple[FundamentalSnapshot, dict[str, pl.DataFrame]]:
    root = Path(directory)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise SnapshotError("fundamental snapshot is missing manifest.json")
    try:
        stored = FundamentalSnapshot.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SnapshotError("fundamental manifest.json is invalid") from exc
    if stored.schema_version != FUNDAMENTAL_SCHEMA_VERSION:
        raise SnapshotError(
            f"fundamental snapshot schema version {stored.schema_version} is unsupported; "
            f"expected {FUNDAMENTAL_SCHEMA_VERSION}"
        )
    if stored.report_availability_policy != REPORT_AVAILABILITY_POLICY:
        raise SnapshotError("fundamental report availability policy does not match the executable contract")
    if stored.valuation_availability_policy != VALUATION_AVAILABILITY_POLICY:
        raise SnapshotError("valuation availability policy does not match the executable contract")
    tables: dict[str, pl.DataFrame] = {}
    for name in FUNDAMENTAL_TABLE_NAMES:
        path = root / f"{name}.parquet"
        if not path.is_file():
            raise SnapshotError(f"fundamental snapshot is missing {name}.parquet")
        tables[name] = pl.read_parquet(path)
    recomputed = build_fundamental_snapshot(
        tables,
        source_name=stored.source_name,
        source_version=stored.source_version,
        fetched_at=datetime.fromisoformat(stored.fetched_at),
        base_market_snapshot_id=stored.base_market_snapshot_id,
        collection_request_id=stored.collection_request_id,
        requested_symbols=stored.requested_symbols,
    )
    if stored.snapshot_id != recomputed.snapshot_id or stored.table_hashes != recomputed.table_hashes:
        raise SnapshotError("fundamental manifest.json does not match parquet content hashes")
    return stored, tables


def source_row_hash(row: dict[str, object], columns: tuple[str, ...]) -> str:
    parts = [f"{name}={_canonical_value(row.get(name))}" for name in columns]
    return hashlib.sha256(("\n".join(parts) + "\n").encode("utf-8")).hexdigest()


def _combine_fundamental_hashes(
    hashes: dict[str, str],
    *,
    base_market_snapshot_id: str | None = None,
    collection_request_id: str | None = None,
) -> str:
    parts = [f"schema_version={FUNDAMENTAL_SCHEMA_VERSION}"]
    for name in FUNDAMENTAL_TABLE_NAMES:
        parts.append(f"{name}={hashes.get(name, sha256_text(''))}")
    parts.append(f"report_availability_policy={REPORT_AVAILABILITY_POLICY}")
    parts.append(f"valuation_availability_policy={VALUATION_AVAILABILITY_POLICY}")
    # Keep the legacy v1 content hash stable when these fields are absent.
    if base_market_snapshot_id is not None:
        parts.append(f"base_market_snapshot_id={base_market_snapshot_id}")
    if collection_request_id is not None:
        parts.append(f"collection_request_id={collection_request_id}")
    return sha256_text("\n".join(parts) + "\n")


def _covered_symbols(frame: pl.DataFrame) -> int:
    if frame.is_empty() or "symbol" not in frame.columns:
        return 0
    return int(frame["symbol"].n_unique())


def _coverage(frame: pl.DataFrame) -> tuple[date | None, date | None]:
    if frame.is_empty() or "date" not in frame.columns:
        return None, None
    days = [value for value in frame["date"].to_list() if isinstance(value, date)]
    return (min(days), max(days)) if days else (None, None)


def _canonical_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return float(value).hex()
    return str(value)


def require_no_conflicting_duplicates(
    frame: pl.DataFrame,
    *,
    key: list[str],
    table: str,
) -> pl.DataFrame:
    if frame.is_empty():
        raise DataQualityError(f"{table} returned no rows")
    duplicate = frame.group_by(key).agg(pl.col("source_row_hash").n_unique().alias("variants"))
    bad = duplicate.filter(pl.col("variants") > 1)
    if bad.height:
        sample = bad.sort(key).head(1).to_dicts()[0]
        raise DataQualityError(f"{table} has conflicting duplicate logical key: {sample}")
    return frame.unique(subset=[*key, "source_row_hash"], keep="first").sort(key)
