"""Layer-two constraint assembler (E10d-2).

Read-only assembly of E10a/E10b/E10c/E10d-0/E10d-1 evidence into sealed
constraint rows for a later stateful allocator. Does not rank, select stocks,
construct portfolios, compute orders, or trade.

Callers must supply already-built upstream reports plus a sealed MarketStore
(for cluster recompute) and repo_root. Verifiers re-run real upstream verifiers
and refuse to trust ready flags alone.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_allocation_protocol import (
    CONFIRMED_CLUSTER_MAX_POSITIONS,
    DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH,
    _require_non_bool_int,
    _require_real_number,
    cluster_notional_cap,
    plan_base_slots,
    plan_final_target_notional,
    verify_layer_two_allocation_protocol_file,
)
from app.research.layer_two_candidate_eligibility import (
    LayerTwoCandidateEligibilityReport,
    verify_layer_two_candidate_eligibility_report,
)
from app.research.layer_two_financial_negative_list import (
    LayerTwoFinancialNegativeListReport,
    verify_layer_two_financial_negative_list_report,
)
from app.research.layer_two_statistical_risk_clusters import (
    LayerTwoStatisticalRiskClusterReport,
    verify_layer_two_statistical_risk_cluster_report,
)
from app.research.layer_two_tranche_phase_schedule import (
    LayerTwoTranchePhaseScheduleReport,
    ScheduledOpportunity,
    verify_layer_two_tranche_phase_schedule_report,
    verify_layer_two_tranche_phase_schedule_report_file,
)
from app.research.layer_two_tranche_phase_schedule import (
    canonical_report_payload as canonical_phase_report_payload,
)
from app.storage.protocol import MarketStore

LAYER_TWO_CONSTRAINT_ASSEMBLER_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_CONSTRAINT_ASSEMBLER_ENGINE_VERSION: Literal["layer-two-constraint-assembler-v1"] = (
    "layer-two-constraint-assembler-v1"
)

BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID: Literal[
    "0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"
] = "0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"
BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH: Literal[
    "config/research/layer-two-allocation-implementation-protocol-v1.json"
] = "config/research/layer-two-allocation-implementation-protocol-v1.json"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NOTIONAL_ABS_TOL = 1e-9
_BUDGET_ABS_TOL = 1e-12

CashRetentionReason = Literal[
    "zero_risk_budget",
    "insufficient_capital_for_minimum_base_slot",
    "financial_hard_exclude",
    "financial_unknown",
    "cluster_report_unknown_or_incomplete",
    "cluster_single_name_exceeds_cap",
    "no_active_tranche",
    "risk_multiplier_released_capital",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_aware_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _require_date(value: object, *, field_name: str) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str) and value.strip():
        return date.fromisoformat(value.strip())
    raise ValueError(f"{field_name} must be a datetime.date")


def _notionals_equal(left: float, right: float) -> bool:
    return abs(left - right) <= _NOTIONAL_ABS_TOL


def _require_sealed_report_id(value: str | None, *, field_name: str) -> str:
    if value is None or not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sealed 64-char lowercase hex report_id")
    return value


class LayerTwoConstraintRow(_StrictModel):
    symbol: str = Field(min_length=1)
    size_multiplier: float
    financial_decision_status: Literal["clean", "halved", "hard_excluded", "insufficient_evidence"]
    financial_multiplier: float | Literal["unknown"]
    financial_report_id: str = Field(pattern=_HEX64.pattern)
    financial_data_snapshot_id: str = Field(min_length=1)
    base_slot_notional: float | None
    final_target_notional: float | None
    target_for_later_allocator: float | None
    retain_cash: bool
    cash_retention_reason: CashRetentionReason | None = None
    hard_excluded: bool
    cluster_id: str | None
    cluster_sleeve_cap_notional: float | None
    cluster_max_positions: Literal[2] = CONFIRMED_CLUSTER_MAX_POSITIONS
    cluster_single_name_admissible: bool | None
    usable_for_later_allocator: bool

    @field_validator("size_multiplier", mode="before")
    @classmethod
    def _size(cls, value: object) -> float:
        return _require_real_number(value, field_name="size_multiplier", minimum=0.0, minimum_exclusive=True)

    @field_validator("base_slot_notional", "final_target_notional", "target_for_later_allocator", mode="before")
    @classmethod
    def _optional_notional(cls, value: object, info: Any) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("financial_multiplier", mode="before")
    @classmethod
    def _fin_mult(cls, value: object) -> object:
        if value == "unknown":
            return value
        return _require_real_number(value, field_name="financial_multiplier", minimum=0.0)

    @field_validator("cluster_sleeve_cap_notional", mode="before")
    @classmethod
    def _cluster_cap(cls, value: object) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name="cluster_sleeve_cap_notional", minimum=0.0)


class LayerTwoConstraintAssemblerReport(_StrictModel):
    schema_version: Literal["1"] = LAYER_TWO_CONSTRAINT_ASSEMBLER_SCHEMA_VERSION
    engine_version: Literal["layer-two-constraint-assembler-v1"] = LAYER_TWO_CONSTRAINT_ASSEMBLER_ENGINE_VERSION
    report_id: str | None = Field(default=None, pattern=_HEX64.pattern)
    as_of: date
    decision_at: datetime
    market_data_snapshot_id: str = Field(min_length=1)
    eligibility_report_id: str = Field(pattern=_HEX64.pattern)
    cluster_report_id: str = Field(pattern=_HEX64.pattern)
    phase_report_id: str = Field(pattern=_HEX64.pattern)
    allocation_implementation_protocol_id: Literal[
        "0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"
    ] = BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID
    allocation_implementation_protocol_path: Literal[
        "config/research/layer-two-allocation-implementation-protocol-v1.json"
    ] = BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH
    financial_report_ids_by_symbol: dict[str, str]
    financial_snapshot_ids_by_symbol: dict[str, str]
    current_account_equity: float
    risk_budget: float
    sleeve_budget: float
    base_slot_count: int
    base_slot_notional: float | None
    active_tranche_count: int
    eligible_symbols: list[str]
    as_of_has_selected_phase_opportunity: bool
    selected_phase_opportunity: ScheduledOpportunity | None = None
    cluster_constraints_complete: bool
    cluster_is_not_industry_classification: Literal[True] = True
    portfolio_cash_retention_reason: CashRetentionReason | None = None
    rows: list[LayerTwoConstraintRow]
    diagnostic_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_rank_or_select_stocks: Literal[True] = True
    does_not_construct_portfolio: Literal[True] = True
    does_not_compute_orders: Literal[True] = True
    does_not_trade: Literal[True] = True
    ready_for_stateful_allocator_input: bool
    ready_for_stateful_allocator_input_is_not_tradable: Literal[True] = True

    @field_validator("current_account_equity", "risk_budget", "sleeve_budget", mode="before")
    @classmethod
    def _nonneg(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("base_slot_notional", mode="before")
    @classmethod
    def _optional_base(cls, value: object) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name="base_slot_notional", minimum=0.0)

    @field_validator("base_slot_count", "active_tranche_count", mode="before")
    @classmethod
    def _counts(cls, value: object, info: Any) -> int:
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=0)

    @field_validator("decision_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, field_name="decision_at")

    @field_validator("allocation_implementation_protocol_path", mode="before")
    @classmethod
    def _path(cls, value: object) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("allocation path must be non-empty")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("allocation path must be repo-relative without parent traversal")
        if value != BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH:
            raise ValueError("allocation path does not match bound constant")
        return value

    @model_validator(mode="after")
    def _gate(self) -> LayerTwoConstraintAssemblerReport:
        if (
            self.ready_for_scoring
            or self.ready_for_backtest
            or self.ready_for_portfolio_construction
            or self.ready_for_orders
            or self.ready_for_trading
            or self.auto_apply
        ):
            raise ValueError("assembler cannot authorize scoring/backtest/portfolio/orders/trading")
        if not self.diagnostic_only:
            raise ValueError("diagnostic_only must remain true")
        if self.active_tranche_count != self.base_slot_count:
            raise ValueError("active_tranche_count must equal base_slot_count")
        if self.eligible_symbols != sorted(self.eligible_symbols):
            raise ValueError("eligible_symbols must be sorted")
        if len(set(self.eligible_symbols)) != len(self.eligible_symbols):
            raise ValueError("eligible_symbols must be unique")
        row_symbols = [row.symbol for row in self.rows]
        if row_symbols != sorted(row_symbols):
            raise ValueError("rows must be sorted by symbol")
        if self.ready_for_stateful_allocator_input and not self.cluster_constraints_complete:
            raise ValueError("ready_for_stateful_allocator_input requires complete cluster constraints")
        if self.ready_for_stateful_allocator_input:
            for row in self.rows:
                if not row.usable_for_later_allocator and row.target_for_later_allocator is not None:
                    raise ValueError("unusable row cannot carry target_for_later_allocator")
        if self.as_of_has_selected_phase_opportunity:
            if self.selected_phase_opportunity is None:
                raise ValueError("selected_phase_opportunity required when as_of has opportunity")
            if self.selected_phase_opportunity.decision_date != self.as_of:
                raise ValueError("selected_phase_opportunity.decision_date must equal as_of")
        elif self.selected_phase_opportunity is not None:
            raise ValueError("selected_phase_opportunity must be null when as_of has no opportunity")
        return self


class LayerTwoConstraintAssemblerVerificationResult(_StrictModel):
    """Verification outcome flags.

    On the structural verifier, upstream *_binding_ok stay false.
    On the file verifier:
    - eligibility/financial/cluster_binding_ok: in-memory upstream verifier full
      recompute plus their disk contract binding (via repo_root), not a per-report
      disk file for those reports
    - phase_binding_ok: requires a sealed phase report file on disk whose
      report_id and canonical payload match the in-memory phase_report
    - allocation_protocol_binding_ok: allocation protocol JSON on disk
    """

    report_id: str
    structural_ok: bool
    eligibility_binding_ok: bool = False
    financial_binding_ok: bool = False
    cluster_binding_ok: bool = False
    phase_binding_ok: bool = False
    allocation_protocol_binding_ok: bool = False
    diagnostic_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_rank_or_select_stocks: Literal[True] = True
    does_not_construct_portfolio: Literal[True] = True
    does_not_trade: Literal[True] = True


def _extract_eligible_ordered(
    eligibility: LayerTwoCandidateEligibilityReport,
) -> list[tuple[str, Any]]:
    eligible: list[tuple[str, Any]] = []
    for evaluation in eligibility.evaluations:
        if evaluation.unknown_critical_input:
            continue
        if evaluation.eligible_for_new_entry is not True:
            continue
        if evaluation.size_multiplier is None:
            raise ValueError(f"eligible symbol {evaluation.symbol} missing size_multiplier")
        eligible.append((evaluation.symbol, evaluation))
    symbols = [symbol for symbol, _ in eligible]
    if symbols != sorted(symbols):
        raise ValueError("eligible evaluations must already be sorted by symbol")
    return eligible


def _financial_multiplier_from_report(
    report: LayerTwoFinancialNegativeListReport,
) -> tuple[float | Literal["unknown"], bool, CashRetentionReason | None]:
    if report.decision_status == "hard_excluded":
        return 0.0, True, "financial_hard_exclude"
    if report.decision_status == "insufficient_evidence":
        return "unknown", False, "financial_unknown"
    if report.decision_status == "halved":
        if report.target_multiplier != 0.5:
            raise ValueError("halved decision must have target_multiplier=0.5")
        return 0.5, False, None
    if report.decision_status == "clean":
        if report.target_multiplier != 1.0:
            raise ValueError("clean decision must have target_multiplier=1.0")
        return 1.0, False, None
    raise ValueError(f"unsupported financial decision_status: {report.decision_status}")


def _build_symbol_cluster_map(
    cluster: LayerTwoStatisticalRiskClusterReport,
    *,
    eligible_symbols: Sequence[str],
) -> tuple[dict[str, str], bool]:
    mapping: dict[str, str] = {}
    complete = (
        cluster.ready_for_cluster_constraints is True
        and not cluster.diagnostic.unresolved_symbols
        and not cluster.diagnostic.unresolved_pairs
    )
    for group in cluster.diagnostic.clusters:
        for symbol in group.symbols:
            if symbol in mapping:
                return {}, False
            mapping[symbol] = group.cluster_id
    if not complete:
        return mapping, False
    if set(mapping) != set(eligible_symbols):
        return mapping, False
    if any(symbol not in mapping for symbol in eligible_symbols):
        return mapping, False
    return mapping, True


def _selected_opportunity_on_as_of(
    phase: LayerTwoTranchePhaseScheduleReport,
    *,
    as_of: date,
) -> ScheduledOpportunity | None:
    matches = [row for row in phase.selected_schedule.opportunities if row.decision_date == as_of]
    if len(matches) > 1:
        raise ValueError("multiple selected opportunities on as_of violate unique phase offsets")
    return matches[0] if matches else None


def assemble_layer_two_constraints(
    *,
    eligibility_report: LayerTwoCandidateEligibilityReport,
    financial_reports: Sequence[LayerTwoFinancialNegativeListReport],
    cluster_report: LayerTwoStatisticalRiskClusterReport,
    phase_report: LayerTwoTranchePhaseScheduleReport,
    store: MarketStore,
    repo_root: Path,
) -> LayerTwoConstraintAssemblerReport:
    """Assemble sealed constraint rows after verifying all upstream inputs."""
    root = Path(repo_root).resolve()

    eligibility = verify_layer_two_candidate_eligibility_report(eligibility_report, repo_root=root)
    verified_financials = [
        verify_layer_two_financial_negative_list_report(financial_input, repo_root=root)
        for financial_input in financial_reports
    ]
    cluster = verify_layer_two_statistical_risk_cluster_report(cluster_report, store=store, repo_root=root)
    verify_layer_two_tranche_phase_schedule_report(phase_report)
    phase = phase_report

    # Timing binding.
    as_of = eligibility.as_of
    decision_at = eligibility.decision_at
    if cluster.as_of != as_of or cluster.decision_at != decision_at:
        raise ValueError("cluster as_of/decision_at must equal eligibility as_of/decision_at")
    for financial_report in verified_financials:
        if financial_report.as_of != as_of or financial_report.decision_at != decision_at:
            raise ValueError("financial as_of/decision_at must equal eligibility as_of/decision_at")
    if not (phase.start <= as_of <= phase.end):
        raise ValueError("phase decision window must contain as_of")
    if as_of not in phase.market_calendar:
        raise ValueError("as_of must appear in phase market_calendar")

    # Market snapshot binding (financial snapshots stay independent).
    market_snap = eligibility.data_snapshot_id
    if cluster.data_snapshot_id != market_snap:
        raise ValueError("cluster data_snapshot_id must equal eligibility data_snapshot_id")
    if phase.market_data_snapshot_id != market_snap:
        raise ValueError("phase market_data_snapshot_id must equal eligibility data_snapshot_id")
    if store.snapshot().snapshot_id != market_snap:
        raise ValueError("store snapshot_id must equal market_data_snapshot_id")

    # Capital / phase base-slot binding.
    slot_plan = plan_base_slots(
        current_account_equity=phase.current_account_equity,
        risk_budget=phase.risk_budget,
    )
    if (
        not _notionals_equal(slot_plan.sleeve_budget, phase.base_slot.sleeve_budget)
        or slot_plan.base_slot_count != phase.base_slot.base_slot_count
        or slot_plan.budget_slot_cap != phase.base_slot.budget_slot_cap
    ):
        raise ValueError("phase.base_slot must equal allocation plan_base_slots(equity, risk_budget)")
    if slot_plan.base_slot_notional is None:
        if phase.base_slot.base_slot_notional is not None:
            raise ValueError("phase.base_slot_notional must be null when plan yields null")
    elif phase.base_slot.base_slot_notional is None or not _notionals_equal(
        slot_plan.base_slot_notional,
        phase.base_slot.base_slot_notional,
    ):
        raise ValueError("phase.base_slot_notional must equal plan_base_slots notional")
    if phase.active_tranche_count != slot_plan.base_slot_count:
        raise ValueError("phase.active_tranche_count must equal base_slot_count")

    eligible_pairs = _extract_eligible_ordered(eligibility)
    eligible_symbols = [symbol for symbol, _ in eligible_pairs]
    if list(cluster.candidates) != eligible_symbols:
        raise ValueError("cluster.candidates must equal eligible_for_new_entry symbol set exactly")

    financial_by_symbol: dict[str, LayerTwoFinancialNegativeListReport] = {}
    for financial_report in verified_financials:
        if financial_report.symbol in financial_by_symbol:
            raise ValueError(f"duplicate financial report for symbol {financial_report.symbol}")
        financial_by_symbol[financial_report.symbol] = financial_report
    if set(financial_by_symbol) != set(eligible_symbols):
        raise ValueError("financial reports must be a 1:1 map over eligible symbols (no missing/extra)")

    # Planned buy must equal pre-multiplier base when slots exist.
    if slot_plan.base_slot_count > 0:
        assert slot_plan.base_slot_notional is not None
        for symbol, evaluation in eligible_pairs:
            planned = evaluation.candidate_input.planned_buy_notional_cny
            if not _notionals_equal(planned, slot_plan.base_slot_notional):
                raise ValueError(
                    f"eligible {symbol} planned_buy_notional_cny must equal base_slot_notional "
                    f"({slot_plan.base_slot_notional})"
                )

    symbol_to_cluster, cluster_complete = _build_symbol_cluster_map(cluster, eligible_symbols=eligible_symbols)
    sleeve_cap = cluster_notional_cap(sleeve_budget=slot_plan.sleeve_budget)
    opportunity = _selected_opportunity_on_as_of(phase, as_of=as_of)

    rows: list[LayerTwoConstraintRow] = []
    portfolio_cash_reason: CashRetentionReason | None = None
    if slot_plan.base_slot_count == 0:
        slot_reason = slot_plan.cash_retention_reason
        if slot_reason in ("zero_risk_budget", "insufficient_capital_for_minimum_base_slot", "no_active_tranche"):
            portfolio_cash_reason = slot_reason
        else:
            portfolio_cash_reason = "no_active_tranche"
    elif not cluster_complete:
        portfolio_cash_reason = "cluster_report_unknown_or_incomplete"

    usable_inputs = slot_plan.base_slot_count > 0 and cluster_complete and slot_plan.base_slot_notional is not None

    if usable_inputs:
        assert slot_plan.base_slot_notional is not None
        for symbol, evaluation in eligible_pairs:
            financial = financial_by_symbol[symbol]
            fin_mult, hard_excluded, fin_reason = _financial_multiplier_from_report(financial)
            size = float(evaluation.size_multiplier)
            cluster_id = symbol_to_cluster[symbol]
            final_target: float | None = None
            retain_cash = False
            cash_reason: CashRetentionReason | None = None
            hard = hard_excluded
            if hard_excluded:
                retain_cash = True
                cash_reason = "financial_hard_exclude"
            elif fin_mult == "unknown":
                retain_cash = True
                cash_reason = "financial_unknown"
            else:
                planned = plan_final_target_notional(
                    base_slot_notional=slot_plan.base_slot_notional,
                    size_multiplier=size,
                    financial_multiplier=fin_mult,
                )
                final_target = planned.final_target_notional
                if planned.cash_retention_reason == "risk_multiplier_released_capital":
                    cash_reason = "risk_multiplier_released_capital"
            admissible: bool | None = None
            target_for_allocator: float | None = None
            row_usable = False
            if final_target is not None:
                admissible = final_target <= sleeve_cap + _NOTIONAL_ABS_TOL
                if admissible:
                    target_for_allocator = final_target
                    row_usable = True
                else:
                    retain_cash = True
                    cash_reason = "cluster_single_name_exceeds_cap"
                    target_for_allocator = None
                    row_usable = False
            rows.append(
                LayerTwoConstraintRow(
                    symbol=symbol,
                    size_multiplier=size,
                    financial_decision_status=financial.decision_status,
                    financial_multiplier=fin_mult,
                    financial_report_id=_require_sealed_report_id(
                        financial.report_id, field_name="financial.report_id"
                    ),
                    financial_data_snapshot_id=financial.data_snapshot_id,
                    base_slot_notional=slot_plan.base_slot_notional,
                    final_target_notional=final_target,
                    target_for_later_allocator=target_for_allocator,
                    retain_cash=retain_cash,
                    cash_retention_reason=cash_reason or fin_reason,
                    hard_excluded=hard,
                    cluster_id=cluster_id,
                    cluster_sleeve_cap_notional=sleeve_cap,
                    cluster_single_name_admissible=admissible,
                    usable_for_later_allocator=row_usable,
                )
            )
    elif slot_plan.base_slot_count > 0 and not cluster_complete:
        # Emit diagnostic rows without usable allocator targets.
        assert slot_plan.base_slot_notional is not None
        for symbol, evaluation in eligible_pairs:
            financial = financial_by_symbol[symbol]
            fin_mult, hard_excluded, fin_reason = _financial_multiplier_from_report(financial)
            rows.append(
                LayerTwoConstraintRow(
                    symbol=symbol,
                    size_multiplier=float(evaluation.size_multiplier),
                    financial_decision_status=financial.decision_status,
                    financial_multiplier=fin_mult,
                    financial_report_id=_require_sealed_report_id(
                        financial.report_id, field_name="financial.report_id"
                    ),
                    financial_data_snapshot_id=financial.data_snapshot_id,
                    base_slot_notional=slot_plan.base_slot_notional,
                    final_target_notional=None,
                    target_for_later_allocator=None,
                    retain_cash=True,
                    cash_retention_reason="cluster_report_unknown_or_incomplete",
                    hard_excluded=hard_excluded,
                    cluster_id=symbol_to_cluster.get(symbol),
                    cluster_sleeve_cap_notional=sleeve_cap,
                    cluster_single_name_admissible=None,
                    usable_for_later_allocator=False,
                )
            )

    ready_for_allocator = usable_inputs and all(
        (not row.usable_for_later_allocator) or row.target_for_later_allocator is not None for row in rows
    )
    # N=0 with complete cluster binding is a valid "all cash" allocator input.
    if slot_plan.base_slot_count == 0 and cluster_complete and list(cluster.candidates) == eligible_symbols:
        ready_for_allocator = True
        rows = []

    assembled = LayerTwoConstraintAssemblerReport(
        as_of=as_of,
        decision_at=decision_at,
        market_data_snapshot_id=market_snap,
        eligibility_report_id=_require_sealed_report_id(eligibility.report_id, field_name="eligibility.report_id"),
        cluster_report_id=_require_sealed_report_id(cluster.report_id, field_name="cluster.report_id"),
        phase_report_id=_require_sealed_report_id(phase.report_id, field_name="phase.report_id"),
        financial_report_ids_by_symbol={
            symbol: _require_sealed_report_id(
                financial_by_symbol[symbol].report_id, field_name=f"financial[{symbol}].report_id"
            )
            for symbol in eligible_symbols
        },
        financial_snapshot_ids_by_symbol={
            symbol: financial_by_symbol[symbol].data_snapshot_id for symbol in eligible_symbols
        },
        current_account_equity=phase.current_account_equity,
        risk_budget=phase.risk_budget,
        sleeve_budget=slot_plan.sleeve_budget,
        base_slot_count=slot_plan.base_slot_count,
        base_slot_notional=slot_plan.base_slot_notional,
        active_tranche_count=slot_plan.base_slot_count,
        eligible_symbols=eligible_symbols,
        as_of_has_selected_phase_opportunity=opportunity is not None,
        selected_phase_opportunity=opportunity,
        cluster_constraints_complete=cluster_complete,
        portfolio_cash_retention_reason=portfolio_cash_reason,
        rows=rows,
        ready_for_stateful_allocator_input=ready_for_allocator,
    )
    return seal_layer_two_constraint_assembler_report(assembled)


def canonical_report_payload(report: LayerTwoConstraintAssemblerReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: LayerTwoConstraintAssemblerReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: LayerTwoConstraintAssemblerReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_layer_two_constraint_assembler_report(
    report: LayerTwoConstraintAssemblerReport,
) -> LayerTwoConstraintAssemblerReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: LayerTwoConstraintAssemblerReport) -> None:
    if report.report_id is None:
        raise ValueError("constraint assembler report_id is missing")
    if report.report_id != compute_report_id(report):
        raise ValueError("constraint assembler report_id does not match canonical content hash")


def assert_matches_recomputed_assembly(
    report: LayerTwoConstraintAssemblerReport,
    *,
    eligibility_report: LayerTwoCandidateEligibilityReport,
    financial_reports: Sequence[LayerTwoFinancialNegativeListReport],
    cluster_report: LayerTwoStatisticalRiskClusterReport,
    phase_report: LayerTwoTranchePhaseScheduleReport,
    store: MarketStore,
    repo_root: Path,
) -> None:
    expected = assemble_layer_two_constraints(
        eligibility_report=eligibility_report,
        financial_reports=financial_reports,
        cluster_report=cluster_report,
        phase_report=phase_report,
        store=store,
        repo_root=repo_root,
    )
    if report.report_id != expected.report_id:
        raise ValueError("constraint assembler report_id does not match full recompute")
    if canonical_report_payload(report) != canonical_report_payload(expected):
        raise ValueError("constraint assembler canonical payload does not match full recompute")


def verify_layer_two_constraint_assembler_report(
    report: LayerTwoConstraintAssemblerReport,
    *,
    eligibility_report: LayerTwoCandidateEligibilityReport,
    financial_reports: Sequence[LayerTwoFinancialNegativeListReport],
    cluster_report: LayerTwoStatisticalRiskClusterReport,
    phase_report: LayerTwoTranchePhaseScheduleReport,
    store: MarketStore,
    repo_root: Path,
) -> LayerTwoConstraintAssemblerVerificationResult:
    """Structural verifier: self-hash + upstream verify + full recompute.

    Does not claim disk file bindings; all *_binding_ok remain false.
    """
    root = Path(repo_root).resolve()
    assert_report_self_hash(report)
    verify_layer_two_candidate_eligibility_report(eligibility_report, repo_root=root)
    for financial in financial_reports:
        verify_layer_two_financial_negative_list_report(financial, repo_root=root)
    verify_layer_two_statistical_risk_cluster_report(cluster_report, store=store, repo_root=root)
    verify_layer_two_tranche_phase_schedule_report(phase_report)
    assert_matches_recomputed_assembly(
        report,
        eligibility_report=eligibility_report,
        financial_reports=financial_reports,
        cluster_report=cluster_report,
        phase_report=phase_report,
        store=store,
        repo_root=root,
    )
    if report.allocation_implementation_protocol_id != BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID:
        raise ValueError("allocation protocol id drift")
    return LayerTwoConstraintAssemblerVerificationResult(
        report_id=report.report_id or compute_report_id(report),
        structural_ok=True,
        eligibility_binding_ok=False,
        financial_binding_ok=False,
        cluster_binding_ok=False,
        phase_binding_ok=False,
        allocation_protocol_binding_ok=False,
    )


def verify_layer_two_constraint_assembler_report_file(
    *,
    report: LayerTwoConstraintAssemblerReport,
    eligibility_report: LayerTwoCandidateEligibilityReport,
    financial_reports: Sequence[LayerTwoFinancialNegativeListReport],
    cluster_report: LayerTwoStatisticalRiskClusterReport,
    phase_report: LayerTwoTranchePhaseScheduleReport,
    store: MarketStore,
    repo_root: Path,
    phase_report_path: Path,
) -> LayerTwoConstraintAssemblerVerificationResult:
    """File verifier: structural path + disk-bound phase/allocation protocols.

    eligibility/financial/cluster flags mean their real verifiers (full recompute
    and disk contract binding via repo_root) succeeded — those reports need not
    be loaded from separate paths here. phase_binding_ok requires
    ``phase_report_path`` to exist and pass
    ``verify_layer_two_tranche_phase_schedule_report_file``, with loaded
    report_id and canonical payload identical to ``phase_report``.
    """
    root = Path(repo_root).resolve()
    structural = verify_layer_two_constraint_assembler_report(
        report,
        eligibility_report=eligibility_report,
        financial_reports=financial_reports,
        cluster_report=cluster_report,
        phase_report=phase_report,
        store=store,
        repo_root=root,
    )
    # In-memory upstream verifiers (full recompute + disk contract via repo_root).
    verify_layer_two_candidate_eligibility_report(eligibility_report, repo_root=root)
    for financial in financial_reports:
        verify_layer_two_financial_negative_list_report(financial, repo_root=root)
    verify_layer_two_statistical_risk_cluster_report(cluster_report, store=store, repo_root=root)

    phase_path = Path(phase_report_path)
    if not phase_path.is_file():
        raise ValueError(f"phase report file missing: {phase_path}")
    loaded_phase, _phase_result = verify_layer_two_tranche_phase_schedule_report_file(
        report_path=phase_path,
        repo_root=root,
    )
    if loaded_phase.report_id != phase_report.report_id:
        raise ValueError("phase report file id does not match assembled phase report")
    if canonical_phase_report_payload(loaded_phase) != canonical_phase_report_payload(phase_report):
        raise ValueError("phase report file canonical payload does not match assembled phase report")

    allocation_path = root / BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH
    _allocation, allocation_result = verify_layer_two_allocation_protocol_file(
        protocol_path=allocation_path,
        repo_root=root,
    )
    if allocation_result.protocol_id != report.allocation_implementation_protocol_id:
        raise ValueError("allocation protocol_id on disk does not match assembler binding")
    if str(DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH) != BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH:
        raise ValueError("allocation default path drifted")

    return structural.model_copy(
        update={
            "eligibility_binding_ok": True,
            "financial_binding_ok": True,
            "cluster_binding_ok": True,
            "phase_binding_ok": True,
            "allocation_protocol_binding_ok": True,
        }
    )


def load_layer_two_constraint_assembler_report(path: Path) -> LayerTwoConstraintAssemblerReport:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("constraint assembler report is missing or invalid") from exc
    try:
        return LayerTwoConstraintAssemblerReport.model_validate(payload)
    except Exception as exc:
        raise ValueError("constraint assembler report is missing or invalid") from exc


def write_layer_two_constraint_assembler_report(
    path: Path,
    report: LayerTwoConstraintAssemblerReport,
) -> LayerTwoConstraintAssemblerReport:
    sealed = seal_layer_two_constraint_assembler_report(report)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID",
    "BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH",
    "LAYER_TWO_CONSTRAINT_ASSEMBLER_ENGINE_VERSION",
    "LAYER_TWO_CONSTRAINT_ASSEMBLER_SCHEMA_VERSION",
    "LayerTwoConstraintAssemblerReport",
    "LayerTwoConstraintAssemblerVerificationResult",
    "LayerTwoConstraintRow",
    "assemble_layer_two_constraints",
    "assert_matches_recomputed_assembly",
    "assert_report_self_hash",
    "canonical_report_bytes",
    "canonical_report_payload",
    "compute_report_id",
    "load_layer_two_constraint_assembler_report",
    "seal_layer_two_constraint_assembler_report",
    "verify_layer_two_constraint_assembler_report",
    "verify_layer_two_constraint_assembler_report_file",
    "write_layer_two_constraint_assembler_report",
]
