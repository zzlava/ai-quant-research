"""Sealed recovery overlay for the otherwise terminal layer-one risk lock.

The upstream layer-one protocol requires explicit human confirmation but its
cash-only lock cannot recover account drawdown by itself. This downstream
overlay preserves that history and adds an auditable risk-capital epoch reset.
It is a research/implementation contract only and never authorizes an automatic
unlock, scoring, orders or trading.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.layer_one_regime import (
    BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_ID,
    BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
)
from app.research.repo_file_safety import resolve_repo_regular_file

SCHEMA_VERSION: Literal["1"] = "1"
POLICY_VERSION: Literal["layer-one-risk-lock-recovery-policy-v1"] = (
    "layer-one-risk-lock-recovery-policy-v1"
)
CONFIRMATION_AS_OF = date(2026, 8, 27)
DEFAULT_POLICY_PATH = Path("config/research/layer-one-risk-lock-recovery-policy-v1.json")
EXPECTED_POLICY_ID = "84c2734b0b418873b4c5dcf20890cb480c9861ea18996dba66e923847b610c8e"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LiveRecoveryRule(_StrictModel):
    min_cooling_trading_days: Literal[20] = 20
    index_trend_must_not_be_negative: Literal[True] = True
    realized_volatility_must_be_strictly_below: float = 0.27
    explicit_user_confirmation_required: Literal[True] = True
    user_must_confirm_new_risk_capital_epoch: Literal[True] = True
    new_epoch_peak_equals_current_equity: Literal[True] = True
    pre_reset_peak_and_drawdown_remain_in_audit: Literal[True] = True
    red_line_breach_remains_latched: Literal[True] = True
    first_reentry_budget_cap: float = 0.3
    first_reentry_only_on_first_market_trading_day_of_week: Literal[True] = True
    auto_clear_forbidden: Literal[True] = True
    reset_without_lock_forbidden: Literal[True] = True

    @model_validator(mode="after")
    def _frozen_values(self) -> LiveRecoveryRule:
        if abs(self.realized_volatility_must_be_strictly_below - 0.27) > 1e-15:
            raise ValueError("recovery volatility threshold must remain 0.27")
        if abs(self.first_reentry_budget_cap - 0.3) > 1e-15:
            raise ValueError("first re-entry budget cap must remain 0.3")
        return self


class HistoricalCounterfactualRule(_StrictModel):
    purpose: Literal["historical_validation_recovery_sensitivity_only"]
    simulate_confirmation_at_first_eligible_weekly_action: Literal[True] = True
    simulated_confirmation_is_not_observed_user_action: Literal[True] = True
    same_live_eligibility_and_epoch_reset_rules: Literal[True] = True
    may_be_used_for_frozen_2013_2021_gate_evidence: Literal[True] = True
    may_not_be_used_for_oos_claim: Literal[True] = True
    may_not_auto_apply_live: Literal[True] = True


class LayerOneRiskLockRecoveryPolicy(_StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    policy_version: Literal["layer-one-risk-lock-recovery-policy-v1"] = POLICY_VERSION
    policy_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmation_as_of: date = CONFIRMATION_AS_OF
    layer_one_index_data_evidence_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    layer_one_index_protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    two_layer_decision_contract_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    problem_statement: Literal[
        "cash-only risk lock leaves account drawdown unchanged and makes the original unlock predicate unreachable"
    ]
    live_recovery: LiveRecoveryRule
    historical_counterfactual: HistoricalCounterfactualRule
    policy_overlay_only: Literal[True] = True
    upstream_loss_history_not_rewritten: Literal[True] = True
    requires_engine_and_persistence_implementation_before_controlled_trial: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @model_validator(mode="after")
    def _bindings(self) -> LayerOneRiskLockRecoveryPolicy:
        if self.confirmation_as_of != CONFIRMATION_AS_OF:
            raise ValueError("confirmation_as_of drifted")
        if self.layer_one_index_data_evidence_id != BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_ID:
            raise ValueError("index data evidence binding drifted")
        if self.layer_one_index_protocol_id != BOUND_LAYER_ONE_INDEX_PROTOCOL_ID:
            raise ValueError("layer-one protocol binding drifted")
        if self.two_layer_decision_contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
            raise ValueError("two-layer contract binding drifted")
        if any((self.ready_for_scoring, self.ready_for_backtest, self.ready_for_orders, self.ready_for_trading)):
            raise ValueError("recovery policy cannot authorize scoring, backtest, orders or trading")
        return self


def canonical_payload(policy: LayerOneRiskLockRecoveryPolicy) -> dict[str, Any]:
    return policy.model_dump(mode="json", exclude={"policy_id"})


def compute_policy_id(policy: LayerOneRiskLockRecoveryPolicy) -> str:
    return hashlib.sha256(
        json.dumps(
            canonical_payload(policy),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def seal_policy(policy: LayerOneRiskLockRecoveryPolicy) -> LayerOneRiskLockRecoveryPolicy:
    return policy.model_copy(update={"policy_id": compute_policy_id(policy)})


def build_policy() -> LayerOneRiskLockRecoveryPolicy:
    return seal_policy(
        LayerOneRiskLockRecoveryPolicy(
            layer_one_index_data_evidence_id=BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_ID,
            layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            problem_statement=(
                "cash-only risk lock leaves account drawdown unchanged and makes the original "
                "unlock predicate unreachable"
            ),
            live_recovery=LiveRecoveryRule(),
            historical_counterfactual=HistoricalCounterfactualRule(
                purpose="historical_validation_recovery_sensitivity_only"
            ),
        )
    )


def verify_policy(policy: LayerOneRiskLockRecoveryPolicy) -> LayerOneRiskLockRecoveryPolicy:
    if policy.policy_id is None or policy.policy_id != compute_policy_id(policy):
        raise ValueError("risk-lock recovery policy_id mismatch")
    expected = build_policy()
    if policy.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise ValueError("risk-lock recovery policy does not match the sealed factory")
    return policy


def load_policy(path: Path) -> LayerOneRiskLockRecoveryPolicy:
    try:
        return LayerOneRiskLockRecoveryPolicy.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("risk-lock recovery policy is missing or invalid") from exc


def verify_policy_file(
    *,
    repo_root: Path,
    policy_path: Path = DEFAULT_POLICY_PATH,
) -> LayerOneRiskLockRecoveryPolicy:
    root = Path(repo_root).resolve(strict=True)
    path = resolve_repo_regular_file(policy_path, repo_root=root, field_name="policy_path")
    expected_path = (root / DEFAULT_POLICY_PATH).resolve()
    if path != expected_path:
        raise ValueError("risk-lock recovery policy must use the fixed repository path")
    policy = verify_policy(load_policy(path))
    if policy.policy_id != EXPECTED_POLICY_ID:
        raise ValueError("risk-lock recovery policy_id drifted from bound constant")
    return policy


__all__ = [
    "DEFAULT_POLICY_PATH",
    "EXPECTED_POLICY_ID",
    "HistoricalCounterfactualRule",
    "LayerOneRiskLockRecoveryPolicy",
    "LiveRecoveryRule",
    "build_policy",
    "canonical_payload",
    "compute_policy_id",
    "load_policy",
    "seal_policy",
    "verify_policy",
    "verify_policy_file",
]
