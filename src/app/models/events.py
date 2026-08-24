from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

EVENT_SCHEMA_VERSION = "1"
EVENT_TABLE_NAMES = (
    "earnings_forecast_events",
    "earnings_express_events",
    "holder_count_events",
    "share_unlock_events",
    "audit_opinion_events",
)
EVENT_SOURCE_NAMES = (
    "forecast",
    "express",
    "stk_holdernumber",
    "share_float",
    "fina_audit",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventSourceFile(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EventSourceManifest(_StrictModel):
    """Provenance supplied with the five raw, offline event exports."""

    schema_version: Literal["1"] = "1"
    source_name: str = Field(min_length=1)
    source_version: str | None = None
    fetched_at: datetime
    coverage_start: date
    coverage_end: date
    files: dict[str, EventSourceFile]
    availability_evidence: dict[str, str]
    notes: str | None = None

    @model_validator(mode="after")
    def complete_contract(self) -> EventSourceManifest:
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("event source fetched_at must include a timezone offset")
        if self.coverage_end < self.coverage_start:
            raise ValueError("event source coverage_end precedes coverage_start")
        if self.coverage_end > self.fetched_at.astimezone(ZoneInfo("Asia/Shanghai")).date():
            raise ValueError("event source coverage_end is later than fetched_at")
        expected = set(EVENT_SOURCE_NAMES)
        if set(self.files) != expected:
            raise ValueError(
                f"event source files must contain exactly {sorted(expected)}"
            )
        if set(self.availability_evidence) != expected:
            raise ValueError(
                "availability_evidence must contain exactly the five event sources"
            )
        if any(not value.strip() for value in self.availability_evidence.values()):
            raise ValueError("availability_evidence entries cannot be blank")
        paths = [item.path for item in self.files.values()]
        if len(paths) != len(set(paths)):
            raise ValueError("event source file paths must be unique")
        return self


class EventSnapshot(_StrictModel):
    """Content-addressed, market-bound A-share event overlay."""

    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal["1"] = "1"
    table_hashes: dict[str, str]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_name: str
    source_version: str | None = None
    fetched_at: str
    coverage_start: date
    coverage_end: date
    row_counts: dict[str, int]
    covered_symbols: int = Field(ge=1)
    base_market_snapshot_id: str = Field(min_length=1)
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    availability_policy: str
