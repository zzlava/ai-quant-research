"""Verifier for the frozen plan-level research stop rule."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.repo_file_safety import resolve_repo_regular_file

DEFAULT_PATH = Path("config/research/research-plan-stop-rule-v1.json")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactBinding(_StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: str


class ResearchPlanStopRule(_StrictModel):
    schema_version: Literal["1"]
    contract_version: Literal["research-plan-stop-rule-v1"]
    contract_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_as_of: date
    source_bindings: dict[str, ArtifactBinding]
    round_outcome: Literal["no_detectable_deployable_advantage"]
    deployment_decision: Literal["no_go"]
    architecture_target: str
    architecture_current_status: str
    individual_stock_alpha_moratorium: dict[str, Any]
    permitted_work: list[str]
    reopen_conditions: dict[str, bool]
    capital_unlock: dict[str, bool]
    readiness: dict[str, bool]

    @model_validator(mode="after")
    def _fail_closed(self) -> ResearchPlanStopRule:
        if self.confirmation_as_of != date(2026, 8, 27):
            raise ValueError("stop-rule confirmation date drifted")
        if self.individual_stock_alpha_moratorium.get("ends_not_before") != "2028-08-27":
            raise ValueError("alpha moratorium end drifted")
        if self.reopen_conditions.get("prominent_manual_user_confirmation_required") is not True:
            raise ValueError("prominent manual confirmation is required")
        if self.reopen_conditions.get("automatic_restart_forbidden") is not True:
            raise ValueError("automatic restart must remain forbidden")
        authorization_keys = (
            "thirty_percent_controlled_trial_authorized",
            "sixty_percent_authorized",
            "ninety_percent_authorized",
        )
        if any(self.capital_unlock.get(key) is not False for key in authorization_keys):
            raise ValueError("capital unlock authorizations must remain false")
        if (
            self.capital_unlock.get("manual_unlock_cannot_override_failed_research_gate_without_new_protocol")
            is not True
        ):
            raise ValueError("manual unlock must not bypass the failed research gate")
        if any(self.readiness.values()):
            raise ValueError("stop rule cannot authorize downstream use")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contract_id(contract: ResearchPlanStopRule) -> str:
    payload = contract.model_dump(mode="json", exclude={"contract_id"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def verify_research_plan_stop_rule(*, repo_root: Path, path: Path = DEFAULT_PATH) -> ResearchPlanStopRule:
    root = Path(repo_root).resolve(strict=True)
    resolved = resolve_repo_regular_file(path, repo_root=root, field_name="stop_rule_path")
    try:
        contract = ResearchPlanStopRule.model_validate_json(resolved.read_text())
    except Exception as exc:
        raise ValueError("research plan stop rule is missing or invalid") from exc
    if contract.contract_id != _contract_id(contract):
        raise ValueError("research plan stop rule self-hash mismatch")
    for name, binding in contract.source_bindings.items():
        source = resolve_repo_regular_file(
            Path(binding.path), repo_root=root, field_name=f"source_bindings.{name}.path"
        )
        if _sha256_file(source) != binding.sha256:
            raise ValueError(f"research plan source hash mismatch: {name}")
        payload = json.loads(source.read_text())
        ids = {
            payload.get("report_id"),
            payload.get("review_id"),
            payload.get("audit_id"),
            payload.get("ledger_id"),
        }
        if binding.artifact_id not in ids:
            raise ValueError(f"research plan source artifact ID mismatch: {name}")
    return contract


__all__ = ["DEFAULT_PATH", "ResearchPlanStopRule", "verify_research_plan_stop_rule"]
