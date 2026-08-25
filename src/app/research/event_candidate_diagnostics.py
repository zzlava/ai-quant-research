from __future__ import annotations

import hashlib
import json
import math
import shutil
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from app.clock import decision_at_utc
from app.models.config import StrategyConfig
from app.models.events import EventSnapshot, EventSourceManifest
from app.providers.tushare_events import EVENT_AVAILABILITY_POLICY
from app.storage.snapshot_io import load_verified_snapshot, read_tables

EVENT_CANDIDATE_DIAGNOSTIC_SCHEMA_VERSION: Literal["1"] = "1"
EVENT_CANDIDATE_DIAGNOSTIC_VERSION: Literal["development-2022-2023-v1"] = "development-2022-2023-v1"
DEVELOPMENT_WINDOW_START = date(2022, 1, 1)
DEVELOPMENT_WINDOW_END = date(2023, 12, 31)
LABEL_HARD_END = date(2023, 12, 31)
FORWARD_HORIZONS: tuple[int, ...] = (5, 10, 20)
UNLOCK_HORIZON_CALENDAR_DAYS = 30
UNLOCK_HIGH_RATIO_THRESHOLD = 5.0

FORECAST_BULLISH_TYPES = frozenset({"预增", "略增", "扭亏", "续盈"})
FORECAST_BEARISH_TYPES = frozenset({"预减", "略减", "首亏", "续亏"})
STANDARD_UNQUALIFIED_AUDIT = "标准无保留意见"

OBSERVATION_COLUMNS = (
    "source",
    "symbol",
    "ann_date",
    "available_at",
    "first_usable_trade_date",
    "hypothesis_id",
    "threshold_bucket",
    "signal_value",
    "signal_known",
    "year",
    "source_row_hash",
    "fwd_raw_ret_5d",
    "fwd_raw_ret_10d",
    "fwd_raw_ret_20d",
    "fwd_rel_hs300_ret_5d",
    "fwd_rel_hs300_ret_10d",
    "fwd_rel_hs300_ret_20d",
    "label_known_5d",
    "label_known_10d",
    "label_known_20d",
)

