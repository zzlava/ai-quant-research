"""Pre-evaluation MDE calibration using only the sealed static control frontier."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import tempfile
from datetime import date
from pathlib import Path
from statistics import NormalDist, stdev
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.index_research_product_cost_contract import (
    verify_index_research_product_cost_contract,
)
from app.research.index_risk_budget_evaluator import (
    STATIC_WEIGHTS,
    _calmar_from_returns,
    _circular_block_indices,
    load_verified_index_inputs,
    simulate_arm,
)
from app.research.index_time_series_trial_ledger import verify_index_time_series_trial_ledger
from app.research.repo_file_safety import resolve_repo_regular_file

DEFAULT_PROTOCOL_PATH = Path("config/research/index-risk-budget-power-protocol-v1.json")
DEFAULT_REVIEW_PATH = Path("data/research/index-risk-budget-power-v1/review.json")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class IndexRiskBudgetPowerProtocol(_StrictModel):
    schema_version: Literal["1"]
    protocol_version: Literal["index-risk-budget-power-protocol-v1"]
    protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_as_of: date
    role: Literal["pre_evaluation_static_frontier_uncertainty_calibration"]
    source_bindings: dict[str, SourceBinding]
    calibration_window: Literal["2005-01-04..2024-12-31"]
    static_equity_weight_grid: list[float]
    calibration_cost_scenario: Literal["stress"]
    familywise_alpha: float = Field(gt=0.0, lt=1.0)
    family_size: Literal[2]
    pretest_alpha_allocation: Literal["bonferroni_worst_case"]
    target_power: float = Field(gt=0.5, lt=1.0)
    minimum_effect_of_interest_calmar_difference: float = Field(gt=0.0)
    method: dict[str, Any]
    policies: dict[str, bool]
    readiness: dict[str, bool]

    @model_validator(mode="after")
    def _fail_closed(self) -> IndexRiskBudgetPowerProtocol:
        if self.sealed_as_of != date(2026, 8, 27):
            raise ValueError("index power protocol date drifted")
        if self.familywise_alpha != 0.05 or self.target_power != 0.8:
            raise ValueError("index power alpha or target power drifted")
        if self.minimum_effect_of_interest_calmar_difference != 0.1:
            raise ValueError("index power minimum effect of interest drifted")
        if self.static_equity_weight_grid != list(STATIC_WEIGHTS):
            raise ValueError("index power static grid drifted")
        if set(self.source_bindings) != {
            "equity_snapshot",
            "defensive_snapshot",
            "product_cost_contract",
            "trial_ledger",
        }:
            raise ValueError("index power source binding set drifted")
        method = self.method
        expected = {
            "bootstrap": "synchronized_circular_moving_block",
            "block_length_trading_days": 20,
            "calibration_replications": 2000,
            "calibration_seed": 20260827,
            "evaluation_bootstrap_replications": 2000,
            "evaluation_bootstrap_seed": 20260828,
            "calibration_statistic": "maximum_bootstrap_standard_deviation_of_best_static_minus_each_static_calmar",
            "mde_formula": "(z_1_minus_alpha_over_family_plus_z_target_power)*maximum_standard_deviation",
        }
        if method != expected:
            raise ValueError("index power method drifted")
        required_true = (
            "dynamic_candidate_returns_must_not_be_loaded_for_calibration",
            "calibration_is_not_candidate_evaluation",
            "calibration_is_not_oos",
            "consumed_oos_reuse_forbidden",
            "mde_must_not_be_changed_after_candidate_results",
            "minimum_effect_of_interest_must_not_be_changed_after_candidate_results",
        )
        if any(self.policies.get(key) is not True for key in required_true):
            raise ValueError("index power fail-closed policy drifted")
        expected_readiness = {
            "ready_to_build_static_only_power_review": True,
            "ready_for_candidate_evaluation": False,
            "ready_for_orders": False,
            "ready_for_trading": False,
        }
        if self.readiness != expected_readiness:
            raise ValueError("index power readiness boundary drifted")
        return self


class IndexRiskBudgetPowerReview(_StrictModel):
    schema_version: Literal["1"] = "1"
    review_version: Literal["index-risk-budget-power-review-v1"] = (
        "index-risk-budget-power-review-v1"
    )
    review_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    equity_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    defensive_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    product_cost_contract_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    trial_ledger_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    calibration_observations: int = Field(gt=2420)
    block_length_trading_days: Literal[20]
    calibration_replications: Literal[2000]
    calibration_seed: Literal[20260827]
    evaluation_bootstrap_replications: Literal[2000]
    evaluation_bootstrap_seed: Literal[20260828]
    per_candidate_pretest_alpha: float = Field(gt=0.0, lt=0.05)
    target_power: float = Field(gt=0.5, lt=1.0)
    maximum_static_frontier_calmar_standard_deviation: float = Field(gt=0.0)
    sealed_mde_calmar_difference: float = Field(gt=0.0)
    minimum_effect_of_interest_calmar_difference: float = Field(gt=0.0)
    family_outcome: Literal["evaluable_for_sealed_mde", "not_evaluable"]
    dynamic_candidate_returns_loaded: Literal[False] = False
    calibration_is_candidate_evaluation: Literal[False] = False
    consumes_oos: Literal[False] = False
    ready_for_authorized_historical_replay: bool
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False

    @model_validator(mode="after")
    def _outcome_matches(self) -> IndexRiskBudgetPowerReview:
        if self.target_power != 0.8 or self.minimum_effect_of_interest_calmar_difference != 0.1:
            raise ValueError("index power review target or minimum effect drifted")
        expected = (
            "evaluable_for_sealed_mde"
            if self.sealed_mde_calmar_difference
            <= self.minimum_effect_of_interest_calmar_difference
            else "not_evaluable"
        )
        if self.family_outcome != expected:
            raise ValueError("index power family outcome does not match sealed MDE")
        if self.ready_for_authorized_historical_replay != (expected == "evaluable_for_sealed_mde"):
            raise ValueError("index power replay readiness does not match family outcome")
        return self


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_protocol_id(protocol: IndexRiskBudgetPowerProtocol) -> str:
    return _json_hash(protocol.model_dump(mode="json", exclude={"protocol_id"}))


def compute_review_id(review: IndexRiskBudgetPowerReview) -> str:
    return _json_hash(review.model_dump(mode="json", exclude={"review_id"}))


def verify_index_risk_budget_power_protocol(
    *, repo_root: Path, path: Path = DEFAULT_PROTOCOL_PATH
) -> IndexRiskBudgetPowerProtocol:
    root = Path(repo_root).resolve(strict=True)
    resolved = resolve_repo_regular_file(path, repo_root=root, field_name="power_protocol_path")
    try:
        protocol = IndexRiskBudgetPowerProtocol.model_validate_json(resolved.read_text())
    except Exception as exc:
        raise ValueError("index risk-budget power protocol is missing or invalid") from exc
    if protocol.protocol_id != compute_protocol_id(protocol):
        raise ValueError("index risk-budget power protocol self-hash mismatch")
    for name, binding in protocol.source_bindings.items():
        source = resolve_repo_regular_file(
            Path(binding.path), repo_root=root, field_name=f"source_bindings.{name}.path"
        )
        if _sha256_file(source) != binding.sha256:
            raise ValueError(f"index power source hash mismatch: {name}")
        payload = json.loads(source.read_text())
        if binding.artifact_id not in {
            payload.get("snapshot_id"),
            payload.get("contract_id"),
            payload.get("ledger_id"),
        }:
            raise ValueError(f"index power source artifact ID mismatch: {name}")
    return protocol


def build_index_risk_budget_power_review(
    *, repo_root: Path, protocol_path: Path = DEFAULT_PROTOCOL_PATH
) -> IndexRiskBudgetPowerReview:
    root = Path(repo_root).resolve(strict=True)
    protocol = verify_index_risk_budget_power_protocol(repo_root=root, path=protocol_path)
    dates, risk, equity, defensive, equity_snapshot_id, defensive_snapshot_id = (
        load_verified_index_inputs(repo_root=root)
    )
    cost_contract = verify_index_research_product_cost_contract(repo_root=root)
    ledger = verify_index_time_series_trial_ledger(repo_root=root)
    scenario = cost_contract.research_cost_scenarios["stress"]
    returns: dict[str, list[float]] = {}
    for weight in STATIC_WEIGHTS:
        arm_id = f"static_equity_{int(weight * 100):03d}"
        _metrics, frame = simulate_arm(
            arm_id=arm_id,
            dates=dates,
            risk_levels=risk,
            equity_levels=equity,
            defensive_levels=defensive,
            scenario_label="stress",
            scenario=scenario,
            static_weight=weight,
        )
        values = [float(value) for value in frame.get_column("net_return").to_list()]
        returns[arm_id] = values[1:]
    length = len(next(iter(returns.values())))
    if any(len(values) != length for values in returns.values()):
        raise ValueError("static power-calibration return paths are not aligned")
    randomizer = random.Random(int(protocol.method["calibration_seed"]))
    gaps: dict[str, list[float]] = {arm_id: [] for arm_id in returns}
    for _ in range(int(protocol.method["calibration_replications"])):
        indices = _circular_block_indices(
            length=length,
            block_length=int(protocol.method["block_length_trading_days"]),
            randomizer=randomizer,
        )
        calmars = {
            arm_id: _calmar_from_returns([values[index] for index in indices])
            for arm_id, values in returns.items()
        }
        if any(not math.isfinite(value) for value in calmars.values()):
            raise ValueError("static power-calibration Calmar is not finite")
        best = max(calmars.values())
        for arm_id, value in calmars.items():
            gaps[arm_id].append(best - value)
    maximum_standard_deviation = max(stdev(values) for values in gaps.values())
    per_candidate_alpha = protocol.familywise_alpha / protocol.family_size
    z_alpha = NormalDist().inv_cdf(1.0 - per_candidate_alpha)
    z_power = NormalDist().inv_cdf(protocol.target_power)
    mde = (z_alpha + z_power) * maximum_standard_deviation
    outcome = (
        "evaluable_for_sealed_mde"
        if mde <= protocol.minimum_effect_of_interest_calmar_difference
        else "not_evaluable"
    )
    review = IndexRiskBudgetPowerReview(
        protocol_id=protocol.protocol_id,
        equity_snapshot_id=equity_snapshot_id,
        defensive_snapshot_id=defensive_snapshot_id,
        product_cost_contract_id=cost_contract.contract_id,
        trial_ledger_id=ledger.ledger_id,
        calibration_observations=length,
        block_length_trading_days=20,
        calibration_replications=2000,
        calibration_seed=20260827,
        evaluation_bootstrap_replications=2000,
        evaluation_bootstrap_seed=20260828,
        per_candidate_pretest_alpha=per_candidate_alpha,
        target_power=0.8,
        maximum_static_frontier_calmar_standard_deviation=maximum_standard_deviation,
        sealed_mde_calmar_difference=mde,
        minimum_effect_of_interest_calmar_difference=0.1,
        family_outcome=outcome,
        ready_for_authorized_historical_replay=(outcome == "evaluable_for_sealed_mde"),
    )
    return review.model_copy(update={"review_id": compute_review_id(review)})


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_index_risk_budget_power_review(
    *, repo_root: Path, review: IndexRiskBudgetPowerReview, path: Path = DEFAULT_REVIEW_PATH
) -> Path:
    root = Path(repo_root).resolve(strict=True)
    if review.review_id != compute_review_id(review):
        raise ValueError("index risk-budget power review self-hash mismatch")
    destination = (root / path).resolve()
    if not destination.is_relative_to(root):
        raise ValueError("index power review output escapes repository")
    payload = review.model_dump(mode="json")
    if destination.exists():
        if json.loads(destination.read_text()) != payload:
            raise FileExistsError("existing index power review differs from full recomputation")
    else:
        _atomic_json(destination, payload)
    return destination


def verify_index_risk_budget_power_review(
    *,
    repo_root: Path,
    review_path: Path = DEFAULT_REVIEW_PATH,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
) -> IndexRiskBudgetPowerReview:
    root = Path(repo_root).resolve(strict=True)
    resolved = resolve_repo_regular_file(review_path, repo_root=root, field_name="power_review_path")
    try:
        actual = IndexRiskBudgetPowerReview.model_validate_json(resolved.read_text())
    except Exception as exc:
        raise ValueError("index risk-budget power review is missing or invalid") from exc
    if actual.review_id != compute_review_id(actual):
        raise ValueError("index risk-budget power review self-hash mismatch")
    expected = build_index_risk_budget_power_review(repo_root=root, protocol_path=protocol_path)
    if actual != expected:
        raise ValueError("index risk-budget power review differs from full static-only recomputation")
    return actual


__all__ = [
    "DEFAULT_PROTOCOL_PATH",
    "DEFAULT_REVIEW_PATH",
    "IndexRiskBudgetPowerProtocol",
    "IndexRiskBudgetPowerReview",
    "build_index_risk_budget_power_review",
    "compute_protocol_id",
    "compute_review_id",
    "verify_index_risk_budget_power_protocol",
    "verify_index_risk_budget_power_review",
    "write_index_risk_budget_power_review",
]
