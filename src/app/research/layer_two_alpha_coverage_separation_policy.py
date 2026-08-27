"""Frozen pre-outcome correction for layer-two alpha evidence coverage.

The original E11a denominator required a complete financial-negative-list
verdict before a name could contribute to factor evidence.  A sealed optimistic
upper-bound review proved that this denominator cannot reach the already frozen
sample-size gates.  This policy records the correction without mutating E11a:
factor efficacy is evaluated on the complete candidate-eligibility cross
section, while the financial negative list remains an independent safety
overlay and never becomes a known-only alpha sample selector.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_alpha_development_protocol import (
    DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH,
    verify_layer_two_alpha_development_protocol_file,
)
from app.research.layer_two_alpha_input_feasibility import (
    DEFAULT_OUTPUT_PATH as DEFAULT_FEASIBILITY_REPORT_PATH,
)
from app.research.layer_two_alpha_input_feasibility import (
    LayerTwoAlphaInputFeasibilityReport,
    verify_feasibility_report_file,
    verify_report_self_hash,
)

POLICY_SCHEMA_VERSION: Literal["1"] = "1"
POLICY_VERSION: Literal["layer-two-alpha-coverage-separation-policy-v1"] = (
    "layer-two-alpha-coverage-separation-policy-v1"
)
DEFAULT_POLICY_PATH = Path("config/research/layer-two-alpha-coverage-separation-policy-v1.json")


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _hex64(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{field_name} must be a 64-char lowercase hex SHA-256")
    return value


class SeparationSourceBinding(_StrictFrozen):
    base_alpha_protocol_path: str
    base_alpha_protocol_id: str
    base_alpha_protocol_file_sha256: str
    feasibility_report_path: str
    feasibility_report_id: str
    feasibility_report_file_sha256: str
    candidate_pack_id: str
    financial_overlay_id: str
    market_snapshot_id: str
    fundamental_snapshot_id: str

    @field_validator(
        "base_alpha_protocol_id",
        "base_alpha_protocol_file_sha256",
        "feasibility_report_id",
        "feasibility_report_file_sha256",
        "candidate_pack_id",
        "financial_overlay_id",
        "market_snapshot_id",
        "fundamental_snapshot_id",
        mode="before",
    )
    @classmethod
    def _hex_fields(cls, value: object, info: Any) -> str:
        return _hex64(value, field_name=str(info.field_name))


class AlphaEvidenceDenominatorV2(_StrictFrozen):
    definition: Literal[
        "complete_verified_candidate_eligibility_and_eligible_for_new_entry_true"
    ]
    financial_negative_list_must_not_condition_factor_ic_sample: Literal[True]
    financial_known_only_subsample_selection_forbidden: Literal[True]
    factor_known_count_gate_500_unchanged: Literal[True]
    factor_known_fraction_gate_60pct_unchanged: Literal[True]
    pooled_and_per_year_date_gates_unchanged: Literal[True]


class FinancialSafetyOverlayRole(_StrictFrozen):
    role: Literal["independent_safety_overlay_not_alpha_evidence_denominator"]
    unknown_is_not_clean: Literal[True]
    hard_exclusion_cannot_be_overridden_by_alpha: Literal[True]
    incomplete_financial_verdict_blocks_later_new_entry: Literal[True]
    coverage_and_missingness_reported_separately: Literal[True]
    no_imputation_or_zero_fill: Literal[True]


class ClusterCompanionV2(_StrictFrozen):
    population: Literal[
        "same_candidate_eligible_factor_known_cross_section_used_by_raw_factor_evidence"
    ]
    financial_known_only_conditioning_forbidden: Literal[True]
    monthly_first_trading_day_pit_recompute_unchanged: Literal[True]
    statistical_risk_proxy_not_industry: Literal[True]
    not_a_fifth_hypothesis: Literal[True]


class WindowBoundaryV2(_StrictFrozen):
    development: Literal["2022-01-01..2023-12-31"]
    seen_robustness_report_only: Literal["2024-01-01..2024-12-31"]
    consumed_oos_forbidden: Literal["2025-01-01..2026-08-21"]
    new_frozen_oos_begins: Literal["2026-08-22"]
    no_2024_selection_or_weight_change: Literal[True]
    no_2025_plus_evaluation_authorized: Literal[True]


class SeparationPolicyReadiness(_StrictFrozen):
    research_only: Literal[True]
    pre_outcome_feasibility_correction: Literal[True]
    no_factor_ic_or_forward_return_observed_before_freeze: Literal[True]
    base_e11a_remains_immutable: Literal[True]
    requires_new_protocol_and_run_contract: Literal[True]
    requires_trial_ledger_registration_before_execution: Literal[True]
    ready_for_alpha_diagnostic_execution: Literal[False]
    ready_for_scoring: Literal[False]
    ready_for_backtest: Literal[False]
    ready_for_portfolio_construction: Literal[False]
    ready_for_orders: Literal[False]
    ready_for_trading: Literal[False]
    auto_apply: Literal[False]


class LayerTwoAlphaCoverageSeparationPolicy(_StrictFrozen):
    schema_version: Literal["1"]
    policy_version: Literal["layer-two-alpha-coverage-separation-policy-v1"]
    status: Literal["frozen_protocol_correction_not_executable"]
    source_binding: SeparationSourceBinding
    alpha_evidence_denominator_v2: AlphaEvidenceDenominatorV2
    financial_safety_overlay: FinancialSafetyOverlayRole
    cluster_companion_v2: ClusterCompanionV2
    windows: WindowBoundaryV2
    rationale: Literal[
        "sealed_optimistic_coverage_upper_bound_failed_before_any_factor_outcome_was_computed"
    ]
    forbidden_shortcuts: tuple[
        Literal["lower_500_name_gate_to_fit_current_data"],
        Literal["treat_financial_unknown_as_clean"],
        Literal["select_alpha_on_financial_known_only_subsample"],
        Literal["use_2024_to_choose_or_change_weights"],
        Literal["evaluate_consumed_or_new_frozen_oos"],
    ]
    readiness: SeparationPolicyReadiness
    policy_id: str | None = Field(default=None)

    @field_validator("policy_id", mode="before")
    @classmethod
    def _policy_id(cls, value: object) -> str | None:
        if value is None:
            return None
        return _hex64(value, field_name="policy_id")

    @model_validator(mode="after")
    def _forbidden_order(self) -> LayerTwoAlphaCoverageSeparationPolicy:
        expected = (
            "lower_500_name_gate_to_fit_current_data",
            "treat_financial_unknown_as_clean",
            "select_alpha_on_financial_known_only_subsample",
            "use_2024_to_choose_or_change_weights",
            "evaluate_consumed_or_new_frozen_oos",
        )
        if self.forbidden_shortcuts != expected:
            raise ValueError("forbidden_shortcuts must remain the exact frozen ordered set")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_payload(policy: LayerTwoAlphaCoverageSeparationPolicy) -> dict[str, Any]:
    return policy.model_dump(mode="json", exclude={"policy_id"})


def compute_policy_id(policy: LayerTwoAlphaCoverageSeparationPolicy) -> str:
    encoded = json.dumps(_canonical_payload(policy), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def seal_policy(policy: LayerTwoAlphaCoverageSeparationPolicy) -> LayerTwoAlphaCoverageSeparationPolicy:
    return policy.model_copy(update={"policy_id": compute_policy_id(policy)})


def verify_policy_self_hash(policy: LayerTwoAlphaCoverageSeparationPolicy) -> None:
    if policy.policy_id is None or policy.policy_id != compute_policy_id(policy):
        raise ValueError("layer-two alpha coverage separation policy self-hash mismatch")


def _load_feasibility(path: Path) -> LayerTwoAlphaInputFeasibilityReport:
    try:
        report = LayerTwoAlphaInputFeasibilityReport.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("feasibility report is missing or invalid") from exc
    verify_report_self_hash(report)
    if report.readiness.ready_for_alpha_diagnostic_execution is not False:
        raise ValueError("feasibility report readiness drift")
    return report


def _safe_repo_relative(path: Path, *, repo_root: Path, field_name: str) -> str:
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
    return relative.as_posix()


def build_policy(
    *,
    repo_root: Path,
    feasibility_report_path: Path = DEFAULT_FEASIBILITY_REPORT_PATH,
) -> LayerTwoAlphaCoverageSeparationPolicy:
    root = repo_root.resolve()
    report_path = feasibility_report_path if feasibility_report_path.is_absolute() else root / feasibility_report_path
    report_rel = _safe_repo_relative(
        report_path,
        repo_root=root,
        field_name="feasibility_report_path",
    )
    report = _load_feasibility(report_path)
    protocol_path = root / DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH
    protocol, _ = verify_layer_two_alpha_development_protocol_file(
        protocol_path=protocol_path,
        repo_root=root,
    )
    if report.report_id is None or protocol.protocol_id is None:
        raise ValueError("sealed source ID missing")
    binding = report.source_binding
    policy = LayerTwoAlphaCoverageSeparationPolicy(
        schema_version=POLICY_SCHEMA_VERSION,
        policy_version=POLICY_VERSION,
        status="frozen_protocol_correction_not_executable",
        source_binding=SeparationSourceBinding(
            base_alpha_protocol_path=DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH.as_posix(),
            base_alpha_protocol_id=protocol.protocol_id,
            base_alpha_protocol_file_sha256=_sha256_file(protocol_path),
            feasibility_report_path=report_rel,
            feasibility_report_id=report.report_id,
            feasibility_report_file_sha256=_sha256_file(report_path),
            candidate_pack_id=binding.candidate_pack_id,
            financial_overlay_id=binding.financial_overlay_id,
            market_snapshot_id=binding.market_snapshot_id,
            fundamental_snapshot_id=binding.fundamental_snapshot_id,
        ),
        alpha_evidence_denominator_v2=AlphaEvidenceDenominatorV2(
            definition="complete_verified_candidate_eligibility_and_eligible_for_new_entry_true",
            financial_negative_list_must_not_condition_factor_ic_sample=True,
            financial_known_only_subsample_selection_forbidden=True,
            factor_known_count_gate_500_unchanged=True,
            factor_known_fraction_gate_60pct_unchanged=True,
            pooled_and_per_year_date_gates_unchanged=True,
        ),
        financial_safety_overlay=FinancialSafetyOverlayRole(
            role="independent_safety_overlay_not_alpha_evidence_denominator",
            unknown_is_not_clean=True,
            hard_exclusion_cannot_be_overridden_by_alpha=True,
            incomplete_financial_verdict_blocks_later_new_entry=True,
            coverage_and_missingness_reported_separately=True,
            no_imputation_or_zero_fill=True,
        ),
        cluster_companion_v2=ClusterCompanionV2(
            population="same_candidate_eligible_factor_known_cross_section_used_by_raw_factor_evidence",
            financial_known_only_conditioning_forbidden=True,
            monthly_first_trading_day_pit_recompute_unchanged=True,
            statistical_risk_proxy_not_industry=True,
            not_a_fifth_hypothesis=True,
        ),
        windows=WindowBoundaryV2(
            development="2022-01-01..2023-12-31",
            seen_robustness_report_only="2024-01-01..2024-12-31",
            consumed_oos_forbidden="2025-01-01..2026-08-21",
            new_frozen_oos_begins="2026-08-22",
            no_2024_selection_or_weight_change=True,
            no_2025_plus_evaluation_authorized=True,
        ),
        rationale="sealed_optimistic_coverage_upper_bound_failed_before_any_factor_outcome_was_computed",
        forbidden_shortcuts=(
            "lower_500_name_gate_to_fit_current_data",
            "treat_financial_unknown_as_clean",
            "select_alpha_on_financial_known_only_subsample",
            "use_2024_to_choose_or_change_weights",
            "evaluate_consumed_or_new_frozen_oos",
        ),
        readiness=SeparationPolicyReadiness(
            research_only=True,
            pre_outcome_feasibility_correction=True,
            no_factor_ic_or_forward_return_observed_before_freeze=True,
            base_e11a_remains_immutable=True,
            requires_new_protocol_and_run_contract=True,
            requires_trial_ledger_registration_before_execution=True,
            ready_for_alpha_diagnostic_execution=False,
            ready_for_scoring=False,
            ready_for_backtest=False,
            ready_for_portfolio_construction=False,
            ready_for_orders=False,
            ready_for_trading=False,
            auto_apply=False,
        ),
    )
    return seal_policy(policy)


def verify_policy_file(
    path: Path,
    *,
    repo_root: Path,
    full_source_recomputation: bool = True,
) -> LayerTwoAlphaCoverageSeparationPolicy:
    try:
        policy = LayerTwoAlphaCoverageSeparationPolicy.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("layer-two alpha coverage separation policy is missing or invalid") from exc
    verify_policy_self_hash(policy)
    report_path = repo_root.resolve() / policy.source_binding.feasibility_report_path
    if full_source_recomputation:
        verify_feasibility_report_file(report_path, repo_root=repo_root)
    rebuilt = build_policy(repo_root=repo_root, feasibility_report_path=report_path)
    if policy.model_dump(mode="json") != rebuilt.model_dump(mode="json"):
        raise ValueError("coverage separation policy does not recompute from bound sources")
    return policy


def write_policy(
    path: Path,
    policy: LayerTwoAlphaCoverageSeparationPolicy,
    *,
    replace_existing: bool = False,
) -> None:
    verify_policy_self_hash(policy)
    if path.exists() and not replace_existing:
        raise FileExistsError(f"coverage separation policy already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(policy.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


__all__ = [
    "DEFAULT_POLICY_PATH",
    "LayerTwoAlphaCoverageSeparationPolicy",
    "build_policy",
    "compute_policy_id",
    "seal_policy",
    "verify_policy_file",
    "verify_policy_self_hash",
    "write_policy",
]
