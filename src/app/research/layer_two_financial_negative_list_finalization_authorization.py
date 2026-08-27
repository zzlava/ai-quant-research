"""Offline authorization for finalizing an already collected FN dataset.

This gate cannot authorize network access.  It binds one existing collection
request to response-boundary policy v3 and only permits source-omitted
``end_type`` metadata to remain null in balancesheet/income receipts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_financial_negative_list_response_boundary_policy import (
    POLICY_FILE_PATH_V3,
    POLICY_VERSION_V3,
    verify_policy_file,
)
from app.research.repo_file_safety import resolve_repo_regular_file

FINALIZATION_AUTHORIZATION_PATH = Path(
    "config/research/financial-negative-list-finalization-authorization-20260827-v1.json"
)
FINALIZATION_AUTHORIZATION_VERSION: Literal[
    "financial-negative-list-finalization-authorization-v1"
] = "financial-negative-list-finalization-authorization-v1"
FINALIZATION_ORIGINAL_REQUEST_ID: Literal[
    "465f7d74a30a463b67746134430b629ec7d6b7d4c181c76c4fad95c8675aa75f"
] = "465f7d74a30a463b67746134430b629ec7d6b7d4c181c76c4fad95c8675aa75f"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FinancialNegativeListFinalizationAuthorization(_StrictFrozen):
    schema_version: Literal["1"]
    authorization_version: Literal["financial-negative-list-finalization-authorization-v1"]
    authorization_scope: Literal["offline_response_boundary_finalization_only"]
    original_request_id: str = Field(min_length=64, max_length=64)
    original_collection_authorization_id: str = Field(min_length=64, max_length=64)
    original_run_contract_id: str = Field(min_length=64, max_length=64)
    original_response_boundary_policy_id: str = Field(min_length=64, max_length=64)
    finalization_policy_path: Literal[
        "config/research/financial-negative-list-response-boundary-policy-v3.json"
    ]
    finalization_policy_id: str = Field(min_length=64, max_length=64)
    finalization_policy_file_sha256: str = Field(min_length=64, max_length=64)
    nullable_end_type_endpoints: tuple[Literal["balancesheet"], Literal["income"]]
    requires_original_receipt_source_row_hash: Literal[True]
    does_not_rewrite_receipts: Literal[True]
    network_access_allowed: Literal[False]
    future_payload_values_allowed: Literal[False]
    ready_for_scoring: Literal[False]
    ready_for_backtest: Literal[False]
    ready_for_trading: Literal[False]
    user_authorization_phrase: str = Field(min_length=1)
    authorization_date: str
    authorization_time: str
    authorization_timezone: Literal["Asia/Shanghai"]
    authorization_id: str = Field(min_length=64, max_length=64)

    @field_validator(
        "original_request_id",
        "original_collection_authorization_id",
        "original_run_contract_id",
        "original_response_boundary_policy_id",
        "finalization_policy_id",
        "finalization_policy_file_sha256",
        "authorization_id",
        mode="before",
    )
    @classmethod
    def _hex64(cls, value: object) -> str:
        if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
            raise ValueError("must be 64-char lowercase hex")
        return value

    @model_validator(mode="after")
    def _strict_scope(self) -> FinancialNegativeListFinalizationAuthorization:
        if self.original_request_id != FINALIZATION_ORIGINAL_REQUEST_ID:
            raise ValueError("original_request_id must match the frozen finalization target")
        if self.nullable_end_type_endpoints != ("balancesheet", "income"):
            raise ValueError("nullable_end_type_endpoints must be exactly balancesheet,income")
        date.fromisoformat(self.authorization_date)
        if not re.fullmatch(r"^\d{2}:\d{2}:\d{2}$", self.authorization_time):
            raise ValueError("authorization_time must be HH:MM:SS")
        return self


@dataclass(frozen=True)
class FinalizationAuthorizationVerificationResult:
    authorization_id: str
    authorization_file_sha256: str
    policy_id: str
    policy_file_sha256: str
    policy_path: str
    nullable_end_type_endpoints: tuple[str, str]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_finalization_authorization_id(payload: dict[str, Any]) -> str:
    canonical = {key: value for key, value in payload.items() if key != "authorization_id"}
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def verify_finalization_authorization_file(
    *,
    repo_root: Path,
    request: dict[str, Any],
    authorization_path: Path = FINALIZATION_AUTHORIZATION_PATH,
) -> tuple[
    FinancialNegativeListFinalizationAuthorization,
    FinalizationAuthorizationVerificationResult,
]:
    root = Path(repo_root).resolve()
    auth_path = resolve_repo_regular_file(
        authorization_path,
        repo_root=root,
        field_name="financial-negative-list finalization authorization",
    )
    payload = json.loads(auth_path.read_text(encoding="utf-8"))
    model = FinancialNegativeListFinalizationAuthorization.model_validate(payload)
    if compute_finalization_authorization_id(payload) != model.authorization_id:
        raise ValueError("finalization authorization_id self-seal mismatch")
    expected_request_bindings = {
        "original_request_id": request.get("request_id"),
        "original_collection_authorization_id": request.get("collection_authorization_id"),
        "original_run_contract_id": request.get("verified_run_contract_id"),
        "original_response_boundary_policy_id": request.get("verified_response_boundary_policy_id"),
    }
    for field_name, expected in expected_request_bindings.items():
        if getattr(model, field_name) != expected:
            raise ValueError(f"finalization authorization {field_name} mismatch")
    policy, policy_result = verify_policy_file(
        repo_root=root,
        policy_path=Path(POLICY_FILE_PATH_V3),
    )
    if policy.policy_version != POLICY_VERSION_V3:
        raise ValueError("finalization policy version mismatch")
    if model.finalization_policy_path != POLICY_FILE_PATH_V3:
        raise ValueError("finalization policy path mismatch")
    if model.finalization_policy_id != policy.policy_id:
        raise ValueError("finalization policy id mismatch")
    if model.finalization_policy_file_sha256 != policy_result.policy_file_sha256:
        raise ValueError("finalization policy file sha256 mismatch")
    return model, FinalizationAuthorizationVerificationResult(
        authorization_id=model.authorization_id,
        authorization_file_sha256=_sha256_file(auth_path),
        policy_id=policy.policy_id,
        policy_file_sha256=policy_result.policy_file_sha256,
        policy_path=POLICY_FILE_PATH_V3,
        nullable_end_type_endpoints=model.nullable_end_type_endpoints,
    )


__all__ = [
    "FINALIZATION_AUTHORIZATION_PATH",
    "FINALIZATION_ORIGINAL_REQUEST_ID",
    "FINALIZATION_AUTHORIZATION_VERSION",
    "FinancialNegativeListFinalizationAuthorization",
    "FinalizationAuthorizationVerificationResult",
    "compute_finalization_authorization_id",
    "verify_finalization_authorization_file",
]
