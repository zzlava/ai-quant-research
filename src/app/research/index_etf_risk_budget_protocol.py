"""Fail-closed verifier for the design-only index ETF risk-budget protocol."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.repo_file_safety import resolve_repo_regular_file

DEFAULT_PATH = Path("config/research/index-etf-risk-budget-research-protocol-v1.json")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class FileBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class IndexEtfRiskBudgetProtocol(_StrictModel):
    schema_version: Literal["1"]
    protocol_version: Literal["index-etf-risk-budget-research-protocol-v1"]
    protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["implementation_complete_pending_prominent_manual_run_confirmation"]
    created_as_of: date
    design_authorization: dict[str, bool]
    source_bindings: dict[str, ArtifactBinding]
    implementation_bindings: dict[str, FileBinding]
    research_family: dict[str, Any]
    scope: dict[str, Any]
    beta_core: dict[str, Any]
    defensive_leg: dict[str, Any]
    static_controls: dict[str, Any]
    dynamic_candidates: list[dict[str, Any]]
    existing_binary_lock_comparator: dict[str, Any]
    evaluation_windows: dict[str, Any]
    decision_timing: dict[str, bool]
    costs: dict[str, Any]
    endpoints: dict[str, Any]
    a_share_specific_diagnostics: dict[str, Any]
    product_policy: dict[str, Any]
    slow_oos_role: dict[str, Any]
    completed_prerequisites: list[str]
    blockers_before_any_run: list[str]
    manual_run_gate: dict[str, Any]
    readiness: dict[str, bool]

    @model_validator(mode="after")
    def _fail_closed(self) -> IndexEtfRiskBudgetProtocol:
        if self.created_as_of != date(2026, 8, 27):
            raise ValueError("protocol creation date drifted")

        expected_authorization = {
            "protocol_writing_authorized": True,
            "historical_replay_authorized": False,
            "prospective_evaluation_authorized": False,
            "orders_or_trading_authorized": False,
            "writing_this_protocol_is_not_restart_authorization": True,
        }
        if self.design_authorization != expected_authorization:
            raise ValueError("design-only authorization boundary drifted")

        expected_bindings = {
            "research_plan_stop_rule",
            "index_data_evidence",
            "frozen_binary_risk_lock_policy",
            "index_time_series_trial_ledger",
            "defensive_leg_contract",
            "defensive_leg_snapshot",
            "product_cost_contract",
            "power_protocol",
            "power_review",
        }
        if set(self.source_bindings) != expected_bindings:
            raise ValueError("source binding set drifted")
        if set(self.implementation_bindings) != {
            "historical_replay_engine",
            "power_calibration_engine",
            "product_cost_evidence_engine",
        }:
            raise ValueError("implementation binding set drifted")

        family = self.research_family
        if family.get("family_id") != "index_time_series_risk_budget_v1":
            raise ValueError("research family ID drifted")
        required_true = (
            "separate_trial_ledger_required",
            "individual_stock_cross_sectional_alpha_forbidden",
            "factor_or_etf_rotation_forbidden",
            "two_parameterizations_count_as_two_registered_hypotheses",
            "parameter_scanning_forbidden",
            "prominent_manual_run_confirmation_required",
            "automatic_restart_forbidden",
        )
        if any(family.get(key) is not True for key in required_true):
            raise ValueError("research-family fail-closed rule drifted")
        if family.get("inherit_closed_individual_stock_trial_count") is not False:
            raise ValueError("new time-series ledger must remain separate")

        if self.beta_core.get("historical_total_return_proxy") != "H00985.CSI":
            raise ValueError("broad beta total-return proxy drifted")
        if self.beta_core.get("risk_state_price_index") != "000985.CSI":
            raise ValueError("risk-state price index drifted")
        if self.beta_core.get("beta_policy") != "fixed_broad_market_beta":
            raise ValueError("beta policy must remain fixed broad-market beta")
        if self.beta_core.get("dynamic_industry_or_style_etf_rotation_forbidden") is not True:
            raise ValueError("ETF rotation must remain forbidden")

        defensive = self.defensive_leg
        if defensive.get("status") != "verified_index_level_total_return_history":
            raise ValueError("defensive-leg factual blocker drifted")
        if defensive.get("assumed_fixed_carry_forbidden") is not True:
            raise ValueError("fixed defensive carry must remain forbidden")
        if defensive.get("must_use_point_in_time_realized_total_returns") is not True:
            raise ValueError("defensive leg must use realized point-in-time total returns")
        if defensive.get("contract_id") != (
            "9c085d0bc4edd60a3d81b964108910264ebeff6b23b88eda2c4943b62af25f4f"
        ):
            raise ValueError("defensive-leg contract binding drifted")
        if defensive.get("snapshot_id") != (
            "4cfd36b96f972d582735e5d1bc9323fbccf6addcbc1710e9f5a647341fb34ba2"
        ):
            raise ValueError("defensive-leg snapshot binding drifted")

        if self.static_controls.get("confirmatory_equity_weight_grid") != [
            0.3,
            0.4,
            0.5,
            0.6,
            0.7,
            0.8,
        ]:
            raise ValueError("static equity-weight grid drifted")
        matched = self.static_controls.get("ex_post_average_exposure_matched_control")
        if not isinstance(matched, dict):
            raise ValueError("matched-exposure diagnostic contract is missing")
        if matched.get("allowed_for_attribution_only") is not True:
            raise ValueError("matched-exposure control must remain attribution-only")
        if matched.get("confirmatory_baseline") is not False:
            raise ValueError("ex-post matched exposure cannot be confirmatory")

        expected_candidates = {
            "vol_target_20d_12pct_weekly_v1": 20,
            "vol_target_60d_12pct_weekly_v1": 60,
        }
        if len(self.dynamic_candidates) != 2:
            raise ValueError("exactly two dynamic candidates are pre-registered")
        observed: dict[str, int] = {}
        for candidate in self.dynamic_candidates:
            candidate_id = candidate.get("candidate_id")
            lookback = candidate.get("realized_volatility_lookback_trading_days")
            if not isinstance(candidate_id, str) or not isinstance(lookback, int):
                raise ValueError("dynamic candidate identity is invalid")
            observed[candidate_id] = lookback
            if candidate.get("annualized_target_volatility") != 0.12:
                raise ValueError("annualized target volatility drifted")
            if candidate.get("minimum_equity_weight") != 0.0:
                raise ValueError("minimum equity weight drifted")
            if candidate.get("maximum_equity_weight") != 1.0:
                raise ValueError("maximum equity weight drifted")
            if candidate.get("rebalance_frequency") != "weekly_first_market_trading_day":
                raise ValueError("rebalance frequency drifted")
            if candidate.get("signal_lag") != "prior_market_close_only_T_plus_1_action":
                raise ValueError("signal lag drifted")
            if candidate.get("parameter_tuning_after_seal_forbidden") is not True:
                raise ValueError("post-seal tuning must remain forbidden")
        if observed != expected_candidates:
            raise ValueError("dynamic candidate hypothesis set drifted")

        windows = self.evaluation_windows
        consumed = windows.get("consumed_oos")
        prospective = windows.get("prospective_frozen_record")
        seen = windows.get("seen_historical_replay")
        if not isinstance(consumed, dict) or consumed.get("reuse_forbidden") is not True:
            raise ValueError("consumed OOS reuse must remain forbidden")
        if consumed.get("start") != "2025-01-01" or consumed.get("end") != "2026-08-21":
            raise ValueError("consumed OOS boundary drifted")
        if not isinstance(prospective, dict):
            raise ValueError("prospective frozen-record window is missing")
        if prospective.get("start") != "2026-08-22":
            raise ValueError("prospective frozen-record start drifted")
        if prospective.get("not_mature_before") != "2027-08-21":
            raise ValueError("prospective maturity date drifted")
        if prospective.get("one_year_is_execution_validation_not_final_statistical_confirmation") is not True:
            raise ValueError("one-year live record must not become final confirmation")
        if not isinstance(seen, dict) or seen.get("confirmatory_claim_forbidden") is not True:
            raise ValueError("seen history must remain non-confirmatory")

        if self.endpoints.get("primary", {}).get("metric") != (
            "net_of_cost_calmar_difference_vs_best_static_grid_arm"
        ):
            raise ValueError("primary endpoint drifted")
        hard_gates = self.endpoints.get("hard_gates")
        if not isinstance(hard_gates, dict):
            raise ValueError("hard gates are missing")
        if hard_gates.get("maximum_drawdown_floor") != -0.2:
            raise ValueError("maximum drawdown utility constraint drifted")
        if hard_gates.get("two_candidate_family_wise_alpha") != 0.05:
            raise ValueError("family-wise alpha drifted")
        if hard_gates.get("multiple_testing_correction") != "holm":
            raise ValueError("multiple-testing correction drifted")
        if hard_gates.get("power_and_mde_status_must_be_evaluable") is not True:
            raise ValueError("power/MDE gate must remain mandatory")

        if self.costs.get("current_cost_contract_status") != (
            "sealed_synthetic_index_level_cost_envelope_live_product_cost_pending"
        ):
            raise ValueError("cost-contract blocker drifted")
        if self.costs.get("cost_values_must_be_sealed_before_evaluation") is not True:
            raise ValueError("cost values must be sealed before evaluation")
        if self.costs.get("historical_index_replay_cost_contract_ready") is not True:
            raise ValueError("historical index replay cost contract must be ready")
        if self.costs.get("live_product_cost_contract_ready") is not False:
            raise ValueError("live product cost contract must remain pending")

        if self.product_policy.get("review_frequency_at_most_annual") is not True:
            raise ValueError("beta product review must remain low frequency")
        if self.product_policy.get("performance_chasing_and_dynamic_rotation_forbidden") is not True:
            raise ValueError("performance chasing and ETF rotation must remain forbidden")
        if self.slow_oos_role.get("primary_role") != (
            "execution_semantics_and_cost_model_validation"
        ):
            raise ValueError("slow OOS primary role drifted")
        if self.slow_oos_role.get("one_realized_path_must_not_be_called_final_strategy_confirmation") is not True:
            raise ValueError("one realized path cannot be final confirmation")

        completed = {
            "separate_index_time_series_trial_ledger",
            "defensive_leg_identity_and_point_in_time_total_return_history_contract",
            "fixed_index_proxy_product_policy_record",
            "sealed_index_level_product_boundary_and_synthetic_cost_envelope",
            "sealed_mde_and_power_review",
            "implementation_and_independent_code_review",
        }
        if set(self.completed_prerequisites) != completed:
            raise ValueError("completed prerequisite set drifted")
        if self.blockers_before_any_run != ["prominent_manual_user_restart_confirmation"]:
            raise ValueError("pre-run blocker set drifted")
        gate = self.manual_run_gate
        if gate.get("confirmation_present") is not False:
            raise ValueError("manual run confirmation must remain absent")
        if gate.get("exact_confirmation_text") != (
            "我确认按照已封印的指数风险预算协议执行2005-01-04至2024-12-31一次性历史回放；"
            "该回放仅为已见历史研究，不是OOS、评分、荐股、组合指令或交易授权。"
        ):
            raise ValueError("manual run confirmation text drifted")
        if gate.get("single_use_authorization_receipt_required") is not True:
            raise ValueError("single-use run authorization receipt is required")

        expected_readiness = {
            "design_only": False,
            "implementation_complete": True,
            "ready_for_authorized_historical_replay": True,
            "manual_confirmation_present": False,
            "ready_for_historical_replay": False,
            "ready_for_prospective_evaluation": False,
            "ready_for_live_product_mapping": False,
            "ready_for_scoring": False,
            "ready_for_backtest": False,
            "ready_for_portfolio_construction": False,
            "ready_for_orders": False,
            "ready_for_trading": False,
            "auto_apply": False,
        }
        if self.readiness != expected_readiness:
            raise ValueError("design-only readiness boundary drifted")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _protocol_id(protocol: IndexEtfRiskBudgetProtocol) -> str:
    payload = protocol.model_dump(mode="json", exclude={"protocol_id"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def verify_index_etf_risk_budget_protocol(
    *, repo_root: Path, path: Path = DEFAULT_PATH
) -> IndexEtfRiskBudgetProtocol:
    root = Path(repo_root).resolve(strict=True)
    resolved = resolve_repo_regular_file(path, repo_root=root, field_name="protocol_path")
    try:
        protocol = IndexEtfRiskBudgetProtocol.model_validate_json(resolved.read_text())
    except Exception as exc:
        raise ValueError("index ETF risk-budget protocol is missing or invalid") from exc
    if protocol.protocol_id != _protocol_id(protocol):
        raise ValueError("index ETF risk-budget protocol self-hash mismatch")
    for name, binding in protocol.source_bindings.items():
        source = resolve_repo_regular_file(
            Path(binding.path), repo_root=root, field_name=f"source_bindings.{name}.path"
        )
        if _sha256_file(source) != binding.sha256:
            raise ValueError(f"index ETF protocol source hash mismatch: {name}")
        payload = json.loads(source.read_text())
        source_ids = {
            payload.get("contract_id"),
            payload.get("evidence_id"),
            payload.get("policy_id"),
            payload.get("protocol_id"),
            payload.get("ledger_id"),
            payload.get("snapshot_id"),
            payload.get("review_id"),
        }
        if binding.artifact_id not in source_ids:
            raise ValueError(f"index ETF protocol source artifact ID mismatch: {name}")
    for name, impl_binding in protocol.implementation_bindings.items():
        source = resolve_repo_regular_file(
            Path(impl_binding.path), repo_root=root, field_name=f"implementation_bindings.{name}.path"
        )
        if _sha256_file(source) != impl_binding.sha256:
            raise ValueError(f"index ETF protocol implementation hash mismatch: {name}")
    return protocol


__all__ = [
    "DEFAULT_PATH",
    "IndexEtfRiskBudgetProtocol",
    "verify_index_etf_risk_budget_protocol",
]
