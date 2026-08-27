"""Tranche evaluation protocol: v1 (legacy sealed) + v2 (confirmed not-ready).

Schema v1 unresolved drafts remain verifiable under their original semantics.
Schema v2 records user-confirmed rolling-tranche / capital / timing / window
choices with categorized evidence blockers and status
``confirmed_for_implementation_but_not_ready``.

Important: ``holding_period_market_trading_days`` /
``holding_cycle_market_trading_days`` (=40) are the hold and uniform phase-cycle
lengths. They are **not** an active tranche count. Active tranches track budget
caps at 3/6/9 (absolute max 9), one stock per active tranche.

File verification binds the research trial ledger, two-layer decision contract,
and layer-one index protocol from disk. Ready/auto flags stay false. Does not
invent alpha weights, run experiments, select phases by return, or reuse
consumed OOS.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.experiment_ledger import (
    ResearchTrialLedger,
    verify_research_trial_ledger,
)
from app.research.layer_one_index_protocol import (
    DEFAULT_LAYER_ONE_INDEX_PROTOCOL_DRAFT_PATH,
    load_layer_one_index_protocol_draft,
    verify_layer_one_index_protocol_draft,
)
from app.research.two_layer_contract import (
    BOUND_RESEARCH_TRIAL_LEDGER_ID,
    BOUND_RESEARCH_TRIAL_LEDGER_PATH,
    CONFIRMED_ABSOLUTE_MAX_POSITIONS,
    CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS,
    CONFIRMED_INITIAL_CASH,
    CONFIRMED_MAX_POSITIONS_BY_BUDGET,
    CONTRACT_CONFIRMATION_AS_OF,
    DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH,
    CostAssumptionsConfirmed,
    DeploymentUpgradePolicyConfirmed,
    PositionSizingPolicyConfirmed,
    TrancheHoldPolicyConfirmed,
    _assert_v2_tranche_position_consistency,
    _reject_blank_string,
    _validate_ledger_path_field,
    compute_two_layer_v2_overall_resolved,
    load_two_layer_decision_draft,
    verify_two_layer_decision_draft,
)

TRANCHE_EVALUATION_PROTOCOL_SCHEMA_VERSION_V1: Literal["1"] = "1"
TRANCHE_EVALUATION_PROTOCOL_SCHEMA_VERSION_V2: Literal["2"] = "2"
TRANCHE_EVALUATION_PROTOCOL_SCHEMA_VERSION = TRANCHE_EVALUATION_PROTOCOL_SCHEMA_VERSION_V2
TRANCHE_EVALUATION_PROTOCOL_VERSION_V1: Literal["tranche-evaluation-protocol-draft-v1"] = (
    "tranche-evaluation-protocol-draft-v1"
)
TRANCHE_EVALUATION_PROTOCOL_VERSION_V2: Literal["tranche-evaluation-protocol-v2"] = "tranche-evaluation-protocol-v2"
TRANCHE_EVALUATION_PROTOCOL_VERSION = TRANCHE_EVALUATION_PROTOCOL_VERSION_V2
DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH = Path("config/research/tranche-evaluation-protocol-draft-v1.json")
BOUND_TWO_LAYER_DECISION_CONTRACT_PATH: Literal["config/research/two-layer-strategy-decision-draft-v1.json"] = (
    "config/research/two-layer-strategy-decision-draft-v1.json"
)
# Disk-bound schema-v2 contract_id; must match the sealed two-layer contract on disk.
BOUND_TWO_LAYER_DECISION_CONTRACT_ID = "27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6"
BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH: Literal["config/research/layer-one-index-development-protocol-draft-v1.json"] = (
    "config/research/layer-one-index-development-protocol-draft-v1.json"
)
# Disk-bound schema-v2 protocol_id; must match the sealed layer-one protocol on disk.
BOUND_LAYER_ONE_INDEX_PROTOCOL_ID = "b7aa9de1539cdd791aee5b74ca8ec3f269b6ed809a070caa917686742c4b1b2f"

ProtocolStatusV1 = Literal["blocked_pending_user_decisions"]
ProtocolStatusV2 = Literal["confirmed_for_implementation_but_not_ready"]
ProtocolBlockerCategory = Literal[
    "pending_user_decision",
    "pending_factual_source_verification",
    "pending_implementation",
    "pending_development_evidence",
    "future_oos_observation",
    "future_enhancement",
]

REQUIRED_TRANCHE_PROTOCOL_DECISION_PATHS: tuple[str, ...] = (
    "tranche_count",
    "holding_period_bars",
    "decision_entry_timing",
    "exit_timing",
    "windows.development",
    "windows.validation_oos",
    "benchmark",
    "costs_minimum_commission_lot_handling",
    "capital_allocation_policy",
    "candidate_availability_policy",
    "go_no_go_metrics",
    "phase_comparison_policy",
    "trial_family_registration",
)

# Schema-v2 hard evidence blockers: exact path -> category (extras allowed as future enhancements).
REQUIRED_TRANCHE_EVIDENCE_BLOCKERS: dict[str, ProtocolBlockerCategory] = {
    "benchmark.csi_all_share_total_return_symbol": "pending_factual_source_verification",
    "costs.stamp_tax_schedule": "pending_factual_source_verification",
    "tranche_evaluation_runner": "pending_implementation",
    "execution_cash_attribution": "pending_implementation",
    "alpha_weight_selection": "pending_development_evidence",
    "windows.new_frozen_oos": "future_oos_observation",
}
REQUIRED_TRANCHE_EVIDENCE_BLOCKER_PATHS: tuple[str, ...] = tuple(REQUIRED_TRANCHE_EVIDENCE_BLOCKERS)

CONFIRMED_FACTOR_EVIDENCE_METHODS: tuple[str, ...] = (
    "full_cross_section_quantile_portfolios",
    "ic_icir_hac_overlap_corrected",
)
CONFIRMED_CASH_OCCUPANCY_CAUSES: tuple[str, ...] = (
    "candidate_shortage",
    "gates",
    "unaffordable_board_lot_or_min_commission",
    "suspension",
    "limit_up_or_limit_down",
    "risk_budget",
)

CONFIRMED_LAYER_TWO_DEVELOPMENT_START = date(2022, 1, 1)
CONFIRMED_LAYER_TWO_DEVELOPMENT_END = date(2023, 12, 31)
CONFIRMED_SEEN_ROBUSTNESS_START = date(2024, 1, 1)
CONFIRMED_SEEN_ROBUSTNESS_END = date(2024, 12, 31)
CONFIRMED_CONSUMED_OOS_START = date(2025, 1, 1)
CONFIRMED_CONSUMED_OOS_END = date(2026, 8, 21)
CONFIRMED_NEW_FROZEN_OOS_START = date(2026, 8, 22)
CONFIRMED_NEW_FROZEN_OOS_PLANNED_MONTHS: Literal[12] = 12


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProtocolEvidenceBlocker(_StrictModel):
    path: str = Field(min_length=1)
    category: ProtocolBlockerCategory
    detail: str = Field(min_length=1)

    @field_validator("path", "detail", mode="before")
    @classmethod
    def _reject_blank(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)


def _parse_iso_date(value: object, *, field_name: str) -> date:
    if isinstance(value, date) and type(value) is date:
        return value
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be an ISO date")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


class DateWindow(_StrictModel):
    start: date
    end: date

    @field_validator("start", "end", mode="before")
    @classmethod
    def _parse_dates(cls, value: object, info: Any) -> date:
        return _parse_iso_date(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _ordered(self) -> DateWindow:
        if self.end < self.start:
            raise ValueError("window end must be on or after start")
        return self


class ResearchWindowsPending(_StrictModel):
    development: DateWindow | None = None
    validation_oos: DateWindow | None = None


class ConfirmedTrancheCapital(_StrictModel):
    initial_cash: Literal[80000] = CONFIRMED_INITIAL_CASH
    initial_cash_confirmed: Literal[True] = True
    initial_cash_is_blocker: Literal[False] = False
    note: str = (
        "initial_cash=80000 is confirmed for account-scale tranche research and is not a pending user decision blocker."
    )


class ConsumedOosReusePolicy(_StrictModel):
    reuse_forbidden: Literal[True] = True
    note: str = (
        "Consumed one-shot OOS windows and receipts are terminal. Tranche "
        "evaluation development must not bind to them or reuse their evaluation windows."
    )


class TrancheEvaluationProtocolDraftV1(_StrictModel):
    schema_version: Literal["1"] = TRANCHE_EVALUATION_PROTOCOL_SCHEMA_VERSION_V1
    protocol_version: Literal["tranche-evaluation-protocol-draft-v1"] = TRANCHE_EVALUATION_PROTOCOL_VERSION_V1
    status: ProtocolStatusV1 = "blocked_pending_user_decisions"
    research_trial_ledger_id: str = Field(min_length=1)
    research_trial_ledger_path: Literal["config/research/research-trial-ledger-v1.json"] = (
        BOUND_RESEARCH_TRIAL_LEDGER_PATH
    )
    confirmed: ConfirmedTrancheCapital = Field(default_factory=ConfirmedTrancheCapital)
    consumed_oos: ConsumedOosReusePolicy = Field(default_factory=ConsumedOosReusePolicy)
    tranche_count: int | None = None
    holding_period_bars: int | None = None
    decision_entry_timing: str | None = None
    exit_timing: str | None = None
    windows: ResearchWindowsPending
    benchmark: str | None = None
    costs_minimum_commission_lot_handling: str | None = None
    capital_allocation_policy: str | None = None
    candidate_availability_policy: str | None = None
    go_no_go_metrics: str | None = None
    phase_comparison_policy: str | None = None
    trial_family_registration: str | None = None
    bound_consumed_oos_receipt_path: None = None
    bound_consumed_oos_freeze_id: None = None
    bound_consumed_oos_authorization_id: None = None
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    protocol_id: str | None = None

    @field_validator("research_trial_ledger_id", mode="before")
    @classmethod
    def _reject_blank_ledger_id(cls, value: object) -> object:
        return _reject_blank_string(value, field_name="research_trial_ledger_id")

    @field_validator("research_trial_ledger_path", mode="before")
    @classmethod
    def _reject_ledger_path_escape(cls, value: object) -> object:
        return _validate_ledger_path_field(value)

    @field_validator(
        "decision_entry_timing",
        "exit_timing",
        "benchmark",
        "costs_minimum_commission_lot_handling",
        "capital_allocation_policy",
        "candidate_availability_policy",
        "go_no_go_metrics",
        "phase_comparison_policy",
        "trial_family_registration",
        mode="before",
    )
    @classmethod
    def _reject_blank_text(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)

    @field_validator("tranche_count", "holding_period_bars")
    @classmethod
    def _validate_positive_optional(cls, value: int | None, info: Any) -> int | None:
        if value is None:
            return None
        if type(value) is not int or isinstance(value, bool):
            raise ValueError(f"{info.field_name} must be an int")
        if value < 1:
            raise ValueError(f"{info.field_name} must be >= 1")
        return value

    @model_validator(mode="after")
    def _gate_flags(self) -> TrancheEvaluationProtocolDraftV1:
        if self.status != "blocked_pending_user_decisions":
            raise ValueError("protocol status must remain blocked_pending_user_decisions")
        if self.ready_for_scoring or self.ready_for_backtest or self.ready_for_trading or self.auto_apply:
            raise ValueError("tranche evaluation protocol cannot authorize scoring, backtest, trading, or auto-apply")
        if self.confirmed.initial_cash != CONFIRMED_INITIAL_CASH:
            raise ValueError("confirmed initial_cash must remain 80000")
        if self.confirmed.initial_cash_is_blocker is not False:
            raise ValueError("initial_cash must never be a blocker")
        if not self.consumed_oos.reuse_forbidden:
            raise ValueError("consumed OOS reuse must remain forbidden")
        if self.bound_consumed_oos_receipt_path is not None:
            raise ValueError("bound_consumed_oos_receipt_path must remain null")
        if self.bound_consumed_oos_freeze_id is not None:
            raise ValueError("bound_consumed_oos_freeze_id must remain null")
        if self.bound_consumed_oos_authorization_id is not None:
            raise ValueError("bound_consumed_oos_authorization_id must remain null")
        if (
            self.tranche_count is not None
            and self.holding_period_bars is not None
            and self.holding_period_bars > self.tranche_count
        ):
            raise ValueError("holding_period_bars > tranche_count implies hidden leverage under daily round-robin")
        return self


TrancheEvaluationProtocolDraft = TrancheEvaluationProtocolDraftV1


class NewFrozenOosPlan(_StrictModel):
    start: date = CONFIRMED_NEW_FROZEN_OOS_START
    planned_continuous_months: Literal[12] = CONFIRMED_NEW_FROZEN_OOS_PLANNED_MONTHS
    role: Literal["new_frozen_oos"] = "new_frozen_oos"
    tunable_parameter_window: Literal[False] = False
    note: str = (
        "New future OOS begins 2026-08-22 and continues to accumulate for a planned "
        "12 months. It is distinct from consumed OOS 2025-01-01..2026-08-21. "
        "12-month accumulation is not a hard gate for manual 60%/90% unlock."
    )

    @field_validator("start", mode="before")
    @classmethod
    def _parse_start(cls, value: object) -> date:
        return _parse_iso_date(value, field_name="start")

    @model_validator(mode="after")
    def _freeze(self) -> NewFrozenOosPlan:
        if self.start != CONFIRMED_NEW_FROZEN_OOS_START:
            raise ValueError("new_frozen_oos.start must equal 2026-08-22")
        return self


class TrancheResearchWindowsConfirmed(_StrictModel):
    seen_development: DateWindow
    seen_robustness_check_only: DateWindow
    consumed_oos: DateWindow
    new_frozen_oos: NewFrozenOosPlan = Field(default_factory=NewFrozenOosPlan)
    note: str = (
        "Layer-two seen development is limited to 2022-01-01..2023-12-31; 2024 is "
        "seen robustness only (already observed; report-only, not unseen); "
        "consumed 2025-01-01..2026-08-21 must not be reused; new frozen OOS starts "
        "2026-08-22. Stricter local bounds win if present."
    )

    @model_validator(mode="after")
    def _ordered_and_strict(self) -> TrancheResearchWindowsConfirmed:
        if self.seen_development.start != CONFIRMED_LAYER_TWO_DEVELOPMENT_START:
            raise ValueError("seen_development must start on 2022-01-01")
        if self.seen_development.end != CONFIRMED_LAYER_TWO_DEVELOPMENT_END:
            raise ValueError("seen_development must end on 2023-12-31")
        if self.seen_robustness_check_only.start != CONFIRMED_SEEN_ROBUSTNESS_START:
            raise ValueError("seen_robustness_check_only must start on 2024-01-01")
        if self.seen_robustness_check_only.end != CONFIRMED_SEEN_ROBUSTNESS_END:
            raise ValueError("seen_robustness_check_only must end on 2024-12-31")
        if self.consumed_oos.start != CONFIRMED_CONSUMED_OOS_START:
            raise ValueError("consumed_oos must start on 2025-01-01")
        if self.consumed_oos.end != CONFIRMED_CONSUMED_OOS_END:
            raise ValueError("consumed_oos must end on 2026-08-21")
        if self.seen_development.end >= self.seen_robustness_check_only.start:
            raise ValueError("seen_development must end before seen_robustness_check_only starts")
        if self.seen_robustness_check_only.end >= self.consumed_oos.start:
            raise ValueError("seen_robustness_check_only must end before consumed_oos starts")
        if self.consumed_oos.end >= self.new_frozen_oos.start:
            raise ValueError("consumed_oos must end before new_frozen_oos starts")
        return self


class TrancheDecisionTimingConfirmed(_StrictModel):
    decision_after_close_on_t: Literal[True] = True
    attempt_fill_at_next_open_t_plus_1: Literal[True] = True
    fill_day_is_holding_day_1: Literal[True] = True
    exit_after_holding_period_at_next_tradable_open: Literal[True] = True
    suspension_holding_day_clock: Literal["count_suspended_days"] = "count_suspended_days"
    limit_or_suspension_defers_no_hindsight_fill: Literal[True] = True
    no_hindsight_rewrite_of_original_orders: Literal[True] = True


class TrancheBudgetAdjustmentConfirmed(_StrictModel):
    layer_one_risk_lock_and_reduce_have_priority: Literal[True] = True
    stock_budget_reduce_allowed_daily: Literal[True] = True
    stock_budget_increase_only_on_first_trading_day_of_week: Literal[True] = True
    stock_budget_increase_uses_prior_trading_day_known_state: Literal[True] = True


class TranchePhasePolicyConfirmed(_StrictModel):
    phase_determined_only_by_frozen_calendar_and_anchor: Literal[True] = True
    uniform_stagger_within_holding_cycle: Literal[True] = True
    never_select_phase_by_return: Literal[True] = True
    same_day_catchup_fill_forbidden: Literal[True] = True
    risk_reduce_not_phase_limited: Literal[True] = True
    report_full_phase_family_and_tranche_combined_path: Literal[True] = True
    note: str = (
        "Active phases for each budget tier are deterministically and as-uniformly-as-"
        "possible staggered across the 40 market-trading-day cycle. Phase identity is "
        "frozen-calendar/anchor only; never pick a 'best' phase by return."
    )


class TrancheCandidateCashPolicyConfirmed(_StrictModel):
    retain_cash_when_candidates_insufficient: Literal[True] = True
    retain_cash_when_critical_input_missing_or_unknown: Literal[True] = True
    retain_cash_when_board_lot_or_min_commission_unaffordable: Literal[True] = True
    never_shrink_min_notional_or_backfill_small_caps: Literal[True] = True
    never_relax_candidate_gates: Literal[True] = True
    never_reuse_other_date_candidates: Literal[True] = True
    never_fill_holes_with_future_data: Literal[True] = True


class TrancheOwnershipPolicyConfirmed(_StrictModel):
    ownership_proxy_role: Literal["diagnostic"] = "diagnostic"
    ownership_never_in_tranche_scoring_or_exclusion: Literal[True] = True
    note: str = (
        "Institutional ownership / holding proxies are diagnostic only and must never "
        "participate in tranche selection, scoring, or exclusion."
    )


class TrancheBenchmarkConfirmed(_StrictModel):
    name: Literal["csi_all_share_total_return"] = "csi_all_share_total_return"
    return_definition: Literal["total_return"] = "total_return"
    market_data_primary_source: Literal["tushare"] = "tushare"
    identity_cross_check: Literal["csi_index_official_website"] = "csi_index_official_website"
    symbol: None = None
    symbol_status: Literal["pending_factual_source_verification"] = "pending_factual_source_verification"
    note: str = (
        "CSI All Share Total Return is the performance benchmark. Exact official "
        "Tushare/CSI symbol is a factual evidence blocker; do not guess."
    )


class TrancheEvaluationMachineConfirmed(_StrictModel):
    """Frozen recommended evaluation machine; not executed in this milestone."""

    never_select_single_best_phase_by_return: Literal[True] = True
    report_full_frozen_phase_family: Literal[True] = True
    primary_execution_structure: Literal["phase_aggregation_and_tranche_combined_path"] = (
        "phase_aggregation_and_tranche_combined_path"
    )
    list_all_phase_results_individually: Literal[True] = True
    factor_evidence_methods: list[str] = Field(default_factory=lambda: list(CONFIRMED_FACTOR_EVIDENCE_METHODS))
    ten_name_or_tranche_portfolio_is_execution_check_only: Literal[True] = True
    factor_scoring_wiring_forbidden_in_this_milestone: Literal[True] = True
    must_attribute_cash_occupancy_causes: list[str] = Field(
        default_factory=lambda: list(CONFIRMED_CASH_OCCUPANCY_CAUSES)
    )
    never_report_total_return_alone: Literal[True] = True
    trial_family_registration_via_research_trial_ledger: Literal[True] = True
    never_pick_phase_or_params_by_return: Literal[True] = True
    note: str = (
        "Evaluation machine is frozen as protocol text only. E8c must not run score, "
        "IC, phase, or backtest evaluations."
    )

    @model_validator(mode="after")
    def _freeze_confirmed_sequences(self) -> TrancheEvaluationMachineConfirmed:
        if list(self.factor_evidence_methods) != list(CONFIRMED_FACTOR_EVIDENCE_METHODS):
            raise ValueError(
                f"factor_evidence_methods must equal confirmed ordered set {list(CONFIRMED_FACTOR_EVIDENCE_METHODS)!r}"
            )
        if list(self.must_attribute_cash_occupancy_causes) != list(CONFIRMED_CASH_OCCUPANCY_CAUSES):
            raise ValueError(
                "must_attribute_cash_occupancy_causes must equal confirmed ordered set "
                f"{list(CONFIRMED_CASH_OCCUPANCY_CAUSES)!r}"
            )
        return self


class TrancheGoNoGoConfirmed(_StrictModel):
    evaluate_after_costs_and_15bps_stress: Literal[True] = True
    require_phase_year_and_market_state_reports: Literal[True] = True
    not_ready_when_evidence_gates_unmet: Literal[True] = True
    invent_alpha_return_threshold_forbidden: Literal[True] = True
    cite_upstream_hard_risk_gates_do_not_copy_drift: Literal[True] = True
    upstream_hard_risk_gates_source: Literal["layer-one-index-development-protocol-v2.hard_gates"] = (
        "layer-one-index-development-protocol-v2.hard_gates"
    )
    note: str = (
        "Go/no-go criteria are declared only. Do not fabricate results. If upstream "
        "layer-one hard risk gates are already frozen, cite them; do not restate "
        "drifted copies. No invented alpha return hurdle."
    )


def default_tranche_evidence_blockers() -> list[ProtocolEvidenceBlocker]:
    return [
        ProtocolEvidenceBlocker(
            path="benchmark.csi_all_share_total_return_symbol",
            category="pending_factual_source_verification",
            detail=(
                "CSI All-Share total-return exact Tushare/CSI symbol pending factual source verification; do not guess."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="costs.stamp_tax_schedule",
            category="pending_factual_source_verification",
            detail=(
                "Official full historical sell-side stamp-tax timetable evidence incomplete; "
                "flat 0.1%-since-1900 must not be treated as done."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="tranche_evaluation_runner",
            category="pending_implementation",
            detail=(
                "Confirmed tranche/hold/exit, capital, phase-stagger, and cash-retain "
                "policies are not yet implemented as a tranche evaluation engine/runner."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="execution_cash_attribution",
            category="pending_implementation",
            detail=(
                "Execution cash-occupancy attribution "
                "(candidate_shortage / gates / unaffordable board-lot or min commission / "
                "suspension / limit_up_or_limit_down / risk_budget) is not yet implemented."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="alpha_weight_selection",
            category="pending_development_evidence",
            detail=(
                "Alpha families are pre-registered only; weights remain pending development "
                "evidence from quantile/ICIR evaluation. Not a pending user decision. "
                "Factor scoring must not be wired in E8c."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="windows.new_frozen_oos",
            category="future_oos_observation",
            detail=(
                "New frozen OOS from 2026-08-22 is incomplete; distinct from consumed OOS "
                "2025-01-01..2026-08-21. Complete OOS pass claim forbidden before maturity."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="ownership_proxy_scoring_integration",
            category="future_enhancement",
            detail=("Institutional ownership proxies stay diagnostic only; never enter tranche/scoring/exclusion."),
        ),
        ProtocolEvidenceBlocker(
            path="event_factor_scoring_integration",
            category="future_enhancement",
            detail=(
                "Shareholder count / earnings preview / unlock / pledge stay diagnostic until "
                "independent development and PIT coverage gates pass."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="pit_industry_history",
            category="future_enhancement",
            detail="Real PIT industry history remains a future enhancement; clusters are the current proxy.",
        ),
    ]


class TrancheEvaluationProtocolV2(_StrictModel):
    schema_version: Literal["2"] = TRANCHE_EVALUATION_PROTOCOL_SCHEMA_VERSION_V2
    protocol_version: Literal["tranche-evaluation-protocol-v2"] = TRANCHE_EVALUATION_PROTOCOL_VERSION_V2
    status: ProtocolStatusV2 = "confirmed_for_implementation_but_not_ready"
    confirmation_as_of: date = CONTRACT_CONFIRMATION_AS_OF
    research_trial_ledger_id: str = Field(min_length=1)
    research_trial_ledger_path: Literal["config/research/research-trial-ledger-v1.json"] = (
        BOUND_RESEARCH_TRIAL_LEDGER_PATH
    )
    two_layer_decision_contract_id: str = Field(min_length=1)
    two_layer_decision_contract_path: Literal["config/research/two-layer-strategy-decision-draft-v1.json"] = (
        BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
    )
    layer_one_index_protocol_id: str = Field(min_length=1)
    layer_one_index_protocol_path: Literal["config/research/layer-one-index-development-protocol-draft-v1.json"] = (
        BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH
    )
    confirmed: ConfirmedTrancheCapital = Field(default_factory=ConfirmedTrancheCapital)
    consumed_oos: ConsumedOosReusePolicy = Field(default_factory=ConsumedOosReusePolicy)
    tranche_hold: TrancheHoldPolicyConfirmed = Field(default_factory=TrancheHoldPolicyConfirmed)
    position_sizing: PositionSizingPolicyConfirmed = Field(default_factory=PositionSizingPolicyConfirmed)
    phase_policy: TranchePhasePolicyConfirmed = Field(default_factory=TranchePhasePolicyConfirmed)
    decision_timing: TrancheDecisionTimingConfirmed = Field(default_factory=TrancheDecisionTimingConfirmed)
    budget_adjustment: TrancheBudgetAdjustmentConfirmed = Field(default_factory=TrancheBudgetAdjustmentConfirmed)
    candidate_cash_policy: TrancheCandidateCashPolicyConfirmed = Field(
        default_factory=TrancheCandidateCashPolicyConfirmed
    )
    ownership: TrancheOwnershipPolicyConfirmed = Field(default_factory=TrancheOwnershipPolicyConfirmed)
    windows: TrancheResearchWindowsConfirmed
    benchmark: TrancheBenchmarkConfirmed = Field(default_factory=TrancheBenchmarkConfirmed)
    cost_assumptions: CostAssumptionsConfirmed = Field(default_factory=CostAssumptionsConfirmed)
    deployment_upgrade: DeploymentUpgradePolicyConfirmed = Field(default_factory=DeploymentUpgradePolicyConfirmed)
    evaluation_machine: TrancheEvaluationMachineConfirmed = Field(default_factory=TrancheEvaluationMachineConfirmed)
    go_no_go: TrancheGoNoGoConfirmed = Field(default_factory=TrancheGoNoGoConfirmed)
    trial_family_registration: Literal["register_trial_family_before_any_evaluation_via_research_trial_ledger"] = (
        "register_trial_family_before_any_evaluation_via_research_trial_ledger"
    )
    evidence_blockers: list[ProtocolEvidenceBlocker]
    pending_user_decisions: list[str] = Field(default_factory=list)
    bound_consumed_oos_receipt_path: None = None
    bound_consumed_oos_freeze_id: None = None
    bound_consumed_oos_authorization_id: None = None
    research_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    protocol_id: str | None = None

    @field_validator(
        "research_trial_ledger_id",
        "two_layer_decision_contract_id",
        "layer_one_index_protocol_id",
        mode="before",
    )
    @classmethod
    def _reject_blank_ids(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)

    @field_validator("research_trial_ledger_path", mode="before")
    @classmethod
    def _reject_ledger_path_escape(cls, value: object) -> object:
        return _validate_ledger_path_field(value)

    @field_validator("two_layer_decision_contract_path", mode="before")
    @classmethod
    def _reject_contract_path_escape(cls, value: object) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("two_layer_decision_contract_path must be a non-empty relative path")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("two_layer_decision_contract_path must be relative without parent traversal")
        if value != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
            raise ValueError("two_layer_decision_contract_path does not match bound two-layer contract path")
        return value

    @field_validator("layer_one_index_protocol_path", mode="before")
    @classmethod
    def _reject_layer_one_path_escape(cls, value: object) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("layer_one_index_protocol_path must be a non-empty relative path")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("layer_one_index_protocol_path must be relative without parent traversal")
        if value != BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH:
            raise ValueError("layer_one_index_protocol_path does not match bound layer-one protocol path")
        return value

    @field_validator("confirmation_as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, value: object) -> date:
        return _parse_iso_date(value, field_name="confirmation_as_of")

    @model_validator(mode="after")
    def _gate_flags(self) -> TrancheEvaluationProtocolV2:
        if self.status != "confirmed_for_implementation_but_not_ready":
            raise ValueError("v2 protocol status must be confirmed_for_implementation_but_not_ready")
        if (
            self.ready_for_scoring
            or self.ready_for_backtest
            or self.ready_for_trading
            or self.auto_apply
            or not self.research_only
        ):
            raise ValueError(
                "tranche evaluation protocol must remain research_only with scoring/"
                "backtest/trading/auto-apply unauthorized"
            )
        if self.confirmed.initial_cash != CONFIRMED_INITIAL_CASH:
            raise ValueError("confirmed initial_cash must remain 80000")
        if not self.consumed_oos.reuse_forbidden:
            raise ValueError("consumed OOS reuse must remain forbidden")
        if self.pending_user_decisions:
            raise ValueError("confirmed protocol must have empty pending_user_decisions")
        if self.research_trial_ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
            raise ValueError("research_trial_ledger_id does not match bound research trial ledger")
        if self.two_layer_decision_contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
            raise ValueError("two_layer_decision_contract_id does not match bound two-layer contract")
        if self.layer_one_index_protocol_id != BOUND_LAYER_ONE_INDEX_PROTOCOL_ID:
            raise ValueError("layer_one_index_protocol_id does not match bound layer-one protocol")
        _assert_v2_tranche_position_consistency(
            position_sizing=self.position_sizing,
            tranche_hold=self.tranche_hold,
        )
        if self.tranche_hold.holding_period_market_trading_days != CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS:
            raise ValueError("holding_period_market_trading_days must equal 40")
        if self.tranche_hold.holding_cycle_market_trading_days != CONFIRMED_HOLDING_CYCLE_MARKET_TRADING_DAYS:
            raise ValueError("holding_cycle_market_trading_days must equal 40")
        if self.tranche_hold.max_active_tranches_by_budget != CONFIRMED_MAX_POSITIONS_BY_BUDGET:
            raise ValueError("max_active_tranches_by_budget must map 0.3/0.6/0.9 -> 3/6/9")
        if self.position_sizing.max_positions_by_budget != CONFIRMED_MAX_POSITIONS_BY_BUDGET:
            raise ValueError("max_positions_by_budget must map 0.3/0.6/0.9 -> 3/6/9")
        if self.position_sizing.min_target_notional_cny != 8000:
            raise ValueError("min_target_notional_cny must equal 8000")
        if self.position_sizing.absolute_max_positions != CONFIRMED_ABSOLUTE_MAX_POSITIONS:
            raise ValueError("absolute_max_positions must equal 9")
        if self.benchmark.symbol is not None or self.benchmark.symbol_status != "pending_factual_source_verification":
            raise ValueError("benchmark symbol must remain pending factual verification")
        if self.cost_assumptions.stamp_tax_schedule_status != "pending_factual_implementation_evidence":
            raise ValueError("stamp schedule must remain pending factual; flat completion forbidden")
        if any(b.category == "pending_user_decision" for b in self.evidence_blockers):
            raise ValueError("confirmed protocol must not retain pending_user_decision blockers")
        categories = {blocker.category for blocker in self.evidence_blockers}
        required = {
            "pending_factual_source_verification",
            "pending_implementation",
            "pending_development_evidence",
            "future_oos_observation",
        }
        missing = required - categories
        if missing:
            raise ValueError(f"evidence_blockers missing required categories: {sorted(missing)}")
        seen_paths: set[str] = set()
        path_to_category: dict[str, str] = {}
        for blocker in self.evidence_blockers:
            if blocker.path in seen_paths:
                raise ValueError(f"evidence_blockers duplicate path: {blocker.path}")
            seen_paths.add(blocker.path)
            path_to_category[blocker.path] = blocker.category
        missing_mappings = [path for path in REQUIRED_TRANCHE_EVIDENCE_BLOCKERS if path not in path_to_category]
        if missing_mappings:
            raise ValueError(f"evidence_blockers missing required paths: {missing_mappings}")
        wrong_category = [
            f"{path}->{path_to_category[path]} (expected {expected})"
            for path, expected in REQUIRED_TRANCHE_EVIDENCE_BLOCKERS.items()
            if path_to_category[path] != expected
        ]
        if wrong_category:
            raise ValueError(
                "evidence_blockers path->category mismatch for required blockers: " + "; ".join(wrong_category)
            )
        if self.bound_consumed_oos_receipt_path is not None:
            raise ValueError("bound_consumed_oos_receipt_path must remain null")
        if self.bound_consumed_oos_freeze_id is not None:
            raise ValueError("bound_consumed_oos_freeze_id must remain null")
        if self.bound_consumed_oos_authorization_id is not None:
            raise ValueError("bound_consumed_oos_authorization_id must remain null")
        return self


TrancheEvaluationProtocolDocument = TrancheEvaluationProtocolDraftV1 | TrancheEvaluationProtocolV2


class TrancheEvaluationProtocolVerificationResult(_StrictModel):
    protocol_id: str
    schema_version: Literal["1", "2"]
    protocol_version: str
    status: str
    structural_ok: bool
    research_trial_ledger_id: str
    research_trial_ledger_path: Literal["config/research/research-trial-ledger-v1.json"]
    research_trial_ledger_binding_ok: bool
    two_layer_decision_contract_id: str | None = None
    two_layer_decision_contract_path: str | None = None
    two_layer_decision_contract_binding_ok: bool = False
    layer_one_index_protocol_id: str | None = None
    layer_one_index_protocol_path: str | None = None
    layer_one_index_protocol_binding_ok: bool = False
    resolved: bool
    user_decisions_resolved: bool
    pending_user_decision_count: int
    pending_user_decisions: list[str] = Field(default_factory=list)
    blockers: list[str]
    evidence_blockers: list[ProtocolEvidenceBlocker] = Field(default_factory=list)
    windows_overlap: bool
    consumed_oos_reuse_check_ok: bool
    consumed_oos_reuse_forbidden: Literal[True] = True
    confirmed_initial_cash: Literal[80000] = CONFIRMED_INITIAL_CASH
    initial_cash_is_blocker: Literal[False] = False
    research_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_score: Literal[True] = True
    does_not_backtest: Literal[True] = True
    does_not_trade: Literal[True] = True


def canonical_protocol_payload(draft: TrancheEvaluationProtocolDocument) -> dict[str, Any]:
    return draft.model_dump(mode="json", exclude={"protocol_id"})


def canonical_protocol_bytes(draft: TrancheEvaluationProtocolDocument) -> bytes:
    payload = canonical_protocol_payload(draft)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_protocol_id(draft: TrancheEvaluationProtocolDocument) -> str:
    return hashlib.sha256(canonical_protocol_bytes(draft)).hexdigest()


def seal_tranche_evaluation_protocol_draft(
    draft: TrancheEvaluationProtocolDocument,
) -> TrancheEvaluationProtocolDocument:
    return draft.model_copy(update={"protocol_id": compute_protocol_id(draft)})


def build_unresolved_tranche_evaluation_protocol_draft(
    *,
    research_trial_ledger_id: str = BOUND_RESEARCH_TRIAL_LEDGER_ID,
    research_trial_ledger_path: Literal[
        "config/research/research-trial-ledger-v1.json"
    ] = BOUND_RESEARCH_TRIAL_LEDGER_PATH,
) -> TrancheEvaluationProtocolDraftV1:
    draft = TrancheEvaluationProtocolDraftV1(
        research_trial_ledger_id=research_trial_ledger_id,
        research_trial_ledger_path=research_trial_ledger_path,
        windows=ResearchWindowsPending(),
    )
    return seal_tranche_evaluation_protocol_draft(draft)  # type: ignore[return-value]


def build_confirmed_tranche_evaluation_protocol_v2(
    *,
    research_trial_ledger_id: str = BOUND_RESEARCH_TRIAL_LEDGER_ID,
    research_trial_ledger_path: Literal[
        "config/research/research-trial-ledger-v1.json"
    ] = BOUND_RESEARCH_TRIAL_LEDGER_PATH,
    two_layer_decision_contract_id: str = BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    two_layer_decision_contract_path: Literal[
        "config/research/two-layer-strategy-decision-draft-v1.json"
    ] = BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
    layer_one_index_protocol_id: str = BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
    layer_one_index_protocol_path: Literal[
        "config/research/layer-one-index-development-protocol-draft-v1.json"
    ] = BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH,
    confirmation_as_of: date = CONTRACT_CONFIRMATION_AS_OF,
) -> TrancheEvaluationProtocolV2:
    draft = TrancheEvaluationProtocolV2(
        confirmation_as_of=confirmation_as_of,
        research_trial_ledger_id=research_trial_ledger_id,
        research_trial_ledger_path=research_trial_ledger_path,
        two_layer_decision_contract_id=two_layer_decision_contract_id,
        two_layer_decision_contract_path=two_layer_decision_contract_path,
        layer_one_index_protocol_id=layer_one_index_protocol_id,
        layer_one_index_protocol_path=layer_one_index_protocol_path,
        windows=TrancheResearchWindowsConfirmed(
            seen_development=DateWindow(
                start=CONFIRMED_LAYER_TWO_DEVELOPMENT_START,
                end=CONFIRMED_LAYER_TWO_DEVELOPMENT_END,
            ),
            seen_robustness_check_only=DateWindow(
                start=CONFIRMED_SEEN_ROBUSTNESS_START,
                end=CONFIRMED_SEEN_ROBUSTNESS_END,
            ),
            consumed_oos=DateWindow(
                start=CONFIRMED_CONSUMED_OOS_START,
                end=CONFIRMED_CONSUMED_OOS_END,
            ),
            new_frozen_oos=NewFrozenOosPlan(),
        ),
        evidence_blockers=default_tranche_evidence_blockers(),
        pending_user_decisions=[],
    )
    return seal_tranche_evaluation_protocol_draft(draft)  # type: ignore[return-value]


def migrate_tranche_evaluation_protocol_v1_to_v2(
    draft_v1: TrancheEvaluationProtocolDraftV1,
    *,
    confirmation_as_of: date = CONTRACT_CONFIRMATION_AS_OF,
) -> TrancheEvaluationProtocolV2:
    blockers = collect_protocol_decision_blockers_v1(draft_v1)
    if blockers and len(blockers) != len(REQUIRED_TRANCHE_PROTOCOL_DECISION_PATHS):
        raise ValueError(
            "partially filled schema-v1 protocol cannot auto-migrate; provide an explicit confirmed v2 overlay"
        )
    if draft_v1.research_trial_ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
        raise ValueError("migration requires bound research trial ledger id")
    return build_confirmed_tranche_evaluation_protocol_v2(
        research_trial_ledger_id=draft_v1.research_trial_ledger_id,
        research_trial_ledger_path=draft_v1.research_trial_ledger_path,
        confirmation_as_of=confirmation_as_of,
    )


def _decision_value(draft: TrancheEvaluationProtocolDraftV1, path: str) -> object:
    current: object = draft
    for part in path.split("."):
        current = getattr(current, part)
    return current


def collect_protocol_decision_blockers_v1(draft: TrancheEvaluationProtocolDraftV1) -> list[str]:
    blockers: list[str] = []
    for path in REQUIRED_TRANCHE_PROTOCOL_DECISION_PATHS:
        if _decision_value(draft, path) is None:
            blockers.append(path)
    return blockers


def collect_protocol_decision_blockers(draft: TrancheEvaluationProtocolDocument) -> list[str]:
    if isinstance(draft, TrancheEvaluationProtocolDraftV1):
        return collect_protocol_decision_blockers_v1(draft)
    return list(draft.pending_user_decisions)


def assert_protocol_self_hash(draft: TrancheEvaluationProtocolDocument) -> None:
    if draft.protocol_id is None:
        raise ValueError("tranche evaluation protocol_id is missing")
    expected = compute_protocol_id(draft)
    if draft.protocol_id != expected:
        raise ValueError("tranche evaluation protocol_id does not match canonical content hash")


def assert_status_ready_consistency(draft: TrancheEvaluationProtocolDocument) -> None:
    if draft.ready_for_scoring or draft.ready_for_backtest or draft.ready_for_trading or draft.auto_apply:
        raise ValueError("status/ready contradiction: ready flags must remain false")
    if isinstance(draft, TrancheEvaluationProtocolDraftV1):
        if draft.status != "blocked_pending_user_decisions":
            raise ValueError("status/ready contradiction: v1 status must be blocked_pending_user_decisions")
    elif draft.status != "confirmed_for_implementation_but_not_ready":
        raise ValueError("status/ready contradiction: v2 status must be confirmed_for_implementation_but_not_ready")
    elif not draft.research_only:
        raise ValueError("status/ready contradiction: research_only must remain true")


def assert_bound_research_trial_ledger_id(draft: TrancheEvaluationProtocolDocument) -> None:
    if draft.research_trial_ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
        raise ValueError("research_trial_ledger_id does not match bound research trial ledger")
    if draft.research_trial_ledger_path != BOUND_RESEARCH_TRIAL_LEDGER_PATH:
        raise ValueError("research_trial_ledger_path does not match bound research trial ledger path")


def assert_bound_upstream_ids(draft: TrancheEvaluationProtocolV2) -> None:
    if draft.two_layer_decision_contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
        raise ValueError("two_layer_decision_contract_id does not match bound two-layer contract")
    if draft.two_layer_decision_contract_path != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two_layer_decision_contract_path does not match bound two-layer contract path")
    if draft.layer_one_index_protocol_id != BOUND_LAYER_ONE_INDEX_PROTOCOL_ID:
        raise ValueError("layer_one_index_protocol_id does not match bound layer-one protocol")
    if draft.layer_one_index_protocol_path != BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH:
        raise ValueError("layer_one_index_protocol_path does not match bound layer-one protocol path")


def windows_overlap(left: DateWindow, right: DateWindow) -> bool:
    return left.start <= right.end and right.start <= left.end


def assert_no_window_overlap(draft: TrancheEvaluationProtocolDocument) -> None:
    if isinstance(draft, TrancheEvaluationProtocolDraftV1):
        development = draft.windows.development
        validation = draft.windows.validation_oos
        if development is None or validation is None:
            return
        if windows_overlap(development, validation):
            raise ValueError("development and validation_oos windows must not overlap")
        return
    windows = (
        draft.windows.seen_development,
        draft.windows.seen_robustness_check_only,
        draft.windows.consumed_oos,
    )
    for i, left in enumerate(windows):
        for right in windows[i + 1 :]:
            if windows_overlap(left, right):
                raise ValueError("tranche research windows must not overlap")
    if draft.windows.consumed_oos.end >= draft.windows.new_frozen_oos.start:
        raise ValueError("consumed_oos must end before new_frozen_oos starts")


def assert_no_future_tranche_windows(
    draft: TrancheEvaluationProtocolV2,
    *,
    reference_date: date | None = None,
) -> None:
    as_of = reference_date or draft.confirmation_as_of
    if draft.windows.seen_development.end > as_of:
        raise ValueError("seen_development window end is after reference_date/confirmation_as_of")
    if draft.windows.seen_robustness_check_only.end > as_of:
        raise ValueError("seen_robustness_check_only window end is after reference_date/confirmation_as_of")


def _window_overlaps_consumed_oos(
    window: DateWindow,
    ledger: ResearchTrialLedger,
) -> list[str]:
    hits: list[str] = []
    for trial in ledger.trials:
        if not trial.oos_consumed:
            continue
        evaluation = trial.evaluation_window
        if evaluation is None or evaluation.start is None or evaluation.end is None:
            continue
        consumed = DateWindow(start=evaluation.start, end=evaluation.end)
        if windows_overlap(window, consumed):
            hits.append(trial.trial_id)
    return hits


def assert_no_consumed_oos_binding(
    draft: TrancheEvaluationProtocolDocument,
    ledger: ResearchTrialLedger,
) -> None:
    if draft.bound_consumed_oos_receipt_path is not None:
        raise ValueError("binding to a consumed OOS receipt path is forbidden")
    if draft.bound_consumed_oos_freeze_id is not None:
        raise ValueError("binding to a consumed OOS freeze_id is forbidden")
    if draft.bound_consumed_oos_authorization_id is not None:
        raise ValueError("binding to a consumed OOS authorization_id is forbidden")

    consumed_receipts = {
        trial.receipt_path for trial in ledger.trials if trial.oos_consumed and trial.receipt_path is not None
    }
    consumed_freezes = {
        trial.freeze_id for trial in ledger.trials if trial.oos_consumed and trial.freeze_id is not None
    }
    consumed_auths = {
        trial.authorization_id for trial in ledger.trials if trial.oos_consumed and trial.authorization_id is not None
    }

    payload = json.dumps(canonical_protocol_payload(draft), ensure_ascii=False)
    for receipt in consumed_receipts:
        if receipt and receipt in payload:
            raise ValueError(f"protocol payload binds consumed OOS receipt path: {receipt}")
    for freeze_id in consumed_freezes:
        if freeze_id and freeze_id in payload:
            raise ValueError(f"protocol payload binds consumed OOS freeze_id: {freeze_id}")
    for auth_id in consumed_auths:
        if auth_id and auth_id in payload:
            raise ValueError(f"protocol payload binds consumed OOS authorization_id: {auth_id}")

    if isinstance(draft, TrancheEvaluationProtocolDraftV1):
        validation = draft.windows.validation_oos
        if validation is not None:
            hits = _window_overlaps_consumed_oos(validation, ledger)
            if hits:
                raise ValueError("validation_oos window overlaps consumed OOS evaluation window(s): " + ",".join(hits))
        development = draft.windows.development
        if development is not None:
            hits = _window_overlaps_consumed_oos(development, ledger)
            if hits:
                raise ValueError("development window overlaps consumed OOS evaluation window(s): " + ",".join(hits))
        return

    # v2: development / robustness must not reuse ledger-consumed OOS windows.
    # The protocol's own consumed_oos label documents the terminal window; it is not reuse.
    for label, window in (
        ("seen_development", draft.windows.seen_development),
        ("seen_robustness_check_only", draft.windows.seen_robustness_check_only),
    ):
        hits = _window_overlaps_consumed_oos(window, ledger)
        if hits:
            raise ValueError(f"{label} window overlaps consumed OOS evaluation window(s): " + ",".join(hits))


def compute_tranche_v2_overall_resolved(
    *,
    evidence_blockers: Sequence[object],
    status: str,
    ready_for_scoring: bool,
    ready_for_backtest: bool,
    ready_for_trading: bool,
) -> bool:
    """Fail-closed overall resolved for schema-v2 protocol verification results."""
    return compute_two_layer_v2_overall_resolved(
        evidence_blockers=evidence_blockers,
        status=status,
        ready_for_scoring=ready_for_scoring,
        ready_for_backtest=ready_for_backtest,
        ready_for_trading=ready_for_trading,
    )


def verify_tranche_evaluation_protocol_draft(
    draft: TrancheEvaluationProtocolDocument,
    *,
    reference_date: date | None = None,
) -> TrancheEvaluationProtocolVerificationResult:
    assert_protocol_self_hash(draft)
    assert_status_ready_consistency(draft)
    assert_bound_research_trial_ledger_id(draft)
    assert_no_window_overlap(draft)

    if isinstance(draft, TrancheEvaluationProtocolDraftV1):
        blockers = collect_protocol_decision_blockers_v1(draft)
        resolved = len(blockers) == 0
        if not resolved and draft.status != "blocked_pending_user_decisions":
            raise ValueError("unresolved protocol must keep status blocked_pending_user_decisions")
        return TrancheEvaluationProtocolVerificationResult(
            protocol_id=draft.protocol_id or compute_protocol_id(draft),
            schema_version="1",
            protocol_version=draft.protocol_version,
            status=draft.status,
            structural_ok=True,
            research_trial_ledger_id=draft.research_trial_ledger_id,
            research_trial_ledger_path=draft.research_trial_ledger_path,
            research_trial_ledger_binding_ok=False,
            two_layer_decision_contract_id=None,
            two_layer_decision_contract_path=None,
            two_layer_decision_contract_binding_ok=False,
            layer_one_index_protocol_id=None,
            layer_one_index_protocol_path=None,
            layer_one_index_protocol_binding_ok=False,
            resolved=resolved,
            user_decisions_resolved=resolved,
            pending_user_decision_count=len(blockers),
            pending_user_decisions=list(blockers),
            blockers=blockers,
            evidence_blockers=[],
            windows_overlap=False,
            consumed_oos_reuse_check_ok=False,
        )

    assert_bound_upstream_ids(draft)
    assert_no_future_tranche_windows(draft, reference_date=reference_date)
    if any(b.category == "pending_user_decision" for b in draft.evidence_blockers):
        raise ValueError("confirmed protocol has pending_user_decision blockers")
    if draft.pending_user_decisions:
        raise ValueError("confirmed protocol has non-empty pending_user_decisions")
    path_blockers = [f"{b.category}:{b.path}" for b in draft.evidence_blockers]
    overall_resolved = compute_tranche_v2_overall_resolved(
        evidence_blockers=draft.evidence_blockers,
        status=draft.status,
        ready_for_scoring=draft.ready_for_scoring,
        ready_for_backtest=draft.ready_for_backtest,
        ready_for_trading=draft.ready_for_trading,
    )
    return TrancheEvaluationProtocolVerificationResult(
        protocol_id=draft.protocol_id or compute_protocol_id(draft),
        schema_version="2",
        protocol_version=draft.protocol_version,
        status=draft.status,
        structural_ok=True,
        research_trial_ledger_id=draft.research_trial_ledger_id,
        research_trial_ledger_path=draft.research_trial_ledger_path,
        research_trial_ledger_binding_ok=False,
        two_layer_decision_contract_id=draft.two_layer_decision_contract_id,
        two_layer_decision_contract_path=draft.two_layer_decision_contract_path,
        two_layer_decision_contract_binding_ok=False,
        layer_one_index_protocol_id=draft.layer_one_index_protocol_id,
        layer_one_index_protocol_path=draft.layer_one_index_protocol_path,
        layer_one_index_protocol_binding_ok=False,
        resolved=overall_resolved,
        user_decisions_resolved=True,
        pending_user_decision_count=0,
        pending_user_decisions=[],
        blockers=path_blockers,
        evidence_blockers=list(draft.evidence_blockers),
        windows_overlap=False,
        consumed_oos_reuse_check_ok=False,
    )


def _assert_repo_relative_path(
    value: str,
    *,
    repo_root: Path,
    expected: str,
    field_name: str,
) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError(f"{field_name} must be a relative path without parent traversal")
    if value != expected:
        raise ValueError(f"{field_name} does not match bound path")
    resolved = (repo_root / path).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"{field_name} escapes repository root") from exc
    if not resolved.is_file():
        raise ValueError(f"{field_name} does not exist: {value}")
    return resolved


def load_tranche_evaluation_protocol_draft(path: Path) -> TrancheEvaluationProtocolDocument:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("tranche evaluation protocol draft is missing or invalid") from exc
    if isinstance(payload, dict) and payload.get("schema_version") == "2" and "tranche_count" in payload:
        raise ValueError(
            "schema v2 rejects tranche_count; 40 is the holding/phase cycle length, not an active tranche count"
        )
    version = payload.get("schema_version")
    try:
        if version == "1":
            return TrancheEvaluationProtocolDraftV1.model_validate(payload)
        if version == "2":
            return TrancheEvaluationProtocolV2.model_validate(payload)
    except Exception as exc:
        raise ValueError("tranche evaluation protocol draft is missing or invalid") from exc
    raise ValueError(f"unsupported tranche evaluation protocol schema_version: {version!r}")


def verify_tranche_evaluation_protocol_draft_file(
    *,
    protocol_path: Path,
    repo_root: Path,
    reference_date: date | None = None,
) -> tuple[TrancheEvaluationProtocolDocument, TrancheEvaluationProtocolVerificationResult]:
    root = Path(repo_root).resolve()
    draft = load_tranche_evaluation_protocol_draft(protocol_path)
    structural = verify_tranche_evaluation_protocol_draft(draft, reference_date=reference_date)
    ledger_path = _assert_repo_relative_path(
        draft.research_trial_ledger_path,
        repo_root=root,
        expected=BOUND_RESEARCH_TRIAL_LEDGER_PATH,
        field_name="research_trial_ledger_path",
    )
    ledger, _summary = verify_research_trial_ledger(ledger_path=ledger_path, repo_root=root)
    if ledger.ledger_id != draft.research_trial_ledger_id:
        raise ValueError("research trial ledger_id does not match protocol research_trial_ledger_id")
    if ledger.ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
        raise ValueError("research trial ledger_id does not match bound research trial ledger")
    assert_no_consumed_oos_binding(draft, ledger)

    updates: dict[str, Any] = {
        "research_trial_ledger_binding_ok": True,
        "consumed_oos_reuse_check_ok": True,
    }

    if isinstance(draft, TrancheEvaluationProtocolV2):
        contract_path = _assert_repo_relative_path(
            draft.two_layer_decision_contract_path,
            repo_root=root,
            expected=BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
            field_name="two_layer_decision_contract_path",
        )
        contract = load_two_layer_decision_draft(contract_path)
        contract_result = verify_two_layer_decision_draft(contract, reference_date=reference_date)
        if contract_result.schema_version != "2":
            raise ValueError("bound two-layer decision contract must be schema version 2")
        if contract_result.contract_id != draft.two_layer_decision_contract_id:
            raise ValueError(
                "two-layer decision contract_id on disk does not match protocol two_layer_decision_contract_id"
            )
        if contract_result.contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
            raise ValueError("two-layer decision contract_id on disk does not match bound constant")
        if str(DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH) != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
            raise ValueError("two-layer decision contract default path drifted from protocol binding")
        updates["two_layer_decision_contract_binding_ok"] = True

        layer_one_path = _assert_repo_relative_path(
            draft.layer_one_index_protocol_path,
            repo_root=root,
            expected=BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH,
            field_name="layer_one_index_protocol_path",
        )
        layer_one = load_layer_one_index_protocol_draft(layer_one_path)
        layer_one_result = verify_layer_one_index_protocol_draft(layer_one, reference_date=reference_date)
        if layer_one_result.schema_version != "2":
            raise ValueError("bound layer-one index protocol must be schema version 2")
        if layer_one_result.protocol_id != draft.layer_one_index_protocol_id:
            raise ValueError("layer-one index protocol_id on disk does not match protocol layer_one_index_protocol_id")
        if layer_one_result.protocol_id != BOUND_LAYER_ONE_INDEX_PROTOCOL_ID:
            raise ValueError("layer-one index protocol_id on disk does not match bound constant")
        if str(DEFAULT_LAYER_ONE_INDEX_PROTOCOL_DRAFT_PATH) != BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH:
            raise ValueError("layer-one index protocol default path drifted from protocol binding")
        updates["layer_one_index_protocol_binding_ok"] = True

    result = structural.model_copy(update=updates)
    return draft, result


def write_tranche_evaluation_protocol_draft(
    path: Path,
    draft: TrancheEvaluationProtocolDocument,
) -> TrancheEvaluationProtocolDocument:
    sealed = seal_tranche_evaluation_protocol_draft(draft)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_LAYER_ONE_INDEX_PROTOCOL_ID",
    "BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH",
    "BOUND_RESEARCH_TRIAL_LEDGER_ID",
    "BOUND_RESEARCH_TRIAL_LEDGER_PATH",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_ID",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_PATH",
    "CONFIRMED_CASH_OCCUPANCY_CAUSES",
    "CONFIRMED_FACTOR_EVIDENCE_METHODS",
    "CONFIRMED_INITIAL_CASH",
    "CONFIRMED_NEW_FROZEN_OOS_START",
    "DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH",
    "REQUIRED_TRANCHE_EVIDENCE_BLOCKERS",
    "REQUIRED_TRANCHE_EVIDENCE_BLOCKER_PATHS",
    "REQUIRED_TRANCHE_PROTOCOL_DECISION_PATHS",
    "TRANCHE_EVALUATION_PROTOCOL_SCHEMA_VERSION",
    "TRANCHE_EVALUATION_PROTOCOL_SCHEMA_VERSION_V1",
    "TRANCHE_EVALUATION_PROTOCOL_SCHEMA_VERSION_V2",
    "TRANCHE_EVALUATION_PROTOCOL_VERSION",
    "TRANCHE_EVALUATION_PROTOCOL_VERSION_V1",
    "TRANCHE_EVALUATION_PROTOCOL_VERSION_V2",
    "ConfirmedTrancheCapital",
    "ConsumedOosReusePolicy",
    "DateWindow",
    "NewFrozenOosPlan",
    "ProtocolEvidenceBlocker",
    "ResearchWindowsPending",
    "TrancheEvaluationProtocolDraft",
    "TrancheEvaluationProtocolDraftV1",
    "TrancheEvaluationProtocolV2",
    "TrancheEvaluationProtocolVerificationResult",
    "TrancheResearchWindowsConfirmed",
    "assert_bound_research_trial_ledger_id",
    "assert_bound_upstream_ids",
    "assert_no_consumed_oos_binding",
    "assert_no_future_tranche_windows",
    "assert_no_window_overlap",
    "assert_protocol_self_hash",
    "assert_status_ready_consistency",
    "build_confirmed_tranche_evaluation_protocol_v2",
    "build_unresolved_tranche_evaluation_protocol_draft",
    "canonical_protocol_bytes",
    "canonical_protocol_payload",
    "collect_protocol_decision_blockers",
    "compute_protocol_id",
    "compute_tranche_v2_overall_resolved",
    "default_tranche_evidence_blockers",
    "load_tranche_evaluation_protocol_draft",
    "migrate_tranche_evaluation_protocol_v1_to_v2",
    "seal_tranche_evaluation_protocol_draft",
    "verify_tranche_evaluation_protocol_draft",
    "verify_tranche_evaluation_protocol_draft_file",
    "windows_overlap",
    "write_tranche_evaluation_protocol_draft",
]
