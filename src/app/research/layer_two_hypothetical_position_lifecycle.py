"""Layer-two hypothetical open position lifecycle record (E10f-1).

Research-only lift of one fully verified E10e-0 ``hypothetically_fillable`` base
scenario into a sealed open holding record for a later fixed 20-market-day exit
diagnostic (E10f-2). Does not mutate or impersonate E10d-3 portfolio state, does
not emit orders, and does not claim a live fill.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_allocation_protocol import _require_non_bool_int, _require_real_number
from app.research.layer_two_candidate_eligibility import LayerTwoCandidateEligibilityReport
from app.research.layer_two_constraint_assembler import LayerTwoConstraintAssemblerReport
from app.research.layer_two_entry_execution_diagnostic import (
    BOUND_BOARD_LOT_SIZE,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
    LayerTwoEntryExecutionDiagnosticReport,
    LayerTwoEntryExecutionObservation,
    verify_layer_two_entry_execution_diagnostic_report,
    verify_layer_two_entry_execution_diagnostic_report_file,
)
from app.research.layer_two_entry_execution_diagnostic import (
    assert_report_self_hash as assert_entry_report_self_hash,
)
from app.research.layer_two_financial_negative_list import LayerTwoFinancialNegativeListReport
from app.research.layer_two_stateful_allocator import (
    LayerTwoStatefulAllocatorReport,
    LayerTwoStatefulPortfolioState,
    UnvalidatedDevelopmentRankingInput,
)
from app.research.layer_two_statistical_risk_clusters import LayerTwoStatisticalRiskClusterReport
from app.research.layer_two_tranche_phase_schedule import LayerTwoTranchePhaseScheduleReport
from app.storage.protocol import MarketStore

LAYER_TWO_HYPOTHETICAL_LIFECYCLE_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_HYPOTHETICAL_LIFECYCLE_ENGINE_VERSION: Literal["layer-two-hypothetical-position-lifecycle-v1"] = (
    "layer-two-hypothetical-position-lifecycle-v1"
)

BOUND_HOLDING_MARKET_BARS_ELAPSED_AT_OPEN: Literal[1] = 1
BOUND_BOARD_LOT: Literal[100] = BOUND_BOARD_LOT_SIZE

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CASH_ABS_TOL = 1e-9

LifecycleStatus = Literal["hypothetical_open"]


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


def _require_sealed_hex64(value: str | None, *, field_name: str) -> str:
    if value is None or not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sealed 64-char lowercase hex digest")
    return value


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
        raise ValueError(f"{field_name} must be a strict bool")
    return value


def _amounts_equal(left: float, right: float, *, abs_tol: float = _CASH_ABS_TOL) -> bool:
    return abs(float(left) - float(right)) <= abs_tol


@dataclass(frozen=True, slots=True)
class LayerTwoHypotheticalLifecycleStructuralInput:
    """Exact upstreams required to structurally verify one E10e-0 fillable diagnostic."""

    entry_execution_report: LayerTwoEntryExecutionDiagnosticReport
    allocator_report: LayerTwoStatefulAllocatorReport
    constraint_report: LayerTwoConstraintAssemblerReport
    current_state: LayerTwoStatefulPortfolioState
    ranking: UnvalidatedDevelopmentRankingInput
    phase_report: LayerTwoTranchePhaseScheduleReport
    execution_observation: LayerTwoEntryExecutionObservation


@dataclass(frozen=True, slots=True)
class LayerTwoHypotheticalLifecycleFileBindings:
    """Disk-bound upstreams required to file-verify via real E10e-0 file verifier."""

    eligibility_report: LayerTwoCandidateEligibilityReport
    financial_reports: tuple[LayerTwoFinancialNegativeListReport, ...]
    cluster_report: LayerTwoStatisticalRiskClusterReport
    store: MarketStore
    repo_root: Path
    phase_report_path: Path


@dataclass(frozen=True, slots=True)
class LayerTwoHypotheticalLifecycleFileInput:
    structural: LayerTwoHypotheticalLifecycleStructuralInput
    file_bindings: LayerTwoHypotheticalLifecycleFileBindings


class LayerTwoHypotheticalPositionLifecycleRecord(_StrictModel):
    """Sealed research-only hypothetical open position; input for E10f-2 exit diagnostic."""

    schema_version: Literal["1"] = LAYER_TWO_HYPOTHETICAL_LIFECYCLE_SCHEMA_VERSION
    engine_version: Literal["layer-two-hypothetical-position-lifecycle-v1"] = (
        LAYER_TWO_HYPOTHETICAL_LIFECYCLE_ENGINE_VERSION
    )
    record_id: str | None = Field(default=None, pattern=_HEX64.pattern)

    as_of: date
    decision_at: datetime
    market_data_snapshot_id: str = Field(min_length=1)

    entry_execution_report_id: str = Field(pattern=_HEX64.pattern)
    allocator_report_id: str = Field(pattern=_HEX64.pattern)
    constraint_assembler_report_id: str = Field(pattern=_HEX64.pattern)
    phase_report_id: str = Field(pattern=_HEX64.pattern)
    current_state_id: str = Field(pattern=_HEX64.pattern)
    allocation_implementation_protocol_id: str = Field(pattern=_HEX64.pattern)
    tranche_evaluation_protocol_id: Literal["8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_ID
    )
    tranche_evaluation_protocol_path: Literal["config/research/tranche-evaluation-protocol-draft-v1.json"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
    )

    symbol: str = Field(min_length=1)
    tranche_id: int
    cluster_id: str = Field(min_length=1)
    ranking_position: int

    entry_trade_date: date
    shares: int
    board_lots: int
    board_lot_size: Literal[100] = BOUND_BOARD_LOT
    hypothetical_entry_price: float
    stock_notional: float
    buy_commission: float
    entry_total_cash_used: float
    target_notional: float
    unused_target_cash: float
    entry_cost_basis_total: float
    entry_cost_basis_per_share: float

    holding_market_bars_elapsed: Literal[1] = BOUND_HOLDING_MARKET_BARS_ELAPSED_AT_OPEN
    entry_market_day_counts_as_holding_bar_one: Literal[True] = True
    lifecycle_status: LifecycleStatus = "hypothetical_open"

    research_only: Literal[True] = True
    hypothetical_not_fill: Literal[True] = True
    diagnostic_only: Literal[True] = True
    post_decision_label_only: Literal[True] = True
    does_not_claim_order_or_live_fill: Literal[True] = True
    does_not_mutate_or_impersonate_stateful_portfolio_state: Literal[True] = True
    does_not_invent_mark_or_pnl: Literal[True] = True
    stress_scenario_not_used_for_open_record: Literal[True] = True
    input_record_for_e10f2_exit_diagnostic_only: Literal[True] = True
    # Record body is structural-only; only VerificationResult from the file verifier may be ready.
    ready_for_lifecycle_diagnostic: Literal[False] = False
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator("as_of", "entry_trade_date", mode="before")
    @classmethod
    def _dates(cls, value: object, info: Any) -> date:
        return _require_date(value, field_name=str(info.field_name))

    @field_validator("decision_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, field_name="decision_at")

    @field_validator("tranche_id", "ranking_position", "shares", "board_lots", mode="before")
    @classmethod
    def _ints(cls, value: object, info: Any) -> int:
        minimum = 0 if info.field_name in ("tranche_id", "ranking_position") else 1
        return _require_non_bool_int(value, field_name=str(info.field_name), minimum=minimum)

    @field_validator(
        "hypothetical_entry_price",
        "stock_notional",
        "buy_commission",
        "entry_total_cash_used",
        "target_notional",
        "unused_target_cash",
        "entry_cost_basis_total",
        "entry_cost_basis_per_share",
        mode="before",
    )
    @classmethod
    def _amounts(cls, value: object, info: Any) -> float:
        minimum = 0.0
        exclusive = info.field_name in (
            "hypothetical_entry_price",
            "stock_notional",
            "entry_total_cash_used",
            "target_notional",
            "entry_cost_basis_total",
            "entry_cost_basis_per_share",
        )
        return _require_real_number(
            value,
            field_name=str(info.field_name),
            minimum=minimum,
            minimum_exclusive=exclusive,
        )

    @field_validator("symbol", "cluster_id", "market_data_snapshot_id", mode="before")
    @classmethod
    def _nonblank(cls, value: object, info: Any) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value.strip()

    @field_validator(
        "research_only",
        "hypothetical_not_fill",
        "diagnostic_only",
        "post_decision_label_only",
        "does_not_claim_order_or_live_fill",
        "does_not_mutate_or_impersonate_stateful_portfolio_state",
        "does_not_invent_mark_or_pnl",
        "stress_scenario_not_used_for_open_record",
        "input_record_for_e10f2_exit_diagnostic_only",
        "entry_market_day_counts_as_holding_bar_one",
        mode="before",
    )
    @classmethod
    def _true(cls, value: object, info: Any) -> object:
        return _require_literal_true(value, field_name=str(info.field_name))

    @field_validator(
        "ready_for_lifecycle_diagnostic",
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
    def _identities(self) -> LayerTwoHypotheticalPositionLifecycleRecord:
        if self.holding_market_bars_elapsed != BOUND_HOLDING_MARKET_BARS_ELAPSED_AT_OPEN:
            raise ValueError("holding_market_bars_elapsed must be 1 at open (entry market day = bar 1)")
        if self.shares % BOUND_BOARD_LOT != 0:
            raise ValueError("shares must be an exact multiple of board lot 100")
        if self.board_lots * BOUND_BOARD_LOT != self.shares:
            raise ValueError("board_lots * 100 must equal shares")
        if self.shares <= 0 or self.board_lots <= 0:
            raise ValueError("shares and board_lots must be positive")
        if not _amounts_equal(self.entry_cost_basis_total, self.entry_total_cash_used):
            raise ValueError("entry_cost_basis_total must equal entry_total_cash_used")
        expected_per_share = self.entry_cost_basis_total / float(self.shares)
        if not _amounts_equal(self.entry_cost_basis_per_share, expected_per_share):
            raise ValueError("entry_cost_basis_per_share must equal entry_cost_basis_total / shares")
        if not _amounts_equal(self.unused_target_cash, self.target_notional - self.entry_total_cash_used):
            raise ValueError("unused_target_cash must equal target_notional - entry_total_cash_used")
        if self.entry_total_cash_used > self.target_notional + _CASH_ABS_TOL:
            raise ValueError("entry_total_cash_used must not exceed target_notional")
        if not _amounts_equal(self.stock_notional + self.buy_commission, self.entry_total_cash_used):
            raise ValueError("stock_notional + buy_commission must equal entry_total_cash_used")
        return self


class LayerTwoHypotheticalPositionLifecycleVerificationResult(_StrictModel):
    record_id: str = Field(pattern=_HEX64.pattern)
    structural_ok: bool
    entry_execution_binding_ok: bool
    allocator_binding_ok: bool
    phase_binding_ok: bool
    tranche_evaluation_protocol_binding_ok: bool
    ready_for_lifecycle_diagnostic: bool
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator(
        "structural_ok",
        "entry_execution_binding_ok",
        "allocator_binding_ok",
        "phase_binding_ok",
        "tranche_evaluation_protocol_binding_ok",
        "ready_for_lifecycle_diagnostic",
        mode="before",
    )
    @classmethod
    def _bools(cls, value: object, info: Any) -> bool:
        return _require_strict_bool(value, field_name=str(info.field_name))

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
    def _binding_ready_state_machine(self) -> LayerTwoHypotheticalPositionLifecycleVerificationResult:
        bindings = (
            self.entry_execution_binding_ok,
            self.allocator_binding_ok,
            self.phase_binding_ok,
            self.tranche_evaluation_protocol_binding_ok,
        )
        any_bound = any(bindings)
        all_bound = all(bindings)

        if self.structural_ok is not True:
            if any_bound or self.ready_for_lifecycle_diagnostic:
                raise ValueError("structural_ok=false forbids any disk binding or ready_for_lifecycle_diagnostic")
            return self

        # structural_ok=true: only structural-path or full file-path shapes are legal.
        if self.ready_for_lifecycle_diagnostic is True:
            if not all_bound:
                raise ValueError(
                    "ready_for_lifecycle_diagnostic=true requires structural_ok and all four bindings true"
                )
            return self

        if any_bound:
            if not all_bound:
                raise ValueError("partial disk bindings are forbidden")
            raise ValueError("all four bindings true requires ready_for_lifecycle_diagnostic=true (file-path shape)")
        return self


def _verify_e10e0_structural(inputs: LayerTwoHypotheticalLifecycleStructuralInput) -> None:
    assert_entry_report_self_hash(inputs.entry_execution_report)
    verify_layer_two_entry_execution_diagnostic_report(
        inputs.entry_execution_report,
        allocator_report=inputs.allocator_report,
        constraint_report=inputs.constraint_report,
        current_state=inputs.current_state,
        ranking=inputs.ranking,
        phase_report=inputs.phase_report,
        execution_observation=inputs.execution_observation,
    )


def _require_fillable_base_scenario(
    entry: LayerTwoEntryExecutionDiagnosticReport,
    *,
    observation: LayerTwoEntryExecutionObservation,
    allocator: LayerTwoStatefulAllocatorReport,
) -> tuple[Any, Any]:
    """Return (proposed_entry, base_scenario) after fail-closed fillable checks."""
    if entry.outcome != "hypothetically_fillable":
        raise ValueError(
            "hypothetical lifecycle open requires entry_report.outcome == hypothetically_fillable "
            f"(got {entry.outcome!r})"
        )
    if entry.base_scenario is None:
        raise ValueError("hypothetically_fillable entry report must carry base_scenario")
    base = entry.base_scenario
    if base.scenario_label != "base_5bps":
        raise ValueError("open record must use base_5bps scenario only (stress must not masquerade as base)")
    if base.can_afford_one_lot is not True:
        raise ValueError("base_scenario.can_afford_one_lot must be true")
    if base.affordable_shares <= 0:
        raise ValueError("base_scenario.affordable_shares must be positive")
    if base.affordable_shares % BOUND_BOARD_LOT != 0:
        raise ValueError("base_scenario.affordable_shares must be an exact board lot multiple of 100")
    if allocator.proposed_entry is None:
        raise ValueError("allocator proposed_entry required for fillable lifecycle open")
    proposed = allocator.proposed_entry
    target = (
        float(entry.proposed_target_notional)
        if entry.proposed_target_notional is not None
        else float(proposed.target_notional)
    )
    if entry.proposed_target_notional is None:
        raise ValueError("fillable entry report requires proposed_target_notional")
    if not _amounts_equal(target, float(proposed.target_notional)):
        raise ValueError("entry proposed_target_notional must equal allocator proposed_entry.target_notional")
    if base.total_cash_used > target + _CASH_ABS_TOL:
        raise ValueError("base_scenario.total_cash_used must not exceed target_notional")
    if observation.published_up_limit is None:
        raise ValueError("tradable fillable observation requires published_up_limit")
    if base.hypothetical_fill_price > float(observation.published_up_limit) + _CASH_ABS_TOL:
        raise ValueError("hypothetical_fill_price must be <= published_up_limit")
    if entry.expected_t1_execution_date != observation.execution_date:
        raise ValueError("entry_trade_date requires expected_t1_execution_date == observation.execution_date")
    if entry.proposed_symbol != proposed.symbol or entry.proposed_symbol != observation.symbol:
        raise ValueError("symbol must agree across entry report, proposed_entry, and observation")
    # Explicitly ignore stress_scenario for open-record amounts.
    _ = entry.stress_scenario
    return proposed, base


def open_layer_two_hypothetical_position_lifecycle(
    *,
    entry_execution_report: LayerTwoEntryExecutionDiagnosticReport,
    allocator_report: LayerTwoStatefulAllocatorReport,
    constraint_report: LayerTwoConstraintAssemblerReport,
    current_state: LayerTwoStatefulPortfolioState,
    ranking: UnvalidatedDevelopmentRankingInput,
    phase_report: LayerTwoTranchePhaseScheduleReport,
    execution_observation: LayerTwoEntryExecutionObservation,
) -> LayerTwoHypotheticalPositionLifecycleRecord:
    """Lift a verified fillable E10e-0 base scenario into a sealed open lifecycle record."""
    structural = LayerTwoHypotheticalLifecycleStructuralInput(
        entry_execution_report=entry_execution_report,
        allocator_report=allocator_report,
        constraint_report=constraint_report,
        current_state=current_state,
        ranking=ranking,
        phase_report=phase_report,
        execution_observation=execution_observation,
    )
    _verify_e10e0_structural(structural)
    proposed, base = _require_fillable_base_scenario(
        entry_execution_report,
        observation=execution_observation,
        allocator=allocator_report,
    )

    if entry_execution_report.proposed_target_notional is None:
        raise ValueError("fillable entry report requires proposed_target_notional")
    shares = int(base.affordable_shares)
    board_lots = int(base.affordable_lots)
    target_notional = float(entry_execution_report.proposed_target_notional)
    entry_total_cash_used = float(base.total_cash_used)
    unused_target_cash = float(base.unused_target_cash)
    if not _amounts_equal(unused_target_cash, target_notional - entry_total_cash_used):
        raise ValueError("base unused_target_cash must equal target_notional - total_cash_used")

    assembled = LayerTwoHypotheticalPositionLifecycleRecord(
        as_of=entry_execution_report.as_of,
        decision_at=entry_execution_report.decision_at,
        market_data_snapshot_id=entry_execution_report.market_data_snapshot_id,
        entry_execution_report_id=_require_sealed_hex64(
            entry_execution_report.report_id, field_name="entry_execution_report_id"
        ),
        allocator_report_id=_require_sealed_hex64(allocator_report.report_id, field_name="allocator_report_id"),
        constraint_assembler_report_id=_require_sealed_hex64(
            constraint_report.report_id, field_name="constraint_assembler_report_id"
        ),
        phase_report_id=_require_sealed_hex64(phase_report.report_id, field_name="phase_report_id"),
        current_state_id=_require_sealed_hex64(current_state.state_id, field_name="current_state_id"),
        allocation_implementation_protocol_id=allocator_report.allocation_implementation_protocol_id,
        tranche_evaluation_protocol_id=entry_execution_report.tranche_evaluation_protocol_id,
        tranche_evaluation_protocol_path=entry_execution_report.tranche_evaluation_protocol_path,
        symbol=proposed.symbol,
        tranche_id=int(proposed.tranche_id),
        cluster_id=proposed.cluster_id,
        ranking_position=int(proposed.ranking_position),
        entry_trade_date=entry_execution_report.expected_t1_execution_date,
        shares=shares,
        board_lots=board_lots,
        hypothetical_entry_price=float(base.hypothetical_fill_price),
        stock_notional=float(base.stock_notional),
        buy_commission=float(base.commission),
        entry_total_cash_used=entry_total_cash_used,
        target_notional=target_notional,
        unused_target_cash=unused_target_cash,
        entry_cost_basis_total=entry_total_cash_used,
        entry_cost_basis_per_share=entry_total_cash_used / float(shares),
        holding_market_bars_elapsed=BOUND_HOLDING_MARKET_BARS_ELAPSED_AT_OPEN,
    )
    return seal_layer_two_hypothetical_position_lifecycle_record(assembled)


def canonical_record_payload(record: LayerTwoHypotheticalPositionLifecycleRecord) -> dict[str, Any]:
    return record.model_dump(mode="json", exclude={"record_id"})


def canonical_record_bytes(record: LayerTwoHypotheticalPositionLifecycleRecord) -> bytes:
    return json.dumps(
        canonical_record_payload(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_record_id(record: LayerTwoHypotheticalPositionLifecycleRecord) -> str:
    return hashlib.sha256(canonical_record_bytes(record)).hexdigest()


def seal_layer_two_hypothetical_position_lifecycle_record(
    record: LayerTwoHypotheticalPositionLifecycleRecord,
) -> LayerTwoHypotheticalPositionLifecycleRecord:
    return record.model_copy(update={"record_id": compute_record_id(record)})


def assert_record_self_hash(record: LayerTwoHypotheticalPositionLifecycleRecord) -> None:
    if record.record_id is None:
        raise ValueError("hypothetical lifecycle record_id is missing")
    if record.record_id != compute_record_id(record):
        raise ValueError("hypothetical lifecycle record_id does not match canonical content hash")


def assert_matches_recomputed_lifecycle_record(
    record: LayerTwoHypotheticalPositionLifecycleRecord,
    *,
    structural: LayerTwoHypotheticalLifecycleStructuralInput,
) -> None:
    expected = open_layer_two_hypothetical_position_lifecycle(
        entry_execution_report=structural.entry_execution_report,
        allocator_report=structural.allocator_report,
        constraint_report=structural.constraint_report,
        current_state=structural.current_state,
        ranking=structural.ranking,
        phase_report=structural.phase_report,
        execution_observation=structural.execution_observation,
    )
    if record.record_id != expected.record_id:
        raise ValueError("hypothetical lifecycle record_id does not match full recompute")
    if canonical_record_payload(record) != canonical_record_payload(expected):
        raise ValueError("hypothetical lifecycle canonical payload does not match full recompute")


def verify_layer_two_hypothetical_position_lifecycle_record(
    record: LayerTwoHypotheticalPositionLifecycleRecord,
    *,
    structural: LayerTwoHypotheticalLifecycleStructuralInput,
) -> LayerTwoHypotheticalPositionLifecycleVerificationResult:
    """Structural verifier: self-hash + real E10e-0 structural verify + full recompute.

    Does not claim entry-execution / allocator / phase / protocol disk bindings.
    """
    assert_record_self_hash(record)
    _verify_e10e0_structural(structural)
    assert_matches_recomputed_lifecycle_record(record, structural=structural)
    # Newly constructed: never trust caller-supplied binding/ready booleans.
    return LayerTwoHypotheticalPositionLifecycleVerificationResult(
        record_id=record.record_id or compute_record_id(record),
        structural_ok=True,
        entry_execution_binding_ok=False,
        allocator_binding_ok=False,
        phase_binding_ok=False,
        tranche_evaluation_protocol_binding_ok=False,
        ready_for_lifecycle_diagnostic=False,
    )


def verify_layer_two_hypothetical_position_lifecycle_record_file(
    *,
    record: LayerTwoHypotheticalPositionLifecycleRecord,
    file_input: LayerTwoHypotheticalLifecycleFileInput,
) -> LayerTwoHypotheticalPositionLifecycleVerificationResult:
    """File verifier: structural path + real E10e-0 file verifier; then new-construct ready flags."""
    structural_result = verify_layer_two_hypothetical_position_lifecycle_record(
        record,
        structural=file_input.structural,
    )
    if structural_result.structural_ok is not True:
        raise ValueError("structural verifier must succeed before file binding")
    if (
        structural_result.entry_execution_binding_ok
        or structural_result.allocator_binding_ok
        or structural_result.phase_binding_ok
        or structural_result.tranche_evaluation_protocol_binding_ok
        or structural_result.ready_for_lifecycle_diagnostic
    ):
        raise ValueError("structural verifier must not claim disk binding or lifecycle readiness")

    e10e0 = verify_layer_two_entry_execution_diagnostic_report_file(
        report=file_input.structural.entry_execution_report,
        allocator_report=file_input.structural.allocator_report,
        constraint_report=file_input.structural.constraint_report,
        current_state=file_input.structural.current_state,
        ranking=file_input.structural.ranking,
        phase_report=file_input.structural.phase_report,
        execution_observation=file_input.structural.execution_observation,
        eligibility_report=file_input.file_bindings.eligibility_report,
        financial_reports=file_input.file_bindings.financial_reports,
        cluster_report=file_input.file_bindings.cluster_report,
        store=file_input.file_bindings.store,
        repo_root=file_input.file_bindings.repo_root,
        phase_report_path=file_input.file_bindings.phase_report_path,
    )
    if (
        e10e0.structural_ok is not True
        or e10e0.allocator_binding_ok is not True
        or e10e0.phase_binding_ok is not True
        or e10e0.tranche_evaluation_protocol_binding_ok is not True
        or e10e0.execution_observation_binding_ok is not True
    ):
        raise ValueError(
            "E10e-0 file verifier structural_ok/allocator/phase/protocol/observation bindings "
            "required for lifecycle file path"
        )
    if e10e0.report_id != record.entry_execution_report_id:
        raise ValueError("E10e-0 file verifier report_id must equal record.entry_execution_report_id")

    # Newly constructed readiness — do not model_copy / trust caller bools.
    return LayerTwoHypotheticalPositionLifecycleVerificationResult(
        record_id=record.record_id or compute_record_id(record),
        structural_ok=True,
        entry_execution_binding_ok=True,
        allocator_binding_ok=True,
        phase_binding_ok=True,
        tranche_evaluation_protocol_binding_ok=True,
        ready_for_lifecycle_diagnostic=True,
    )


def load_layer_two_hypothetical_position_lifecycle_record(
    path: Path,
) -> LayerTwoHypotheticalPositionLifecycleRecord:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("hypothetical lifecycle record is missing or invalid") from exc
    try:
        return LayerTwoHypotheticalPositionLifecycleRecord.model_validate(payload)
    except Exception as exc:
        raise ValueError("hypothetical lifecycle record is missing or invalid") from exc


def write_layer_two_hypothetical_position_lifecycle_record(
    path: Path,
    record: LayerTwoHypotheticalPositionLifecycleRecord,
) -> LayerTwoHypotheticalPositionLifecycleRecord:
    sealed = seal_layer_two_hypothetical_position_lifecycle_record(record)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_BOARD_LOT",
    "BOUND_HOLDING_MARKET_BARS_ELAPSED_AT_OPEN",
    "BOUND_TRANCHE_EVALUATION_PROTOCOL_ID",
    "BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH",
    "LAYER_TWO_HYPOTHETICAL_LIFECYCLE_ENGINE_VERSION",
    "LAYER_TWO_HYPOTHETICAL_LIFECYCLE_SCHEMA_VERSION",
    "LayerTwoHypotheticalLifecycleFileBindings",
    "LayerTwoHypotheticalLifecycleFileInput",
    "LayerTwoHypotheticalLifecycleStructuralInput",
    "LayerTwoHypotheticalPositionLifecycleRecord",
    "LayerTwoHypotheticalPositionLifecycleVerificationResult",
    "assert_matches_recomputed_lifecycle_record",
    "assert_record_self_hash",
    "canonical_record_bytes",
    "canonical_record_payload",
    "compute_record_id",
    "load_layer_two_hypothetical_position_lifecycle_record",
    "open_layer_two_hypothetical_position_lifecycle",
    "seal_layer_two_hypothetical_position_lifecycle_record",
    "verify_layer_two_hypothetical_position_lifecycle_record",
    "verify_layer_two_hypothetical_position_lifecycle_record_file",
    "write_layer_two_hypothetical_position_lifecycle_record",
]
