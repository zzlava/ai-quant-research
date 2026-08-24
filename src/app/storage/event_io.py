from __future__ import annotations

import hashlib
import shutil
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from app.errors import SnapshotError
from app.models.events import (
    EVENT_SCHEMA_VERSION,
    EVENT_TABLE_NAMES,
    EventSnapshot,
    EventSourceManifest,
)
from app.providers.tushare_events import EVENT_AVAILABILITY_POLICY, validate_event_tables
from app.storage.hashing import hash_table, sha256_text


def build_event_snapshot(
    tables: dict[str, pl.DataFrame],
    *,
    source_name: str,
    source_version: str | None,
    base_market_snapshot_id: str,
    source_manifest_sha256: str,
    fetched_at: datetime | None = None,
) -> EventSnapshot:
    canonical = validate_event_tables(tables)
    hashes = {name: hash_table(canonical[name], name) for name in EVENT_TABLE_NAMES}
    content_hash = _combine_event_hashes(
        hashes,
        base_market_snapshot_id=base_market_snapshot_id,
        source_manifest_sha256=source_manifest_sha256,
    )
    coverage_start, coverage_end = _coverage(canonical)
    fetched = fetched_at or datetime.now(UTC)
    if fetched.tzinfo is not None:
        fetched = fetched.astimezone(UTC).replace(tzinfo=None)
    symbols = {
        str(symbol)
        for frame in canonical.values()
        for symbol in frame["symbol"].to_list()
    }
    return EventSnapshot(
        snapshot_id=content_hash,
        schema_version=EVENT_SCHEMA_VERSION,
        table_hashes=hashes,
        content_hash=content_hash,
        source_name=source_name,
        source_version=source_version,
        fetched_at=fetched.isoformat(timespec="seconds"),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        row_counts={name: canonical[name].height for name in EVENT_TABLE_NAMES},
        covered_symbols=len(symbols),
        base_market_snapshot_id=base_market_snapshot_id,
        source_manifest_sha256=source_manifest_sha256,
        availability_policy=EVENT_AVAILABILITY_POLICY,
    )


def write_event_snapshot_atomically(
    dest_dir: Path,
    tables: dict[str, pl.DataFrame],
    snapshot: EventSnapshot,
    source_manifest_bytes: bytes,
    *,
    replace_existing: bool = False,
) -> None:
    canonical = validate_event_tables(tables)
    manifest_hash = hashlib.sha256(source_manifest_bytes).hexdigest()
    if manifest_hash != snapshot.source_manifest_sha256:
        raise SnapshotError("event source manifest bytes do not match source_manifest_sha256")
    recomputed = build_event_snapshot(
        canonical,
        source_name=snapshot.source_name,
        source_version=snapshot.source_version,
        base_market_snapshot_id=snapshot.base_market_snapshot_id,
        source_manifest_sha256=snapshot.source_manifest_sha256,
        fetched_at=datetime.fromisoformat(snapshot.fetched_at),
    )
    if snapshot != recomputed:
        raise SnapshotError("event snapshot manifest does not match the supplied tables")

    dest = Path(dest_dir)
    if dest.exists() and not replace_existing:
        raise SnapshotError("event snapshot directory already exists; pass --replace-existing explicitly")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".event-import-{uuid.uuid4().hex}"
    backup = dest.parent / f".event-import-bak-{uuid.uuid4().hex}"
    try:
        tmp.mkdir(parents=True)
        for name in EVENT_TABLE_NAMES:
            canonical[name].write_parquet(tmp / f"{name}.parquet")
        (tmp / "source_manifest.json").write_bytes(source_manifest_bytes)
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


def load_verified_event_snapshot(
    directory: Path,
    *,
    expected_market_snapshot_id: str | None = None,
) -> tuple[EventSnapshot, dict[str, pl.DataFrame]]:
    root = Path(directory)
    manifest_path = root / "manifest.json"
    source_manifest_path = root / "source_manifest.json"
    if not manifest_path.is_file():
        raise SnapshotError("event snapshot is missing manifest.json")
    if not source_manifest_path.is_file():
        raise SnapshotError("event snapshot is missing source_manifest.json")
    try:
        stored = EventSnapshot.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SnapshotError("event manifest.json is invalid") from exc
    if stored.schema_version != EVENT_SCHEMA_VERSION:
        raise SnapshotError(
            f"event snapshot schema version {stored.schema_version} is unsupported; "
            f"expected {EVENT_SCHEMA_VERSION}"
        )
    if stored.availability_policy != EVENT_AVAILABILITY_POLICY:
        raise SnapshotError("event availability policy does not match the executable contract")
    if (
        expected_market_snapshot_id is not None
        and stored.base_market_snapshot_id != expected_market_snapshot_id
    ):
        raise SnapshotError("event overlay is bound to a different market snapshot")
    source_manifest_bytes = source_manifest_path.read_bytes()
    source_hash = hashlib.sha256(source_manifest_bytes).hexdigest()
    if source_hash != stored.source_manifest_sha256:
        raise SnapshotError("event source_manifest.json hash does not match the overlay manifest")
    try:
        source_manifest = EventSourceManifest.model_validate_json(source_manifest_bytes)
    except Exception as exc:
        raise SnapshotError("event source_manifest.json is invalid") from exc
    source_fetched = source_manifest.fetched_at.astimezone(UTC).replace(tzinfo=None)
    if (
        source_manifest.source_name != stored.source_name
        or source_manifest.source_version != stored.source_version
        or source_fetched.isoformat(timespec="seconds") != stored.fetched_at
    ):
        raise SnapshotError("event source manifest metadata does not match the overlay manifest")
    if (
        stored.coverage_start < source_manifest.coverage_start
        or stored.coverage_end > source_manifest.coverage_end
    ):
        raise SnapshotError("event snapshot coverage exceeds its source manifest coverage")

    tables: dict[str, pl.DataFrame] = {}
    for name in EVENT_TABLE_NAMES:
        path = root / f"{name}.parquet"
        if not path.is_file():
            raise SnapshotError(f"event snapshot is missing {name}.parquet")
        tables[name] = pl.read_parquet(path)
    canonical = validate_event_tables(tables)
    recomputed = build_event_snapshot(
        canonical,
        source_name=stored.source_name,
        source_version=stored.source_version,
        base_market_snapshot_id=stored.base_market_snapshot_id,
        source_manifest_sha256=stored.source_manifest_sha256,
        fetched_at=datetime.fromisoformat(stored.fetched_at),
    )
    if stored != recomputed:
        raise SnapshotError("event manifest.json does not match parquet content or metadata")
    return stored, canonical


def _combine_event_hashes(
    hashes: dict[str, str],
    *,
    base_market_snapshot_id: str,
    source_manifest_sha256: str,
) -> str:
    parts = [
        f"schema_version={EVENT_SCHEMA_VERSION}",
        f"availability_policy={EVENT_AVAILABILITY_POLICY}",
        f"base_market_snapshot_id={base_market_snapshot_id}",
        f"source_manifest_sha256={source_manifest_sha256}",
    ]
    for name in EVENT_TABLE_NAMES:
        parts.append(f"{name}={hashes.get(name, sha256_text(''))}")
    return sha256_text("\n".join(parts) + "\n")


def _coverage(tables: dict[str, pl.DataFrame]) -> tuple[date, date]:
    values = [
        value
        for frame in tables.values()
        for value in frame["ann_date"].to_list()
        if isinstance(value, date)
    ]
    if not values:
        raise SnapshotError("event overlay has no announcement dates")
    return min(values), max(values)
