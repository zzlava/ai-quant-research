"""Fail-closed verifier for the closed index risk-budget research family."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.repo_file_safety import resolve_repo_regular_file

DEFAULT_PATH = Path("config/research/index-risk-budget-closeout-protocol-v1.json")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class FileBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class IndexRiskBudgetCloseoutProtocol(_StrictModel):
    schema_version: Literal["1"]
    protocol_version: Literal["index-risk-budget-closeout-protocol-v1"]
    closeout_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    decided_as_of: date
    user_utility_decision: Literal["maximum_drawdown_budget_relaxed_to_minus_30_percent"]
    source_bindings: dict[str, ArtifactBinding]
    file_bindings: dict[str, FileBinding]
    family_closure: dict[str, Any]
    mechanism_archive: dict[str, Any]
    cost_archive: dict[str, Any]
    static_control_limitations: dict[str, Any]
    allocation_policy: dict[str, Any]
    rebalance_policy: dict[str, Any]
    consumed_data_boundaries: dict[str, Any]
    future_evidence_policy: dict[str, Any]
    authorization_boundary: dict[str, bool]
    readiness: dict[str, bool]

    @model_validator(mode="after")
    def _fail_closed(self) -> IndexRiskBudgetCloseoutProtocol:
        if self.decided_as_of != date(2026, 8, 27):
            raise ValueError("closeout decision date drifted")
        if set(self.source_bindings) != {
            "sealed_research_protocol",
            "historical_replay_report",
            "authorization_consumption_receipt",
            "product_cost_contract",
        }:
            raise ValueError("closeout source binding set drifted")
        if set(self.file_bindings) != {"historical_daily_path", "historical_replay_engine"}:
            raise ValueError("closeout file binding set drifted")

        closure = self.family_closure
        if closure.get("family_id") != "index_time_series_risk_budget_v1":
            raise ValueError("closed research family drifted")
        if closure.get("outcome") != "effect_real_but_insufficient_and_too_costly":
            raise ValueError("closeout outcome drifted")
        for key in (
            "family_closed",
            "parameter_rescue_forbidden",
            "monthly_or_threshold_or_alternative_target_scan_forbidden",
            "trend_or_drawdown_add_on_in_consumed_history_forbidden",
            "automatic_restart_forbidden",
        ):
            if closure.get(key) is not True:
                raise ValueError(f"family closeout rule drifted: {key}")

        mechanism = self.mechanism_archive
        expected_mechanism = {
            "high_volatility_crash_deleveraging_effect_observed": True,
            "equal_average_exposure_drawdown_reduction_observed": True,
            "low_volatility_slow_decline_blindness_observed": True,
            "2014_2015_high_volatility_rally_miss_prediction_supported": False,
            "volatility_is_not_a_directional_trend_measure": True,
            "mechanism_effect_was_insufficient_for_utility_gate": True,
        }
        if mechanism != expected_mechanism:
            raise ValueError("mechanism archive drifted")

        cost = self.cost_archive
        expected_costs = {
            "vol_target_20d_base_explicit_cny": 26739.4055121966,
            "vol_target_20d_stress_explicit_cny": 50409.60095995553,
            "vol_target_60d_base_explicit_cny": 15193.352089808934,
            "vol_target_60d_stress_explicit_cny": 24440.826986691252,
        }
        if any(cost.get(key) != value for key, value in expected_costs.items()):
            raise ValueError("archived cost amount drifted")
        if cost.get("cost_totals_alone_are_not_zero_cost_counterfactuals") is not True:
            raise ValueError("cost attribution caveat drifted")
        if cost.get("base_cost_mde_gate_still_not_met") is not True:
            raise ValueError("base-cost robustness conclusion drifted")

        limitations = self.static_control_limitations
        for key in (
            "implicit_frictionless_daily_constant_mix",
            "not_buy_and_hold",
            "not_annual_calendar_rebalance",
            "not_a_live_implementable_upper_or_lower_bound",
            "reported_implementation_cost_understates_constant_mix_turnover",
        ):
            if limitations.get(key) is not True:
                raise ValueError(f"static-control limitation drifted: {key}")
        if limitations.get("role") != "confirmatory_benchmark_abstraction_only":
            raise ValueError("static-control role drifted")

        allocation = self.allocation_policy
        if allocation.get("policy_status") != "unfunded_execution_design_not_historical_claim":
            raise ValueError("allocation policy status drifted")
        if allocation.get("maximum_drawdown_utility_budget") != -0.30:
            raise ValueError("maximum drawdown utility budget drifted")
        if allocation.get("equity_policy_starting_weight") != 0.30:
            raise ValueError("equity policy starting weight drifted")
        if allocation.get("defensive_policy_starting_weight") != 0.70:
            raise ValueError("defensive policy starting weight drifted")
        if allocation.get("dynamic_risk_signal_enabled") is not False:
            raise ValueError("dynamic risk signal must remain disabled")
        if allocation.get("live_products_selected") is not False:
            raise ValueError("live products must remain unselected")

        rebalance = self.rebalance_policy
        expected_rebalance = {
            "rule": "annual_calendar",
            "signal_observation": "prior_calendar_year_final_market_close",
            "implementation_attempt": "next_calendar_year_first_market_trading_day_close",
            "target_equity_weight": 0.30,
            "target_defensive_weight": 0.70,
            "intrayear_threshold_rebalancing": False,
            "intrayear_signal_rebalancing": False,
            "natural_weight_drift_between_annual_events": True,
            "historical_performance_claim": False,
            "execution_validation_required": True,
        }
        if rebalance != expected_rebalance:
            raise ValueError("annual rebalance policy drifted")

        boundaries = self.consumed_data_boundaries
        if boundaries.get("seen_history") != "2005-01-04..2024-12-31":
            raise ValueError("seen-history boundary drifted")
        if boundaries.get("consumed_oos") != "2025-01-01..2026-08-21":
            raise ValueError("consumed OOS boundary drifted")
        if boundaries.get("reuse_for_new_dynamic_rule_development_forbidden") is not True:
            raise ValueError("consumed data reuse must remain forbidden")
        if boundaries.get("annual_rebalance_policy_was_not_backtested") is not True:
            raise ValueError("annual policy historical boundary drifted")

        future = self.future_evidence_policy
        if future.get("role") != "execution_validation_and_slow_evidence_accumulation":
            raise ValueError("future evidence role drifted")
        if future.get("new_trend_or_drawdown_rule_requires_new_protocol") is not True:
            raise ValueError("new dynamic rule must require a new protocol")
        if future.get("new_rule_record_starts_only_after_its_own_seal") is not True:
            raise ValueError("future rule record boundary drifted")
        if future.get("one_year_cannot_confirm_predictive_value") is not True:
            raise ValueError("one-year evidence boundary drifted")

        if any(self.authorization_boundary.values()):
            raise ValueError("closeout protocol cannot authorize downstream action")
        if any(self.readiness.values()):
            raise ValueError("closeout protocol cannot declare operational readiness")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _closeout_id(protocol: IndexRiskBudgetCloseoutProtocol) -> str:
    payload = protocol.model_dump(mode="json", exclude={"closeout_id"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def verify_index_risk_budget_closeout(
    *, repo_root: Path, path: Path = DEFAULT_PATH
) -> IndexRiskBudgetCloseoutProtocol:
    root = Path(repo_root).resolve(strict=True)
    resolved = resolve_repo_regular_file(path, repo_root=root, field_name="closeout_path")
    try:
        protocol = IndexRiskBudgetCloseoutProtocol.model_validate_json(resolved.read_text())
    except Exception as exc:
        raise ValueError("index risk-budget closeout protocol is missing or invalid") from exc
    if protocol.closeout_id != _closeout_id(protocol):
        raise ValueError("index risk-budget closeout self-hash mismatch")

    id_fields = {
        "sealed_research_protocol": "protocol_id",
        "historical_replay_report": "report_id",
        "authorization_consumption_receipt": "receipt_id",
        "product_cost_contract": "contract_id",
    }
    payloads: dict[str, dict[str, Any]] = {}
    for name, artifact_binding in protocol.source_bindings.items():
        source = resolve_repo_regular_file(
            Path(artifact_binding.path),
            repo_root=root,
            field_name=f"source_bindings.{name}.path",
        )
        if _sha256_file(source) != artifact_binding.sha256:
            raise ValueError(f"index risk-budget closeout source hash mismatch: {name}")
        payload = json.loads(source.read_text())
        if payload.get(id_fields[name]) != artifact_binding.artifact_id:
            raise ValueError(f"index risk-budget closeout source ID mismatch: {name}")
        payloads[name] = payload

    for name, file_binding in protocol.file_bindings.items():
        source = resolve_repo_regular_file(
            Path(file_binding.path),
            repo_root=root,
            field_name=f"file_bindings.{name}.path",
        )
        if _sha256_file(source) != file_binding.sha256:
            raise ValueError(f"index risk-budget closeout file hash mismatch: {name}")

    report = payloads["historical_replay_report"]
    receipt = payloads["authorization_consumption_receipt"]
    if receipt.get("report_id") != report.get("report_id"):
        raise ValueError("closeout replay report/receipt binding mismatch")
    if receipt.get("report_sha256") != protocol.source_bindings[
        "historical_replay_report"
    ].sha256:
        raise ValueError("closeout receipt report hash mismatch")
    if receipt.get("daily_path_sha256") != protocol.file_bindings[
        "historical_daily_path"
    ].sha256:
        raise ValueError("closeout receipt daily-path hash mismatch")
    if any(item.get("all_hard_gates_pass") is not False for item in report["candidate_comparisons"]):
        raise ValueError("closed candidate unexpectedly passed a hard gate")
    if report.get("oos_claim") is not False or report.get("ready_for_trading") is not False:
        raise ValueError("historical report boundary drifted")
    return protocol


__all__ = [
    "DEFAULT_PATH",
    "IndexRiskBudgetCloseoutProtocol",
    "verify_index_risk_budget_closeout",
]
