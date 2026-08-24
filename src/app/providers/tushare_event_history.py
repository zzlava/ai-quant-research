from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

from app.errors import DataQualityError, SnapshotError
from app.models.events import EVENT_SOURCE_NAMES, EventSnapshot, EventSourceManifest
from app.providers.tushare_events import normalize_event_sources
from app.storage.event_io import build_event_snapshot, write_event_snapshot_atomically
from app.storage.snapshot_io import load_verified_snapshot


@dataclass(frozen=True)
class EventOverlayMaterializeResult:
    snapshot: EventSnapshot
    source_manifest: EventSourceManifest


def materialize_tushare_event_overlay(
    *,
    source_dir: Path,
    market_dir: Path,
    dest_dir: Path,
    replace_existing: bool = False,
) -> EventOverlayMaterializeResult:
    """Normalize five offline Tushare exports into a verified event overlay."""
    root = Path(source_dir)
    source_manifest_path = root / "source_manifest.json"
    if not source_manifest_path.is_file():
        raise SnapshotError("event source directory is missing source_manifest.json")
    source_manifest_bytes = source_manifest_path.read_bytes()
    try:
        source_manifest = EventSourceManifest.model_validate_json(source_manifest_bytes)
    except Exception as exc:
        raise SnapshotError("event source_manifest.json is invalid") from exc

    raw: dict[str, pl.DataFrame] = {}
    for source_name in EVENT_SOURCE_NAMES:
        item = source_manifest.files[source_name]
        path = _safe_source_path(root, item.path)
        if not path.is_file():
            raise SnapshotError(f"event source file is missing for {source_name}")
        if _sha256_file(path) != item.sha256:
            raise SnapshotError(f"event source file hash mismatch for {source_name}")
        raw[source_name] = _read_frame(path)

    tables = normalize_event_sources(raw)
    market_snapshot = load_verified_snapshot(Path(market_dir))
    if market_snapshot.coverage_end is None:
        raise SnapshotError("market snapshot has no coverage_end")
    _validate_source_coverage(
        tables,
        coverage_start=source_manifest.coverage_start,
        coverage_end=source_manifest.coverage_end,
        market_coverage_end=market_snapshot.coverage_end,
    )
    _validate_market_symbols(tables, Path(market_dir) / "instruments.parquet")

    source_manifest_sha256 = hashlib.sha256(source_manifest_bytes).hexdigest()
    snapshot = build_event_snapshot(
        tables,
        source_name=source_manifest.source_name,
        source_version=source_manifest.source_version,
        base_market_snapshot_id=market_snapshot.snapshot_id,
        source_manifest_sha256=source_manifest_sha256,
        fetched_at=source_manifest.fetched_at,
    )
    write_event_snapshot_atomically(
        dest_dir,
        tables,
        snapshot,
        source_manifest_bytes,
        replace_existing=replace_existing,
    )
    return EventOverlayMaterializeResult(snapshot=snapshot, source_manifest=source_manifest)


def _safe_source_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise SnapshotError("event source file paths must be relative")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise SnapshotError("event source file path escapes the source directory")
    return resolved


def _read_frame(path: Path) -> pl.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pl.read_csv(path, infer_schema_length=10_000, null_values=["", "null", "None"])
    if suffix in {".parquet", ".pq"}:
        return pl.read_parquet(path)
    raise SnapshotError(f"unsupported event source file type: {suffix or '<none>'}")


def _validate_source_coverage(
    tables: dict[str, pl.DataFrame],
    *,
    coverage_start: date,
    coverage_end: date,
    market_coverage_end: date,
) -> None:
    if coverage_end > market_coverage_end:
        raise DataQualityError(
            "event source manifest coverage exceeds the bound market snapshot"
        )
    for table, frame in tables.items():
        invalid = frame.filter(
            (pl.col("ann_date") < pl.lit(coverage_start))
            | (pl.col("ann_date") > pl.lit(coverage_end))
        )
        if invalid.height:
            raise DataQualityError(
                f"{table} contains ann_date outside source manifest coverage"
            )
        future = frame.filter(pl.col("ann_date") > pl.lit(market_coverage_end))
        if future.height:
            raise DataQualityError(
                f"{table} contains announcements after the bound market snapshot"
            )


def _validate_market_symbols(
    tables: dict[str, pl.DataFrame],
    instruments_path: Path,
) -> None:
    if not instruments_path.is_file():
        raise SnapshotError("market snapshot is missing instruments.parquet")
    instruments = pl.read_parquet(instruments_path)
    if "symbol" not in instruments.columns:
        raise SnapshotError("market instruments table is missing symbol")
    allowed = set(str(value) for value in instruments["symbol"].to_list())
    observed = {
        str(value)
        for frame in tables.values()
        for value in frame["symbol"].to_list()
    }
    unexpected = sorted(observed - allowed)
    if unexpected:
        raise DataQualityError(
            f"event overlay contains symbols outside the market snapshot: {unexpected[:3]}"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
