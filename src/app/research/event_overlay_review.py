from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import median
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from app.clock import decision_at_utc
from app.models.config import StrategyConfig
from app.models.events import (
    EVENT_TABLE_NAMES,
    EventSnapshot,
    EventSourceManifest,
)
from app.providers.tushare_events import EVENT_COLUMNS, SOURCE_TO_TABLE
from app.storage.snapshot_io import load_verified_snapshot

EVENT_OVERLAY_REVIEW_SCHEMA_VERSION: Literal["1"] = "1"
ANNUAL_REVIEW_COLUMNS = (
    "year",
    "source_name",
    "table_name",
    "announcement_row_count",
    "announcement_symbol_count",
    "pit_visible_row_count_at_year_end",
    "pit_visible_symbol_count_at_year_end",
    "logical_groups",
    "groups_with_multiple_announcement_dates",
    "max_announcement_versions",
    "timing_metric",
    "timing_observations",
    "timing_minimum_days",
    "timing_median_days",
    "timing_maximum_days",
)
TABLE_TO_SOURCE = {table: source for source, table in SOURCE_TO_TABLE.items()}
REVISION_GROUP_KEYS: dict[str, tuple[str, ...]] = {
    "earnings_forecast_events": ("symbol", "report_period"),
    "earnings_express_events": ("symbol", "report_period"),
    "holder_count_events": ("symbol", "end_date"),
    "share_unlock_events": (
        "symbol",
        "float_date",
        "holder_name",
        "share_type",
        "float_share",
        "float_ratio",
    ),
    "audit_opinion_events": ("symbol", "report_period"),
}
TIMING_COMPARE_COLUMN: dict[str, str] = {
    "earnings_forecast_events": "report_period",
    "earnings_express_events": "report_period",
    "holder_count_events": "end_date",
    "share_unlock_events": "float_date",
    "audit_opinion_events": "report_period",
}
IDENTITY_COLUMNS = frozenset({"symbol", "ann_date", "available_at", "source_row_hash"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldMissingness(_StrictModel):
    field: str
    null_or_blank_rows: int = Field(ge=0)


class AnnualSourceReview(_StrictModel):
    year: int
    source_name: str
    table_name: str
    announcement_row_count: int = Field(ge=0)
    announcement_symbol_count: int = Field(ge=0)
    pit_visible_row_count_at_year_end: int = Field(ge=0)
    pit_visible_symbol_count_at_year_end: int = Field(ge=0)
    field_missingness: list[FieldMissingness]
    logical_groups: int = Field(ge=0)
    groups_with_multiple_announcement_dates: int = Field(ge=0)
    max_announcement_versions: int = Field(ge=0)
    timing_metric: str
    timing_observations: int = Field(ge=0)
    timing_minimum_days: int | None = None
    timing_median_days: float | None = None
    timing_maximum_days: int | None = None


class HolderCountMissingness(_StrictModel):
    raw_collection_holder_rows: int = Field(ge=0)
    raw_collection_holder_num_blank_rows: int = Field(ge=0)
    canonical_holder_rows_in_window: int = Field(ge=0)
    symbols_with_canonical_holder_observation: int = Field(ge=0)
    listed_symbols_in_window_end: int = Field(ge=0)
    symbols_with_no_observable_canonical_holder_data: int = Field(ge=0)
    semantics: str = (
        "raw_collection_holder_num_blank_rows is taken only from the verified collector "
        "quality_report sources.stk_holdernumber.field_missing_counts.holder_num; blank "
        "raw holder_num rows are excluded from the canonical overlay and must never be "
        "inferred as zero from canonical tables. "
        "symbols_with_no_observable_canonical_holder_data is coverage absence, never a "
        "zero holder count. Unavailable source-level missingness must never be reported as 0."
    )


class UnlockRatioCoverage(_StrictModel):
    raw_collection_unlock_rows: int = Field(ge=0)
    raw_collection_float_ratio_blank_rows: int = Field(ge=0)
    canonical_unlock_rows_in_window: int = Field(ge=0)
    canonical_float_ratio_known_rows: int = Field(ge=0)
    canonical_float_ratio_missing_rows: int = Field(ge=0)
    canonical_float_ratio_known_ratio: float | None = None
    semantics: str = (
        "raw_collection_float_ratio_blank_rows is taken only from the verified collector "
        "quality_report sources.share_float.field_missing_counts.float_ratio. "
        "Missing float_ratio is missing coverage, not a zero unlock risk. "
        "Unavailable source-level missingness must never be reported as 0."
    )


class VerifiedCollectionQualityProvenance(_StrictModel):
    """Offline-verified collection counters bound into the review report_id."""

    collection_source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_quality_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_collection_holder_rows: int = Field(ge=0)
    raw_collection_holder_num_blank_rows: int = Field(ge=0)
    raw_collection_unlock_rows: int = Field(ge=0)
    raw_collection_float_ratio_blank_rows: int = Field(ge=0)


class PitAvailabilityProbe(_StrictModel):
    as_of_date: date
    decision_at_utc: datetime
    listed_symbols: int = Field(ge=0)
    visible_event_rows: dict[str, int]
    same_day_announcement_rows_not_yet_visible: dict[str, int]
    symbols_with_visible_data: dict[str, int]
    symbols_with_no_observable_data: dict[str, int]
    semantics: str = (
        "available_at uses ann_date 23:59 Asia/Shanghai; strategy decision_time never "
        "treats ann_date itself as known at the open or at the same-day close. "
        "symbols_with_no_observable_data is coverage absence, not a zero-valued risk."
    )


class EventOverlayReviewReport(_StrictModel):
    schema_version: Literal["1"] = EVENT_OVERLAY_REVIEW_SCHEMA_VERSION
    strategy_config_hash: str
    market_snapshot_id: str
    event_snapshot_id: str
    source_manifest_sha256: str
    collection_source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_quality_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    window_start: date
    window_end: date
    event_source_coverage_start: date
    event_source_coverage_end: date
    annual_by_source: list[AnnualSourceReview]
    audit_result_distribution: dict[str, int]
    audit_results_requiring_manual_classification: list[str]
    holder_count_missingness: HolderCountMissingness
    unlock_ratio_coverage: UnlockRatioCoverage
    forecast_type_transition_counts: dict[str, int]
    pit_availability_probes: list[PitAvailabilityProbe]
    annual_review_file: str = "annual_source_review.parquet"
    annual_review_file_sha256: str | None = None
    report_id: str | None = None
    ready_for_scoring: bool = False
    ready_for_trading: bool = False
    research_boundary: str = (
        "Offline event-overlay coverage review only; no threshold, exclusion, score, "
        "order, trade, or alpha claim is authorized"
    )


def load_verified_collection_quality_provenance(
    source_collection_dir: Path,
    *,
    expected_source_manifest_sha256: str,
) -> VerifiedCollectionQualityProvenance:
    """Load collection quality counters only after offline hash binding succeeds.

    Fail closed on missing artifacts, hash mismatch, or missing source-level counters.
    Never invent a zero for unavailable raw missingness.
    """
    root = Path(source_collection_dir)
    collection_manifest_path = root / "collection_manifest.json"
    source_manifest_path = root / "source_manifest.json"
    quality_report_path = root / "quality_report.json"
    for path, label in (
        (collection_manifest_path, "collection_manifest.json"),
        (source_manifest_path, "source_manifest.json"),
        (quality_report_path, "quality_report.json"),
    ):
        if not path.is_file():
            raise ValueError(f"source collection directory is missing {label}")

    try:
        collection_manifest = json.loads(collection_manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("collection_manifest.json is invalid") from exc
    if not isinstance(collection_manifest, dict):
        raise ValueError("collection_manifest.json must be an object")

    source_manifest_sha256 = _sha256_file(source_manifest_path)
    quality_report_sha256 = _sha256_file(quality_report_path)
    declared_source = collection_manifest.get("source_manifest_sha256")
    declared_quality = collection_manifest.get("quality_report_sha256")
    if declared_source != source_manifest_sha256:
        raise ValueError(
            "collection_manifest source_manifest_sha256 does not match source_manifest.json"
        )
    if declared_quality != quality_report_sha256:
        raise ValueError(
            "collection_manifest quality_report_sha256 does not match quality_report.json"
        )
    if source_manifest_sha256 != expected_source_manifest_sha256:
        raise ValueError(
            "collection source_manifest hash does not match the event snapshot "
            "source_manifest_sha256"
        )

    try:
        quality = json.loads(quality_report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("quality_report.json is invalid") from exc
    if not isinstance(quality, dict):
        raise ValueError("quality_report.json must be an object")
    boundary = quality.get("research_boundary")
    if (
        not isinstance(boundary, dict)
        or boundary.get("ready_for_scoring") is not False
        or boundary.get("ready_for_trading") is not False
    ):
        raise ValueError("collection quality report violates research boundaries")

    sources = quality.get("sources")
    if not isinstance(sources, dict):
        raise ValueError("quality_report.json is missing sources")
    holder_source = sources.get("stk_holdernumber")
    unlock_source = sources.get("share_float")
    if not isinstance(holder_source, dict) or not isinstance(unlock_source, dict):
        raise ValueError("quality_report.json is missing holder or unlock source diagnostics")

    holder_missing = holder_source.get("field_missing_counts")
    unlock_missing = unlock_source.get("field_missing_counts")
    if not isinstance(holder_missing, dict) or "holder_num" not in holder_missing:
        raise ValueError(
            "quality_report.json is missing sources.stk_holdernumber."
            "field_missing_counts.holder_num; refusing to treat raw blank holder rows as zero"
        )
    if not isinstance(unlock_missing, dict) or "float_ratio" not in unlock_missing:
        raise ValueError(
            "quality_report.json is missing sources.share_float."
            "field_missing_counts.float_ratio; refusing to treat raw blank float_ratio as zero"
        )

    return VerifiedCollectionQualityProvenance(
        collection_source_manifest_sha256=source_manifest_sha256,
        collection_quality_report_sha256=quality_report_sha256,
        raw_collection_holder_rows=_require_nonneg_int(
            holder_source.get("raw_rows"),
            "sources.stk_holdernumber.raw_rows",
        ),
        raw_collection_holder_num_blank_rows=_require_nonneg_int(
            holder_missing.get("holder_num"),
            "sources.stk_holdernumber.field_missing_counts.holder_num",
        ),
        raw_collection_unlock_rows=_require_nonneg_int(
            unlock_source.get("raw_rows"),
            "sources.share_float.raw_rows",
        ),
        raw_collection_float_ratio_blank_rows=_require_nonneg_int(
            unlock_missing.get("float_ratio"),
            "sources.share_float.field_missing_counts.float_ratio",
        ),
    )


def build_event_overlay_review(
    *,
    market_dir: Path,
    event_snapshot: EventSnapshot,
    event_source_manifest: EventSourceManifest,
    event_tables: dict[str, pl.DataFrame],
    config: StrategyConfig,
    window_start: date,
    window_end: date,
    source_collection_dir: Path,
) -> tuple[EventOverlayReviewReport, pl.DataFrame]:
    """Build a deterministic coverage/PIT review of a verified five-table event overlay."""
    if window_end < window_start:
        raise ValueError("review window_end precedes window_start")
    market = load_verified_snapshot(Path(market_dir))
    if event_snapshot.base_market_snapshot_id != market.snapshot_id:
        raise ValueError("event overlay is bound to a different market snapshot")
    _assert_source_binding(event_snapshot, event_source_manifest)
    collection_quality = load_verified_collection_quality_provenance(
        source_collection_dir,
        expected_source_manifest_sha256=event_snapshot.source_manifest_sha256,
    )
    if market.coverage_start is None or market.coverage_end is None:
        raise ValueError("market snapshot coverage is incomplete")
    if not market.coverage_start <= window_start <= window_end <= market.coverage_end:
        raise ValueError("review window is outside the market snapshot coverage")
    if not (
        event_source_manifest.coverage_start
        <= window_start
        <= window_end
        <= event_source_manifest.coverage_end
    ):
        raise ValueError("review window is outside the declared event source coverage")
    if set(event_tables) != set(EVENT_TABLE_NAMES):
        raise ValueError("event overlay review requires all five verified event tables")

    calendar = pl.read_parquet(Path(market_dir) / "calendar.parquet")
    if "date" not in calendar.columns:
        raise ValueError("market calendar is missing date")
    trading_days = sorted(
        day
        for day in calendar["date"].to_list()
        if isinstance(day, date) and window_start <= day <= window_end
    )
    if not trading_days:
        raise ValueError("review window contains no trading days in the market snapshot")

    instruments = pl.read_parquet(Path(market_dir) / "instruments.parquet")
    required = {"symbol", "listing_date", "is_index", "is_global"}
    missing = sorted(required - set(instruments.columns))
    if missing:
        raise ValueError(f"market instruments missing event review columns: {missing}")

    windowed = {
        name: table.filter(
            (pl.col("ann_date") >= pl.lit(window_start))
            & (pl.col("ann_date") <= pl.lit(window_end))
        )
        for name, table in event_tables.items()
    }
    years = sorted({day.year for day in trading_days})
    year_end_decisions = {
        year: decision_at_utc(_year_end_trading_day(trading_days, year), config.data)
        for year in years
    }
    annual: list[AnnualSourceReview] = []
    for year in years:
        year_cutoff = year_end_decisions[year]
        for table_name in EVENT_TABLE_NAMES:
            annual.append(
                _annual_source_review(
                    windowed[table_name],
                    table_name=table_name,
                    year=year,
                    year_end_cutoff=year_cutoff,
                )
            )

    audit = windowed["audit_opinion_events"]
    audit_distribution = Counter(
        str(value) for value in audit["audit_result"].to_list() if value is not None
    )
    non_exact = sorted(value for value in audit_distribution if value != "标准无保留意见")
    holder = windowed["holder_count_events"]
    unlock = windowed["share_unlock_events"]
    listed_at_end = _listed_symbols(instruments, as_of=trading_days[-1])
    holder_symbols = set(holder["symbol"].to_list()) if holder.height else set()
    known_ratio = unlock.filter(pl.col("float_ratio").is_not_null()).height
    missing_ratio = unlock.filter(pl.col("float_ratio").is_null()).height

    probes = _pit_probes(
        event_tables=event_tables,
        instruments=instruments,
        trading_days=trading_days,
        config=config,
        window_start=window_start,
        window_end=window_end,
    )
    report = EventOverlayReviewReport(
        strategy_config_hash=config.config_hash(),
        market_snapshot_id=market.snapshot_id,
        event_snapshot_id=event_snapshot.snapshot_id,
        source_manifest_sha256=event_snapshot.source_manifest_sha256,
        collection_source_manifest_sha256=(
            collection_quality.collection_source_manifest_sha256
        ),
        collection_quality_report_sha256=(
            collection_quality.collection_quality_report_sha256
        ),
        window_start=window_start,
        window_end=window_end,
        event_source_coverage_start=event_source_manifest.coverage_start,
        event_source_coverage_end=event_source_manifest.coverage_end,
        annual_by_source=annual,
        audit_result_distribution=dict(sorted(audit_distribution.items())),
        audit_results_requiring_manual_classification=non_exact,
        holder_count_missingness=HolderCountMissingness(
            raw_collection_holder_rows=collection_quality.raw_collection_holder_rows,
            raw_collection_holder_num_blank_rows=(
                collection_quality.raw_collection_holder_num_blank_rows
            ),
            canonical_holder_rows_in_window=holder.height,
            symbols_with_canonical_holder_observation=len(holder_symbols),
            listed_symbols_in_window_end=len(listed_at_end),
            symbols_with_no_observable_canonical_holder_data=len(
                listed_at_end - holder_symbols
            ),
        ),
        unlock_ratio_coverage=UnlockRatioCoverage(
            raw_collection_unlock_rows=collection_quality.raw_collection_unlock_rows,
            raw_collection_float_ratio_blank_rows=(
                collection_quality.raw_collection_float_ratio_blank_rows
            ),
            canonical_unlock_rows_in_window=unlock.height,
            canonical_float_ratio_known_rows=known_ratio,
            canonical_float_ratio_missing_rows=missing_ratio,
            canonical_float_ratio_known_ratio=(
                known_ratio / unlock.height if unlock.height > 0 else None
            ),
        ),
        forecast_type_transition_counts=_forecast_transitions(
            windowed["earnings_forecast_events"]
        ),
        pit_availability_probes=probes,
    )
    return report, _annual_frame(annual)


def write_event_overlay_review_atomically(
    output_dir: Path,
    report: EventOverlayReviewReport,
    annual_frame: pl.DataFrame,
    *,
    replace_existing: bool = False,
) -> EventOverlayReviewReport:
    if tuple(annual_frame.columns) != ANNUAL_REVIEW_COLUMNS:
        raise ValueError("event overlay review annual columns do not match the schema")
    if annual_frame.height != len(report.annual_by_source):
        raise ValueError("event overlay review annual frame row count does not match report")
    destination = Path(output_dir)
    if destination.exists() and not replace_existing:
        raise ValueError("event overlay review output already exists; pass --replace-existing explicitly")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".event-overlay-review-{uuid.uuid4().hex}"
    backup = destination.parent / f".event-overlay-review-bak-{uuid.uuid4().hex}"
    try:
        temporary.mkdir(parents=True)
        parquet_path = temporary / report.annual_review_file
        annual_frame.write_parquet(parquet_path)
        with_file_hash = report.model_copy(
            update={"annual_review_file_sha256": _sha256_file(parquet_path)}
        )
        sealed = with_file_hash.model_copy(update={"report_id": _report_id(with_file_hash)})
        (temporary / "report.json").write_text(
            sealed.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            destination.rename(backup)
        temporary.rename(destination)
        if backup.exists():
            shutil.rmtree(backup)
        return sealed
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if backup.exists() and not destination.exists():
            backup.rename(destination)
        elif backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        raise


def load_verified_event_overlay_review(
    output_dir: Path,
) -> tuple[EventOverlayReviewReport, pl.DataFrame]:
    root = Path(output_dir)
    try:
        report = EventOverlayReviewReport.model_validate_json(
            (root / "report.json").read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError("event overlay review report is missing or invalid") from exc
    if report.report_id is None or report.report_id != _report_id(report):
        raise ValueError("event overlay review report ID does not match its content")
    if report.ready_for_scoring or report.ready_for_trading:
        raise ValueError("event overlay review report violates research boundaries")
    path = root / report.annual_review_file
    if not path.is_file() or report.annual_review_file_sha256 != _sha256_file(path):
        raise ValueError("event overlay review parquet hash does not match report")
    frame = pl.read_parquet(path)
    if tuple(frame.columns) != ANNUAL_REVIEW_COLUMNS or frame.height != len(
        report.annual_by_source
    ):
        raise ValueError("event overlay review parquet does not match report schema or row count")
    return report, frame


def _assert_source_binding(
    event_snapshot: EventSnapshot,
    event_source_manifest: EventSourceManifest,
) -> None:
    source_fetched = event_source_manifest.fetched_at.astimezone(UTC).replace(tzinfo=None)
    if (
        event_source_manifest.source_name != event_snapshot.source_name
        or event_source_manifest.source_version != event_snapshot.source_version
        or source_fetched.isoformat(timespec="seconds") != event_snapshot.fetched_at
        or event_snapshot.coverage_start < event_source_manifest.coverage_start
        or event_snapshot.coverage_end > event_source_manifest.coverage_end
    ):
        raise ValueError("event source manifest metadata does not match the event snapshot")


def _annual_source_review(
    frame: pl.DataFrame,
    *,
    table_name: str,
    year: int,
    year_end_cutoff: datetime,
) -> AnnualSourceReview:
    year_rows = frame.filter(pl.col("ann_date").dt.year() == year)
    visible = year_rows.filter(pl.col("available_at") <= pl.lit(year_end_cutoff))
    revisions = _revision_stats(year_rows, table_name=table_name)
    timing = _timing_stats(year_rows, table_name=table_name)
    return AnnualSourceReview(
        year=year,
        source_name=TABLE_TO_SOURCE[table_name],
        table_name=table_name,
        announcement_row_count=year_rows.height,
        announcement_symbol_count=int(year_rows["symbol"].n_unique()) if year_rows.height else 0,
        pit_visible_row_count_at_year_end=visible.height,
        pit_visible_symbol_count_at_year_end=(
            int(visible["symbol"].n_unique()) if visible.height else 0
        ),
        field_missingness=_field_missingness(year_rows, table_name=table_name),
        logical_groups=revisions["logical_groups"],
        groups_with_multiple_announcement_dates=revisions[
            "groups_with_multiple_announcement_dates"
        ],
        max_announcement_versions=revisions["max_announcement_versions"],
        timing_metric=str(timing["metric"]),
        timing_observations=int(timing["observations"]),
        timing_minimum_days=_optional_int(timing["minimum"]),
        timing_median_days=_optional_float(timing["median"]),
        timing_maximum_days=_optional_int(timing["maximum"]),
    )


def _revision_stats(frame: pl.DataFrame, *, table_name: str) -> dict[str, int]:
    if frame.is_empty():
        return {
            "logical_groups": 0,
            "groups_with_multiple_announcement_dates": 0,
            "max_announcement_versions": 0,
        }
    keys = list(REVISION_GROUP_KEYS[table_name])
    groups = frame.group_by(keys).agg(pl.col("ann_date").n_unique().alias("versions"))
    revised = groups.filter(pl.col("versions") > 1)
    maximum = groups["versions"].max()
    return {
        "logical_groups": groups.height,
        "groups_with_multiple_announcement_dates": revised.height,
        "max_announcement_versions": int(str(maximum)) if maximum is not None else 0,
    }


def _timing_stats(frame: pl.DataFrame, *, table_name: str) -> dict[str, Any]:
    other = TIMING_COMPARE_COLUMN[table_name]
    metric = (
        "days_from_announcement_to_unlock"
        if table_name == "share_unlock_events"
        else "days_from_period_end_to_announcement"
    )
    if frame.is_empty():
        return {
            "metric": metric,
            "observations": 0,
            "minimum": None,
            "median": None,
            "maximum": None,
        }
    values: list[int] = []
    for item in frame.select(["ann_date", other]).iter_rows(named=True):
        ann = item["ann_date"]
        comparison = item[other]
        if not isinstance(ann, date) or not isinstance(comparison, date):
            raise ValueError(f"{table_name} timing columns are invalid")
        delta = comparison - ann if table_name == "share_unlock_events" else ann - comparison
        values.append(delta.days)
    return {
        "metric": metric,
        "observations": len(values),
        "minimum": min(values),
        "median": float(median(values)),
        "maximum": max(values),
    }


def _field_missingness(frame: pl.DataFrame, *, table_name: str) -> list[FieldMissingness]:
    result: list[FieldMissingness] = []
    for name in EVENT_COLUMNS[table_name]:
        if name in IDENTITY_COLUMNS:
            continue
        if frame.is_empty():
            result.append(FieldMissingness(field=name, null_or_blank_rows=0))
            continue
        count = 0
        for value in frame[name].to_list():
            if value is None or (isinstance(value, str) and not value.strip()):
                count += 1
        result.append(FieldMissingness(field=name, null_or_blank_rows=count))
    return result


def _pit_probes(
    *,
    event_tables: dict[str, pl.DataFrame],
    instruments: pl.DataFrame,
    trading_days: list[date],
    config: StrategyConfig,
    window_start: date,
    window_end: date,
) -> list[PitAvailabilityProbe]:
    trading_set = set(trading_days)
    # Prefer non-unlock announcement days for same-day probes; unlock calendars are huge
    # and only need representative PIT checks plus window edges.
    announcement_days = sorted(
        {
            day
            for table_name, table in event_tables.items()
            if table_name != "share_unlock_events"
            for day in table["ann_date"].to_list()
            if isinstance(day, date) and window_start <= day <= window_end and day in trading_set
        }
    )
    unlock_days = sorted(
        {
            day
            for day in event_tables["share_unlock_events"]["ann_date"].to_list()
            if isinstance(day, date) and window_start <= day <= window_end and day in trading_set
        }
    )
    if unlock_days:
        sample_indexes = {0, len(unlock_days) // 2, len(unlock_days) - 1}
        announcement_days = sorted(
            set(announcement_days) | {unlock_days[index] for index in sample_indexes}
        )
    probe_dates: list[date] = []
    for day in announcement_days:
        probe_dates.append(day)
        following = _next_trading_day(trading_days, day)
        if following is not None:
            probe_dates.append(following)
    for edge in (trading_days[0], trading_days[-1]):
        probe_dates.append(edge)
    probe_dates = sorted(set(probe_dates))

    availability_index = {
        table_name: _availability_index(table) for table_name, table in event_tables.items()
    }
    same_day_index = {
        table_name: _same_day_hidden_index(table) for table_name, table in event_tables.items()
    }
    listing_index = _listing_index(instruments)

    probes: list[PitAvailabilityProbe] = []
    for as_of in probe_dates:
        cutoff = decision_at_utc(as_of, config.data)
        listed_count = _listed_count(listing_index, as_of=as_of)
        visible_rows: dict[str, int] = {}
        same_day_hidden: dict[str, int] = {}
        symbols_visible: dict[str, int] = {}
        symbols_missing: dict[str, int] = {}
        for table_name in EVENT_TABLE_NAMES:
            source = TABLE_TO_SOURCE[table_name]
            visible_count, visible_symbol_count = _visible_at(
                availability_index[table_name], cutoff=cutoff
            )
            visible_rows[source] = visible_count
            same_day_hidden[source] = same_day_index[table_name].get(as_of, 0)
            symbols_visible[source] = visible_symbol_count
            symbols_missing[source] = max(listed_count - visible_symbol_count, 0)
        probes.append(
            PitAvailabilityProbe(
                as_of_date=as_of,
                decision_at_utc=cutoff,
                listed_symbols=listed_count,
                visible_event_rows=visible_rows,
                same_day_announcement_rows_not_yet_visible=same_day_hidden,
                symbols_with_visible_data=symbols_visible,
                symbols_with_no_observable_data=symbols_missing,
            )
        )
    return probes


def _availability_index(
    table: pl.DataFrame,
) -> tuple[list[tuple[datetime, int]], list[tuple[datetime, int]]]:
    """Return cumulative row and first-seen-symbol checkpoints by available_at."""
    if table.is_empty():
        return [], []
    row_groups = (
        table.group_by("available_at")
        .agg(pl.len().alias("rows"))
        .sort("available_at")
    )
    row_checkpoints: list[tuple[datetime, int]] = []
    row_total = 0
    for item in row_groups.iter_rows(named=True):
        stamp = item["available_at"]
        if not isinstance(stamp, datetime):
            raise ValueError("event available_at must be datetime")
        row_total += int(str(item["rows"]))
        row_checkpoints.append((stamp, row_total))

    first_seen = table.group_by("symbol").agg(
        pl.col("available_at").min().alias("first_available")
    )
    symbol_groups = (
        first_seen.group_by("first_available")
        .agg(pl.len().alias("symbols"))
        .sort("first_available")
    )
    symbol_checkpoints: list[tuple[datetime, int]] = []
    symbol_total = 0
    for item in symbol_groups.iter_rows(named=True):
        stamp = item["first_available"]
        if not isinstance(stamp, datetime):
            raise ValueError("event available_at must be datetime")
        symbol_total += int(str(item["symbols"]))
        symbol_checkpoints.append((stamp, symbol_total))
    return row_checkpoints, symbol_checkpoints


def _visible_at(
    indexes: tuple[list[tuple[datetime, int]], list[tuple[datetime, int]]],
    *,
    cutoff: datetime,
) -> tuple[int, int]:
    rows, symbols = indexes
    return _checkpoint_value(rows, cutoff=cutoff), _checkpoint_value(symbols, cutoff=cutoff)


def _checkpoint_value(
    checkpoints: list[tuple[datetime, int]],
    *,
    cutoff: datetime,
) -> int:
    if not checkpoints:
        return 0
    lo = 0
    hi = len(checkpoints)
    while lo < hi:
        mid = (lo + hi) // 2
        if checkpoints[mid][0] <= cutoff:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return 0
    return checkpoints[lo - 1][1]


def _same_day_hidden_index(table: pl.DataFrame) -> dict[date, int]:
    """Count rows announced on a calendar day.

    Date-only available_at is always after the same-day decision_time, so every
    same-day announcement row is not yet visible at the close decision.
    """
    if table.is_empty():
        return {}
    grouped = table.group_by("ann_date").agg(pl.len().alias("rows"))
    result: dict[date, int] = {}
    for item in grouped.iter_rows(named=True):
        ann = item["ann_date"]
        if not isinstance(ann, date):
            raise ValueError("event ann_date is invalid")
        result[ann] = int(str(item["rows"]))
    return result


def _listing_index(instruments: pl.DataFrame) -> list[tuple[date, int]]:
    stocks = instruments.filter(~pl.col("is_index") & ~pl.col("is_global"))
    if stocks.is_empty():
        return []
    grouped = (
        stocks.group_by("listing_date")
        .agg(pl.len().alias("rows"))
        .sort("listing_date")
    )
    total = 0
    result: list[tuple[date, int]] = []
    for item in grouped.iter_rows(named=True):
        listing = item["listing_date"]
        if not isinstance(listing, date):
            raise ValueError("instrument listing_date is invalid")
        total += int(str(item["rows"]))
        result.append((listing, total))
    return result


def _listed_count(listing_index: list[tuple[date, int]], *, as_of: date) -> int:
    if not listing_index:
        return 0
    lo = 0
    hi = len(listing_index)
    while lo < hi:
        mid = (lo + hi) // 2
        if listing_index[mid][0] <= as_of:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return 0
    return listing_index[lo - 1][1]


def _forecast_transitions(frame: pl.DataFrame) -> dict[str, int]:
    if frame.is_empty():
        return {}
    grouped: dict[tuple[str, date], list[tuple[date, str]]] = {}
    for item in frame.select(
        ["symbol", "report_period", "ann_date", "forecast_type"]
    ).iter_rows(named=True):
        key = (str(item["symbol"]), item["report_period"])
        grouped.setdefault(key, []).append((item["ann_date"], str(item["forecast_type"])))
    transitions: Counter[str] = Counter()
    for values in grouped.values():
        ordered = sorted(values)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            transitions[f"{previous[1]} -> {current[1]}"] += 1
    return dict(sorted(transitions.items()))


def _annual_frame(annual: list[AnnualSourceReview]) -> pl.DataFrame:
    rows = [
        {
            "year": item.year,
            "source_name": item.source_name,
            "table_name": item.table_name,
            "announcement_row_count": item.announcement_row_count,
            "announcement_symbol_count": item.announcement_symbol_count,
            "pit_visible_row_count_at_year_end": item.pit_visible_row_count_at_year_end,
            "pit_visible_symbol_count_at_year_end": item.pit_visible_symbol_count_at_year_end,
            "logical_groups": item.logical_groups,
            "groups_with_multiple_announcement_dates": (
                item.groups_with_multiple_announcement_dates
            ),
            "max_announcement_versions": item.max_announcement_versions,
            "timing_metric": item.timing_metric,
            "timing_observations": item.timing_observations,
            "timing_minimum_days": item.timing_minimum_days,
            "timing_median_days": item.timing_median_days,
            "timing_maximum_days": item.timing_maximum_days,
        }
        for item in annual
    ]
    if not rows:
        return pl.DataFrame(schema={name: pl.Null for name in ANNUAL_REVIEW_COLUMNS})
    return pl.DataFrame(rows).select(ANNUAL_REVIEW_COLUMNS).sort(
        ["year", "source_name"]
    )


def _listed_symbols(instruments: pl.DataFrame, *, as_of: date) -> set[str]:
    values = instruments.filter(
        ~pl.col("is_index")
        & ~pl.col("is_global")
        & (pl.col("listing_date") <= pl.lit(as_of))
    )["symbol"].to_list()
    return {str(value) for value in values}


def _year_end_trading_day(trading_days: list[date], year: int) -> date:
    candidates = [day for day in trading_days if day.year == year]
    if not candidates:
        raise ValueError(f"review window has no trading days in {year}")
    return candidates[-1]


def _next_trading_day(trading_days: list[date], day: date) -> date | None:
    for candidate in trading_days:
        if candidate > day:
            return candidate
    return None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(str(value))


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(str(value))


def _require_nonneg_int(value: object, label: str) -> int:
    if value is None:
        raise ValueError(
            f"quality_report.json is missing {label}; "
            "refusing to treat unavailable source-level missingness as zero"
        )
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"quality_report.json has invalid {label}") from exc
    if parsed < 0:
        raise ValueError(f"quality_report.json has negative {label}")
    return parsed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_id(report: EventOverlayReviewReport) -> str:
    payload = report.model_dump(mode="json", exclude={"report_id"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
