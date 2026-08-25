from __future__ import annotations

import hashlib
import json
import math
import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from app.models.config import StrategyConfig
from app.models.events import EventSnapshot, EventSourceManifest
from app.providers.tushare_events import EVENT_AVAILABILITY_POLICY
from app.research.event_candidate_diagnostics import (
    FORWARD_HORIZONS,
    OBSERVATION_COLUMNS,
    CandidateHypothesisSpec,
    _assert_source_binding,
    _build_observations,
    _hypothesis_stats,
    _load_label_prices,
    _sha256_file,
)
from app.research.event_candidate_oos_authorization import (
    AuthorizedEvaluationGates,
    EventCandidateOosOneShotAuthorization,
    assert_authorization_paths_unused,
    assert_authorization_self_consistent,
    verify_authorization_against_freeze,
)
from app.storage.snapshot_io import load_verified_snapshot, read_tables

EVENT_CANDIDATE_OOS_EVAL_SCHEMA_VERSION: Literal["1"] = "1"
EVENT_CANDIDATE_OOS_EVAL_VERSION: Literal["one-shot-v1"] = "one-shot-v1"
EVENT_CANDIDATE_OOS_RECEIPT_VERSION: Literal[
    "a-share-event-candidate-oos-one-shot-receipt-v1"
] = "a-share-event-candidate-oos-one-shot-receipt-v1"

CANDIDATE_SUMMARY_COLUMNS = (
    "hypothesis_id",
    "source",
    "signal_kind",
    "threshold_bucket",
    "candidate_direction",
    "eligible",
    "known",
    "unknown",
    "labeled",
    "known_coverage",
    "labeled_coverage",
    "labeled_signal_1",
    "labeled_signal_0",
    "incomplete_20d_label_rows",
    "incomplete_20d_label",
    "mean_rel_hs300_return_signal_1",
    "mean_rel_hs300_return_signal_0",
    "primary_effect_mean_rel_hs300_spread_1_minus_0",
    "expected_direction",
    "outcome",
)

