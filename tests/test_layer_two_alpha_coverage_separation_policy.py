from __future__ import annotations

import pytest

from app.research.layer_two_alpha_coverage_separation_policy import (
    AlphaEvidenceDenominatorV2,
    ClusterCompanionV2,
    FinancialSafetyOverlayRole,
    LayerTwoAlphaCoverageSeparationPolicy,
    SeparationPolicyReadiness,
    SeparationSourceBinding,
    WindowBoundaryV2,
    seal_policy,
    verify_policy_self_hash,
)


def _policy() -> LayerTwoAlphaCoverageSeparationPolicy:
    ids = [f"{digit:x}" * 64 for digit in range(1, 9)]
    policy = LayerTwoAlphaCoverageSeparationPolicy(
        schema_version="1",
        policy_version="layer-two-alpha-coverage-separation-policy-v1",
        status="frozen_protocol_correction_not_executable",
        source_binding=SeparationSourceBinding(
            base_alpha_protocol_path="config/base.json",
            base_alpha_protocol_id=ids[0],
            base_alpha_protocol_file_sha256=ids[1],
            feasibility_report_path="data/review.json",
            feasibility_report_id=ids[2],
            feasibility_report_file_sha256=ids[3],
            candidate_pack_id=ids[4],
            financial_overlay_id=ids[5],
            market_snapshot_id=ids[6],
            fundamental_snapshot_id=ids[7],
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


def test_policy_self_hash_detects_tamper() -> None:
    policy = _policy()
    verify_policy_self_hash(policy)
    tampered = policy.model_copy(update={"status": "changed"})
    with pytest.raises(ValueError, match="self-hash mismatch"):
        verify_policy_self_hash(tampered)


def test_policy_rejects_reordered_or_missing_shortcuts() -> None:
    payload = _policy().model_dump(mode="json")
    payload["forbidden_shortcuts"] = payload["forbidden_shortcuts"][:-1]
    with pytest.raises(ValueError):
        LayerTwoAlphaCoverageSeparationPolicy.model_validate(payload)


def test_policy_cannot_authorize_execution() -> None:
    payload = _policy().model_dump(mode="json")
    payload["readiness"]["ready_for_alpha_diagnostic_execution"] = True
    with pytest.raises(ValueError):
        LayerTwoAlphaCoverageSeparationPolicy.model_validate(payload)
