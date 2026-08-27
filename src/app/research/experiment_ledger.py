from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RESEARCH_TRIAL_LEDGER_SCHEMA_VERSION: Literal["1"] = "1"
RESEARCH_TRIAL_LEDGER_VERSION: Literal["research-trial-ledger-v1"] = "research-trial-ledger-v1"
DEFAULT_RESEARCH_TRIAL_LEDGER_PATH = Path("config/research/research-trial-ledger-v1.json")

TrialStage = Literal[
    "engineering_record",
    "development",
    "validation",
    "holdout",
    "nomination",
    "oos",
]
TrialStatus = Literal[
    "recorded",
    "rejected",
    "no_go",
    "not_evaluable",
    "conditional_go",
    "passed_gate",
    "selected",
    "superseded",
]
DeclaredBeforeObservation = Literal["yes", "no", "unknown"]
ResultDirection = Literal["positive", "negative", "mixed", "none", "unknown"]
ResultStatus = Literal[
    "recorded",
    "rejected",
    "no_go",
    "not_evaluable",
    "conditional_go",
    "passed_gate",
    "selected",
    "inconclusive",
    "unknown",
]
OosReuseClaim = Literal["consumed_terminal", "not_applicable", "available"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DateWindow(_StrictModel):
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def _validate_window(self) -> DateWindow:
        if self.start is None and self.end is None:
            return self
        if self.start is None or self.end is None:
            raise ValueError("date window requires both start and end, or both null")
        if self.start > self.end:
            raise ValueError("date window start must be on or before end")
        return self


class ResearchTrial(_StrictModel):
    trial_id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    parent_trial_id: str | None = None
    hypothesis: str = Field(min_length=1)
    stage: TrialStage
    status: TrialStatus
    strategy_config_id: str | None = None
    config_hash: str | None = None
    data_snapshot_id: str | None = None
    development_window: DateWindow | None = None
    evaluation_window: DateWindow | None = None
    primary_endpoint: str | None = None
    result_direction: ResultDirection
    result_status: ResultStatus
    evidence_doc: str = Field(min_length=1)
    declared_before_observation: DeclaredBeforeObservation
    oos_consumed: bool
    freeze_id: str | None = None
    authorization_id: str | None = None
    freeze_path: str | None = None
    authorization_path: str | None = None
    receipt_path: str | None = None
    oos_reuse_claim: OosReuseClaim
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False

    @field_validator(
        "trial_id",
        "family_id",
        "hypothesis",
        "evidence_doc",
        mode="before",
    )
    @classmethod
    def _reject_blank_required_strings(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            raise ValueError("required string fields cannot be empty")
        return value

    @field_validator(
        "parent_trial_id",
        "strategy_config_id",
        "config_hash",
        "data_snapshot_id",
        "primary_endpoint",
        "freeze_id",
        "authorization_id",
        "freeze_path",
        "authorization_path",
        "receipt_path",
        mode="before",
    )
    @classmethod
    def _reject_unknown_masquerades(cls, value: object) -> object:
        if value is None:
            return None
        if value == 0 or value == 0.0:
            raise ValueError("unknown optional fields must be null, not zero")
        if isinstance(value, str) and value.strip() == "":
            raise ValueError("unknown optional fields must be null, not empty string")
        return value

    @model_validator(mode="after")
    def _validate_trial_consistency(self) -> ResearchTrial:
        if self.ready_for_scoring or self.ready_for_trading or self.auto_deploy:
            raise ValueError("research trials cannot authorize scoring, trading, or deploy")
        if self.oos_consumed:
            missing = [
                name
                for name, value in (
                    ("freeze_id", self.freeze_id),
                    ("authorization_id", self.authorization_id),
                    ("freeze_path", self.freeze_path),
                    ("authorization_path", self.authorization_path),
                    ("receipt_path", self.receipt_path),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    "oos_consumed trials require freeze_id, authorization_id, "
                    "freeze_path, authorization_path, and receipt_path"
                )
            if self.oos_reuse_claim != "consumed_terminal":
                raise ValueError("consumed OOS must be marked consumed_terminal, never reusable/clean")
            if self.stage not in {"oos", "holdout"}:
                raise ValueError("oos_consumed trials must use stage oos or holdout")
        elif self.oos_reuse_claim == "consumed_terminal":
            raise ValueError("oos_reuse_claim=consumed_terminal requires oos_consumed=true")
        if self.oos_reuse_claim == "available" and self.oos_consumed:
            raise ValueError("consumed OOS cannot be claimed available/reusable/clean")
        return self


class ResearchTrialLedger(_StrictModel):
    schema_version: Literal["1"] = RESEARCH_TRIAL_LEDGER_SCHEMA_VERSION
    ledger_version: Literal["research-trial-ledger-v1"] = RESEARCH_TRIAL_LEDGER_VERSION
    complete: bool
    historical_backfill: bool = False
    counting_notes: str | None = None
    trials: list[ResearchTrial]
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    ledger_id: str | None = None

    @model_validator(mode="after")
    def _validate_ledger_flags(self) -> ResearchTrialLedger:
        if self.ready_for_scoring or self.ready_for_trading or self.auto_deploy:
            raise ValueError("research trial ledger cannot authorize scoring, trading, or deploy")
        if self.complete and self.historical_backfill:
            raise ValueError("historical_backfill ledgers must set complete=false (lower bound only)")
        return self


class ResearchTrialLedgerSummary(_StrictModel):
    ledger_id: str
    complete: bool
    historical_backfill: bool
    trial_count: int = Field(ge=0)
    trial_count_is_lower_bound: bool
    counts_by_family: dict[str, int]
    counts_by_stage: dict[str, int]
    counts_by_status: dict[str, int]
    oos_consumed_count: int = Field(ge=0)
    declared_before_observation_yes: int = Field(ge=0)
    declared_before_observation_no: int = Field(ge=0)
    declared_before_observation_unknown: int = Field(ge=0)
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False


class DeflatedSharpeRatioInputs(_StrictModel):
    """Input contract only. Missing fields must stay null; never invent values."""

    observed_sharpe: float | None = None
    trial_sharpe_stddev: float | None = None
    n_return_observations: int | None = None
    return_skewness: float | None = None
    return_kurtosis: float | None = None
    n_effective_independent_trials: float | None = None

    @field_validator(
        "observed_sharpe",
        "trial_sharpe_stddev",
        "n_return_observations",
        "return_skewness",
        "return_kurtosis",
        "n_effective_independent_trials",
        mode="before",
    )
    @classmethod
    def _reject_empty_string_inputs(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            raise ValueError("DSR inputs must be null when unknown, not empty string")
        return value


class DeflatedSharpeRatioAssessment(_StrictModel):
    status: Literal["not_evaluable"] = "not_evaluable"
    reasons: list[str] = Field(min_length=1)
    deflated_sharpe: None = None
    p_value: None = None


def canonical_ledger_payload(ledger: ResearchTrialLedger) -> dict[str, Any]:
    return ledger.model_dump(mode="json", exclude={"ledger_id"})


def canonical_ledger_bytes(ledger: ResearchTrialLedger) -> bytes:
    payload = canonical_ledger_payload(ledger)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_ledger_id(ledger: ResearchTrialLedger) -> str:
    return hashlib.sha256(canonical_ledger_bytes(ledger)).hexdigest()


def seal_research_trial_ledger(ledger: ResearchTrialLedger) -> ResearchTrialLedger:
    return ledger.model_copy(update={"ledger_id": compute_ledger_id(ledger)})


def summarize_research_trial_ledger(ledger: ResearchTrialLedger) -> ResearchTrialLedgerSummary:
    if ledger.ledger_id is None:
        raise ValueError("ledger_id is required before summarization")
    by_family: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    by_status: dict[str, int] = {}
    declared_yes = 0
    declared_no = 0
    declared_unknown = 0
    oos_consumed = 0
    for trial in ledger.trials:
        by_family[trial.family_id] = by_family.get(trial.family_id, 0) + 1
        by_stage[trial.stage] = by_stage.get(trial.stage, 0) + 1
        by_status[trial.status] = by_status.get(trial.status, 0) + 1
        if trial.oos_consumed:
            oos_consumed += 1
        if trial.declared_before_observation == "yes":
            declared_yes += 1
        elif trial.declared_before_observation == "no":
            declared_no += 1
        else:
            declared_unknown += 1
    return ResearchTrialLedgerSummary(
        ledger_id=ledger.ledger_id,
        complete=ledger.complete,
        historical_backfill=ledger.historical_backfill,
        trial_count=len(ledger.trials),
        trial_count_is_lower_bound=not ledger.complete,
        counts_by_family=dict(sorted(by_family.items())),
        counts_by_stage=dict(sorted(by_stage.items())),
        counts_by_status=dict(sorted(by_status.items())),
        oos_consumed_count=oos_consumed,
        declared_before_observation_yes=declared_yes,
        declared_before_observation_no=declared_no,
        declared_before_observation_unknown=declared_unknown,
    )


def assess_deflated_sharpe_ratio(
    inputs: DeflatedSharpeRatioInputs,
) -> DeflatedSharpeRatioAssessment:
    """Fail closed: never invent a Deflated Sharpe number when inputs are incomplete."""
    reasons: list[str] = []
    required = {
        "observed_sharpe": inputs.observed_sharpe,
        "trial_sharpe_stddev": inputs.trial_sharpe_stddev,
        "n_return_observations": inputs.n_return_observations,
        "return_skewness": inputs.return_skewness,
        "return_kurtosis": inputs.return_kurtosis,
        "n_effective_independent_trials": inputs.n_effective_independent_trials,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        reasons.append("required Deflated Sharpe inputs are unbound: " + ", ".join(missing))
    else:
        reasons.append(
            "Deflated Sharpe numeric evaluation is not bound in this repository; "
            "inputs alone are insufficient without an audited formula binding"
        )
    reasons.append("raw Sharpe / single best trial results are not comparable without multiplicity adjustment")
    return DeflatedSharpeRatioAssessment(status="not_evaluable", reasons=reasons)


def load_research_trial_ledger(path: Path) -> ResearchTrialLedger:
    try:
        return ResearchTrialLedger.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("research trial ledger is missing or invalid") from exc


def verify_research_trial_ledger(
    *,
    ledger_path: Path,
    repo_root: Path | None = None,
) -> tuple[ResearchTrialLedger, ResearchTrialLedgerSummary]:
    root = (repo_root or Path.cwd()).resolve()
    ledger = load_research_trial_ledger(ledger_path)
    _assert_self_hash(ledger)
    _assert_trial_graph(ledger)
    _assert_paths(ledger, root)
    _assert_consumed_evidence_bindings(ledger, root)
    _assert_oos_consumption_rules(ledger)
    summary = summarize_research_trial_ledger(ledger)
    return ledger, summary


def write_research_trial_ledger(path: Path, ledger: ResearchTrialLedger) -> ResearchTrialLedger:
    sealed = seal_research_trial_ledger(ledger)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


def _assert_self_hash(ledger: ResearchTrialLedger) -> None:
    if ledger.ledger_id is None:
        raise ValueError("research trial ledger_id is missing")
    expected = compute_ledger_id(ledger)
    if ledger.ledger_id != expected:
        raise ValueError("research trial ledger_id does not match canonical content hash")


def _assert_trial_graph(ledger: ResearchTrialLedger) -> None:
    seen: dict[str, int] = {}
    for index, trial in enumerate(ledger.trials):
        if trial.trial_id in seen:
            raise ValueError(f"duplicate trial_id: {trial.trial_id}")
        seen[trial.trial_id] = index
    for index, trial in enumerate(ledger.trials):
        parent = trial.parent_trial_id
        if parent is None:
            continue
        if parent not in seen:
            raise ValueError(f"parent_trial_id does not exist: {parent}")
        if seen[parent] >= index:
            raise ValueError(f"parent_trial_id {parent} must appear earlier than child {trial.trial_id}")


def _assert_paths(ledger: ResearchTrialLedger, repo_root: Path) -> None:
    for trial in ledger.trials:
        _assert_repo_relative_existing_file(trial.evidence_doc, repo_root, "evidence_doc")
        if trial.freeze_path is not None:
            _assert_repo_relative_existing_file(trial.freeze_path, repo_root, "freeze_path")
        if trial.authorization_path is not None:
            _assert_repo_relative_existing_file(trial.authorization_path, repo_root, "authorization_path")
        if trial.receipt_path is not None:
            _assert_repo_relative_existing_file(trial.receipt_path, repo_root, "receipt_path")


def _assert_repo_relative_existing_file(value: str, repo_root: Path, label: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError(f"{label} must be a relative path without parent traversal")
    resolved = (repo_root / path).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes repository root") from exc
    if not resolved.is_file():
        raise ValueError(f"{label} does not exist: {value}")


def _assert_consumed_evidence_bindings(ledger: ResearchTrialLedger, repo_root: Path) -> None:
    for trial in ledger.trials:
        if not trial.oos_consumed:
            continue
        assert trial.freeze_id is not None
        assert trial.authorization_id is not None
        assert trial.freeze_path is not None
        assert trial.authorization_path is not None
        assert trial.receipt_path is not None
        freeze_payload = _load_evidence_json_object(repo_root / trial.freeze_path, "freeze_path")
        authorization_payload = _load_evidence_json_object(repo_root / trial.authorization_path, "authorization_path")
        receipt_payload = _load_evidence_json_object(repo_root / trial.receipt_path, "receipt_path")

        freeze_id = _require_evidence_string(freeze_payload, "freeze_id", "freeze_path")
        if freeze_id != trial.freeze_id:
            raise ValueError(f"freeze_path freeze_id does not match trial freeze_id for {trial.trial_id}")

        auth_id = _require_evidence_string(authorization_payload, "authorization_id", "authorization_path")
        auth_freeze_id = _require_evidence_string(authorization_payload, "freeze_id", "authorization_path")
        if auth_id != trial.authorization_id:
            raise ValueError(
                f"authorization_path authorization_id does not match trial authorization_id for {trial.trial_id}"
            )
        if auth_freeze_id != trial.freeze_id:
            raise ValueError(f"authorization_path freeze_id does not match trial freeze_id for {trial.trial_id}")

        receipt_auth_id = _require_evidence_string(receipt_payload, "authorization_id", "receipt_path")
        if receipt_auth_id != trial.authorization_id:
            raise ValueError(
                f"receipt_path authorization_id does not match trial authorization_id for {trial.trial_id}"
            )
        if "freeze_id" in receipt_payload:
            receipt_freeze_id = _require_evidence_string(receipt_payload, "freeze_id", "receipt_path")
            if receipt_freeze_id != trial.freeze_id:
                raise ValueError(f"receipt_path freeze_id does not match trial freeze_id for {trial.trial_id}")

        _assert_one_shot_true(authorization_payload, "authorization_path")
        _assert_one_shot_true(receipt_payload, "receipt_path")
        _assert_ready_flags_false_if_present(authorization_payload, "authorization_path")
        _assert_ready_flags_false_if_present(receipt_payload, "receipt_path")


def _load_evidence_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _require_evidence_string(payload: dict[str, Any], field: str, label: str) -> str:
    if field not in payload:
        raise ValueError(f"{label} is missing required field {field}")
    value = payload[field]
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{label} field {field} must be a non-empty string")
    return value


def _assert_one_shot_true(payload: dict[str, Any], label: str) -> None:
    if "one_shot" not in payload:
        raise ValueError(f"{label} is missing required field one_shot")
    if payload["one_shot"] is not True:
        raise ValueError(f"{label} one_shot must be true")


def _assert_ready_flags_false_if_present(payload: dict[str, Any], label: str) -> None:
    for field in ("ready_for_scoring", "ready_for_trading", "auto_deploy"):
        if field in payload and payload[field] is not False:
            raise ValueError(f"{label} {field} must be false when present")


def _assert_oos_consumption_rules(ledger: ResearchTrialLedger) -> None:
    consumed_freeze_ids: dict[str, str] = {}
    for trial in ledger.trials:
        if not trial.oos_consumed:
            continue
        assert trial.freeze_id is not None
        if trial.freeze_id in consumed_freeze_ids:
            raise ValueError(
                f"freeze_id {trial.freeze_id} consumed more than once "
                f"({consumed_freeze_ids[trial.freeze_id]} and {trial.trial_id})"
            )
        consumed_freeze_ids[trial.freeze_id] = trial.trial_id
        if trial.oos_reuse_claim != "consumed_terminal":
            raise ValueError("consumed OOS must remain a terminal non-reusable state")
        if trial.oos_reuse_claim == "available":
            raise ValueError("consumed OOS cannot be labeled available/reusable/clean")

    for trial in ledger.trials:
        if trial.oos_consumed:
            continue
        if (
            trial.oos_reuse_claim == "available"
            and trial.freeze_id is not None
            and trial.freeze_id in consumed_freeze_ids
        ):
            raise ValueError(f"freeze_id {trial.freeze_id} already consumed; cannot claim available/clean OOS")


__all__ = [
    "DEFAULT_RESEARCH_TRIAL_LEDGER_PATH",
    "RESEARCH_TRIAL_LEDGER_SCHEMA_VERSION",
    "RESEARCH_TRIAL_LEDGER_VERSION",
    "DateWindow",
    "DeflatedSharpeRatioAssessment",
    "DeflatedSharpeRatioInputs",
    "ResearchTrial",
    "ResearchTrialLedger",
    "ResearchTrialLedgerSummary",
    "assess_deflated_sharpe_ratio",
    "canonical_ledger_bytes",
    "canonical_ledger_payload",
    "compute_ledger_id",
    "load_research_trial_ledger",
    "seal_research_trial_ledger",
    "summarize_research_trial_ledger",
    "verify_research_trial_ledger",
    "write_research_trial_ledger",
]
