from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from statistics import NormalDist
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STATISTICAL_POWER_GATE_SCHEMA_VERSION: Literal["1"] = "1"
STATISTICAL_POWER_GATE_VERSION: Literal["statistical-power-gate-v1"] = "statistical-power-gate-v1"
STATISTICAL_POWER_REVIEW_VERSION: Literal["statistical-power-review-v1"] = "statistical-power-review-v1"
DEFAULT_STATISTICAL_POWER_GATE_PATH = Path("config/research/statistical-power-gate-v1.json")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PowerPlanningMethod(_StrictModel):
    alternative: Literal["one_sided_positive"] = "one_sided_positive"
    familywise_alpha: float = Field(gt=0.0, lt=1.0)
    target_power: float = Field(gt=0.5, lt=1.0)
    family_size: int = Field(ge=1)
    alpha_allocation: Literal["bonferroni_worst_case_pretest"] = "bonferroni_worst_case_pretest"
    uncertainty_source: Literal["audited_hac_standard_error_only"] = "audited_hac_standard_error_only"
    approximation: Literal["normal_known_standard_error"] = "normal_known_standard_error"


class PowerEndpoint(_StrictModel):
    endpoint_id: str = Field(min_length=1)
    endpoint_type: Literal["mean_spearman_ic"] = "mean_spearman_ic"
    horizon_market_days: int = Field(ge=1)
    minimum_effect_of_interest: float = Field(gt=0.0)
    direction: Literal["positive"] = "positive"

    @field_validator("endpoint_id", mode="before")
    @classmethod
    def _reject_blank_endpoint_id(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            raise ValueError("endpoint_id cannot be blank")
        return value


class PowerSourceBinding(_StrictModel):
    diagnostic_report_path: str = Field(min_length=1)
    diagnostic_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_role: Literal["retrospective_variance_calibration_only"] = (
        "retrospective_variance_calibration_only"
    )

    @field_validator("diagnostic_report_path", mode="before")
    @classmethod
    def _validate_repo_relative_path(cls, value: object) -> object:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("diagnostic_report_path must be a non-blank repository-relative path")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("diagnostic_report_path must stay within the repository")
        return value


class PowerGatePolicies(_StrictModel):
    missing_or_invalid_standard_error: Literal["not_evaluable"] = "not_evaluable"
    mde_above_minimum_effect: Literal["not_evaluable"] = "not_evaluable"
    family_requires_every_endpoint_powered: Literal[True] = True
    retrospective_review_must_not_reinterpret_results: Literal[True] = True
    retrospective_review_must_not_select_factors: Literal[True] = True
    no_oos_consumption: Literal[True] = True


class StatisticalPowerGateProtocol(_StrictModel):
    schema_version: Literal["1"] = STATISTICAL_POWER_GATE_SCHEMA_VERSION
    protocol_version: Literal["statistical-power-gate-v1"] = STATISTICAL_POWER_GATE_VERSION
    planning_role: Literal["prospective_gate_with_retrospective_calibration"] = (
        "prospective_gate_with_retrospective_calibration"
    )
    method: PowerPlanningMethod
    endpoints: list[PowerEndpoint] = Field(min_length=1)
    source_binding: PowerSourceBinding
    policies: PowerGatePolicies = Field(default_factory=PowerGatePolicies)
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    protocol_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_family(self) -> StatisticalPowerGateProtocol:
        endpoint_ids = [item.endpoint_id for item in self.endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("statistical power endpoint_id values must be unique")
        if self.method.family_size != len(endpoint_ids):
            raise ValueError("method.family_size must equal the number of endpoints")
        horizons = {item.horizon_market_days for item in self.endpoints}
        if len(horizons) != 1:
            raise ValueError("v1 requires one common endpoint horizon")
        return self


class PowerReviewRow(_StrictModel):
    endpoint_id: str
    endpoint_type: Literal["mean_spearman_ic"] = "mean_spearman_ic"
    horizon_market_days: int = Field(ge=1)
    minimum_effect_of_interest: float = Field(gt=0.0)
    valid_ic_dates: int = Field(ge=2)
    hac_lag: int = Field(ge=0)
    calibration_mean_ic: float
    calibration_hac_statistic: float
    calibration_hac_standard_error: float = Field(gt=0.0)
    per_endpoint_alpha: float = Field(gt=0.0, lt=1.0)
    target_power: float = Field(gt=0.5, lt=1.0)
    alpha_critical_z: float = Field(gt=0.0)
    power_z: float = Field(gt=0.0)
    normal_approximation_mde: float = Field(gt=0.0)
    maximum_standard_error_for_minimum_effect: float = Field(gt=0.0)
    sufficiently_powered_for_minimum_effect: bool
    outcome: Literal["evaluable_for_minimum_effect", "not_evaluable"]

    @model_validator(mode="after")
    def _outcome_matches_power(self) -> PowerReviewRow:
        expected = "evaluable_for_minimum_effect" if self.sufficiently_powered_for_minimum_effect else "not_evaluable"
        if self.outcome != expected:
            raise ValueError("row outcome does not match sufficiently_powered_for_minimum_effect")
        return self


class StatisticalPowerReview(_StrictModel):
    schema_version: Literal["1"] = STATISTICAL_POWER_GATE_SCHEMA_VERSION
    review_version: Literal["statistical-power-review-v1"] = STATISTICAL_POWER_REVIEW_VERSION
    protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_diagnostic_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_diagnostic_report_id: str = Field(min_length=1)
    retrospective_calibration_only: Literal[True] = True
    rows: list[PowerReviewRow] = Field(min_length=1)
    endpoints_evaluable: int = Field(ge=0)
    endpoints_not_evaluable: int = Field(ge=0)
    family_outcome: Literal["evaluable_for_minimum_effect", "not_evaluable"]
    does_not_reinterpret_observed_results: Literal[True] = True
    does_not_select_factors: Literal[True] = True
    consumes_oos: Literal[False] = False
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    review_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_counts_and_outcome(self) -> StatisticalPowerReview:
        if self.endpoints_evaluable + self.endpoints_not_evaluable != len(self.rows):
            raise ValueError("power review endpoint counts do not match rows")
        actual_evaluable = sum(item.sufficiently_powered_for_minimum_effect for item in self.rows)
        if self.endpoints_evaluable != actual_evaluable:
            raise ValueError("endpoints_evaluable does not match row outcomes")
        expected = "evaluable_for_minimum_effect" if actual_evaluable == len(self.rows) else "not_evaluable"
        if self.family_outcome != expected:
            raise ValueError("family_outcome does not match endpoint outcomes")
        return self


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_protocol_payload(protocol: StatisticalPowerGateProtocol) -> dict[str, Any]:
    return protocol.model_dump(mode="json", exclude={"protocol_id"})


def compute_protocol_id(protocol: StatisticalPowerGateProtocol) -> str:
    return hashlib.sha256(_canonical_json_bytes(canonical_protocol_payload(protocol))).hexdigest()


def seal_protocol(protocol: StatisticalPowerGateProtocol) -> StatisticalPowerGateProtocol:
    return protocol.model_copy(update={"protocol_id": compute_protocol_id(protocol)})


def load_protocol(path: Path) -> StatisticalPowerGateProtocol:
    try:
        return StatisticalPowerGateProtocol.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("statistical power gate protocol is missing or invalid") from exc


def verify_protocol(
    protocol_path: Path = DEFAULT_STATISTICAL_POWER_GATE_PATH,
    *,
    repo_root: Path | None = None,
) -> StatisticalPowerGateProtocol:
    root = (repo_root or Path.cwd()).resolve()
    protocol = load_protocol(protocol_path)
    if protocol.protocol_id is None:
        raise ValueError("statistical power gate protocol_id is missing")
    if protocol.protocol_id != compute_protocol_id(protocol):
        raise ValueError("statistical power gate protocol_id does not match canonical content hash")
    source_path = (root / protocol.source_binding.diagnostic_report_path).resolve()
    if not source_path.is_relative_to(root) or not source_path.is_file():
        raise ValueError("bound diagnostic report is missing or outside the repository")
    if _sha256_file(source_path) != protocol.source_binding.diagnostic_report_sha256:
        raise ValueError("bound diagnostic report SHA-256 does not match protocol")
    return protocol


def _finite_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _positive_int(value: object, *, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")
    return value


def _load_diagnostic_factor_calibration(path: Path) -> tuple[str, dict[str, dict[str, object]]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("bound alpha diagnostic report is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("bound alpha diagnostic report must be a JSON object")
    report_id = payload.get("report_id")
    if not isinstance(report_id, str) or report_id.strip() == "":
        raise ValueError("bound alpha diagnostic report_id is missing")
    decisions = payload.get("factor_decisions")
    if not isinstance(decisions, list):
        raise ValueError("bound alpha diagnostic factor_decisions is missing")
    by_factor: dict[str, dict[str, object]] = {}
    for item in decisions:
        if not isinstance(item, dict):
            raise ValueError("factor_decisions entries must be objects")
        factor_id = item.get("factor_id")
        primary = item.get("primary")
        if not isinstance(factor_id, str) or factor_id.strip() == "" or not isinstance(primary, dict):
            raise ValueError("factor_decisions entries require factor_id and primary")
        if factor_id in by_factor:
            raise ValueError(f"duplicate factor decision: {factor_id}")
        by_factor[factor_id] = primary
    return report_id, by_factor


def _build_row(
    endpoint: PowerEndpoint,
    primary: dict[str, object],
    *,
    method: PowerPlanningMethod,
) -> PowerReviewRow:
    horizon = _positive_int(primary.get("horizon_days"), field_name="primary.horizon_days", minimum=1)
    if horizon != endpoint.horizon_market_days:
        raise ValueError(f"diagnostic horizon mismatch for endpoint {endpoint.endpoint_id}")
    valid_ic_dates = _positive_int(primary.get("valid_ic_dates"), field_name="primary.valid_ic_dates", minimum=2)
    hac_lag = _positive_int(primary.get("hac_lag"), field_name="primary.hac_lag", minimum=0)
    mean_ic = _finite_number(primary.get("pooled_mean_ic"), field_name="primary.pooled_mean_ic")
    hac_stat = _finite_number(primary.get("hac_statistic"), field_name="primary.hac_statistic")
    if mean_ic == 0.0 or hac_stat == 0.0 or mean_ic * hac_stat <= 0.0:
        raise ValueError(f"cannot recover a positive HAC standard error for endpoint {endpoint.endpoint_id}")
    hac_standard_error = mean_ic / hac_stat
    if not math.isfinite(hac_standard_error) or hac_standard_error <= 0.0:
        raise ValueError(f"invalid HAC standard error for endpoint {endpoint.endpoint_id}")

    per_endpoint_alpha = method.familywise_alpha / method.family_size
    alpha_critical_z = NormalDist().inv_cdf(1.0 - per_endpoint_alpha)
    power_z = NormalDist().inv_cdf(method.target_power)
    z_sum = alpha_critical_z + power_z
    mde = z_sum * hac_standard_error
    maximum_se = endpoint.minimum_effect_of_interest / z_sum
    powered = mde <= endpoint.minimum_effect_of_interest
    return PowerReviewRow(
        endpoint_id=endpoint.endpoint_id,
        endpoint_type=endpoint.endpoint_type,
        horizon_market_days=endpoint.horizon_market_days,
        minimum_effect_of_interest=endpoint.minimum_effect_of_interest,
        valid_ic_dates=valid_ic_dates,
        hac_lag=hac_lag,
        calibration_mean_ic=mean_ic,
        calibration_hac_statistic=hac_stat,
        calibration_hac_standard_error=hac_standard_error,
        per_endpoint_alpha=per_endpoint_alpha,
        target_power=method.target_power,
        alpha_critical_z=alpha_critical_z,
        power_z=power_z,
        normal_approximation_mde=mde,
        maximum_standard_error_for_minimum_effect=maximum_se,
        sufficiently_powered_for_minimum_effect=powered,
        outcome="evaluable_for_minimum_effect" if powered else "not_evaluable",
    )


def canonical_review_payload(review: StatisticalPowerReview) -> dict[str, Any]:
    return review.model_dump(mode="json", exclude={"review_id"})


def compute_review_id(review: StatisticalPowerReview) -> str:
    return hashlib.sha256(_canonical_json_bytes(canonical_review_payload(review))).hexdigest()


def seal_review(review: StatisticalPowerReview) -> StatisticalPowerReview:
    return review.model_copy(update={"review_id": compute_review_id(review)})


def build_retrospective_power_review(
    *,
    protocol_path: Path = DEFAULT_STATISTICAL_POWER_GATE_PATH,
    repo_root: Path | None = None,
) -> StatisticalPowerReview:
    root = (repo_root or Path.cwd()).resolve()
    protocol = verify_protocol(protocol_path, repo_root=root)
    assert protocol.protocol_id is not None
    source_path = (root / protocol.source_binding.diagnostic_report_path).resolve()
    report_id, calibrations = _load_diagnostic_factor_calibration(source_path)
    expected = {item.endpoint_id for item in protocol.endpoints}
    actual = set(calibrations)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"diagnostic factor set does not match protocol; missing={missing}, extra={extra}")
    rows = [
        _build_row(endpoint, calibrations[endpoint.endpoint_id], method=protocol.method)
        for endpoint in protocol.endpoints
    ]
    evaluable = sum(item.sufficiently_powered_for_minimum_effect for item in rows)
    review = StatisticalPowerReview(
        protocol_id=protocol.protocol_id,
        source_diagnostic_report_sha256=protocol.source_binding.diagnostic_report_sha256,
        source_diagnostic_report_id=report_id,
        rows=rows,
        endpoints_evaluable=evaluable,
        endpoints_not_evaluable=len(rows) - evaluable,
        family_outcome="evaluable_for_minimum_effect" if evaluable == len(rows) else "not_evaluable",
    )
    return seal_review(review)


def write_power_review(path: Path, review: StatisticalPowerReview) -> StatisticalPowerReview:
    sealed = seal_review(review)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = sealed.model_dump_json(indent=2) + "\n"
    if destination.exists():
        if not destination.is_file():
            raise ValueError("statistical power review destination exists and is not a file")
        if destination.read_text(encoding="utf-8") == text:
            return sealed
        raise FileExistsError("statistical power review destination already contains different bytes")
    handle: int | None = None
    temporary: Path | None = None
    try:
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            handle = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if handle is not None:
            os.close(handle)
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return sealed


def verify_power_review(
    *,
    review_path: Path,
    protocol_path: Path = DEFAULT_STATISTICAL_POWER_GATE_PATH,
    repo_root: Path | None = None,
) -> StatisticalPowerReview:
    try:
        review = StatisticalPowerReview.model_validate_json(Path(review_path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("statistical power review is missing or invalid") from exc
    if review.review_id is None or review.review_id != compute_review_id(review):
        raise ValueError("statistical power review_id does not match canonical content hash")
    rebuilt = build_retrospective_power_review(protocol_path=protocol_path, repo_root=repo_root)
    if canonical_review_payload(review) != canonical_review_payload(rebuilt):
        raise ValueError("statistical power review does not match full recomputation")
    return review


__all__ = [
    "DEFAULT_STATISTICAL_POWER_GATE_PATH",
    "PowerEndpoint",
    "PowerGatePolicies",
    "PowerPlanningMethod",
    "PowerReviewRow",
    "PowerSourceBinding",
    "StatisticalPowerGateProtocol",
    "StatisticalPowerReview",
    "build_retrospective_power_review",
    "compute_protocol_id",
    "compute_review_id",
    "seal_protocol",
    "verify_power_review",
    "verify_protocol",
    "write_power_review",
]
