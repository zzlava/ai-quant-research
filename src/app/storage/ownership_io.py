from __future__ import annotations

import shutil
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from app.errors import SnapshotError
from app.models.ownership import (
    OWNERSHIP_SCHEMA_VERSION,
    OWNERSHIP_TABLE_NAME,
    OwnershipSnapshot,
)
from app.providers.tushare_ownership import (
    OWNERSHIP_AVAILABILITY_POLICY,
    validate_top10_float_holders,
)
from app.storage.hashing import hash_table, sha256_text


def build_ownership_snapshot(
    table: pl.DataFrame,
    *,
    source_name: str,
    base_market_snapshot_id: str,
    fundamental_snapshot_id: str,
    source_version: str | None = None,
    fetched_at: datetime | None = None,
) -> OwnershipSnapshot:
    validate_top10_float_holders(table)
    table_hash = hash_table(table, OWNERSHIP_TABLE_NAME)
    content = sha256_text(
        "\n".join(
            (
                f"schema_version={OWNERSHIP_SCHEMA_VERSION}",
                f"availability_policy={OWNERSHIP_AVAILABILITY_POLICY}",
                f"base_market_snapshot_id={base_market_snapshot_id}",
                f"fundamental_snapshot_id={fundamental_snapshot_id}",
                f"{OWNERSHIP_TABLE_NAME}={table_hash}",
            )
        )
        + "\n"
    )
    dates = [value for value in table["ann_date"].to_list() if isinstance(value, date)]
    if not dates:
        raise SnapshotError("ownership overlay has no announcement dates")
    fetched = fetched_at or datetime.now(UTC).replace(tzinfo=None)
    if fetched.tzinfo is not None:
        fetched = fetched.astimezone(UTC).replace(tzinfo=None)
    return OwnershipSnapshot(
        snapshot_id=content,
        schema_version=OWNERSHIP_SCHEMA_VERSION,
        table_hash=table_hash,
        content_hash=content,
        source_name=source_name,
        source_version=source_version,
        fetched_at=fetched.isoformat(timespec="seconds"),
        coverage_start=min(dates),
        coverage_end=max(dates),
        row_count=table.height,
        covered_symbols=int(table["symbol"].n_unique()),
        base_market_snapshot_id=base_market_snapshot_id,
        fundamental_snapshot_id=fundamental_snapshot_id,
        availability_policy=OWNERSHIP_AVAILABILITY_POLICY,
    )


def write_ownership_snapshot_atomically(
    dest_dir: Path,
    table: pl.DataFrame,
    snapshot: OwnershipSnapshot,
    *,
    replace_existing: bool = False,
) -> None:
    recomputed = build_ownership_snapshot(
        table,
        source_name=snapshot.source_name,
        source_version=snapshot.source_version,
        fetched_at=datetime.fromisoformat(snapshot.fetched_at),
        base_market_snapshot_id=snapshot.base_market_snapshot_id,
        fundamental_snapshot_id=snapshot.fundamental_snapshot_id,
    )
    if recomputed != snapshot:
        raise SnapshotError("ownership snapshot manifest does not match the supplied table")
    dest = Path(dest_dir)
    if dest.exists() and not replace_existing:
        raise SnapshotError("ownership snapshot directory already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".ownership-import-{uuid.uuid4().hex}"
    backup = dest.parent / f".ownership-import-bak-{uuid.uuid4().hex}"
    try:
        tmp.mkdir(parents=True)
        table.write_parquet(tmp / f"{OWNERSHIP_TABLE_NAME}.parquet")
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


def load_verified_ownership_snapshot(
    directory: Path,
) -> tuple[OwnershipSnapshot, pl.DataFrame]:
    root = Path(directory)
    manifest_path = root / "manifest.json"
    table_path = root / f"{OWNERSHIP_TABLE_NAME}.parquet"
    if not manifest_path.is_file() or not table_path.is_file():
        raise SnapshotError("ownership snapshot is missing manifest.json or parquet")
    try:
        stored = OwnershipSnapshot.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SnapshotError("ownership manifest.json is invalid") from exc
    if stored.schema_version != OWNERSHIP_SCHEMA_VERSION:
        raise SnapshotError("ownership snapshot schema version is unsupported")
    if stored.availability_policy != OWNERSHIP_AVAILABILITY_POLICY:
        raise SnapshotError("ownership availability policy does not match executable contract")
    table = pl.read_parquet(table_path)
    recomputed = build_ownership_snapshot(
        table,
        source_name=stored.source_name,
        source_version=stored.source_version,
        fetched_at=datetime.fromisoformat(stored.fetched_at),
        base_market_snapshot_id=stored.base_market_snapshot_id,
        fundamental_snapshot_id=stored.fundamental_snapshot_id,
    )
    if recomputed != stored:
        raise SnapshotError("ownership manifest.json does not match parquet content")
    return stored, table
