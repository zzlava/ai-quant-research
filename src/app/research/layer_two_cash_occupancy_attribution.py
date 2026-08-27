"""Layer-two longitudinal cash-occupancy attribution (E10e-1).

Research-only aggregation over sealed E10e-0 entry-execution diagnostics.
Attributes retained / unused entry cash to the confirmed occupancy-cause set.
Does not resolve the tranche-protocol cash-occupancy blocker, does not modify
allocator/execution semantics, and does not claim account-level utilization.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_allocation_protocol import _require_non_bool_int, _require_real_number
from app.research.layer_two_candidate_eligibility import LayerTwoCandidateEligibilityReport
from app.research.layer_two_constraint_assembler import LayerTwoConstraintAssemblerReport
from app.research.layer_two_entry_execution_diagnostic import (
    BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
    LayerTwoEntryExecutionDiagnosticReport,
    LayerTwoEntryExecutionObservation,
    verify_layer_two_entry_execution_diagnostic_report,
    verify_layer_two_entry_execution_diagnostic_report_file,
)
from app.research.layer_two_entry_execution_diagnostic import (
    assert_report_self_hash as assert_entry_execution_report_self_hash,
)
from app.research.layer_two_financial_negative_list import LayerTwoFinancialNegativeListReport
from app.research.layer_two_stateful_allocator import (
    LayerTwoStatefulAllocatorReport,
    LayerTwoStatefulPortfolioState,
    UnvalidatedDevelopmentRankingInput,
)
from app.research.layer_two_statistical_risk_clusters import LayerTwoStatisticalRiskClusterReport
from app.research.layer_two_tranche_phase_schedule import LayerTwoTranchePhaseScheduleReport
from app.research.tranche_evaluation_protocol import CONFIRMED_CASH_OCCUPANCY_CAUSES
from app.storage.protocol import MarketStore

LAYER_TWO_CASH_OCCUPANCY_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_CASH_OCCUPANCY_ENGINE_VERSION: Literal["layer-two-cash-occupancy-attribution-v1"] = (
    "layer-two-cash-occupancy-attribution-v1"
)

BOUND_OCCUPANCY_CAUSES: tuple[str, ...] = tuple(CONFIRMED_CASH_OCCUPANCY_CAUSES)
assert BOUND_OCCUPANCY_CAUSES == (
    "candidate_shortage",
    "gates",
    "unaffordable_board_lot_or_min_commission",
    "suspension",
    "limit_up_or_limit_down",
    "risk_budget",
)

KnownOccupancyCause = Literal[
    "candidate_shortage",
    "gates",
    "unaffordable_board_lot_or_min_commission",
    "suspension",
    "limit_up_or_limit_down",
    "risk_budget",
]
RowCauseMarker = KnownOccupancyCause | Literal["unknown", "no_retained_cash"]
ExecutionOutcome = Literal[
    "not_attempted",
    "unknown_execution_observation",
    "blocked_suspension",
    "blocked_limit_up",
    "unaffordable_board_lot_or_minimum_commission",
    "hypothetically_fillable",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_AMOUNT_ABS_TOL = 1e-6

_RISK_BUDGET_RETENTION = frozenset(
    {
        "zero_risk_budget",
        "insufficient_capital_for_minimum_base_slot",
        "preexisting_sleeve_breach",
    }
)
_GATES_RETENTION = frozenset(
    {
        "upstream_not_ready_for_stateful_allocator_input",
        "no_active_tranche",
        "no_selected_phase_opportunity",
        "selected_tranche_occupied",
        "preexisting_cluster_breach",
    }
)
_RISK_BUDGET_REJECTIONS = frozenset({"insufficient_cash", "sleeve_notional_cap"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_literal_true(value: object, *, field_name: str) -> Literal[True]:
    if value is not True:
        raise ValueError(f"{field_name} must be the boolean True")
    return True


def _require_literal_false(value: object, *, field_name: str) -> Literal[False]:
    if value is not False:
        raise ValueError(f"{field_name} must be the boolean False")
    return False


def _require_strict_bool(value: object, *, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _require_date(value: object, *, field_name: str) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str) and value.strip():
        return date.fromisoformat(value.strip())
    raise ValueError(f"{field_name} must be a datetime.date")


def _require_sealed_hex64(value: str | None, *, field_name: str) -> str:
    if value is None or not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sealed 64-char lowercase hex digest")
    return value


def _amounts_equal(left: float, right: float) -> bool:
    return abs(left - right) <= _AMOUNT_ABS_TOL


@dataclass(frozen=True, slots=True)
class LayerTwoCashOccupancyStructuralRowInput:
    """Exact inputs required to structurally verify one E10e-0 diagnostic."""

    entry_execution_report: LayerTwoEntryExecutionDiagnosticReport
    allocator_report: LayerTwoStatefulAllocatorReport
    constraint_report: LayerTwoConstraintAssemblerReport
    current_state: LayerTwoStatefulPortfolioState
    ranking: UnvalidatedDevelopmentRankingInput
    phase_report: LayerTwoTranchePhaseScheduleReport
    execution_observation: LayerTwoEntryExecutionObservation | None


@dataclass(frozen=True, slots=True)
class LayerTwoCashOccupancyFileRowBindings:
    """Disk-bound upstreams required to file-verify one E10e-0 diagnostic."""

    eligibility_report: LayerTwoCandidateEligibilityReport
    financial_reports: tuple[LayerTwoFinancialNegativeListReport, ...]
    cluster_report: LayerTwoStatisticalRiskClusterReport
    store: MarketStore
    repo_root: Path
    phase_report_path: Path


@dataclass(frozen=True, slots=True)
class LayerTwoCashOccupancyFileRowInput:
    structural: LayerTwoCashOccupancyStructuralRowInput
    file_bindings: LayerTwoCashOccupancyFileRowBindings


class CashOccupancyRowAttribution(_StrictModel):
    as_of: date
    entry_execution_report_id: str = Field(pattern=_HEX64.pattern)
    allocator_report_id: str = Field(pattern=_HEX64.pattern)
    execution_outcome: ExecutionOutcome
    cause_marker: RowCauseMarker
    amount_quantified: bool
    known_target_cash: float | None = None
    known_base_cash_used: float | None = None
    known_retained_cash: float | None = None
    classification_evidence: str = Field(min_length=1)
    limit_up_or_limit_down_represents_buy_side_limit_up_only: Literal[True] = True
    stress_scenario_not_used_for_attribution: Literal[True] = True

    @field_validator("as_of", mode="before")
    @classmethod
    def _as_of(cls, value: object) -> date:
        return _require_date(value, field_name="as_of")

    @field_validator("amount_quantified", mode="before")
    @classmethod
    def _quant(cls, value: object) -> bool:
        return _require_strict_bool(value, field_name="amount_quantified")

    @field_validator(
        "known_target_cash",
        "known_base_cash_used",
        "known_retained_cash",
        mode="before",
    )
    @classmethod
    def _optional_amount(cls, value: object, info: Any) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator(
        "limit_up_or_limit_down_represents_buy_side_limit_up_only",
        "stress_scenario_not_used_for_attribution",
        mode="before",
    )
    @classmethod
    def _true(cls, value: object, info: Any) -> object:
        return _require_literal_true(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _identities(self) -> CashOccupancyRowAttribution:
        if not self.amount_quantified:
            if (
                self.known_target_cash is not None
                or self.known_base_cash_used is not None
                or self.known_retained_cash is not None
            ):
                raise ValueError("unquantified row must keep all known cash amounts null")
            if self.cause_marker in (
                "suspension",
                "limit_up_or_limit_down",
                "unaffordable_board_lot_or_min_commission",
                "no_retained_cash",
            ):
                raise ValueError(f"{self.cause_marker} rows must be amount_quantified")
            return self
        if self.known_target_cash is None or self.known_base_cash_used is None or self.known_retained_cash is None:
            raise ValueError("quantified row requires known target/used/retained cash")
        if not _amounts_equal(
            self.known_target_cash,
            self.known_base_cash_used + self.known_retained_cash,
        ):
            raise ValueError("known_target_cash must equal known_base_cash_used + known_retained_cash")
        return self


class CashOccupancyCauseSummary(_StrictModel):
    cause: KnownOccupancyCause
    decision_count: int
    quantified_row_count: int
    unquantified_row_count: int
    sum_known_target_cash: float
    sum_known_base_cash_used: float
    sum_known_retained_cash: float

    @field_validator(
        "decision_count",
        "quantified_row_count",
        "unquantified_row_count",
        mode="before",
    )
    @classmethod
    def _counts(cls, value: object, info: Any) -> int:
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=0)

    @field_validator(
        "sum_known_target_cash",
        "sum_known_base_cash_used",
        "sum_known_retained_cash",
        mode="before",
    )
    @classmethod
    def _sums(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @model_validator(mode="after")
    def _gate(self) -> CashOccupancyCauseSummary:
        if self.decision_count != self.quantified_row_count + self.unquantified_row_count:
            raise ValueError("decision_count must equal quantified + unquantified row counts")
        if not _amounts_equal(
            self.sum_known_target_cash,
            self.sum_known_base_cash_used + self.sum_known_retained_cash,
        ):
            raise ValueError("cause summary target must equal used + retained")
        if self.quantified_row_count == 0:
            if (
                abs(self.sum_known_target_cash) > _AMOUNT_ABS_TOL
                or abs(self.sum_known_base_cash_used) > _AMOUNT_ABS_TOL
                or abs(self.sum_known_retained_cash) > _AMOUNT_ABS_TOL
            ):
                raise ValueError("unquantified-only cause must keep cash sums at zero")
        return self


class LayerTwoCashOccupancyAttributionReport(_StrictModel):
    schema_version: Literal["1"] = LAYER_TWO_CASH_OCCUPANCY_SCHEMA_VERSION
    engine_version: Literal["layer-two-cash-occupancy-attribution-v1"] = LAYER_TWO_CASH_OCCUPANCY_ENGINE_VERSION
    report_id: str | None = Field(default=None, pattern=_HEX64.pattern)
    market_data_snapshot_id: str = Field(min_length=1)
    phase_report_id: str = Field(pattern=_HEX64.pattern)
    tranche_evaluation_protocol_id: Literal["8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_ID
    )
    input_entry_execution_report_ids: list[str]
    coverage_as_of_start: date
    coverage_as_of_end: date
    row_count: int
    rows: list[CashOccupancyRowAttribution]
    cause_summaries: list[CashOccupancyCauseSummary]
    total_report_count: int
    total_attempt_count: int
    total_not_attempt_count: int
    total_unknown_count: int
    total_no_retained_count: int
    global_sum_known_target_cash: float
    global_sum_known_base_cash_used: float
    global_sum_known_retained_cash: float
    amount_abs_tol: float = _AMOUNT_ABS_TOL
    does_not_claim_account_utilization: Literal[True] = True
    does_not_claim_full_sleeve_cash_attribution: Literal[True] = True
    limit_up_or_limit_down_buy_side_limit_up_only: Literal[True] = True
    protocol_cash_occupancy_blocker_not_resolved: Literal[True] = True
    diagnostic_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_modify_allocator_or_execution: Literal[True] = True

    @field_validator("coverage_as_of_start", "coverage_as_of_end", mode="before")
    @classmethod
    def _dates(cls, value: object, info: Any) -> date:
        return _require_date(value, field_name=str(info.field_name))

    @field_validator(
        "row_count",
        "total_report_count",
        "total_attempt_count",
        "total_not_attempt_count",
        "total_unknown_count",
        "total_no_retained_count",
        mode="before",
    )
    @classmethod
    def _counts(cls, value: object, info: Any) -> int:
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=0)

    @field_validator(
        "global_sum_known_target_cash",
        "global_sum_known_base_cash_used",
        "global_sum_known_retained_cash",
        "amount_abs_tol",
        mode="before",
    )
    @classmethod
    def _nums(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator(
        "diagnostic_only",
        "does_not_modify_allocator_or_execution",
        "does_not_claim_account_utilization",
        "does_not_claim_full_sleeve_cash_attribution",
        "limit_up_or_limit_down_buy_side_limit_up_only",
        "protocol_cash_occupancy_blocker_not_resolved",
        mode="before",
    )
    @classmethod
    def _true(cls, value: object, info: Any) -> object:
        return _require_literal_true(value, field_name=str(info.field_name))

    @field_validator(
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_portfolio_construction",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
        mode="before",
    )
    @classmethod
    def _false(cls, value: object, info: Any) -> object:
        return _require_literal_false(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _gate(self) -> LayerTwoCashOccupancyAttributionReport:
        if self.row_count != len(self.rows) or self.row_count < 1:
            raise ValueError("row_count must equal non-empty rows length")
        if self.total_report_count != self.row_count:
            raise ValueError("total_report_count must equal row_count")
        if len(self.input_entry_execution_report_ids) != self.row_count:
            raise ValueError("input_entry_execution_report_ids length must equal row_count")
        as_ofs = [row.as_of for row in self.rows]
        if as_ofs != sorted(as_ofs) or len(as_ofs) != len(set(as_ofs)):
            raise ValueError("rows must be strictly increasing by unique as_of")
        if as_ofs[0] != self.coverage_as_of_start or as_ofs[-1] != self.coverage_as_of_end:
            raise ValueError("coverage_as_of_start/end must match first/last row as_of")
        ids = [row.entry_execution_report_id for row in self.rows]
        if ids != self.input_entry_execution_report_ids:
            raise ValueError("input_entry_execution_report_ids must match row order")
        if len(set(ids)) != len(ids):
            raise ValueError("entry_execution_report_id must be unique")
        if [s.cause for s in self.cause_summaries] != list(BOUND_OCCUPANCY_CAUSES):
            raise ValueError("cause_summaries must cover CONFIRMED_CASH_OCCUPANCY_CAUSES in frozen order")
        attempt = sum(1 for row in self.rows if row.execution_outcome != "not_attempted")
        not_attempt = sum(1 for row in self.rows if row.execution_outcome == "not_attempted")
        if attempt + not_attempt != self.row_count:
            raise ValueError("attempt + not_attempt counts must equal row_count")
        if attempt != self.total_attempt_count or not_attempt != self.total_not_attempt_count:
            raise ValueError("total_attempt_count/total_not_attempt_count must recompute from rows")
        unknown = sum(1 for row in self.rows if row.cause_marker == "unknown")
        no_ret = sum(1 for row in self.rows if row.cause_marker == "no_retained_cash")
        if unknown != self.total_unknown_count or no_ret != self.total_no_retained_count:
            raise ValueError("unknown/no_retained totals must match row markers")
        cause_decision_total = sum(summary.decision_count for summary in self.cause_summaries)
        if cause_decision_total + unknown + no_ret != self.row_count:
            raise ValueError("cause decisions + unknown + no_retained must equal row_count")
        for summary in self.cause_summaries:
            matching = [row for row in self.rows if row.cause_marker == summary.cause]
            quantified = [row for row in matching if row.amount_quantified]
            unquantified = [row for row in matching if not row.amount_quantified]
            if summary.decision_count != len(matching):
                raise ValueError(f"cause summary decision_count drift for {summary.cause}")
            if summary.quantified_row_count != len(quantified):
                raise ValueError(f"cause summary quantified_row_count drift for {summary.cause}")
            if summary.unquantified_row_count != len(unquantified):
                raise ValueError(f"cause summary unquantified_row_count drift for {summary.cause}")
            sum_target = sum(float(row.known_target_cash or 0.0) for row in quantified)
            sum_used = sum(float(row.known_base_cash_used or 0.0) for row in quantified)
            sum_retained = sum(float(row.known_retained_cash or 0.0) for row in quantified)
            if not _amounts_equal(summary.sum_known_target_cash, sum_target):
                raise ValueError(f"cause summary sum_known_target_cash drift for {summary.cause}")
            if not _amounts_equal(summary.sum_known_base_cash_used, sum_used):
                raise ValueError(f"cause summary sum_known_base_cash_used drift for {summary.cause}")
            if not _amounts_equal(summary.sum_known_retained_cash, sum_retained):
                raise ValueError(f"cause summary sum_known_retained_cash drift for {summary.cause}")
        global_target = sum(float(row.known_target_cash or 0.0) for row in self.rows if row.amount_quantified)
        global_used = sum(float(row.known_base_cash_used or 0.0) for row in self.rows if row.amount_quantified)
        global_retained = sum(float(row.known_retained_cash or 0.0) for row in self.rows if row.amount_quantified)
        if not _amounts_equal(self.global_sum_known_target_cash, global_target):
            raise ValueError("global_sum_known_target_cash must recompute from quantified rows")
        if not _amounts_equal(self.global_sum_known_base_cash_used, global_used):
            raise ValueError("global_sum_known_base_cash_used must recompute from quantified rows")
        if not _amounts_equal(self.global_sum_known_retained_cash, global_retained):
            raise ValueError("global_sum_known_retained_cash must recompute from quantified rows")
        if not _amounts_equal(
            self.global_sum_known_target_cash,
            self.global_sum_known_base_cash_used + self.global_sum_known_retained_cash,
        ):
            raise ValueError("global known target must equal known used + retained")
        if abs(self.amount_abs_tol - _AMOUNT_ABS_TOL) > 1e-15:
            raise ValueError("amount_abs_tol must equal declared module tolerance")
        return self


class LayerTwoCashOccupancyAttributionVerificationResult(_StrictModel):
    report_id: str
    structural_ok: bool
    entry_execution_binding_ok: bool = False
    phase_binding_ok: bool = False
    tranche_evaluation_protocol_binding_ok: bool = False
    diagnostic_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator(
        "structural_ok",
        "entry_execution_binding_ok",
        "phase_binding_ok",
        "tranche_evaluation_protocol_binding_ok",
        mode="before",
    )
    @classmethod
    def _plain(cls, value: object, info: Any) -> bool:
        return _require_strict_bool(value, field_name=str(info.field_name))

    @field_validator("diagnostic_only", mode="before")
    @classmethod
    def _true(cls, value: object, info: Any) -> object:
        return _require_literal_true(value, field_name=str(info.field_name))

    @field_validator(
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_portfolio_construction",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
        mode="before",
    )
    @classmethod
    def _false(cls, value: object, info: Any) -> object:
        return _require_literal_false(value, field_name=str(info.field_name))


def _map_not_attempted_cause(allocator: LayerTwoStatefulAllocatorReport) -> tuple[KnownOccupancyCause, str]:
    reason = allocator.portfolio_cash_retention_reason
    if reason is None:
        raise ValueError("not_attempted allocator missing portfolio_cash_retention_reason")
    if reason in _RISK_BUDGET_RETENTION:
        return "risk_budget", f"not_attempted retention={reason}"
    if reason in _GATES_RETENTION:
        return "gates", f"not_attempted retention={reason}"
    if reason != "no_admissible_candidate":
        raise ValueError(f"unsupported portfolio_cash_retention_reason: {reason}")
    diagnostics = list(allocator.candidate_rejection_diagnostics)
    if not diagnostics or all(item.reason == "already_held" for item in diagnostics):
        return "candidate_shortage", "no_admissible_candidate empty_or_all_already_held"
    reasons = {item.reason for item in diagnostics}
    if reasons and reasons <= _RISK_BUDGET_REJECTIONS:
        return "risk_budget", f"no_admissible_candidate reasons={sorted(reasons)}"
    return "gates", f"no_admissible_candidate mixed_or_other reasons={sorted(reasons)}"


def _classify_row(
    *,
    entry: LayerTwoEntryExecutionDiagnosticReport,
    allocator: LayerTwoStatefulAllocatorReport,
) -> CashOccupancyRowAttribution:
    outcome = entry.outcome
    common = {
        "as_of": entry.as_of,
        "entry_execution_report_id": _require_sealed_hex64(entry.report_id, field_name="entry.report_id"),
        "allocator_report_id": _require_sealed_hex64(allocator.report_id, field_name="allocator.report_id"),
        "execution_outcome": outcome,
    }
    if outcome == "unknown_execution_observation":
        return CashOccupancyRowAttribution(
            **common,
            cause_marker="unknown",
            amount_quantified=False,
            classification_evidence="unknown_execution_observation",
        )
    if outcome == "blocked_suspension":
        target = float(entry.proposed_target_notional or 0.0)
        if entry.proposed_target_notional is None:
            raise ValueError("blocked_suspension requires proposed_target_notional")
        return CashOccupancyRowAttribution(
            **common,
            cause_marker="suspension",
            amount_quantified=True,
            known_target_cash=target,
            known_base_cash_used=0.0,
            known_retained_cash=target,
            classification_evidence="blocked_suspension",
        )
    if outcome == "blocked_limit_up":
        if entry.proposed_target_notional is None:
            raise ValueError("blocked_limit_up requires proposed_target_notional")
        target = float(entry.proposed_target_notional)
        return CashOccupancyRowAttribution(
            **common,
            cause_marker="limit_up_or_limit_down",
            amount_quantified=True,
            known_target_cash=target,
            known_base_cash_used=0.0,
            known_retained_cash=target,
            classification_evidence="blocked_limit_up_buy_side_only",
        )
    if outcome == "unaffordable_board_lot_or_minimum_commission":
        if entry.proposed_target_notional is None or entry.base_scenario is None:
            raise ValueError("unaffordable outcome requires target and base_scenario")
        target = float(entry.proposed_target_notional)
        used = float(entry.base_scenario.total_cash_used)
        retained = target - used
        if retained < -_AMOUNT_ABS_TOL:
            raise ValueError("retained cash negative for unaffordable outcome")
        if retained < 0.0:
            retained = 0.0
        return CashOccupancyRowAttribution(
            **common,
            cause_marker="unaffordable_board_lot_or_min_commission",
            amount_quantified=True,
            known_target_cash=target,
            known_base_cash_used=used,
            known_retained_cash=retained,
            classification_evidence="unaffordable_board_lot_or_minimum_commission",
        )
    if outcome == "hypothetically_fillable":
        if entry.proposed_target_notional is None or entry.base_scenario is None:
            raise ValueError("hypothetically_fillable requires target and base_scenario")
        target = float(entry.proposed_target_notional)
        used = float(entry.base_scenario.total_cash_used)
        retained = float(entry.base_scenario.unused_target_cash)
        if not _amounts_equal(target, used + retained):
            raise ValueError("fillable target/used/unused identity failed")
        if retained > _AMOUNT_ABS_TOL:
            marker: RowCauseMarker = "unaffordable_board_lot_or_min_commission"
            evidence = "hypothetically_fillable_partial_lot_residual"
        else:
            marker = "no_retained_cash"
            evidence = "hypothetically_fillable_full_target_used"
            retained = 0.0
        return CashOccupancyRowAttribution(
            **common,
            cause_marker=marker,
            amount_quantified=True,
            known_target_cash=target,
            known_base_cash_used=used,
            known_retained_cash=retained,
            classification_evidence=evidence,
        )
    if outcome == "not_attempted":
        cause, evidence = _map_not_attempted_cause(allocator)
        return CashOccupancyRowAttribution(
            **common,
            cause_marker=cause,
            amount_quantified=False,
            classification_evidence=evidence,
        )
    raise ValueError(f"unsupported execution outcome: {outcome}")


def _empty_cause_bucket() -> dict[str, Any]:
    return {
        "decision_count": 0,
        "quantified_row_count": 0,
        "unquantified_row_count": 0,
        "sum_known_target_cash": 0.0,
        "sum_known_base_cash_used": 0.0,
        "sum_known_retained_cash": 0.0,
    }


def _verify_structural_row(row: LayerTwoCashOccupancyStructuralRowInput) -> None:
    assert_entry_execution_report_self_hash(row.entry_execution_report)
    verify_layer_two_entry_execution_diagnostic_report(
        row.entry_execution_report,
        allocator_report=row.allocator_report,
        constraint_report=row.constraint_report,
        current_state=row.current_state,
        ranking=row.ranking,
        phase_report=row.phase_report,
        execution_observation=row.execution_observation,
    )


def attribute_layer_two_cash_occupancy(
    rows: Sequence[LayerTwoCashOccupancyStructuralRowInput],
) -> LayerTwoCashOccupancyAttributionReport:
    """Aggregate sealed E10e-0 diagnostics into longitudinal cash-occupancy attribution."""
    if not rows:
        raise ValueError("cash occupancy attribution requires a non-empty row sequence")

    verified_rows: list[CashOccupancyRowAttribution] = []
    snapshot: str | None = None
    phase_id: str | None = None
    protocol_id: str | None = None
    attempt = 0
    not_attempt = 0
    unknown = 0
    no_retained = 0
    buckets = {cause: _empty_cause_bucket() for cause in BOUND_OCCUPANCY_CAUSES}

    ordered = list(rows)
    as_ofs = [item.entry_execution_report.as_of for item in ordered]
    if as_ofs != sorted(as_ofs) or len(as_ofs) != len(set(as_ofs)):
        raise ValueError("input rows must be strictly increasing by unique as_of")

    for item in ordered:
        entry = item.entry_execution_report
        allocator = item.allocator_report
        _verify_structural_row(item)
        if entry.allocator_report_id != allocator.report_id:
            raise ValueError("entry allocator_report_id drift")
        if entry.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
            raise ValueError("entry tranche protocol id drift")
        if snapshot is None:
            snapshot = entry.market_data_snapshot_id
            phase_id = entry.phase_report_id
            protocol_id = entry.tranche_evaluation_protocol_id
        if entry.market_data_snapshot_id != snapshot:
            raise ValueError("all rows must share market_data_snapshot_id")
        if entry.phase_report_id != phase_id:
            raise ValueError("all rows must share phase_report_id")
        if entry.tranche_evaluation_protocol_id != protocol_id:
            raise ValueError("all rows must share tranche_evaluation_protocol_id")
        if item.phase_report.report_id != entry.phase_report_id:
            raise ValueError("phase_report.report_id must equal entry phase_report_id")

        classified = _classify_row(entry=entry, allocator=allocator)
        verified_rows.append(classified)
        if entry.outcome == "not_attempted":
            not_attempt += 1
        else:
            attempt += 1
        if classified.cause_marker == "unknown":
            unknown += 1
        elif classified.cause_marker == "no_retained_cash":
            no_retained += 1
        if classified.cause_marker in buckets:
            bucket = buckets[classified.cause_marker]
            bucket["decision_count"] += 1
            if classified.amount_quantified:
                bucket["quantified_row_count"] += 1
                bucket["sum_known_target_cash"] += float(classified.known_target_cash or 0.0)
                bucket["sum_known_base_cash_used"] += float(classified.known_base_cash_used or 0.0)
                bucket["sum_known_retained_cash"] += float(classified.known_retained_cash or 0.0)
            else:
                bucket["unquantified_row_count"] += 1
        elif classified.cause_marker == "no_retained_cash" and classified.amount_quantified:
            # Full deployment: still accumulate global known used/target via row sums below.
            pass

    cause_summaries = [
        CashOccupancyCauseSummary(
            cause=cause,
            decision_count=int(buckets[cause]["decision_count"]),
            quantified_row_count=int(buckets[cause]["quantified_row_count"]),
            unquantified_row_count=int(buckets[cause]["unquantified_row_count"]),
            sum_known_target_cash=float(buckets[cause]["sum_known_target_cash"]),
            sum_known_base_cash_used=float(buckets[cause]["sum_known_base_cash_used"]),
            sum_known_retained_cash=float(buckets[cause]["sum_known_retained_cash"]),
        )
        for cause in BOUND_OCCUPANCY_CAUSES
    ]
    global_target = sum(float(row.known_target_cash or 0.0) for row in verified_rows if row.amount_quantified)
    global_used = sum(float(row.known_base_cash_used or 0.0) for row in verified_rows if row.amount_quantified)
    global_retained = sum(float(row.known_retained_cash or 0.0) for row in verified_rows if row.amount_quantified)

    assert snapshot is not None and phase_id is not None
    assembled = LayerTwoCashOccupancyAttributionReport(
        market_data_snapshot_id=snapshot,
        phase_report_id=phase_id,
        input_entry_execution_report_ids=[
            _require_sealed_hex64(row.entry_execution_report_id, field_name="row.id") for row in verified_rows
        ],
        coverage_as_of_start=verified_rows[0].as_of,
        coverage_as_of_end=verified_rows[-1].as_of,
        row_count=len(verified_rows),
        rows=verified_rows,
        cause_summaries=cause_summaries,
        total_report_count=len(verified_rows),
        total_attempt_count=attempt,
        total_not_attempt_count=not_attempt,
        total_unknown_count=unknown,
        total_no_retained_count=no_retained,
        global_sum_known_target_cash=global_target,
        global_sum_known_base_cash_used=global_used,
        global_sum_known_retained_cash=global_retained,
    )
    return seal_layer_two_cash_occupancy_attribution_report(assembled)


def canonical_report_payload(report: LayerTwoCashOccupancyAttributionReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: LayerTwoCashOccupancyAttributionReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: LayerTwoCashOccupancyAttributionReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_layer_two_cash_occupancy_attribution_report(
    report: LayerTwoCashOccupancyAttributionReport,
) -> LayerTwoCashOccupancyAttributionReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: LayerTwoCashOccupancyAttributionReport) -> None:
    if report.report_id is None:
        raise ValueError("cash occupancy attribution report_id is missing")
    if report.report_id != compute_report_id(report):
        raise ValueError("cash occupancy attribution report_id does not match canonical content hash")


def assert_matches_recomputed_attribution(
    report: LayerTwoCashOccupancyAttributionReport,
    *,
    rows: Sequence[LayerTwoCashOccupancyStructuralRowInput],
) -> None:
    expected = attribute_layer_two_cash_occupancy(rows)
    if report.report_id != expected.report_id:
        raise ValueError("cash occupancy attribution report_id does not match full recompute")
    if canonical_report_payload(report) != canonical_report_payload(expected):
        raise ValueError("cash occupancy attribution canonical payload does not match full recompute")


def verify_layer_two_cash_occupancy_attribution_report(
    report: LayerTwoCashOccupancyAttributionReport,
    *,
    rows: Sequence[LayerTwoCashOccupancyStructuralRowInput],
) -> LayerTwoCashOccupancyAttributionVerificationResult:
    """Structural verifier: self-hash + every E10e-0 structural verify + full recompute."""
    assert_report_self_hash(report)
    assert_matches_recomputed_attribution(report, rows=rows)
    return LayerTwoCashOccupancyAttributionVerificationResult(
        report_id=report.report_id or compute_report_id(report),
        structural_ok=True,
        entry_execution_binding_ok=False,
        phase_binding_ok=False,
        tranche_evaluation_protocol_binding_ok=False,
    )


def verify_layer_two_cash_occupancy_attribution_report_file(
    *,
    report: LayerTwoCashOccupancyAttributionReport,
    rows: Sequence[LayerTwoCashOccupancyFileRowInput],
) -> LayerTwoCashOccupancyAttributionVerificationResult:
    """File verifier: structural path + real E10e-0 file verifier for every row."""
    structural_rows = [item.structural for item in rows]
    structural = verify_layer_two_cash_occupancy_attribution_report(report, rows=structural_rows)
    for item in rows:
        result = verify_layer_two_entry_execution_diagnostic_report_file(
            report=item.structural.entry_execution_report,
            allocator_report=item.structural.allocator_report,
            constraint_report=item.structural.constraint_report,
            current_state=item.structural.current_state,
            ranking=item.structural.ranking,
            phase_report=item.structural.phase_report,
            execution_observation=item.structural.execution_observation,
            eligibility_report=item.file_bindings.eligibility_report,
            financial_reports=item.file_bindings.financial_reports,
            cluster_report=item.file_bindings.cluster_report,
            store=item.file_bindings.store,
            repo_root=item.file_bindings.repo_root,
            phase_report_path=item.file_bindings.phase_report_path,
        )
        if (
            result.structural_ok is not True
            or result.allocator_binding_ok is not True
            or result.phase_binding_ok is not True
            or result.tranche_evaluation_protocol_binding_ok is not True
            or result.execution_observation_binding_ok is not True
        ):
            raise ValueError(
                "E10e-0 file verifier structural_ok/allocator/phase/protocol/observation bindings "
                "required for each occupancy row"
            )
    return structural.model_copy(
        update={
            "entry_execution_binding_ok": True,
            "phase_binding_ok": True,
            "tranche_evaluation_protocol_binding_ok": True,
        }
    )


def load_layer_two_cash_occupancy_attribution_report(path: Path) -> LayerTwoCashOccupancyAttributionReport:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("cash occupancy attribution report is missing or invalid") from exc
    try:
        return LayerTwoCashOccupancyAttributionReport.model_validate(payload)
    except Exception as exc:
        raise ValueError("cash occupancy attribution report is missing or invalid") from exc


def write_layer_two_cash_occupancy_attribution_report(
    path: Path,
    report: LayerTwoCashOccupancyAttributionReport,
) -> LayerTwoCashOccupancyAttributionReport:
    sealed = seal_layer_two_cash_occupancy_attribution_report(report)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_OCCUPANCY_CAUSES",
    "LAYER_TWO_CASH_OCCUPANCY_ENGINE_VERSION",
    "LAYER_TWO_CASH_OCCUPANCY_SCHEMA_VERSION",
    "CashOccupancyCauseSummary",
    "CashOccupancyRowAttribution",
    "LayerTwoCashOccupancyAttributionReport",
    "LayerTwoCashOccupancyAttributionVerificationResult",
    "LayerTwoCashOccupancyFileRowBindings",
    "LayerTwoCashOccupancyFileRowInput",
    "LayerTwoCashOccupancyStructuralRowInput",
    "assert_matches_recomputed_attribution",
    "assert_report_self_hash",
    "attribute_layer_two_cash_occupancy",
    "canonical_report_bytes",
    "canonical_report_payload",
    "compute_report_id",
    "load_layer_two_cash_occupancy_attribution_report",
    "seal_layer_two_cash_occupancy_attribution_report",
    "verify_layer_two_cash_occupancy_attribution_report",
    "verify_layer_two_cash_occupancy_attribution_report_file",
    "write_layer_two_cash_occupancy_attribution_report",
]
