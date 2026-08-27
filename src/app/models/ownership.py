from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

OWNERSHIP_SCHEMA_VERSION = "2"
OWNERSHIP_TABLE_NAME = "top10_float_holders"


class OwnershipSnapshot(BaseModel):
    """Content-addressed PIT top-ten-float-holder ownership overlay."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: str = OWNERSHIP_SCHEMA_VERSION
    table_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_name: str
    source_version: str | None = None
    fetched_at: str
    coverage_start: date
    coverage_end: date
    row_count: int = Field(gt=0)
    covered_symbols: int = Field(gt=0)
    base_market_snapshot_id: str = Field(min_length=1)
    fundamental_snapshot_id: str = Field(min_length=1)
    availability_policy: str
