"""Audited Deflated Sharpe formula and fail-closed local input binding."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import uuid
from datetime import date
from pathlib import Path
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.experiment_ledger import (
    DEFAULT_RESEARCH_TRIAL_LEDGER_PATH,
    verify_research_trial_ledger,
)
from app.research.layer_one_recovery_counterfactual import (
    DEFAULT_REPORT_PATH as RECOVERY_REPORT_PATH,
)
from app.research.layer_one_recovery_counterfactual import (
    verify_recovery_counterfactual_file,
)
from app.research.repo_file_safety import resolve_repo_regular_file

SCHEMA_VERSION: Literal["1"] = "1"
AUDIT_VERSION: Literal["deflated-sharpe-audit-v1"] = "deflated-sharpe-audit-v1"
DEFAULT_OUTPUT_PATH = Path("data/research/deflated-sharpe-audit-v1.json")
TRADING_DAYS = 242


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DsrFormulaInputs(_StrictModel):
    observed_daily_sharpe: float
    trial_daily_sharpe_stddev: float = Field(gt=0)
    n_return_observations: int = Field(gt=1)
    return_skewness: float
    return_pearson_kurtosis: float = Field(gt=0)
    n_effective_independent_trials: float = Field(gt=1)


class DsrFormulaResult(_StrictModel):
    expected_max_daily_sharpe_under_null: float
    deflated_sharpe_probability: float = Field(ge=0, le=1)
    one_sided_p_value: float = Field(ge=0, le=1)


class DeflatedSharpeAuditReport(_StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    audit_version: Literal["deflated-sharpe-audit-v1"] = AUDIT_VERSION
    audit_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    formula_id: Literal["bailey_lopez_de_prado_normal_max_sr_v1"]
    source_hashes: dict[str, str]
    source_report_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_daily_sharpe: float
    observed_annualized_sharpe: float
    n_return_observations: int = Field(gt=1)
    return_skewness: float
    return_pearson_kurtosis: float
    registered_trial_count_lower_bound: int = Field(gt=0)
    ledger_complete: Literal[False]
    trial_daily_sharpe_stddev: None = None
    n_effective_independent_trials: None = None
    status: Literal["not_evaluable"]
    missing_bindings: list[str]
    numeric_dsr: None = None
    one_sided_p_value: None = None
    consumed_oos_reused: Literal[False] = False
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @model_validator(mode="after")
    def _fail_closed(self) -> DeflatedSharpeAuditReport:
        required = {"trial_daily_sharpe_stddev", "n_effective_independent_trials"}
        if set(self.missing_bindings) != required:
            raise ValueError("current DSR audit must name both unbound multiplicity inputs")
        return self


def calculate_deflated_sharpe(inputs: DsrFormulaInputs) -> DsrFormulaResult:
    """Return the one-sided probability that Sharpe exceeds selection inflation.

    All Sharpe quantities use the same per-observation (daily) frequency.
    """
    normal = statistics.NormalDist()
    trials = inputs.n_effective_independent_trials
    gamma = 0.5772156649015329
    expected_max = inputs.trial_daily_sharpe_stddev * (
        (1.0 - gamma) * normal.inv_cdf(1.0 - 1.0 / trials) + gamma * normal.inv_cdf(1.0 - 1.0 / (trials * math.e))
    )
    variance_term = (
        1.0
        - inputs.return_skewness * inputs.observed_daily_sharpe
        + (inputs.return_pearson_kurtosis - 1.0) / 4.0 * inputs.observed_daily_sharpe**2
    )
    if variance_term <= 0 or not math.isfinite(variance_term):
        raise ValueError("Deflated Sharpe variance correction is non-positive")
    statistic = (
        (inputs.observed_daily_sharpe - expected_max)
        * math.sqrt(inputs.n_return_observations - 1.0)
        / math.sqrt(variance_term)
    )
    probability = normal.cdf(statistic)
    return DsrFormulaResult(
        expected_max_daily_sharpe_under_null=expected_max,
        deflated_sharpe_probability=probability,
        one_sided_p_value=1.0 - probability,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _id(report: DeflatedSharpeAuditReport) -> str:
    payload = report.model_dump(mode="json", exclude={"audit_id"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _moments(values: list[float]) -> tuple[float, float, float, float]:
    if len(values) < 3:
        raise ValueError("too few returns for moment binding")
    mean = statistics.fmean(values)
    std = statistics.stdev(values)
    if std <= 0:
        raise ValueError("return standard deviation is zero")
    centered = [(value - mean) / std for value in values]
    skew = statistics.fmean([value**3 for value in centered])
    kurtosis = statistics.fmean([value**4 for value in centered])
    return mean / std, mean / std * math.sqrt(TRADING_DAYS), skew, kurtosis


def build_deflated_sharpe_audit(*, repo_root: Path) -> DeflatedSharpeAuditReport:
    root = Path(repo_root).resolve(strict=True)
    recovery = verify_recovery_counterfactual_file(repo_root=root, report_path=RECOVERY_REPORT_PATH)
    _, ledger = verify_research_trial_ledger(ledger_path=root / DEFAULT_RESEARCH_TRIAL_LEDGER_PATH, repo_root=root)
    daily_path = root / recovery.daily_path
    frame = (
        pl.read_parquet(daily_path)
        .filter(pl.col("date").is_between(date(2013, 1, 1), date(2021, 12, 31)))
        .select("date", "base_equity")
        .sort("date")
    )
    equities = [float(value) for value in frame["base_equity"].to_list()]
    returns = [equities[i] / equities[i - 1] - 1.0 for i in range(1, len(equities))]
    daily_sharpe, annualized, skew, kurtosis = _moments(returns)
    report = DeflatedSharpeAuditReport(
        formula_id="bailey_lopez_de_prado_normal_max_sr_v1",
        source_hashes={
            "src/app/research/deflated_sharpe_audit.py": _sha256_file(Path(__file__)),
            RECOVERY_REPORT_PATH.as_posix(): _sha256_file(root / RECOVERY_REPORT_PATH),
            recovery.daily_path: _sha256_file(daily_path),
            DEFAULT_RESEARCH_TRIAL_LEDGER_PATH.as_posix(): _sha256_file(root / DEFAULT_RESEARCH_TRIAL_LEDGER_PATH),
        },
        source_report_id=recovery.report_id or "",
        ledger_id=ledger.ledger_id,
        observed_daily_sharpe=daily_sharpe,
        observed_annualized_sharpe=annualized,
        n_return_observations=len(returns),
        return_skewness=skew,
        return_pearson_kurtosis=kurtosis,
        registered_trial_count_lower_bound=ledger.trial_count,
        ledger_complete=False,
        status="not_evaluable",
        missing_bindings=[
            "n_effective_independent_trials",
            "trial_daily_sharpe_stddev",
        ],
    )
    return report.model_copy(update={"audit_id": _id(report)})


def write_deflated_sharpe_audit(*, repo_root: Path, output: Path = DEFAULT_OUTPUT_PATH) -> DeflatedSharpeAuditReport:
    root = Path(repo_root).resolve(strict=True)
    report = build_deflated_sharpe_audit(repo_root=root)
    destination = output if output.is_absolute() else root / output
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    temporary.replace(destination)
    return report


def verify_deflated_sharpe_audit_file(
    *, repo_root: Path, path: Path = DEFAULT_OUTPUT_PATH
) -> DeflatedSharpeAuditReport:
    root = Path(repo_root).resolve(strict=True)
    resolved = resolve_repo_regular_file(path, repo_root=root, field_name="audit_path")
    observed = DeflatedSharpeAuditReport.model_validate_json(resolved.read_text())
    if observed.audit_id != _id(observed):
        raise ValueError("Deflated Sharpe audit self-hash mismatch")
    expected = build_deflated_sharpe_audit(repo_root=root)
    if expected.model_dump(mode="json") != observed.model_dump(mode="json"):
        raise ValueError("Deflated Sharpe audit differs from full recomputation")
    return observed


__all__ = [
    "DEFAULT_OUTPUT_PATH",
    "DeflatedSharpeAuditReport",
    "DsrFormulaInputs",
    "DsrFormulaResult",
    "build_deflated_sharpe_audit",
    "calculate_deflated_sharpe",
    "verify_deflated_sharpe_audit_file",
    "write_deflated_sharpe_audit",
]
