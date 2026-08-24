from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

FUNDAMENTAL_SCHEMA_VERSION = "1"
FUNDAMENTAL_TABLE_NAMES = ("fundamental_reports", "daily_valuation")


class FundamentalSnapshot(BaseModel):
    """Content-addressed point-in-time fundamental/valuation overlay."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    schema_version: str = FUNDAMENTAL_SCHEMA_VERSION
    table_hashes: dict[str, str]
    content_hash: str
    source_name: str
    fetched_at: str
    coverage_start: date | None = None
    coverage_end: date | None = None
    row_counts: dict[str, int]
    source_version: str | None = None
    # New full-market overlays are cryptographically bound to the exact
    # six-table market snapshot they were collected against.  These remain
    # optional so previously materialized research overlays can still be read.
    base_market_snapshot_id: str | None = None
    collection_request_id: str | None = None
    requested_symbols: int | None = None
    covered_report_symbols: int | None = None
    covered_valuation_symbols: int | None = None
    report_availability_policy: str
    valuation_availability_policy: str