SUMMARY_COLUMNS = (
    "hypothesis_id",
    "source",
    "year",
    "horizon_days",
    "signal_kind",
    "annual_stability_metric",
    "eligible",
    "known",
    "unknown",
    "labeled",
    "known_coverage",
    "labeled_coverage",
    "candidate_direction",
    # Continuous: mean/median/win over all labeled known signals.
    # Binary: these pooled fields stay null; use signal_1 / signal_0 fields below.
    "mean_raw_return",
    "median_raw_return",
    "win_rate_raw",
    "mean_rel_hs300_return",
    "median_rel_hs300_return",
    "win_rate_rel_hs300",
    # Binary only: labeled counts and means by signal class; spread = mean(1) - mean(0).
    "labeled_signal_1",
    "labeled_signal_0",
    "mean_raw_return_signal_1",
    "mean_raw_return_signal_0",
    "mean_rel_hs300_return_signal_1",
    "mean_rel_hs300_return_signal_0",
    "mean_raw_return_spread_1_minus_0",
    "mean_rel_hs300_return_spread_1_minus_0",
    "spearman_signal_vs_raw",
    "spearman_signal_vs_rel_hs300",
    # same_sign compares annual_stability_metric by year (binary=spread, continuous=spearman).
    "same_sign_2022_2023_raw",
    "same_sign_2022_2023_rel_hs300",
    # True only when both years' stability metrics agree with declared candidate_direction.
    "candidate_direction_supported_2022_2023_raw",
    "candidate_direction_supported_2022_2023_rel_hs300",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateHypothesisSpec(_StrictModel):
    hypothesis_id: str = Field(min_length=1)
    source: Literal["forecast", "express", "stk_holdernumber", "share_float", "fina_audit"]
    signal_kind: Literal["binary_bucket", "continuous"]
    threshold_bucket: str = Field(min_length=1)
    candidate_direction: Literal["positive", "negative"]
    economic_meaning: str = Field(min_length=1)



CANDIDATE_HYPOTHESES: tuple[CandidateHypothesisSpec, ...] = (
    CandidateHypothesisSpec(
        hypothesis_id="forecast_bullish_type",
        source="forecast",
        signal_kind="binary_bucket",
        threshold_bucket="bullish_type",
        candidate_direction="positive",
        economic_meaning="业绩预告类型为预增/略增/扭亏/续盈，预期正向收益反应",
    ),
    CandidateHypothesisSpec(
        hypothesis_id="forecast_bearish_type",
        source="forecast",
        signal_kind="binary_bucket",
        threshold_bucket="bearish_type",
        candidate_direction="negative",
        economic_meaning="业绩预告类型为预减/略减/首亏/续亏，预期负向收益反应",
    ),
    CandidateHypothesisSpec(
        hypothesis_id="forecast_p_change_midpoint",
        source="forecast",
        signal_kind="continuous",
        threshold_bucket="p_change_midpoint",
        candidate_direction="positive",
        economic_meaning="业绩预告净利润变动中点越高，后续收益可能越好",
    ),
    CandidateHypothesisSpec(
        hypothesis_id="forecast_upward_revision",
        source="forecast",
        signal_kind="binary_bucket",
        threshold_bucket="upward_revision",
        candidate_direction="positive",
        economic_meaning="同一报告期后续公告上调变动中点，预期正向反应",
    ),
    CandidateHypothesisSpec(
        hypothesis_id="express_yoy_net_profit_positive",
        source="express",
        signal_kind="binary_bucket",
        threshold_bucket="yoy_net_profit_positive",
        candidate_direction="positive",
        economic_meaning="业绩快报净利润同比为正，预期正向反应",
    ),
    CandidateHypothesisSpec(
        hypothesis_id="express_yoy_net_profit",
        source="express",
        signal_kind="continuous",
        threshold_bucket="yoy_net_profit",
        candidate_direction="positive",
        economic_meaning="业绩快报净利润同比越高，后续收益可能越好",
    ),
    CandidateHypothesisSpec(
        hypothesis_id="holder_count_decrease",
        source="stk_holdernumber",
        signal_kind="binary_bucket",
        threshold_bucket="holder_decrease",
        candidate_direction="positive",
        economic_meaning="股东户数下降可能意味着筹码集中，预期正向反应",
    ),
    CandidateHypothesisSpec(
        hypothesis_id="holder_count_change_pct",
        source="stk_holdernumber",
        signal_kind="continuous",
        threshold_bucket="holder_change_pct",
        candidate_direction="negative",
        economic_meaning="股东户数增幅越高筹码越分散，预期负向反应",
    ),
    CandidateHypothesisSpec(
        hypothesis_id="unlock_announced_pressure_next_30d",
        source="share_float",
        signal_kind="continuous",
        threshold_bucket="announced_unlock_ratio_sum_30d",
        candidate_direction="negative",
        economic_meaning="已公告且即将解禁的比例合计越高，供给压力越大",
    ),
    CandidateHypothesisSpec(
        hypothesis_id="unlock_announced_pressure_high",
        source="share_float",
        signal_kind="binary_bucket",
        threshold_bucket=f"unlock_ratio_sum_ge_{UNLOCK_HIGH_RATIO_THRESHOLD:g}",
        candidate_direction="negative",
        economic_meaning=(
            f"已公告未来{UNLOCK_HORIZON_CALENDAR_DAYS}自然日解禁比例合计"
            f">={UNLOCK_HIGH_RATIO_THRESHOLD:g}，预期负向反应"
        ),
    ),
    CandidateHypothesisSpec(
        hypothesis_id="audit_non_standard_opinion",
        source="fina_audit",
        signal_kind="binary_bucket",
        threshold_bucket="non_standard_opinion",
        candidate_direction="negative",
        economic_meaning="审计意见非精确标准无保留意见，预期负向反应",
    ),
)
PREDECLARED_HYPOTHESIS_IDS = frozenset(item.hypothesis_id for item in CANDIDATE_HYPOTHESES)


class EventCandidateDiagnosticReport(_StrictModel):
    schema_version: Literal["1"] = EVENT_CANDIDATE_DIAGNOSTIC_SCHEMA_VERSION
    diagnostic_version: Literal["development-2022-2023-v1"] = EVENT_CANDIDATE_DIAGNOSTIC_VERSION
    strategy_config_hash: str
    market_snapshot_id: str
    event_snapshot_id: str
    event_source_coverage_start: date
    event_source_coverage_end: date
    window_start: date
    window_end: date
    label_hard_end: date = LABEL_HARD_END
    forward_horizons: list[int]
    benchmark_symbol: str
    availability_policy: str = EVENT_AVAILABILITY_POLICY
    unlock_horizon_calendar_days: int = UNLOCK_HORIZON_CALENDAR_DAYS
    unlock_high_ratio_threshold: float = UNLOCK_HIGH_RATIO_THRESHOLD
    hypotheses: list[CandidateHypothesisSpec]
    observation_rows: int = Field(ge=0)
    summary_rows: int = Field(ge=0)
    observation_file: str = "observations.parquet"
    observation_file_sha256: str | None = None
    summary_file: str = "hypothesis_annual_summary.parquet"
    summary_file_sha256: str | None = None
    report_id: str | None = None
    ready_for_scoring: bool = False
    ready_for_trading: bool = False
    development_only: bool = True
    research_boundary: str = (
        "Development-window event-candidate evidence only; no score, IC, exclusion, "
        "portfolio, order, trade, or alpha claim is authorized. 2024 is already observed "
        "and must not be used for selection."
    )


def build_event_candidate_diagnostics(
    *,
    market_dir: Path,
    event_snapshot: EventSnapshot,
    event_source_manifest: EventSourceManifest,
    event_tables: dict[str, pl.DataFrame],
    config: StrategyConfig,
    window_start: date,
    window_end: date,
) -> tuple[EventCandidateDiagnosticReport, pl.DataFrame, pl.DataFrame]:
    """Build event-level candidate diagnostics for the sealed development window."""
    _assert_development_window(window_start, window_end)
    market = load_verified_snapshot(Path(market_dir))
    if event_snapshot.base_market_snapshot_id != market.snapshot_id:
        raise ValueError("event overlay is bound to a different market snapshot")
    _assert_source_binding(event_snapshot, event_source_manifest)

    tables = read_tables(Path(market_dir))
    calendar = (
        tables["calendar"]
        .select(pl.col("date").cast(pl.Date))
        .unique()
        .sort("date")["date"]
        .to_list()
    )
    trading_days = [day for day in calendar if isinstance(day, date)]
    if not trading_days:
        raise ValueError("market calendar is empty")
    day_index = {day: idx for idx, day in enumerate(trading_days)}

    prices, benchmark_prices = _load_label_prices(
        tables,
        benchmark_symbol=config.data.market_index,
        label_hard_end=LABEL_HARD_END,
    )

    observations = _build_observations(
        event_tables=event_tables,
        window_start=window_start,
        window_end=window_end,
        trading_days=trading_days,
        day_index=day_index,
        prices=prices,
        benchmark_prices=benchmark_prices,
        config=config,
        label_hard_end=LABEL_HARD_END,
    )
    summary = _build_summary(observations)

    report = EventCandidateDiagnosticReport(
        strategy_config_hash=config.config_hash(),
        market_snapshot_id=market.snapshot_id,
        event_snapshot_id=event_snapshot.snapshot_id,
        event_source_coverage_start=event_source_manifest.coverage_start,
        event_source_coverage_end=event_source_manifest.coverage_end,
        window_start=window_start,
        window_end=window_end,
        forward_horizons=list(FORWARD_HORIZONS),
        benchmark_symbol=config.data.market_index,
        hypotheses=list(CANDIDATE_HYPOTHESES),
        observation_rows=observations.height,
        summary_rows=summary.height,
    )
    return report, observations, summary


def write_event_candidate_diagnostics_atomically(
    output_dir: Path,
    report: EventCandidateDiagnosticReport,
    observations: pl.DataFrame,
    summary: pl.DataFrame,
    *,
    replace_existing: bool = False,
) -> EventCandidateDiagnosticReport:
    if tuple(observations.columns) != OBSERVATION_COLUMNS:
        raise ValueError("event candidate observation columns do not match the schema")
    if tuple(summary.columns) != SUMMARY_COLUMNS:
        raise ValueError("event candidate summary columns do not match the schema")
    if observations.height != report.observation_rows:
        raise ValueError("observation row count does not match report")
    if summary.height != report.summary_rows:
        raise ValueError("summary row count does not match report")
    if report.ready_for_scoring or report.ready_for_trading or not report.development_only:
        raise ValueError("event candidate diagnostic report violates research boundaries")

    destination = Path(output_dir)
    if destination.exists() and not replace_existing:
        raise ValueError(
            "event candidate diagnostic output already exists; pass --replace-existing explicitly"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".event-candidate-diagnostic-{uuid.uuid4().hex}"
    backup = destination.parent / f".event-candidate-diagnostic-bak-{uuid.uuid4().hex}"
    try:
        temporary.mkdir(parents=True)
        observation_path = temporary / report.observation_file
        summary_path = temporary / report.summary_file
        observations.write_parquet(observation_path)
        summary.write_parquet(summary_path)
        with_hashes = report.model_copy(
            update={
                "observation_file_sha256": _sha256_file(observation_path),
                "summary_file_sha256": _sha256_file(summary_path),
            }
        )
        sealed = with_hashes.model_copy(update={"report_id": _report_id(with_hashes)})
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


def load_verified_event_candidate_diagnostics(
    output_dir: Path,
) -> tuple[EventCandidateDiagnosticReport, pl.DataFrame, pl.DataFrame]:
    root = Path(output_dir)
    try:
        report = EventCandidateDiagnosticReport.model_validate_json(
            (root / "report.json").read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError("event candidate diagnostic report is missing or invalid") from exc
    if report.report_id is None or report.report_id != _report_id(report):
        raise ValueError("event candidate diagnostic report ID does not match its content")
    if report.ready_for_scoring or report.ready_for_trading or not report.development_only:
        raise ValueError("event candidate diagnostic report violates research boundaries")
    if report.window_start != DEVELOPMENT_WINDOW_START or report.window_end != DEVELOPMENT_WINDOW_END:
        raise ValueError("event candidate diagnostic window is outside the sealed development window")
    if report.label_hard_end != LABEL_HARD_END:
        raise ValueError("event candidate diagnostic label_hard_end is invalid")

    observation_path = root / report.observation_file
    summary_path = root / report.summary_file
    if not observation_path.is_file() or report.observation_file_sha256 != _sha256_file(
        observation_path
    ):
        raise ValueError("event candidate observation parquet hash does not match report")
    if not summary_path.is_file() or report.summary_file_sha256 != _sha256_file(summary_path):
        raise ValueError("event candidate summary parquet hash does not match report")

    observations = pl.read_parquet(observation_path)
    summary = pl.read_parquet(summary_path)
    if tuple(observations.columns) != OBSERVATION_COLUMNS:
        raise ValueError("event candidate observation parquet schema mismatch")
    if tuple(summary.columns) != SUMMARY_COLUMNS:
        raise ValueError("event candidate summary parquet schema mismatch")
    if observations.height != report.observation_rows or summary.height != report.summary_rows:
        raise ValueError("event candidate parquet row counts do not match report")
    return report, observations, summary


def _assert_development_window(window_start: date, window_end: date) -> None:
    if window_end < window_start:
        raise ValueError("window_end precedes window_start")
    if window_start != DEVELOPMENT_WINDOW_START or window_end != DEVELOPMENT_WINDOW_END:
        raise ValueError(
            "event candidate diagnostics only allow the sealed development window "
            f"{DEVELOPMENT_WINDOW_START.isoformat()}..{DEVELOPMENT_WINDOW_END.isoformat()}"
        )


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


def _load_label_prices(
    tables: dict[str, pl.DataFrame],
    *,
    benchmark_symbol: str,
    label_hard_end: date,
) -> tuple[dict[tuple[str, date], float], dict[date, float]]:
    daily = tables["daily_bars"].filter(pl.col("date") <= pl.lit(label_hard_end))
    if "adj_close" not in daily.columns:
        raise ValueError("daily_bars is missing adj_close; cannot build return labels")
    price_rows = (
        daily.select(["symbol", "date", "adj_close"])
        .drop_nulls()
        .filter(pl.col("adj_close") > 0)
        .to_dicts()
    )
    prices: dict[tuple[str, date], float] = {}
    for row in price_rows:
        day = row["date"]
        if isinstance(day, date):
            prices[(str(row["symbol"]), day)] = float(row["adj_close"])

    index = tables["index_bars"].filter(
        (pl.col("symbol") == benchmark_symbol) & (pl.col("date") <= pl.lit(label_hard_end))
    )
    close_col = "adj_close" if "adj_close" in index.columns else "close"
    bench_rows = (
        index.select(["date", close_col])
        .drop_nulls()
        .filter(pl.col(close_col) > 0)
        .to_dicts()
    )
    benchmark_prices: dict[date, float] = {}
    for row in bench_rows:
        day = row["date"]
        if isinstance(day, date):
            benchmark_prices[day] = float(row[close_col])
    if not benchmark_prices:
        raise ValueError(f"benchmark prices for {benchmark_symbol} are unavailable within label_hard_end")
    return prices, benchmark_prices


def _build_observations(
    *,
    event_tables: dict[str, pl.DataFrame],
    window_start: date,
    window_end: date,
    trading_days: list[date],
    day_index: dict[date, int],
    prices: dict[tuple[str, date], float],
    benchmark_prices: dict[date, float],
    config: StrategyConfig,
    label_hard_end: date = LABEL_HARD_END,
    entry_end: date | None = None,
    allowed_hypothesis_ids: frozenset[str] | None = None,
) -> pl.DataFrame:
    if allowed_hypothesis_ids is not None:
        unknown = allowed_hypothesis_ids - PREDECLARED_HYPOTHESIS_IDS
        if unknown:
            raise ValueError(
                "allowed hypothesis IDs are not predeclared: "
                f"{sorted(unknown)}"
            )
    usable_end = entry_end if entry_end is not None else window_end
    frames = [
        _forecast_observations(
            event_tables["earnings_forecast_events"],
            window_start=window_start,
            window_end=window_end,
            trading_days=trading_days,
            entry_end=usable_end,
        ),
        _express_observations(
            event_tables["earnings_express_events"],
            window_start=window_start,
            window_end=window_end,
            trading_days=trading_days,
            entry_end=usable_end,
        ),
        _holder_observations(
            event_tables["holder_count_events"],
            window_start=window_start,
            window_end=window_end,
            trading_days=trading_days,
            config=config,
            entry_end=usable_end,
        ),
        _unlock_observations(
            event_tables["share_unlock_events"],
            window_start=window_start,
            window_end=window_end,
            trading_days=trading_days,
            entry_end=usable_end,
        ),
        _audit_observations(
            event_tables["audit_opinion_events"],
            window_start=window_start,
            window_end=window_end,
            trading_days=trading_days,
            entry_end=usable_end,
        ),
    ]
    combined = pl.concat([frame for frame in frames if frame.height > 0], how="vertical_relaxed")
    if combined.is_empty():
        return pl.DataFrame(schema={name: _observation_dtype(name) for name in OBSERVATION_COLUMNS})

    if allowed_hypothesis_ids is not None:
        combined = combined.filter(pl.col("hypothesis_id").is_in(list(allowed_hypothesis_ids)))
        if combined.is_empty():
            return pl.DataFrame(schema={name: _observation_dtype(name) for name in OBSERVATION_COLUMNS})

    labeled_rows: list[dict[str, Any]] = []
    for row in combined.sort(
        ["hypothesis_id", "symbol", "ann_date", "source_row_hash"]
    ).iter_rows(named=True):
        labeled_rows.append(
            _attach_labels(
                row,
                day_index=day_index,
                trading_days=trading_days,
                prices=prices,
                benchmark_prices=benchmark_prices,
                label_hard_end=label_hard_end,
            )
        )
    return pl.DataFrame(labeled_rows, schema={name: _observation_dtype(name) for name in OBSERVATION_COLUMNS}).select(
        list(OBSERVATION_COLUMNS)
    )


def _forecast_observations(
    frame: pl.DataFrame,
    *,
    window_start: date,
    window_end: date,
    trading_days: list[date],
    entry_end: date,
) -> pl.DataFrame:
    if frame.is_empty():
        return _empty_observations()

    rows: list[dict[str, Any]] = []
    ordered = frame.sort(["symbol", "report_period", "ann_date", "source_row_hash"])
    history_mids: dict[tuple[str, date], list[float | None]] = {}
    for item in ordered.iter_rows(named=True):
        ann_date = item["ann_date"]
        report_period = item["report_period"]
        if not isinstance(ann_date, date) or not isinstance(report_period, date):
            continue
        mid = _forecast_midpoint(item.get("p_change_min"), item.get("p_change_max"))
        key = (str(item["symbol"]), report_period)
        prior_mids = history_mids.get(key, [])
        in_window = window_start <= ann_date <= window_end
        first_usable = _first_usable_trade_date(ann_date, trading_days) if in_window else None
        if in_window and first_usable is not None and first_usable <= entry_end:
            forecast_type = item.get("forecast_type")
            type_known = isinstance(forecast_type, str) and bool(forecast_type.strip())
            base = _base_observation(
                source="forecast",
                item=item,
                first_usable=first_usable,
            )
            if type_known:
                bullish = 1.0 if forecast_type in FORECAST_BULLISH_TYPES else 0.0
                bearish = 1.0 if forecast_type in FORECAST_BEARISH_TYPES else 0.0
                rows.append(
                    {
                        **base,
                        "hypothesis_id": "forecast_bullish_type",
                        "threshold_bucket": "bullish_type",
                        "signal_value": bullish,
                        "signal_known": True,
                    }
                )
                rows.append(
                    {
                        **base,
                        "hypothesis_id": "forecast_bearish_type",
                        "threshold_bucket": "bearish_type",
                        "signal_value": bearish,
                        "signal_known": True,
                    }
                )
            else:
                rows.append(
                    {
                        **base,
                        "hypothesis_id": "forecast_bullish_type",
                        "threshold_bucket": "bullish_type",
                        "signal_value": None,
                        "signal_known": False,
                    }
                )
                rows.append(
                    {
                        **base,
                        "hypothesis_id": "forecast_bearish_type",
                        "threshold_bucket": "bearish_type",
                        "signal_value": None,
                        "signal_known": False,
                    }
                )
            rows.append(
                {
                    **base,
                    "hypothesis_id": "forecast_p_change_midpoint",
                    "threshold_bucket": "p_change_midpoint",
                    "signal_value": mid,
                    "signal_known": mid is not None,
                }
            )
            if prior_mids:
                prev_mid = prior_mids[-1]
                if prev_mid is not None and mid is not None:
                    revision_signal: float | None = 1.0 if mid > prev_mid else 0.0
                    revision_known = True
                else:
                    revision_signal = None
                    revision_known = False
                rows.append(
                    {
                        **base,
                        "hypothesis_id": "forecast_upward_revision",
                        "threshold_bucket": "upward_revision",
                        "signal_value": revision_signal,
                        "signal_known": revision_known,
                    }
                )
        history_mids.setdefault(key, []).append(mid)
    return pl.DataFrame(rows) if rows else _empty_observations()


def _express_observations(
    frame: pl.DataFrame,
    *,
    window_start: date,
    window_end: date,
    trading_days: list[date],
    entry_end: date,
) -> pl.DataFrame:
    windowed = _window_events(frame, window_start=window_start, window_end=window_end)
    if windowed.is_empty():
        return _empty_observations()
    rows: list[dict[str, Any]] = []
    for item in windowed.sort(["symbol", "ann_date", "source_row_hash"]).iter_rows(named=True):
        first_usable = _first_usable_trade_date(item["ann_date"], trading_days)
        if first_usable is None or first_usable > entry_end:
            continue
        yoy = item.get("yoy_net_profit")
        yoy_value: float | None
        if isinstance(yoy, int | float) and math.isfinite(float(yoy)):
            yoy_value = float(yoy)
            known = True
        else:
            yoy_value = None
            known = False
        base = _base_observation(source="express", item=item, first_usable=first_usable)
        if known and yoy_value is not None:
            positive_signal: float | None = 1.0 if yoy_value > 0 else 0.0
            positive_known = True
        else:
            positive_signal = None
            positive_known = False
        rows.append(
            {
                **base,
                "hypothesis_id": "express_yoy_net_profit_positive",
                "threshold_bucket": "yoy_net_profit_positive",
                "signal_value": positive_signal,
                "signal_known": positive_known,
            }
        )
        rows.append(
            {
                **base,
                "hypothesis_id": "express_yoy_net_profit",
                "threshold_bucket": "yoy_net_profit",
                "signal_value": yoy_value,
                "signal_known": known,
            }
        )
    return pl.DataFrame(rows) if rows else _empty_observations()


def _holder_observations(
    frame: pl.DataFrame,
    *,
    window_start: date,
    window_end: date,
    trading_days: list[date],
    config: StrategyConfig,
    entry_end: date,
) -> pl.DataFrame:
    history_frame = frame.sort(["symbol", "end_date", "available_at", "source_row_hash"])
    if history_frame.is_empty():
        return _empty_observations()

    history_by_symbol: dict[str, list[tuple[date, datetime, float]]] = {}
    rows: list[dict[str, Any]] = []
    for item in history_frame.iter_rows(named=True):
        symbol = str(item["symbol"])
        ann_date = item["ann_date"]
        end_date = item["end_date"]
        available_at = item["available_at"]
        holder_num = item.get("holder_num")
        if not isinstance(ann_date, date) or not isinstance(end_date, date):
            continue
        if not isinstance(available_at, datetime):
            continue
        if not isinstance(holder_num, int | float) or not math.isfinite(float(holder_num)):
            continue
        holder_value = float(holder_num)
        in_window = window_start <= ann_date <= window_end
        first_usable = _first_usable_trade_date(ann_date, trading_days) if in_window else None
        if in_window and first_usable is not None and first_usable <= entry_end:
            base = _base_observation(source="stk_holdernumber", item=item, first_usable=first_usable)
            cutoff = decision_at_utc(first_usable, config.data)
            prior = _select_visible_holder_prior(
                history_by_symbol.get(symbol, []),
                current_end_date=end_date,
                cutoff=cutoff,
            )
            change: float | None = None
            known = False
            if prior is not None and prior[2] > 0:
                change = (holder_value - prior[2]) / prior[2]
                known = True
            if known and change is not None:
                decrease_signal: float | None = 1.0 if change < 0 else 0.0
                decrease_known = True
            else:
                decrease_signal = None
                decrease_known = False
            rows.append(
                {
                    **base,
                    "hypothesis_id": "holder_count_decrease",
                    "threshold_bucket": "holder_decrease",
                    "signal_value": decrease_signal,
                    "signal_known": decrease_known,
                }
            )
            rows.append(
                {
                    **base,
                    "hypothesis_id": "holder_count_change_pct",
                    "threshold_bucket": "holder_change_pct",
                    "signal_value": change,
                    "signal_known": known,
                }
            )
        history_by_symbol.setdefault(symbol, []).append((end_date, available_at, holder_value))
    return pl.DataFrame(rows) if rows else _empty_observations()


def _select_visible_holder_prior(
    history: list[tuple[date, datetime, float]],
    *,
    current_end_date: date,
    cutoff: datetime,
) -> tuple[date, datetime, float] | None:
    """Pick PIT-visible prior: max end_date < current, then max available_at <= cutoff."""
    visible = [
        item
        for item in history
        if item[0] < current_end_date and item[1] <= cutoff and item[2] > 0
    ]
    if not visible:
        return None
    return max(visible, key=lambda item: (item[0], item[1]))


def _unlock_observations(
    frame: pl.DataFrame,
    *,
    window_start: date,
    window_end: date,
    trading_days: list[date],
    entry_end: date,
) -> pl.DataFrame:
    windowed = _window_events(frame, window_start=window_start, window_end=window_end)
    if windowed.is_empty():
        return _empty_observations()

    # Explicit event-day aggregation: one observation unit per (symbol, ann_date).
    grouped = (
        windowed.group_by(["symbol", "ann_date"])
        .agg(
            pl.col("available_at").min().alias("available_at"),
            pl.col("source_row_hash"),
            pl.col("float_date"),
            pl.col("float_ratio"),
        )
        .sort(["symbol", "ann_date"])
    )
    rows: list[dict[str, Any]] = []
    for item in grouped.iter_rows(named=True):
        ann_date = item["ann_date"]
        first_usable = _first_usable_trade_date(ann_date, trading_days)
        if first_usable is None or first_usable > entry_end:
            continue
        horizon_end = first_usable + timedelta(days=UNLOCK_HORIZON_CALENDAR_DAYS)
        float_dates = item["float_date"]
        float_ratios = item["float_ratio"]
        source_hashes = item["source_row_hash"]
        known_sum = 0.0
        missing_ratio = False
        contributing_hashes: list[str] = []
        eligible_tranches = 0
        for float_date, float_ratio, row_hash in zip(
            float_dates, float_ratios, source_hashes, strict=True
        ):
            if not isinstance(float_date, date):
                continue
            if float_date <= first_usable or float_date > horizon_end:
                continue
            eligible_tranches += 1
            contributing_hashes.append(str(row_hash))
            if isinstance(float_ratio, int | float) and math.isfinite(float(float_ratio)):
                known_sum += float(float_ratio)
            else:
                missing_ratio = True
        if eligible_tranches == 0:
            continue
        known = not missing_ratio
        signal = known_sum if known else None
        base = {
            "source": "share_float",
            "symbol": str(item["symbol"]),
            "ann_date": ann_date,
            "available_at": item["available_at"],
            "first_usable_trade_date": first_usable,
            "year": ann_date.year,
            "source_row_hash": _aggregate_source_row_hash(contributing_hashes),
        }
        rows.append(
            {
                **base,
                "hypothesis_id": "unlock_announced_pressure_next_30d",
                "threshold_bucket": "announced_unlock_ratio_sum_30d",
                "signal_value": signal,
                "signal_known": known,
            }
        )
        if known and signal is not None:
            high_signal: float | None = 1.0 if signal >= UNLOCK_HIGH_RATIO_THRESHOLD else 0.0
            high_known = True
        else:
            high_signal = None
            high_known = False
        rows.append(
            {
                **base,
                "hypothesis_id": "unlock_announced_pressure_high",
                "threshold_bucket": f"unlock_ratio_sum_ge_{UNLOCK_HIGH_RATIO_THRESHOLD:g}",
                "signal_value": high_signal,
                "signal_known": high_known,
            }
        )
    return pl.DataFrame(rows) if rows else _empty_observations()


def _aggregate_source_row_hash(hashes: list[str]) -> str:
    payload = "\n".join(sorted(hashes))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit_observations(
    frame: pl.DataFrame,
    *,
    window_start: date,
    window_end: date,
    trading_days: list[date],
    entry_end: date,
) -> pl.DataFrame:
    windowed = _window_events(frame, window_start=window_start, window_end=window_end)
    if windowed.is_empty():
        return _empty_observations()
    rows: list[dict[str, Any]] = []
    for item in windowed.sort(["symbol", "ann_date", "source_row_hash"]).iter_rows(named=True):
        first_usable = _first_usable_trade_date(item["ann_date"], trading_days)
        if first_usable is None or first_usable > entry_end:
            continue
        result = item.get("audit_result")
        known = isinstance(result, str) and bool(result.strip())
        base = _base_observation(source="fina_audit", item=item, first_usable=first_usable)
        if known:
            signal: float | None = 0.0 if result == STANDARD_UNQUALIFIED_AUDIT else 1.0
        else:
            signal = None
        rows.append(
            {
                **base,
                "hypothesis_id": "audit_non_standard_opinion",
                "threshold_bucket": "non_standard_opinion",
                "signal_value": signal,
                "signal_known": known,
            }
        )
    return pl.DataFrame(rows) if rows else _empty_observations()


def _window_events(frame: pl.DataFrame, *, window_start: date, window_end: date) -> pl.DataFrame:
    return frame.filter(
        (pl.col("ann_date") >= pl.lit(window_start)) & (pl.col("ann_date") <= pl.lit(window_end))
    )


def _base_observation(*, source: str, item: dict[str, Any], first_usable: date) -> dict[str, Any]:
    ann_date = item["ann_date"]
    if not isinstance(ann_date, date):
        raise ValueError("ann_date must be a date")
    return {
        "source": source,
        "symbol": str(item["symbol"]),
        "ann_date": ann_date,
        "available_at": item["available_at"],
        "first_usable_trade_date": first_usable,
        "year": ann_date.year,
        "source_row_hash": str(item["source_row_hash"]),
    }


def _first_usable_trade_date(ann_date: date, trading_days: list[date]) -> date | None:
    if not isinstance(ann_date, date):
        return None
    for day in trading_days:
        if day > ann_date:
            return day
    return None


def _forecast_midpoint(low: object, high: object) -> float | None:
    if not isinstance(low, int | float) or not isinstance(high, int | float):
        return None
    if not math.isfinite(float(low)) or not math.isfinite(float(high)):
        return None
    return (float(low) + float(high)) / 2.0


def _attach_labels(
    row: dict[str, Any],
    *,
    day_index: dict[date, int],
    trading_days: list[date],
    prices: dict[tuple[str, date], float],
    benchmark_prices: dict[date, float],
    label_hard_end: date = LABEL_HARD_END,
) -> dict[str, Any]:
    entry = row["first_usable_trade_date"]
    symbol = str(row["symbol"])
    out = dict(row)
    for horizon in FORWARD_HORIZONS:
        raw_key = f"fwd_raw_ret_{horizon}d"
        rel_key = f"fwd_rel_hs300_ret_{horizon}d"
        known_key = f"label_known_{horizon}d"
        raw: float | None = None
        rel: float | None = None
        known = False
        entry_idx = day_index.get(entry)
        if entry_idx is not None:
            exit_idx = entry_idx + horizon
            if exit_idx < len(trading_days):
                exit_day = trading_days[exit_idx]
                if exit_day <= label_hard_end:
                    entry_px = prices.get((symbol, entry))
                    exit_px = prices.get((symbol, exit_day))
                    bench_entry = benchmark_prices.get(entry)
                    bench_exit = benchmark_prices.get(exit_day)
                    if (
                        entry_px is not None
                        and exit_px is not None
                        and entry_px > 0
                        and exit_px > 0
                        and bench_entry is not None
                        and bench_exit is not None
                        and bench_entry > 0
                        and bench_exit > 0
                    ):
                        raw = exit_px / entry_px - 1.0
                        rel = raw - (bench_exit / bench_entry - 1.0)
                        known = True
        out[raw_key] = raw
        out[rel_key] = rel
        out[known_key] = known
    return out


def _build_summary(observations: pl.DataFrame) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    by_id = {item.hypothesis_id: item for item in CANDIDATE_HYPOTHESES}
    for spec in CANDIDATE_HYPOTHESES:
        subset = (
            observations.filter(pl.col("hypothesis_id") == spec.hypothesis_id)
            if observations.height
            else observations
        )
        stability_metric = _annual_stability_metric_name(spec.signal_kind)
        # horizon -> year -> direction metric used for same-sign / support checks
        annual_metric_raw: dict[int, dict[int, float | None]] = {
            horizon: {} for horizon in FORWARD_HORIZONS
        }
        annual_metric_rel: dict[int, dict[int, float | None]] = {
            horizon: {} for horizon in FORWARD_HORIZONS
        }
        for year in (2022, 2023, None):
            year_frame = subset if year is None else subset.filter(pl.col("year") == year)
            year_label = "all" if year is None else str(year)
            for horizon in FORWARD_HORIZONS:
                stats = _hypothesis_stats(
                    year_frame,
                    horizon=horizon,
                    candidate_direction=spec.candidate_direction,
                    signal_kind=spec.signal_kind,
                )
                if year in (2022, 2023):
                    annual_metric_raw[horizon][year] = _stability_metric_value(
                        stats, signal_kind=spec.signal_kind, side="raw"
                    )
                    annual_metric_rel[horizon][year] = _stability_metric_value(
                        stats, signal_kind=spec.signal_kind, side="rel"
                    )
                rows.append(
                    {
                        "hypothesis_id": spec.hypothesis_id,
                        "source": spec.source,
                        "year": year_label,
                        "horizon_days": horizon,
                        "annual_stability_metric": stability_metric,
                        **stats,
                        "same_sign_2022_2023_raw": None,
                        "same_sign_2022_2023_rel_hs300": None,
                        "candidate_direction_supported_2022_2023_raw": None,
                        "candidate_direction_supported_2022_2023_rel_hs300": None,
                    }
                )
        for row in rows:
            if row["hypothesis_id"] != spec.hypothesis_id:
                continue
            horizon = int(row["horizon_days"])
            metric_2022_raw = annual_metric_raw[horizon].get(2022)
            metric_2023_raw = annual_metric_raw[horizon].get(2023)
            metric_2022_rel = annual_metric_rel[horizon].get(2022)
            metric_2023_rel = annual_metric_rel[horizon].get(2023)
            row["same_sign_2022_2023_raw"] = _same_sign(metric_2022_raw, metric_2023_raw)
            row["same_sign_2022_2023_rel_hs300"] = _same_sign(metric_2022_rel, metric_2023_rel)
            row["candidate_direction_supported_2022_2023_raw"] = (
                _candidate_direction_supported(
                    metric_2022_raw,
                    metric_2023_raw,
                    candidate_direction=spec.candidate_direction,
                )
            )
            row["candidate_direction_supported_2022_2023_rel_hs300"] = (
                _candidate_direction_supported(
                    metric_2022_rel,
                    metric_2023_rel,
                    candidate_direction=spec.candidate_direction,
                )
            )

    present = {row["hypothesis_id"] for row in rows}
    for hypothesis_id, spec in by_id.items():
        if hypothesis_id in present:
            continue
        for year_label in ("2022", "2023", "all"):
            for horizon in FORWARD_HORIZONS:
                rows.append(
                    {
                        "hypothesis_id": hypothesis_id,
                        "source": spec.source,
                        "year": year_label,
                        "horizon_days": horizon,
                        "annual_stability_metric": _annual_stability_metric_name(
                            spec.signal_kind
                        ),
                        **_empty_hypothesis_stats(
                            candidate_direction=spec.candidate_direction,
                            signal_kind=spec.signal_kind,
                        ),
                        "same_sign_2022_2023_raw": None,
                        "same_sign_2022_2023_rel_hs300": None,
                        "candidate_direction_supported_2022_2023_raw": None,
                        "candidate_direction_supported_2022_2023_rel_hs300": None,
                    }
                )
    frame = pl.DataFrame(rows).sort(["hypothesis_id", "year", "horizon_days"])
    return frame.select(list(SUMMARY_COLUMNS))


def _annual_stability_metric_name(signal_kind: str) -> str:
    if signal_kind == "binary_bucket":
        return "mean_spread_1_minus_0"
    return "spearman"


def _stability_metric_value(
    stats: dict[str, Any],
    *,
    signal_kind: str,
    side: Literal["raw", "rel"],
) -> float | None:
    if signal_kind == "binary_bucket":
        key = (
            "mean_raw_return_spread_1_minus_0"
            if side == "raw"
            else "mean_rel_hs300_return_spread_1_minus_0"
        )
    else:
        key = (
            "spearman_signal_vs_raw"
            if side == "raw"
            else "spearman_signal_vs_rel_hs300"
        )
    value = stats.get(key)
    return float(value) if isinstance(value, int | float) else None


def _candidate_direction_supported(
    year_2022: float | None,
    year_2023: float | None,
    *,
    candidate_direction: str,
) -> bool | None:
    if year_2022 is None or year_2023 is None:
        return None
    if candidate_direction == "positive":
        return year_2022 > 0 and year_2023 > 0
    return year_2022 < 0 and year_2023 < 0


def _empty_hypothesis_stats(
    *,
    candidate_direction: str,
    signal_kind: str,
) -> dict[str, Any]:
    return {
        "signal_kind": signal_kind,
        "eligible": 0,
        "known": 0,
        "unknown": 0,
        "labeled": 0,
        "known_coverage": None,
        "labeled_coverage": None,
        "candidate_direction": candidate_direction,
        "mean_raw_return": None,
        "median_raw_return": None,
        "win_rate_raw": None,
        "mean_rel_hs300_return": None,
        "median_rel_hs300_return": None,
        "win_rate_rel_hs300": None,
        "labeled_signal_1": None,
        "labeled_signal_0": None,
        "mean_raw_return_signal_1": None,
        "mean_raw_return_signal_0": None,
        "mean_rel_hs300_return_signal_1": None,
        "mean_rel_hs300_return_signal_0": None,
        "mean_raw_return_spread_1_minus_0": None,
        "mean_rel_hs300_return_spread_1_minus_0": None,
        "spearman_signal_vs_raw": None,
        "spearman_signal_vs_rel_hs300": None,
    }


def _hypothesis_stats(
    frame: pl.DataFrame,
    *,
    horizon: int,
    candidate_direction: str,
    signal_kind: str,
) -> dict[str, Any]:
    base = _empty_hypothesis_stats(
        candidate_direction=candidate_direction,
        signal_kind=signal_kind,
    )
    eligible = frame.height
    if eligible == 0:
        return base
    known = int(frame.filter(pl.col("signal_known")).height)
    unknown = eligible - known
    raw_col = f"fwd_raw_ret_{horizon}d"
    rel_col = f"fwd_rel_hs300_ret_{horizon}d"
    known_col = f"label_known_{horizon}d"
    labeled_frame = frame.filter(pl.col(known_col) & pl.col("signal_known"))
    labeled = labeled_frame.height
    pairs_raw = _signal_return_pairs(labeled_frame, signal_col="signal_value", return_col=raw_col)
    pairs_rel = _signal_return_pairs(labeled_frame, signal_col="signal_value", return_col=rel_col)
    base.update(
        {
            "eligible": eligible,
            "known": known,
            "unknown": unknown,
            "labeled": labeled,
            "known_coverage": known / eligible if eligible else None,
            "labeled_coverage": labeled / eligible if eligible else None,
            "spearman_signal_vs_raw": _spearman(pairs_raw),
            "spearman_signal_vs_rel_hs300": _spearman(pairs_rel),
        }
    )
    if signal_kind == "continuous":
        raw_values = [ret for _, ret in pairs_raw]
        rel_values = [ret for _, ret in pairs_rel]
        base.update(
            {
                "mean_raw_return": _mean(raw_values),
                "median_raw_return": _median(raw_values),
                "win_rate_raw": _win_rate(raw_values),
                "mean_rel_hs300_return": _mean(rel_values),
                "median_rel_hs300_return": _median(rel_values),
                "win_rate_rel_hs300": _win_rate(rel_values),
            }
        )
        return base

    # Binary: never pool 0/1 into a single mean/median/win.
    ones = labeled_frame.filter(pl.col("signal_value") == 1.0)
    zeros = labeled_frame.filter(pl.col("signal_value") == 0.0)
    raw_1 = [float(v) for v in ones[raw_col].to_list() if isinstance(v, int | float)]
    raw_0 = [float(v) for v in zeros[raw_col].to_list() if isinstance(v, int | float)]
    rel_1 = [float(v) for v in ones[rel_col].to_list() if isinstance(v, int | float)]
    rel_0 = [float(v) for v in zeros[rel_col].to_list() if isinstance(v, int | float)]
    mean_raw_1 = _mean(raw_1)
    mean_raw_0 = _mean(raw_0)
    mean_rel_1 = _mean(rel_1)
    mean_rel_0 = _mean(rel_0)
    base.update(
        {
            "labeled_signal_1": ones.height,
            "labeled_signal_0": zeros.height,
            "mean_raw_return_signal_1": mean_raw_1,
            "mean_raw_return_signal_0": mean_raw_0,
            "mean_rel_hs300_return_signal_1": mean_rel_1,
            "mean_rel_hs300_return_signal_0": mean_rel_0,
            "mean_raw_return_spread_1_minus_0": _spread(mean_raw_1, mean_raw_0),
            "mean_rel_hs300_return_spread_1_minus_0": _spread(mean_rel_1, mean_rel_0),
        }
    )
    return base


def _signal_return_pairs(
    frame: pl.DataFrame,
    *,
    signal_col: str,
    return_col: str,
) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    if frame.is_empty():
        return pairs
    for signal, ret in frame.select([signal_col, return_col]).iter_rows():
        if (
            isinstance(signal, int | float)
            and isinstance(ret, int | float)
            and math.isfinite(float(signal))
            and math.isfinite(float(ret))
        ):
            pairs.append((float(signal), float(ret)))
    return pairs


def _spread(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def _same_sign(left: float | None, right: float | None) -> bool | None:
    if left is None or right is None:
        return None
    if left == 0.0 or right == 0.0:
        return left == right
    return (left > 0) == (right > 0)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def _win_rate(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value > 0) / len(values)


def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs = [item[0] for item in pairs]
    ys = [item[1] for item in pairs]
    x_rank = _average_ranks(xs)
    y_rank = _average_ranks(ys)
    x_mean = sum(x_rank) / len(x_rank)
    y_mean = sum(y_rank) / len(y_rank)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_rank, y_rank, strict=True))
    x_var = sum((x - x_mean) ** 2 for x in x_rank)
    y_var = sum((y - y_mean) ** 2 for y in y_rank)
    denominator = math.sqrt(x_var * y_var)
    return numerator / denominator if denominator > 0 else None


def _average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    pos = 0
    while pos < len(ordered):
        end = pos + 1
        while end < len(ordered) and ordered[end][1] == ordered[pos][1]:
            end += 1
        average = (pos + 1 + end) / 2.0
        for index, _ in ordered[pos:end]:
            ranks[index] = average
        pos = end
    return ranks


def _empty_observations() -> pl.DataFrame:
    return pl.DataFrame(schema={name: _observation_dtype(name) for name in OBSERVATION_COLUMNS})


def _observation_dtype(name: str) -> Any:
    if name in {"source", "symbol", "hypothesis_id", "threshold_bucket", "source_row_hash"}:
        return pl.String
    if name in {"ann_date", "first_usable_trade_date"}:
        return pl.Date
    if name == "available_at":
        return pl.Datetime("us")
    if name == "year":
        return pl.Int64
    if name in {"signal_known", "label_known_5d", "label_known_10d", "label_known_20d"}:
        return pl.Boolean
    return pl.Float64


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_id(report: EventCandidateDiagnosticReport) -> str:
    payload = report.model_dump(mode="json", exclude={"report_id"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
