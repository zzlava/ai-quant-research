from __future__ import annotations

import csv
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
from app.universe.materialize import SnapshotCrossSection, read_universe_snapshots_file

PROVENANCE_SCHEMA_VERSION = "2"
SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
EVENT_EVIDENCE_COLUMNS = (
    "effective_from",
    "available_at",
    "availability_basis",
    "source_published_on",
    "evidence_type",
    "source_url",
    "source_document",
    "source_document_sha256",
)
AVAILABILITY_BASES = frozenset(
    {
        "observed_source_timestamp",
        "conservative_next_cn_decision_after_notice_date",
        "licensed_delivery_timestamp",
    }
)
EVIDENCE_TYPES = frozenset(
    {
        "official_constituent_list",
        "official_adjustment_notice",
        "public_media_report",
        "public_api_response",
        "reconstruction_artifact",
    }
)


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


class EventEvidenceLedgerReference(StrictModel):
    path: str
    sha256: str

    @field_validator("path", mode="before")
    @classmethod
    def require_path(cls, value: object) -> str:
        return _require_nonblank_str(value, "event_evidence_ledger.path")

    @field_validator("sha256", mode="before")
    @classmethod
    def require_sha256(cls, value: object) -> str:
        text = _require_nonblank_str(value, "event_evidence_ledger.sha256")
        if SHA256_HEX.fullmatch(text) is None:
            raise ValueError("event_evidence_ledger.sha256 must be a 64-character hex SHA-256")
        return text.lower()


class UniverseSourceManifest(StrictModel):
    schema_version: Literal["1", "2"]
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
    event_evidence_ledger: EventEvidenceLedgerReference | None = None

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
        if self.schema_version == "2" and self.event_evidence_ledger is None:
            raise ValueError("schema_version=2 requires event_evidence_ledger")
        if self.schema_version == "1" and self.event_evidence_ledger is not None:
            raise ValueError("schema_version=1 must not include event_evidence_ledger; use schema_version=2")
        return self


@dataclass(frozen=True)
class EventEvidence:
    effective_from: date
    available_at: datetime
    availability_basis: str
    source_published_on: date
    evidence_type: str
    source_url: str
    source_document: Path
    source_document_sha256: str


@dataclass(frozen=True)
class UniverseSourceVerification:
    schema_version: str
    universe_id: str
    source_name: str
    snapshots_file_sha256: str
    effective_from_start: date
    effective_from_end: date
    snapshot_count: int
    expected_constituents: int
    event_evidence_count: int | None
    event_evidence_ledger_sha256: str | None


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


