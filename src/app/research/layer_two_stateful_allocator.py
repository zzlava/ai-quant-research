"""Layer-two stateful single-opportunity allocation diagnostic (E10d-3).

Research-only: consumes a sealed LayerTwoConstraintAssemblerReport plus an
explicit unvalidated development ranking and an explicit sealed portfolio
state. Selects at most one diagnostic entry intent for the selected phase
tranche. Does not score, rank-derive, construct production portfolios, emit
orders, or trade.
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
    _require_non_bool_int,
    _require_real_number,
    cluster_notional_cap,
)
from app.research.layer_two_candidate_eligibility import LayerTwoCandidateEligibilityReport
from app.research.layer_two_constraint_assembler import (
    BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID,
    BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH,
    LayerTwoConstraintAssemblerReport,
    LayerTwoConstraintRow,
    verify_layer_two_constraint_assembler_report,
    verify_layer_two_constraint_assembler_report_file,
)
from app.research.layer_two_constraint_assembler import (
    assert_report_self_hash as assert_constraint_report_self_hash,
)
from app.research.layer_two_financial_negative_list import LayerTwoFinancialNegativeListReport
from app.research.layer_two_statistical_risk_clusters import LayerTwoStatisticalRiskClusterReport
from app.research.layer_two_tranche_phase_schedule import LayerTwoTranchePhaseScheduleReport
from app.storage.protocol import MarketStore

LAYER_TWO_STATEFUL_ALLOCATOR_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_STATEFUL_ALLOCATOR_ENGINE_VERSION: Literal["layer-two-stateful-allocator-v1"] = (
    "layer-two-stateful-allocator-v1"
)
LAYER_TWO_PORTFOLIO_STATE_SCHEMA_VERSION: Literal["1"] = "1"

BOUND_ALLOCATION_PROTOCOL_ID = BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID
BOUND_ALLOCATION_PROTOCOL_PATH = BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_PATH

# Declared absolute tolerance for cash + position notionals vs equity.
STATE_EQUITY_ABS_TOL: float = 1e-6
_NOTIONAL_ABS_TOL = 1e-9

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

RANKING_INPUT_LABEL: Literal["unvalidated_development_ranking_input"] = "unvalidated_development_ranking_input"

PortfolioCashRetentionReason = Literal[
    "upstream_not_ready_for_stateful_allocator_input",
    "zero_risk_budget",
    "insufficient_capital_for_minimum_base_slot",
    "no_active_tranche",
    "no_selected_phase_opportunity",
    "selected_tranche_occupied",
    "preexisting_cluster_breach",
    "preexisting_sleeve_breach",
    "no_admissible_candidate",
]

CandidateRejectionReason = Literal[
    "already_held",
    "missing_constraint_row",
    "row_unusable",
    "null_target",
    "hard_excluded",
    "financial_unknown",
    "insufficient_cash",
    "sleeve_notional_cap",
    "cluster_position_cap",
    "cluster_notional_cap",
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


def _notionals_equal(left: float, right: float, *, tol: float = _NOTIONAL_ABS_TOL) -> bool:
    return abs(left - right) <= tol


def _require_sealed_hex64(value: str | None, *, field_name: str) -> str:
    if value is None or not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sealed 64-char lowercase hex digest")
    return value


class LayerTwoActiveTranchePosition(_StrictModel):
    """One active tranche slot in the explicit diagnostic portfolio state."""

    tranche_id: int
    symbol: str = Field(min_length=1)
    current_market_notional: float
    cluster_id: str = Field(min_length=1)

    @field_validator("tranche_id", mode="before")
    @classmethod
    def _tranche(cls, value: object) -> int:
        return _require_non_bool_int(value, field_name="tranche_id", minimum=0)

    @field_validator("current_market_notional", mode="before")
    @classmethod
    def _notional(cls, value: object) -> float:
        return _require_real_number(value, field_name="current_market_notional", minimum=0.0)

    @field_validator("symbol", "cluster_id", mode="before")
    @classmethod
    def _nonblank(cls, value: object, info: Any) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value.strip()


class LayerTwoStatefulPortfolioState(_StrictModel):
    """Explicit sealed current portfolio state for the same decision snapshot."""

    schema_version: Literal["1"] = LAYER_TWO_PORTFOLIO_STATE_SCHEMA_VERSION
    state_id: str | None = Field(default=None, pattern=_HEX64.pattern)
    as_of: date
    decision_at: datetime
    market_data_snapshot_id: str = Field(min_length=1)
    current_account_equity: float
    cash: float
    positions: list[LayerTwoActiveTranchePosition]
    equity_accounting_abs_tol: float = STATE_EQUITY_ABS_TOL
    diagnostic_only: Literal[True] = True
    account_identity_not_required: Literal[True] = True

    @field_validator("as_of", mode="before")
    @classmethod
    def _as_of(cls, value: object) -> date:
        return _require_date(value, field_name="as_of")

    @field_validator("decision_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, field_name="decision_at")

    @field_validator("current_account_equity", "cash", "equity_accounting_abs_tol", mode="before")
    @classmethod
    def _nonneg(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("market_data_snapshot_id", mode="before")
    @classmethod
    def _snap(cls, value: object) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("market_data_snapshot_id must be a non-empty string")
        return value.strip()

    @field_validator("diagnostic_only", "account_identity_not_required", mode="before")
    @classmethod
    def _require_literal_true(cls, value: object, info: Any) -> object:
        if value is not True:
            raise ValueError(f"{info.field_name} must be the boolean True")
        return True

    @model_validator(mode="after")
    def _validate_positions(self) -> LayerTwoStatefulPortfolioState:
        symbols = [row.symbol for row in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("positions must have unique symbols")
        tranche_ids = [row.tranche_id for row in self.positions]
        if len(tranche_ids) != len(set(tranche_ids)):
            raise ValueError("positions must have unique tranche_id")
        if tranche_ids != sorted(tranche_ids):
            raise ValueError("positions must be ordered by strictly increasing tranche_id")
        gross = sum(row.current_market_notional for row in self.positions)
        accounted = self.cash + gross
        if abs(accounted - self.current_account_equity) > self.equity_accounting_abs_tol:
            raise ValueError(
                "cash + position notionals must equal current_account_equity "
                f"within equity_accounting_abs_tol={self.equity_accounting_abs_tol}"
            )
        return self


class UnvalidatedDevelopmentRankingInput(_StrictModel):
    """Explicit ranking; allocator never derives scores or weights from it."""

    ranking_label: Literal["unvalidated_development_ranking_input"] = RANKING_INPUT_LABEL
    ranked_symbols: list[str]
    does_not_derive_scores_or_weights: Literal[True] = True

    @field_validator("ranked_symbols", mode="before")
    @classmethod
    def _symbols(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("ranked_symbols must be a list")
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str) or item.strip() == "":
                raise ValueError("ranked_symbols entries must be non-empty strings")
            cleaned.append(item.strip())
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("ranked_symbols must be duplicate-free")
        return cleaned

    @field_validator("does_not_derive_scores_or_weights", mode="before")
    @classmethod
    def _require_literal_true(cls, value: object, info: Any) -> object:
        if value is not True:
            raise ValueError(f"{info.field_name} must be the boolean True")
        return True


class ProposedDiagnosticEntry(_StrictModel):
    """Diagnostic entry intent only — not an order."""

    tranche_id: int
    symbol: str = Field(min_length=1)
    target_notional: float
    cluster_id: str = Field(min_length=1)
    ranking_position: int

    @field_validator("tranche_id", "ranking_position", mode="before")
    @classmethod
    def _ints(cls, value: object, info: Any) -> int:
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=0)

    @field_validator("target_notional", mode="before")
    @classmethod
    def _target(cls, value: object) -> float:
        return _require_real_number(value, field_name="target_notional", minimum=0.0)


class CandidateRejectionDiagnostic(_StrictModel):
    symbol: str = Field(min_length=1)
    ranking_position: int
    reason: CandidateRejectionReason

    @field_validator("ranking_position", mode="before")
    @classmethod
    def _pos(cls, value: object) -> int:
        return _require_non_bool_int(value, field_name="ranking_position", minimum=0)


class AllocationAccountingSnapshot(_StrictModel):
    current_cash: float
    current_gross_notional: float
    proposed_cash: float
    proposed_gross_notional: float

    @field_validator(
        "current_cash",
        "current_gross_notional",
        "proposed_cash",
        "proposed_gross_notional",
        mode="before",
    )
    @classmethod
    def _nums(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)


class LayerTwoStatefulAllocatorReport(_StrictModel):
    schema_version: Literal["1"] = LAYER_TWO_STATEFUL_ALLOCATOR_SCHEMA_VERSION
    engine_version: Literal["layer-two-stateful-allocator-v1"] = LAYER_TWO_STATEFUL_ALLOCATOR_ENGINE_VERSION
    report_id: str | None = Field(default=None, pattern=_HEX64.pattern)
    as_of: date
    decision_at: datetime
    market_data_snapshot_id: str = Field(min_length=1)
    constraint_assembler_report_id: str = Field(pattern=_HEX64.pattern)
    current_state_id: str = Field(pattern=_HEX64.pattern)
    phase_report_id: str = Field(pattern=_HEX64.pattern)
    allocation_implementation_protocol_id: Literal[
        "0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"
    ] = BOUND_ALLOCATION_PROTOCOL_ID
    allocation_implementation_protocol_path: Literal[
        "config/research/layer-two-allocation-implementation-protocol-v1.json"
    ] = BOUND_ALLOCATION_PROTOCOL_PATH
    ranking_label: Literal["unvalidated_development_ranking_input"] = RANKING_INPUT_LABEL
    ranked_symbols: list[str]
    selected_tranche_id: int | None
    proposed_entry: ProposedDiagnosticEntry | None
    candidate_rejection_diagnostics: list[CandidateRejectionDiagnostic]
    portfolio_cash_retention_reason: PortfolioCashRetentionReason | None
    accounting: AllocationAccountingSnapshot
    equity_accounting_abs_tol: float = STATE_EQUITY_ABS_TOL
    diagnostic_only: Literal[True] = True
    ready_for_allocation_diagnostic: Literal[True] = True
    ready_for_allocation_diagnostic_is_not_production_ready: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_derive_scores_or_weights: Literal[True] = True
    does_not_clip_or_redistribute_targets: Literal[True] = True
    does_not_emit_orders: Literal[True] = True
    does_not_trade: Literal[True] = True
    does_not_assume_fills_or_pnl: Literal[True] = True

    @field_validator("as_of", mode="before")
    @classmethod
    def _as_of(cls, value: object) -> date:
        return _require_date(value, field_name="as_of")

    @field_validator("decision_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, field_name="decision_at")

    @field_validator("selected_tranche_id", mode="before")
    @classmethod
    def _optional_tranche(cls, value: object) -> object:
        if value is None:
            return None
        return _require_non_bool_int(value, field_name="selected_tranche_id", minimum=0)

    @field_validator("equity_accounting_abs_tol", mode="before")
    @classmethod
    def _tol(cls, value: object) -> float:
        return _require_real_number(value, field_name="equity_accounting_abs_tol", minimum=0.0)

    @field_validator(
        "diagnostic_only",
        "ready_for_allocation_diagnostic",
        "ready_for_allocation_diagnostic_is_not_production_ready",
        mode="before",
    )
    @classmethod
    def _require_literal_true(cls, value: object, info: Any) -> object:
        # Reject 1/"true"/Truthiness coercion — only the boolean True is accepted.
        if value is not True:
            raise ValueError(f"{info.field_name} must be the boolean True")
        return True

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
    def _require_literal_false(cls, value: object, info: Any) -> object:
        if value is not False:
            raise ValueError(f"{info.field_name} must be the boolean False")
        return False

    @model_validator(mode="after")
    def _gate(self) -> LayerTwoStatefulAllocatorReport:
        if (
            self.ready_for_scoring
            or self.ready_for_backtest
            or self.ready_for_portfolio_construction
            or self.ready_for_orders
            or self.ready_for_trading
            or self.auto_apply
        ):
            raise ValueError("allocator cannot authorize scoring/backtest/portfolio/orders/trading")
        if not self.diagnostic_only:
            raise ValueError("diagnostic_only must remain true")
        if self.ready_for_allocation_diagnostic is not True:
            raise ValueError("ready_for_allocation_diagnostic must remain true")
        if self.proposed_entry is not None and self.portfolio_cash_retention_reason is not None:
            raise ValueError("proposed_entry and portfolio_cash_retention_reason are mutually exclusive")
        if self.proposed_entry is None and self.portfolio_cash_retention_reason is None:
            raise ValueError("cash retention reason required when no proposed_entry")
        if self.proposed_entry is not None and self.selected_tranche_id is None:
            raise ValueError("selected_tranche_id required when proposed_entry is set")
        if self.proposed_entry is not None and self.proposed_entry.tranche_id != self.selected_tranche_id:
            raise ValueError("proposed_entry.tranche_id must equal selected_tranche_id")
        return self


class LayerTwoStatefulAllocatorVerificationResult(_StrictModel):
    """Verification outcome flags.

    Structural verifier keeps disk/upstream binding flags false.
    File verifier calls the real E10d-2 file verifier (including required
    phase_report_path) before setting binding flags.
    """

    report_id: str
    structural_ok: bool
    constraint_assembler_binding_ok: bool = False
    phase_binding_ok: bool = False
    allocation_protocol_binding_ok: bool = False
    diagnostic_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_emit_orders: Literal[True] = True
    does_not_trade: Literal[True] = True


def canonical_state_payload(state: LayerTwoStatefulPortfolioState) -> dict[str, Any]:
    return state.model_dump(mode="json", exclude={"state_id"})


def canonical_state_bytes(state: LayerTwoStatefulPortfolioState) -> bytes:
    return json.dumps(
        canonical_state_payload(state),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_state_id(state: LayerTwoStatefulPortfolioState) -> str:
    return hashlib.sha256(canonical_state_bytes(state)).hexdigest()


def seal_layer_two_stateful_portfolio_state(
    state: LayerTwoStatefulPortfolioState,
) -> LayerTwoStatefulPortfolioState:
    return state.model_copy(update={"state_id": compute_state_id(state)})


def assert_state_self_hash(state: LayerTwoStatefulPortfolioState) -> None:
    if state.state_id is None:
        raise ValueError("portfolio state_id is missing")
    if state.state_id != compute_state_id(state):
        raise ValueError("portfolio state_id does not match canonical content hash")


def canonical_report_payload(report: LayerTwoStatefulAllocatorReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: LayerTwoStatefulAllocatorReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: LayerTwoStatefulAllocatorReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_layer_two_stateful_allocator_report(
    report: LayerTwoStatefulAllocatorReport,
) -> LayerTwoStatefulAllocatorReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: LayerTwoStatefulAllocatorReport) -> None:
    if report.report_id is None:
        raise ValueError("stateful allocator report_id is missing")
    if report.report_id != compute_report_id(report):
        raise ValueError("stateful allocator report_id does not match canonical content hash")


def _row_by_symbol(constraint: LayerTwoConstraintAssemblerReport) -> dict[str, LayerTwoConstraintRow]:
    mapping: dict[str, LayerTwoConstraintRow] = {}
    for row in constraint.rows:
        if row.symbol in mapping:
            raise ValueError(f"duplicate constraint row symbol: {row.symbol}")
        mapping[row.symbol] = row
    return mapping


def _validate_ranking_permutation(
    ranking: UnvalidatedDevelopmentRankingInput,
    *,
    eligible_symbols: Sequence[str],
) -> list[str]:
    if ranking.ranking_label != RANKING_INPUT_LABEL:
        raise ValueError("ranking_label must be unvalidated_development_ranking_input")
    ranked = list(ranking.ranked_symbols)
    eligible = list(eligible_symbols)
    if len(ranked) != len(eligible) or set(ranked) != set(eligible):
        raise ValueError("ranked_symbols must be an exact duplicate-free permutation of eligible_symbols")
    return ranked


def _validate_selected_phase_tranche(constraint: LayerTwoConstraintAssemblerReport) -> None:
    """Reject out-of-range selected tranche even on the structural diagnostic path."""
    active = constraint.active_tranche_count
    opportunity = constraint.selected_phase_opportunity
    if constraint.as_of_has_selected_phase_opportunity:
        if opportunity is None:
            raise ValueError("selected_phase_opportunity required when as_of_has_selected_phase_opportunity")
        if opportunity.tranche_id < 0 or opportunity.tranche_id >= active:
            raise ValueError("selected_phase_opportunity.tranche_id must be within [0, active_tranche_count)")
    elif opportunity is not None:
        raise ValueError("selected_phase_opportunity must be null when as_of has no opportunity")


def _validate_state_against_constraint(
    state: LayerTwoStatefulPortfolioState,
    *,
    constraint: LayerTwoConstraintAssemblerReport,
) -> None:
    assert_state_self_hash(state)
    if state.as_of != constraint.as_of:
        raise ValueError("current state as_of must equal constraint as_of")
    if state.decision_at != constraint.decision_at:
        raise ValueError("current state decision_at must equal constraint decision_at")
    if state.market_data_snapshot_id != constraint.market_data_snapshot_id:
        raise ValueError("current state market_data_snapshot_id must equal constraint snapshot")
    if not _notionals_equal(
        state.current_account_equity,
        constraint.current_account_equity,
        tol=STATE_EQUITY_ABS_TOL,
    ):
        raise ValueError("current state equity must equal constraint current_account_equity")
    if abs(state.equity_accounting_abs_tol - STATE_EQUITY_ABS_TOL) > _NOTIONAL_ABS_TOL:
        raise ValueError("equity_accounting_abs_tol must equal declared STATE_EQUITY_ABS_TOL")
    active = constraint.active_tranche_count
    if len(state.positions) > active:
        raise ValueError("position count must not exceed active_tranche_count")
    for position in state.positions:
        if position.tranche_id >= active:
            raise ValueError("tranche_id must be within [0, active_tranche_count)")

    rows = _row_by_symbol(constraint)
    for position in state.positions:
        row = rows.get(position.symbol)
        if row is None:
            continue
        if row.cluster_id is None:
            raise ValueError(
                f"cluster state inconsistency: held {position.symbol} has constraint row without cluster_id"
            )
        if row.cluster_id != position.cluster_id:
            raise ValueError(
                f"cluster state inconsistency: held {position.symbol} cluster_id does not match "
                "constraint row (state evidence must not be silently replaced)"
            )


def _cluster_occupancy(
    state: LayerTwoStatefulPortfolioState,
    *,
    cluster_id: str,
) -> tuple[int, float]:
    count = 0
    notional = 0.0
    for position in state.positions:
        if position.cluster_id == cluster_id:
            count += 1
            notional += position.current_market_notional
    return count, notional


def _any_preexisting_cluster_breach(
    state: LayerTwoStatefulPortfolioState,
    *,
    global_cluster_cap: float,
) -> bool:
    """True if any cluster already breaches max-2 or the global 35% sleeve cap.

    Includes positions whose symbols are outside the current eligible set.
    Cap is the protocol/constraint sleeve-derived global cluster cap, not a
    per-candidate row field.
    """
    by_cluster: dict[str, list[LayerTwoActiveTranchePosition]] = {}
    for position in state.positions:
        by_cluster.setdefault(position.cluster_id, []).append(position)
    for members in by_cluster.values():
        if len(members) > CONFIRMED_CLUSTER_MAX_POSITIONS:
            return True
        notional = sum(row.current_market_notional for row in members)
        if notional > global_cluster_cap + _NOTIONAL_ABS_TOL:
            return True
    return False


def _portfolio_gate_reason(
    constraint: LayerTwoConstraintAssemblerReport,
    state: LayerTwoStatefulPortfolioState,
    *,
    current_gross: float,
    global_cluster_cap: float,
) -> PortfolioCashRetentionReason | None:
    if not constraint.ready_for_stateful_allocator_input:
        return "upstream_not_ready_for_stateful_allocator_input"
    if constraint.base_slot_count == 0 or constraint.active_tranche_count == 0:
        upstream = constraint.portfolio_cash_retention_reason
        if upstream in ("zero_risk_budget", "insufficient_capital_for_minimum_base_slot", "no_active_tranche"):
            return upstream
        return "no_active_tranche"
    if not constraint.as_of_has_selected_phase_opportunity or constraint.selected_phase_opportunity is None:
        return "no_selected_phase_opportunity"
    if _any_preexisting_cluster_breach(state, global_cluster_cap=global_cluster_cap):
        return "preexisting_cluster_breach"
    if current_gross > constraint.sleeve_budget + _NOTIONAL_ABS_TOL:
        return "preexisting_sleeve_breach"
    selected = constraint.selected_phase_opportunity.tranche_id
    if any(position.tranche_id == selected for position in state.positions):
        return "selected_tranche_occupied"
    return None


def _reject_candidate(
    *,
    symbol: str,
    ranking_position: int,
    row: LayerTwoConstraintRow | None,
    state: LayerTwoStatefulPortfolioState,
    held_symbols: set[str],
    current_gross: float,
    sleeve_budget: float,
    global_cluster_cap: float,
) -> CandidateRejectionReason | None:
    if symbol in held_symbols:
        return "already_held"
    if row is None:
        return "missing_constraint_row"
    if row.hard_excluded or row.financial_decision_status == "hard_excluded":
        return "hard_excluded"
    if row.financial_decision_status == "insufficient_evidence" or row.financial_multiplier == "unknown":
        return "financial_unknown"
    if not row.usable_for_later_allocator:
        return "row_unusable"
    if row.target_for_later_allocator is None:
        return "null_target"
    if row.cluster_id is None:
        return "row_unusable"
    target = float(row.target_for_later_allocator)
    if target > state.cash + _NOTIONAL_ABS_TOL:
        return "insufficient_cash"
    # Sleeve risk budget is independent of account cash outside the sleeve.
    if current_gross + target > sleeve_budget + _NOTIONAL_ABS_TOL:
        return "sleeve_notional_cap"
    count, cluster_notional = _cluster_occupancy(state, cluster_id=row.cluster_id)
    max_positions = int(row.cluster_max_positions)
    if max_positions != CONFIRMED_CLUSTER_MAX_POSITIONS:
        return "row_unusable"
    if count >= max_positions:
        return "cluster_position_cap"
    if cluster_notional + target > global_cluster_cap + _NOTIONAL_ABS_TOL:
        return "cluster_notional_cap"
    return None


def allocate_layer_two_stateful_single_opportunity(
    *,
    constraint_report: LayerTwoConstraintAssemblerReport,
    current_state: LayerTwoStatefulPortfolioState,
    ranking: UnvalidatedDevelopmentRankingInput,
) -> LayerTwoStatefulAllocatorReport:
    """Produce a sealed single-opportunity allocation diagnostic.

    Never derives scores/weights, never clips targets, never redistributes
    released capital, never fills another tranche, and never emits orders.
    Caller must supply an already-sealed portfolio state (state_id set).
    """
    assert_constraint_report_self_hash(constraint_report)
    if constraint_report.allocation_implementation_protocol_id != BOUND_ALLOCATION_PROTOCOL_ID:
        raise ValueError("allocation protocol id drift on constraint report")
    if current_state.state_id is None:
        raise ValueError("portfolio state_id is missing; caller must seal state explicitly")
    assert_state_self_hash(current_state)

    ranked = _validate_ranking_permutation(ranking, eligible_symbols=constraint_report.eligible_symbols)
    _validate_selected_phase_tranche(constraint_report)
    _validate_state_against_constraint(current_state, constraint=constraint_report)

    current_gross = sum(position.current_market_notional for position in current_state.positions)
    held_symbols = {position.symbol for position in current_state.positions}
    rows = _row_by_symbol(constraint_report)
    global_cluster_cap = cluster_notional_cap(sleeve_budget=constraint_report.sleeve_budget)

    selected_tranche_id: int | None = None
    if constraint_report.selected_phase_opportunity is not None:
        selected_tranche_id = constraint_report.selected_phase_opportunity.tranche_id

    gate = _portfolio_gate_reason(
        constraint_report,
        current_state,
        current_gross=current_gross,
        global_cluster_cap=global_cluster_cap,
    )
    diagnostics: list[CandidateRejectionDiagnostic] = []
    proposed: ProposedDiagnosticEntry | None = None
    cash_reason: PortfolioCashRetentionReason | None = gate

    if gate is None:
        assert selected_tranche_id is not None
        for position_index, symbol in enumerate(ranked):
            reason = _reject_candidate(
                symbol=symbol,
                ranking_position=position_index,
                row=rows.get(symbol),
                state=current_state,
                held_symbols=held_symbols,
                current_gross=current_gross,
                sleeve_budget=float(constraint_report.sleeve_budget),
                global_cluster_cap=global_cluster_cap,
            )
            if reason is not None:
                diagnostics.append(
                    CandidateRejectionDiagnostic(
                        symbol=symbol,
                        ranking_position=position_index,
                        reason=reason,
                    )
                )
                continue
            row = rows[symbol]
            assert row.target_for_later_allocator is not None
            assert row.cluster_id is not None
            proposed = ProposedDiagnosticEntry(
                tranche_id=selected_tranche_id,
                symbol=symbol,
                target_notional=float(row.target_for_later_allocator),
                cluster_id=row.cluster_id,
                ranking_position=position_index,
            )
            break
        if proposed is None:
            cash_reason = "no_admissible_candidate"

    if proposed is not None:
        proposed_cash = current_state.cash - proposed.target_notional
        proposed_gross = current_gross + proposed.target_notional
        if proposed_cash < -_NOTIONAL_ABS_TOL:
            raise ValueError("proposed target exceeds current cash (internal invariant)")
        if proposed_gross > constraint_report.sleeve_budget + _NOTIONAL_ABS_TOL:
            raise ValueError("proposed gross exceeds sleeve_budget (internal invariant)")
        if proposed_cash < 0.0:
            proposed_cash = 0.0
    else:
        proposed_cash = current_state.cash
        proposed_gross = current_gross

    assembled = LayerTwoStatefulAllocatorReport(
        as_of=constraint_report.as_of,
        decision_at=constraint_report.decision_at,
        market_data_snapshot_id=constraint_report.market_data_snapshot_id,
        constraint_assembler_report_id=_require_sealed_hex64(
            constraint_report.report_id, field_name="constraint_assembler_report_id"
        ),
        current_state_id=_require_sealed_hex64(current_state.state_id, field_name="current_state_id"),
        phase_report_id=_require_sealed_hex64(constraint_report.phase_report_id, field_name="phase_report_id"),
        ranked_symbols=ranked,
        # Preserve selected tranche id whenever an opportunity exists (auditability),
        # including occupied-tranche / no-admissible cash-retention paths.
        selected_tranche_id=selected_tranche_id,
        proposed_entry=proposed,
        candidate_rejection_diagnostics=diagnostics,
        portfolio_cash_retention_reason=cash_reason,
        accounting=AllocationAccountingSnapshot(
            current_cash=current_state.cash,
            current_gross_notional=current_gross,
            proposed_cash=proposed_cash,
            proposed_gross_notional=proposed_gross,
        ),
    )
    return seal_layer_two_stateful_allocator_report(assembled)


def assert_matches_recomputed_allocation(
    report: LayerTwoStatefulAllocatorReport,
    *,
    constraint_report: LayerTwoConstraintAssemblerReport,
    current_state: LayerTwoStatefulPortfolioState,
    ranking: UnvalidatedDevelopmentRankingInput,
) -> None:
    expected = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint_report,
        current_state=current_state,
        ranking=ranking,
    )
    if report.report_id != expected.report_id:
        raise ValueError("stateful allocator report_id does not match full recompute")
    if canonical_report_payload(report) != canonical_report_payload(expected):
        raise ValueError("stateful allocator canonical payload does not match full recompute")


def verify_layer_two_stateful_allocator_report(
    report: LayerTwoStatefulAllocatorReport,
    *,
    constraint_report: LayerTwoConstraintAssemblerReport,
    current_state: LayerTwoStatefulPortfolioState,
    ranking: UnvalidatedDevelopmentRankingInput,
) -> LayerTwoStatefulAllocatorVerificationResult:
    """Structural verifier: self-hash + full allocation recompute.

    Does not claim E10d-2 disk bindings; binding flags remain false.
    """
    assert_report_self_hash(report)
    assert_constraint_report_self_hash(constraint_report)
    assert_state_self_hash(current_state)
    assert_matches_recomputed_allocation(
        report,
        constraint_report=constraint_report,
        current_state=current_state,
        ranking=ranking,
    )
    if report.constraint_assembler_report_id != constraint_report.report_id:
        raise ValueError("allocator constraint_assembler_report_id drift")
    if report.current_state_id != current_state.state_id:
        raise ValueError("allocator current_state_id drift")
    if report.phase_report_id != constraint_report.phase_report_id:
        raise ValueError("allocator phase_report_id drift")
    if report.allocation_implementation_protocol_id != BOUND_ALLOCATION_PROTOCOL_ID:
        raise ValueError("allocation protocol id drift")
    return LayerTwoStatefulAllocatorVerificationResult(
        report_id=report.report_id or compute_report_id(report),
        structural_ok=True,
        constraint_assembler_binding_ok=False,
        phase_binding_ok=False,
        allocation_protocol_binding_ok=False,
    )


def verify_layer_two_stateful_allocator_report_file(
    *,
    report: LayerTwoStatefulAllocatorReport,
    constraint_report: LayerTwoConstraintAssemblerReport,
    current_state: LayerTwoStatefulPortfolioState,
    ranking: UnvalidatedDevelopmentRankingInput,
    eligibility_report: LayerTwoCandidateEligibilityReport,
    financial_reports: Sequence[LayerTwoFinancialNegativeListReport],
    cluster_report: LayerTwoStatisticalRiskClusterReport,
    phase_report: LayerTwoTranchePhaseScheduleReport,
    store: MarketStore,
    repo_root: Path,
    phase_report_path: Path,
) -> LayerTwoStatefulAllocatorVerificationResult:
    """File verifier: structural path + real E10d-2 file verifier (disk-bound phase)."""
    root = Path(repo_root).resolve()
    structural = verify_layer_two_stateful_allocator_report(
        report,
        constraint_report=constraint_report,
        current_state=current_state,
        ranking=ranking,
    )
    assembler_result = verify_layer_two_constraint_assembler_report_file(
        report=constraint_report,
        eligibility_report=eligibility_report,
        financial_reports=financial_reports,
        cluster_report=cluster_report,
        phase_report=phase_report,
        store=store,
        repo_root=root,
        phase_report_path=phase_report_path,
    )
    if not assembler_result.structural_ok:
        raise ValueError("constraint assembler structural verification failed")
    if not assembler_result.phase_binding_ok:
        raise ValueError("constraint assembler phase_binding_ok required for allocator file verify")
    if not assembler_result.allocation_protocol_binding_ok:
        raise ValueError("constraint assembler allocation_protocol_binding_ok required")
    return structural.model_copy(
        update={
            "constraint_assembler_binding_ok": True,
            "phase_binding_ok": True,
            "allocation_protocol_binding_ok": True,
        }
    )


def verify_layer_two_stateful_allocator_report_with_constraint_structural(
    report: LayerTwoStatefulAllocatorReport,
    *,
    constraint_report: LayerTwoConstraintAssemblerReport,
    current_state: LayerTwoStatefulPortfolioState,
    ranking: UnvalidatedDevelopmentRankingInput,
    eligibility_report: LayerTwoCandidateEligibilityReport,
    financial_reports: Sequence[LayerTwoFinancialNegativeListReport],
    cluster_report: LayerTwoStatisticalRiskClusterReport,
    phase_report: LayerTwoTranchePhaseScheduleReport,
    store: MarketStore,
    repo_root: Path,
) -> LayerTwoStatefulAllocatorVerificationResult:
    """Optional structural path that also runs E10d-2 structural verify.

    Still does **not** claim disk phase binding (binding flags remain false).
    """
    root = Path(repo_root).resolve()
    structural = verify_layer_two_stateful_allocator_report(
        report,
        constraint_report=constraint_report,
        current_state=current_state,
        ranking=ranking,
    )
    verify_layer_two_constraint_assembler_report(
        constraint_report,
        eligibility_report=eligibility_report,
        financial_reports=financial_reports,
        cluster_report=cluster_report,
        phase_report=phase_report,
        store=store,
        repo_root=root,
    )
    return structural


def load_layer_two_stateful_allocator_report(path: Path) -> LayerTwoStatefulAllocatorReport:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("stateful allocator report is missing or invalid") from exc
    try:
        return LayerTwoStatefulAllocatorReport.model_validate(payload)
    except Exception as exc:
        raise ValueError("stateful allocator report is missing or invalid") from exc


def write_layer_two_stateful_allocator_report(
    path: Path,
    report: LayerTwoStatefulAllocatorReport,
) -> LayerTwoStatefulAllocatorReport:
    sealed = seal_layer_two_stateful_allocator_report(report)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_ALLOCATION_PROTOCOL_ID",
    "BOUND_ALLOCATION_PROTOCOL_PATH",
    "LAYER_TWO_PORTFOLIO_STATE_SCHEMA_VERSION",
    "LAYER_TWO_STATEFUL_ALLOCATOR_ENGINE_VERSION",
    "LAYER_TWO_STATEFUL_ALLOCATOR_SCHEMA_VERSION",
    "RANKING_INPUT_LABEL",
    "STATE_EQUITY_ABS_TOL",
    "AllocationAccountingSnapshot",
    "CandidateRejectionDiagnostic",
    "LayerTwoActiveTranchePosition",
    "LayerTwoStatefulAllocatorReport",
    "LayerTwoStatefulAllocatorVerificationResult",
    "LayerTwoStatefulPortfolioState",
    "ProposedDiagnosticEntry",
    "UnvalidatedDevelopmentRankingInput",
    "allocate_layer_two_stateful_single_opportunity",
    "assert_matches_recomputed_allocation",
    "assert_report_self_hash",
    "assert_state_self_hash",
    "canonical_report_bytes",
    "canonical_report_payload",
    "canonical_state_bytes",
    "canonical_state_payload",
    "compute_report_id",
    "compute_state_id",
    "load_layer_two_stateful_allocator_report",
    "seal_layer_two_stateful_allocator_report",
    "seal_layer_two_stateful_portfolio_state",
    "verify_layer_two_stateful_allocator_report",
    "verify_layer_two_stateful_allocator_report_file",
    "verify_layer_two_stateful_allocator_report_with_constraint_structural",
    "write_layer_two_stateful_allocator_report",
]
