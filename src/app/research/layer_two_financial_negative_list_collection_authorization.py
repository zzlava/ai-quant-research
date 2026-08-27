"""E11b-2c: Explicit authorization gate for FN list collection.

Strict offline authorization model + verifier. This artifact authorizes only
historical data collection for a specific prepared run-contract binding and is
never a trading/scoring/backtest authorization.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_financial_negative_list_collection_run_contract import (
    DEFAULT_RUN_CONTRACT_PATH,
    FinancialNegativeListCollectionRunContract,
    RunContractVerificationResult,
    verify_run_contract_file,
)
from app.research.layer_two_financial_negative_list_data_protocol import SOURCE_ENDPOINTS
from app.research.repo_file_safety import resolve_repo_regular_file

AUTHORIZATION_SCHEMA_VERSION: Literal["1"] = "1"
AUTHORIZATION_VERSION: Literal["financial-negative-list-collection-authorization-v1"] = (
    "financial-negative-list-collection-authorization-v1"
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_SH_TZ = ZoneInfo("Asia/Shanghai")
_AUTH_TIME_FUTURE_SKEW = timedelta(minutes=2)


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FinancialNegativeListCollectionAuthorization(_StrictFrozen):
    schema_version: Literal["1"]
    authorization_version: Literal["financial-negative-list-collection-authorization-v1"]
    authorization_scope: Literal["historical_financial_negative_list_collection_only"]
    not_trading_authorization: Literal[True]
    allows_resume_until_collection_manifest_complete: Literal[True]
    run_contract_id: str = Field(min_length=64, max_length=64)
    protocol_id: str = Field(min_length=64, max_length=64)
    protocol_file_sha256: str = Field(min_length=64, max_length=64)
    staging_dir: str = Field(min_length=1)
    canonical_symbol_count: int = Field(ge=1)
    canonical_symbols_sha256: str = Field(min_length=64, max_length=64)
    expected_partition_count: int = Field(ge=1)
    source_endpoints: tuple[str, str, str, str]
    user_authorization_phrase: str = Field(min_length=1)
    authorization_date: str
    authorization_time: str
    authorization_timezone: Literal["Asia/Shanghai"]
    network_collection_allowed: Literal[True]
    ready_for_scoring: Literal[False]
    ready_for_backtest: Literal[False]
    ready_for_trading: Literal[False]
    authorization_id: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator(
        "run_contract_id",
        "protocol_id",
        "protocol_file_sha256",
        "canonical_symbols_sha256",
        mode="before",
    )
    @classmethod
    def _hex64(cls, value: object) -> str:
        if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
            raise ValueError("must be 64-char lowercase hex")
        return value

    @field_validator("authorization_id", mode="before")
    @classmethod
    def _auth_id_hex(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
            raise ValueError("authorization_id must be 64-char lowercase hex")
        return value

    @field_validator("authorization_date", mode="before")
    @classmethod
    def _valid_date(cls, value: object) -> str:
        if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
            raise ValueError("authorization_date must be YYYY-MM-DD")
        date.fromisoformat(value)
        return value

    @field_validator("authorization_time", mode="before")
    @classmethod
    def _valid_time(cls, value: object) -> str:
        if not isinstance(value, str) or _TIME_RE.fullmatch(value) is None:
            raise ValueError("authorization_time must be HH:MM:SS")
        time.fromisoformat(value)
        return value

    @field_validator("user_authorization_phrase", mode="before")
    @classmethod
    def _non_blank_phrase(cls, value: object) -> str:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("user_authorization_phrase must be non-empty")
        return value

    @model_validator(mode="after")
    def _endpoint_and_count_semantics(self) -> FinancialNegativeListCollectionAuthorization:
        if self.source_endpoints != SOURCE_ENDPOINTS:
            raise ValueError("source_endpoints must exactly match four frozen endpoints")
        if self.expected_partition_count != self.canonical_symbol_count * len(SOURCE_ENDPOINTS):
            raise ValueError("expected_partition_count must equal symbol_count * endpoint_count")
        return self


@dataclass(frozen=True)
class AuthorizationVerificationResult:
    authorization_id: str
    run_contract_id: str
    run_contract_version: str
    protocol_id: str
    staging_dir: str
    response_boundary_policy_id: str | None
    response_boundary_policy_file_sha256: str | None
    response_boundary_reason_code: str | None
    network_collection_allowed: bool
    ready_for_scoring: bool
    ready_for_backtest: bool
    ready_for_trading: bool


def _canonical_sha(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def canonical_authorization_payload(payload: FinancialNegativeListCollectionAuthorization) -> dict[str, Any]:
    return payload.model_dump(mode="json", exclude={"authorization_id"})


def compute_authorization_id(payload: FinancialNegativeListCollectionAuthorization) -> str:
    return _canonical_sha(canonical_authorization_payload(payload))


def assert_authorization_self_hash(payload: FinancialNegativeListCollectionAuthorization) -> None:
    if payload.authorization_id is None:
        raise ValueError("authorization_id is missing")
    expected = compute_authorization_id(payload)
    if payload.authorization_id != expected:
        raise ValueError("authorization_id self-seal mismatch")


def load_collection_authorization(path: Path) -> FinancialNegativeListCollectionAuthorization:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return FinancialNegativeListCollectionAuthorization.model_validate(data)


def _assert_matches_run_contract(
    authorization: FinancialNegativeListCollectionAuthorization,
    run_contract: FinancialNegativeListCollectionRunContract,
) -> None:
    if authorization.run_contract_id != run_contract.run_contract_id:
        raise ValueError("authorization run_contract_id mismatch")
    if authorization.protocol_id != run_contract.e11b_2a_protocol_id:
        raise ValueError("authorization protocol_id mismatch")
    if authorization.protocol_file_sha256 != run_contract.e11b_2a_protocol_file_sha256:
        raise ValueError("authorization protocol_file_sha256 mismatch")
    if authorization.staging_dir != run_contract.fixed_staging_dir:
        raise ValueError("authorization staging_dir mismatch")
    if authorization.canonical_symbol_count != run_contract.canonical_symbol_count:
        raise ValueError("authorization canonical_symbol_count mismatch")
    if authorization.canonical_symbols_sha256 != run_contract.canonical_symbols_sha256:
        raise ValueError("authorization canonical_symbols_sha256 mismatch")
    if authorization.expected_partition_count != run_contract.expected_partition_count:
        raise ValueError("authorization expected_partition_count mismatch")
    expected_endpoints = tuple(item.tushare_api for item in run_contract.source_endpoints)
    if authorization.source_endpoints != expected_endpoints:
        raise ValueError("authorization source_endpoints mismatch")


def verify_collection_authorization_file(
    *,
    authorization_path: Path,
    repo_root: Path,
    run_contract_path: Path = DEFAULT_RUN_CONTRACT_PATH,
    preverified_run_contract: FinancialNegativeListCollectionRunContract | None = None,
    preverified_run_contract_result: RunContractVerificationResult | None = None,
) -> tuple[FinancialNegativeListCollectionAuthorization, AuthorizationVerificationResult]:
    root = Path(repo_root).resolve()
    auth_path = resolve_repo_regular_file(
        Path(authorization_path),
        repo_root=root,
        field_name="authorization file",
    )
    has_preverified_contract = preverified_run_contract is not None
    has_preverified_result = preverified_run_contract_result is not None
    if has_preverified_contract != has_preverified_result:
        raise ValueError(
            "preverified run contract and preverified run contract result must both be provided or both be omitted"
        )
    if has_preverified_contract:
        run_contract = preverified_run_contract
        if run_contract is None or preverified_run_contract_result is None:
            raise ValueError("preverified run contract inputs missing")
        if preverified_run_contract_result.run_contract_id != str(run_contract.run_contract_id):
            raise ValueError("preverified run contract result mismatch")
    else:
        run_contract, preverified_run_contract_result = verify_run_contract_file(
            run_contract_path=run_contract_path,
            repo_root=root,
        )
    if run_contract.run_contract_id is None:
        raise ValueError("run contract is unexpectedly unsealed")
    if run_contract.status != "prepared_not_authorized":
        raise ValueError("run contract status drift")
    authorization = load_collection_authorization(auth_path)
    assert_authorization_self_hash(authorization)
    _assert_matches_run_contract(authorization, run_contract)
    prepared_at = date.fromisoformat(run_contract.prepared_at)
    auth_date = date.fromisoformat(authorization.authorization_date)
    if auth_date < prepared_at:
        raise ValueError("authorization_date must not be earlier than prepared_at")
    now_sh = datetime.now(_SH_TZ)
    today_sh = now_sh.date()
    if auth_date > today_sh:
        raise ValueError("authorization_date must not be later than current Asia/Shanghai date")
    if auth_date == today_sh:
        auth_dt = datetime.combine(auth_date, time.fromisoformat(authorization.authorization_time), _SH_TZ)
        if auth_dt > now_sh + _AUTH_TIME_FUTURE_SKEW:
            raise ValueError("authorization_time is in the future relative to current Asia/Shanghai time")
    if authorization.network_collection_allowed is not True:
        raise ValueError("authorization must set network_collection_allowed=true")
    if authorization.ready_for_scoring is not False:
        raise ValueError("authorization ready_for_scoring must remain false")
    if authorization.ready_for_backtest is not False:
        raise ValueError("authorization ready_for_backtest must remain false")
    if authorization.ready_for_trading is not False:
        raise ValueError("authorization ready_for_trading must remain false")

    return (
        authorization,
        AuthorizationVerificationResult(
            authorization_id=str(authorization.authorization_id),
            run_contract_id=authorization.run_contract_id,
            run_contract_version=run_contract.run_contract_version,
            protocol_id=authorization.protocol_id,
            staging_dir=authorization.staging_dir,
            response_boundary_policy_id=run_contract.response_boundary_policy_id,
            response_boundary_policy_file_sha256=run_contract.response_boundary_policy_file_sha256,
            response_boundary_reason_code=run_contract.response_boundary_reason_code,
            network_collection_allowed=authorization.network_collection_allowed,
            ready_for_scoring=authorization.ready_for_scoring,
            ready_for_backtest=authorization.ready_for_backtest,
            ready_for_trading=authorization.ready_for_trading,
        ),
    )


__all__ = [
    "AUTHORIZATION_SCHEMA_VERSION",
    "AUTHORIZATION_VERSION",
    "AuthorizationVerificationResult",
    "FinancialNegativeListCollectionAuthorization",
    "assert_authorization_self_hash",
    "compute_authorization_id",
    "load_collection_authorization",
    "verify_collection_authorization_file",
]