def _resolve_relative_artifact(*, base_dir: Path, value: str, field_name: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise DataQualityError(f"{field_name} must be a relative path inside the provenance directory")
    root = base_dir.resolve()
    resolved = (root / relative).resolve()
    if resolved != root and root not in resolved.parents:
        raise DataQualityError(f"{field_name} must resolve inside the provenance directory")
    return resolved


def _parse_event_date(value: object, *, field_name: str, line: int) -> date:
    if not isinstance(value, str) or not value.strip():
        raise DataQualityError(f"event evidence row {line} {field_name} must be an ISO date")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise DataQualityError(f"event evidence row {line} {field_name} must be an ISO date") from exc


def _parse_event_available_at(value: object, *, line: int) -> datetime:
    if not isinstance(value, str) or not value.strip() or "T" not in value.upper():
        raise DataQualityError(
            f"event evidence row {line} available_at must be a UTC timestamp, not a date-only value"
        )
    return parse_available_at_utc(value.strip(), name=f"event evidence row {line} available_at")


def _require_event_value(value: object, *, field_name: str, line: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataQualityError(f"event evidence row {line} {field_name} must be a non-empty string")
    return value.strip()


def _require_event_sha256(value: object, *, field_name: str, line: int) -> str:
    text = _require_event_value(value, field_name=field_name, line=line)
    if SHA256_HEX.fullmatch(text) is None:
        raise DataQualityError(f"event evidence row {line} {field_name} must be a 64-character hex SHA-256")
    return text.lower()


def read_event_evidence_ledger(path: Path, *, provenance_dir: Path) -> list[EventEvidence]:
    source = Path(path)
    if not source.is_file():
        raise DataQualityError(f"event evidence ledger not found: {source.name}")
    try:
        with source.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            actual_columns = tuple(reader.fieldnames or ())
            if actual_columns != EVENT_EVIDENCE_COLUMNS:
                missing = sorted(set(EVENT_EVIDENCE_COLUMNS) - set(actual_columns))
                unknown = sorted(set(actual_columns) - set(EVENT_EVIDENCE_COLUMNS))
                details: list[str] = []
                if missing:
                    details.append(f"missing required columns {missing}")
                if unknown:
                    details.append(f"unknown columns {unknown}")
                if not details:
                    details.append("columns must use the documented order")
                raise DataQualityError("event evidence ledger " + "; ".join(details))
            raw_rows = list(reader)
    except UnicodeDecodeError as exc:
        raise DataQualityError("event evidence ledger must be UTF-8 CSV") from exc
    if not raw_rows:
        raise DataQualityError("event evidence ledger has no rows")

    evidence: list[EventEvidence] = []
    seen_effective_from: set[date] = set()
    for line, row in enumerate(raw_rows, start=2):
        if None in row:
            raise DataQualityError(f"event evidence row {line} has more values than documented columns")
        effective_from = _parse_event_date(row.get("effective_from"), field_name="effective_from", line=line)
        if effective_from in seen_effective_from:
            raise DataQualityError(
                f"event evidence ledger has duplicate effective_from {effective_from.isoformat()}"
            )
        seen_effective_from.add(effective_from)
        available_at = _parse_event_available_at(row.get("available_at"), line=line)
        availability_basis = _require_event_value(
            row.get("availability_basis"), field_name="availability_basis", line=line
        )
        if availability_basis not in AVAILABILITY_BASES:
            allowed = ", ".join(sorted(AVAILABILITY_BASES))
            raise DataQualityError(
                f"event evidence row {line} availability_basis must be one of: {allowed}"
            )
        source_published_on = _parse_event_date(
            row.get("source_published_on"), field_name="source_published_on", line=line
        )
        if source_published_on > available_at.date():
            raise DataQualityError(
                f"event evidence row {line} source_published_on cannot be after available_at"
            )
        if (
            availability_basis == "conservative_next_cn_decision_after_notice_date"
            and source_published_on >= available_at.date()
        ):
            raise DataQualityError(
                f"event evidence row {line} conservative availability must be after source_published_on"
            )
        evidence_type = _require_event_value(row.get("evidence_type"), field_name="evidence_type", line=line)
        if evidence_type not in EVIDENCE_TYPES:
            allowed = ", ".join(sorted(EVIDENCE_TYPES))
            raise DataQualityError(f"event evidence row {line} evidence_type must be one of: {allowed}")
        source_url = _require_event_value(row.get("source_url"), field_name="source_url", line=line)
        source_document = _resolve_relative_artifact(
            base_dir=provenance_dir,
            value=_require_event_value(row.get("source_document"), field_name="source_document", line=line),
            field_name=f"event evidence row {line} source_document",
        )
        if not source_document.is_file():
            raise DataQualityError(f"event evidence row {line} source_document not found: {source_document.name}")
        document_sha256 = _require_event_sha256(
            row.get("source_document_sha256"), field_name="source_document_sha256", line=line
        )
        if sha256_file_bytes(source_document) != document_sha256:
            raise DataQualityError(
                f"event evidence row {line} source_document_sha256 does not match exact document bytes"
            )
        evidence.append(
            EventEvidence(
                effective_from=effective_from,
                available_at=available_at,
                availability_basis=availability_basis,
                source_published_on=source_published_on,
                evidence_type=evidence_type,
                source_url=source_url,
                source_document=source_document,
                source_document_sha256=document_sha256,
            )
        )
    return sorted(evidence, key=lambda item: item.effective_from)


def _verify_event_evidence(
    *, manifest: UniverseSourceManifest, provenance_file: Path, snapshots: list[SnapshotCrossSection]
) -> tuple[int | None, str | None]:
    if manifest.schema_version == "1":
        return None, None
    ledger_ref = manifest.event_evidence_ledger
    if ledger_ref is None:  # Defensive guard; the model validator already rejects this state.
        raise DataQualityError("schema_version=2 requires event_evidence_ledger")
    ledger_path = _resolve_relative_artifact(
        base_dir=provenance_file.parent,
        value=ledger_ref.path,
        field_name="event_evidence_ledger.path",
    )
    ledger_sha256 = sha256_file_bytes(ledger_path) if ledger_path.is_file() else ""
    if ledger_sha256 != ledger_ref.sha256:
        raise DataQualityError("event_evidence_ledger.sha256 does not match the exact bytes of the ledger")
    evidence = read_event_evidence_ledger(ledger_path, provenance_dir=provenance_file.parent)
    snapshots_by_date = {item.effective_from: item for item in snapshots}
    evidence_by_date = {item.effective_from: item for item in evidence}
    snapshot_dates = set(snapshots_by_date)
    evidence_dates = set(evidence_by_date)
    if snapshot_dates != evidence_dates:
        missing = sorted(day.isoformat() for day in snapshot_dates - evidence_dates)
        orphaned = sorted(day.isoformat() for day in evidence_dates - snapshot_dates)
        details: list[str] = []
        if missing:
            details.append(f"missing snapshot dates {missing}")
        if orphaned:
            details.append(f"orphaned ledger dates {orphaned}")
        raise DataQualityError("event evidence ledger must cover each snapshot exactly once: " + "; ".join(details))
    for effective_from, snapshot in snapshots_by_date.items():
        if evidence_by_date[effective_from].available_at != snapshot.available_at:
            raise DataQualityError(
                "event evidence available_at does not match snapshot available_at for effective_from="
                f"{effective_from.isoformat()}"
            )
    return len(evidence), ledger_sha256


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
    evidence_count, evidence_ledger_sha256 = _verify_event_evidence(
        manifest=manifest,
        provenance_file=Path(provenance_file),
        snapshots=snapshots,
    )
    return UniverseSourceVerification(
        schema_version=manifest.schema_version,
        universe_id=manifest.universe_id,
        source_name=manifest.source_name,
        snapshots_file_sha256=digest,
        effective_from_start=actual_start,
        effective_from_end=actual_end,
        snapshot_count=len(snapshots),
        expected_constituents=expected,
        event_evidence_count=evidence_count,
        event_evidence_ledger_sha256=evidence_ledger_sha256,
    )