OosOutcome = Literal["not_evaluable", "direction_replicated", "direction_failed"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventCandidateOosEvaluationReport(_StrictModel):
    schema_version: Literal["1"] = EVENT_CANDIDATE_OOS_EVAL_SCHEMA_VERSION
    evaluation_version: Literal["one-shot-v1"] = EVENT_CANDIDATE_OOS_EVAL_VERSION
    authorization_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    freeze_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_config_id: str = Field(min_length=1)
    strategy_config_hash: str = Field(min_length=1)
    market_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_market_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_source_coverage_start: date
    event_source_coverage_end: date
    announcement_window_start: date
    announcement_window_end: date
    first_2025_trading_day: date
    last_complete_label_entry_date: date
    label_hard_end: date
    forward_horizons: list[int]
    benchmark_symbol: str
    availability_policy: str = EVENT_AVAILABILITY_POLICY
    primary_endpoint_field: Literal["fwd_rel_hs300_ret_20d"] = "fwd_rel_hs300_ret_20d"
    primary_statistic: Literal["mean_rel_hs300_return_spread_1_minus_0"] = (
        "mean_rel_hs300_return_spread_1_minus_0"
    )
    nominated_hypothesis_ids: list[str]
    candidate_multiplicity: int = Field(ge=1)
    observation_rows: int = Field(ge=0)
    candidate_summary_rows: int = Field(ge=0)
    observation_file: str = "observations.parquet"
    observation_file_sha256: str | None = None
    candidate_summary_file: str = "candidate_summary.parquet"
    candidate_summary_file_sha256: str | None = None
    candidate_outcomes: dict[str, OosOutcome]
    report_id: str | None = None
    one_shot: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    human_review_required: Literal[True] = True
    research_boundary: str = (
        "One-shot 2025+ directional replication diagnostic only. No p-value, alpha claim, "
        "score, IC, exclusion, portfolio, order, trade, or automatic promotion."
    )


class EventCandidateOosConsumptionReceipt(_StrictModel):
    schema_version: Literal["1"] = "1"
    receipt_version: Literal["a-share-event-candidate-oos-one-shot-receipt-v1"] = (
        EVENT_CANDIDATE_OOS_RECEIPT_VERSION
    )
    authorization_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_dir: str = Field(min_length=1)
    one_shot: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    human_review_required: Literal[True] = True
    receipt_id: str | None = None


def build_event_candidate_oos_evaluation(
    *,
    authorization: EventCandidateOosOneShotAuthorization,
    freeze_path: Path,
    market_dir: Path,
    event_snapshot: EventSnapshot,
    event_source_manifest: EventSourceManifest,
    event_tables: dict[str, pl.DataFrame],
    config: StrategyConfig,
    strategy_config_id: str,
) -> tuple[EventCandidateOosEvaluationReport, pl.DataFrame, pl.DataFrame]:
    """Build the authorized one-shot OOS directional replication diagnostic."""
    assert_authorization_self_consistent(authorization)
    verify_authorization_against_freeze(authorization, freeze_path=freeze_path)
    _assert_runtime_bindings(
        authorization,
        market_dir=market_dir,
        event_snapshot=event_snapshot,
        event_source_manifest=event_source_manifest,
        config=config,
        strategy_config_id=strategy_config_id,
    )

    market = load_verified_snapshot(Path(market_dir))
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
    first_2025 = next((day for day in trading_days if day >= date(2025, 1, 1)), None)
    if first_2025 != authorization.windows.first_2025_trading_day:
        raise ValueError(
            "OOS market first 2025 trading day does not match the authorization contract"
        )
    day_index = {day: idx for idx, day in enumerate(trading_days)}
    _assert_authorized_label_horizon(
        trading_days=trading_days,
        day_index=day_index,
        last_complete_label_entry_date=authorization.windows.last_complete_label_entry_date,
        label_hard_end=authorization.windows.label_hard_end,
    )

    prices, benchmark_prices = _load_label_prices(
        tables,
        benchmark_symbol=authorization.benchmark_symbol,
        label_hard_end=authorization.windows.label_hard_end,
    )
    nominated_ids = list(authorization.nominated_hypothesis_ids)
    observations = _build_observations(
        event_tables=event_tables,
        window_start=authorization.windows.announcement_start,
        window_end=authorization.windows.announcement_end,
        trading_days=trading_days,
        day_index=day_index,
        prices=prices,
        benchmark_prices=benchmark_prices,
        config=config,
        label_hard_end=authorization.windows.label_hard_end,
        entry_end=authorization.windows.last_complete_label_entry_date,
        allowed_hypothesis_ids=frozenset(nominated_ids),
    )
    observed_ids = (
        set(observations["hypothesis_id"].unique().to_list()) if observations.height else set()
    )
    unexpected = observed_ids - set(nominated_ids)
    if unexpected:
        raise ValueError(f"OOS observations contain unexpected hypotheses: {sorted(unexpected)}")
    if observations.height and (
        observations.filter(pl.col("ann_date") < authorization.windows.announcement_start).height
        or observations.filter(pl.col("ann_date") > authorization.windows.announcement_end).height
    ):
        raise ValueError("OOS observations include events outside the authorized announcement window")
    if observations.height and observations.filter(
        pl.col("first_usable_trade_date") > authorization.windows.last_complete_label_entry_date
    ).height:
        raise ValueError("OOS observations include entries after the last complete label entry date")

    summary = _build_candidate_summary(
        observations,
        candidates=authorization.nominated_candidates,
        gates=authorization.evaluation_gates,
        trading_days=trading_days,
        day_index=day_index,
        label_hard_end=authorization.windows.label_hard_end,
    )
    outcomes = {
        str(row["hypothesis_id"]): str(row["outcome"])
        for row in summary.sort("hypothesis_id").iter_rows(named=True)
    }
    if set(outcomes) != set(nominated_ids):
        raise ValueError("OOS candidate summary is missing or has extra nominated candidates")

    report = EventCandidateOosEvaluationReport(
        authorization_id=_require_authorization_id(authorization),
        freeze_id=authorization.freeze_id,
        strategy_config_id=strategy_config_id,
        strategy_config_hash=config.config_hash(),
        market_snapshot_id=market.snapshot_id,
        event_snapshot_id=event_snapshot.snapshot_id,
        base_market_snapshot_id=event_snapshot.base_market_snapshot_id,
        event_source_manifest_sha256=event_snapshot.source_manifest_sha256,
        event_source_coverage_start=event_source_manifest.coverage_start,
        event_source_coverage_end=event_source_manifest.coverage_end,
        announcement_window_start=authorization.windows.announcement_start,
        announcement_window_end=authorization.windows.announcement_end,
        first_2025_trading_day=authorization.windows.first_2025_trading_day,
        last_complete_label_entry_date=authorization.windows.last_complete_label_entry_date,
        label_hard_end=authorization.windows.label_hard_end,
        forward_horizons=list(FORWARD_HORIZONS),
        benchmark_symbol=authorization.benchmark_symbol,
        nominated_hypothesis_ids=nominated_ids,
        candidate_multiplicity=authorization.candidate_multiplicity,
        observation_rows=observations.height,
        candidate_summary_rows=summary.height,
        candidate_outcomes={key: outcomes[key] for key in nominated_ids},
    )
    return report, observations, summary


def write_event_candidate_oos_evaluation_atomically(
    output_dir: Path,
    report: EventCandidateOosEvaluationReport,
    observations: pl.DataFrame,
    summary: pl.DataFrame,
    *,
    receipt_path: Path,
    authorization_id: str,
    authorization_output_dir: str,
) -> tuple[EventCandidateOosEvaluationReport, EventCandidateOosConsumptionReceipt]:
    if tuple(observations.columns) != OBSERVATION_COLUMNS:
        raise ValueError("event candidate OOS observation columns do not match the schema")
    if tuple(summary.columns) != CANDIDATE_SUMMARY_COLUMNS:
        raise ValueError("event candidate OOS summary columns do not match the schema")
    if observations.height != report.observation_rows:
        raise ValueError("observation row count does not match report")
    if summary.height != report.candidate_summary_rows:
        raise ValueError("candidate summary row count does not match report")
    if (
        report.ready_for_scoring
        or report.ready_for_trading
        or report.auto_deploy
        or not report.human_review_required
        or not report.one_shot
    ):
        raise ValueError("event candidate OOS evaluation report violates research boundaries")
    if report.authorization_id != authorization_id:
        raise ValueError("evaluation report authorization_id mismatch")

    destination = Path(output_dir)
    receipt_destination = Path(receipt_path)
    if destination.exists():
        raise ValueError(
            "one-shot OOS evaluation output already exists and is immutable; refuse overwrite"
        )
    if receipt_destination.exists():
        raise ValueError(
            "one-shot OOS evaluation consumption receipt already exists; refuse overwrite"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    receipt_destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".event-candidate-oos-eval-{uuid.uuid4().hex}"
    receipt_temporary = receipt_destination.parent / f".event-candidate-oos-receipt-{uuid.uuid4().hex}"
    try:
        temporary.mkdir(parents=True)
        observation_path = temporary / report.observation_file
        summary_path = temporary / report.candidate_summary_file
        observations.write_parquet(observation_path)
        summary.write_parquet(summary_path)
        with_hashes = report.model_copy(
            update={
                "observation_file_sha256": _sha256_file(observation_path),
                "candidate_summary_file_sha256": _sha256_file(summary_path),
            }
        )
        sealed = with_hashes.model_copy(update={"report_id": _report_id(with_hashes)})
        (temporary / "report.json").write_text(
            sealed.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.rename(destination)

        receipt = EventCandidateOosConsumptionReceipt(
            authorization_id=authorization_id,
            report_id=_require_report_id(sealed),
            output_dir=str(Path(authorization_output_dir)),
        )
        receipt = receipt.model_copy(update={"receipt_id": _receipt_id(receipt)})
        receipt_temporary.write_text(
            receipt.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        receipt_temporary.rename(receipt_destination)
        return sealed, receipt
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if receipt_temporary.exists():
            receipt_temporary.unlink(missing_ok=True)
        if destination.exists() and not receipt_destination.exists():
            # Partial success still marks consumption via output presence; leave immutable dir.
            pass
        raise


def load_verified_event_candidate_oos_evaluation(
    output_dir: Path,
) -> tuple[EventCandidateOosEvaluationReport, pl.DataFrame, pl.DataFrame]:
    root = Path(output_dir)
    try:
        report = EventCandidateOosEvaluationReport.model_validate_json(
            (root / "report.json").read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError("event candidate OOS evaluation report is missing or invalid") from exc
    if report.report_id is None or report.report_id != _report_id(report):
        raise ValueError("event candidate OOS evaluation report ID does not match its content")
    if (
        report.ready_for_scoring
        or report.ready_for_trading
        or report.auto_deploy
        or not report.human_review_required
        or not report.one_shot
    ):
        raise ValueError("event candidate OOS evaluation report violates research boundaries")

    observation_path = root / report.observation_file
    summary_path = root / report.candidate_summary_file
    if not observation_path.is_file() or report.observation_file_sha256 != _sha256_file(
        observation_path
    ):
        raise ValueError("event candidate OOS observation parquet hash does not match report")
    if not summary_path.is_file() or report.candidate_summary_file_sha256 != _sha256_file(
        summary_path
    ):
        raise ValueError("event candidate OOS summary parquet hash does not match report")

    observations = pl.read_parquet(observation_path)
    summary = pl.read_parquet(summary_path)
    if tuple(observations.columns) != OBSERVATION_COLUMNS:
        raise ValueError("event candidate OOS observation parquet schema mismatch")
    if tuple(summary.columns) != CANDIDATE_SUMMARY_COLUMNS:
        raise ValueError("event candidate OOS summary parquet schema mismatch")
    if observations.height != report.observation_rows or summary.height != report.candidate_summary_rows:
        raise ValueError("event candidate OOS parquet row counts do not match report")
    return report, observations, summary


def load_verified_consumption_receipt(path: Path) -> EventCandidateOosConsumptionReceipt:
    receipt_path = Path(path)
    try:
        receipt = EventCandidateOosConsumptionReceipt.model_validate_json(
            receipt_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError("one-shot OOS consumption receipt is missing or invalid") from exc
    if receipt.receipt_id is None or receipt.receipt_id != _receipt_id(receipt):
        raise ValueError("one-shot OOS consumption receipt ID does not match its content")
    if (
        receipt.ready_for_scoring
        or receipt.ready_for_trading
        or receipt.auto_deploy
        or not receipt.human_review_required
        or not receipt.one_shot
    ):
        raise ValueError("one-shot OOS consumption receipt violates research boundaries")
    return receipt


def evaluate_and_write_event_candidate_oos_one_shot(
    *,
    authorization: EventCandidateOosOneShotAuthorization,
    freeze_path: Path,
    market_dir: Path,
    event_dir: Path,
    config: StrategyConfig,
    strategy_config_id: str,
    root: Path | None = None,
) -> tuple[
    EventCandidateOosEvaluationReport,
    EventCandidateOosConsumptionReceipt,
    Path,
]:
    from app.storage.event_io import load_verified_event_snapshot

    assert_authorization_paths_unused(authorization, root=root)
    base = Path(root) if root is not None else Path()
    output_dir = base / authorization.output_dir
    receipt_path = base / authorization.consumption_receipt_path

    market = load_verified_snapshot(Path(market_dir))
    event_snapshot, tables = load_verified_event_snapshot(
        Path(event_dir),
        expected_market_snapshot_id=market.snapshot_id,
    )
    event_source_bytes = (Path(event_dir) / "source_manifest.json").read_bytes()
    if hashlib.sha256(event_source_bytes).hexdigest() != event_snapshot.source_manifest_sha256:
        raise ValueError("event source manifest changed during OOS evaluation loading")
    event_source_manifest = EventSourceManifest.model_validate_json(event_source_bytes)

    report, observations, summary = build_event_candidate_oos_evaluation(
        authorization=authorization,
        freeze_path=freeze_path,
        market_dir=Path(market_dir),
        event_snapshot=event_snapshot,
        event_source_manifest=event_source_manifest,
        event_tables=tables,
        config=config,
        strategy_config_id=strategy_config_id,
    )
    sealed, receipt = write_event_candidate_oos_evaluation_atomically(
        output_dir,
        report,
        observations,
        summary,
        receipt_path=receipt_path,
        authorization_id=_require_authorization_id(authorization),
        authorization_output_dir=authorization.output_dir,
    )
    return sealed, receipt, output_dir


def decide_oos_outcome(
    *,
    candidate_direction: Literal["positive", "negative"],
    known_coverage: float | None,
    labeled: int,
    labeled_signal_1: int | None,
    labeled_signal_0: int | None,
    primary_effect: float | None,
    incomplete_20d_label: bool,
    min_known_coverage: float,
    min_labeled: int,
    min_binary_arm_labeled: int,
) -> OosOutcome:
    if (
        incomplete_20d_label
        or known_coverage is None
        or known_coverage < min_known_coverage
        or labeled < min_labeled
        or labeled_signal_1 is None
        or labeled_signal_0 is None
        or labeled_signal_1 < min_binary_arm_labeled
        or labeled_signal_0 < min_binary_arm_labeled
        or primary_effect is None
        or not math.isfinite(primary_effect)
    ):
        return "not_evaluable"
    if candidate_direction == "positive":
        return "direction_replicated" if primary_effect > 0 else "direction_failed"
    return "direction_replicated" if primary_effect < 0 else "direction_failed"


def _build_candidate_summary(
    observations: pl.DataFrame,
    *,
    candidates: list[CandidateHypothesisSpec],
    gates: AuthorizedEvaluationGates,
    trading_days: list[date],
    day_index: dict[date, int],
    label_hard_end: date,
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for spec in candidates:
        subset = (
            observations.filter(pl.col("hypothesis_id") == spec.hypothesis_id)
            if observations.height
            else observations
        )
        stats = _hypothesis_stats(
            subset,
            horizon=20,
            candidate_direction=spec.candidate_direction,
            signal_kind=spec.signal_kind,
        )
        known = int(stats["known"])
        labeled = int(stats["labeled"])
        incomplete_rows = _count_incomplete_horizon_20d_rows(
            subset,
            day_index=day_index,
            trading_days=trading_days,
            label_hard_end=label_hard_end,
        )
        incomplete = incomplete_rows > 0
        primary = stats.get("mean_rel_hs300_return_spread_1_minus_0")
        primary_effect = float(primary) if isinstance(primary, int | float) else None
        if primary_effect is not None and not math.isfinite(primary_effect):
            primary_effect = None
        outcome = decide_oos_outcome(
            candidate_direction=spec.candidate_direction,
            known_coverage=stats.get("known_coverage"),
            labeled=labeled,
            labeled_signal_1=stats.get("labeled_signal_1"),
            labeled_signal_0=stats.get("labeled_signal_0"),
            primary_effect=primary_effect,
            incomplete_20d_label=incomplete,
            min_known_coverage=gates.min_known_coverage,
            min_labeled=gates.min_labeled,
            min_binary_arm_labeled=gates.min_binary_arm_labeled,
        )
        rows.append(
            {
                "hypothesis_id": spec.hypothesis_id,
                "source": spec.source,
                "signal_kind": spec.signal_kind,
                "threshold_bucket": spec.threshold_bucket,
                "candidate_direction": spec.candidate_direction,
                "eligible": stats["eligible"],
                "known": known,
                "unknown": stats["unknown"],
                "labeled": labeled,
                "known_coverage": stats["known_coverage"],
                "labeled_coverage": stats["labeled_coverage"],
                "labeled_signal_1": stats["labeled_signal_1"],
                "labeled_signal_0": stats["labeled_signal_0"],
                "incomplete_20d_label_rows": incomplete_rows,
                "incomplete_20d_label": incomplete,
                "mean_rel_hs300_return_signal_1": stats["mean_rel_hs300_return_signal_1"],
                "mean_rel_hs300_return_signal_0": stats["mean_rel_hs300_return_signal_0"],
                "primary_effect_mean_rel_hs300_spread_1_minus_0": primary_effect,
                "expected_direction": spec.candidate_direction,
                "outcome": outcome,
            }
        )
    return pl.DataFrame(rows).select(list(CANDIDATE_SUMMARY_COLUMNS))


def _assert_authorized_label_horizon(
    *,
    trading_days: list[date],
    day_index: dict[date, int],
    last_complete_label_entry_date: date,
    label_hard_end: date,
) -> None:
    entry_idx = day_index.get(last_complete_label_entry_date)
    if entry_idx is None:
        raise ValueError(
            "last complete label entry date is not a trading day in the bound market calendar"
        )
    exit_idx = entry_idx + 20
    if exit_idx >= len(trading_days):
        raise ValueError("authorized label horizon extends beyond the bound market calendar")
    expected_hard_end = trading_days[exit_idx]
    if expected_hard_end != label_hard_end:
        raise ValueError(
            "label_hard_end does not equal last_complete_label_entry_date plus 20 trading days"
        )


def _count_incomplete_horizon_20d_rows(
    subset: pl.DataFrame,
    *,
    day_index: dict[date, int],
    trading_days: list[date],
    label_hard_end: date,
) -> int:
    if subset.is_empty():
        return 0
    incomplete_rows = 0
    for row in subset.iter_rows(named=True):
        entry = row["first_usable_trade_date"]
        if not isinstance(entry, date):
            incomplete_rows += 1
            continue
        entry_idx = day_index.get(entry)
        if entry_idx is None:
            incomplete_rows += 1
            continue
        exit_idx = entry_idx + 20
        if exit_idx >= len(trading_days):
            incomplete_rows += 1
            continue
        exit_day = trading_days[exit_idx]
        if exit_day > label_hard_end:
            incomplete_rows += 1
    return incomplete_rows


def _assert_runtime_bindings(
    authorization: EventCandidateOosOneShotAuthorization,
    *,
    market_dir: Path,
    event_snapshot: EventSnapshot,
    event_source_manifest: EventSourceManifest,
    config: StrategyConfig,
    strategy_config_id: str,
) -> None:
    market = load_verified_snapshot(Path(market_dir))
    if market.snapshot_id != authorization.market_snapshot_id:
        raise ValueError("OOS market snapshot_id does not match the authorization contract")
    if event_snapshot.snapshot_id != authorization.event_snapshot_id:
        raise ValueError("OOS event snapshot_id does not match the authorization contract")
    if event_snapshot.base_market_snapshot_id != authorization.base_market_snapshot_id:
        raise ValueError("OOS event base_market_snapshot_id does not match authorization")
    if event_snapshot.base_market_snapshot_id != market.snapshot_id:
        raise ValueError("event overlay is bound to a different market snapshot")
    _assert_source_binding(event_snapshot, event_source_manifest)
    if (
        event_source_manifest.coverage_start
        != authorization.windows.event_source_coverage_start
        or event_source_manifest.coverage_end != authorization.windows.event_source_coverage_end
    ):
        raise ValueError("OOS event source coverage does not match the authorization contract")
    if config.config_hash() != authorization.strategy_config_hash:
        raise ValueError("strategy config hash does not match the authorization contract")
    if strategy_config_id != authorization.strategy_config_id:
        raise ValueError("strategy config id does not match the authorization contract")
    if config.data.market_index != authorization.benchmark_symbol:
        raise ValueError("strategy market_index does not match the authorization benchmark")


def _require_authorization_id(authorization: EventCandidateOosOneShotAuthorization) -> str:
    if authorization.authorization_id is None:
        raise ValueError("authorization_id is missing")
    return authorization.authorization_id


def _require_report_id(report: EventCandidateOosEvaluationReport) -> str:
    if report.report_id is None:
        raise ValueError("report_id is missing")
    return report.report_id


def _report_id(report: EventCandidateOosEvaluationReport) -> str:
    payload = report.model_dump(mode="json", exclude={"report_id"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _receipt_id(receipt: EventCandidateOosConsumptionReceipt) -> str:
    payload = receipt.model_dump(mode="json", exclude={"receipt_id"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
