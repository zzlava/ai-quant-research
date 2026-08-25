from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from app.research.event_candidate_diagnostics import (
    CANDIDATE_HYPOTHESES,
    DEVELOPMENT_WINDOW_END,
    DEVELOPMENT_WINDOW_START,
    EVENT_CANDIDATE_DIAGNOSTIC_VERSION,
    FORWARD_HORIZONS,
    LABEL_HARD_END,
    CandidateHypothesisSpec,
    EventCandidateDiagnosticReport,
    _candidate_direction_supported,
    load_verified_event_candidate_diagnostics,
)

EVENT_CANDIDATE_OOS_FREEZE_SCHEMA_VERSION: Literal["1"] = "1"
EVENT_CANDIDATE_OOS_FREEZE_VERSION: Literal["a-share-event-candidate-oos-freeze-v1"] = (
    "a-share-event-candidate-oos-freeze-v1"
)
DEFAULT_EVENT_CANDIDATE_OOS_FREEZE_PATH = Path(
    "config/research/a-share-event-candidate-oos-freeze-v1.json"
)
REGISTERED_HYPOTHESIS_COUNT = 11
PRIMARY_OOS_HORIZON_DAYS: Literal[20] = 20
PRIMARY_BENCHMARK_SYMBOL: Literal["000300.SH"] = "000300.SH"
MIN_KNOWN_COVERAGE = 0.90
MIN_LABELED = 100
MIN_BINARY_ARM_LABELED = 20
GATE_YEARS: tuple[Literal["2022"], Literal["2023"]] = ("2022", "2023")
FAILURE_CODE_ORDER: tuple[str, ...] = (
    "primary_metric_unknown",
    "direction_not_supported",
    "known_coverage_below_minimum",
    "labeled_sample_below_minimum",
    "binary_arm_labeled_below_minimum",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NominationGateSpec(_StrictModel):
    primary_horizon_days: Literal[20] = PRIMARY_OOS_HORIZON_DAYS
    primary_return_kind: Literal["relative_vs_benchmark"] = "relative_vs_benchmark"
    benchmark_symbol: Literal["000300.SH"] = PRIMARY_BENCHMARK_SYMBOL
    years: list[Literal["2022", "2023"]]
    min_known_coverage: float = MIN_KNOWN_COVERAGE
    min_labeled: int = MIN_LABELED
    min_binary_arm_labeled: int = MIN_BINARY_ARM_LABELED


class BoundDiagnosticEvidence(_StrictModel):
    artifact_dir: str = Field(min_length=1)
    diagnostic_version: Literal["development-2022-2023-v1"] = EVENT_CANDIDATE_DIAGNOSTIC_VERSION
    report_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_config_id: str = Field(min_length=1)
    strategy_config_hash: str = Field(min_length=1)
    market_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_rows: int = Field(ge=0)
    summary_rows: int = Field(ge=0)
    window_start: date
    window_end: date
    label_hard_end: date
    benchmark_symbol: str = Field(min_length=1)
    forward_horizons: list[int]


class YearGateSnapshot(_StrictModel):
    known_coverage: float | None
    labeled: int = Field(ge=0)
    labeled_signal_1: int | None = None
    labeled_signal_0: int | None = None
    primary_rel_metric_known: bool


class NominationFailure(_StrictModel):
    code: Literal[
        "primary_metric_unknown",
        "direction_not_supported",
        "known_coverage_below_minimum",
        "labeled_sample_below_minimum",
        "binary_arm_labeled_below_minimum",
    ]
    years: list[Literal["2022", "2023"]]


class HypothesisNomination(_StrictModel):
    hypothesis_id: str = Field(min_length=1)
    passed: bool
    reason: str = Field(min_length=1)
    failures: list[NominationFailure]
    candidate_direction_supported_2022_2023_rel_hs300: bool | None
    year_2022: YearGateSnapshot
    year_2023: YearGateSnapshot


class PrimaryOosEndpoint(_StrictModel):
    horizon_days: Literal[20] = PRIMARY_OOS_HORIZON_DAYS
    return_kind: Literal["relative"] = "relative"
    benchmark_symbol: Literal["000300.SH"] = PRIMARY_BENCHMARK_SYMBOL
    observation_field: Literal["fwd_rel_hs300_ret_20d"] = "fwd_rel_hs300_ret_20d"
    decides_oos_result: Literal[True] = True
    may_promote_to_scoring: Literal[False] = False
    may_promote_to_trading: Literal[False] = False


class SecondaryDescriptiveEndpoint(_StrictModel):
    horizon_days: Literal[5, 10, 20]
    return_kind: Literal["raw", "relative"]
    observation_field: str = Field(min_length=1)
    decides_oos_result: Literal[False] = False
    may_promote_candidate: Literal[False] = False


class FrozenOosPolicy(_StrictModel):
    evaluation_mode: Literal["one_shot"] = "one_shot"
    authorized_oos_window: Literal["future_2025_plus_not_yet_authorized"] = (
        "future_2025_plus_not_yet_authorized"
    )
    forbidden_for_selection: list[str]
    must_remain_untouched: list[str]
    preserve_unknown_missingness: Literal[True] = True
    lock_parameters: Literal[True] = True
    lock_sign: Literal[True] = True
    lock_source: Literal[True] = True
    lock_availability: Literal[True] = True
    lock_thresholds: Literal[True] = True
    lock_candidate_list: Literal[True] = True
    report_multiplicity: Literal[True] = True
    auto_promote_to_scoring: Literal[False] = False
    auto_promote_to_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    human_review_required: Literal[True] = True


class EventCandidateOosFreezeContract(_StrictModel):
    schema_version: Literal["1"] = EVENT_CANDIDATE_OOS_FREEZE_SCHEMA_VERSION
    freeze_version: Literal["a-share-event-candidate-oos-freeze-v1"] = (
        EVENT_CANDIDATE_OOS_FREEZE_VERSION
    )
    bound_diagnostic: BoundDiagnosticEvidence
    hypotheses: list[CandidateHypothesisSpec]
    nomination_gate: NominationGateSpec
    hypothesis_nominations: list[HypothesisNomination]
    nominated_hypothesis_ids: list[str]
    nominated_count: int = Field(ge=0)
    registered_hypothesis_count: Literal[11] = 11
    primary_oos_endpoint: PrimaryOosEndpoint
    secondary_descriptive_endpoints: list[SecondaryDescriptiveEndpoint]
    oos_policy: FrozenOosPolicy
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    development_only: Literal[True] = True
    freeze_id: str | None = None
    research_boundary: str = (
        "Development-only freeze for the first future authorized 2025+ one-shot OOS "
        "evaluation. 2024 is already observed and forbidden for selection. No score, IC, "
        "exclusion, portfolio, order, trade, or automatic promotion is authorized."
    )


NOMINATION_GATE = NominationGateSpec(years=["2022", "2023"])
PRIMARY_OOS_ENDPOINT = PrimaryOosEndpoint()
SECONDARY_DESCRIPTIVE_ENDPOINTS: tuple[SecondaryDescriptiveEndpoint, ...] = (
    SecondaryDescriptiveEndpoint(
        horizon_days=5, return_kind="raw", observation_field="fwd_raw_ret_5d"
    ),
    SecondaryDescriptiveEndpoint(
        horizon_days=10, return_kind="raw", observation_field="fwd_raw_ret_10d"
    ),
    SecondaryDescriptiveEndpoint(
        horizon_days=20, return_kind="raw", observation_field="fwd_raw_ret_20d"
    ),
    SecondaryDescriptiveEndpoint(
        horizon_days=5, return_kind="relative", observation_field="fwd_rel_hs300_ret_5d"
    ),
    SecondaryDescriptiveEndpoint(
        horizon_days=10, return_kind="relative", observation_field="fwd_rel_hs300_ret_10d"
    ),
)
FROZEN_OOS_POLICY = FrozenOosPolicy(
    forbidden_for_selection=["2024"],
    must_remain_untouched=["2025+"],
)


def evaluate_nomination_gate(
    summary: pl.DataFrame,
    hypotheses: list[CandidateHypothesisSpec] | tuple[CandidateHypothesisSpec, ...],
    *,
    gate: NominationGateSpec = NOMINATION_GATE,
) -> list[HypothesisNomination]:
    """Nominate hypotheses from the sealed 2022/2023 20d relative summary only."""
    specs = list(hypotheses)
    _assert_registered_hypotheses(specs)
    _assert_summary_gate_rows(summary, specs, gate=gate)
    nominations: list[HypothesisNomination] = []
    for spec in specs:
        row_2022 = _summary_row(summary, spec.hypothesis_id, "2022", gate.primary_horizon_days)
        row_2023 = _summary_row(summary, spec.hypothesis_id, "2023", gate.primary_horizon_days)
        metric_2022 = _primary_rel_metric(row_2022, spec.signal_kind)
        metric_2023 = _primary_rel_metric(row_2023, spec.signal_kind)
        direction_supported = _candidate_direction_supported(
            metric_2022,
            metric_2023,
            candidate_direction=spec.candidate_direction,
        )
        stored_2022 = row_2022["candidate_direction_supported_2022_2023_rel_hs300"]
        stored_2023 = row_2023["candidate_direction_supported_2022_2023_rel_hs300"]
        if stored_2022 != stored_2023 or stored_2022 != direction_supported:
            raise ValueError(
                f"{spec.hypothesis_id} candidate_direction_supported_2022_2023_rel_hs300 "
                "does not match the sealed 20d relative metrics"
            )
        year_2022 = _year_snapshot(row_2022, spec.signal_kind, metric_known=metric_2022 is not None)
        year_2023 = _year_snapshot(row_2023, spec.signal_kind, metric_known=metric_2023 is not None)
        nominations.append(
            _nomination_from_snapshots(
                spec,
                year_2022=year_2022,
                year_2023=year_2023,
                direction_supported=direction_supported,
                gate=gate,
            )
        )
    return nominations


def build_event_candidate_oos_freeze(
    *,
    report: EventCandidateDiagnosticReport,
    summary: pl.DataFrame,
    artifact_dir: str,
    strategy_config_id: str,
) -> EventCandidateOosFreezeContract:
    """Seal a freeze contract from an already-verified development diagnostic."""
    _assert_bound_report(report)
    _assert_relative_artifact_dir(artifact_dir)
    if list(report.hypotheses) != list(CANDIDATE_HYPOTHESES):
        raise ValueError("diagnostic hypotheses do not match the predeclared freeze set")
    nominations = evaluate_nomination_gate(summary, report.hypotheses)
    nominated_ids = [item.hypothesis_id for item in nominations if item.passed]
    contract = EventCandidateOosFreezeContract(
        bound_diagnostic=BoundDiagnosticEvidence(
            artifact_dir=artifact_dir,
            diagnostic_version=report.diagnostic_version,
            report_id=_require_report_id(report),
            strategy_config_id=strategy_config_id,
            strategy_config_hash=report.strategy_config_hash,
            market_snapshot_id=report.market_snapshot_id,
            event_snapshot_id=report.event_snapshot_id,
            observation_file_sha256=_require_sha256(report.observation_file_sha256),
            summary_file_sha256=_require_sha256(report.summary_file_sha256),
            observation_rows=report.observation_rows,
            summary_rows=report.summary_rows,
            window_start=report.window_start,
            window_end=report.window_end,
            label_hard_end=report.label_hard_end,
            benchmark_symbol=report.benchmark_symbol,
            forward_horizons=list(report.forward_horizons),
        ),
        hypotheses=list(CANDIDATE_HYPOTHESES),
        nomination_gate=NOMINATION_GATE,
        hypothesis_nominations=nominations,
        nominated_hypothesis_ids=nominated_ids,
        nominated_count=len(nominated_ids),
        primary_oos_endpoint=PRIMARY_OOS_ENDPOINT,
        secondary_descriptive_endpoints=list(SECONDARY_DESCRIPTIVE_ENDPOINTS),
        oos_policy=FROZEN_OOS_POLICY,
    )
    return contract.model_copy(update={"freeze_id": _freeze_id(contract)})


def write_event_candidate_oos_freeze(
    path: Path,
    contract: EventCandidateOosFreezeContract,
) -> EventCandidateOosFreezeContract:
    sealed = contract if contract.freeze_id == _freeze_id(contract) else build_copy_with_id(contract)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


def build_copy_with_id(contract: EventCandidateOosFreezeContract) -> EventCandidateOosFreezeContract:
    return contract.model_copy(update={"freeze_id": _freeze_id(contract)})


def load_verified_event_candidate_oos_freeze(path: Path) -> EventCandidateOosFreezeContract:
    freeze_path = Path(path)
    try:
        contract = EventCandidateOosFreezeContract.model_validate_json(
            freeze_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError("event candidate OOS freeze contract is missing or invalid") from exc
    _assert_freeze_self_consistent(contract)
    return contract


def verify_event_candidate_oos_freeze(
    *,
    freeze_path: Path,
    diagnostic_dir: Path,
) -> EventCandidateOosFreezeContract:
    contract = load_verified_event_candidate_oos_freeze(freeze_path)
    report, _observations, summary = load_verified_event_candidate_diagnostics(diagnostic_dir)
    _assert_bound_report(report)
    _assert_report_matches_freeze(contract, report)
    recomputed = evaluate_nomination_gate(summary, report.hypotheses, gate=contract.nomination_gate)
    if recomputed != contract.hypothesis_nominations:
        raise ValueError("freeze nominations do not match the sealed 2022/2023 summary gate")
    nominated = [item.hypothesis_id for item in recomputed if item.passed]
    if nominated != contract.nominated_hypothesis_ids:
        raise ValueError("freeze nominated hypothesis IDs do not match the recalculated gate")
    return contract


def _assert_freeze_self_consistent(contract: EventCandidateOosFreezeContract) -> None:
    if contract.freeze_id is None or contract.freeze_id != _freeze_id(contract):
        raise ValueError("event candidate OOS freeze ID does not match its content")
    if contract.ready_for_scoring or contract.ready_for_trading or not contract.development_only:
        raise ValueError("event candidate OOS freeze violates research boundaries")
    if list(contract.hypotheses) != list(CANDIDATE_HYPOTHESES):
        raise ValueError("freeze hypotheses do not match the predeclared candidate definitions")
    if len(contract.hypotheses) != REGISTERED_HYPOTHESIS_COUNT:
        raise ValueError("freeze registered hypothesis count is invalid")
    if contract.nomination_gate != NOMINATION_GATE:
        raise ValueError("freeze nomination gate does not match the frozen protocol")
    if contract.primary_oos_endpoint != PRIMARY_OOS_ENDPOINT:
        raise ValueError("freeze primary OOS endpoint does not match the frozen protocol")
    if contract.secondary_descriptive_endpoints != list(SECONDARY_DESCRIPTIVE_ENDPOINTS):
        raise ValueError("freeze secondary endpoints do not match the frozen protocol")
    if any(item.decides_oos_result or item.may_promote_candidate for item in contract.secondary_descriptive_endpoints):
        raise ValueError("secondary descriptive endpoints cannot decide or promote a candidate")
    if contract.oos_policy != FROZEN_OOS_POLICY:
        raise ValueError("freeze OOS policy does not match the frozen protocol")
    _assert_bound_dates(contract.bound_diagnostic)
    if contract.bound_diagnostic.benchmark_symbol != PRIMARY_BENCHMARK_SYMBOL:
        raise ValueError("freeze benchmark is not 000300.SH")
    if contract.bound_diagnostic.forward_horizons != list(FORWARD_HORIZONS):
        raise ValueError("freeze forward horizons do not match the diagnostic protocol")
    expected_ids = [item.hypothesis_id for item in CANDIDATE_HYPOTHESES]
    observed_ids = [item.hypothesis_id for item in contract.hypothesis_nominations]
    if observed_ids != expected_ids:
        raise ValueError("freeze nominations must register every predeclared hypothesis in order")
    recomputed: list[HypothesisNomination] = []
    by_spec = {item.hypothesis_id: item for item in contract.hypotheses}
    for item in contract.hypothesis_nominations:
        spec = by_spec[item.hypothesis_id]
        recomputed.append(
            _nomination_from_snapshots(
                spec,
                year_2022=item.year_2022,
                year_2023=item.year_2023,
                direction_supported=item.candidate_direction_supported_2022_2023_rel_hs300,
                gate=contract.nomination_gate,
            )
        )
    if recomputed != contract.hypothesis_nominations:
        raise ValueError("freeze pass/fail reasons do not match the frozen gate snapshots")
    nominated = [item.hypothesis_id for item in recomputed if item.passed]
    if nominated != contract.nominated_hypothesis_ids:
        raise ValueError("freeze nominated hypothesis IDs do not match passing hypotheses")
    if contract.nominated_count != len(contract.nominated_hypothesis_ids):
        raise ValueError("freeze nominated_count does not match nominated_hypothesis_ids")
    _assert_relative_artifact_dir(contract.bound_diagnostic.artifact_dir)


def _assert_report_matches_freeze(
    contract: EventCandidateOosFreezeContract,
    report: EventCandidateDiagnosticReport,
) -> None:
    bound = contract.bound_diagnostic
    report_id = _require_report_id(report)
    expected = {
        "report_id": bound.report_id,
        "diagnostic_version": bound.diagnostic_version,
        "strategy_config_hash": bound.strategy_config_hash,
        "market_snapshot_id": bound.market_snapshot_id,
        "event_snapshot_id": bound.event_snapshot_id,
        "observation_file_sha256": bound.observation_file_sha256,
        "summary_file_sha256": bound.summary_file_sha256,
        "observation_rows": bound.observation_rows,
        "summary_rows": bound.summary_rows,
        "window_start": bound.window_start,
        "window_end": bound.window_end,
        "label_hard_end": bound.label_hard_end,
        "benchmark_symbol": bound.benchmark_symbol,
        "forward_horizons": bound.forward_horizons,
    }
    actual = {
        "report_id": report_id,
        "diagnostic_version": report.diagnostic_version,
        "strategy_config_hash": report.strategy_config_hash,
        "market_snapshot_id": report.market_snapshot_id,
        "event_snapshot_id": report.event_snapshot_id,
        "observation_file_sha256": report.observation_file_sha256,
        "summary_file_sha256": report.summary_file_sha256,
        "observation_rows": report.observation_rows,
        "summary_rows": report.summary_rows,
        "window_start": report.window_start,
        "window_end": report.window_end,
        "label_hard_end": report.label_hard_end,
        "benchmark_symbol": report.benchmark_symbol,
        "forward_horizons": list(report.forward_horizons),
    }
    for key, value in expected.items():
        if actual[key] != value:
            raise ValueError(f"freeze bound {key} does not match the verified diagnostic")
    if list(report.hypotheses) != list(contract.hypotheses):
        raise ValueError("freeze hypotheses do not match the bound diagnostic")


def _assert_bound_report(report: EventCandidateDiagnosticReport) -> None:
    if report.report_id is None:
        raise ValueError("diagnostic report is not sealed")
    if report.ready_for_scoring or report.ready_for_trading or not report.development_only:
        raise ValueError("diagnostic report violates research boundaries")
    if report.window_start != DEVELOPMENT_WINDOW_START or report.window_end != DEVELOPMENT_WINDOW_END:
        raise ValueError("diagnostic window is outside the sealed development window")
    if report.label_hard_end != LABEL_HARD_END:
        raise ValueError("diagnostic label_hard_end is invalid")
    if report.benchmark_symbol != PRIMARY_BENCHMARK_SYMBOL:
        raise ValueError("diagnostic benchmark is not 000300.SH")
    if list(report.forward_horizons) != list(FORWARD_HORIZONS):
        raise ValueError("diagnostic forward horizons are invalid")


def _assert_bound_dates(bound: BoundDiagnosticEvidence) -> None:
    if bound.window_start != DEVELOPMENT_WINDOW_START or bound.window_end != DEVELOPMENT_WINDOW_END:
        raise ValueError("freeze development window is invalid")
    if bound.label_hard_end != LABEL_HARD_END:
        raise ValueError("freeze label_hard_end is invalid")


def _assert_registered_hypotheses(hypotheses: list[CandidateHypothesisSpec]) -> None:
    if hypotheses != list(CANDIDATE_HYPOTHESES):
        raise ValueError("hypotheses do not match the predeclared freeze set")
    if len(hypotheses) != REGISTERED_HYPOTHESIS_COUNT:
        raise ValueError("predeclared hypothesis count is invalid")


def _assert_summary_gate_rows(
    summary: pl.DataFrame,
    hypotheses: list[CandidateHypothesisSpec],
    *,
    gate: NominationGateSpec,
) -> None:
    if "hypothesis_id" not in summary.columns:
        raise ValueError("sealed summary is missing hypothesis_id")
    expected = {item.hypothesis_id for item in hypotheses}
    observed = set(
        summary.filter(
            (pl.col("horizon_days") == gate.primary_horizon_days)
            & (pl.col("year").is_in(list(gate.years)))
        )["hypothesis_id"].unique().to_list()
    )
    if observed != expected:
        raise ValueError("sealed summary 20d 2022/2023 rows do not cover the registered hypotheses")


def _assert_relative_artifact_dir(artifact_dir: str) -> None:
    path = Path(artifact_dir)
    if path.is_absolute() or ".." in path.parts or not artifact_dir.strip():
        raise ValueError("freeze artifact_dir must be a relative path without parent traversal")


def _nomination_from_snapshots(
    spec: CandidateHypothesisSpec,
    *,
    year_2022: YearGateSnapshot,
    year_2023: YearGateSnapshot,
    direction_supported: bool | None,
    gate: NominationGateSpec,
) -> HypothesisNomination:
    failures = _nomination_failures(
        spec,
        year_2022=year_2022,
        year_2023=year_2023,
        direction_supported=direction_supported,
        gate=gate,
    )
    passed = not failures
    return HypothesisNomination(
        hypothesis_id=spec.hypothesis_id,
        passed=passed,
        reason=_reason_text(passed, failures),
        failures=failures,
        candidate_direction_supported_2022_2023_rel_hs300=direction_supported,
        year_2022=year_2022,
        year_2023=year_2023,
    )


def _nomination_failures(
    spec: CandidateHypothesisSpec,
    *,
    year_2022: YearGateSnapshot,
    year_2023: YearGateSnapshot,
    direction_supported: bool | None,
    gate: NominationGateSpec,
) -> list[NominationFailure]:
    years = {"2022": year_2022, "2023": year_2023}
    failures: list[NominationFailure] = []
    unknown_years = [year for year, snapshot in years.items() if not snapshot.primary_rel_metric_known]
    if unknown_years:
        failures.append(
            NominationFailure(code="primary_metric_unknown", years=_year_literals(unknown_years))
        )
    elif direction_supported is not True:
        failures.append(
            NominationFailure(code="direction_not_supported", years=["2022", "2023"])
        )
    coverage_years = [
        year
        for year, snapshot in years.items()
        if snapshot.known_coverage is None or snapshot.known_coverage < gate.min_known_coverage
    ]
    if coverage_years:
        failures.append(
            NominationFailure(
                code="known_coverage_below_minimum",
                years=_year_literals(coverage_years),
            )
        )
    labeled_years = [year for year, snapshot in years.items() if snapshot.labeled < gate.min_labeled]
    if labeled_years:
        failures.append(
            NominationFailure(
                code="labeled_sample_below_minimum",
                years=_year_literals(labeled_years),
            )
        )
    if spec.signal_kind == "binary_bucket":
        arm_years = [
            year
            for year, snapshot in years.items()
            if (
                snapshot.labeled_signal_1 is None
                or snapshot.labeled_signal_0 is None
                or snapshot.labeled_signal_1 < gate.min_binary_arm_labeled
                or snapshot.labeled_signal_0 < gate.min_binary_arm_labeled
            )
        ]
        if arm_years:
            failures.append(
                NominationFailure(
                    code="binary_arm_labeled_below_minimum",
                    years=_year_literals(arm_years),
                )
            )
    return sorted(failures, key=lambda item: FAILURE_CODE_ORDER.index(item.code))


def _reason_text(passed: bool, failures: list[NominationFailure]) -> str:
    if passed:
        return "passed_primary_20d_rel_hs300_gate"
    parts: list[str] = []
    for item in failures:
        years = ",".join(item.years)
        if item.code == "direction_not_supported":
            parts.append(item.code)
        else:
            parts.append(f"{item.code}[{years}]")
    return "; ".join(parts)


def _summary_row(
    summary: pl.DataFrame,
    hypothesis_id: str,
    year: str,
    horizon_days: int,
) -> dict[str, Any]:
    matched = summary.filter(
        (pl.col("hypothesis_id") == hypothesis_id)
        & (pl.col("year") == year)
        & (pl.col("horizon_days") == horizon_days)
    )
    if matched.height != 1:
        raise ValueError(
            f"sealed summary must contain exactly one {hypothesis_id} {year} {horizon_days}d row"
        )
    return matched.row(0, named=True)


def _year_snapshot(
    row: dict[str, Any],
    signal_kind: str,
    *,
    metric_known: bool,
) -> YearGateSnapshot:
    labeled_1 = _optional_int(row.get("labeled_signal_1"))
    labeled_0 = _optional_int(row.get("labeled_signal_0"))
    if signal_kind == "continuous":
        labeled_1 = None
        labeled_0 = None
    return YearGateSnapshot(
        known_coverage=_optional_float(row.get("known_coverage")),
        labeled=_require_int(row.get("labeled"), "labeled"),
        labeled_signal_1=labeled_1,
        labeled_signal_0=labeled_0,
        primary_rel_metric_known=metric_known,
    )


def _primary_rel_metric(row: dict[str, Any], signal_kind: str) -> float | None:
    key = (
        "mean_rel_hs300_return_spread_1_minus_0"
        if signal_kind == "binary_bucket"
        else "spearman_signal_vs_rel_hs300"
    )
    return _optional_float(row.get(key))


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("expected a finite number")
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _require_int(value, "integer field")


def _require_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} is not an integer")
    number = int(value)
    if float(value) != float(number):
        raise ValueError(f"{field} is not an integer")
    return number


def _require_report_id(report: EventCandidateDiagnosticReport) -> str:
    if report.report_id is None:
        raise ValueError("diagnostic report_id is missing")
    return report.report_id


def _require_sha256(value: str | None) -> str:
    if value is None:
        raise ValueError("diagnostic file hash is missing")
    return value


def _year_literals(years: list[str]) -> list[Literal["2022", "2023"]]:
    out: list[Literal["2022", "2023"]] = []
    for year in GATE_YEARS:
        if year in years:
            out.append(year)
    return out


def _freeze_id(contract: EventCandidateOosFreezeContract) -> str:
    payload = contract.model_dump(mode="json", exclude={"freeze_id"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
