"""E11b-2d: Frozen response-boundary policy for FN collection.

This policy is narrowly scoped to two allowed quarantine cases:
- balancesheet/income: ann_date is in-window and f_ann_date is future
- fina_indicator/fina_audit: report-period queries return a future ann_date

In that case, effective disclosure date is outside the authorized window, so the
row must be excluded from PIT parquet and emitted as a sealed quarantine receipt.
All other unsupported future-date or malformed shapes fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.layer_two_financial_negative_list_data_protocol import (
    ANNOUNCEMENT_COLLECTION_END,
    ANNOUNCEMENT_COLLECTION_START,
    PROTOCOL_FILE_PATH,
    PROTOCOL_ID,
)
from app.research.repo_file_safety import resolve_repo_regular_file

POLICY_FILE_PATH_V1: Literal["config/research/financial-negative-list-response-boundary-policy-v1.json"] = (
    "config/research/financial-negative-list-response-boundary-policy-v1.json"
)
POLICY_FILE_PATH_V2: Literal["config/research/financial-negative-list-response-boundary-policy-v2.json"] = (
    "config/research/financial-negative-list-response-boundary-policy-v2.json"
)
# v3 is an offline finalization policy.  The collection-time default remains v2
# so an already completed network request cannot be silently rebound.
POLICY_FILE_PATH_V3: Literal["config/research/financial-negative-list-response-boundary-policy-v3.json"] = (
    "config/research/financial-negative-list-response-boundary-policy-v3.json"
)
POLICY_FILE_PATH = POLICY_FILE_PATH_V2
POLICY_SCHEMA_VERSION: Literal["1"] = "1"
POLICY_VERSION_V1: Literal["financial-negative-list-response-boundary-policy-v1"] = (
    "financial-negative-list-response-boundary-policy-v1"
)
POLICY_VERSION_V2: Literal["financial-negative-list-response-boundary-policy-v2"] = (
    "financial-negative-list-response-boundary-policy-v2"
)
POLICY_VERSION_V3: Literal["financial-negative-list-response-boundary-policy-v3"] = (
    "financial-negative-list-response-boundary-policy-v3"
)
POLICY_VERSION = POLICY_VERSION_V2
POLICY_STATUS: Literal["frozen_for_development"] = "frozen_for_development"
POLICY_REASON_CODE: Literal["FNLD-013"] = "FNLD-013"
BOUND_BASE_PROTOCOL_PATH: Literal["config/research/layer-two-financial-negative-list-data-protocol-v1.json"] = (
    "config/research/layer-two-financial-negative-list-data-protocol-v1.json"
)
BOUND_BASE_PROTOCOL_ID: Literal["314e9d644b897ed4398cc349e3772b09bbe6f80cfd2d518a7cdbf19bb651d2ea"] = (
    "314e9d644b897ed4398cc349e3772b09bbe6f80cfd2d518a7cdbf19bb651d2ea"
)
BOUND_BASE_PROTOCOL_FILE_SHA256: Literal["b3e8310e158979fc0f1d22fd5122d6ca0cf694908d78d3ef96af3c1fec6d72c2"] = (
    "b3e8310e158979fc0f1d22fd5122d6ca0cf694908d78d3ef96af3c1fec6d72c2"
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_QUARANTINE_CASE_V1 = (
    "ann_date valid and in-window; end_date <= 2024-12-31; f_ann_date valid and > 2024-12-31; "
    "effective_disclosure_date=max(ann_date,f_ann_date) therefore outside authorized window"
)
_ALLOWED_QUARANTINE_CASE_V2 = (
    "case_a: endpoint in {balancesheet,income}; ann_date valid and in-window; end_date <= 2024-12-31; "
    "f_ann_date valid and > 2024-12-31; effective_disclosure_date=f_ann_date; "
    "case_b: endpoint in {fina_indicator,fina_audit}; ann_date valid and > 2024-12-31; "
    "end_date <= 2024-12-31; f_ann_date absent; effective_disclosure_date=ann_date"
)
_ALLOWED_QUARANTINE_CASE_V3 = (
    _ALLOWED_QUARANTINE_CASE_V2
    + "; case_a metadata requires integer report_type/comp_type and update_flag in {0,1}; "
    "end_type must be an integer when present and may be null only when the source row omits it"
)


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResponseBoundaryPolicy(_StrictFrozen):
    schema_version: Literal["1"]
    policy_version: Literal[
        "financial-negative-list-response-boundary-policy-v1",
        "financial-negative-list-response-boundary-policy-v2",
        "financial-negative-list-response-boundary-policy-v3",
    ]
    status: Literal["frozen_for_development"]
    purpose: str = Field(min_length=1)
    bound_base_protocol_path: str
    bound_base_protocol_id: str = Field(min_length=64, max_length=64)
    bound_base_protocol_file_sha256: str = Field(min_length=64, max_length=64)
    announcement_window_start: str
    announcement_window_end: str
    allowed_quarantine_case: str = Field(min_length=1)
    quarantine_reason_code: str = Field(min_length=1)
    unsupported_shapes_fail_closed: bool
    exclude_quarantined_rows_from_pit: bool
    preserve_source_row_hash_for_quarantine_receipt: bool
    do_not_persist_future_payload_values: bool
    ready_for_scoring: Literal[False]
    ready_for_backtest: Literal[False]
    ready_for_trading: Literal[False]
    policy_id: str

    @model_validator(mode="after")
    def _strict_bindings(self) -> ResponseBoundaryPolicy:
        if self.bound_base_protocol_path != BOUND_BASE_PROTOCOL_PATH:
            raise ValueError("bound_base_protocol_path mismatch")
        if self.bound_base_protocol_id != BOUND_BASE_PROTOCOL_ID:
            raise ValueError("bound_base_protocol_id mismatch")
        if self.bound_base_protocol_file_sha256 != BOUND_BASE_PROTOCOL_FILE_SHA256:
            raise ValueError("bound_base_protocol_file_sha256 mismatch")
        if self.announcement_window_start != ANNOUNCEMENT_COLLECTION_START.isoformat():
            raise ValueError("announcement_window_start mismatch")
        if self.announcement_window_end != ANNOUNCEMENT_COLLECTION_END.isoformat():
            raise ValueError("announcement_window_end mismatch")
        if self.quarantine_reason_code != POLICY_REASON_CODE:
            raise ValueError("quarantine_reason_code mismatch")
        expected_case = {
            POLICY_VERSION_V1: _ALLOWED_QUARANTINE_CASE_V1,
            POLICY_VERSION_V2: _ALLOWED_QUARANTINE_CASE_V2,
            POLICY_VERSION_V3: _ALLOWED_QUARANTINE_CASE_V3,
        }[self.policy_version]
        if self.allowed_quarantine_case != expected_case:
            raise ValueError("allowed_quarantine_case mismatch")
        if (
            not self.unsupported_shapes_fail_closed
            or not self.exclude_quarantined_rows_from_pit
            or not self.preserve_source_row_hash_for_quarantine_receipt
            or not self.do_not_persist_future_payload_values
        ):
            raise ValueError("response-boundary fail-closed semantics mismatch")
        return self


@dataclass(frozen=True)
class ResponseBoundaryPolicyVerificationResult:
    policy_id: str
    policy_version: str
    policy_file_sha256: str
    quarantine_reason_code: str
    bound_base_protocol_id: str
    bound_base_protocol_file_sha256: str


@dataclass(frozen=True)
class ResponseBoundaryDecision:
    action: Literal["allow", "quarantine", "reject"]
    effective_disclosure_date: date | None
    reason_code: str | None = None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_policy_id(payload: dict[str, Any]) -> str:
    data = {k: v for k, v in payload.items() if k != "policy_id"}
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_policy(path: Path) -> ResponseBoundaryPolicy:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return ResponseBoundaryPolicy.model_validate(payload)


def verify_policy_file(
    *,
    repo_root: Path,
    policy_path: Path = Path(POLICY_FILE_PATH),
) -> tuple[ResponseBoundaryPolicy, ResponseBoundaryPolicyVerificationResult]:
    root = Path(repo_root).resolve()
    full_path = resolve_repo_regular_file(
        Path(policy_path),
        repo_root=root,
        field_name="response-boundary policy file",
    )

    payload = json.loads(full_path.read_text(encoding="utf-8"))
    stored_policy_id = payload.get("policy_id")
    if not isinstance(stored_policy_id, str) or _HEX64_RE.fullmatch(stored_policy_id) is None:
        raise ValueError("policy_id must be 64-char lowercase hex")
    computed_policy_id = compute_policy_id(payload)
    if computed_policy_id != stored_policy_id:
        raise ValueError("response-boundary policy_id self-seal mismatch")

    if BOUND_BASE_PROTOCOL_ID != PROTOCOL_ID or BOUND_BASE_PROTOCOL_PATH != PROTOCOL_FILE_PATH:
        raise ValueError("bound base protocol constants drift")
    bound_protocol_path = resolve_repo_regular_file(
        Path(BOUND_BASE_PROTOCOL_PATH),
        repo_root=root,
        field_name="bound base protocol file",
    )
    if _sha256_file(bound_protocol_path) != BOUND_BASE_PROTOCOL_FILE_SHA256:
        raise ValueError("bound base protocol file sha256 drift")

    model = ResponseBoundaryPolicy.model_validate(payload)
    return (
        model,
        ResponseBoundaryPolicyVerificationResult(
            policy_id=model.policy_id,
            policy_version=model.policy_version,
            policy_file_sha256=_sha256_file(full_path),
            quarantine_reason_code=model.quarantine_reason_code,
            bound_base_protocol_id=model.bound_base_protocol_id,
            bound_base_protocol_file_sha256=model.bound_base_protocol_file_sha256,
        ),
    )


def evaluate_response_boundary(
    *,
    endpoint: str,
    ann_date: date | None,
    f_ann_date: date | None,
    end_date: date,
    policy_version: str = POLICY_VERSION,
) -> ResponseBoundaryDecision:
    """Apply the frozen narrow response-boundary decision.

    Caller must already parse dates strictly. This function only encodes the
    allowed quarantine case and otherwise leaves rows to normal in-window logic.
    """
    if end_date > ANNOUNCEMENT_COLLECTION_END:
        return ResponseBoundaryDecision(action="reject", effective_disclosure_date=None, reason_code="end_date_oos")
    if endpoint not in {"balancesheet", "income", "fina_indicator", "fina_audit"}:
        return ResponseBoundaryDecision(
            action="reject", effective_disclosure_date=None, reason_code="endpoint_unsupported"
        )
    if policy_version not in {POLICY_VERSION_V1, POLICY_VERSION_V2, POLICY_VERSION_V3}:
        return ResponseBoundaryDecision(
            action="reject", effective_disclosure_date=None, reason_code="policy_unsupported"
        )
    if ann_date is None:
        if f_ann_date is not None and f_ann_date > ANNOUNCEMENT_COLLECTION_END:
            return ResponseBoundaryDecision(
                action="reject", effective_disclosure_date=None, reason_code="future_f_ann_without_ann"
            )
        return ResponseBoundaryDecision(action="allow", effective_disclosure_date=None)
    if ann_date < ANNOUNCEMENT_COLLECTION_START:
        return ResponseBoundaryDecision(action="reject", effective_disclosure_date=None, reason_code="ann_date_oos")
    if ann_date > ANNOUNCEMENT_COLLECTION_END:
        if policy_version in {POLICY_VERSION_V2, POLICY_VERSION_V3} and endpoint in {
            "fina_indicator",
            "fina_audit",
        } and f_ann_date is None:
            return ResponseBoundaryDecision(
                action="quarantine",
                effective_disclosure_date=ann_date,
                reason_code=POLICY_REASON_CODE,
            )
        return ResponseBoundaryDecision(action="reject", effective_disclosure_date=None, reason_code="ann_date_oos")
    if f_ann_date is None:
        return ResponseBoundaryDecision(action="allow", effective_disclosure_date=ann_date)
    if f_ann_date <= ANNOUNCEMENT_COLLECTION_END:
        return ResponseBoundaryDecision(action="allow", effective_disclosure_date=max(ann_date, f_ann_date))
    if endpoint not in {"balancesheet", "income"}:
        return ResponseBoundaryDecision(
            action="reject", effective_disclosure_date=None, reason_code="future_f_ann_endpoint"
        )
    return ResponseBoundaryDecision(
        action="quarantine",
        effective_disclosure_date=f_ann_date,
        reason_code=POLICY_REASON_CODE,
    )


__all__ = [
    "BOUND_BASE_PROTOCOL_FILE_SHA256",
    "BOUND_BASE_PROTOCOL_ID",
    "BOUND_BASE_PROTOCOL_PATH",
    "POLICY_FILE_PATH",
    "POLICY_FILE_PATH_V1",
    "POLICY_FILE_PATH_V2",
    "POLICY_FILE_PATH_V3",
    "POLICY_REASON_CODE",
    "POLICY_SCHEMA_VERSION",
    "POLICY_STATUS",
    "POLICY_VERSION",
    "POLICY_VERSION_V1",
    "POLICY_VERSION_V2",
    "POLICY_VERSION_V3",
    "ResponseBoundaryDecision",
    "ResponseBoundaryPolicy",
    "ResponseBoundaryPolicyVerificationResult",
    "compute_policy_id",
    "evaluate_response_boundary",
    "load_policy",
    "verify_policy_file",
]
