from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "4"
RAW_PLUS_ADJUSTED_PRICE_BASIS: Literal["raw_ohlc_plus_adjusted_features"] = (
    "raw_ohlc_plus_adjusted_features"
)
LEGACY_UNKNOWN_PRICE_BASIS: Literal["legacy_unknown"] = "legacy_unknown"

TABLE_NAMES = (
    "daily_bars",
    "index_bars",
    "global_bars",
    "instruments",
    "calendar",
    "universe_membership",
)


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
    # Kept readable so historic backtest JSON can still be displayed.  It is
    # rejected by current preflight and can never execute a new backtest.
    price_basis: Literal["raw_ohlc_plus_adjusted_features", "legacy_unknown"] = (
        LEGACY_UNKNOWN_PRICE_BASIS
    )
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
