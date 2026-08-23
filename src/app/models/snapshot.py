from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"

TABLE_NAMES = ("daily_bars", "index_bars", "global_bars", "instruments", "calendar")


class DataSnapshot(BaseModel):
    """Content-addressed market data snapshot. snapshot_id is a content hash."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    schema_version: str = SCHEMA_VERSION
    table_hashes: dict[str, str]
    content_hash: str
    source_name: str
    fetched_at: str
    coverage_start: date | None = None
    coverage_end: date | None = None
    adjustment: str
    row_counts: dict[str, int]
    market_index: str | None = None
    global_symbol: str | None = None
    source_version: str | None = None


class SnapshotInfo(BaseModel):
    """Slim pointer used when only the id must travel with a score row."""

    snapshot_id: str
    schema_version: str = SCHEMA_VERSION
    source_name: str | None = None
    adjustment: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)
