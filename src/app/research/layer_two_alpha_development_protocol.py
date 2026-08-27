"""Layer-two alpha development evidence protocol (E11a).

Read-only freeze of the four high-is-good factor families, evidence gates,
development/robustness windows, and pre-freeze selection rules. Does not run
data, score, wire scoring, generate strategy YAML, backtest, or trade.

Upstream disk bindings (any drift fails file verification):
- research trial ledger
- two-layer decision contract
- tranche evaluation protocol
- layer-two allocation implementation protocol
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.experiment_ledger import (
    DEFAULT_RESEARCH_TRIAL_LEDGER_PATH,
    verify_research_trial_ledger,
)
from app.research.layer_two_allocation_protocol import (
    DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH,
    load_layer_two_allocation_protocol,
    verify_layer_two_allocation_protocol,
    verify_layer_two_allocation_protocol_file,
)
from app.research.tranche_evaluation_protocol import (
    DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH,
    load_tranche_evaluation_protocol_draft,
    verify_tranche_evaluation_protocol_draft,
    verify_tranche_evaluation_protocol_draft_file,
)
from app.research.two_layer_contract import (
    BOUND_RESEARCH_TRIAL_LEDGER_ID,
    BOUND_RESEARCH_TRIAL_LEDGER_PATH,
    DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH,
    StatisticalRiskClusterPolicyConfirmed,
    _reject_blank_string,
    _require_exact_float,
    load_two_layer_decision_draft,
    verify_two_layer_decision_draft,
    verify_two_layer_decision_draft_file,
)

BOUND_TWO_LAYER_DECISION_CONTRACT_PATH: Literal["config/research/two-layer-strategy-decision-draft-v1.json"] = (
    "config/research/two-layer-strategy-decision-draft-v1.json"
)
BOUND_TWO_LAYER_DECISION_CONTRACT_ID = "27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6"

LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_VERSION: Literal["layer-two-alpha-development-protocol-v1"] = (
    "layer-two-alpha-development-protocol-v1"
)
DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH = Path("config/research/layer-two-alpha-development-protocol-v1.json")

BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH: Literal["config/research/tranche-evaluation-protocol-draft-v1.json"] = (
    "config/research/tranche-evaluation-protocol-draft-v1.json"
)
BOUND_TRANCHE_EVALUATION_PROTOCOL_ID = "8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a"
BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_PATH: Literal[
    "config/research/layer-two-allocation-implementation-protocol-v1.json"
] = "config/research/layer-two-allocation-implementation-protocol-v1.json"
BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_ID = "0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"

CONFIRMED_FACTOR_FAMILIES: tuple[str, ...] = (
    "quality",
    "value",
    "medium_momentum_12_1",
    "defensive_low_vol",
)
CONFIRMATION_AS_OF = date(2026, 8, 26)
CONFIRMED_DEVELOPMENT_START = date(2022, 1, 1)
CONFIRMED_DEVELOPMENT_END = date(2023, 12, 31)
CONFIRMED_SEEN_ROBUSTNESS_START = date(2024, 1, 1)
CONFIRMED_SEEN_ROBUSTNESS_END = date(2024, 12, 31)
CONFIRMED_CONSUMED_OOS_START = date(2025, 1, 1)
CONFIRMED_CONSUMED_OOS_END = date(2026, 8, 21)
CONFIRMED_NEW_FROZEN_OOS_START = date(2026, 8, 22)

ProtocolStatus = Literal["confirmed_for_development_but_not_ready"]
ProtocolBlockerCategory = Literal[
    "pending_implementation",
    "pending_development_evidence",
    "future_enhancement",
]

REQUIRED_ALPHA_DEVELOPMENT_EVIDENCE_BLOCKERS: dict[str, ProtocolBlockerCategory] = {
    "factor_evidence_pipeline": "pending_implementation",
    "research_trial_ledger_four_hypothesis_family": "pending_implementation",
    "pit_industry_history": "future_enhancement",
    "alpha_weight_wiring": "pending_development_evidence",
}

REQUIRED_INFERENCE_REPORT_FIELDS: tuple[str, ...] = (
    "sample_count",
    "valid_dates",
    "ic",
    "spread",
    "hac_statistic",
    "hac_p_value",
    "holm_input_p_value",
    "holm_sorted_position",
    "holm_threshold",
    "holm_rejection",
)

HOLM_TIE_BREAK_FACTOR_FAMILY_ORDER: tuple[str, ...] = (
    "quality",
    "value",
    "medium_momentum_12_1",
    "defensive_low_vol",
)

CONFIRMED_SIZE_BANDS: tuple[tuple[str, float, float | None], ...] = (
    ("3bn_5bn", 3e9, 5e9),
    ("5bn_10bn", 5e9, 1e10),
    ("10bn_plus", 1e10, None),
)


def _require_real_number(
    value: object,
    *,
    field_name: str,
    minimum: float | None = 0.0,
    minimum_exclusive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a real number (bool rejected)")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite (NaN/Inf rejected)")
    if minimum is not None:
        if minimum_exclusive:
            if number <= minimum:
                raise ValueError(f"{field_name} must be > {minimum}")
        elif number < minimum:
            raise ValueError(f"{field_name} must be >= {minimum}")
    return number


def _require_literal_false(value: object, *, field_name: str) -> Literal[False]:
    if value is not False:
        raise ValueError(f"{field_name} must be false")
    return False


def _parse_iso_date(value: object, *, field_name: str) -> date:
    if isinstance(value, date) and type(value) is date:
        return value
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be an ISO date")
    return date.fromisoformat(value.strip())


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


class ProtocolDateWindow(_StrictModel):
    start: date
    end: date

    @field_validator("start", "end", mode="before")
    @classmethod
    def _parse(cls, value: object, info: Any) -> date:
        return _parse_iso_date(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _chronological(self) -> ProtocolDateWindow:
        if self.start > self.end:
            raise ValueError("date window start must be on or before end")
        return self


class AlphaResearchWindows(_StrictModel):
    development: ProtocolDateWindow
    seen_robustness: ProtocolDateWindow
    consumed_oos: ProtocolDateWindow
    new_frozen_oos_begins: date
    seen_robustness_report_only: Literal[True] = True
    seen_robustness_must_not_select_or_alter_weights: Literal[True] = True
    consumed_oos_forbidden: Literal[True] = True
    new_frozen_oos_cannot_be_evaluated_in_e11a: Literal[True] = True
    note: str = (
        "Development is 2022-01-01..2023-12-31. 2024 is seen robustness report-only and "
        "must not select or alter weights. Consumed OOS 2025-01-01..2026-08-21 is forbidden. "
        "New frozen OOS begins 2026-08-22 and cannot be evaluated in E11a."
    )

    @field_validator("new_frozen_oos_begins", mode="before")
    @classmethod
    def _parse_new_frozen(cls, value: object) -> date:
        return _parse_iso_date(value, field_name="new_frozen_oos_begins")

    @model_validator(mode="after")
    def _freeze_windows(self) -> AlphaResearchWindows:
        if self.development.start != CONFIRMED_DEVELOPMENT_START or self.development.end != CONFIRMED_DEVELOPMENT_END:
            raise ValueError("development window must be 2022-01-01..2023-12-31")
        if (
            self.seen_robustness.start != CONFIRMED_SEEN_ROBUSTNESS_START
            or self.seen_robustness.end != CONFIRMED_SEEN_ROBUSTNESS_END
        ):
            raise ValueError("seen_robustness window must be 2024-01-01..2024-12-31")
        if (
            self.consumed_oos.start != CONFIRMED_CONSUMED_OOS_START
            or self.consumed_oos.end != CONFIRMED_CONSUMED_OOS_END
        ):
            raise ValueError("consumed_oos window must be 2025-01-01..2026-08-21")
        if self.new_frozen_oos_begins != CONFIRMED_NEW_FROZEN_OOS_START:
            raise ValueError("new_frozen_oos_begins must remain 2026-08-22")
        assert_windows_non_overlapping(self)
        return self


class QualityComponent(_StrictModel):
    metric: str = Field(min_length=1)
    direction: Literal["high", "low"]


class QualityFundamentalTiming(_StrictModel):
    mode: Literal["strict_initial_as_announced"] = "strict_initial_as_announced"
    available_at_lte_decision_at: Literal[True] = True
    report_period_lte_as_of: Literal[True] = True
    max_report_age_days: Literal[550] = 550


class QualityFactorFormula(_StrictModel):
    family_id: Literal["quality"] = "quality"
    high_is_good: Literal[True] = True
    aggregation: Literal["equal_mean_of_cs_average_rank_percentiles"] = "equal_mean_of_cs_average_rank_percentiles"
    components: list[QualityComponent] = Field(
        default_factory=lambda: [
            QualityComponent(metric="roe", direction="high"),
            QualityComponent(metric="roic", direction="high"),
            QualityComponent(metric="grossprofit_margin", direction="high"),
            QualityComponent(metric="debt_to_assets", direction="low"),
            QualityComponent(metric="ocf_to_or", direction="high"),
        ]
    )
    min_known_components: Literal[3] = 3
    fundamental_timing: QualityFundamentalTiming = Field(default_factory=QualityFundamentalTiming)
    formula: Literal[
        "equal_mean_cs_average_rank_percentiles(roe high, roic high, grossprofit_margin high, "
        "debt_to_assets low, ocf_to_or high)"
    ] = (
        "equal_mean_cs_average_rank_percentiles(roe high, roic high, grossprofit_margin high, "
        "debt_to_assets low, ocf_to_or high)"
    )

    @model_validator(mode="after")
    def _freeze_components(self) -> QualityFactorFormula:
        expected = [
            ("roe", "high"),
            ("roic", "high"),
            ("grossprofit_margin", "high"),
            ("debt_to_assets", "low"),
            ("ocf_to_or", "high"),
        ]
        if len(self.components) != len(expected):
            raise ValueError("quality components count mismatch")
        for component, (metric, direction) in zip(self.components, expected, strict=True):
            if component.metric != metric or component.direction != direction:
                raise ValueError("quality component metric/direction mismatch")
        return self


class ValueDailyTiming(_StrictModel):
    available_at_lte_decision_at: Literal[True] = True
    date_lte_as_of: Literal[True] = True
    max_age_days: Literal[10] = 10


class ValueFactorFormula(_StrictModel):
    family_id: Literal["value"] = "value"
    high_is_good: Literal[True] = True
    aggregation: Literal["equal_mean_of_inverted_average_rank_percentiles_for_positive_metrics"] = (
        "equal_mean_of_inverted_average_rank_percentiles_for_positive_metrics"
    )
    metrics: list[str] = Field(default_factory=lambda: ["pe_ttm", "pb", "ps_ttm"])
    min_known_metrics: Literal[2] = 2
    daily_valuation_timing: ValueDailyTiming = Field(default_factory=ValueDailyTiming)
    formula: Literal[
        "equal_mean_inverted_cs_average_rank_percentiles(pe_ttm positive, pb positive, ps_ttm positive)"
    ] = "equal_mean_inverted_cs_average_rank_percentiles(pe_ttm positive, pb positive, ps_ttm positive)"

    @model_validator(mode="after")
    def _freeze_metrics(self) -> ValueFactorFormula:
        if self.metrics != ["pe_ttm", "pb", "ps_ttm"]:
            raise ValueError("value metrics must remain [pe_ttm, pb, ps_ttm]")
        return self


class MediumMomentumFactorFormula(_StrictModel):
    family_id: Literal["medium_momentum_12_1"] = "medium_momentum_12_1"
    high_is_good: Literal[True] = True
    formula: Literal["adjusted_close[t-21]/adjusted_close[t-242]-1"] = "adjusted_close[t-21]/adjusted_close[t-242]-1"
    require_ordered_positive_finite_market_bars: Literal[243] = 243
    window_definition: Literal["exactly_latest_243_consecutive_market_calendar_observations_ending_at_decision_t"] = (
        "exactly_latest_243_consecutive_market_calendar_observations_ending_at_decision_t"
    )
    formula_uses_fixed_indices: Literal["t-242_and_t-21_within_that_window"] = "t-242_and_t-21_within_that_window"
    any_missing_or_unverified_market_day_makes_factor_unknown: Literal[True] = True
    never_skip_compress_gaps: Literal[True] = True
    no_future_rows: Literal[True] = True
    note: str = (
        "Momentum uses exactly the latest 243 consecutive market-calendar observations ending at "
        "decision t. The formula uses those fixed indices t-242 and t-21 inside that window. Any "
        "missing or unverified market day makes the factor unknown; gaps are never skip-compressed."
    )


class DefensiveLowVolFactorFormula(_StrictModel):
    family_id: Literal["defensive_low_vol"] = "defensive_low_vol"
    high_is_good: Literal[True] = True
    formula: Literal["negative_annualized_stdev_of_60_daily_adjusted_close_returns_using_sqrt_242"] = (
        "negative_annualized_stdev_of_60_daily_adjusted_close_returns_using_sqrt_242"
    )
    require_ordered_positive_finite_market_bars: Literal[61] = 61
    window_definition: Literal["exactly_latest_61_consecutive_market_calendar_observations_ending_at_decision_t"] = (
        "exactly_latest_61_consecutive_market_calendar_observations_ending_at_decision_t"
    )
    any_missing_or_unverified_market_day_makes_factor_unknown: Literal[True] = True
    never_skip_compress_gaps: Literal[True] = True
    no_future_rows: Literal[True] = True
    return_count: Literal[60] = 60
    close_count: Literal[61] = 61
    sample_stdev_ddof: Literal[1] = 1
    annualization_sqrt_242: Literal[True] = True
    sign: Literal["negative"] = "negative"
    note: str = (
        "Low-vol uses exactly the latest 61 consecutive market-calendar observations ending at "
        "decision t to form exactly 60 simple returns. Negative sample stdev (ddof=1) is annualized "
        "by sqrt(242). Any missing or unverified market day makes the factor unknown; gaps are never "
        "skip-compressed."
    )

    @model_validator(mode="after")
    def _freeze_low_vol_semantics(self) -> DefensiveLowVolFactorFormula:
        if self.close_count != self.require_ordered_positive_finite_market_bars:
            raise ValueError("defensive_low_vol close_count must equal require_ordered_positive_finite_market_bars")
        if self.return_count != self.close_count - 1:
            raise ValueError("defensive_low_vol return_count must equal close_count - 1")
        return self


class AlphaFactorFamilyDefinition(_StrictModel):
    family_id: str = Field(min_length=1)
    quality: QualityFactorFormula | None = None
    value: ValueFactorFormula | None = None
    medium_momentum_12_1: MediumMomentumFactorFormula | None = None
    defensive_low_vol: DefensiveLowVolFactorFormula | None = None

    @model_validator(mode="after")
    def _one_formula(self) -> AlphaFactorFamilyDefinition:
        present = {
            "quality": self.quality is not None,
            "value": self.value is not None,
            "medium_momentum_12_1": self.medium_momentum_12_1 is not None,
            "defensive_low_vol": self.defensive_low_vol is not None,
        }
        active = [name for name, is_present in present.items() if is_present]
        if len(active) != 1:
            raise ValueError("each factor family entry must contain exactly one formula block")
        if self.family_id != active[0]:
            raise ValueError("family_id must match its formula block")
        return self


class CrossSectionRankingPolicy(_StrictModel):
    convert_raw_to_cs_average_rank_percentile_0_100: Literal[True] = True
    percentile_formula: Literal["(average_rank_1_based - 1)/(n - 1)*100"] = "(average_rank_1_based - 1)/(n - 1)*100"
    n_equals_1_is_unknown: Literal[True] = True
    ties_averaged: Literal[True] = True
    missing_or_invalid_stays_unknown: Literal[True] = True
    missing_or_invalid_never_imputed: Literal[True] = True
    never_zero_or_neutral_fill: Literal[True] = True
    no_winsorization_at_any_stage: Literal[True] = True
    all_families_high_is_good: Literal[True] = True
    low_direction_inverted_percentile_formula: Literal["100 - p"] = "100 - p"
    component_to_family_composite_rule: Literal[
        "mean_of_known_component_percentiles_then_rerank_family_composite_by_same_formula"
    ] = "mean_of_known_component_percentiles_then_rerank_family_composite_by_same_formula"
    note: str = (
        "Convert each raw family to a 0..100 cross-sectional average-rank percentile using "
        "(average_rank_1_based - 1)/(n - 1)*100 with ties averaged; n=1 is unknown, not 50. "
        "Missing/invalid stays unknown and is never imputed or zero/neutral filled. No winsorization "
        "at any stage (not merely after rank). Low-direction/inverted percentiles use 100 - p. For "
        "multi-component families (quality, value) each component is percentile-ranked, the known "
        "component percentiles are averaged, and the resulting family composite is reranked by the "
        "same percentile formula to a final 0..100 value."
    )


class QuintileSemanticsPolicy(_StrictModel):
    rank_basis: Literal["average_ranks_ties_averaged"] = "average_ranks_ties_averaged"
    bucket_formula: Literal["min(floor((rank-1)/n*5),4)"] = "min(floor((rank-1)/n*5),4)"
    quantile_count: Literal[5] = 5
    ties_never_split_across_buckets: Literal[True] = True
    all_equal_or_empty_extreme_bucket_invalid: Literal[True] = True
    bucket_weighting: Literal["equal_weight_mean_within_bucket"] = "equal_weight_mean_within_bucket"
    note: str = (
        "Quintiles are built from average ranks (ties averaged); bucket = "
        "min(floor((rank-1)/n*5), 4), matching quantile_portfolios.py. Ties are never split across "
        "buckets. An all-equal cross-section or an empty top/bottom bucket is invalid, not zero. "
        "Bucket means are equal-weight. quantile_count is fixed at 5."
    )


class SpearmanIcSemanticsPolicy(_StrictModel):
    rank_basis: Literal["average_ranks_on_paired_known_finite_factor_label_rows"] = (
        "average_ranks_on_paired_known_finite_factor_label_rows"
    )
    pairwise_deletion_of_unknown_or_nonfinite: Literal[True] = True
    all_equal_factor_or_label_invalid: Literal[True] = True
    note: str = (
        "Spearman IC uses average ranks computed only over the paired known finite factor/label rows "
        "for that decision day (pairwise deletion). An all-equal factor or all-equal label for that "
        "day is invalid/unknown, never coerced to a zero correlation."
    )


class ClusterCompanionPolicy(_StrictModel):
    statistical_risk_cluster: StatisticalRiskClusterPolicyConfirmed = Field(
        default_factory=StatisticalRiskClusterPolicyConfirmed
    )
    lookback_trading_days: Literal[120] = 120
    required_close_points: Literal[121] = 121
    correlation_threshold: float = 0.65
    linkage: Literal["connected_components_chain"] = "connected_components_chain"
    static_current_industry_labels_forbidden: Literal[True] = True
    no_current_industry_labels: Literal[True] = True
    statistical_clusters_pit_diagnostic_companion_only: Literal[True] = True
    not_replacement_for_industry_pit: Literal[True] = True
    not_automatic_weight_selector: Literal[True] = True
    recompute_anchor: Literal[
        "first_market_trading_day_of_each_calendar_month_using_data_through_that_decision_close"
    ] = "first_market_trading_day_of_each_calendar_month_using_data_through_that_decision_close"
    carry_assignment_until_before_next_anchor: Literal[True] = True
    new_unassigned_or_incomplete_is_unknown_no_backfill: Literal[True] = True
    companion_factor_definition: Literal[
        "same_final_raw_family_composite_reranked_within_each_complete_cluster_using_same_percentile_formula"
    ] = "same_final_raw_family_composite_reranked_within_each_complete_cluster_using_same_percentile_formula"
    singleton_clusters_unknown: Literal[True] = True
    companion_evidence_basis: Literal["paired_known_names_same_daily_calendar_endpoint_coverage_quintile_spearman"] = (
        "paired_known_names_same_daily_calendar_endpoint_coverage_quintile_spearman"
    )
    companion_requires_both_pooled_h40_ic_and_spread_positive: Literal[True] = True
    safeguard_only_never_independent_weight: Literal[True] = True
    never_fifth_holm_hypothesis: Literal[True] = True
    note: str = (
        "Companion clusters bind to the two-layer sealed StatisticalRiskClusterPolicyConfirmed: 120 "
        "trading-day lookback over 121 adjusted closes, Pearson threshold 0.65, connected-component "
        "chain linkage, no current industry labels. Cluster membership recomputes only at the first "
        "market trading day of each calendar month using data through that decision close; the prior "
        "assignment carries until just before the next monthly anchor. New, unassigned, or incomplete "
        "names are unknown with no backfill. The companion factor reranks the same final raw family "
        "composite within each complete cluster using the same percentile formula; singleton clusters "
        "are unknown. Companion evidence requires the same daily calendar, endpoint, coverage, "
        "quintile, and Spearman gates on paired known names, and requires both pooled h40 IC and "
        "pooled h40 spread to be positive. The companion is a safeguard only: it is never an "
        "independent weight and never a fifth Holm hypothesis."
    )

    @field_validator("correlation_threshold", mode="before")
    @classmethod
    def _reject_bool_threshold(cls, value: object) -> float:
        return _require_real_number(value, field_name="correlation_threshold", minimum=0.0, minimum_exclusive=True)

    @model_validator(mode="after")
    def _freeze_cluster_companion(self) -> ClusterCompanionPolicy:
        self.correlation_threshold = _require_exact_float(
            self.correlation_threshold, 0.65, field_name="correlation_threshold"
        )
        if self.statistical_risk_cluster.lookback_trading_days != self.lookback_trading_days:
            raise ValueError("cluster companion lookback_trading_days must match statistical_risk_cluster")
        if abs(self.statistical_risk_cluster.correlation_threshold - self.correlation_threshold) > 1e-12:
            raise ValueError("cluster companion correlation_threshold must match statistical_risk_cluster")
        if self.required_close_points != self.lookback_trading_days + 1:
            raise ValueError("cluster companion required_close_points must equal lookback_trading_days + 1")
        if self.statistical_risk_cluster.max_positions_per_cluster != 2:
            raise ValueError("cluster companion statistical_risk_cluster max_positions_per_cluster must remain 2")
        return self


class PrimaryFactorLabel(_StrictModel):
    horizon_market_days: Literal[40] = 40
    definition: Literal["adjusted_close_t_to_t_plus_40_close_to_close_forward_return"] = (
        "adjusted_close_t_to_t_plus_40_close_to_close_forward_return"
    )
    exact_endpoint_definition: Literal["market_calendar_observation_t_plus_h_for_same_symbol"] = (
        "market_calendar_observation_t_plus_h_for_same_symbol"
    )
    missing_or_unverified_endpoint_is_unknown: Literal[True] = True
    horizon_never_shifts: Literal[True] = True
    diagnostic_only: Literal[True] = True
    primary_selection_horizon: Literal[True] = True


class SecondaryHorizonPolicy(_StrictModel):
    horizons_market_days: list[int] = Field(default_factory=lambda: [5, 20])
    exact_endpoint_definition: Literal["market_calendar_observation_t_plus_h_for_same_symbol"] = (
        "market_calendar_observation_t_plus_h_for_same_symbol"
    )
    missing_or_unverified_endpoint_is_unknown: Literal[True] = True
    horizon_never_shifts: Literal[True] = True
    diagnostic_only: Literal[True] = True
    cannot_select: Literal[True] = True

    @model_validator(mode="after")
    def _freeze_horizons(self) -> SecondaryHorizonPolicy:
        if self.horizons_market_days != [5, 20]:
            raise ValueError("secondary horizons must remain [5, 20]")
        return self


class ForwardLabelAndPoolingPolicy(_StrictModel):
    same_window_endpoint_required: Literal[True] = True
    never_shorten_horizon: Literal[True] = True
    exact_label_endpoint: Literal["market_calendar_observation_t_plus_h_for_same_symbol"] = (
        "market_calendar_observation_t_plus_h_for_same_symbol"
    )
    missing_or_unverified_endpoint_is_unknown: Literal[True] = True
    horizon_never_shifts: Literal[True] = True
    calendar_every_eligible_market_trading_day_not_tranche_phases: Literal[True] = True
    pool_arithmetic_mean_of_per_decision_day_observations: Literal[True] = True
    never_pool_at_name_row_level: Literal[True] = True
    development_labels_must_not_cross_2023_12_31: Literal[True] = True
    robustness_2024_labels_must_not_cross_2024_12_31: Literal[True] = True
    robustness_2024_must_never_read_consumed_oos: Literal[True] = True
    missing_exact_endpoint_is_unknown: Literal[True] = True
    note: str = (
        "Every factor evidence observation requires decision t and label endpoint t+h inside the same "
        "evidence window. The exact h label endpoint is the market-calendar observation t+h for the "
        "same symbol; a missing or unverified endpoint is unknown and the horizon never shifts or "
        "shortens. Development 40d/20d/5d labels may not cross 2023-12-31; 2024 robustness labels may "
        "not cross 2024-12-31 and may never read consumed OOS. The evaluation calendar is every "
        "eligible market trading day, not tranche phases, subject to the same-window endpoint and "
        "coverage gates. Pooled metrics are arithmetic means of per-decision-day observations; "
        "pooling never happens at the name-row level."
    )


class NeweyWestBartlettExactAlgorithm(_StrictModel):
    input_series: Literal["chronologically_ordered_finite_per_decision_day_metric_series_x_1_to_x_n_no_gap_filling"] = (
        "chronologically_ordered_finite_per_decision_day_metric_series_x_1_to_x_n_no_gap_filling"
    )
    mean_definition: Literal["arithmetic_xbar"] = "arithmetic_xbar"
    gamma_k_formula: Literal["gamma_k=(1/n)*sum_{t=k+1..n}(x_t-xbar)*(x_{t-k}-xbar)_including_gamma_0_divisor_n"] = (
        "gamma_k=(1/n)*sum_{t=k+1..n}(x_t-xbar)*(x_{t-k}-xbar)_including_gamma_0_divisor_n"
    )
    bartlett_weight_formula: Literal["w_k=1-k/(L+1)"] = "w_k=1-k/(L+1)"
    long_run_variance_formula: Literal["LRV=gamma_0+2*sum_{k=1..min(L,n-1)}w_k*gamma_k"] = (
        "LRV=gamma_0+2*sum_{k=1..min(L,n-1)}w_k*gamma_k"
    )
    variance_of_mean_formula: Literal["LRV/n"] = "LRV/n"
    undefined_when: Literal["n_le_L_or_LRV_or_variance_nonfinite_or_le_0_then_statistic_and_raw_hac_p_null"] = (
        "n_le_L_or_LRV_or_variance_nonfinite_or_le_0_then_statistic_and_raw_hac_p_null"
    )
    never_coerce_undefined_to_number: Literal[True] = True
    positive_test_statistic_formula: Literal["xbar/sqrt(var_mean)"] = "xbar/sqrt(var_mean)"
    positive_p_value_formula: Literal["1-Phi(stat)_standard_normal_cdf"] = "1-Phi(stat)_standard_normal_cdf"
    negative_size_band_p_value_formula: Literal["Phi(stat)_standard_normal_cdf"] = "Phi(stat)_standard_normal_cdf"
    note: str = (
        "Exact Newey-West/Bartlett HAC: input is the chronologically ordered finite per-decision-day "
        "metric series with no gap filling; xbar is the arithmetic mean; gamma_k uses divisor n "
        "including gamma_0; Bartlett weight w_k=1-k/(L+1); LRV=gamma_0+2*sum w_k*gamma_k for "
        "k=1..min(L,n-1); var(mean)=LRV/n. If n<=L or LRV/variance is nonfinite or <=0, the inference "
        "statistic and raw HAC p are null/undefined and must not be coerced. Positive one-sided test "
        "uses stat=xbar/sqrt(var_mean) and p=1-Phi(stat); negative size-band tests use p=Phi(stat)."
    )


class HolmStepDownExactAlgorithm(_StrictModel):
    hypothesis_family: Literal["exactly_four_pooled_h40_daily_ic_hypotheses"] = (
        "exactly_four_pooled_h40_daily_ic_hypotheses"
    )
    spread_positivity_and_yearly_direction_are_gates_not_holm_members: Literal[True] = True
    sort_key: Literal["effective_p_ascending_then_frozen_factor_family_order_tie_break"] = (
        "effective_p_ascending_then_frozen_factor_family_order_tie_break"
    )
    tie_break_factor_family_order: list[str] = Field(default_factory=lambda: list(HOLM_TIE_BREAK_FACTOR_FAMILY_ORDER))
    threshold_at_sorted_position_i: Literal["alpha/(4-i+1)_for_i_equals_1_to_4"] = "alpha/(4-i+1)_for_i_equals_1_to_4"
    sequential_reject_until_first_failure_then_all_remaining_nonrejected: Literal[True] = True
    missing_or_undefined_raw_hac_leaves_raw_null_but_holm_input_p_equals_1: Literal[True] = True
    missing_or_undefined_raw_hac_rejection_false: Literal[True] = True
    report_per_factor_fields: list[str] = Field(
        default_factory=lambda: [
            "holm_sorted_position",
            "holm_threshold",
            "holm_input_p_value",
            "hac_p_value",
            "holm_rejection",
        ]
    )
    note: str = (
        "Holm step-down covers exactly the four pooled h40 daily IC hypotheses. Spread positivity and "
        "yearly direction remain gates but are not Holm family members. Sort by effective p ascending "
        "with frozen factor-family order quality/value/medium_momentum_12_1/defensive_low_vol as the "
        "deterministic tie-break. At sorted position i=1..4 the threshold is alpha/(4-i+1). Reject "
        "sequentially only until the first failure; all remaining hypotheses stay non-rejected. When "
        "factor evidence/variance is missing, raw hac_statistic and hac_p_value stay null while "
        "holm_input_p_value=1 and rejection=false. Report each factor's sorted position, threshold, "
        "effective p, raw p, and rejection."
    )

    @model_validator(mode="after")
    def _freeze_holm(self) -> HolmStepDownExactAlgorithm:
        if self.tie_break_factor_family_order != list(HOLM_TIE_BREAK_FACTOR_FAMILY_ORDER):
            raise ValueError("Holm tie-break order must remain the frozen four-family order")
        expected_report = [
            "holm_sorted_position",
            "holm_threshold",
            "holm_input_p_value",
            "hac_p_value",
            "holm_rejection",
        ]
        if self.report_per_factor_fields != expected_report:
            raise ValueError("Holm per-factor report fields must remain the frozen set")
        return self


class InferencePolicy(_StrictModel):
    hac_lag_for_horizon_h_is_h_minus_1: Literal[True] = True
    primary_selection_horizon_market_days: Literal[40] = 40
    primary_hac_lag: Literal[39] = 39
    hac_kernel: Literal["bartlett_newey_west"] = "bartlett_newey_west"
    newey_west_bartlett_exact: NeweyWestBartlettExactAlgorithm = Field(default_factory=NeweyWestBartlettExactAlgorithm)
    variance_of_sample_mean_definition: Literal["LRV/n"] = "LRV/n"
    hypothesis_form: Literal["one_sided_h0_mean_le_0_h1_gt_0"] = "one_sided_h0_mean_le_0_h1_gt_0"
    holm_family_wise_alpha: float = 0.05
    holm_hypothesis_count: Literal[4] = 4
    holm_step_down_exact: HolmStepDownExactAlgorithm = Field(default_factory=HolmStepDownExactAlgorithm)
    holm_step_down_method: Literal["holm_step_down_exactly_four_hypotheses"] = "holm_step_down_exactly_four_hypotheses"
    missing_evidence_rule: Literal["raw_hac_statistic_and_hac_p_null_holm_input_p_equals_1_rejection_false"] = (
        "raw_hac_statistic_and_hac_p_null_holm_input_p_equals_1_rejection_false"
    )
    qualification_rule: Literal["qualify_only_if_own_null_rejected"] = "qualify_only_if_own_null_rejected"
    undefined_variance_rule: Literal["raw_statistic_and_raw_p_null_do_not_coerce_holm_input_p_equals_1"] = (
        "raw_statistic_and_raw_p_null_do_not_coerce_holm_input_p_equals_1"
    )
    required_report_fields: list[str] = Field(default_factory=lambda: list(REQUIRED_INFERENCE_REPORT_FIELDS))
    one_sided_hac_inference_required: Literal[True] = True
    note: str = (
        "Exact Newey-West/Bartlett HAC lag is h-1 (primary h=40 -> L=39) on the chronologically ordered "
        "finite per-decision-day metric series with no gap filling. gamma_k uses divisor n; Bartlett "
        "weight w_k=1-k/(L+1); LRV=gamma_0+2*sum w_k*gamma_k; var(mean)=LRV/n. If n<=L or LRV/variance "
        "is nonfinite or <=0, raw hac_statistic and hac_p_value are null (never coerced) while "
        "holm_input_p_value=1 and rejection=false. Positive test uses p=1-Phi(xbar/sqrt(var_mean)); "
        "negative size-band tests use p=Phi(stat). Holm step-down covers exactly the four pooled h40 "
        "daily IC hypotheses (spread/yearly direction are gates only), sorting by effective p then "
        "frozen family order, with threshold alpha/(4-i+1) and sequential reject-until-first-failure. "
        "A factor qualifies only if its own null is rejected."
    )

    @field_validator("holm_family_wise_alpha", mode="before")
    @classmethod
    def _reject_bool_alpha(cls, value: object) -> float:
        return _require_real_number(value, field_name="holm_family_wise_alpha", minimum=0.0, minimum_exclusive=True)

    @model_validator(mode="after")
    def _freeze_inference(self) -> InferencePolicy:
        self.holm_family_wise_alpha = _require_exact_float(
            self.holm_family_wise_alpha, 0.05, field_name="holm_family_wise_alpha"
        )
        if self.primary_hac_lag != self.primary_selection_horizon_market_days - 1:
            raise ValueError("primary_hac_lag must equal primary_selection_horizon_market_days - 1")
        if self.required_report_fields != list(REQUIRED_INFERENCE_REPORT_FIELDS):
            raise ValueError("required_report_fields must remain the frozen inference report field set")
        if self.newey_west_bartlett_exact.variance_of_mean_formula != self.variance_of_sample_mean_definition:
            raise ValueError("variance_of_sample_mean_definition must equal exact algorithm LRV/n")
        return self


class LabelsAndEvidencePolicy(_StrictModel):
    primary_label: PrimaryFactorLabel = Field(default_factory=PrimaryFactorLabel)
    secondary_horizons: SecondaryHorizonPolicy = Field(default_factory=SecondaryHorizonPolicy)
    forward_label_and_pooling: ForwardLabelAndPoolingPolicy = Field(default_factory=ForwardLabelAndPoolingPolicy)
    inference: InferencePolicy = Field(default_factory=InferencePolicy)
    quintile_semantics: QuintileSemanticsPolicy = Field(default_factory=QuintileSemanticsPolicy)
    spearman_ic_semantics: SpearmanIcSemanticsPolicy = Field(default_factory=SpearmanIcSemanticsPolicy)
    full_cs_quintiles_mandatory: Literal[True] = True
    spearman_ic_mandatory: Literal[True] = True
    factor_scoring_cannot_serve_as_evidence: Literal[True] = True
    tranche_portfolio_cannot_serve_as_evidence: Literal[True] = True
    note: str = (
        "Primary factor label is adjusted-close t to t+40 close-to-close forward return (diagnostic). "
        "Secondary horizons 5 and 20 cannot select. Full CS quintiles and Spearman IC are mandatory. "
        "Factor scoring and 10-name/tranche portfolio cannot serve as factor evidence."
    )


class CoverageGates(_StrictModel):
    min_factor_known_cs_per_decision: Literal[500] = 500
    min_factor_known_cs_fraction_of_eligible: float = 0.60
    min_valid_primary_scoring_dates_pooled: Literal[120] = 120
    min_valid_primary_scoring_dates_in_2022: Literal[40] = 40
    min_valid_primary_scoring_dates_in_2023: Literal[40] = 40
    note: str = (
        "Per decision: factor-known CS >= 500 and >= 60% of eligible names. Pooled primary scoring "
        "dates >= 120 with >= 40 in each of 2022 and 2023."
    )

    @field_validator("min_factor_known_cs_fraction_of_eligible", mode="before")
    @classmethod
    def _reject_bool_fraction(cls, value: object) -> float:
        return _require_real_number(
            value,
            field_name="min_factor_known_cs_fraction_of_eligible",
            minimum=0.0,
            minimum_exclusive=True,
        )

    @model_validator(mode="after")
    def _freeze_fraction(self) -> CoverageGates:
        self.min_factor_known_cs_fraction_of_eligible = _require_exact_float(
            self.min_factor_known_cs_fraction_of_eligible,
            0.60,
            field_name="min_factor_known_cs_fraction_of_eligible",
        )
        return self


class PreFreezeSelectionPolicy(_StrictModel):
    no_continuous_optimization: Literal[True] = True
    require_positive_pooled_40d_mean_ic: Literal[True] = True
    require_positive_pooled_top_minus_bottom_quintile_spread: Literal[True] = True
    require_positive_direction_in_2022_and_2023_for_both_metrics: Literal[True] = True
    require_positive_cluster_companion_pooled_direction: Literal[True] = True
    require_pooled_40d_ic_one_sided_hac_holm: Literal[True] = True
    holm_across_exactly_four_primary_hypotheses: Literal[True] = True
    holm_family_members_are_pooled_h40_daily_ic_only: Literal[True] = True
    spread_and_yearly_direction_are_gates_not_holm_members: Literal[True] = True
    note: str = (
        "Each factor must have positive pooled 40d mean IC and top-minus-bottom quintile spread, "
        "positive direction separately in both 2022 and 2023 for both metrics, positive "
        "cluster-companion pooled direction, and pooled 40d IC passing one-sided HAC inference "
        "with Holm alpha 0.05 across exactly four primary hypotheses. Spread positivity and yearly "
        "direction are gates, but only pooled h40 daily IC forms the four-member Holm family. No "
        "continuous optimization."
    )


class SizeBand(_StrictModel):
    label: Literal["3bn_5bn", "5bn_10bn", "10bn_plus"]
    min_inclusive: float
    max_exclusive: float | None

    @field_validator("min_inclusive", mode="before")
    @classmethod
    def _reject_bool_min(cls, value: object) -> float:
        return _require_real_number(value, field_name="min_inclusive", minimum=0.0, minimum_exclusive=True)

    @field_validator("max_exclusive", mode="before")
    @classmethod
    def _reject_bool_max(cls, value: object) -> object:
        if value is None:
            return None
        return _require_real_number(value, field_name="max_exclusive", minimum=0.0, minimum_exclusive=True)

    @model_validator(mode="after")
    def _bounds_ordered(self) -> SizeBand:
        if self.max_exclusive is not None and self.max_exclusive <= self.min_inclusive:
            raise ValueError("size band max_exclusive must exceed min_inclusive")
        return self


class SizeBandDiagnosticSafeguards(_StrictModel):
    role: Literal["diagnostic_safeguards_not_new_trials"] = "diagnostic_safeguards_not_new_trials"
    free_float_market_cap_currency: Literal["CNY"] = "CNY"
    bands: list[SizeBand] = Field(
        default_factory=lambda: [
            SizeBand(label=label, min_inclusive=lower, max_exclusive=upper)
            for label, lower, upper in CONFIRMED_SIZE_BANDS
        ]
    )
    under_lowest_band_outside_safeguard: Literal[True] = True
    unknown_stays_unknown: Literal[True] = True
    min_valid_primary_dates_per_band: Literal[40] = 40
    band_below_min_dates_is_unknown_and_not_positive: Literal[True] = True
    positive_band_rule: Literal["pooled_h40_mean_ic_and_pooled_h40_top_minus_bottom_spread_both_positive"] = (
        "pooled_h40_mean_ic_and_pooled_h40_top_minus_bottom_spread_both_positive"
    )
    require_at_least_two_bands_positive: Literal[True] = True
    min_bands_positive: Literal[2] = 2
    negative_significance_rule: Literal["lower_tail_one_sided_nw_bartlett_h0_mean_ge_0_h1_lt_0_pooled_h40_daily_ic"] = (
        "lower_tail_one_sided_nw_bartlett_h0_mean_ge_0_h1_lt_0_pooled_h40_daily_ic"
    )
    negative_significance_alpha: float = 0.05
    no_band_significantly_negative_one_sided_5pct: Literal[True] = True
    any_significant_negative_band_fails: Literal[True] = True
    note: str = (
        "Size bands are exact free-float market-cap CNY ranges: [3e9,5e9), [5e9,1e10), [1e10,+inf). "
        "Below 3e9 is outside the safeguard; unknown stays unknown and never silently passes. A band "
        "is positive only when both pooled h40 mean IC and pooled h40 top-minus-bottom spread are "
        "positive, and at least two bands must be positive. A band is significantly negative when a "
        "lower-tail one-sided Newey-West/Bartlett test (H0 mean>=0 vs H1 mean<0) on pooled h40 daily "
        "IC rejects at 5%; any significantly negative band fails the safeguard. A band with fewer than "
        "40 valid primary scoring dates is unknown and cannot count as positive."
    )

    @field_validator("negative_significance_alpha", mode="before")
    @classmethod
    def _reject_bool_alpha(cls, value: object) -> float:
        return _require_real_number(
            value, field_name="negative_significance_alpha", minimum=0.0, minimum_exclusive=True
        )

    @model_validator(mode="after")
    def _freeze_bands(self) -> SizeBandDiagnosticSafeguards:
        self.negative_significance_alpha = _require_exact_float(
            self.negative_significance_alpha, 0.05, field_name="negative_significance_alpha"
        )
        if len(self.bands) != len(CONFIRMED_SIZE_BANDS):
            raise ValueError("size bands must contain exactly three bands")
        for band, (label, lower, upper) in zip(self.bands, CONFIRMED_SIZE_BANDS, strict=True):
            if band.label != label or band.min_inclusive != lower or band.max_exclusive != upper:
                raise ValueError(f"size band {label} boundaries must remain [{lower}, {upper})")
        return self


class EligibilityDenominatorPolicy(_StrictModel):
    denominator_definition: Literal[
        "names_with_complete_verified_layer_two_candidate_eligibility_and_financial_negative_list_verdict_at_decision"
    ] = "names_with_complete_verified_layer_two_candidate_eligibility_and_financial_negative_list_verdict_at_decision"
    entry_requires_eligible_for_new_entry_true: Literal[True] = True
    entry_requires_financial_verdict_not_hard_exclude_or_unknown: Literal[True] = True
    alpha_factor_must_not_determine_eligibility: Literal[True] = True
    note: str = (
        "The evidence denominator is names with complete, verified layer-two candidate eligibility "
        "and a financial negative-list verdict at that decision. Only names with "
        "eligible_for_new_entry=true and a financial verdict that is neither hard-exclude nor unknown "
        "enter factor evidence. The alpha factor itself must never determine eligibility."
    )


class PitSnapshotBindingPolicy(_StrictModel):
    all_as_of_decision_at_available_at_must_be_pit: Literal[True] = True
    must_share_exact_sealed_market_snapshot: Literal[True] = True
    protocol_describes_future_input_bindings_only: Literal[True] = True
    ready_flags_remain_false: Literal[True] = True
    note: str = (
        "All relevant as_of/decision_at/available_at timestamps must be point-in-time and share the "
        "exact sealed market snapshot. This protocol may describe required future input bindings but "
        "remains protocol-only; all ready flags remain false."
    )


class AlphaWeightingPolicy(_StrictModel):
    qualifying_factors_equal_weight_summing_to_one: Literal[True] = True
    nonqualifying_weight_zero: Literal[True] = True
    if_none_qualify_weights_unavailable: Literal[True] = True
    if_none_qualify_not_ready: Literal[True] = True
    note: str = (
        "Qualifying factors receive equal weights summing to 1; nonqualifying receive zero. If none "
        "qualify, weights are unavailable and the protocol remains not ready."
    )


class Robustness2024Policy(_StrictModel):
    may_only_test_frozen_qualifying_set_equal_weights: Literal[True] = True
    must_not_select_or_alter_weights: Literal[True] = True
    reversal_of_pooled_40d_ic_marks_failure: Literal[True] = True
    reversal_of_quintile_spread_marks_failure: Literal[True] = True
    failure_never_retunes: Literal[True] = True
    report_only: Literal[True] = True
    note: str = (
        "2024 may only test the frozen qualifying set with equal weights. Reversal of pooled 40d IC "
        "or quintile spread marks robustness failure but never retunes weights."
    )


class ReportOnlyDiagnostics(_StrictModel):
    secondary_horizons: Literal[True] = True
    raw_vs_cluster_companion: Literal[True] = True
    long_only_top_quintile_returns: Literal[True] = True
    cannot_select_or_alter_weights: Literal[True] = True
    note: str = (
        "Report-only diagnostics include secondary horizons, raw vs cluster companion, and long-only "
        "top-quintile returns. They cannot select or alter weights."
    )


class LedgerRegistrationNote(_StrictModel):
    register_as_one_four_hypothesis_family_in_future_ledger_updates: Literal[True] = True
    e11a_does_not_modify_ledger: Literal[True] = True
    hypothesis_count: Literal[4] = 4
    note: str = (
        "Register as one four-hypothesis family in future research-trial-ledger updates. E11a does "
        "not modify the ledger in this milestone."
    )


class ReadinessGates(_StrictModel):
    research_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_data: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_run_data: Literal[True] = True
    does_not_score: Literal[True] = True
    does_not_wire_scoring: Literal[True] = True
    does_not_generate_strategy_config: Literal[True] = True
    note: str = (
        "E11a is a read-only protocol freeze. All ready flags remain false. This milestone does not "
        "run data, score, wire scoring, or generate strategy config."
    )


class LayerTwoAlphaDevelopmentProtocolV1(_StrictModel):
    schema_version: Literal["1"] = LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_SCHEMA_VERSION
    protocol_version: Literal["layer-two-alpha-development-protocol-v1"] = LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_VERSION
    status: ProtocolStatus = "confirmed_for_development_but_not_ready"
    confirmation_as_of: date = CONFIRMATION_AS_OF
    research_trial_ledger_id: str = Field(min_length=1)
    research_trial_ledger_path: Literal["config/research/research-trial-ledger-v1.json"] = (
        BOUND_RESEARCH_TRIAL_LEDGER_PATH
    )
    two_layer_decision_contract_id: str = Field(min_length=1)
    two_layer_decision_contract_path: Literal["config/research/two-layer-strategy-decision-draft-v1.json"] = (
        BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
    )
    tranche_evaluation_protocol_id: str = Field(min_length=1)
    tranche_evaluation_protocol_path: Literal["config/research/tranche-evaluation-protocol-draft-v1.json"] = (
        BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
    )
    layer_two_allocation_protocol_id: str = Field(min_length=1)
    layer_two_allocation_protocol_path: Literal[
        "config/research/layer-two-allocation-implementation-protocol-v1.json"
    ] = BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_PATH
    factor_families: list[AlphaFactorFamilyDefinition]
    windows: AlphaResearchWindows
    ranking: CrossSectionRankingPolicy = Field(default_factory=CrossSectionRankingPolicy)
    cluster_companion: ClusterCompanionPolicy = Field(default_factory=ClusterCompanionPolicy)
    labels_and_evidence: LabelsAndEvidencePolicy = Field(default_factory=LabelsAndEvidencePolicy)
    coverage_gates: CoverageGates = Field(default_factory=CoverageGates)
    pre_freeze_selection: PreFreezeSelectionPolicy = Field(default_factory=PreFreezeSelectionPolicy)
    size_bands: SizeBandDiagnosticSafeguards = Field(default_factory=SizeBandDiagnosticSafeguards)
    weighting: AlphaWeightingPolicy = Field(default_factory=AlphaWeightingPolicy)
    robustness_2024: Robustness2024Policy = Field(default_factory=Robustness2024Policy)
    report_only_diagnostics: ReportOnlyDiagnostics = Field(default_factory=ReportOnlyDiagnostics)
    eligibility_denominator: EligibilityDenominatorPolicy = Field(default_factory=EligibilityDenominatorPolicy)
    pit_snapshot_binding: PitSnapshotBindingPolicy = Field(default_factory=PitSnapshotBindingPolicy)
    ledger_registration: LedgerRegistrationNote = Field(default_factory=LedgerRegistrationNote)
    readiness: ReadinessGates = Field(default_factory=ReadinessGates)
    evidence_blockers: list[ProtocolEvidenceBlocker]
    pending_user_decisions: list[str] = Field(default_factory=list)
    protocol_id: str | None = None

    @field_validator(
        "research_trial_ledger_id",
        "two_layer_decision_contract_id",
        "tranche_evaluation_protocol_id",
        "layer_two_allocation_protocol_id",
        mode="before",
    )
    @classmethod
    def _reject_blank_ids(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)

    @field_validator("research_trial_ledger_path", mode="before")
    @classmethod
    def _reject_ledger_path(cls, value: object) -> object:
        return _assert_bound_relative_path(
            value,
            expected=BOUND_RESEARCH_TRIAL_LEDGER_PATH,
            field_name="research_trial_ledger_path",
        )

    @field_validator("two_layer_decision_contract_path", mode="before")
    @classmethod
    def _reject_contract_path(cls, value: object) -> object:
        return _assert_bound_relative_path(
            value,
            expected=BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
            field_name="two_layer_decision_contract_path",
        )

    @field_validator("tranche_evaluation_protocol_path", mode="before")
    @classmethod
    def _reject_tranche_path(cls, value: object) -> object:
        return _assert_bound_relative_path(
            value,
            expected=BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
            field_name="tranche_evaluation_protocol_path",
        )

    @field_validator("layer_two_allocation_protocol_path", mode="before")
    @classmethod
    def _reject_allocation_path(cls, value: object) -> object:
        return _assert_bound_relative_path(
            value,
            expected=BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_PATH,
            field_name="layer_two_allocation_protocol_path",
        )

    @field_validator("confirmation_as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, value: object) -> date:
        return _parse_iso_date(value, field_name="confirmation_as_of")

    @model_validator(mode="after")
    def _gate(self) -> LayerTwoAlphaDevelopmentProtocolV1:
        if self.status != "confirmed_for_development_but_not_ready":
            raise ValueError("status must be confirmed_for_development_but_not_ready")
        readiness = self.readiness
        if (
            readiness.ready_for_scoring
            or readiness.ready_for_backtest
            or readiness.ready_for_portfolio_construction
            or readiness.ready_for_data
            or readiness.ready_for_orders
            or readiness.ready_for_trading
            or readiness.auto_apply
            or not readiness.research_only
            or not readiness.does_not_run_data
            or not readiness.does_not_score
            or not readiness.does_not_wire_scoring
            or not readiness.does_not_generate_strategy_config
        ):
            raise ValueError("alpha development protocol must remain research_only with all ready flags false")
        if self.pending_user_decisions:
            raise ValueError("confirmed protocol must have empty pending_user_decisions")
        if self.research_trial_ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
            raise ValueError("research_trial_ledger_id does not match bound research trial ledger")
        if self.two_layer_decision_contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
            raise ValueError("two_layer_decision_contract_id does not match bound two-layer contract")
        if self.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
            raise ValueError("tranche_evaluation_protocol_id does not match bound tranche protocol")
        if self.layer_two_allocation_protocol_id != BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_ID:
            raise ValueError("layer_two_allocation_protocol_id does not match bound allocation protocol")
        if len(self.factor_families) != len(CONFIRMED_FACTOR_FAMILIES):
            raise ValueError("factor_families must contain exactly four families")
        family_ids = [entry.family_id for entry in self.factor_families]
        if family_ids != list(CONFIRMED_FACTOR_FAMILIES):
            raise ValueError("factor_families order must be quality, value, medium_momentum_12_1, defensive_low_vol")
        path_to_category: dict[str, str] = {}
        for blocker in self.evidence_blockers:
            if blocker.path in path_to_category:
                raise ValueError(f"evidence_blockers duplicate path: {blocker.path}")
            path_to_category[blocker.path] = blocker.category
        missing = [path for path in REQUIRED_ALPHA_DEVELOPMENT_EVIDENCE_BLOCKERS if path not in path_to_category]
        if missing:
            raise ValueError(f"evidence_blockers missing required paths: {missing}")
        wrong = [
            f"{path}->{path_to_category[path]} (expected {expected})"
            for path, expected in REQUIRED_ALPHA_DEVELOPMENT_EVIDENCE_BLOCKERS.items()
            if path_to_category[path] != expected
        ]
        if wrong:
            raise ValueError("evidence_blockers path->category mismatch: " + "; ".join(wrong))
        pooling = self.labels_and_evidence.forward_label_and_pooling
        if (
            pooling.development_labels_must_not_cross_2023_12_31
            and self.windows.development.end != CONFIRMED_DEVELOPMENT_END
        ):
            raise ValueError("forward_label_and_pooling development cutoff must match windows.development.end")
        if (
            pooling.robustness_2024_labels_must_not_cross_2024_12_31
            and self.windows.seen_robustness.end != CONFIRMED_SEEN_ROBUSTNESS_END
        ):
            raise ValueError("forward_label_and_pooling robustness cutoff must match windows.seen_robustness.end")
        if self.eligibility_denominator.alpha_factor_must_not_determine_eligibility is not True:
            raise ValueError("alpha factor must never determine eligibility")
        momentum = self.factor_families[2].medium_momentum_12_1
        if momentum is None or momentum.never_skip_compress_gaps is not True:
            raise ValueError("momentum bar window must never skip-compress gaps")
        low_vol = self.factor_families[3].defensive_low_vol
        if low_vol is None or low_vol.never_skip_compress_gaps is not True:
            raise ValueError("low-vol bar window must never skip-compress gaps")
        if pooling.horizon_never_shifts is not True or pooling.exact_label_endpoint != (
            "market_calendar_observation_t_plus_h_for_same_symbol"
        ):
            raise ValueError("exact label endpoint must be market-calendar t+h for same symbol")
        inference = self.labels_and_evidence.inference
        if inference.holm_step_down_exact.tie_break_factor_family_order != list(HOLM_TIE_BREAK_FACTOR_FAMILY_ORDER):
            raise ValueError("Holm tie-break order must match confirmed factor families")
        if self.pre_freeze_selection.holm_family_members_are_pooled_h40_daily_ic_only is not True:
            raise ValueError("Holm family members must be pooled h40 daily IC only")
        return self


class LayerTwoAlphaDevelopmentProtocolVerificationResult(_StrictModel):
    protocol_id: str
    schema_version: Literal["1"] = "1"
    protocol_version: str
    status: str
    structural_ok: bool
    research_trial_ledger_id: str
    research_trial_ledger_path: str
    research_trial_ledger_binding_ok: bool = False
    two_layer_decision_contract_id: str
    two_layer_decision_contract_path: str
    two_layer_decision_contract_binding_ok: bool = False
    tranche_evaluation_protocol_id: str
    tranche_evaluation_protocol_path: str
    tranche_evaluation_protocol_binding_ok: bool = False
    layer_two_allocation_protocol_id: str
    layer_two_allocation_protocol_path: str
    layer_two_allocation_protocol_binding_ok: bool = False
    resolved: bool
    user_decisions_resolved: bool
    pending_user_decision_count: int
    pending_user_decisions: list[str] = Field(default_factory=list)
    blockers: list[str]
    evidence_blockers: list[ProtocolEvidenceBlocker] = Field(default_factory=list)
    research_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_data: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_run_data: Literal[True] = True
    does_not_score: Literal[True] = True
    does_not_wire_scoring: Literal[True] = True
    does_not_generate_strategy_config: Literal[True] = True

    @field_validator(
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_portfolio_construction",
        "ready_for_data",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
        mode="before",
    )
    @classmethod
    def _false_ready(cls, value: object, info: Any) -> object:
        return _require_literal_false(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _state_machine(self) -> LayerTwoAlphaDevelopmentProtocolVerificationResult:
        bindings = (
            self.research_trial_ledger_binding_ok,
            self.two_layer_decision_contract_binding_ok,
            self.tranche_evaluation_protocol_binding_ok,
            self.layer_two_allocation_protocol_binding_ok,
        )
        any_bound = any(bindings)
        all_bound = all(bindings)
        if self.structural_ok is not True:
            if any_bound:
                raise ValueError("structural_ok=false forbids disk bindings")
            return self
        if any_bound and not all_bound:
            raise ValueError("partial bindings are forbidden")
        return self


def _windows_overlap(left: ProtocolDateWindow, right: ProtocolDateWindow) -> bool:
    return left.start <= right.end and right.start <= left.end


def assert_windows_non_overlapping(windows: AlphaResearchWindows) -> None:
    pairs = (
        (windows.development, windows.seen_robustness, "development", "seen_robustness"),
        (windows.development, windows.consumed_oos, "development", "consumed_oos"),
        (windows.seen_robustness, windows.consumed_oos, "seen_robustness", "consumed_oos"),
    )
    for left, right, left_name, right_name in pairs:
        if _windows_overlap(left, right):
            raise ValueError(f"{left_name} window overlaps {right_name}")
    if windows.new_frozen_oos_begins <= windows.consumed_oos.end:
        raise ValueError("new_frozen_oos_begins must be after consumed_oos.end")
    if windows.development.end >= windows.seen_robustness.start:
        raise ValueError("development must be strictly before seen_robustness")
    if windows.seen_robustness.end >= windows.consumed_oos.start:
        raise ValueError("seen_robustness must be strictly before consumed_oos")


def _assert_bound_relative_path(value: object, *, expected: str, field_name: str) -> object:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be relative without parent traversal")
    if value != expected:
        raise ValueError(f"{field_name} does not match bound path")
    return value


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


def default_factor_families() -> list[AlphaFactorFamilyDefinition]:
    return [
        AlphaFactorFamilyDefinition(family_id="quality", quality=QualityFactorFormula()),
        AlphaFactorFamilyDefinition(family_id="value", value=ValueFactorFormula()),
        AlphaFactorFamilyDefinition(
            family_id="medium_momentum_12_1",
            medium_momentum_12_1=MediumMomentumFactorFormula(),
        ),
        AlphaFactorFamilyDefinition(
            family_id="defensive_low_vol",
            defensive_low_vol=DefensiveLowVolFactorFormula(),
        ),
    ]


def default_alpha_research_windows() -> AlphaResearchWindows:
    return AlphaResearchWindows(
        development=ProtocolDateWindow(
            start=CONFIRMED_DEVELOPMENT_START,
            end=CONFIRMED_DEVELOPMENT_END,
        ),
        seen_robustness=ProtocolDateWindow(
            start=CONFIRMED_SEEN_ROBUSTNESS_START,
            end=CONFIRMED_SEEN_ROBUSTNESS_END,
        ),
        consumed_oos=ProtocolDateWindow(
            start=CONFIRMED_CONSUMED_OOS_START,
            end=CONFIRMED_CONSUMED_OOS_END,
        ),
        new_frozen_oos_begins=CONFIRMED_NEW_FROZEN_OOS_START,
    )


def default_alpha_evidence_blockers() -> list[ProtocolEvidenceBlocker]:
    return [
        ProtocolEvidenceBlocker(
            path="factor_evidence_pipeline",
            category="pending_implementation",
            detail=(
                "E11a freezes factor evidence gates only; the pipeline that computes IC, quintiles, "
                "and coverage on real data is not implemented in this milestone."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="research_trial_ledger_four_hypothesis_family",
            category="pending_implementation",
            detail=(
                "Future ledger update must register one four-hypothesis family before evaluation; "
                "E11a does not modify the research trial ledger."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="pit_industry_history",
            category="future_enhancement",
            detail=(
                "Real PIT industry history remains a future enhancement; statistical clusters stay "
                "diagnostic companion only and must not silently rewrite this protocol."
            ),
        ),
        ProtocolEvidenceBlocker(
            path="alpha_weight_wiring",
            category="pending_development_evidence",
            detail=(
                "Qualifying equal weights remain pending development evidence; this protocol does not "
                "wire scoring or generate strategy config."
            ),
        ),
    ]


def canonical_protocol_payload(draft: LayerTwoAlphaDevelopmentProtocolV1) -> dict[str, Any]:
    return draft.model_dump(mode="json", exclude={"protocol_id"})


def canonical_protocol_bytes(draft: LayerTwoAlphaDevelopmentProtocolV1) -> bytes:
    payload = canonical_protocol_payload(draft)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_protocol_id(draft: LayerTwoAlphaDevelopmentProtocolV1) -> str:
    return hashlib.sha256(canonical_protocol_bytes(draft)).hexdigest()


def seal_layer_two_alpha_development_protocol(
    draft: LayerTwoAlphaDevelopmentProtocolV1,
) -> LayerTwoAlphaDevelopmentProtocolV1:
    return draft.model_copy(update={"protocol_id": compute_protocol_id(draft)})


def build_confirmed_layer_two_alpha_development_protocol_v1(
    *,
    research_trial_ledger_id: str = BOUND_RESEARCH_TRIAL_LEDGER_ID,
    research_trial_ledger_path: Literal["config/research/research-trial-ledger-v1.json"] = (
        BOUND_RESEARCH_TRIAL_LEDGER_PATH
    ),
    two_layer_decision_contract_id: str = BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    two_layer_decision_contract_path: Literal[
        "config/research/two-layer-strategy-decision-draft-v1.json"
    ] = BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
    tranche_evaluation_protocol_id: str = BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
    tranche_evaluation_protocol_path: Literal[
        "config/research/tranche-evaluation-protocol-draft-v1.json"
    ] = BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
    layer_two_allocation_protocol_id: str = BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_ID,
    layer_two_allocation_protocol_path: Literal[
        "config/research/layer-two-allocation-implementation-protocol-v1.json"
    ] = BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_PATH,
    confirmation_as_of: date = CONFIRMATION_AS_OF,
) -> LayerTwoAlphaDevelopmentProtocolV1:
    draft = LayerTwoAlphaDevelopmentProtocolV1(
        confirmation_as_of=confirmation_as_of,
        research_trial_ledger_id=research_trial_ledger_id,
        research_trial_ledger_path=research_trial_ledger_path,
        two_layer_decision_contract_id=two_layer_decision_contract_id,
        two_layer_decision_contract_path=two_layer_decision_contract_path,
        tranche_evaluation_protocol_id=tranche_evaluation_protocol_id,
        tranche_evaluation_protocol_path=tranche_evaluation_protocol_path,
        layer_two_allocation_protocol_id=layer_two_allocation_protocol_id,
        layer_two_allocation_protocol_path=layer_two_allocation_protocol_path,
        factor_families=default_factor_families(),
        windows=default_alpha_research_windows(),
        evidence_blockers=default_alpha_evidence_blockers(),
        pending_user_decisions=[],
    )
    return seal_layer_two_alpha_development_protocol(draft)


def assert_protocol_self_hash(draft: LayerTwoAlphaDevelopmentProtocolV1) -> None:
    if draft.protocol_id is None:
        raise ValueError("layer-two alpha development protocol_id is missing")
    expected = compute_protocol_id(draft)
    if draft.protocol_id != expected:
        raise ValueError("layer-two alpha development protocol_id does not match canonical content hash")


def assert_status_ready_consistency(draft: LayerTwoAlphaDevelopmentProtocolV1) -> None:
    readiness = draft.readiness
    if (
        readiness.ready_for_scoring
        or readiness.ready_for_backtest
        or readiness.ready_for_portfolio_construction
        or readiness.ready_for_data
        or readiness.ready_for_orders
        or readiness.ready_for_trading
        or readiness.auto_apply
    ):
        raise ValueError("status/ready contradiction: ready flags must remain false")
    if draft.status != "confirmed_for_development_but_not_ready":
        raise ValueError("status/ready contradiction: status must be confirmed_for_development_but_not_ready")
    if not readiness.research_only:
        raise ValueError("status/ready contradiction: research_only must remain true")


def assert_bound_upstream_ids(draft: LayerTwoAlphaDevelopmentProtocolV1) -> None:
    if draft.research_trial_ledger_id != BOUND_RESEARCH_TRIAL_LEDGER_ID:
        raise ValueError("research_trial_ledger_id does not match bound research trial ledger")
    if draft.research_trial_ledger_path != BOUND_RESEARCH_TRIAL_LEDGER_PATH:
        raise ValueError("research_trial_ledger_path does not match bound research trial ledger path")
    if draft.two_layer_decision_contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
        raise ValueError("two_layer_decision_contract_id does not match bound two-layer contract")
    if draft.two_layer_decision_contract_path != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two_layer_decision_contract_path does not match bound two-layer contract path")
    if draft.tranche_evaluation_protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("tranche_evaluation_protocol_id does not match bound tranche protocol")
    if draft.tranche_evaluation_protocol_path != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
        raise ValueError("tranche_evaluation_protocol_path does not match bound tranche protocol path")
    if draft.layer_two_allocation_protocol_id != BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_ID:
        raise ValueError("layer_two_allocation_protocol_id does not match bound allocation protocol")
    if draft.layer_two_allocation_protocol_path != BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_PATH:
        raise ValueError("layer_two_allocation_protocol_path does not match bound allocation protocol path")


def assert_matches_sealed_factory_canonical(draft: LayerTwoAlphaDevelopmentProtocolV1) -> None:
    canonical = build_confirmed_layer_two_alpha_development_protocol_v1()
    if draft.protocol_id != canonical.protocol_id:
        raise ValueError("layer-two alpha development protocol_id does not match sealed factory canonical protocol_id")
    if canonical_protocol_payload(draft) != canonical_protocol_payload(canonical):
        raise ValueError("layer-two alpha development protocol canonical payload does not match sealed factory")


def verify_layer_two_alpha_development_protocol(
    draft: LayerTwoAlphaDevelopmentProtocolV1,
) -> LayerTwoAlphaDevelopmentProtocolVerificationResult:
    assert_protocol_self_hash(draft)
    assert_status_ready_consistency(draft)
    assert_bound_upstream_ids(draft)
    assert_matches_sealed_factory_canonical(draft)
    assert_windows_non_overlapping(draft.windows)
    path_blockers = [f"{b.category}:{b.path}" for b in draft.evidence_blockers]
    return LayerTwoAlphaDevelopmentProtocolVerificationResult(
        protocol_id=draft.protocol_id or compute_protocol_id(draft),
        schema_version="1",
        protocol_version=draft.protocol_version,
        status=draft.status,
        structural_ok=True,
        research_trial_ledger_id=draft.research_trial_ledger_id,
        research_trial_ledger_path=draft.research_trial_ledger_path,
        research_trial_ledger_binding_ok=False,
        two_layer_decision_contract_id=draft.two_layer_decision_contract_id,
        two_layer_decision_contract_path=draft.two_layer_decision_contract_path,
        two_layer_decision_contract_binding_ok=False,
        tranche_evaluation_protocol_id=draft.tranche_evaluation_protocol_id,
        tranche_evaluation_protocol_path=draft.tranche_evaluation_protocol_path,
        tranche_evaluation_protocol_binding_ok=False,
        layer_two_allocation_protocol_id=draft.layer_two_allocation_protocol_id,
        layer_two_allocation_protocol_path=draft.layer_two_allocation_protocol_path,
        layer_two_allocation_protocol_binding_ok=False,
        resolved=False,
        user_decisions_resolved=True,
        pending_user_decision_count=0,
        pending_user_decisions=[],
        blockers=path_blockers,
        evidence_blockers=list(draft.evidence_blockers),
    )


def load_layer_two_alpha_development_protocol(path: Path) -> LayerTwoAlphaDevelopmentProtocolV1:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("layer-two alpha development protocol is missing or invalid") from exc
    if isinstance(payload, dict):
        windows = payload.get("windows")
        if isinstance(windows, dict):
            development = windows.get("development")
            consumed = windows.get("consumed_oos")
            if isinstance(development, dict) and isinstance(consumed, dict):
                dev_end = development.get("end")
                con_start = consumed.get("start")
                if dev_end == "2023-12-31" and con_start == "2023-01-01":
                    raise ValueError("consumed_oos must not overlap development window")
    try:
        return LayerTwoAlphaDevelopmentProtocolV1.model_validate(payload)
    except Exception as exc:
        raise ValueError("layer-two alpha development protocol is missing or invalid") from exc


def verify_layer_two_alpha_development_protocol_file(
    *,
    protocol_path: Path,
    repo_root: Path,
    reference_date: date | None = None,
) -> tuple[LayerTwoAlphaDevelopmentProtocolV1, LayerTwoAlphaDevelopmentProtocolVerificationResult]:
    root = Path(repo_root).resolve()
    draft = load_layer_two_alpha_development_protocol(protocol_path)
    structural = verify_layer_two_alpha_development_protocol(draft)

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
        raise ValueError("research trial ledger_id on disk does not match bound constant")
    if str(DEFAULT_RESEARCH_TRIAL_LEDGER_PATH) != BOUND_RESEARCH_TRIAL_LEDGER_PATH:
        raise ValueError("research trial ledger default path drifted from protocol binding")

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

    tranche_path = _assert_repo_relative_path(
        draft.tranche_evaluation_protocol_path,
        repo_root=root,
        expected=BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
        field_name="tranche_evaluation_protocol_path",
    )
    tranche = load_tranche_evaluation_protocol_draft(tranche_path)
    tranche_result = verify_tranche_evaluation_protocol_draft(tranche, reference_date=reference_date)
    if tranche_result.schema_version != "2":
        raise ValueError("bound tranche evaluation protocol must be schema version 2")
    if tranche_result.protocol_id != draft.tranche_evaluation_protocol_id:
        raise ValueError(
            "tranche evaluation protocol_id on disk does not match protocol tranche_evaluation_protocol_id"
        )
    if tranche_result.protocol_id != BOUND_TRANCHE_EVALUATION_PROTOCOL_ID:
        raise ValueError("tranche evaluation protocol_id on disk does not match bound constant")
    if str(DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH) != BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH:
        raise ValueError("tranche evaluation protocol default path drifted from protocol binding")

    allocation_path = _assert_repo_relative_path(
        draft.layer_two_allocation_protocol_path,
        repo_root=root,
        expected=BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_PATH,
        field_name="layer_two_allocation_protocol_path",
    )
    allocation = load_layer_two_allocation_protocol(allocation_path)
    allocation_result = verify_layer_two_allocation_protocol(allocation)
    if allocation_result.schema_version != "1":
        raise ValueError("bound layer-two allocation protocol must be schema version 1")
    if allocation_result.protocol_id != draft.layer_two_allocation_protocol_id:
        raise ValueError(
            "layer-two allocation protocol_id on disk does not match protocol layer_two_allocation_protocol_id"
        )
    if allocation_result.protocol_id != BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_ID:
        raise ValueError("layer-two allocation protocol_id on disk does not match bound constant")
    if str(DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH) != BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_PATH:
        raise ValueError("layer-two allocation protocol default path drifted from protocol binding")

    # Cross-check the real file verifiers were not bypassed by reusing their entry points once each.
    _ledger_doc, _ledger_result = verify_research_trial_ledger(ledger_path=ledger_path, repo_root=root)
    _contract_doc, _contract_file_result = verify_two_layer_decision_draft_file(
        draft_path=contract_path,
        repo_root=root,
        reference_date=reference_date,
    )
    _tranche_doc, _tranche_file_result = verify_tranche_evaluation_protocol_draft_file(
        protocol_path=tranche_path,
        repo_root=root,
        reference_date=reference_date,
    )
    _allocation_doc, _allocation_file_result = verify_layer_two_allocation_protocol_file(
        protocol_path=allocation_path,
        repo_root=root,
        reference_date=reference_date,
    )
    if _contract_file_result.research_trial_ledger_binding_ok is not True:
        raise ValueError("two-layer decision file verifier must confirm research trial ledger binding")
    if _tranche_file_result.research_trial_ledger_binding_ok is not True:
        raise ValueError("tranche evaluation file verifier must confirm research trial ledger binding")
    if _allocation_file_result.two_layer_decision_contract_binding_ok is not True:
        raise ValueError("allocation file verifier must confirm two-layer decision contract binding")

    result = structural.model_copy(
        update={
            "research_trial_ledger_binding_ok": True,
            "two_layer_decision_contract_binding_ok": True,
            "tranche_evaluation_protocol_binding_ok": True,
            "layer_two_allocation_protocol_binding_ok": True,
        }
    )
    return draft, result


def write_layer_two_alpha_development_protocol(
    path: Path,
    draft: LayerTwoAlphaDevelopmentProtocolV1,
) -> LayerTwoAlphaDevelopmentProtocolV1:
    sealed = seal_layer_two_alpha_development_protocol(draft)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_ID",
    "BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_PATH",
    "BOUND_RESEARCH_TRIAL_LEDGER_ID",
    "BOUND_RESEARCH_TRIAL_LEDGER_PATH",
    "BOUND_TRANCHE_EVALUATION_PROTOCOL_ID",
    "BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_ID",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_PATH",
    "CONFIRMED_FACTOR_FAMILIES",
    "CONFIRMED_SIZE_BANDS",
    "DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH",
    "HOLM_TIE_BREAK_FACTOR_FAMILY_ORDER",
    "LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_SCHEMA_VERSION",
    "LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_VERSION",
    "REQUIRED_ALPHA_DEVELOPMENT_EVIDENCE_BLOCKERS",
    "REQUIRED_INFERENCE_REPORT_FIELDS",
    "ClusterCompanionPolicy",
    "CrossSectionRankingPolicy",
    "DefensiveLowVolFactorFormula",
    "EligibilityDenominatorPolicy",
    "ForwardLabelAndPoolingPolicy",
    "HolmStepDownExactAlgorithm",
    "InferencePolicy",
    "LabelsAndEvidencePolicy",
    "LayerTwoAlphaDevelopmentProtocolV1",
    "LayerTwoAlphaDevelopmentProtocolVerificationResult",
    "MediumMomentumFactorFormula",
    "NeweyWestBartlettExactAlgorithm",
    "PitSnapshotBindingPolicy",
    "ProtocolEvidenceBlocker",
    "QuintileSemanticsPolicy",
    "SizeBand",
    "SizeBandDiagnosticSafeguards",
    "SpearmanIcSemanticsPolicy",
    "assert_bound_upstream_ids",
    "assert_matches_sealed_factory_canonical",
    "assert_protocol_self_hash",
    "assert_status_ready_consistency",
    "assert_windows_non_overlapping",
    "build_confirmed_layer_two_alpha_development_protocol_v1",
    "canonical_protocol_bytes",
    "canonical_protocol_payload",
    "compute_protocol_id",
    "default_alpha_evidence_blockers",
    "default_alpha_research_windows",
    "default_factor_families",
    "load_layer_two_alpha_development_protocol",
    "seal_layer_two_alpha_development_protocol",
    "verify_layer_two_alpha_development_protocol",
    "verify_layer_two_alpha_development_protocol_file",
    "write_layer_two_alpha_development_protocol",
]
