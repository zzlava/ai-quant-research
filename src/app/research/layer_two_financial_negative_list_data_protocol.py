"""E11b-2a: Frozen PIT financial-negative-list data protocol.

Strict frozen Pydantic models, canonical JSON SHA-256 self-sealed verifier,
path-safe, 2025/OOS rejecting. Defines exact source endpoints, PIT availability
semantics, rule math thresholds, scope, and freshness for the E10b adjudicator.

This module does NOT:
- Modify the existing E10b evaluator
- Deploy rules
- Collect data or read any token
- Materialize verdicts
- Touch 2025+ data
- Score/IC/backtest/trade
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

PROTOCOL_FILE_PATH: Literal["config/research/layer-two-financial-negative-list-data-protocol-v1.json"] = (
    "config/research/layer-two-financial-negative-list-data-protocol-v1.json"
)

PROTOCOL_ID: Literal["314e9d644b897ed4398cc349e3772b09bbe6f80cfd2d518a7cdbf19bb651d2ea"] = (
    "314e9d644b897ed4398cc349e3772b09bbe6f80cfd2d518a7cdbf19bb651d2ea"
)

BOUND_TWO_LAYER_CONTRACT_ID: Literal["27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6"] = (
    "27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6"
)

BOUND_TWO_LAYER_CONTRACT_PATH: Literal["config/research/two-layer-strategy-decision-draft-v1.json"] = (
    "config/research/two-layer-strategy-decision-draft-v1.json"
)

BOUND_TWO_LAYER_CONTRACT_FILE_SHA256: Literal["0e1afbf963c5d5b11e6db86d8fb5f7ccec3c364eb304c2227e7d9ae9eda345f6"] = (
    "0e1afbf963c5d5b11e6db86d8fb5f7ccec3c364eb304c2227e7d9ae9eda345f6"
)

BOUND_E10B_ENGINE_VERSION: Literal["layer-two-financial-negative-list-engine-v1"] = (
    "layer-two-financial-negative-list-engine-v1"
)

BOUND_E10B_MODULE_PATH: Literal["src/app/research/layer_two_financial_negative_list.py"] = (
    "src/app/research/layer_two_financial_negative_list.py"
)

BOUND_E10B_MODULE_SHA256: Literal["5eba8c96392620bcae54f41dd532756ad528a3e5bbc939d134987e309f4fc15c"] = (
    "5eba8c96392620bcae54f41dd532756ad528a3e5bbc939d134987e309f4fc15c"
)

BOUND_CANDIDATE_PACK_PATH: Literal["data/all-a-share-historical-v1/research/candidate-eligibility-pack-v1"] = (
    "data/all-a-share-historical-v1/research/candidate-eligibility-pack-v1"
)

BOUND_CANDIDATE_PACK_ID: Literal["cd904a7974d019689d933bcb0c0e329e51e38f96a26e30cdea4c5b15aaa4d09e"] = (
    "cd904a7974d019689d933bcb0c0e329e51e38f96a26e30cdea4c5b15aaa4d09e"
)

BOUND_CANDIDATE_PACK_PARQUET_SHA256: Literal["6f6518828df99f7111b6632e0fad51335e6feaa8e06a65dc06457e062a65ffd4"] = (
    "6f6518828df99f7111b6632e0fad51335e6feaa8e06a65dc06457e062a65ffd4"
)

BOUND_CANDIDATE_PACK_ROW_COUNT: Literal[3597408] = 3597408

BOUND_CANDIDATE_PACK_COVERAGE_START: Literal["2022-01-01"] = "2022-01-01"
BOUND_CANDIDATE_PACK_COVERAGE_END: Literal["2024-12-31"] = "2024-12-31"

BOUND_RAW_COLLECTION_DIR: Literal["data/raw/all-a-share-history-20211008-20241231-v1"] = (
    "data/raw/all-a-share-history-20211008-20241231-v1"
)

BOUND_RAW_COLLECTION_REQUEST_ID: Literal["0b1e4abf58af7c68e7e00e2ecddc7b205010e8a9f26c6c2bb9f7a81e0699f7d1"] = (
    "0b1e4abf58af7c68e7e00e2ecddc7b205010e8a9f26c6c2bb9f7a81e0699f7d1"
)

BOUND_RAW_COLLECTION_MANIFEST_SHA256: Literal["2e79423dbcfd49dca8148960071495d45abcb36c439b97f226f29ddd6757bbfa"] = (
    "2e79423dbcfd49dca8148960071495d45abcb36c439b97f226f29ddd6757bbfa"
)

BOUND_RAW_QUALITY_REPORT_SHA256: Literal["8fe834efd812d685228ad8a74733270e9526ea8b1ade876f349cb29da4b00081"] = (
    "8fe834efd812d685228ad8a74733270e9526ea8b1ade876f349cb29da4b00081"
)

DECISION_WINDOW_START = date(2022, 1, 1)
DECISION_WINDOW_END = date(2024, 12, 31)

ANNOUNCEMENT_COLLECTION_START = date(2020, 1, 1)
ANNOUNCEMENT_COLLECTION_END = date(2024, 12, 31)

DECISION_TIME = time(17, 30, 0)
AVAILABLE_AT_TIME = time(23, 59, 59)
ASIA_SHANGHAI = timezone(offset=timedelta(hours=8))

INCLUDED_REPORT_TYPES: frozenset[int] = frozenset({1, 4, 5})
EXCLUDED_REPORT_TYPES: frozenset[int] = frozenset({2, 3, 6, 7, 8, 9, 10, 11, 12})

MAX_REPORT_PERIOD_AGE_AUDIT_DAYS = 550
MAX_REPORT_PERIOD_AGE_STATEMENT_DAYS = 240

AUDIT_CLEAN_VALUE: Literal["标准无保留意见"] = "标准无保留意见"

THRESHOLD_CASH_DEBT_RATIO = 0.25
THRESHOLD_RECEIVABLES_REVENUE_GAP = 0.20
THRESHOLD_OTHER_RECEIVABLES_RATIO = 0.05
THRESHOLD_GOODWILL_RATIO = 0.30

DEBT_COMPONENT_FIELDS: tuple[str, ...] = (
    "st_borr",
    "lt_borr",
    "st_bonds_payable",
    "non_cur_liab_due_1y",
    "bond_payable",
)

SOURCE_ENDPOINT_DOCS: dict[str, str] = {
    "balancesheet": "https://tushare.pro/document/2?doc_id=36",
    "income": "https://tushare.pro/document/2?doc_id=33",
    "fina_indicator": "https://tushare.pro/document/2?doc_id=79",
    "fina_audit": "https://tushare.pro/document/2?doc_id=80",
}

EXPECTED_ISSUE_CODES: dict[str, str] = {
    "FNLD-001": "missing_ann_date_row_unusable",
    "FNLD-002": "same_day_disclosure_unusable_at_decision",
    "FNLD-003": "report_type_excluded",
    "FNLD-004": "comp_type_not_general_industrial",
    "FNLD-005": "conflicting_values_same_key_unknown",
    "FNLD-006": "ambiguous_restatement_chronology",
    "FNLD-007": "stale_report_period_beyond_max_age",
    "FNLD-008": "partial_debt_components_not_zero_filled",
    "FNLD-009": "no_silent_total_revenue_fallback",
    "FNLD-010": "negative_numerator_invalid",
    "FNLD-011": "nonpositive_denominator_invalid",
    "FNLD-012": "future_date_rejected",
    "FNLD-013": "oos_2025_plus_rejected",
}

EXPECTED_SCHEMA_VERSION: Literal["1"] = "1"
EXPECTED_PROTOCOL_VERSION: Literal["layer-two-financial-negative-list-data-protocol-v1"] = (
    "layer-two-financial-negative-list-data-protocol-v1"
)
EXPECTED_STATUS: Literal["frozen_for_development"] = "frozen_for_development"
RULE_A_LOGIC = (
    "audit_result exactly equals clean_value => false; any other nonblank result => true; missing/conflict => unknown"
)
RULE_B_CONDITIONS: tuple[str, ...] = (
    "total_assets > 0",
    "money_cap / total_assets > 0.25",
    "interestdebt / total_assets > 0.25",
)
RULE_C_GAP_FORMULA = "(exposure_current / exposure_prior - 1) - (revenue_current / revenue_prior - 1)"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

SOURCE_ENDPOINTS: tuple[str, ...] = (
    "balancesheet",
    "income",
    "fina_indicator",
    "fina_audit",
)

BALANCESHEET_FIELDS: tuple[str, ...] = (
    "ts_code",
    "ann_date",
    "f_ann_date",
    "end_date",
    "report_type",
    "comp_type",
    "end_type",
    "money_cap",
    "notes_receiv",
    "accounts_receiv",
    "oth_receiv",
    "inventories",
    "goodwill",
    "total_assets",
    "st_borr",
    "lt_borr",
    "st_bonds_payable",
    "non_cur_liab_due_1y",
    "bond_payable",
    "total_hldr_eqy_exc_min_int",
    "update_flag",
)

INCOME_FIELDS: tuple[str, ...] = (
    "ts_code",
    "ann_date",
    "f_ann_date",
    "end_date",
    "report_type",
    "comp_type",
    "end_type",
    "revenue",
    "total_revenue",
    "update_flag",
)

FINA_INDICATOR_FIELDS: tuple[str, ...] = (
    "ts_code",
    "ann_date",
    "end_date",
    "interestdebt",
    "update_flag",
)

FINA_AUDIT_FIELDS: tuple[str, ...] = (
    "ts_code",
    "ann_date",
    "end_date",
    "audit_result",
    "audit_fees",
    "audit_agency",
    "audit_sign",
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

_OOS_BOUNDARY_RE = re.compile(r"(?:^|[/\-_])oos(?:$|[/\-_])")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProtocolBindings(_FrozenModel):
    two_layer_decision_contract_id: str = Field(min_length=64, max_length=64)
    two_layer_decision_contract_path: str
    two_layer_decision_contract_file_sha256: str = Field(min_length=64, max_length=64)
    e10b_engine_version: str
    e10b_module_path: str
    e10b_module_sha256: str = Field(min_length=64, max_length=64)
    candidate_pack_path: str
    candidate_pack_id: str = Field(min_length=64, max_length=64)
    candidate_pack_parquet_sha256: str = Field(min_length=64, max_length=64)
    candidate_pack_row_count: int = Field(ge=0)
    candidate_pack_coverage_start: str
    candidate_pack_coverage_end: str
    raw_collection_dir: str
    raw_collection_request_id: str = Field(min_length=64, max_length=64)
    raw_collection_manifest_sha256: str = Field(min_length=64, max_length=64)
    raw_quality_report_sha256: str = Field(min_length=64, max_length=64)


class ProtocolDecisionWindow(_FrozenModel):
    start: str
    end: str
    note: str | None = None

    @model_validator(mode="after")
    def _exact_window(self) -> ProtocolDecisionWindow:
        if self.start != DECISION_WINDOW_START.isoformat() or self.end != DECISION_WINDOW_END.isoformat():
            raise ValueError("decision_window must exactly match frozen dates")
        return self


class ProtocolAnnouncementWindow(_FrozenModel):
    start: str
    end: str
    note: str | None = None

    @model_validator(mode="after")
    def _exact_window(self) -> ProtocolAnnouncementWindow:
        if (
            self.start != ANNOUNCEMENT_COLLECTION_START.isoformat()
            or self.end != ANNOUNCEMENT_COLLECTION_END.isoformat()
        ):
            raise ValueError("source_announcement_collection_window must exactly match frozen dates")
        return self


class ProtocolSourceEndpointEntry(_FrozenModel):
    tushare_api: str
    official_doc: str
    fields: tuple[str, ...]
    partition_key: str
    resumable: bool


class ProtocolSourceEndpoints(_FrozenModel):
    balancesheet: ProtocolSourceEndpointEntry
    income: ProtocolSourceEndpointEntry
    fina_indicator: ProtocolSourceEndpointEntry
    fina_audit: ProtocolSourceEndpointEntry

    @model_validator(mode="after")
    def _exact_source_endpoints(self) -> ProtocolSourceEndpoints:
        expected: dict[str, tuple[str, str, tuple[str, ...], str, bool]] = {
            "balancesheet": (
                "balancesheet",
                SOURCE_ENDPOINT_DOCS["balancesheet"],
                BALANCESHEET_FIELDS,
                "ts_code",
                True,
            ),
            "income": ("income", SOURCE_ENDPOINT_DOCS["income"], INCOME_FIELDS, "ts_code", True),
            "fina_indicator": (
                "fina_indicator",
                SOURCE_ENDPOINT_DOCS["fina_indicator"],
                FINA_INDICATOR_FIELDS,
                "ts_code",
                True,
            ),
            "fina_audit": ("fina_audit", SOURCE_ENDPOINT_DOCS["fina_audit"], FINA_AUDIT_FIELDS, "ts_code", True),
        }
        actual_entries = {
            "balancesheet": self.balancesheet,
            "income": self.income,
            "fina_indicator": self.fina_indicator,
            "fina_audit": self.fina_audit,
        }
        for endpoint in SOURCE_ENDPOINTS:
            entry = actual_entries[endpoint]
            api, doc, fields, partition_key, resumable = expected[endpoint]
            if entry.tushare_api != api:
                raise ValueError(f"source_endpoints.{endpoint}.tushare_api must exactly match")
            if entry.official_doc != doc:
                raise ValueError(f"source_endpoints.{endpoint}.official_doc must exactly match")
            if entry.fields != fields:
                raise ValueError(f"source_endpoints.{endpoint}.fields must exactly match ordered tuple")
            if entry.partition_key != partition_key:
                raise ValueError(f"source_endpoints.{endpoint}.partition_key must exactly match")
            if entry.resumable is not resumable:
                raise ValueError(f"source_endpoints.{endpoint}.resumable must exactly match")
        return self


class ProtocolCollectionSemantics(_FrozenModel):
    partition_per_endpoint_symbol: bool
    raw_response_row_hashes: bool
    request_quality_collection_manifests: bool
    complete_empty_partitions_allowed_after_audited_response: bool
    truncation_fail_closed: bool
    duplicate_fail_closed: bool
    conflict_fail_closed: bool
    collected_at_never_becomes_available_at: bool
    note: str | None = None

    @model_validator(mode="after")
    def _exact_fail_closed_semantics(self) -> ProtocolCollectionSemantics:
        expected_true_flags = (
            self.partition_per_endpoint_symbol,
            self.raw_response_row_hashes,
            self.request_quality_collection_manifests,
            self.complete_empty_partitions_allowed_after_audited_response,
            self.truncation_fail_closed,
            self.duplicate_fail_closed,
            self.conflict_fail_closed,
            self.collected_at_never_becomes_available_at,
        )
        if not all(expected_true_flags):
            raise ValueError("collection_semantics flags must exactly match frozen fail-closed semantics")
        return self


class ProtocolPitAvailability(_FrozenModel):
    effective_disclosure_date: str
    available_at_time: str
    same_day_decision_unusable: bool
    first_usable_at_next_decision_date: bool
    missing_invalid_ann_date_row_unusable: bool
    all_evidence_requires_available_at_le_decision_at: bool
    preserve_every_retrieved_version: bool
    preserve_source_row_hash: bool
    never_overwrite_older_rows: bool
    report_type_included: tuple[int, ...]
    report_type_included_meaning: str
    report_type_excluded: tuple[int, ...]
    update_flag_is_metadata_not_availability: bool
    same_key_conflicting_values_unknown: bool
    ambiguous_restatement_chronology_unknown: bool
    note: str | None = None

    @model_validator(mode="after")
    def _exact_pit_semantics(self) -> ProtocolPitAvailability:
        if self.effective_disclosure_date != "max(valid ann_date, valid f_ann_date when present)":
            raise ValueError("pit_availability.effective_disclosure_date must exactly match")
        if self.available_at_time != "23:59:59 Asia/Shanghai on effective disclosure date":
            raise ValueError("pit_availability.available_at_time must exactly match")
        if (
            not self.same_day_decision_unusable
            or not self.first_usable_at_next_decision_date
            or not self.missing_invalid_ann_date_row_unusable
            or not self.all_evidence_requires_available_at_le_decision_at
            or not self.preserve_every_retrieved_version
            or not self.preserve_source_row_hash
            or not self.never_overwrite_older_rows
            or not self.update_flag_is_metadata_not_availability
            or not self.same_key_conflicting_values_unknown
            or not self.ambiguous_restatement_chronology_unknown
        ):
            raise ValueError("pit_availability flags must exactly match frozen PIT fail-closed semantics")
        if self.report_type_included != tuple(sorted(INCLUDED_REPORT_TYPES)):
            raise ValueError("pit_availability.report_type_included must exactly match ordered tuple")
        if self.report_type_included_meaning != "consolidated cumulative statements only":
            raise ValueError("pit_availability.report_type_included_meaning must exactly match")
        if self.report_type_excluded != tuple(sorted(EXCLUDED_REPORT_TYPES)):
            raise ValueError("pit_availability.report_type_excluded must exactly match ordered tuple")
        return self


class ProtocolScope(_FrozenModel):
    generic_rules_require_comp_type_1: bool
    comp_type_1_meaning: str
    comp_type_2_bank_excluded: bool
    comp_type_3_insurer_excluded: bool
    comp_type_4_securities_excluded: bool
    missing_conflicting_comp_type_generic_rules_unknown: bool
    financial_sector_insufficient_evidence: bool
    fina_audit_applies_to_all: bool
    future_financial_sector_v2_backlog: bool
    note: str | None = None

    @model_validator(mode="after")
    def _exact_scope(self) -> ProtocolScope:
        if not self.generic_rules_require_comp_type_1:
            raise ValueError("scope.generic_rules_require_comp_type_1 must be true")
        if self.comp_type_1_meaning != "general industrial":
            raise ValueError("scope.comp_type_1_meaning must exactly match")
        if (
            not self.comp_type_2_bank_excluded
            or not self.comp_type_3_insurer_excluded
            or not self.comp_type_4_securities_excluded
            or not self.missing_conflicting_comp_type_generic_rules_unknown
            or not self.financial_sector_insufficient_evidence
            or not self.fina_audit_applies_to_all
            or not self.future_financial_sector_v2_backlog
        ):
            raise ValueError("scope flags must exactly match frozen semantics")
        return self


class ProtocolComponentDebtCrosscheck(_FrozenModel):
    fields: tuple[str, ...]
    all_five_must_be_explicit_numeric: bool
    partial_components_never_zero_filled: bool
    purpose: str

    @model_validator(mode="after")
    def _exact_crosscheck_semantics(self) -> ProtocolComponentDebtCrosscheck:
        if self.fields != DEBT_COMPONENT_FIELDS:
            raise ValueError("component_debt_crosscheck.fields must exactly match ordered tuple")
        if not self.all_five_must_be_explicit_numeric or not self.partial_components_never_zero_filled:
            raise ValueError("component_debt_crosscheck flags must exactly match")
        if self.purpose != "cross-check/fallback only if all five are explicit numeric":
            raise ValueError("component_debt_crosscheck.purpose must exactly match")
        return self


class ProtocolRuleA(_FrozenModel):
    code: str
    source_endpoint: str
    applies_to_all_comp_types: bool
    latest_usable_annual_audit: bool
    max_report_period_age_days: int
    clean_value: str
    logic: str
    comparison: str
    note: str | None = None

    @model_validator(mode="after")
    def _exact_rule_a(self) -> ProtocolRuleA:
        if self.code != "non_standard_audit" or self.source_endpoint != "fina_audit":
            raise ValueError("rules.A_non_standard_audit code/source must exactly match")
        if not self.applies_to_all_comp_types or not self.latest_usable_annual_audit:
            raise ValueError("rules.A_non_standard_audit scope/latest flags must be true")
        if self.max_report_period_age_days != MAX_REPORT_PERIOD_AGE_AUDIT_DAYS:
            raise ValueError("rules.A_non_standard_audit max_report_period_age_days must exactly match")
        if self.clean_value != AUDIT_CLEAN_VALUE:
            raise ValueError("rules.A_non_standard_audit clean_value must exactly match")
        if self.logic != RULE_A_LOGIC:
            raise ValueError("rules.A_non_standard_audit logic must exactly match")
        if self.comparison != "exact string equality":
            raise ValueError("rules.A_non_standard_audit comparison must exactly match")
        return self


class ProtocolRuleB(_FrozenModel):
    code: str
    source_endpoints: tuple[str, ...]
    requires_comp_type_1: bool
    latest_usable_consolidated_cumulative: bool
    max_report_period_age_days: int
    conditions: tuple[str, ...]
    interestdebt_source: str
    component_debt_crosscheck: ProtocolComponentDebtCrosscheck
    threshold_operator: str
    missing_conflict_unknown: bool
    note: str | None = None

    @model_validator(mode="after")
    def _exact_rule_b(self) -> ProtocolRuleB:
        if self.code != "large_cash_and_interest_bearing_debt":
            raise ValueError("rules.B_large_cash_and_interest_bearing_debt.code must exactly match")
        if self.source_endpoints != ("balancesheet", "fina_indicator"):
            raise ValueError("rules.B_large_cash_and_interest_bearing_debt.source_endpoints must exactly match")
        if not self.requires_comp_type_1 or not self.latest_usable_consolidated_cumulative:
            raise ValueError("rules.B_large_cash_and_interest_bearing_debt scope/latest flags must be true")
        if self.max_report_period_age_days != MAX_REPORT_PERIOD_AGE_STATEMENT_DAYS:
            raise ValueError("rules.B_large_cash_and_interest_bearing_debt freshness must exactly match")
        if self.conditions != RULE_B_CONDITIONS:
            raise ValueError("rules.B_large_cash_and_interest_bearing_debt.conditions must exactly match")
        if self.interestdebt_source != "fina_indicator same-period latest usable":
            raise ValueError("rules.B_large_cash_and_interest_bearing_debt.interestdebt_source must exactly match")
        if self.threshold_operator != "strict_greater_than" or not self.missing_conflict_unknown:
            raise ValueError("rules.B_large_cash_and_interest_bearing_debt threshold/missing flags must exactly match")
        return self


class ProtocolRuleC(_FrozenModel):
    code: str
    source_endpoints: tuple[str, ...]
    requires_comp_type_1: bool
    exposure_formula: str
    exposure_requires_all_three_explicit_numeric: bool
    revenue_field: str
    no_silent_total_revenue_fallback: bool
    consecutive_periods: int
    standard_quarter_periods: bool
    each_period_requires_exact_prior_year_same_quarter: bool
    all_prior_denominators_must_be_positive: bool
    gap_formula: str
    true_condition: str
    false_condition: str
    missing_skipped_nonconsecutive_conflict_unknown: bool
    threshold_value: float
    threshold_operator: str
    note: str | None = None

    @model_validator(mode="after")
    def _exact_rule_c(self) -> ProtocolRuleC:
        if self.code != "receivables_inventory_growth_vs_revenue_two_periods":
            raise ValueError("rules.C_receivables_inventory_growth_vs_revenue_two_periods.code must exactly match")
        if self.source_endpoints != ("balancesheet", "income"):
            raise ValueError(
                "rules.C_receivables_inventory_growth_vs_revenue_two_periods.source_endpoints must exactly match"
            )
        if not self.requires_comp_type_1:
            raise ValueError(
                "rules.C_receivables_inventory_growth_vs_revenue_two_periods.requires_comp_type_1 must be true"
            )
        if self.exposure_formula != "notes_receiv + accounts_receiv + inventories":
            raise ValueError(
                "rules.C_receivables_inventory_growth_vs_revenue_two_periods.exposure_formula must exactly match"
            )
        if (
            not self.exposure_requires_all_three_explicit_numeric
            or self.revenue_field != "revenue"
            or not self.no_silent_total_revenue_fallback
            or self.consecutive_periods != 2
            or not self.standard_quarter_periods
            or not self.each_period_requires_exact_prior_year_same_quarter
            or not self.all_prior_denominators_must_be_positive
            or self.gap_formula != RULE_C_GAP_FORMULA
            or self.true_condition != "both consecutive gaps > 0.20"
            or self.false_condition != "complete and not both true"
            or not self.missing_skipped_nonconsecutive_conflict_unknown
            or self.threshold_value != THRESHOLD_RECEIVABLES_REVENUE_GAP
            or self.threshold_operator != "strict_greater_than"
        ):
            raise ValueError("rules.C_receivables_inventory_growth_vs_revenue_two_periods semantics must exactly match")
        return self


class ProtocolRuleD(_FrozenModel):
    code: str
    source_endpoint: str
    requires_comp_type_1: bool
    formula: str
    threshold: float
    threshold_operator: str
    denominator_must_be_positive: bool
    negative_numerator_invalid_unknown: bool
    latest_usable: bool
    missing_unknown: bool
    note: str | None = None

    @model_validator(mode="after")
    def _exact_rule_d(self) -> ProtocolRuleD:
        if self.code != "other_receivables_to_assets_over_5pct" or self.source_endpoint != "balancesheet":
            raise ValueError("rules.D_other_receivables_to_assets code/source must exactly match")
        if (
            not self.requires_comp_type_1
            or self.formula != "oth_receiv / total_assets"
            or self.threshold != THRESHOLD_OTHER_RECEIVABLES_RATIO
            or self.threshold_operator != "strict_greater_than"
            or not self.denominator_must_be_positive
            or not self.negative_numerator_invalid_unknown
            or not self.latest_usable
            or not self.missing_unknown
        ):
            raise ValueError("rules.D_other_receivables_to_assets semantics must exactly match")
        return self


class ProtocolRuleE(_FrozenModel):
    code: str
    source_endpoint: str
    requires_comp_type_1: bool
    formula: str
    threshold: float
    threshold_operator: str
    denominator_must_be_positive: bool
    nonpositive_equity_unknown: bool
    negative_numerator_invalid_unknown: bool
    latest_usable: bool
    missing_unknown: bool
    note: str | None = None

    @model_validator(mode="after")
    def _exact_rule_e(self) -> ProtocolRuleE:
        if self.code != "goodwill_to_net_assets_over_30pct" or self.source_endpoint != "balancesheet":
            raise ValueError("rules.E_goodwill_to_net_assets code/source must exactly match")
        if (
            not self.requires_comp_type_1
            or self.formula != "goodwill / total_hldr_eqy_exc_min_int"
            or self.threshold != THRESHOLD_GOODWILL_RATIO
            or self.threshold_operator != "strict_greater_than"
            or not self.denominator_must_be_positive
            or not self.nonpositive_equity_unknown
            or not self.negative_numerator_invalid_unknown
            or not self.latest_usable
            or not self.missing_unknown
        ):
            raise ValueError("rules.E_goodwill_to_net_assets semantics must exactly match")
        return self


class ProtocolRules(_FrozenModel):
    A_non_standard_audit: ProtocolRuleA
    B_large_cash_and_interest_bearing_debt: ProtocolRuleB
    C_receivables_inventory_growth_vs_revenue_two_periods: ProtocolRuleC
    D_other_receivables_to_assets: ProtocolRuleD
    E_goodwill_to_net_assets: ProtocolRuleE


class ProtocolFreshness(_FrozenModel):
    max_report_period_age_audit_days: int
    max_report_period_age_statement_days: int
    age_is_calendar_days_from_decision_to_end_date: bool
    stale_beyond_max_age_unknown: bool
    note: str | None = None

    @model_validator(mode="after")
    def _exact_freshness(self) -> ProtocolFreshness:
        if self.max_report_period_age_audit_days != MAX_REPORT_PERIOD_AGE_AUDIT_DAYS:
            raise ValueError("freshness.max_report_period_age_audit_days must exactly match")
        if self.max_report_period_age_statement_days != MAX_REPORT_PERIOD_AGE_STATEMENT_DAYS:
            raise ValueError("freshness.max_report_period_age_statement_days must exactly match")
        if not self.age_is_calendar_days_from_decision_to_end_date or not self.stale_beyond_max_age_unknown:
            raise ValueError("freshness flags must exactly match")
        return self


class ProtocolRowVersionResolution(_FrozenModel):
    latest_usable_by_effective_disclosure_date: bool
    effective_disclosure_date_formula: str
    ties_with_same_availability_same_values_deduplicated: bool
    ties_with_same_availability_conflicting_values_unknown: bool
    chronological_restatement_latest_wins_only_if_unambiguous: bool
    ambiguous_restatement_sequence_unknown: bool
    note: str | None = None

    @model_validator(mode="after")
    def _exact_row_version_resolution(self) -> ProtocolRowVersionResolution:
        if not self.latest_usable_by_effective_disclosure_date:
            raise ValueError("row_version_resolution.latest_usable_by_effective_disclosure_date must be true")
        if self.effective_disclosure_date_formula != "max(ann_date, f_ann_date) when both valid dates":
            raise ValueError("row_version_resolution.effective_disclosure_date_formula must exactly match")
        if (
            not self.ties_with_same_availability_same_values_deduplicated
            or not self.ties_with_same_availability_conflicting_values_unknown
            or not self.chronological_restatement_latest_wins_only_if_unambiguous
            or not self.ambiguous_restatement_sequence_unknown
        ):
            raise ValueError("row_version_resolution flags must exactly match")
        return self


class ProtocolReadiness(_FrozenModel):
    research_only: bool
    ready_for_scoring: bool
    ready_for_backtest: bool
    ready_for_portfolio_construction: bool
    ready_for_trading: bool
    ready_for_data_collection: bool
    auto_apply: bool
    note: str | None = None

    @model_validator(mode="after")
    def _all_false(self) -> ProtocolReadiness:
        if any(
            [
                self.ready_for_scoring,
                self.ready_for_backtest,
                self.ready_for_portfolio_construction,
                self.ready_for_trading,
                self.ready_for_data_collection,
                self.auto_apply,
            ]
        ):
            raise ValueError("all ready_for_* and auto_apply must be false")
        if not self.research_only:
            raise ValueError("research_only must be true")
        return self


class FinancialNegativeListDataProtocol(_FrozenModel):
    schema_version: str
    protocol_version: str
    status: str
    frozen_as_of: str
    purpose: str
    not_alpha_evidence: bool
    not_authorization: bool
    does_not_modify_e10b_evaluator: bool
    does_not_deploy_rules: bool
    bindings: ProtocolBindings
    decision_window: ProtocolDecisionWindow
    source_announcement_collection_window: ProtocolAnnouncementWindow
    source_endpoints: ProtocolSourceEndpoints
    collection_semantics: ProtocolCollectionSemantics
    pit_availability: ProtocolPitAvailability
    scope: ProtocolScope
    rules: ProtocolRules
    freshness: ProtocolFreshness
    row_version_resolution: ProtocolRowVersionResolution
    issue_codes: dict[str, str]
    outcome_driven_changes_forbidden: bool
    future_change_requires_new_version_and_ledger: bool
    readiness: ProtocolReadiness
    protocol_id: str

    @model_validator(mode="after")
    def _sealed(self) -> FinancialNegativeListDataProtocol:
        if self.schema_version != EXPECTED_SCHEMA_VERSION:
            raise ValueError("schema_version must exactly match frozen value")
        if self.protocol_version != EXPECTED_PROTOCOL_VERSION:
            raise ValueError("protocol_version must exactly match frozen value")
        if self.status != EXPECTED_STATUS:
            raise ValueError("status must exactly match frozen value")
        if not self.not_alpha_evidence:
            raise ValueError("not_alpha_evidence must be true")
        if not self.not_authorization:
            raise ValueError("not_authorization must be true")
        if not self.does_not_modify_e10b_evaluator:
            raise ValueError("does_not_modify_e10b_evaluator must be true")
        if not self.does_not_deploy_rules:
            raise ValueError("does_not_deploy_rules must be true")
        if not self.outcome_driven_changes_forbidden:
            raise ValueError("outcome_driven_changes_forbidden must be true")
        if not self.future_change_requires_new_version_and_ledger:
            raise ValueError("future_change_requires_new_version_and_ledger must be true")
        if self.issue_codes != EXPECTED_ISSUE_CODES:
            raise ValueError("issue_codes must exactly match frozen key/value map")
        return self


# ---------------------------------------------------------------------------
# Canonical hashing & verification
# ---------------------------------------------------------------------------


def compute_protocol_id(data: dict[str, Any]) -> str:
    """Compute canonical SHA-256 of protocol JSON excluding protocol_id field."""
    payload = {k: v for k, v in data.items() if k != "protocol_id"}
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_protocol(path: Path) -> dict[str, Any]:
    """Load protocol JSON from disk."""
    if not path.is_file():
        raise FileNotFoundError(f"protocol file not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def verify_protocol(path: Path, *, repo_root: Path | None = None) -> FinancialNegativeListDataProtocol:
    """Load, verify seal, validate model, check path safety.

    Returns the validated frozen protocol model.
    Raises ValueError on any integrity failure.
    """
    if path.is_symlink():
        raise ValueError("protocol file must not be a symlink")
    resolved = path.resolve()
    if repo_root is not None:
        root = repo_root.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ValueError("protocol file must be inside repo_root") from None

    data = load_protocol(resolved)

    stored_id = data.get("protocol_id")
    if not isinstance(stored_id, str) or not _HEX64.fullmatch(stored_id):
        raise ValueError("protocol_id must be a 64-char lowercase hex string")

    computed_id = compute_protocol_id(data)
    if computed_id != stored_id:
        raise ValueError(f"protocol_id seal mismatch: stored={stored_id}, computed={computed_id}")

    if computed_id != PROTOCOL_ID:
        raise ValueError(f"protocol_id does not match frozen constant: expected {PROTOCOL_ID}, got {computed_id}")

    protocol = FinancialNegativeListDataProtocol(**data)

    _verify_bindings(protocol.bindings)
    _verify_decision_window(data)
    _verify_announcement_window(data)

    return protocol


def _verify_bindings(bindings: ProtocolBindings) -> None:
    """Verify all binding fields match frozen constants."""
    if bindings.two_layer_decision_contract_id != BOUND_TWO_LAYER_CONTRACT_ID:
        raise ValueError("contract_id binding mismatch")
    if bindings.two_layer_decision_contract_path != BOUND_TWO_LAYER_CONTRACT_PATH:
        raise ValueError("contract_path binding mismatch")
    if bindings.two_layer_decision_contract_file_sha256 != BOUND_TWO_LAYER_CONTRACT_FILE_SHA256:
        raise ValueError("contract_file_sha256 binding mismatch")
    if bindings.e10b_engine_version != BOUND_E10B_ENGINE_VERSION:
        raise ValueError("e10b_engine_version binding mismatch")
    if bindings.e10b_module_path != BOUND_E10B_MODULE_PATH:
        raise ValueError("e10b_module_path binding mismatch")
    if bindings.e10b_module_sha256 != BOUND_E10B_MODULE_SHA256:
        raise ValueError("e10b_module_sha256 binding mismatch")
    if bindings.candidate_pack_path != BOUND_CANDIDATE_PACK_PATH:
        raise ValueError("candidate_pack_path binding mismatch")
    if bindings.candidate_pack_id != BOUND_CANDIDATE_PACK_ID:
        raise ValueError("candidate_pack_id binding mismatch")
    if bindings.candidate_pack_parquet_sha256 != BOUND_CANDIDATE_PACK_PARQUET_SHA256:
        raise ValueError("candidate_pack_parquet_sha256 binding mismatch")
    if bindings.candidate_pack_row_count != BOUND_CANDIDATE_PACK_ROW_COUNT:
        raise ValueError("candidate_pack_row_count binding mismatch")
    if bindings.candidate_pack_coverage_start != BOUND_CANDIDATE_PACK_COVERAGE_START:
        raise ValueError("candidate_pack_coverage_start binding mismatch")
    if bindings.candidate_pack_coverage_end != BOUND_CANDIDATE_PACK_COVERAGE_END:
        raise ValueError("candidate_pack_coverage_end binding mismatch")
    if bindings.raw_collection_dir != BOUND_RAW_COLLECTION_DIR:
        raise ValueError("raw_collection_dir binding mismatch")
    if bindings.raw_collection_request_id != BOUND_RAW_COLLECTION_REQUEST_ID:
        raise ValueError("raw_collection_request_id binding mismatch")
    if bindings.raw_collection_manifest_sha256 != BOUND_RAW_COLLECTION_MANIFEST_SHA256:
        raise ValueError("raw_collection_manifest_sha256 binding mismatch")
    if bindings.raw_quality_report_sha256 != BOUND_RAW_QUALITY_REPORT_SHA256:
        raise ValueError("raw_quality_report_sha256 binding mismatch")


def _verify_decision_window(data: dict[str, Any]) -> None:
    """Verify decision window matches frozen constants."""
    window = data.get("decision_window", {})
    start = date.fromisoformat(window.get("start", ""))
    end = date.fromisoformat(window.get("end", ""))
    if start != DECISION_WINDOW_START:
        raise ValueError(f"decision_window.start mismatch: {start}")
    if end != DECISION_WINDOW_END:
        raise ValueError(f"decision_window.end mismatch: {end}")


def _verify_announcement_window(data: dict[str, Any]) -> None:
    """Verify announcement collection window."""
    window = data.get("source_announcement_collection_window", {})
    start = date.fromisoformat(window.get("start", ""))
    end = date.fromisoformat(window.get("end", ""))
    if start != ANNOUNCEMENT_COLLECTION_START:
        raise ValueError(f"announcement_window.start mismatch: {start}")
    if end != ANNOUNCEMENT_COLLECTION_END:
        raise ValueError(f"announcement_window.end mismatch: {end}")


# ---------------------------------------------------------------------------
# PIT availability helpers (pure, no network)
# ---------------------------------------------------------------------------


def effective_disclosure_date(
    ann_date: date | None,
    f_ann_date: date | None,
) -> date | None:
    """Compute effective disclosure date = max(ann_date, f_ann_date when present).

    Returns None if ann_date is missing/invalid (row unusable).
    """
    if ann_date is None:
        return None
    if f_ann_date is None:
        return ann_date
    return max(ann_date, f_ann_date)


def make_available_at(disclosure_date: date) -> datetime:
    """available_at = 23:59:59 Asia/Shanghai on effective disclosure date."""
    return datetime.combine(disclosure_date, AVAILABLE_AT_TIME, tzinfo=ASIA_SHANGHAI)


def make_decision_at(as_of: date) -> datetime:
    """decision_at = 17:30:00 Asia/Shanghai on as_of."""
    return datetime.combine(as_of, DECISION_TIME, tzinfo=ASIA_SHANGHAI)


def is_usable_at_decision(disclosure_date: date, decision_date: date) -> bool:
    """Check if evidence disclosed on disclosure_date is usable at decision_date.

    Same-day disclosure is NOT usable (available_at 23:59:59 > decision_at 17:30).
    """
    available_at = make_available_at(disclosure_date)
    decision_at = make_decision_at(decision_date)
    return available_at <= decision_at


def is_in_decision_window(as_of: date) -> bool:
    """Check date is within the frozen decision window."""
    return DECISION_WINDOW_START <= as_of <= DECISION_WINDOW_END


def reject_oos_date(as_of: date) -> None:
    """Raise ValueError if date is 2025+ or outside decision window."""
    if as_of.year >= 2025:
        raise ValueError(f"OOS date rejected: {as_of.isoformat()} is 2025+")
    if not is_in_decision_window(as_of):
        raise ValueError(f"date outside decision window: {as_of.isoformat()}")


def is_included_report_type(report_type: int | None) -> bool:
    """Check if report_type is in the included set {1, 4, 5}."""
    if report_type is None:
        return False
    return report_type in INCLUDED_REPORT_TYPES


def is_general_industrial(comp_type: int | None) -> bool | None:
    """Check if comp_type=1 (general industrial).

    Returns True for comp_type=1, False for 2/3/4, None for missing/invalid.
    """
    if comp_type is None:
        return None
    if isinstance(comp_type, bool):
        return None
    if comp_type not in (1, 2, 3, 4):
        return None
    if comp_type == 1:
        return True
    return False


def report_period_age_days(decision_date: date, end_date: date) -> int:
    """Calendar days from end_date to decision_date."""
    return (decision_date - end_date).days


def is_within_freshness(decision_date: date, end_date: date, max_age_days: int) -> bool:
    """Check if report period is within freshness limit."""
    age = report_period_age_days(decision_date, end_date)
    return 0 <= age <= max_age_days


# ---------------------------------------------------------------------------
# Rule math helpers (pure)
# ---------------------------------------------------------------------------


def evaluate_non_standard_audit(audit_result: str | None) -> Literal["true", "false", "unknown"]:
    """Rule A: non-standard audit.

    Exactly '标准无保留意见' => false (clean).
    Any other nonblank string => true (hit).
    Missing/blank => unknown.
    """
    if audit_result is None:
        return "unknown"
    if not isinstance(audit_result, str):
        return "unknown"
    stripped = audit_result.strip()
    if stripped == "":
        return "unknown"
    if stripped == AUDIT_CLEAN_VALUE:
        return "false"
    return "true"


def evaluate_cash_debt_ratio(
    money_cap: float | None,
    total_assets: float | None,
    interestdebt: float | None,
) -> Literal["true", "false", "unknown"]:
    """Rule B: large cash and interest-bearing debt.

    true if money_cap/total_assets > 0.25 AND interestdebt/total_assets > 0.25.
    Strict >.
    """
    if total_assets is None or money_cap is None or interestdebt is None:
        return "unknown"
    if not _is_valid_numeric(total_assets) or total_assets <= 0:
        return "unknown"
    if not _is_valid_numeric(money_cap) or money_cap < 0:
        return "unknown"
    if not _is_valid_numeric(interestdebt) or interestdebt < 0:
        return "unknown"
    cash_ratio = money_cap / total_assets
    debt_ratio = interestdebt / total_assets
    if cash_ratio > THRESHOLD_CASH_DEBT_RATIO and debt_ratio > THRESHOLD_CASH_DEBT_RATIO:
        return "true"
    return "false"


def evaluate_debt_component_crosscheck(
    st_borr: Any,
    lt_borr: Any,
    st_bonds_payable: Any,
    non_cur_liab_due_1y: Any,
    bond_payable: Any,
) -> float | None:
    """Compute component debt sum if all five are explicit numeric.

    Returns None if any component is not explicitly numeric (partial never zero-filled).
    """
    components = [st_borr, lt_borr, st_bonds_payable, non_cur_liab_due_1y, bond_payable]
    for c in components:
        if c is None:
            return None
        if isinstance(c, bool):
            return None
        if not isinstance(c, (int, float)):
            return None
        if not _is_valid_numeric(c):
            return None
        if c < 0:
            return None
    return sum(float(c) for c in components)


def evaluate_receivables_revenue_gap(
    exposure_current: float | None,
    exposure_prior: float | None,
    revenue_current: float | None,
    revenue_prior: float | None,
) -> float | None:
    """Compute single-period gap for rule C.

    gap = (exposure_current/exposure_prior - 1) - (revenue_current/revenue_prior - 1)
    Returns None if any input invalid or prior denominators <= 0.
    """
    numeric_values: list[float] = []
    for v in (exposure_current, exposure_prior, revenue_current, revenue_prior):
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        if not isinstance(v, (int, float)):
            return None
        if not math.isfinite(v):
            return None
        if v < 0:
            return None
        numeric_values.append(float(v))
    exposure_current_f, exposure_prior_f, revenue_current_f, revenue_prior_f = numeric_values
    if exposure_prior_f <= 0 or revenue_prior_f <= 0:
        return None
    exp_growth = exposure_current_f / exposure_prior_f - 1
    rev_growth = revenue_current_f / revenue_prior_f - 1
    return exp_growth - rev_growth


def evaluate_two_period_gaps(
    gap1: float | None,
    gap2: float | None,
) -> Literal["true", "false", "unknown"]:
    """Rule C final: true only if BOTH consecutive gaps > 0.20."""
    if gap1 is None or gap2 is None:
        return "unknown"
    if gap1 > THRESHOLD_RECEIVABLES_REVENUE_GAP and gap2 > THRESHOLD_RECEIVABLES_REVENUE_GAP:
        return "true"
    return "false"


def evaluate_ratio_rule(
    numerator: float | None,
    denominator: float | None,
    threshold: float,
    *,
    nonpositive_denom_unknown: bool = True,
    negative_num_unknown: bool = True,
) -> Literal["true", "false", "unknown"]:
    """Generic ratio rule: numerator/denominator > threshold (strict >).

    Used for rules D and E.
    """
    if numerator is None or denominator is None:
        return "unknown"
    if not _is_valid_numeric(numerator) or not _is_valid_numeric(denominator):
        return "unknown"
    if negative_num_unknown and numerator < 0:
        return "unknown"
    if nonpositive_denom_unknown and denominator <= 0:
        return "unknown"
    ratio = numerator / denominator
    if ratio > threshold:
        return "true"
    return "false"


def compute_exposure(
    notes_receiv: Any,
    accounts_receiv: Any,
    inventories: Any,
) -> float | None:
    """Compute exposure = notes_receiv + accounts_receiv + inventories.

    All three must be explicit numeric, non-bool, finite, non-negative.
    Returns None otherwise.
    """
    parts = [notes_receiv, accounts_receiv, inventories]
    for p in parts:
        if p is None:
            return None
        if isinstance(p, bool):
            return None
        if not isinstance(p, (int, float)):
            return None
        if not _is_valid_numeric(p):
            return None
        if p < 0:
            return None
    return float(notes_receiv) + float(accounts_receiv) + float(inventories)


def _is_valid_numeric(v: Any) -> bool:
    """Check if value is a finite number (rejects bool)."""
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    return math.isfinite(v)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _validate_safe_path(path: Path, *, repo_root: Path, field_name: str) -> Path:
    """Validate path is inside repo_root with no symlinks or OOS references."""
    root_resolved = repo_root.resolve()
    unresolved_abs = path if path.is_absolute() else (root_resolved / path)

    try:
        unresolved_rel_parts = unresolved_abs.relative_to(root_resolved).parts
    except ValueError as exc:
        raise ValueError(f"{field_name} escapes repo root") from exc

    current = root_resolved
    for component in unresolved_rel_parts:
        current = current / component
        if current.is_symlink():
            raise ValueError(f"{field_name} has a symlink component (forbidden)")

    resolved = unresolved_abs.resolve()
    try:
        rel_str = str(resolved.relative_to(root_resolved))
    except ValueError as exc:
        raise ValueError(f"{field_name} escapes repo root") from exc

    if ".." in Path(rel_str).parts:
        raise ValueError(f"{field_name} contains '..' path escape")
    lower = rel_str.lower()
    if "2025" in lower:
        raise ValueError(f"{field_name} references 2025/OOS namespace (forbidden)")
    if _OOS_BOUNDARY_RE.search(lower):
        raise ValueError(f"{field_name} references OOS namespace (forbidden)")
    return resolved


# ---------------------------------------------------------------------------
# File SHA helper
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Production verify_protocol_file
# ---------------------------------------------------------------------------


def verify_protocol_file(repo_root: Path) -> FinancialNegativeListDataProtocol:
    """Verify canonical protocol file with full disk validation.

    Validates:
    - Protocol JSON seal and model
    - Two-layer contract file: path safety, SHA-256, contract_id
    - E10b module file: path safety, SHA-256, version
    - Candidate pack: manifest seal, readiness, coverage, row counts,
      Parquet byte hash, metadata row count (no full row recomputation)
    - Raw collection: delegates to verify_raw_collection_strict
    - All binding paths/hashes/IDs/coverage cross-checked
    """
    path = repo_root / PROTOCOL_FILE_PATH
    protocol = verify_protocol(path, repo_root=repo_root)
    bindings = protocol.bindings

    _verify_two_layer_contract_on_disk(bindings, repo_root=repo_root)
    _verify_e10b_module_on_disk(bindings, repo_root=repo_root)
    _verify_candidate_pack_on_disk(bindings, repo_root=repo_root)
    _verify_raw_collection_on_disk(bindings, repo_root=repo_root)

    return protocol


def _verify_two_layer_contract_on_disk(bindings: ProtocolBindings, *, repo_root: Path) -> None:
    """Validate two-layer contract file exists, is safe, and hash+ID match."""
    contract_path = Path(bindings.two_layer_decision_contract_path)
    resolved = _validate_safe_path(contract_path, repo_root=repo_root, field_name="two_layer_decision_contract_path")
    if not resolved.is_file():
        raise ValueError(f"two-layer contract file not found: {bindings.two_layer_decision_contract_path}")

    file_sha = _sha256_file(resolved)
    if file_sha != bindings.two_layer_decision_contract_file_sha256:
        raise ValueError(
            f"two-layer contract file SHA-256 mismatch: "
            f"expected {bindings.two_layer_decision_contract_file_sha256}, got {file_sha}"
        )

    contract_data: dict[str, Any] = json.loads(resolved.read_text("utf-8"))
    contract_id = contract_data.get("contract_id") or contract_data.get("protocol_id")
    if contract_id != bindings.two_layer_decision_contract_id:
        raise ValueError(
            f"two-layer contract ID mismatch: expected {bindings.two_layer_decision_contract_id}, got {contract_id}"
        )


def _verify_e10b_module_on_disk(bindings: ProtocolBindings, *, repo_root: Path) -> None:
    """Validate E10b module file exists, is safe, and hash+version match."""
    module_path = Path(bindings.e10b_module_path)
    resolved = _validate_safe_path(module_path, repo_root=repo_root, field_name="e10b_module_path")
    if not resolved.is_file():
        raise ValueError(f"E10b module file not found: {bindings.e10b_module_path}")

    file_sha = _sha256_file(resolved)
    if file_sha != bindings.e10b_module_sha256:
        raise ValueError(f"E10b module SHA-256 mismatch: expected {bindings.e10b_module_sha256}, got {file_sha}")

    from app.research.layer_two_financial_negative_list import (
        LAYER_TWO_FINANCIAL_NEGATIVE_LIST_ENGINE_VERSION,
    )

    if LAYER_TWO_FINANCIAL_NEGATIVE_LIST_ENGINE_VERSION != bindings.e10b_engine_version:
        raise ValueError(
            f"E10b exported engine version "
            f"({LAYER_TWO_FINANCIAL_NEGATIVE_LIST_ENGINE_VERSION}) "
            f"does not match binding ({bindings.e10b_engine_version})"
        )


def _verify_candidate_pack_on_disk(bindings: ProtocolBindings, *, repo_root: Path) -> None:
    """Validate candidate pack manifest seal, readiness, coverage, row counts,
    Parquet byte hash, and metadata row count without full row recomputation."""
    pack_dir = Path(bindings.candidate_pack_path)
    resolved_dir = _validate_safe_path(pack_dir, repo_root=repo_root, field_name="candidate_pack_path")
    if not resolved_dir.is_dir():
        raise ValueError(f"candidate pack directory not found: {bindings.candidate_pack_path}")

    manifest_path = resolved_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("candidate pack manifest.json not found")

    manifest_data: dict[str, Any] = json.loads(manifest_path.read_text("utf-8"))

    stored_pack_id = manifest_data.get("pack_id")
    if not isinstance(stored_pack_id, str) or not _HEX64.fullmatch(stored_pack_id):
        raise ValueError("candidate pack manifest has invalid pack_id")

    payload_for_seal = {k: v for k, v in manifest_data.items() if k != "pack_id"}
    canonical_bytes = json.dumps(payload_for_seal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    recomputed_id = hashlib.sha256(canonical_bytes).hexdigest()
    if recomputed_id != stored_pack_id:
        raise ValueError(f"candidate pack manifest seal broken: expected {stored_pack_id}, recomputed {recomputed_id}")

    if stored_pack_id != bindings.candidate_pack_id:
        raise ValueError(f"candidate pack ID mismatch: expected {bindings.candidate_pack_id}, got {stored_pack_id}")

    readiness = manifest_data.get("readiness", {})
    if not readiness.get("research_only", False):
        raise ValueError("candidate pack readiness.research_only must be true")
    for flag in ("ready_for_scoring", "ready_for_trading", "ready_for_portfolio_construction"):
        if readiness.get(flag, False):
            raise ValueError(f"candidate pack readiness.{flag} must be false")
    if not readiness.get("not_alpha_evidence", False):
        raise ValueError("candidate pack readiness.not_alpha_evidence must be true")
    if not readiness.get("not_authorization", False):
        raise ValueError("candidate pack readiness.not_authorization must be true")

    coverage = manifest_data.get("coverage", {})
    coverage_start = coverage.get("start")
    coverage_end = coverage.get("end")
    if str(coverage_start) != bindings.candidate_pack_coverage_start:
        raise ValueError(
            f"candidate pack coverage_start mismatch: "
            f"expected {bindings.candidate_pack_coverage_start}, got {coverage_start}"
        )
    if str(coverage_end) != bindings.candidate_pack_coverage_end:
        raise ValueError(
            f"candidate pack coverage_end mismatch: expected {bindings.candidate_pack_coverage_end}, got {coverage_end}"
        )

    row_counts = manifest_data.get("row_counts", {})
    row_counts_total = row_counts.get("total")
    if row_counts_total != bindings.candidate_pack_row_count:
        raise ValueError(
            f"candidate pack row_counts.total mismatch: "
            f"expected {bindings.candidate_pack_row_count}, got {row_counts_total}"
        )

    integrity = manifest_data.get("integrity", {})
    integrity_row_count = integrity.get("row_count")
    if integrity_row_count != bindings.candidate_pack_row_count:
        raise ValueError(
            f"candidate pack integrity.row_count mismatch: "
            f"expected {bindings.candidate_pack_row_count}, got {integrity_row_count}"
        )

    integrity_parquet_sha = integrity.get("parquet_file_sha256")
    if integrity_parquet_sha != bindings.candidate_pack_parquet_sha256:
        raise ValueError(
            f"candidate pack integrity.parquet_file_sha256 mismatch: "
            f"expected {bindings.candidate_pack_parquet_sha256}, got {integrity_parquet_sha}"
        )

    parquet_path = resolved_dir / "eligibility_verdicts.parquet"
    if not parquet_path.is_file():
        raise ValueError("candidate pack eligibility_verdicts.parquet not found")

    parquet_sha = _sha256_file(parquet_path)
    if parquet_sha != bindings.candidate_pack_parquet_sha256:
        raise ValueError(
            f"candidate pack Parquet SHA-256 mismatch: "
            f"expected {bindings.candidate_pack_parquet_sha256}, got {parquet_sha}"
        )

    try:
        import pyarrow.parquet as pq
    except ModuleNotFoundError as exc:
        raise ValueError("pyarrow is required to verify candidate pack parquet metadata") from exc

    pf = pq.ParquetFile(parquet_path)
    metadata_row_count = pf.metadata.num_rows
    if metadata_row_count != bindings.candidate_pack_row_count:
        raise ValueError(
            f"candidate pack Parquet metadata row count mismatch: "
            f"expected {bindings.candidate_pack_row_count}, got {metadata_row_count}"
        )


def _verify_raw_collection_on_disk(bindings: ProtocolBindings, *, repo_root: Path) -> None:
    """Validate raw collection via verify_raw_collection_strict from eligibility pack."""
    from app.research.layer_two_candidate_eligibility_pack import (
        verify_raw_collection_strict,
    )

    raw_dir = Path(bindings.raw_collection_dir)
    resolved_raw_dir = _validate_safe_path(raw_dir, repo_root=repo_root, field_name="raw_collection_dir")
    manifest = verify_raw_collection_strict(resolved_raw_dir, repo_root=repo_root)

    if manifest.get("request_id") != bindings.raw_collection_request_id:
        raise ValueError(f"raw collection request_id binding mismatch: expected {bindings.raw_collection_request_id}")

    manifest_sha_field = manifest.get("manifest_sha256")
    if manifest_sha_field is not None and manifest_sha_field != bindings.raw_collection_manifest_sha256:
        raise ValueError("raw collection manifest_sha256 binding mismatch")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "ANNOUNCEMENT_COLLECTION_END",
    "ANNOUNCEMENT_COLLECTION_START",
    "ASIA_SHANGHAI",
    "AUDIT_CLEAN_VALUE",
    "AVAILABLE_AT_TIME",
    "BALANCESHEET_FIELDS",
    "BOUND_CANDIDATE_PACK_COVERAGE_END",
    "BOUND_CANDIDATE_PACK_COVERAGE_START",
    "BOUND_CANDIDATE_PACK_ID",
    "BOUND_CANDIDATE_PACK_PARQUET_SHA256",
    "BOUND_CANDIDATE_PACK_PATH",
    "BOUND_CANDIDATE_PACK_ROW_COUNT",
    "BOUND_E10B_ENGINE_VERSION",
    "BOUND_E10B_MODULE_PATH",
    "BOUND_E10B_MODULE_SHA256",
    "BOUND_RAW_COLLECTION_DIR",
    "BOUND_RAW_COLLECTION_MANIFEST_SHA256",
    "BOUND_RAW_COLLECTION_REQUEST_ID",
    "BOUND_RAW_QUALITY_REPORT_SHA256",
    "BOUND_TWO_LAYER_CONTRACT_FILE_SHA256",
    "BOUND_TWO_LAYER_CONTRACT_ID",
    "BOUND_TWO_LAYER_CONTRACT_PATH",
    "DEBT_COMPONENT_FIELDS",
    "DECISION_TIME",
    "DECISION_WINDOW_END",
    "DECISION_WINDOW_START",
    "EXCLUDED_REPORT_TYPES",
    "FINA_AUDIT_FIELDS",
    "FINA_INDICATOR_FIELDS",
    "INCOME_FIELDS",
    "INCLUDED_REPORT_TYPES",
    "MAX_REPORT_PERIOD_AGE_AUDIT_DAYS",
    "MAX_REPORT_PERIOD_AGE_STATEMENT_DAYS",
    "PROTOCOL_FILE_PATH",
    "PROTOCOL_ID",
    "SOURCE_ENDPOINTS",
    "THRESHOLD_CASH_DEBT_RATIO",
    "THRESHOLD_GOODWILL_RATIO",
    "THRESHOLD_OTHER_RECEIVABLES_RATIO",
    "THRESHOLD_RECEIVABLES_REVENUE_GAP",
    "FinancialNegativeListDataProtocol",
    "ProtocolAnnouncementWindow",
    "ProtocolBindings",
    "ProtocolCollectionSemantics",
    "ProtocolDecisionWindow",
    "ProtocolFreshness",
    "ProtocolPitAvailability",
    "ProtocolReadiness",
    "ProtocolRowVersionResolution",
    "ProtocolRules",
    "ProtocolScope",
    "ProtocolSourceEndpoints",
    "compute_exposure",
    "compute_protocol_id",
    "effective_disclosure_date",
    "evaluate_cash_debt_ratio",
    "evaluate_debt_component_crosscheck",
    "evaluate_non_standard_audit",
    "evaluate_ratio_rule",
    "evaluate_receivables_revenue_gap",
    "evaluate_two_period_gaps",
    "is_general_industrial",
    "is_in_decision_window",
    "is_included_report_type",
    "is_usable_at_decision",
    "is_within_freshness",
    "load_protocol",
    "make_available_at",
    "make_decision_at",
    "reject_oos_date",
    "report_period_age_days",
    "verify_protocol",
    "verify_protocol_file",
]
