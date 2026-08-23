from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, ValidationInfo, field_validator, model_validator

from app.errors import DataQualityError
from app.models.config import StrategyConfig, StrictModel
from app.storage.quality import parse_available_at_utc
from app.universe.materialize import read_universe_snapshots_file

PROVENANCE_SCHEMA_VERSION = "1"
SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


def sha256_file_bytes(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require_nonblank_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{name} must be a non-empty string")
    return text


class EffectiveFromCoverage(StrictModel):
    start: date
    end: date

    @field_validator("start", "end", mode="before")
    @classmethod
    def parse_iso_date(cls, value: object) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            raise ValueError("effective_from_coverage dates must be ISO strings")
        text = value.strip()
        if not text:
            raise ValueError("effective_from_coverage dates must be non-empty")
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("effective_from_coverage dates must be ISO dates") from exc

    @model_validator(mode="after")
    def start_on_or_before_end(self) -> EffectiveFromCoverage:
        if self.end < self.start:
            raise ValueError("effective_from_coverage end must be on or after start")
        return self


class UniverseSourceManifest(StrictModel):
    schema_version: Literal["1"]
    universe_id: str
    source_name: str
    snapshots_file_sha256: str
    file_obtained_at: datetime
    effective_from_coverage: EffectiveFromCoverage
    available_at_definition: str
    available_at_evidence: str
    expected_constituents: int = Field(gt=0)
    source_url: str | None = None
    announcement_id: str | None = None
    source_note: str | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_schema_version(cls, value: object) -> str:
        return _require_nonblank_str(value, "schema_version")

    @field_validator("universe_id", "source_name", "available_at_definition", "available_at_evidence", mode="before")
    @classmethod
    def require_text(cls, value: object, info: ValidationInfo) -> str:
        return _require_nonblank_str(value, str(info.field_name))

    @field_validator("snapshots_file_sha256", mode="before")
    @classmethod
    def require_sha256(cls, value: object) -> str:
        text = _require_nonblank_str(value, "snapshots_file_sha256")
        if SHA256_HEX.fullmatch(text) is None:
            raise ValueError("snapshots_file_sha256 must be a 64-character hex SHA-256")
        return text.lower()

    @field_validator("file_obtained_at", mode="before")
    @classmethod
    def require_utc_obtained_at(cls, value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("file_obtained_at must be a UTC timestamp string")
        text = value.strip()
        if not text:
            raise ValueError("file_obtained_at must be a UTC timestamp string")
        if "T" not in text and "t" not in text:
            raise ValueError("file_obtained_at must be a UTC timestamp, not a date-only value")
        return parse_available_at_utc(text, name="file_obtained_at")

    @field_validator("expected_constituents", mode="before")
    @classmethod
    def require_positive_int(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("expected_constituents must be a positive integer")
        return value

    @field_validator("source_url", "announcement_id", "source_note", mode="before")
    @classmethod
    def optional_source_ref(cls, value: object, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _require_nonblank_str(value, str(info.field_name))

    @model_validator(mode="after")
    def require_source_reference(self) -> UniverseSourceManifest:
        if not any((self.source_url, self.announcement_id, self.source_note)):
            raise ValueError("provenance requires at least one of source_url, announcement_id, or source_note")
        return self


@dataclass(frozen=True)
class UniverseSourceVerification:
    universe_id: str
    source_name: str
    snapshots_file_sha256: str
    effective_from_start: date
    effective_from_end: date
    snapshot_count: int
    expected_constituents: int


def _format_validation(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err.get("loc", ()))
        typ = err.get("type", "")
        msg = err.get("msg", "invalid")
        if typ == "extra_forbidden":
            parts.append(f"provenance has unknown field '{loc}'")
        elif typ == "missing":
            parts.append(f"provenance missing required field '{loc}'")
        elif loc:
            parts.append(f"provenance {loc}: {msg}")
        else:
            parts.append(f"provenance {msg}")
    return "; ".join(parts) or "provenance file is invalid"


def load_universe_source_manifest(path: Path) -> UniverseSourceManifest:
    source = Path(path)
    if not source.is_file():
        raise DataQualityError(f"provenance file not found: {source.name}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DataQualityError("provenance file is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DataQualityError("provenance file must be a JSON object")
    try:
        return UniverseSourceManifest.model_validate(payload)
    except ValidationError as exc:
        raise DataQualityError(_format_validation(exc)) from exc


def verify_universe_source(
    *,
    snapshots_file: Path,
    provenance_file: Path,
    config: StrategyConfig,
) -> UniverseSourceVerification:
    if config.universe.mode != "historical_membership":
        raise DataQualityError(
            "verify-universe-source requires universe.mode=historical_membership; "
            f"got {config.universe.mode}"
        )
    manifest = load_universe_source_manifest(provenance_file)
    if manifest.universe_id != config.universe.id:
        raise DataQualityError(
            f"provenance universe_id '{manifest.universe_id}' "
            f"does not match config universe.id '{config.universe.id}'"
        )
    snapshots_path = Path(snapshots_file)
    if not snapshots_path.is_file():
        raise DataQualityError(f"universe snapshot file not found: {snapshots_path.name}")
    digest = sha256_file_bytes(snapshots_path)
    if digest != manifest.snapshots_file_sha256:
        raise DataQualityError(
            "snapshots_file_sha256 does not match the exact bytes of the snapshots file"
        )
    snapshots = read_universe_snapshots_file(snapshots_path)
    ids = {item.universe_id for item in snapshots}
    if ids != {manifest.universe_id} or ids != {config.universe.id}:
        raise DataQualityError(
            f"universe snapshot universe_id {sorted(ids)} "
            f"does not match provenance/config universe.id '{config.universe.id}'"
        )
    expected = manifest.expected_constituents
    strategy_expected = config.universe.expected_constituents
    if strategy_expected is not None and strategy_expected != expected:
        raise DataQualityError(
            f"strategy expected_constituents={strategy_expected} "
            f"does not match provenance expected_constituents={expected}"
        )
    for item in snapshots:
        if len(item.members) != expected:
            raise DataQualityError(
                f"universe snapshot expected_constituents={expected} "
                f"but effective_from={item.effective_from.isoformat()} has {len(item.members)} members"
            )
    actual_start = min(item.effective_from for item in snapshots)
    actual_end = max(item.effective_from for item in snapshots)
    coverage = manifest.effective_from_coverage
    if coverage.start != actual_start or coverage.end != actual_end:
        raise DataQualityError(
            f"provenance effective_from_coverage {coverage.start.isoformat()}..{coverage.end.isoformat()} "
            f"does not match snapshot effective_from min/max "
            f"{actual_start.isoformat()}..{actual_end.isoformat()}"
        )
    return UniverseSourceVerification(
        universe_id=manifest.universe_id,
        source_name=manifest.source_name,
        snapshots_file_sha256=digest,
        effective_from_start=actual_start,
        effective_from_end=actual_end,
        snapshot_count=len(snapshots),
        expected_constituents=expected,
    )
