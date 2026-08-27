"""Layer-one index development protocol: v1 (legacy sealed) + v2 (confirmed not-ready).

Schema v1 unresolved drafts remain verifiable. Schema v2 records user-confirmed
economic / evaluation choices with categorized evidence blockers and status
confirmed_for_implementation_but_not_ready. Ready/auto flags stay false.

File verification binds the research trial ledger and the two-layer decision
contract from disk. Does not invent index symbols, run experiments, implement a
regime engine, or reuse consumed OOS.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.experiment_ledger import (
    ResearchTrialLedger,
    verify_research_trial_ledger,
)
from app.research.two_layer_contract import (
    BOUND_RESEARCH_TRIAL_LEDGER_ID,
    BOUND_RESEARCH_TRIAL_LEDGER_PATH,
    CONTRACT_CONFIRMATION_AS_OF,
    DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH,
    AccountDrawdownPolicyConfirmed,
    DeploymentUpgradePolicyConfirmed,
    IndexDrawdownPolicyConfirmed,
    IndexIdentityConfirmed,
    LayerOneAdjustmentPolicyConfirmed,
    TrendPolicyConfirmed,
    VolatilityPolicyConfirmed,
    _reject_blank_string,
    _require_exact_float,
    _validate_ledger_path_field,
    compute_two_layer_v2_overall_resolved,
    load_two_layer_decision_draft,
    verify_two_layer_decision_draft,
)

LAYER_ONE_INDEX_PROTOCOL_SCHEMA_VERSION_V1: Literal["1"] = "1"
LAYER_ONE_INDEX_PROTOCOL_SCHEMA_VERSION_V2: Literal["2"] = "2"
LAYER_ONE_INDEX_PROTOCOL_SCHEMA_VERSION = LAYER_ONE_INDEX_PROTOCOL_SCHEMA_VERSION_V2
LAYER_ONE_INDEX_PROTOCOL_VERSION_V1: Literal["layer-one-index-development-protocol-draft-v1"] = (
    "layer-one-index-development-protocol-draft-v1"
)
LAYER_ONE_INDEX_PROTOCOL_VERSION_V2: Literal["layer-one-index-development-protocol-v2"] = (
    "layer-one-index-development-protocol-v2"
)
LAYER_ONE_INDEX_PROTOCOL_VERSION = LAYER_ONE_INDEX_PROTOCOL_VERSION_V2
DEFAULT_LAYER_ONE_INDEX_PROTOCOL_DRAFT_PATH = Path(
    "config/research/layer-one-index-development-protocol-draft-v1.json"
)
BOUND_TWO_LAYER_DECISION_CONTRACT_PATH: Literal["config/research/two-layer-strategy-decision-draft-v1.json"] = (
    "config/research/two-layer-strategy-decision-draft-v1.json"
)
# Disk-bound schema-v2 contract_id; must match the sealed two-layer contract on disk.
BOUND_TWO_LAYER_DECISION_CONTRACT_ID = "27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6"

ProtocolStatusV1 = Literal["blocked_pending_user_decisions"]
ProtocolStatusV2 = Literal["confirmed_for_implementation_but_not_ready"]
IndexReturnDefinition = Literal["total_return", "price_index"]
WindowRole = Literal[
    "development",
    "historical_validation",
    "seen_robustness_check_only",
    "consumed_oos",
]
ProtocolBlockerCategory = Literal[
    "pending_user_decision",
    "pending_factual_source_verification",
    "pending_implementation",
    "pending_development_evidence",
    "future_oos_observation",
    "future_enhancement",
]

REQUIRED_PROTOCOL_DECISION_PATHS: tuple[str, ...] = (
    "index.source",
    "index.symbol",
    "index.return_definition",
    "windows.development",
    "windows.validation_oos",
    "lookbacks.trend_lookback_bars",
    "lookbacks.volatility_lookback_bars",
    "lookbacks.drawdown_lookback_bars",
    "annualization_trading_days_per_year",
    "trend_thresholds",
    "volatility_target_or_risk_budget_mapping",
    "risk_budget_levels",
    "rebalance_frequency_phase_policy",
    "benchmark",
    "cost_assumptions",
    "go_no_go_metrics",
)

CONFIRMED_DEVELOPMENT_START = date(2005, 1, 1)
CONFIRMED_DEVELOPMENT_END = date(2012, 12, 31)
CONFIRMED_VALIDATION_SEGMENTS: tuple[tuple[date, date], ...] = (
    (date(2013, 1, 1), date(2016, 12, 31)),
    (date(2017, 1, 1), date(2019, 12, 31)),
    (date(2020, 1, 1), date(2021, 12, 31)),
)
CONFIRMED_SEEN_ROBUSTNESS_START = date(2022, 1, 1)
CONFIRMED_SEEN_ROBUSTNESS_END = date(2024, 12, 31)
CONFIRMED_CONSUMED_OOS_START = date(2025, 1, 1)
CONFIRMED_CONSUMED_OOS_END = date(2026, 8, 21)
CONFIRMED_NEW_FROZEN_OOS_START = date(2026, 8, 22)
CONFIRMED_NEW_FROZEN_OOS_PLANNED_MONTHS: Literal[12] = 12
CONFIRMED_RISK_BUDGET_LEVELS: list[float] = [0.0, 0.3, 0.6, 0.9]
CONFIRMED_LOOKBACKS: dict[str, int] = {
    "trend_lookback_bars": 200,
    "volatility_lookback_bars": 60,
    "drawdown_lookback_bars": 242,
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _parse_iso_date(value: object, *, field_name: str) -> date:
    if isinstance(value, date) and type(value) is date:
        return value
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be an ISO date")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


class ProtocolEvidenceBlocker(_StrictModel):
    path: str = Field(min_length=1)
    category: ProtocolBlockerCategory
    detail: str = Field(min_length=1)

    @field_validator("path", "detail", mode="before")
    @classmethod
    def _reject_blank(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)


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


class LabeledDateWindow(_StrictModel):
    start: date
    end: date
    role: WindowRole
    tunable_parameter_window: Literal[False] = False

    @field_validator("start", "end", mode="before")
    @classmethod
    def _parse_dates(cls, value: object, info: Any) -> date:
        return _parse_iso_date(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _ordered(self) -> LabeledDateWindow:
        if self.end < self.start:
            raise ValueError("window end must be on or after start")
        if self.tunable_parameter_window:
            raise ValueError("validation/robustness windows must never be tunable parameter windows")
        return self


class IndexIdentityPending(_StrictModel):
    source: str | None = None
    symbol: str | None = None
    return_definition: IndexReturnDefinition | None = None

    @field_validator("source", "symbol", mode="before")
    @classmethod
    def _reject_blank(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)


class ResearchWindowsPending(_StrictModel):
    development: DateWindow | None = None
    validation_oos: DateWindow | None = None


class LookbacksPending(_StrictModel):
    trend_lookback_bars: int | None = None
    volatility_lookback_bars: int | None = None
    drawdown_lookback_bars: int | None = None

    @field_validator("trend_lookback_bars", "drawdown_lookback_bars")
    @classmethod
    def _positive(cls, value: int | None, info: Any) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise ValueError(f"{info.field_name} must be >= 1")
        return value

    @field_validator("volatility_lookback_bars")
    @classmethod
    def _vol_lookback(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 2:
            raise ValueError("volatility_lookback_bars must be >= 2")
        return value


class GoNoGoMetricsPending(_StrictModel):
    primary_metric: str | None = None
    secondary_metrics: list[str] | None = None
    require_per_regime_occupancy: bool | None = None
    require_regime_transition_counts: bool | None = None
    notes: str | None = None

    @field_validator("primary_metric", "notes", mode="before")
    @classmethod
    def _reject_blank(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)

    @field_validator("secondary_metrics", mode="before")
    @classmethod
    def _reject_unknown_list_masquerade(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError("secondary_metrics must be a list or null")
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str) or item.strip() == "":
                raise ValueError("secondary_metrics entries must be non-empty strings")
            cleaned.append(item)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("secondary_metrics entries must be unique")
        return cleaned

    @model_validator(mode="after")
    def _require_regime_diagnostics_when_resolved(self) -> GoNoGoMetricsPending:
        any_set = any(
            value is not None
            for value in (
                self.primary_metric,
                self.secondary_metrics,
                self.require_per_regime_occupancy,
                self.require_regime_transition_counts,
                self.notes,
            )
        )
        if not any_set:
            return self
        if self.primary_metric is None:
            raise ValueError("go_no_go_metrics.primary_metric is required when go_no_go_metrics is set")
        if self.require_per_regime_occupancy is not True:
            raise ValueError("go_no_go_metrics.require_per_regime_occupancy must be true when set")
        if self.require_regime_transition_counts is not True:
            raise ValueError("go_no_go_metrics.require_regime_transition_counts must be true when set")
        return self


class ConsumedOosReusePolicy(_StrictModel):
    reuse_forbidden: Literal[True] = True
    note: str = (
        "Consumed one-shot OOS windows and receipts are terminal. Layer-one index "
        "development must not bind to them or reuse their evaluation windows."
    )


class LayerOneIndexDevelopmentProtocolDraftV1(_StrictModel):
    schema_version: Literal["1"] = LAYER_ONE_INDEX_PROTOCOL_SCHEMA_VERSION_V1
    protocol_version: Literal["layer-one-index-development-protocol-draft-v1"] = (
        LAYER_ONE_INDEX_PROTOCOL_VERSION_V1
    )
    status: ProtocolStatusV1 = "blocked_pending_user_decisions"
    research_trial_ledger_id: str = Field(min_length=1)
    research_trial_ledger_path: Literal["config/research/research-trial-ledger-v1.json"] = (
        BOUND_RESEARCH_TRIAL_LEDGER_PATH
    )
    consumed_oos: ConsumedOosReusePolicy = Field(default_factory=ConsumedOosReusePolicy)
    index: IndexIdentityPending
    windows: ResearchWindowsPending
    lookbacks: LookbacksPending
    annualization_trading_days_per_year: int | None = None
    trend_thresholds: dict[str, float] | None = None
    volatility_target_or_risk_budget_mapping: dict[str, Any] | None = None
    risk_budget_levels: list[float] | None = None
    rebalance_frequency_phase_policy: str | None = None
    benchmark: str | None = None
    cost_assumptions: dict[str, Any] | None = None
    go_no_go_metrics: GoNoGoMetricsPending | None = None
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

    @field_validator("rebalance_frequency_phase_policy", "benchmark", mode="before")
    @classmethod
    def _reject_blank_text(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)

    @field_validator("annualization_trading_days_per_year")
    @classmethod
    def _validate_annualization(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1:
            raise ValueError("annualization_trading_days_per_year must be >= 1")
        return value

    @field_validator("trend_thresholds")
    @classmethod
    def _validate_trend_thresholds(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is None:
            return None
        if len(value) == 0:
            raise ValueError("trend_thresholds must be null when unknown, not empty object")
        cleaned: dict[str, float] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or key.strip() == "":
                raise ValueError("trend_thresholds keys must be non-empty strings")
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError("trend_thresholds values must be finite")
            cleaned[key] = number
        return cleaned

    @field_validator("volatility_target_or_risk_budget_mapping", "cost_assumptions")
    @classmethod
    def _validate_mapping(cls, value: dict[str, Any] | None, info: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if len(value) == 0:
            raise ValueError(f"{info.field_name} must be null when unknown, not empty object")
        return value

    @field_validator("risk_budget_levels")
    @classmethod
    def _validate_risk_budget_levels(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return None
        if len(value) < 2:
            raise ValueError("risk_budget_levels must contain at least two levels when set")
        cleaned: list[float] = []
        for level in value:
            number = float(level)
            if not math.isfinite(number):
                raise ValueError("risk_budget_levels entries must be finite")
            if not (0.0 <= number <= 1.0):
                raise ValueError("risk_budget_levels entries must be in [0, 1]")
            cleaned.append(number)
        for previous, current in zip(cleaned, cleaned[1:], strict=False):
            if not current > previous:
                raise ValueError("risk_budget_levels must be strictly increasing")
        return cleaned

    @model_validator(mode="after")
    def _gate_flags(self) -> LayerOneIndexDevelopmentProtocolDraftV1:
        if self.status != "blocked_pending_user_decisions":
            raise ValueError("protocol status must remain blocked_pending_user_decisions")
        if self.ready_for_scoring or self.ready_for_backtest or self.ready_for_trading or self.auto_apply:
            raise ValueError("layer-one index protocol cannot authorize scoring, backtest, trading, or auto-apply")
        if not self.consumed_oos.reuse_forbidden:
            raise ValueError("consumed OOS reuse must remain forbidden")
        if self.bound_consumed_oos_receipt_path is not None:
            raise ValueError("bound_consumed_oos_receipt_path must remain null")
        if self.bound_consumed_oos_freeze_id is not None:
            raise ValueError("bound_consumed_oos_freeze_id must remain null")
        if self.bound_consumed_oos_authorization_id is not None:
            raise ValueError("bound_consumed_oos_authorization_id must remain null")
        return self


LayerOneIndexDevelopmentProtocolDraft = LayerOneIndexDevelopmentProtocolDraftV1


class NewFrozenOosPlan(_StrictModel):
    start: date = CONFIRMED_NEW_FROZEN_OOS_START
    planned_duration_months: Literal[12] = CONFIRMED_NEW_FROZEN_OOS_PLANNED_MONTHS
    continuous_recording_required: Literal[True] = True
    not_hard_prerequisite_for_60_or_90_unlock: Literal[True] = True
    complete_oos_pass_claim_forbidden_before_maturity: Literal[True] = True
    note: str = (
        "New frozen OOS begins 2026-08-22 with a planned 12-month continuous record. "
        "It is not a hard prerequisite for manual 60%/90% unlock, but a complete OOS "
        "pass claim is forbidden before maturity. Distinct from consumed OOS 2025-01-01..2026-08-21."
    )

    @field_validator("start", mode="before")
    @classmethod
    def _parse_start(cls, value: object) -> date:
        return _parse_iso_date(value, field_name="start")

    @model_validator(mode="after")
    def _freeze_start(self) -> NewFrozenOosPlan:
        if self.start != CONFIRMED_NEW_FROZEN_OOS_START:
            raise ValueError("new_frozen_oos.start must remain 2026-08-22")
        return self


class LayerOneResearchWindowsConfirmed(_StrictModel):
    development: LabeledDateWindow
    historical_validation_segments: list[LabeledDateWindow]
    seen_robustness_check_only: LabeledDateWindow
    consumed_oos: LabeledDateWindow
    new_frozen_oos: NewFrozenOosPlan = Field(default_factory=NewFrozenOosPlan)
    note: str = (
        "Historical validation segments are never tunable parameter windows and must not "
        "be called OOS. 2022-2024 is seen robustness only. Consumed OOS 2025-01-01..2026-08-21 "
        "must not be reused. New frozen OOS starts 2026-08-22 and is not the consumed window."
    )

    @model_validator(mode="after")
    def _validate_roles_and_order(self) -> LayerOneResearchWindowsConfirmed:
        if self.development.role != "development":
            raise ValueError("development.role must be development")
        if (
            self.development.start != CONFIRMED_DEVELOPMENT_START
            or self.development.end != CONFIRMED_DEVELOPMENT_END
        ):
            raise ValueError("development window must be 2005-01-01..2012-12-31")
        if self.seen_robustness_check_only.role != "seen_robustness_check_only":
            raise ValueError("seen_robustness_check_only.role mismatch")
        if (
            self.seen_robustness_check_only.start != CONFIRMED_SEEN_ROBUSTNESS_START
            or self.seen_robustness_check_only.end != CONFIRMED_SEEN_ROBUSTNESS_END
        ):
            raise ValueError("seen_robustness_check_only must be 2022-01-01..2024-12-31")
        if self.consumed_oos.role != "consumed_oos":
            raise ValueError("consumed_oos.role mismatch")
        if (
            self.consumed_oos.start != CONFIRMED_CONSUMED_OOS_START
            or self.consumed_oos.end != CONFIRMED_CONSUMED_OOS_END
        ):
            raise ValueError("consumed_oos must be 2025-01-01..2026-08-21")
        if len(self.historical_validation_segments) != len(CONFIRMED_VALIDATION_SEGMENTS):
            raise ValueError("historical_validation_segments count mismatch")
        for segment, expected in zip(
            self.historical_validation_segments, CONFIRMED_VALIDATION_SEGMENTS, strict=True
        ):
            if segment.role != "historical_validation":
                raise ValueError("validation segment role must be historical_validation (not OOS)")
            if "oos" in segment.role.lower():
                raise ValueError("historical validation segments must not be labeled OOS")
            if segment.tunable_parameter_window:
                raise ValueError("validation segments must not be tunable parameter windows")
            if segment.start != expected[0] or segment.end != expected[1]:
                raise ValueError(
                    f"historical validation segment must be {expected[0].isoformat()}.."
                    f"{expected[1].isoformat()}"
                )
        if self.new_frozen_oos.start <= self.consumed_oos.end:
            raise ValueError("new_frozen_oos.start must be after consumed_oos.end")
        if self.new_frozen_oos.start != CONFIRMED_NEW_FROZEN_OOS_START:
            raise ValueError("new_frozen_oos.start must remain 2026-08-22")
        return self


class LayerOneHardGatesConfirmed(_StrictModel):
    max_drawdown_floor_per_validation_segment_and_combined: float = -0.20
    combined_validation_after_cost_annualized_return_must_be_positive: Literal[True] = True
    calmar_min: float = 0.5
    baseline: dict[str, Any] = Field(
        default_factory=lambda: {
            "composition": "0.9_csi_all_share_total_return_plus_0.1_cash",
            "max_drawdown_amplitude_improvement_min": 0.25,
            "if_baseline_cagr_positive_retain_at_least": 0.60,
        }
    )
    stress_max_drawdown_must_not_breach: float = -0.20
    budget_level_occupancy_diagnostic_only: Literal[True] = True
    occupancy_not_a_post_hoc_hard_gate: Literal[True] = True
    require_per_regime_occupancy_diagnostic: Literal[True] = True
    require_regime_transition_counts_diagnostic: Literal[True] = True
    primary_metric: Literal["max_drawdown_and_calmar_vs_baseline"] = "max_drawdown_and_calmar_vs_baseline"

    @model_validator(mode="after")
    def _freeze_baseline(self) -> LayerOneHardGatesConfirmed:
        self.max_drawdown_floor_per_validation_segment_and_combined = _require_exact_float(
            self.max_drawdown_floor_per_validation_segment_and_combined,
            -0.20,
            field_name="max_drawdown_floor_per_validation_segment_and_combined",
        )
        self.calmar_min = _require_exact_float(self.calmar_min, 0.5, field_name="calmar_min")
        self.stress_max_drawdown_must_not_breach = _require_exact_float(
            self.stress_max_drawdown_must_not_breach,
            -0.20,
            field_name="stress_max_drawdown_must_not_breach",
        )
        composition = self.baseline.get("composition")
        if composition != "0.9_csi_all_share_total_return_plus_0.1_cash":
            raise ValueError("hard-gate baseline composition must remain 90/10 CSI All-Share TR + cash")
        improvement = float(self.baseline["max_drawdown_amplitude_improvement_min"])
        _require_exact_float(improvement, 0.25, field_name="max_drawdown_amplitude_improvement_min")
        retain = float(self.baseline["if_baseline_cagr_positive_retain_at_least"])
        _require_exact_float(retain, 0.60, field_name="if_baseline_cagr_positive_retain_at_least")
        return self


class LayerOneBudgetCompositionConfirmed(_StrictModel):
    final_budget_rule: Literal[
        "min_of_trend_base_vol_cap_index_dd_cap_account_dd_cap"
    ] = "min_of_trend_base_vol_cap_index_dd_cap_account_dd_cap"
    allowed_levels: list[float] = Field(default_factory=lambda: list(CONFIRMED_RISK_BUDGET_LEVELS))
    risk_lock_has_priority: Literal[True] = True
    cash_only_defensive_asset: Literal[True] = True
    max_stock_budget: float = 0.9

    @model_validator(mode="after")
    def _freeze_levels(self) -> LayerOneBudgetCompositionConfirmed:
        if self.allowed_levels != CONFIRMED_RISK_BUDGET_LEVELS:
            raise ValueError("allowed budget levels must remain [0.0, 0.3, 0.6, 0.9]")
        self.max_stock_budget = _require_exact_float(self.max_stock_budget, 0.9, field_name="max_stock_budget")
        return self


class LayerOneDecisionTimingConfirmed(_StrictModel):
    features_use_data_available_after_close_only: Literal[True] = True
    action_contract: Literal["T+1"] = "T+1"
    missing_or_late_bars_fail_closed: Literal[True] = True


class LayerOneCostAssumptionsProtocol(_StrictModel):
    """Protocol cost block; stamp schedule completion via flat historical rate is forbidden."""

    base_commission_per_side: float = 0.00025
    minimum_commission_cny: float = 5.0
    base_slippage_bps_per_side: Literal[5] = 5
    stress_slippage_bps_per_side: Literal[15] = 15
    stamp_tax: Literal["official_historical_sell_side_schedule"] = "official_historical_sell_side_schedule"
    stamp_tax_schedule_status: Literal["pending_factual_source_verification"] = (
        "pending_factual_source_verification"
    )
    stamp_tax_note: str = (
        "Official full historical sell-side stamp-tax timetable remains pending factual "
        "source verification. A simplified flat 0.1%-since-1900 schedule must not be "
        "treated as complete."
    )
    stress_must_not_breach_max_drawdown: float = -0.20

    @model_validator(mode="after")
    def _freeze(self) -> LayerOneCostAssumptionsProtocol:
        self.base_commission_per_side = _require_exact_float(
            self.base_commission_per_side, 0.00025, field_name="base_commission_per_side"
        )
        self.minimum_commission_cny = _require_exact_float(
            self.minimum_commission_cny, 5.0, field_name="minimum_commission_cny"
        )
        self.stress_must_not_breach_max_drawdown = _require_exact_float(
            self.stress_must_not_breach_max_drawdown, -0.20, field_name="stress_must_not_breach_max_drawdown"
        )
        if self.stamp_tax_schedule_status != "pending_factual_source_verification":
            raise ValueError("stamp_tax_schedule_status must remain pending_factual_source_verification")
        if self.stamp_tax != "official_historical_sell_side_schedule":
            raise ValueError("flat stamp-tax schedules are forbidden; official historical schedule required")
        return self


def default_layer_one_evidence_blockers() -> list[ProtocolEvidenceBlocker]:
    return [
        ProtocolEvidenceBlocker(
            path="risk_state_index.symbol",
            category="pending_factual_source_verification",
            detail=(
                "CSI All-Share price-index exact Tushare/CSI symbol pending factual source "
                "verification; do not guess."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="performance_benchmark.symbol",
            category="pending_factual_source_verification",
            detail=(
                "CSI All-Share total-return exact Tushare/CSI symbol pending factual source "
                "verification; do not guess."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="cost_assumptions.stamp_tax_schedule",
            category="pending_factual_source_verification",
            detail=(
                "Official full historical sell-side stamp-tax timetable evidence incomplete; "
                "flat 0.1%-since-1900 must not be treated as done."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="regime_budget_engine",
            category="pending_implementation",
            detail="Confirmed trend/vol/drawdown/account-lock regime budget engine is not implemented.",
        ),
        ProtocolEvidenceBlocker(
            path="risk_lock_persistence_and_ui",
            category="pending_implementation",
            detail=(
                "Risk-lock persistence across restart and prominent UI/output annotation are "
                "contractual but not implemented."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="long_history_index_materializer",
            category="pending_implementation",
            detail="Long-history index bar materializer for confirmed windows is not implemented.",
        ),
        ProtocolEvidenceBlocker(
            path="hard_gates.segment_and_combined_results",
            category="pending_development_evidence",
            detail="Hard gates are confirmed as criteria; segment/combined validation results are not yet produced.",
        ),
        ProtocolEvidenceBlocker(
            path="windows.new_frozen_oos",
            category="future_oos_observation",
            detail=(
                "New frozen OOS from 2026-08-22 is incomplete; distinct from consumed OOS "
                "2025-01-01..2026-08-21. Complete OOS pass claim forbidden before maturity."
            ),
        ),
    ]


class LayerOneIndexDevelopmentProtocolV2(_StrictModel):
    schema_version: Literal["2"] = LAYER_ONE_INDEX_PROTOCOL_SCHEMA_VERSION_V2
    protocol_version: Literal["layer-one-index-development-protocol-v2"] = LAYER_ONE_INDEX_PROTOCOL_VERSION_V2
    status: ProtocolStatusV2 = "confirmed_for_implementation_but_not_ready"
    confirmation_as_of: date = CONTRACT_CONFIRMATION_AS_OF
    research_trial_ledger_id: str = Field(min_length=1)
    research_trial_ledger_path: Literal["config/research/research-trial-ledger-v1.json"] = (
        BOUND_RESEARCH_TRIAL_LEDGER_PATH
    )
    two_layer_decision_contract_id: str = Field(min_length=1)
    two_layer_decision_contract_path: Literal[
        "config/research/two-layer-strategy-decision-draft-v1.json"
    ] = BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
    consumed_oos: ConsumedOosReusePolicy = Field(default_factory=ConsumedOosReusePolicy)
    risk_state_index: IndexIdentityConfirmed
    performance_benchmark: IndexIdentityConfirmed
    windows: LayerOneResearchWindowsConfirmed
    lookbacks: dict[str, int]
    annualization_trading_days_per_year: Literal[242] = 242
    trend: TrendPolicyConfirmed = Field(default_factory=TrendPolicyConfirmed)
    volatility: VolatilityPolicyConfirmed = Field(default_factory=VolatilityPolicyConfirmed)
    index_drawdown: IndexDrawdownPolicyConfirmed = Field(default_factory=IndexDrawdownPolicyConfirmed)
    account_drawdown: AccountDrawdownPolicyConfirmed = Field(default_factory=AccountDrawdownPolicyConfirmed)
    budget_composition: LayerOneBudgetCompositionConfirmed = Field(
        default_factory=LayerOneBudgetCompositionConfirmed
    )
    adjustment_policy: LayerOneAdjustmentPolicyConfirmed = Field(
        default_factory=LayerOneAdjustmentPolicyConfirmed
    )
    risk_budget_levels: list[float]
    rebalance_frequency_phase_policy: Literal[
        "reduce_daily_increase_only_first_trading_day_of_week_prior_day_state_risk_lock_priority"
    ] = "reduce_daily_increase_only_first_trading_day_of_week_prior_day_state_risk_lock_priority"
    benchmark_baseline: dict[str, Any] = Field(
        default_factory=lambda: {"composition": "0.9_csi_all_share_total_return_plus_0.1_cash"}
    )
    cost_assumptions: LayerOneCostAssumptionsProtocol = Field(default_factory=LayerOneCostAssumptionsProtocol)
    hard_gates: LayerOneHardGatesConfirmed = Field(default_factory=LayerOneHardGatesConfirmed)
    deployment_upgrade: DeploymentUpgradePolicyConfirmed = Field(
        default_factory=DeploymentUpgradePolicyConfirmed
    )
    decision_timing: LayerOneDecisionTimingConfirmed = Field(default_factory=LayerOneDecisionTimingConfirmed)
    evidence_blockers: list[ProtocolEvidenceBlocker]
    bound_consumed_oos_receipt_path: None = None
    bound_consumed_oos_freeze_id: None = None
    bound_consumed_oos_authorization_id: None = None
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    protocol_id: str | None = None

    @field_validator("research_trial_ledger_id", "two_layer_decision_contract_id", mode="before")
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

    @field_validator("confirmation_as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, value: object) -> date:
        return _parse_iso_date(value, field_name="confirmation_as_of")

    @model_validator(mode="after")
    def _gate_flags(self) -> LayerOneIndexDevelopmentProtocolV2:
        if self.status != "confirmed_for_implementation_but_not_ready":
            raise ValueError("v2 protocol status must be confirmed_for_implementation_but_not_ready")
        if self.ready_for_scoring or self.ready_for_backtest or self.ready_for_trading or self.auto_apply:
            raise ValueError("layer-one index protocol cannot authorize scoring, backtest, trading, or auto-apply")
        if not self.consumed_oos.reuse_forbidden:
            raise ValueError("consumed OOS reuse must remain forbidden")
        if self.risk_budget_levels != CONFIRMED_RISK_BUDGET_LEVELS:
            raise ValueError("confirmed risk_budget_levels must be [0.0, 0.3, 0.6, 0.9]")
        if self.lookbacks != CONFIRMED_LOOKBACKS:
            raise ValueError(f"confirmed lookbacks must equal {CONFIRMED_LOOKBACKS}")
        if self.benchmark_baseline.get("composition") != "0.9_csi_all_share_total_return_plus_0.1_cash":
            raise ValueError("benchmark_baseline must remain 90% CSI All-Share total return + 10% cash")
        if (
            self.risk_state_index.role != "market_risk_state"
            or self.risk_state_index.return_definition != "price_index"
        ):
            raise ValueError("risk_state_index must be the price-index risk-state series")
        if (
            self.performance_benchmark.role != "performance_comparison"
            or self.performance_benchmark.return_definition != "total_return"
        ):
            raise ValueError("performance_benchmark must be total-return comparison series")
        if self.research_trial_ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
            raise ValueError("research_trial_ledger_id does not match bound research trial ledger")
        if self.two_layer_decision_contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
            raise ValueError("two_layer_decision_contract_id does not match bound two-layer contract")
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
        # Fail closed: flat stamp completion masquerade.
        if self.cost_assumptions.stamp_tax_schedule_status != "pending_factual_source_verification":
            raise ValueError("stamp schedule must remain pending factual; flat completion forbidden")
        return self


LayerOneIndexProtocolDocument = LayerOneIndexDevelopmentProtocolDraftV1 | LayerOneIndexDevelopmentProtocolV2


class LayerOneIndexProtocolVerificationResult(_StrictModel):
    protocol_id: str
    schema_version: Literal["1", "2"]
    protocol_version: str
    status: str
    research_trial_ledger_id: str
    research_trial_ledger_path: Literal["config/research/research-trial-ledger-v1.json"]
    research_trial_ledger_binding_ok: bool
    two_layer_decision_contract_id: str | None = None
    two_layer_decision_contract_path: str | None = None
    two_layer_decision_contract_binding_ok: bool = False
    resolved: bool
    user_decisions_resolved: bool
    pending_user_decision_count: int
    blockers: list[str]
    evidence_blockers: list[ProtocolEvidenceBlocker] = Field(default_factory=list)
    windows_overlap: bool
    consumed_oos_reuse_check_ok: bool
    consumed_oos_reuse_forbidden: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_score: Literal[True] = True
    does_not_backtest: Literal[True] = True
    does_not_trade: Literal[True] = True


def canonical_protocol_payload(draft: LayerOneIndexProtocolDocument) -> dict[str, Any]:
    return draft.model_dump(mode="json", exclude={"protocol_id"})


def canonical_protocol_bytes(draft: LayerOneIndexProtocolDocument) -> bytes:
    payload = canonical_protocol_payload(draft)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_protocol_id(draft: LayerOneIndexProtocolDocument) -> str:
    return hashlib.sha256(canonical_protocol_bytes(draft)).hexdigest()


def seal_layer_one_index_protocol_draft(
    draft: LayerOneIndexProtocolDocument,
) -> LayerOneIndexProtocolDocument:
    return draft.model_copy(update={"protocol_id": compute_protocol_id(draft)})


def build_unresolved_layer_one_index_protocol_draft(
    *,
    research_trial_ledger_id: str = BOUND_RESEARCH_TRIAL_LEDGER_ID,
    research_trial_ledger_path: Literal[
        "config/research/research-trial-ledger-v1.json"
    ] = BOUND_RESEARCH_TRIAL_LEDGER_PATH,
) -> LayerOneIndexDevelopmentProtocolDraftV1:
    draft = LayerOneIndexDevelopmentProtocolDraftV1(
        research_trial_ledger_id=research_trial_ledger_id,
        research_trial_ledger_path=research_trial_ledger_path,
        index=IndexIdentityPending(),
        windows=ResearchWindowsPending(),
        lookbacks=LookbacksPending(),
        go_no_go_metrics=None,
    )
    return seal_layer_one_index_protocol_draft(draft)  # type: ignore[return-value]


def build_confirmed_layer_one_index_protocol_v2(
    *,
    research_trial_ledger_id: str = BOUND_RESEARCH_TRIAL_LEDGER_ID,
    research_trial_ledger_path: Literal[
        "config/research/research-trial-ledger-v1.json"
    ] = BOUND_RESEARCH_TRIAL_LEDGER_PATH,
    two_layer_decision_contract_id: str = BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    two_layer_decision_contract_path: Literal[
        "config/research/two-layer-strategy-decision-draft-v1.json"
    ] = BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
    confirmation_as_of: date = CONTRACT_CONFIRMATION_AS_OF,
) -> LayerOneIndexDevelopmentProtocolV2:
    draft = LayerOneIndexDevelopmentProtocolV2(
        confirmation_as_of=confirmation_as_of,
        research_trial_ledger_id=research_trial_ledger_id,
        research_trial_ledger_path=research_trial_ledger_path,
        two_layer_decision_contract_id=two_layer_decision_contract_id,
        two_layer_decision_contract_path=two_layer_decision_contract_path,
        consumed_oos=ConsumedOosReusePolicy(
            note=(
                "Consumed one-shot OOS windows and receipts are terminal. Layer-one index "
                "development must not bind to them or reuse their evaluation windows. "
                "Do not confuse the consumed window with the new frozen OOS that begins later."
            )
        ),
        risk_state_index=IndexIdentityConfirmed(
            role="market_risk_state",
            name="csi_all_share_price_index",
            return_definition="price_index",
            symbol=None,
            symbol_status="pending_factual_source_verification",
            note=(
                "中证全指价格指数 for market risk-state features (price returns). "
                "Tushare primary market data; CSI official website identity cross-check. "
                "Exact symbol pending factual source verification; do not guess."
            ),
        ),
        performance_benchmark=IndexIdentityConfirmed(
            role="performance_comparison",
            name="csi_all_share_total_return",
            return_definition="total_return",
            symbol=None,
            symbol_status="pending_factual_source_verification",
            note=(
                "中证全指全收益 for performance comparison (total return). "
                "Tushare primary market data; CSI official website identity cross-check. "
                "Exact symbol pending factual source verification; do not guess."
            ),
        ),
        windows=LayerOneResearchWindowsConfirmed(
            development=LabeledDateWindow(
                start=CONFIRMED_DEVELOPMENT_START,
                end=CONFIRMED_DEVELOPMENT_END,
                role="development",
            ),
            historical_validation_segments=[
                LabeledDateWindow(start=start, end=end, role="historical_validation")
                for start, end in CONFIRMED_VALIDATION_SEGMENTS
            ],
            seen_robustness_check_only=LabeledDateWindow(
                start=CONFIRMED_SEEN_ROBUSTNESS_START,
                end=CONFIRMED_SEEN_ROBUSTNESS_END,
                role="seen_robustness_check_only",
            ),
            consumed_oos=LabeledDateWindow(
                start=CONFIRMED_CONSUMED_OOS_START,
                end=CONFIRMED_CONSUMED_OOS_END,
                role="consumed_oos",
            ),
            new_frozen_oos=NewFrozenOosPlan(),
        ),
        lookbacks=dict(CONFIRMED_LOOKBACKS),
        risk_budget_levels=list(CONFIRMED_RISK_BUDGET_LEVELS),
        evidence_blockers=default_layer_one_evidence_blockers(),
    )
    return seal_layer_one_index_protocol_draft(draft)  # type: ignore[return-value]


def migrate_layer_one_index_protocol_v1_to_v2(
    draft_v1: LayerOneIndexDevelopmentProtocolDraftV1,
    *,
    confirmation_as_of: date = CONTRACT_CONFIRMATION_AS_OF,
) -> LayerOneIndexDevelopmentProtocolV2:
    blockers = collect_protocol_decision_blockers_v1(draft_v1)
    if blockers and len(blockers) != len(REQUIRED_PROTOCOL_DECISION_PATHS):
        raise ValueError(
            "partially filled schema-v1 protocol cannot auto-migrate; provide an explicit confirmed v2 overlay"
        )
    if draft_v1.research_trial_ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
        raise ValueError("migration requires bound research trial ledger id")
    return build_confirmed_layer_one_index_protocol_v2(
        research_trial_ledger_id=draft_v1.research_trial_ledger_id,
        research_trial_ledger_path=draft_v1.research_trial_ledger_path,
        confirmation_as_of=confirmation_as_of,
    )


def _decision_value(draft: LayerOneIndexDevelopmentProtocolDraftV1, path: str) -> object:
    current: object = draft
    for part in path.split("."):
        current = getattr(current, part)
    return current


def collect_protocol_decision_blockers_v1(draft: LayerOneIndexDevelopmentProtocolDraftV1) -> list[str]:
    blockers: list[str] = []
    for path in REQUIRED_PROTOCOL_DECISION_PATHS:
        if _decision_value(draft, path) is None:
            blockers.append(path)
    return blockers


def collect_protocol_decision_blockers(draft: LayerOneIndexProtocolDocument) -> list[str]:
    if isinstance(draft, LayerOneIndexDevelopmentProtocolDraftV1):
        return collect_protocol_decision_blockers_v1(draft)
    return [
        f"{blocker.category}:{blocker.path}"
        for blocker in draft.evidence_blockers
        if blocker.category == "pending_user_decision"
    ]


def assert_protocol_self_hash(draft: LayerOneIndexProtocolDocument) -> None:
    if draft.protocol_id is None:
        raise ValueError("layer-one index protocol_id is missing")
    expected = compute_protocol_id(draft)
    if draft.protocol_id != expected:
        raise ValueError("layer-one index protocol_id does not match canonical content hash")


def assert_status_ready_consistency(draft: LayerOneIndexProtocolDocument) -> None:
    if draft.ready_for_scoring or draft.ready_for_backtest or draft.ready_for_trading or draft.auto_apply:
        raise ValueError("status/ready contradiction: ready flags must remain false")
    if isinstance(draft, LayerOneIndexDevelopmentProtocolDraftV1):
        if draft.status != "blocked_pending_user_decisions":
            raise ValueError("status/ready contradiction: v1 status must be blocked_pending_user_decisions")
    elif draft.status != "confirmed_for_implementation_but_not_ready":
        raise ValueError(
            "status/ready contradiction: v2 status must be confirmed_for_implementation_but_not_ready"
        )


def assert_bound_research_trial_ledger_id(draft: LayerOneIndexProtocolDocument) -> None:
    if draft.research_trial_ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
        raise ValueError("research_trial_ledger_id does not match bound research trial ledger")
    if draft.research_trial_ledger_path != BOUND_RESEARCH_TRIAL_LEDGER_PATH:
        raise ValueError("research_trial_ledger_path does not match bound research trial ledger path")


def assert_bound_two_layer_contract_id(draft: LayerOneIndexDevelopmentProtocolV2) -> None:
    if draft.two_layer_decision_contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
        raise ValueError("two_layer_decision_contract_id does not match bound two-layer contract")
    if draft.two_layer_decision_contract_path != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two_layer_decision_contract_path does not match bound two-layer contract path")


def windows_overlap(left: DateWindow | LabeledDateWindow, right: DateWindow | LabeledDateWindow) -> bool:
    return left.start <= right.end and right.start <= left.end


def assert_no_window_overlap(draft: LayerOneIndexProtocolDocument) -> None:
    if isinstance(draft, LayerOneIndexDevelopmentProtocolDraftV1):
        development = draft.windows.development
        validation = draft.windows.validation_oos
        if development is None or validation is None:
            return
        if windows_overlap(development, validation):
            raise ValueError("development and validation_oos windows must not overlap")
        return

    windows: list[LabeledDateWindow] = [
        draft.windows.development,
        *draft.windows.historical_validation_segments,
        draft.windows.seen_robustness_check_only,
        draft.windows.consumed_oos,
    ]
    for i, left in enumerate(windows):
        for right in windows[i + 1 :]:
            if windows_overlap(left, right):
                raise ValueError(
                    f"layer-one windows must not overlap: {left.role}[{left.start}..{left.end}] vs "
                    f"{right.role}[{right.start}..{right.end}]"
                )


def assert_no_future_protocol_windows(
    draft: LayerOneIndexDevelopmentProtocolV2,
    *,
    reference_date: date | None = None,
) -> None:
    as_of = reference_date or draft.confirmation_as_of
    for window in (
        draft.windows.development,
        *draft.windows.historical_validation_segments,
        draft.windows.seen_robustness_check_only,
        draft.windows.consumed_oos,
    ):
        if window.end > as_of:
            raise ValueError(f"{window.role} window end is after reference_date/confirmation_as_of")
    if draft.windows.new_frozen_oos.start > as_of + timedelta(days=3650):
        raise ValueError("new_frozen_oos.start unreasonably far in the future")


def assert_validation_not_labeled_oos(draft: LayerOneIndexDevelopmentProtocolV2) -> None:
    payload = json.dumps(canonical_protocol_payload(draft), ensure_ascii=False).lower()
    # Structural roles already forbid OOS labels; also reject narrative masquerade keys.
    if "validation_oos" in payload:
        raise ValueError("historical validation must not be labeled or keyed as validation_oos / OOS")
    for segment in draft.windows.historical_validation_segments:
        if segment.role != "historical_validation":
            raise ValueError("validation segment role must be historical_validation (not OOS)")


def _window_overlaps_consumed_oos(
    window: DateWindow | LabeledDateWindow,
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
    draft: LayerOneIndexProtocolDocument,
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

    if isinstance(draft, LayerOneIndexDevelopmentProtocolDraftV1):
        validation = draft.windows.validation_oos
        if validation is not None:
            hits = _window_overlaps_consumed_oos(validation, ledger)
            if hits:
                raise ValueError(
                    "validation_oos window overlaps consumed OOS evaluation window(s): " + ",".join(hits)
                )
        development = draft.windows.development
        if development is not None:
            hits = _window_overlaps_consumed_oos(development, ledger)
            if hits:
                raise ValueError(
                    "development window overlaps consumed OOS evaluation window(s): " + ",".join(hits)
                )
        return

    # v2: development / historical validation / robustness must not reuse consumed OOS.
    # The protocol's own consumed_oos label documents the terminal window; it is not reuse.
    # New frozen OOS is future observation and must not be confused with the consumed window.
    for window in (
        draft.windows.development,
        *draft.windows.historical_validation_segments,
        draft.windows.seen_robustness_check_only,
    ):
        hits = _window_overlaps_consumed_oos(window, ledger)
        if hits:
            raise ValueError(
                f"{window.role} window overlaps consumed OOS evaluation window(s): " + ",".join(hits)
            )


def compute_layer_one_v2_overall_resolved(
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


def verify_layer_one_index_protocol_draft(
    draft: LayerOneIndexProtocolDocument,
    *,
    reference_date: date | None = None,
) -> LayerOneIndexProtocolVerificationResult:
    assert_protocol_self_hash(draft)
    assert_status_ready_consistency(draft)
    assert_bound_research_trial_ledger_id(draft)
    assert_no_window_overlap(draft)

    if isinstance(draft, LayerOneIndexDevelopmentProtocolDraftV1):
        blockers = collect_protocol_decision_blockers_v1(draft)
        resolved = len(blockers) == 0
        if not resolved and draft.status != "blocked_pending_user_decisions":
            raise ValueError("unresolved protocol must keep status blocked_pending_user_decisions")
        return LayerOneIndexProtocolVerificationResult(
            protocol_id=draft.protocol_id or compute_protocol_id(draft),
            schema_version="1",
            protocol_version=draft.protocol_version,
            status=draft.status,
            research_trial_ledger_id=draft.research_trial_ledger_id,
            research_trial_ledger_path=draft.research_trial_ledger_path,
            research_trial_ledger_binding_ok=False,
            two_layer_decision_contract_id=None,
            two_layer_decision_contract_path=None,
            two_layer_decision_contract_binding_ok=False,
            resolved=resolved,
            user_decisions_resolved=resolved,
            pending_user_decision_count=len(blockers),
            blockers=blockers,
            evidence_blockers=[],
            windows_overlap=False,
            consumed_oos_reuse_check_ok=False,
        )

    assert_bound_two_layer_contract_id(draft)
    assert_no_future_protocol_windows(draft, reference_date=reference_date)
    assert_validation_not_labeled_oos(draft)
    if any(b.category == "pending_user_decision" for b in draft.evidence_blockers):
        raise ValueError("confirmed protocol has pending_user_decision blockers")
    path_blockers = [f"{b.category}:{b.path}" for b in draft.evidence_blockers]
    overall_resolved = compute_layer_one_v2_overall_resolved(
        evidence_blockers=draft.evidence_blockers,
        status=draft.status,
        ready_for_scoring=draft.ready_for_scoring,
        ready_for_backtest=draft.ready_for_backtest,
        ready_for_trading=draft.ready_for_trading,
    )
    return LayerOneIndexProtocolVerificationResult(
        protocol_id=draft.protocol_id or compute_protocol_id(draft),
        schema_version="2",
        protocol_version=draft.protocol_version,
        status=draft.status,
        research_trial_ledger_id=draft.research_trial_ledger_id,
        research_trial_ledger_path=draft.research_trial_ledger_path,
        research_trial_ledger_binding_ok=False,
        two_layer_decision_contract_id=draft.two_layer_decision_contract_id,
        two_layer_decision_contract_path=draft.two_layer_decision_contract_path,
        two_layer_decision_contract_binding_ok=False,
        resolved=overall_resolved,
        user_decisions_resolved=True,
        pending_user_decision_count=0,
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


def load_layer_one_index_protocol_draft(path: Path) -> LayerOneIndexProtocolDocument:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("layer-one index protocol draft is missing or invalid") from exc
    version = payload.get("schema_version")
    try:
        if version == "1":
            return LayerOneIndexDevelopmentProtocolDraftV1.model_validate(payload)
        if version == "2":
            return LayerOneIndexDevelopmentProtocolV2.model_validate(payload)
    except Exception as exc:
        raise ValueError("layer-one index protocol draft is missing or invalid") from exc
    raise ValueError(f"unsupported layer-one index protocol schema_version: {version!r}")


def verify_layer_one_index_protocol_draft_file(
    *,
    protocol_path: Path,
    repo_root: Path,
    reference_date: date | None = None,
) -> tuple[LayerOneIndexProtocolDocument, LayerOneIndexProtocolVerificationResult]:
    root = Path(repo_root).resolve()
    draft = load_layer_one_index_protocol_draft(protocol_path)
    structural = verify_layer_one_index_protocol_draft(draft, reference_date=reference_date)
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

    if isinstance(draft, LayerOneIndexDevelopmentProtocolV2):
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
        # Also require the bound ledger path constant used by the contract module.
        if str(DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH) != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
            raise ValueError("two-layer decision contract default path drifted from protocol binding")
        updates["two_layer_decision_contract_binding_ok"] = True

    result = structural.model_copy(update=updates)
    return draft, result


def write_layer_one_index_protocol_draft(
    path: Path,
    draft: LayerOneIndexProtocolDocument,
) -> LayerOneIndexProtocolDocument:
    sealed = seal_layer_one_index_protocol_draft(draft)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_RESEARCH_TRIAL_LEDGER_ID",
    "BOUND_RESEARCH_TRIAL_LEDGER_PATH",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_ID",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_PATH",
    "CONFIRMED_LOOKBACKS",
    "CONFIRMED_NEW_FROZEN_OOS_START",
    "CONFIRMED_RISK_BUDGET_LEVELS",
    "DEFAULT_LAYER_ONE_INDEX_PROTOCOL_DRAFT_PATH",
    "REQUIRED_PROTOCOL_DECISION_PATHS",
    "LAYER_ONE_INDEX_PROTOCOL_SCHEMA_VERSION",
    "LAYER_ONE_INDEX_PROTOCOL_SCHEMA_VERSION_V1",
    "LAYER_ONE_INDEX_PROTOCOL_SCHEMA_VERSION_V2",
    "LAYER_ONE_INDEX_PROTOCOL_VERSION",
    "LAYER_ONE_INDEX_PROTOCOL_VERSION_V1",
    "LAYER_ONE_INDEX_PROTOCOL_VERSION_V2",
    "ConsumedOosReusePolicy",
    "DateWindow",
    "GoNoGoMetricsPending",
    "IndexIdentityPending",
    "LabeledDateWindow",
    "LayerOneHardGatesConfirmed",
    "LayerOneIndexDevelopmentProtocolDraft",
    "LayerOneIndexDevelopmentProtocolDraftV1",
    "LayerOneIndexDevelopmentProtocolV2",
    "LayerOneIndexProtocolVerificationResult",
    "LayerOneResearchWindowsConfirmed",
    "LookbacksPending",
    "NewFrozenOosPlan",
    "ProtocolEvidenceBlocker",
    "ResearchWindowsPending",
    "assert_bound_research_trial_ledger_id",
    "assert_bound_two_layer_contract_id",
    "assert_no_consumed_oos_binding",
    "assert_no_future_protocol_windows",
    "assert_no_window_overlap",
    "assert_protocol_self_hash",
    "assert_status_ready_consistency",
    "assert_validation_not_labeled_oos",
    "build_confirmed_layer_one_index_protocol_v2",
    "build_unresolved_layer_one_index_protocol_draft",
    "canonical_protocol_bytes",
    "canonical_protocol_payload",
    "collect_protocol_decision_blockers",
    "compute_layer_one_v2_overall_resolved",
    "compute_protocol_id",
    "default_layer_one_evidence_blockers",
    "load_layer_one_index_protocol_draft",
    "migrate_layer_one_index_protocol_v1_to_v2",
    "seal_layer_one_index_protocol_draft",
    "verify_layer_one_index_protocol_draft",
    "verify_layer_one_index_protocol_draft_file",
    "windows_overlap",
    "write_layer_one_index_protocol_draft",
]
