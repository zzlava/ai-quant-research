"""Fail-closed feasibility review for the frozen layer-two alpha diagnostic.

The review answers one question before any IC or return label is computed:
can the currently sealed candidate and financial-negative-list inputs possibly
meet the frozen cross-sectional coverage gates?  It uses an optimistic upper
bound (every eligible name has a known factor).  Failure of that upper bound is
therefore conclusive and must stop downstream factor diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_alpha_development_protocol import (
    DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH,
    verify_layer_two_alpha_development_protocol_file,
)
from app.research.layer_two_alpha_diagnostic_input_inventory import verify_inventory
from app.research.layer_two_alpha_diagnostic_run_contract import (
    DEFAULT_CONTRACT_PATH,
    verify_contract_file,
)
from app.research.layer_two_candidate_eligibility_pack import verify_candidate_eligibility_pack
from app.research.layer_two_financial_negative_list_overlay import (
    verify_financial_negative_list_verdict_overlay,
)

FEASIBILITY_SCHEMA_VERSION: Literal["1"] = "1"
FEASIBILITY_REPORT_VERSION: Literal["layer-two-alpha-input-feasibility-v1"] = (
    "layer-two-alpha-input-feasibility-v1"
)

DEFAULT_INVENTORY_PATH = Path(
    "data/all-a-share-historical-v1/research/layer-two-alpha-diagnostic-input-inventory-v1.json"
)
DEFAULT_CANDIDATE_PACK_DIR = Path(
    "data/all-a-share-historical-v1/research/candidate-eligibility-pack-v1"
)
DEFAULT_FINANCIAL_OVERLAY_DIR = Path(
    "data/all-a-share-historical-v1/research/financial-negative-list-verdict-overlay-v1"
)
DEFAULT_OUTPUT_PATH = Path(
    "data/all-a-share-historical-v1/research/layer-two-alpha-input-feasibility-v1.json"
)

PRIMARY_HORIZON = 40

BLOCKER_POOLED = "development_primary_valid_date_upper_bound_below_120"
BLOCKER_2022 = "development_2022_primary_valid_date_upper_bound_below_40"
BLOCKER_2023 = "development_2023_primary_valid_date_upper_bound_below_40"
EXPECTED_BLOCKERS = (BLOCKER_POOLED, BLOCKER_2022, BLOCKER_2023)
_OOS_BOUNDARY_RE = re.compile(r"(?:^|[/\-_])oos(?:$|[/\-_])")


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_hex64(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{field_name} must be a 64-char lowercase hex SHA-256")
    return value


class FeasibilitySourceBinding(_StrictFrozen):
    inventory_path: str
    inventory_id: str
    inventory_file_sha256: str
    run_contract_path: str
    run_contract_id: str
    run_contract_file_sha256: str
    alpha_protocol_path: str
    alpha_protocol_id: str
    alpha_protocol_file_sha256: str
    market_snapshot_id: str
    fundamental_snapshot_id: str
    candidate_pack_path: str
    candidate_pack_id: str
    candidate_manifest_sha256: str
    candidate_parquet_sha256: str
    financial_overlay_path: str
    financial_overlay_id: str
    financial_overlay_manifest_sha256: str
    financial_overlay_dataset_hash: str

    @field_validator(
        "inventory_id",
        "inventory_file_sha256",
        "run_contract_id",
        "run_contract_file_sha256",
        "alpha_protocol_id",
        "alpha_protocol_file_sha256",
        "market_snapshot_id",
        "fundamental_snapshot_id",
        "candidate_pack_id",
        "candidate_manifest_sha256",
        "candidate_parquet_sha256",
        "financial_overlay_id",
        "financial_overlay_manifest_sha256",
        "financial_overlay_dataset_hash",
        mode="before",
    )
    @classmethod
    def _hex_fields(cls, value: object, info: Any) -> str:
        return _require_hex64(value, field_name=str(info.field_name))


class FeasibilityThresholds(_StrictFrozen):
    primary_horizon_market_days: Literal[40]
    min_factor_known_cs_per_decision: Literal[500]
    min_factor_known_cs_fraction_of_eligible: float
    min_valid_primary_scoring_dates_pooled: Literal[120]
    min_valid_primary_scoring_dates_in_2022: Literal[40]
    min_valid_primary_scoring_dates_in_2023: Literal[40]

    @field_validator("min_factor_known_cs_fraction_of_eligible", mode="before")
    @classmethod
    def _fraction(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float) or float(value) != 0.60:
            raise ValueError("min_factor_known_cs_fraction_of_eligible must equal 0.60")
        return 0.60


class DailyFeasibility(_StrictFrozen):
    as_of: date
    year: int
    candidate_rows: int = Field(ge=0)
    candidate_complete_rows: int = Field(ge=0)
    candidate_eligible_rows: int = Field(ge=0)
    financial_decisive_rows: int = Field(ge=0)
    financial_hard_excluded_rows: int = Field(ge=0)
    alpha_eligible_upper_bound: int = Field(ge=0)
    h40_endpoint: date | None
    endpoint_within_same_evidence_window: bool
    count_gate_upper_bound_pass: bool
    primary_date_upper_bound_pass: bool

    @model_validator(mode="after")
    def _coherent(self) -> DailyFeasibility:
        if self.year != self.as_of.year:
            raise ValueError("DailyFeasibility.year must equal as_of.year")
        if self.candidate_complete_rows > self.candidate_rows:
            raise ValueError("candidate_complete_rows cannot exceed candidate_rows")
        if self.candidate_eligible_rows > self.candidate_complete_rows:
            raise ValueError("candidate_eligible_rows cannot exceed candidate_complete_rows")
        if self.alpha_eligible_upper_bound > self.candidate_eligible_rows:
            raise ValueError("alpha_eligible_upper_bound cannot exceed candidate_eligible_rows")
        expected_primary = self.count_gate_upper_bound_pass and self.endpoint_within_same_evidence_window
        if self.primary_date_upper_bound_pass is not expected_primary:
            raise ValueError("primary_date_upper_bound_pass is incoherent")
        return self


class YearFeasibility(_StrictFrozen):
    year: Literal[2022, 2023, 2024]
    trading_dates: int = Field(ge=0)
    min_candidate_eligible_rows: int = Field(ge=0)
    median_candidate_eligible_rows: float = Field(ge=0)
    max_candidate_eligible_rows: int = Field(ge=0)
    count_gate_upper_bound_dates: int = Field(ge=0)
    primary_valid_date_upper_bound: int = Field(ge=0)
    min_alpha_eligible_upper_bound: int = Field(ge=0)
    median_alpha_eligible_upper_bound: float = Field(ge=0)
    max_alpha_eligible_upper_bound: int = Field(ge=0)


class FeasibilityReadiness(_StrictFrozen):
    research_only: Literal[True]
    optimistic_upper_bound_only: Literal[True]
    ready_for_alpha_diagnostic_execution: Literal[False]
    ready_for_scoring: Literal[False]
    ready_for_backtest: Literal[False]
    ready_for_portfolio_construction: Literal[False]
    ready_for_orders: Literal[False]
    ready_for_trading: Literal[False]
    auto_apply: Literal[False]


class LayerTwoAlphaInputFeasibilityReport(_StrictFrozen):
    schema_version: Literal["1"]
    report_version: Literal["layer-two-alpha-input-feasibility-v1"]
    source_binding: FeasibilitySourceBinding
    thresholds: FeasibilityThresholds
    coverage_start: date
    coverage_end: date
    trading_date_count: int = Field(ge=1)
    daily: tuple[DailyFeasibility, ...]
    yearly: tuple[YearFeasibility, YearFeasibility, YearFeasibility]
    development_primary_valid_date_upper_bound: int = Field(ge=0)
    blockers: tuple[str, ...]
    statistical_cluster_companion_materialized: Literal[False]
    stop_reason: Literal[
        "frozen_coverage_gates_are_unreachable_even_if_every_eligible_factor_value_is_known"
    ]
    readiness: FeasibilityReadiness
    report_id: str | None = None

    @field_validator("report_id", mode="before")
    @classmethod
    def _report_id(cls, value: object) -> str | None:
        if value is None:
            return None
        return _require_hex64(value, field_name="report_id")

    @model_validator(mode="after")
    def _coherent(self) -> LayerTwoAlphaInputFeasibilityReport:
        if len(self.daily) != self.trading_date_count:
            raise ValueError("daily length must equal trading_date_count")
        if tuple(row.year for row in self.yearly) != (2022, 2023, 2024):
            raise ValueError("yearly rows must be exactly 2022, 2023, 2024")
        if self.blockers != EXPECTED_BLOCKERS:
            raise ValueError("blockers must be the exact fail-closed coverage blockers")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_payload(report: LayerTwoAlphaInputFeasibilityReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def compute_report_id(report: LayerTwoAlphaInputFeasibilityReport) -> str:
    payload = json.dumps(_canonical_payload(report), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def seal_report(report: LayerTwoAlphaInputFeasibilityReport) -> LayerTwoAlphaInputFeasibilityReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def verify_report_self_hash(report: LayerTwoAlphaInputFeasibilityReport) -> None:
    if report.report_id is None or report.report_id != compute_report_id(report):
        raise ValueError("layer-two alpha input feasibility report self-hash mismatch")


def _repo_relative(path: Path, *, repo_root: Path, field_name: str) -> str:
    root = repo_root.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be inside repo_root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{field_name} cannot contain symlinks")
    text = relative.as_posix()
    lower = text.lower()
    if "2025" in lower or _OOS_BOUNDARY_RE.search(lower):
        raise ValueError(f"{field_name} references a forbidden OOS namespace")
    return text


def _load_input_frames(candidate_path: Path, financial_dir: Path) -> tuple[pl.DataFrame, pl.DataFrame]:
    candidate = pl.read_parquet(candidate_path).select(
        "symbol", "as_of", "eligible_for_new_entry", "unknown_critical_input"
    )
    financial = pl.read_parquet(financial_dir / "verdicts" / "*.parquet").select(
        "symbol", "as_of", "decision_status", "eligible_for_new_entry"
    )
    for name, frame in (("candidate", candidate), ("financial", financial)):
        if frame.select(pl.struct("symbol", "as_of").n_unique()).item() != frame.height:
            raise ValueError(f"{name} input contains duplicate symbol/as_of keys")
    if candidate.height != financial.height:
        raise ValueError("candidate and financial input row counts differ")
    return candidate, financial


def compute_daily_feasibility(
    candidate: pl.DataFrame,
    financial: pl.DataFrame,
    *,
    min_known_count: int,
    primary_horizon: int,
) -> tuple[DailyFeasibility, ...]:
    """Pure deterministic upper-bound computation over already verified rows."""
    joined = candidate.join(financial, on=["symbol", "as_of"], how="inner", suffix="_financial")
    if joined.height != candidate.height:
        raise ValueError("candidate and financial key sets differ")
    by_date = (
        joined.group_by("as_of")
        .agg(
            pl.len().alias("candidate_rows"),
            (~pl.col("unknown_critical_input")).sum().alias("candidate_complete_rows"),
            ((~pl.col("unknown_critical_input")) & pl.col("eligible_for_new_entry"))
            .sum()
            .alias("candidate_eligible_rows"),
            (pl.col("decision_status") != "insufficient_evidence").sum().alias("financial_decisive_rows"),
            (pl.col("decision_status") == "hard_excluded").sum().alias("financial_hard_excluded_rows"),
            (
                (~pl.col("unknown_critical_input"))
                & pl.col("eligible_for_new_entry")
                & pl.col("eligible_for_new_entry_financial")
                & pl.col("decision_status").is_in(["clean", "halved"])
            )
            .sum()
            .alias("alpha_eligible_upper_bound"),
        )
        .sort("as_of")
    )
    dates = [date.fromisoformat(str(value)) for value in by_date["as_of"].to_list()]
    rows: list[DailyFeasibility] = []
    for index, values in enumerate(by_date.iter_rows(named=True)):
        as_of = dates[index]
        endpoint = dates[index + primary_horizon] if index + primary_horizon < len(dates) else None
        if as_of.year in (2022, 2023):
            within = endpoint is not None and endpoint <= date(2023, 12, 31)
        elif as_of.year == 2024:
            within = endpoint is not None and endpoint <= date(2024, 12, 31)
        else:
            within = False
        upper_bound = int(values["alpha_eligible_upper_bound"])
        count_pass = upper_bound >= min_known_count
        rows.append(
            DailyFeasibility(
                as_of=as_of,
                year=as_of.year,
                candidate_rows=int(values["candidate_rows"]),
                candidate_complete_rows=int(values["candidate_complete_rows"]),
                candidate_eligible_rows=int(values["candidate_eligible_rows"]),
                financial_decisive_rows=int(values["financial_decisive_rows"]),
                financial_hard_excluded_rows=int(values["financial_hard_excluded_rows"]),
                alpha_eligible_upper_bound=upper_bound,
                h40_endpoint=endpoint,
                endpoint_within_same_evidence_window=within,
                count_gate_upper_bound_pass=count_pass,
                primary_date_upper_bound_pass=count_pass and within,
            )
        )
    return tuple(rows)


def _year_summary(daily: tuple[DailyFeasibility, ...], year: int) -> YearFeasibility:
    rows = [row for row in daily if row.year == year]
    counts = sorted(row.alpha_eligible_upper_bound for row in rows)
    candidate_counts = sorted(row.candidate_eligible_rows for row in rows)
    if not counts:
        raise ValueError(f"no feasibility rows for {year}")
    middle = len(counts) // 2
    median = float(counts[middle]) if len(counts) % 2 else (counts[middle - 1] + counts[middle]) / 2.0
    candidate_median = (
        float(candidate_counts[middle])
        if len(candidate_counts) % 2
        else (candidate_counts[middle - 1] + candidate_counts[middle]) / 2.0
    )
    return YearFeasibility(
        year=year,
        trading_dates=len(rows),
        min_candidate_eligible_rows=min(candidate_counts),
        median_candidate_eligible_rows=candidate_median,
        max_candidate_eligible_rows=max(candidate_counts),
        count_gate_upper_bound_dates=sum(row.count_gate_upper_bound_pass for row in rows),
        primary_valid_date_upper_bound=sum(row.primary_date_upper_bound_pass for row in rows),
        min_alpha_eligible_upper_bound=min(counts),
        median_alpha_eligible_upper_bound=median,
        max_alpha_eligible_upper_bound=max(counts),
    )


def build_feasibility_report(
    *,
    repo_root: Path,
    inventory_path: Path = DEFAULT_INVENTORY_PATH,
    candidate_pack_dir: Path = DEFAULT_CANDIDATE_PACK_DIR,
    financial_overlay_dir: Path = DEFAULT_FINANCIAL_OVERLAY_DIR,
) -> LayerTwoAlphaInputFeasibilityReport:
    root = repo_root.resolve()
    inventory_full = root / inventory_path if not inventory_path.is_absolute() else inventory_path
    candidate_full = root / candidate_pack_dir if not candidate_pack_dir.is_absolute() else candidate_pack_dir
    financial_full = root / financial_overlay_dir if not financial_overlay_dir.is_absolute() else financial_overlay_dir
    inventory_rel = _repo_relative(inventory_full, repo_root=root, field_name="inventory_path")
    candidate_rel = _repo_relative(candidate_full, repo_root=root, field_name="candidate_pack_dir")
    financial_rel = _repo_relative(financial_full, repo_root=root, field_name="financial_overlay_dir")

    inventory = verify_inventory(inventory_full, repo_root=root)
    candidate_manifest = verify_candidate_eligibility_pack(candidate_full, repo_root=root)
    financial_result = verify_financial_negative_list_verdict_overlay(
        repo_root=root, overlay_dir=financial_full
    )
    run_contract, _ = verify_contract_file(contract_path=root / DEFAULT_CONTRACT_PATH, repo_root=root)
    protocol, _ = verify_layer_two_alpha_development_protocol_file(
        protocol_path=root / DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH,
        repo_root=root,
    )
    if inventory.inventory_id is None or candidate_manifest.pack_id is None or protocol.protocol_id is None:
        raise ValueError("an upstream sealed ID is missing")

    candidate_manifest_path = candidate_full / "manifest.json"
    candidate_parquet_path = candidate_full / "eligibility_verdicts.parquet"
    financial_manifest_path = financial_full / "manifest.json"
    financial_manifest = json.loads(financial_manifest_path.read_text(encoding="utf-8"))
    if financial_manifest.get("candidate_pack_id") != candidate_manifest.pack_id:
        raise ValueError("financial overlay candidate pack binding mismatch")

    candidate, financial = _load_input_frames(candidate_parquet_path, financial_full)
    thresholds = protocol.coverage_gates
    daily = compute_daily_feasibility(
        candidate,
        financial,
        min_known_count=thresholds.min_factor_known_cs_per_decision,
        primary_horizon=PRIMARY_HORIZON,
    )
    yearly = tuple(_year_summary(daily, year) for year in (2022, 2023, 2024))
    dev_valid = sum(row.primary_date_upper_bound_pass for row in daily if row.year in (2022, 2023))
    if dev_valid >= thresholds.min_valid_primary_scoring_dates_pooled:
        raise ValueError("pooled upper-bound coverage unexpectedly reaches the frozen gate; review schema must advance")
    if yearly[0].primary_valid_date_upper_bound >= thresholds.min_valid_primary_scoring_dates_in_2022:
        raise ValueError("2022 upper-bound coverage unexpectedly reaches the frozen gate; review schema must advance")
    if yearly[1].primary_valid_date_upper_bound >= thresholds.min_valid_primary_scoring_dates_in_2023:
        raise ValueError("2023 upper-bound coverage unexpectedly reaches the frozen gate; review schema must advance")

    market_slot = next(slot for slot in inventory.slots if slot.kind == "sealed_market_snapshot")
    fundamental_slot = next(slot for slot in inventory.slots if slot.kind == "pit_fundamental_overlay")
    report = LayerTwoAlphaInputFeasibilityReport(
        schema_version=FEASIBILITY_SCHEMA_VERSION,
        report_version=FEASIBILITY_REPORT_VERSION,
        source_binding=FeasibilitySourceBinding(
            inventory_path=inventory_rel,
            inventory_id=inventory.inventory_id,
            inventory_file_sha256=_sha256_file(inventory_full),
            run_contract_path=DEFAULT_CONTRACT_PATH.as_posix(),
            run_contract_id=str(run_contract.contract_id),
            run_contract_file_sha256=_sha256_file(root / DEFAULT_CONTRACT_PATH),
            alpha_protocol_path=DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH.as_posix(),
            alpha_protocol_id=protocol.protocol_id,
            alpha_protocol_file_sha256=_sha256_file(root / DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH),
            market_snapshot_id=market_slot.snapshot_id,  # type: ignore[union-attr]
            fundamental_snapshot_id=fundamental_slot.snapshot_id,  # type: ignore[union-attr]
            candidate_pack_path=candidate_rel,
            candidate_pack_id=candidate_manifest.pack_id,
            candidate_manifest_sha256=_sha256_file(candidate_manifest_path),
            candidate_parquet_sha256=_sha256_file(candidate_parquet_path),
            financial_overlay_path=financial_rel,
            financial_overlay_id=financial_result.overlay_id,
            financial_overlay_manifest_sha256=_sha256_file(financial_manifest_path),
            financial_overlay_dataset_hash=str(financial_manifest["dataset_hash"]),
        ),
        thresholds=FeasibilityThresholds(
            primary_horizon_market_days=40,
            min_factor_known_cs_per_decision=thresholds.min_factor_known_cs_per_decision,
            min_factor_known_cs_fraction_of_eligible=thresholds.min_factor_known_cs_fraction_of_eligible,
            min_valid_primary_scoring_dates_pooled=thresholds.min_valid_primary_scoring_dates_pooled,
            min_valid_primary_scoring_dates_in_2022=thresholds.min_valid_primary_scoring_dates_in_2022,
            min_valid_primary_scoring_dates_in_2023=thresholds.min_valid_primary_scoring_dates_in_2023,
        ),
        coverage_start=daily[0].as_of,
        coverage_end=daily[-1].as_of,
        trading_date_count=len(daily),
        daily=daily,
        yearly=yearly,
        development_primary_valid_date_upper_bound=dev_valid,
        blockers=EXPECTED_BLOCKERS,
        statistical_cluster_companion_materialized=False,
        stop_reason="frozen_coverage_gates_are_unreachable_even_if_every_eligible_factor_value_is_known",
        readiness=FeasibilityReadiness(
            research_only=True,
            optimistic_upper_bound_only=True,
            ready_for_alpha_diagnostic_execution=False,
            ready_for_scoring=False,
            ready_for_backtest=False,
            ready_for_portfolio_construction=False,
            ready_for_orders=False,
            ready_for_trading=False,
            auto_apply=False,
        ),
        report_id=None,
    )
    return seal_report(report)


def verify_feasibility_report_file(
    path: Path,
    *,
    repo_root: Path,
) -> LayerTwoAlphaInputFeasibilityReport:
    try:
        report = LayerTwoAlphaInputFeasibilityReport.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("layer-two alpha input feasibility report is missing or invalid") from exc
    verify_report_self_hash(report)
    rebuilt = build_feasibility_report(
        repo_root=repo_root,
        inventory_path=Path(report.source_binding.inventory_path),
        candidate_pack_dir=Path(report.source_binding.candidate_pack_path),
        financial_overlay_dir=Path(report.source_binding.financial_overlay_path),
    )
    if report.model_dump(mode="json") != rebuilt.model_dump(mode="json"):
        raise ValueError("layer-two alpha input feasibility report does not recompute from sealed inputs")
    return report


def write_feasibility_report(
    path: Path,
    report: LayerTwoAlphaInputFeasibilityReport,
    *,
    replace_existing: bool = False,
) -> None:
    verify_report_self_hash(report)
    destination = path
    if destination.exists() and not replace_existing:
        raise FileExistsError(f"feasibility report already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


__all__ = [
    "BLOCKER_2022",
    "BLOCKER_2023",
    "BLOCKER_POOLED",
    "DEFAULT_CANDIDATE_PACK_DIR",
    "DEFAULT_FINANCIAL_OVERLAY_DIR",
    "DEFAULT_INVENTORY_PATH",
    "DEFAULT_OUTPUT_PATH",
    "DailyFeasibility",
    "LayerTwoAlphaInputFeasibilityReport",
    "build_feasibility_report",
    "compute_daily_feasibility",
    "compute_report_id",
    "seal_report",
    "verify_feasibility_report_file",
    "verify_report_self_hash",
    "write_feasibility_report",
]
