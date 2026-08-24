from __future__ import annotations

from pathlib import Path

import polars as pl

from app.errors import SnapshotError
from app.models.snapshot import RAW_PLUS_ADJUSTED_PRICE_BASIS, SCHEMA_VERSION, TABLE_NAMES, DataSnapshot
from app.storage.hashing import build_snapshot

PARQUET_NAMES = {name: f"{name}.parquet" for name in TABLE_NAMES}


def write_manifest(parquet_dir: Path, snapshot: DataSnapshot) -> Path:
    path = parquet_dir / "manifest.json"
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    return path


def read_tables(parquet_dir: Path) -> dict[str, pl.DataFrame]:
    tables: dict[str, pl.DataFrame] = {}
    missing: list[str] = []
    for name, filename in PARQUET_NAMES.items():
        path = parquet_dir / filename
        if not path.exists():
            missing.append(filename)
            continue
        tables[name] = pl.read_parquet(path)
    if missing:
        hint = ""
        if "universe_membership.parquet" in missing:
            hint = (
                "; universe_membership is required by the six-table snapshot contract. "
                "Re-import market data or regenerate demo data. "
                "Legacy five-table snapshots are rejected and are not treated as an all-instrument universe"
            )
        raise SnapshotError(f"market snapshot is missing required tables: {missing}{hint}")
    return tables


def compute_snapshot_from_dir(
    parquet_dir: Path,
    *,
    adjustment: str,
    price_basis: str = RAW_PLUS_ADJUSTED_PRICE_BASIS,
    source_name: str,
    market_index: str | None = None,
    global_symbol: str | None = None,
    source_version: str | None = None,
) -> DataSnapshot:
    return build_snapshot(
        read_tables(parquet_dir),
        adjustment=adjustment,
        price_basis=price_basis,
        source_name=source_name,
        market_index=market_index,
        global_symbol=global_symbol,
        source_version=source_version,
    )


def load_verified_snapshot(parquet_dir: Path) -> DataSnapshot:
    root = Path(parquet_dir)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise SnapshotError("missing manifest.json; import market data or run generate-demo")
    try:
        stored = DataSnapshot.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SnapshotError("manifest.json is invalid") from exc
    if stored.schema_version != SCHEMA_VERSION:
        raise SnapshotError(
            f"snapshot schema version {stored.schema_version} is legacy; "
            f"re-fetch or re-import under schema {SCHEMA_VERSION}. "
            "Execution requires raw OHLC plus separately stored adjusted feature prices"
        )
    recomputed = compute_snapshot_from_dir(
        root,
        adjustment=stored.adjustment,
        price_basis=stored.price_basis,
        source_name=stored.source_name,
        market_index=stored.market_index,
        global_symbol=stored.global_symbol,
        source_version=stored.source_version,
    )
    if stored.snapshot_id != recomputed.snapshot_id or stored.table_hashes != recomputed.table_hashes:
        raise SnapshotError("manifest.json does not match parquet content hashes")
    return stored
