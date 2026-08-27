"""E11b-2c: Prepared run contract for financial-negative-list collection.

Offline-only strict run contract model and verifier for the historical
financial-negative-list raw collection. This contract is intentionally
prepared-but-not-authorized and never grants network permission by itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_financial_negative_list_data_protocol import (
    ANNOUNCEMENT_COLLECTION_END,
    ANNOUNCEMENT_COLLECTION_START,
    BALANCESHEET_FIELDS,
    FINA_AUDIT_FIELDS,
    FINA_INDICATOR_FIELDS,
    INCOME_FIELDS,
    PROTOCOL_FILE_PATH,
    SOURCE_ENDPOINTS,
    verify_protocol_file,
)
from app.research.layer_two_financial_negative_list_response_boundary_policy import (
    POLICY_FILE_PATH_V1 as RESPONSE_BOUNDARY_POLICY_FILE_PATH_V1,
)
from app.research.layer_two_financial_negative_list_response_boundary_policy import (
    POLICY_FILE_PATH_V2 as RESPONSE_BOUNDARY_POLICY_FILE_PATH_V2,
)
from app.research.layer_two_financial_negative_list_response_boundary_policy import (
    POLICY_REASON_CODE as RESPONSE_BOUNDARY_REASON_CODE,
)
from app.research.layer_two_financial_negative_list_response_boundary_policy import (
    verify_policy_file,
)
from app.research.layer_two_financial_negative_list_stock_basic import (
    canonical_symbols_sha256,
    load_canonical_symbol_listing_dates,
)
from app.research.repo_file_safety import resolve_repo_regular_file

RUN_CONTRACT_SCHEMA_VERSION: Literal["1"] = "1"
RUN_CONTRACT_VERSION_V1: Literal["financial-negative-list-collection-run-contract-v1"] = (
    "financial-negative-list-collection-run-contract-v1"
)
RUN_CONTRACT_VERSION_V2: Literal["financial-negative-list-collection-run-contract-v2"] = (
    "financial-negative-list-collection-run-contract-v2"
)
RUN_CONTRACT_VERSION_V3: Literal["financial-negative-list-collection-run-contract-v3"] = (
    "financial-negative-list-collection-run-contract-v3"
)
DEFAULT_RUN_CONTRACT_PATH = Path("config/research/financial-negative-list-collection-run-contract-v3.json")
LEGACY_RUN_CONTRACT_PATH = Path("config/research/financial-negative-list-collection-run-contract-v1.json")
RUN_CONTRACT_PATH_V2 = Path("config/research/financial-negative-list-collection-run-contract-v2.json")
FIXED_STAGING_DIR_V1: Literal["data/raw/a-share-financial-negative-list-20200101-20241231-v1"] = (
    "data/raw/a-share-financial-negative-list-20200101-20241231-v1"
)
FIXED_STAGING_DIR_V2: Literal["data/raw/a-share-financial-negative-list-20200101-20241231-v2"] = (
    "data/raw/a-share-financial-negative-list-20200101-20241231-v2"
)
FIXED_STAGING_DIR_V3: Literal["data/raw/a-share-financial-negative-list-20200101-20241231-v3"] = (
    "data/raw/a-share-financial-negative-list-20200101-20241231-v3"
)
RAW_STOCK_BASIC_RELATIVE_PATH_SUFFIX: Literal["reference/stock_basic.parquet"] = "reference/stock_basic.parquet"
PREPARED_AT_V1_V2: Literal["2026-08-26"] = "2026-08-26"
PREPARED_AT_V3: Literal["2026-08-27"] = "2026-08-27"
PREPARED_AT = PREPARED_AT_V3

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_EXPECTED_FIELDS: dict[str, tuple[str, ...]] = {
    "balancesheet": BALANCESHEET_FIELDS,
    "income": INCOME_FIELDS,
    "fina_indicator": FINA_INDICATOR_FIELDS,
    "fina_audit": FINA_AUDIT_FIELDS,
}
_EXPECTED_DOCS: dict[str, str] = {
    "balancesheet": "https://tushare.pro/document/2?doc_id=36",
    "income": "https://tushare.pro/document/2?doc_id=33",
    "fina_indicator": "https://tushare.pro/document/2?doc_id=79",
    "fina_audit": "https://tushare.pro/document/2?doc_id=80",
}


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunContractEndpoint(_StrictFrozen):
    tushare_api: str = Field(min_length=1)
    official_doc: str = Field(min_length=1)
    fields: tuple[str, ...]

    @field_validator("tushare_api", "official_doc", mode="before")
    @classmethod
    def _no_blank(cls, value: object) -> str:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("must be non-empty string")
        return value


class RunContractReadiness(_StrictFrozen):
    ready_for_scoring: Literal[False]
    ready_for_backtest: Literal[False]
    ready_for_trading: Literal[False]


class FinancialNegativeListCollectionRunContract(_StrictFrozen):
    schema_version: Literal["1"]
    run_contract_version: Literal[
        "financial-negative-list-collection-run-contract-v1",
        "financial-negative-list-collection-run-contract-v2",
        "financial-negative-list-collection-run-contract-v3",
    ]
    status: Literal["prepared_not_authorized"]
    network_authorized: Literal[False]
    requires_fresh_user_authorization: Literal[True]
    not_authorization_file: Literal[True]
    historical_collection_only: Literal[True]
    collected_at_not_available_at: Literal[True]
    e11b_2a_protocol_path: str
    prepared_at: Literal["2026-08-26", "2026-08-27"]
    e11b_2a_protocol_id: str = Field(min_length=64, max_length=64)
    e11b_2a_protocol_file_sha256: str = Field(min_length=64, max_length=64)
    announcement_window_start: str
    announcement_window_end: str
    source_endpoints: tuple[RunContractEndpoint, RunContractEndpoint, RunContractEndpoint, RunContractEndpoint]
    raw_stock_basic_source_path: str = Field(min_length=1)
    candidate_pack_id: str = Field(min_length=64, max_length=64)
    candidate_pack_parquet_sha256: str = Field(min_length=64, max_length=64)
    fixed_staging_dir: str = Field(min_length=1)
    response_boundary_policy_path: str | None = None
    response_boundary_policy_id: str | None = None
    response_boundary_policy_file_sha256: str | None = None
    response_boundary_reason_code: str | None = None
    canonical_symbol_count: int = Field(ge=1)
    canonical_symbols_sha256: str = Field(min_length=64, max_length=64)
    expected_partition_count: int = Field(ge=1)
    readiness: RunContractReadiness
    run_contract_id: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator(
        "e11b_2a_protocol_id",
        "e11b_2a_protocol_file_sha256",
        "candidate_pack_id",
        "candidate_pack_parquet_sha256",
        "canonical_symbols_sha256",
        mode="before",
    )
    @classmethod
    def _hex64(cls, value: object) -> str:
        if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
            raise ValueError("must be 64-char lowercase hex")
        return value

    @field_validator("run_contract_id", mode="before")
    @classmethod
    def _opt_hex64(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
            raise ValueError("run_contract_id must be 64-char lowercase hex")
        return value

    @model_validator(mode="after")
    def _strict_semantics(self) -> FinancialNegativeListCollectionRunContract:
        if self.e11b_2a_protocol_path != PROTOCOL_FILE_PATH:
            raise ValueError("e11b_2a_protocol_path mismatch")
        if self.announcement_window_start != ANNOUNCEMENT_COLLECTION_START.isoformat():
            raise ValueError("announcement_window_start must match frozen window")
        if self.announcement_window_end != ANNOUNCEMENT_COLLECTION_END.isoformat():
            raise ValueError("announcement_window_end must match frozen window")
        if self.run_contract_version == RUN_CONTRACT_VERSION_V1:
            if self.prepared_at != PREPARED_AT_V1_V2:
                raise ValueError("v1 prepared_at mismatch")
            if self.fixed_staging_dir != FIXED_STAGING_DIR_V1:
                raise ValueError("v1 fixed_staging_dir mismatch")
            if (
                self.response_boundary_policy_path is not None
                or self.response_boundary_policy_id is not None
                or self.response_boundary_policy_file_sha256 is not None
                or self.response_boundary_reason_code is not None
            ):
                raise ValueError("v1 run contract must not bind response-boundary policy")
        elif self.run_contract_version == RUN_CONTRACT_VERSION_V2:
            if self.prepared_at != PREPARED_AT_V1_V2:
                raise ValueError("v2 prepared_at mismatch")
            if self.fixed_staging_dir != FIXED_STAGING_DIR_V2:
                raise ValueError("v2 fixed_staging_dir mismatch")
            if self.response_boundary_policy_path != RESPONSE_BOUNDARY_POLICY_FILE_PATH_V1:
                raise ValueError("v2 response_boundary_policy_path mismatch")
            for field_name, value in (
                ("response_boundary_policy_id", self.response_boundary_policy_id),
                ("response_boundary_policy_file_sha256", self.response_boundary_policy_file_sha256),
            ):
                if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
                    raise ValueError(f"v2 {field_name} must be 64-char lowercase hex")
            if self.response_boundary_reason_code != RESPONSE_BOUNDARY_REASON_CODE:
                raise ValueError("v2 response_boundary_reason_code mismatch")
        elif self.run_contract_version == RUN_CONTRACT_VERSION_V3:
            if self.prepared_at != PREPARED_AT_V3:
                raise ValueError("v3 prepared_at mismatch")
            if self.fixed_staging_dir != FIXED_STAGING_DIR_V3:
                raise ValueError("v3 fixed_staging_dir mismatch")
            if self.response_boundary_policy_path != RESPONSE_BOUNDARY_POLICY_FILE_PATH_V2:
                raise ValueError("v3 response_boundary_policy_path mismatch")
            for field_name, value in (
                ("response_boundary_policy_id", self.response_boundary_policy_id),
                ("response_boundary_policy_file_sha256", self.response_boundary_policy_file_sha256),
            ):
                if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
                    raise ValueError(f"v3 {field_name} must be 64-char lowercase hex")
            if self.response_boundary_reason_code != RESPONSE_BOUNDARY_REASON_CODE:
                raise ValueError("v3 response_boundary_reason_code mismatch")
        else:
            raise ValueError("unsupported run_contract_version")
        if self.expected_partition_count != self.canonical_symbol_count * len(SOURCE_ENDPOINTS):
            raise ValueError("expected_partition_count must equal canonical_symbol_count * endpoint_count")
        if len(self.source_endpoints) != len(SOURCE_ENDPOINTS):
            raise ValueError("source_endpoints length mismatch")
        for index, endpoint in enumerate(SOURCE_ENDPOINTS):
            item = self.source_endpoints[index]
            if item.tushare_api != endpoint:
                raise ValueError(f"source_endpoints[{index}].tushare_api must be {endpoint}")
            if item.official_doc != _EXPECTED_DOCS[endpoint]:
                raise ValueError(f"source_endpoints[{index}] official_doc mismatch")
            if item.fields != _EXPECTED_FIELDS[endpoint]:
                raise ValueError(f"source_endpoints[{index}] fields mismatch")
        return self


@dataclass(frozen=True)
class RunContractVerificationResult:
    run_contract_id: str
    status: str
    network_authorized: bool
    requires_fresh_user_authorization: bool
    canonical_symbol_count: int
    canonical_symbols_sha256: str
    expected_partition_count: int
    run_contract_version: str
    fixed_staging_dir: str
    raw_stock_basic_source_path: str
    response_boundary_policy_id: str | None = None
    response_boundary_policy_path: str | None = None
    response_boundary_policy_file_sha256: str | None = None
    response_boundary_reason_code: str | None = None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def canonical_run_contract_payload(contract: FinancialNegativeListCollectionRunContract) -> dict[str, Any]:
    return contract.model_dump(mode="json", exclude={"run_contract_id"}, exclude_none=True)


def compute_run_contract_id(contract: FinancialNegativeListCollectionRunContract) -> str:
    return _canonical_sha(canonical_run_contract_payload(contract))


def seal_run_contract(
    contract: FinancialNegativeListCollectionRunContract,
) -> FinancialNegativeListCollectionRunContract:
    rid = compute_run_contract_id(contract)
    return contract.model_copy(update={"run_contract_id": rid})


def _stock_basic_source_path(*, repo_root: Path, raw_collection_dir: str) -> Path:
    return repo_root / raw_collection_dir / RAW_STOCK_BASIC_RELATIVE_PATH_SUFFIX


def recompute_symbol_bindings_from_stock_basic(stock_basic_path: Path) -> tuple[int, str]:
    symbols, _listing_dates = load_canonical_symbol_listing_dates(stock_basic_path)
    return len(symbols), canonical_symbols_sha256(symbols)


def build_prepared_run_contract(repo_root: Path) -> FinancialNegativeListCollectionRunContract:
    root = Path(repo_root).resolve()
    policy, policy_result = verify_policy_file(
        repo_root=root,
        policy_path=Path(RESPONSE_BOUNDARY_POLICY_FILE_PATH_V2),
    )
    policy_sha = policy_result.policy_file_sha256
    protocol = verify_protocol_file(root)
    protocol_path = root / PROTOCOL_FILE_PATH
    protocol_file_sha = _sha256_file(protocol_path)
    raw_stock_basic = _stock_basic_source_path(
        repo_root=root, raw_collection_dir=str(protocol.bindings.raw_collection_dir)
    )
    symbol_count, symbols_sha = recompute_symbol_bindings_from_stock_basic(raw_stock_basic)
    endpoints: list[RunContractEndpoint] = []
    for endpoint in SOURCE_ENDPOINTS:
        item = getattr(protocol.source_endpoints, endpoint)
        endpoints.append(
            RunContractEndpoint(
                tushare_api=str(item.tushare_api),
                official_doc=str(item.official_doc),
                fields=tuple(str(field) for field in item.fields),
            )
        )
    return FinancialNegativeListCollectionRunContract(
        schema_version=RUN_CONTRACT_SCHEMA_VERSION,
        run_contract_version=RUN_CONTRACT_VERSION_V3,
        status="prepared_not_authorized",
        network_authorized=False,
        requires_fresh_user_authorization=True,
        not_authorization_file=True,
        historical_collection_only=True,
        collected_at_not_available_at=True,
        e11b_2a_protocol_path=PROTOCOL_FILE_PATH,
        prepared_at=PREPARED_AT,
        e11b_2a_protocol_id=str(protocol.protocol_id),
        e11b_2a_protocol_file_sha256=protocol_file_sha,
        announcement_window_start=ANNOUNCEMENT_COLLECTION_START.isoformat(),
        announcement_window_end=ANNOUNCEMENT_COLLECTION_END.isoformat(),
        source_endpoints=(endpoints[0], endpoints[1], endpoints[2], endpoints[3]),
        raw_stock_basic_source_path=str(raw_stock_basic.relative_to(root).as_posix()),
        candidate_pack_id=str(protocol.bindings.candidate_pack_id),
        candidate_pack_parquet_sha256=str(protocol.bindings.candidate_pack_parquet_sha256),
        fixed_staging_dir=FIXED_STAGING_DIR_V3,
        response_boundary_policy_path=RESPONSE_BOUNDARY_POLICY_FILE_PATH_V2,
        response_boundary_policy_id=policy.policy_id,
        response_boundary_policy_file_sha256=policy_sha,
        response_boundary_reason_code=policy.quarantine_reason_code,
        canonical_symbol_count=symbol_count,
        canonical_symbols_sha256=symbols_sha,
        expected_partition_count=symbol_count * len(SOURCE_ENDPOINTS),
        readiness=RunContractReadiness(
            ready_for_scoring=False,
            ready_for_backtest=False,
            ready_for_trading=False,
        ),
        run_contract_id=None,
    )


def assert_run_contract_self_hash(contract: FinancialNegativeListCollectionRunContract) -> None:
    if contract.run_contract_id is None:
        raise ValueError("run_contract_id is missing")
    expected = compute_run_contract_id(contract)
    if contract.run_contract_id != expected:
        raise ValueError("run_contract_id self-seal mismatch")


def load_run_contract(path: Path) -> FinancialNegativeListCollectionRunContract:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return FinancialNegativeListCollectionRunContract.model_validate(payload)


def verify_run_contract_file(
    *,
    run_contract_path: Path,
    repo_root: Path,
) -> tuple[FinancialNegativeListCollectionRunContract, RunContractVerificationResult]:
    root = Path(repo_root).resolve()
    contract_abs = resolve_repo_regular_file(
        Path(run_contract_path),
        repo_root=root,
        field_name="run contract path",
    )
    contract = load_run_contract(contract_abs)
    assert_run_contract_self_hash(contract)

    protocol_path = root / contract.e11b_2a_protocol_path
    protocol = verify_protocol_file(root)
    protocol_file_sha = _sha256_file(protocol_path)
    if contract.e11b_2a_protocol_id != str(protocol.protocol_id):
        raise ValueError("run contract protocol_id drift")
    if contract.e11b_2a_protocol_file_sha256 != protocol_file_sha:
        raise ValueError("run contract protocol_file_sha256 drift")
    if contract.run_contract_version == RUN_CONTRACT_VERSION_V2:
        policy, policy_result = verify_policy_file(
            repo_root=root,
            policy_path=Path(RESPONSE_BOUNDARY_POLICY_FILE_PATH_V1),
        )
        if contract.response_boundary_policy_path != RESPONSE_BOUNDARY_POLICY_FILE_PATH_V1:
            raise ValueError("run contract response_boundary_policy_path drift")
        if contract.response_boundary_policy_id != policy.policy_id:
            raise ValueError("run contract response_boundary_policy_id drift")
        if contract.response_boundary_policy_file_sha256 != policy_result.policy_file_sha256:
            raise ValueError("run contract response_boundary_policy_file_sha256 drift")
        if contract.response_boundary_reason_code != policy.quarantine_reason_code:
            raise ValueError("run contract response_boundary_reason_code drift")
    elif contract.run_contract_version == RUN_CONTRACT_VERSION_V3:
        policy, policy_result = verify_policy_file(
            repo_root=root,
            policy_path=Path(RESPONSE_BOUNDARY_POLICY_FILE_PATH_V2),
        )
        if contract.response_boundary_policy_path != RESPONSE_BOUNDARY_POLICY_FILE_PATH_V2:
            raise ValueError("run contract response_boundary_policy_path drift")
        if contract.response_boundary_policy_id != policy.policy_id:
            raise ValueError("run contract response_boundary_policy_id drift")
        if contract.response_boundary_policy_file_sha256 != policy_result.policy_file_sha256:
            raise ValueError("run contract response_boundary_policy_file_sha256 drift")
        if contract.response_boundary_reason_code != policy.quarantine_reason_code:
            raise ValueError("run contract response_boundary_reason_code drift")

    for index, endpoint in enumerate(SOURCE_ENDPOINTS):
        item = contract.source_endpoints[index]
        source = getattr(protocol.source_endpoints, endpoint)
        if item.tushare_api != str(source.tushare_api):
            raise ValueError(f"run contract endpoint api drift: {endpoint}")
        if item.official_doc != str(source.official_doc):
            raise ValueError(f"run contract endpoint doc drift: {endpoint}")
        if item.fields != tuple(str(field) for field in source.fields):
            raise ValueError(f"run contract endpoint fields drift: {endpoint}")

    if contract.candidate_pack_id != str(protocol.bindings.candidate_pack_id):
        raise ValueError("run contract candidate_pack_id drift")
    if contract.candidate_pack_parquet_sha256 != str(protocol.bindings.candidate_pack_parquet_sha256):
        raise ValueError("run contract candidate_pack_parquet_sha256 drift")

    expected_stock_basic = _stock_basic_source_path(
        repo_root=root, raw_collection_dir=str(protocol.bindings.raw_collection_dir)
    )
    expected_stock_basic_rel = expected_stock_basic.relative_to(root).as_posix()
    if contract.raw_stock_basic_source_path != expected_stock_basic_rel:
        raise ValueError("run contract raw_stock_basic_source_path drift")
    symbol_count, symbols_sha = recompute_symbol_bindings_from_stock_basic(expected_stock_basic)
    expected_partition_count = symbol_count * len(SOURCE_ENDPOINTS)
    if contract.canonical_symbol_count != symbol_count:
        raise ValueError("run contract canonical_symbol_count drift")
    if contract.canonical_symbols_sha256 != symbols_sha:
        raise ValueError("run contract canonical_symbols_sha256 drift")
    if contract.expected_partition_count != expected_partition_count:
        raise ValueError("run contract expected_partition_count drift")

    if contract.status != "prepared_not_authorized":
        raise ValueError("run contract status must stay prepared_not_authorized")
    if contract.network_authorized is not False:
        raise ValueError("run contract must not authorize network collection")
    if contract.requires_fresh_user_authorization is not True:
        raise ValueError("run contract must require fresh user authorization")

    result = RunContractVerificationResult(
        run_contract_id=str(contract.run_contract_id),
        status=contract.status,
        network_authorized=contract.network_authorized,
        requires_fresh_user_authorization=contract.requires_fresh_user_authorization,
        canonical_symbol_count=symbol_count,
        canonical_symbols_sha256=symbols_sha,
        expected_partition_count=expected_partition_count,
        run_contract_version=contract.run_contract_version,
        fixed_staging_dir=contract.fixed_staging_dir,
        raw_stock_basic_source_path=contract.raw_stock_basic_source_path,
        response_boundary_policy_id=contract.response_boundary_policy_id,
        response_boundary_policy_path=contract.response_boundary_policy_path,
        response_boundary_policy_file_sha256=contract.response_boundary_policy_file_sha256,
        response_boundary_reason_code=contract.response_boundary_reason_code,
    )
    return contract, result


def write_run_contract(
    path: Path, contract: FinancialNegativeListCollectionRunContract
) -> FinancialNegativeListCollectionRunContract:
    sealed = seal_run_contract(contract)
    payload = sealed.model_dump(mode="json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return sealed


__all__ = [
    "DEFAULT_RUN_CONTRACT_PATH",
    "FIXED_STAGING_DIR_V1",
    "FIXED_STAGING_DIR_V2",
    "FIXED_STAGING_DIR_V3",
    "LEGACY_RUN_CONTRACT_PATH",
    "PREPARED_AT",
    "PREPARED_AT_V1_V2",
    "PREPARED_AT_V3",
    "RUN_CONTRACT_SCHEMA_VERSION",
    "RUN_CONTRACT_VERSION_V1",
    "RUN_CONTRACT_VERSION_V2",
    "RUN_CONTRACT_VERSION_V3",
    "RUN_CONTRACT_PATH_V2",
    "FinancialNegativeListCollectionRunContract",
    "RunContractVerificationResult",
    "assert_run_contract_self_hash",
    "build_prepared_run_contract",
    "compute_run_contract_id",
    "load_run_contract",
    "recompute_symbol_bindings_from_stock_basic",
    "seal_run_contract",
    "verify_run_contract_file",
    "write_run_contract",
]
