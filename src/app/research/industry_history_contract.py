"""Offline PIT industry-history source contract (CSV + JSON manifest).

This module verifies auditable point-in-time industry history artifacts and
selects industry membership without lookahead. It does not score, backtest,
trade, invent complete industry universes, or fall back to current-static labels.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app.storage.quality import parse_available_at_utc

INDUSTRY_HISTORY_SCHEMA_VERSION: Literal["1"] = "1"
INDUSTRY_HISTORY_CONTRACT_VERSION: Literal["pit-industry-history-contract-v1"] = (
    "pit-industry-history-contract-v1"
)
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_HISTORY_COLUMNS: tuple[str, ...] = (
    "symbol",
    "industry_scheme",
    "industry_version",
    "industry_code",
    "industry_name",
    "effective_from",
    "effective_to",
    "announced_at",
    "available_at",
    "source_reference",
)

PIT_SEMANTICS: Literal["point_in_time_history"] = "point_in_time_history"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _reject_blank(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _reject_unknown_masquerade(value: object, *, field_name: str) -> object:
    if value is None:
        return None
    if value == 0 or value == 0.0:
        raise ValueError(f"{field_name} must be null when unknown, not 0")
    if isinstance(value, str) and value.strip() == "":
        raise ValueError(f"{field_name} must be null when unknown, not empty string")
    return value


def _parse_iso_date(value: object, *, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO date string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty ISO date")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


def _parse_utc_timestamp(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return parse_available_at_utc(value, name=field_name)
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a UTC timestamp string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a UTC timestamp string")
    if "T" not in text and "t" not in text:
        raise ValueError(f"{field_name} must be a UTC timestamp, not a date-only value")
    return parse_available_at_utc(text, name=field_name)


class CoverageWindow(_StrictModel):
    start: date
    end: date

    @field_validator("start", "end", mode="before")
    @classmethod
    def _parse_dates(cls, value: object, info: ValidationInfo) -> date:
        return _parse_iso_date(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _ordered(self) -> CoverageWindow:
        if self.end < self.start:
            raise ValueError("coverage end must be on or after start")
        return self


class IndustryHistoryRecord(_StrictModel):
    symbol: str = Field(min_length=1)
    industry_scheme: str = Field(min_length=1)
    industry_version: str = Field(min_length=1)
    industry_code: str = Field(min_length=1)
    industry_name: str = Field(min_length=1)
    effective_from: date
    effective_to: date | None = None
    announced_at: datetime
    available_at: datetime
    source_reference: str = Field(min_length=1)

    @field_validator(
        "symbol",
        "industry_scheme",
        "industry_version",
        "industry_code",
        "industry_name",
        "source_reference",
        mode="before",
    )
    @classmethod
    def _require_text(cls, value: object, info: ValidationInfo) -> str:
        return _reject_blank(value, field_name=str(info.field_name))

    @field_validator("effective_from", mode="before")
    @classmethod
    def _parse_effective_from(cls, value: object) -> date:
        return _parse_iso_date(value, field_name="effective_from")

    @field_validator("effective_to", mode="before")
    @classmethod
    def _parse_effective_to(cls, value: object) -> date | None:
        if value is None:
            return None
        if isinstance(value, str) and value.strip() == "":
            return None
        return _parse_iso_date(value, field_name="effective_to")

    @field_validator("announced_at", "available_at", mode="before")
    @classmethod
    def _parse_timestamps(cls, value: object, info: ValidationInfo) -> datetime:
        return _parse_utc_timestamp(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _interval_and_availability(self) -> IndustryHistoryRecord:
        if self.effective_to is not None and self.effective_from > self.effective_to:
            raise ValueError("effective_from must be on or before effective_to")
        if self.announced_at > self.available_at:
            raise ValueError("announced_at must be on or before available_at")
        return self


class IndustryHistoryManifest(_StrictModel):
    """Self-hashed manifest bound to a history CSV. Unknowns stay null — never ''/0."""

    schema_version: Literal["1"] = INDUSTRY_HISTORY_SCHEMA_VERSION
    contract_version: Literal["pit-industry-history-contract-v1"] = INDUSTRY_HISTORY_CONTRACT_VERSION
    manifest_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_name: str = Field(min_length=1)
    industry_scheme: str = Field(min_length=1)
    industry_version: str = Field(min_length=1)
    history_file: str = Field(min_length=1)
    history_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage: CoverageWindow
    available_at_definition: str = Field(min_length=1)
    available_at_evidence: str = Field(min_length=1)
    generated_at: datetime
    retrieved_at: datetime
    pit_semantics: Literal["point_in_time_history"] = PIT_SEMANTICS
    complete: bool
    universe_notes: str | None = None
    row_count: int | None = Field(default=None, ge=0)
    covered_symbols: int | None = Field(default=None, ge=0)
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    does_not_score: Literal[True] = True
    does_not_backtest: Literal[True] = True
    does_not_trade: Literal[True] = True

    @field_validator(
        "source_name",
        "industry_scheme",
        "industry_version",
        "history_file",
        "available_at_definition",
        "available_at_evidence",
        mode="before",
    )
    @classmethod
    def _require_text(cls, value: object, info: ValidationInfo) -> str:
        return _reject_blank(value, field_name=str(info.field_name))

    @field_validator("history_file_sha256", mode="before")
    @classmethod
    def _require_sha(cls, value: object) -> str:
        text = _reject_blank(value, field_name="history_file_sha256").lower()
        if SHA256_HEX.fullmatch(text) is None:
            raise ValueError("history_file_sha256 must be a 64-character hex SHA-256")
        return text

    @field_validator("generated_at", "retrieved_at", mode="before")
    @classmethod
    def _require_utc(cls, value: object, info: ValidationInfo) -> datetime:
        return _parse_utc_timestamp(value, field_name=str(info.field_name))

    @field_validator("universe_notes", "row_count", "covered_symbols", mode="before")
    @classmethod
    def _optional_unknowns(cls, value: object, info: ValidationInfo) -> object:
        return _reject_unknown_masquerade(value, field_name=str(info.field_name))

    @field_validator("pit_semantics", mode="before")
    @classmethod
    def _require_pit_semantics(cls, value: object) -> str:
        text = _reject_blank(value, field_name="pit_semantics")
        if text != PIT_SEMANTICS:
            raise ValueError(
                "pit_semantics must be point_in_time_history; "
                "current-static labels cannot masquerade as PIT industry history"
            )
        return text

    @model_validator(mode="after")
    def _ready_flags(self) -> IndustryHistoryManifest:
        if self.ready_for_scoring or self.ready_for_backtest or self.ready_for_trading:
            raise ValueError("industry history manifest cannot authorize score/backtest/trade")
        if not (self.does_not_score and self.does_not_backtest and self.does_not_trade):
            raise ValueError("does_not_score/backtest/trade must remain true")
        if self.complete and self.universe_notes is None:
            raise ValueError("complete=true requires universe_notes describing the claimed universe")
        return self


class IndustrySelectionResult(_StrictModel):
    status: Literal["known", "unknown"]
    symbol: str
    effective_date: date
    decision_at: datetime
    industry_scheme: str | None = None
    industry_version: str | None = None
    industry_code: str | None = None
    industry_name: str | None = None
    record: IndustryHistoryRecord | None = None
    unknown_reason: str | None = None

    @model_validator(mode="after")
    def _status_payload(self) -> IndustrySelectionResult:
        if self.status == "unknown":
            if self.record is not None or self.industry_code is not None or self.industry_name is not None:
                raise ValueError("unknown selection must not carry industry fields")
            if self.unknown_reason is None or self.unknown_reason.strip() == "":
                raise ValueError("unknown selection requires unknown_reason")
            return self
        if self.record is None:
            raise ValueError("known selection requires record")
        if (
            self.industry_scheme is None
            or self.industry_version is None
            or self.industry_code is None
            or self.industry_name is None
        ):
            raise ValueError("known selection requires industry fields")
        if self.unknown_reason is not None:
            raise ValueError("known selection must not set unknown_reason")
        return self


class IndustryHistoryVerification(_StrictModel):
    manifest_id: str
    source_name: str
    industry_scheme: str
    industry_version: str
    history_file_sha256: str
    coverage_start: date
    coverage_end: date
    row_count: int
    covered_symbols: int
    complete: bool
    pit_semantics: Literal["point_in_time_history"] = PIT_SEMANTICS
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    does_not_score: Literal[True] = True
    does_not_backtest: Literal[True] = True
    does_not_trade: Literal[True] = True


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_manifest_payload(manifest: IndustryHistoryManifest) -> dict[str, Any]:
    return manifest.model_dump(mode="json", exclude={"manifest_id"})


def canonical_manifest_bytes(manifest: IndustryHistoryManifest) -> bytes:
    return json.dumps(
        canonical_manifest_payload(manifest),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_manifest_id(manifest: IndustryHistoryManifest) -> str:
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def seal_industry_history_manifest(manifest: IndustryHistoryManifest) -> IndustryHistoryManifest:
    return manifest.model_copy(update={"manifest_id": compute_manifest_id(manifest)})


def assert_manifest_self_hash(manifest: IndustryHistoryManifest) -> None:
    if manifest.manifest_id is None:
        raise ValueError("industry history manifest_id is missing")
    expected = compute_manifest_id(manifest)
    if manifest.manifest_id != expected:
        raise ValueError("industry history manifest_id does not match canonical content hash")


def load_industry_history_manifest(path: Path) -> IndustryHistoryManifest:
    if not path.is_file():
        raise ValueError(f"manifest file does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest file is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be a JSON object")
    return IndustryHistoryManifest.model_validate(payload)


def load_industry_history_records(path: Path) -> list[IndustryHistoryRecord]:
    if not path.is_file():
        raise ValueError(f"history file does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError("history CSV is missing a header row")
            columns = [name.strip() for name in reader.fieldnames]
            missing = [name for name in REQUIRED_HISTORY_COLUMNS if name not in columns]
            if missing:
                raise ValueError(f"history CSV missing required columns: {', '.join(missing)}")
            extra = [name for name in columns if name and name not in REQUIRED_HISTORY_COLUMNS]
            if extra:
                raise ValueError(f"history CSV has unexpected columns: {', '.join(extra)}")
            records: list[IndustryHistoryRecord] = []
            for index, row in enumerate(reader, start=2):
                cleaned = {key: _csv_cell(row.get(key)) for key in REQUIRED_HISTORY_COLUMNS}
                try:
                    records.append(IndustryHistoryRecord.model_validate(cleaned))
                except Exception as exc:  # noqa: BLE001
                    raise ValueError(f"history CSV row {index} invalid: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"history file cannot be read: {path}") from exc
    return records


def _csv_cell(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text if text != "" else None
    return value


def verify_industry_history_source(
    *,
    history_file: Path,
    manifest_file: Path,
) -> tuple[IndustryHistoryManifest, list[IndustryHistoryRecord], IndustryHistoryVerification]:
    """Fail-closed offline verifier for PIT industry history CSV + manifest."""
    if not history_file.is_file():
        raise ValueError(f"history file does not exist: {history_file}")
    if not manifest_file.is_file():
        raise ValueError(f"manifest file does not exist: {manifest_file}")

    manifest = load_industry_history_manifest(manifest_file)
    assert_manifest_self_hash(manifest)

    digest = sha256_file(history_file)
    if digest != manifest.history_file_sha256:
        raise ValueError("history_file_sha256 does not match history CSV bytes")

    declared_name = Path(manifest.history_file).name
    if declared_name != history_file.name:
        raise ValueError(
            f"manifest history_file basename {declared_name!r} does not match "
            f"provided history file {history_file.name!r}"
        )

    records = load_industry_history_records(history_file)
    if not records:
        raise ValueError("history CSV has no data rows")

    _assert_scheme_version_match(manifest, records)
    _assert_no_overlapping_intervals(records)
    _assert_coverage_matches(manifest, records)

    covered_symbols = len({record.symbol for record in records})
    if manifest.row_count is not None and manifest.row_count != len(records):
        raise ValueError("manifest row_count does not match history CSV row count")
    if manifest.covered_symbols is not None and manifest.covered_symbols != covered_symbols:
        raise ValueError("manifest covered_symbols does not match distinct symbols in history CSV")

    verification = IndustryHistoryVerification(
        manifest_id=manifest.manifest_id or compute_manifest_id(manifest),
        source_name=manifest.source_name,
        industry_scheme=manifest.industry_scheme,
        industry_version=manifest.industry_version,
        history_file_sha256=manifest.history_file_sha256,
        coverage_start=manifest.coverage.start,
        coverage_end=manifest.coverage.end,
        row_count=len(records),
        covered_symbols=covered_symbols,
        complete=manifest.complete,
    )
    return manifest, records, verification


def select_industry_as_of(
    records: Sequence[IndustryHistoryRecord],
    symbol: str,
    effective_date: date,
    decision_at: datetime,
) -> IndustrySelectionResult:
    """Select PIT industry for (symbol, effective_date) observable at decision_at.

    Matching requires available_at <= decision_at and effective interval coverage.
    Zero matches → explicit unknown. More than one match → fail closed on ambiguity.
    Never falls back to current-static industry or end-of-interval industry.
    """
    if not isinstance(symbol, str) or symbol.strip() == "":
        raise ValueError("symbol must be a non-empty string")
    decision = parse_available_at_utc(decision_at, name="decision_at")
    matches: list[IndustryHistoryRecord] = []
    for record in records:
        if record.symbol != symbol:
            continue
        if record.available_at > decision:
            continue
        if effective_date < record.effective_from:
            continue
        if record.effective_to is not None and effective_date > record.effective_to:
            continue
        matches.append(record)

    if len(matches) == 0:
        return IndustrySelectionResult(
            status="unknown",
            symbol=symbol,
            effective_date=effective_date,
            decision_at=decision,
            unknown_reason="no_observable_industry_interval",
        )
    if len(matches) > 1:
        raise ValueError(
            f"ambiguous industry history for {symbol} on {effective_date.isoformat()} "
            f"at decision_at={decision.isoformat()}: {len(matches)} matching intervals"
        )
    record = matches[0]
    return IndustrySelectionResult(
        status="known",
        symbol=symbol,
        effective_date=effective_date,
        decision_at=decision,
        industry_scheme=record.industry_scheme,
        industry_version=record.industry_version,
        industry_code=record.industry_code,
        industry_name=record.industry_name,
        record=record,
    )


def _assert_scheme_version_match(
    manifest: IndustryHistoryManifest,
    records: Sequence[IndustryHistoryRecord],
) -> None:
    for record in records:
        if record.industry_scheme != manifest.industry_scheme:
            raise ValueError(
                f"history row scheme {record.industry_scheme!r} does not match "
                f"manifest industry_scheme {manifest.industry_scheme!r}"
            )
        if record.industry_version != manifest.industry_version:
            raise ValueError(
                f"history row version {record.industry_version!r} does not match "
                f"manifest industry_version {manifest.industry_version!r}"
            )


def _assert_no_overlapping_intervals(records: Sequence[IndustryHistoryRecord]) -> None:
    by_key: dict[tuple[str, str, str], list[IndustryHistoryRecord]] = {}
    for record in records:
        key = (record.symbol, record.industry_scheme, record.industry_version)
        by_key.setdefault(key, []).append(record)

    for key, group in by_key.items():
        ordered = sorted(group, key=lambda item: (item.effective_from, item.available_at, item.industry_code))
        for index, left in enumerate(ordered):
            left_end = left.effective_to
            for right in ordered[index + 1 :]:
                if left_end is None:
                    raise ValueError(
                        f"overlapping industry intervals for {key[0]}: open-ended interval "
                        f"from {left.effective_from.isoformat()} overlaps later rows"
                    )
                if right.effective_from <= left_end:
                    raise ValueError(
                        f"overlapping industry intervals for {key[0]}: "
                        f"{left.effective_from.isoformat()}..{left_end.isoformat()} overlaps "
                        f"{right.effective_from.isoformat()}.."
                        f"{right.effective_to.isoformat() if right.effective_to else 'open'}"
                    )


def _assert_coverage_matches(
    manifest: IndustryHistoryManifest,
    records: Sequence[IndustryHistoryRecord],
) -> None:
    starts = [record.effective_from for record in records]
    ends: list[date] = []
    for record in records:
        if record.effective_to is None:
            ends.append(record.effective_from)
        else:
            ends.append(record.effective_to)
    observed_start = min(starts)
    observed_end = max(ends)
    if observed_start != manifest.coverage.start or observed_end != manifest.coverage.end:
        raise ValueError(
            "manifest coverage does not match history CSV effective interval span: "
            f"declared {manifest.coverage.start.isoformat()}..{manifest.coverage.end.isoformat()}, "
            f"observed {observed_start.isoformat()}..{observed_end.isoformat()}"
        )
