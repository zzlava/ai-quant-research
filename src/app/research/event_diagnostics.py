from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from app.clock import decision_at_utc
from app.models.config import StrategyConfig
from app.models.events import EVENT_TABLE_NAMES, EventSnapshot, EventSourceManifest
from app.storage.snapshot_io import load_verified_snapshot

EVENT_DIAGNOSTIC_SCHEMA_VERSION: Literal["1"] = "1"
EVENT_DIAGNOSTIC_COLUMNS = (
    "symbol",
    "as_of_date",
    "decision_at_utc",
    "latest_forecast_report_period",
    "latest_forecast_ann_date",
    "latest_forecast_type",
    "latest_forecast_p_change_min",
    "latest_forecast_p_change_max",
    "latest_forecast_versions_seen",
    "latest_forecast_source_row_hash",
    "latest_express_report_period",
    "latest_express_ann_date",
    "latest_express_yoy_net_profit",
    "latest_express_yoy_sales",
    "latest_express_source_row_hash",
    "latest_holder_end_date",
    "latest_holder_ann_date",
    "latest_holder_num",
    "previous_holder_num",
    "holder_count_change_pct",
    "latest_holder_source_row_hash",
    "latest_audit_report_period",
    "latest_audit_ann_date",
    "latest_audit_result",
    "latest_audit_is_exact_standard_unqualified",
    "latest_audit_source_row_hash",
    "announced_unlock_events_next_30d",
    "announced_unlock_earliest_date_next_30d",
    "announced_unlock_shares_next_30d",
    "announced_unlock_ratio_known_events_next_30d",
    "announced_unlock_ratio_missing_events_next_30d",
    "announced_unlock_ratio_sum_next_30d",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventDiagnosticReport(_StrictModel):
    schema_version: Literal["1"] = EVENT_DIAGNOSTIC_SCHEMA_VERSION
    strategy_config_hash: str
    market_snapshot_id: str
    event_snapshot_id: str
    event_source_coverage_start: date
    event_source_coverage_end: date
    as_of_date: date
    decision_at_utc: datetime
    unlock_horizon_calendar_days: int = 30
    rows: int = Field(ge=1)
    visible_event_rows: dict[str, int]
    observed_symbol_counts: dict[str, int]
    diagnostic_file: str = "event_diagnostics.parquet"
    diagnostic_file_sha256: str | None = None
    report_id: str | None = None
    ready_for_scoring: bool = False
    ready_for_trading: bool = False
    research_boundary: str = (
        "PIT event observations only; no threshold, exclusion, score, order, or trade is authorized"
    )


def build_event_diagnostics(
    *,
    market_dir: Path,
    event_snapshot: EventSnapshot,
    event_source_manifest: EventSourceManifest,
    event_tables: dict[str, pl.DataFrame],
    config: StrategyConfig,
    as_of: date,
) -> tuple[EventDiagnosticReport, pl.DataFrame]:
    """Build descriptive event observations using only information available by the decision."""
    market = load_verified_snapshot(Path(market_dir))
    if event_snapshot.base_market_snapshot_id != market.snapshot_id:
        raise ValueError("event overlay is bound to a different market snapshot")
    source_fetched = event_source_manifest.fetched_at.astimezone(UTC).replace(tzinfo=None)
    if (
        event_source_manifest.source_name != event_snapshot.source_name
        or event_source_manifest.source_version != event_snapshot.source_version
        or source_fetched.isoformat(timespec="seconds") != event_snapshot.fetched_at
        or event_snapshot.coverage_start < event_source_manifest.coverage_start
        or event_snapshot.coverage_end > event_source_manifest.coverage_end
    ):
        raise ValueError("event source manifest metadata does not match the event snapshot")
    if market.coverage_start is None or market.coverage_end is None:
        raise ValueError("market snapshot coverage is incomplete")
    if not market.coverage_start <= as_of <= market.coverage_end:
        raise ValueError("event diagnostic date is outside the market snapshot coverage")
    if not event_source_manifest.coverage_start <= as_of <= event_source_manifest.coverage_end:
        raise ValueError("event diagnostic date is outside the declared event source coverage")
    if set(event_tables) != set(EVENT_TABLE_NAMES):
        raise ValueError("event diagnostic requires all five verified event tables")
    calendar = pl.read_parquet(Path(market_dir) / "calendar.parquet")
    if "date" not in calendar.columns or as_of not in set(calendar["date"].to_list()):
        raise ValueError("event diagnostic date is not a trading day in the market snapshot")

    cutoff = decision_at_utc(as_of, config.data)
    visible = {
        name: table.filter(pl.col("available_at") <= cutoff)
        for name, table in event_tables.items()
    }
    instruments = pl.read_parquet(Path(market_dir) / "instruments.parquet")
    required = {"symbol", "listing_date", "is_index", "is_global"}
    missing = sorted(required - set(instruments.columns))
    if missing:
        raise ValueError(f"market instruments missing event diagnostic columns: {missing}")
    frame = (
        instruments.filter(
            ~pl.col("is_index")
            & ~pl.col("is_global")
            & (pl.col("listing_date") <= pl.lit(as_of))
        )
        .select(pl.col("symbol").cast(pl.String))
        .unique()
        .sort("symbol")
        .with_columns(
            pl.lit(as_of).cast(pl.Date).alias("as_of_date"),
            pl.lit(cutoff).cast(pl.Datetime("us")).alias("decision_at_utc"),
        )
    )
    if frame.is_empty():
        raise ValueError("event diagnostic population has no listed stocks")

    frame = frame.join(_latest_forecast(visible["earnings_forecast_events"]), on="symbol", how="left")
    frame = frame.join(_latest_express(visible["earnings_express_events"]), on="symbol", how="left")
    frame = frame.join(_latest_holder_count(visible["holder_count_events"]), on="symbol", how="left")
    frame = frame.join(_latest_audit(visible["audit_opinion_events"]), on="symbol", how="left")
    frame = frame.join(
        _upcoming_unlocks(visible["share_unlock_events"], as_of=as_of),
        on="symbol",
        how="left",
    ).with_columns(
        pl.col("announced_unlock_events_next_30d").fill_null(0).cast(pl.UInt32),
        pl.col("announced_unlock_shares_next_30d").fill_null(0.0),
        pl.col("announced_unlock_ratio_known_events_next_30d")
        .fill_null(0)
        .cast(pl.UInt32),
        pl.col("announced_unlock_ratio_missing_events_next_30d")
        .fill_null(0)
        .cast(pl.UInt32),
    )
    frame = frame.select(EVENT_DIAGNOSTIC_COLUMNS)
    observed = {
        "latest_forecast": _nonnull_count(frame, "latest_forecast_ann_date"),
        "latest_express": _nonnull_count(frame, "latest_express_ann_date"),
        "latest_holder_count": _nonnull_count(frame, "latest_holder_ann_date"),
        "latest_audit": _nonnull_count(frame, "latest_audit_ann_date"),
        "forecast_with_multiple_visible_versions": frame.filter(
            pl.col("latest_forecast_versions_seen") > 1
        ).height,
        "non_exact_standard_audit_text": frame.filter(
            pl.col("latest_audit_result").is_not_null()
            & ~pl.col("latest_audit_is_exact_standard_unqualified")
        ).height,
        "announced_unlock_within_30d": frame.filter(
            pl.col("announced_unlock_events_next_30d") > 0
        ).height,
    }
    report = EventDiagnosticReport(
        strategy_config_hash=config.config_hash(),
        market_snapshot_id=market.snapshot_id,
        event_snapshot_id=event_snapshot.snapshot_id,
        event_source_coverage_start=event_source_manifest.coverage_start,
        event_source_coverage_end=event_source_manifest.coverage_end,
        as_of_date=as_of,
        decision_at_utc=cutoff,
        rows=frame.height,
        visible_event_rows={name: visible[name].height for name in EVENT_TABLE_NAMES},
        observed_symbol_counts=observed,
    )
    return report, frame


def write_event_diagnostics_atomically(
    output_dir: Path,
    report: EventDiagnosticReport,
    frame: pl.DataFrame,
    *,
    replace_existing: bool = False,
) -> EventDiagnosticReport:
    if tuple(frame.columns) != EVENT_DIAGNOSTIC_COLUMNS:
        raise ValueError("event diagnostic columns do not match the executable schema")
    destination = Path(output_dir)
    if destination.exists() and not replace_existing:
        raise ValueError("event diagnostic output already exists; pass --replace-existing explicitly")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".event-diagnostic-{uuid.uuid4().hex}"
    backup = destination.parent / f".event-diagnostic-bak-{uuid.uuid4().hex}"
    try:
        temporary.mkdir(parents=True)
        parquet_path = temporary / report.diagnostic_file
        frame.write_parquet(parquet_path)
        with_file_hash = report.model_copy(
            update={"diagnostic_file_sha256": _sha256_file(parquet_path)}
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


def load_verified_event_diagnostics(
    output_dir: Path,
) -> tuple[EventDiagnosticReport, pl.DataFrame]:
    root = Path(output_dir)
    try:
        report = EventDiagnosticReport.model_validate_json(
            (root / "report.json").read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError("event diagnostic report is missing or invalid") from exc
    if report.report_id is None or report.report_id != _report_id(report):
        raise ValueError("event diagnostic report ID does not match its content")
    if report.ready_for_scoring or report.ready_for_trading:
        raise ValueError("event diagnostic report violates research boundaries")
    path = root / report.diagnostic_file
    if not path.is_file() or report.diagnostic_file_sha256 != _sha256_file(path):
        raise ValueError("event diagnostic parquet hash does not match report")
    frame = pl.read_parquet(path)
    if tuple(frame.columns) != EVENT_DIAGNOSTIC_COLUMNS or frame.height != report.rows:
        raise ValueError("event diagnostic parquet does not match report schema or row count")
    return report, frame


def _latest_forecast(frame: pl.DataFrame) -> pl.DataFrame:
    keys = ["symbol", "report_period"]
    versions = frame.group_by(keys).agg(pl.len().alias("latest_forecast_versions_seen"))
    latest = _latest_per_key(frame, keys).join(versions, on=keys, how="left")
    latest = _latest_per_key(latest, ["symbol"], order=["report_period", "available_at"])
    return latest.select(
        "symbol",
        pl.col("report_period").alias("latest_forecast_report_period"),
        pl.col("ann_date").alias("latest_forecast_ann_date"),
        pl.col("forecast_type").alias("latest_forecast_type"),
        pl.col("p_change_min").alias("latest_forecast_p_change_min"),
        pl.col("p_change_max").alias("latest_forecast_p_change_max"),
        "latest_forecast_versions_seen",
        pl.col("source_row_hash").alias("latest_forecast_source_row_hash"),
    )


def _latest_express(frame: pl.DataFrame) -> pl.DataFrame:
    latest = _latest_per_key(frame, ["symbol", "report_period"])
    latest = _latest_per_key(latest, ["symbol"], order=["report_period", "available_at"])
    return latest.select(
        "symbol",
        pl.col("report_period").alias("latest_express_report_period"),
        pl.col("ann_date").alias("latest_express_ann_date"),
        pl.col("yoy_net_profit").alias("latest_express_yoy_net_profit"),
        pl.col("yoy_sales").alias("latest_express_yoy_sales"),
        pl.col("source_row_hash").alias("latest_express_source_row_hash"),
    )


def _latest_holder_count(frame: pl.DataFrame) -> pl.DataFrame:
    latest = _latest_per_key(frame, ["symbol", "end_date"])
    history = latest.sort(["symbol", "end_date", "available_at", "source_row_hash"]).with_columns(
        pl.col("holder_num").shift(1).over("symbol").alias("previous_holder_num")
    )
    latest = _latest_per_key(history, ["symbol"], order=["end_date", "available_at"])
    return latest.select(
        "symbol",
        pl.col("end_date").alias("latest_holder_end_date"),
        pl.col("ann_date").alias("latest_holder_ann_date"),
        pl.col("holder_num").alias("latest_holder_num"),
        "previous_holder_num",
        pl.when(pl.col("previous_holder_num") > 0)
        .then(
            (pl.col("holder_num") - pl.col("previous_holder_num"))
            / pl.col("previous_holder_num")
        )
        .otherwise(None)
        .alias("holder_count_change_pct"),
        pl.col("source_row_hash").alias("latest_holder_source_row_hash"),
    )


def _latest_audit(frame: pl.DataFrame) -> pl.DataFrame:
    latest = _latest_per_key(frame, ["symbol", "report_period"])
    latest = _latest_per_key(latest, ["symbol"], order=["report_period", "available_at"])
    return latest.select(
        "symbol",
        pl.col("report_period").alias("latest_audit_report_period"),
        pl.col("ann_date").alias("latest_audit_ann_date"),
        pl.col("audit_result").alias("latest_audit_result"),
        (pl.col("audit_result") == "标准无保留意见").alias(
            "latest_audit_is_exact_standard_unqualified"
        ),
        pl.col("source_row_hash").alias("latest_audit_source_row_hash"),
    )


def _upcoming_unlocks(frame: pl.DataFrame, *, as_of: date) -> pl.DataFrame:
    horizon = as_of + timedelta(days=30)
    upcoming = frame.filter(
        (pl.col("float_date") > pl.lit(as_of))
        & (pl.col("float_date") <= pl.lit(horizon))
    )
    grouped = upcoming.group_by("symbol").agg(
        pl.len().alias("announced_unlock_events_next_30d"),
        pl.col("float_date").min().alias("announced_unlock_earliest_date_next_30d"),
        pl.col("float_share").sum().alias("announced_unlock_shares_next_30d"),
        pl.col("float_ratio")
        .is_not_null()
        .sum()
        .alias("announced_unlock_ratio_known_events_next_30d"),
        pl.col("float_ratio")
        .is_null()
        .sum()
        .alias("announced_unlock_ratio_missing_events_next_30d"),
        pl.col("float_ratio").sum().alias("announced_unlock_ratio_sum_next_30d"),
    )
    return grouped.with_columns(
        pl.when(pl.col("announced_unlock_ratio_known_events_next_30d") > 0)
        .then(pl.col("announced_unlock_ratio_sum_next_30d"))
        .otherwise(None)
        .alias("announced_unlock_ratio_sum_next_30d")
    )


def _latest_per_key(
    frame: pl.DataFrame,
    keys: list[str],
    *,
    order: list[str] | None = None,
) -> pl.DataFrame:
    ordering = [*keys, *(order or ["available_at"]), "source_row_hash"]
    return frame.sort(ordering).group_by(keys, maintain_order=True).tail(1)


def _nonnull_count(frame: pl.DataFrame, column: str) -> int:
    return frame.filter(pl.col(column).is_not_null()).height


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_id(report: EventDiagnosticReport) -> str:
    payload = report.model_dump(mode="json", exclude={"report_id"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
