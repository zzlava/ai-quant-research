"""Sealed factual identity for the CSI All Share price and total-return indices.

The contract cross-checks CSI official documents with a narrow Tushare probe.
It proves index identity only.  It deliberately does not materialize history,
fill source gaps, run research evaluation, or authorize scoring/backtests/trading.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.repo_file_safety import resolve_repo_regular_file

SCHEMA_VERSION: Literal["1"] = "1"
CONTRACT_VERSION: Literal["csi-all-share-index-identity-v1"] = "csi-all-share-index-identity-v1"
DEFAULT_CONTRACT_PATH = Path("config/research/csi-all-share-index-identity-v1.json")
CONFIRMATION_AS_OF = date(2026, 8, 27)
OBSERVED_AT = datetime(2026, 8, 27, 3, 25, 0, tzinfo=UTC)

PRICE_TS_CODE: Literal["000985.CSI"] = "000985.CSI"
TOTAL_RETURN_TS_CODE: Literal["H00985.CSI"] = "H00985.CSI"
NET_RETURN_TS_CODE: Literal["N00985.CSI"] = "N00985.CSI"
SOURCE_WINDOW_START = date(2005, 1, 1)
SOURCE_WINDOW_END = date(2024, 12, 31)
PRICE_FIRST_DATE = date(2005, 1, 4)
PRICE_LAST_DATE = date(2024, 12, 31)
TOTAL_RETURN_FIRST_DATE = date(2005, 1, 4)
TOTAL_RETURN_LAST_DATE = date(2024, 12, 31)
TOTAL_RETURN_MISSING_DATES: tuple[date, ...] = (date(2011, 8, 2),)

# Filled after canonical contract generation.  File verification also compares
# the complete payload with the factory, so a valid outer re-seal cannot alter
# evidence URLs, hashes, identities, probe results, or readiness gates.
EXPECTED_CURRENT_CONTRACT_ID = "85ef3834da78d3642bfbb7009bbd0c5e1c1c5c2b3af1cf01a6d9e21038f32b22"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _aware_datetime(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed


class EvidenceSource(_StrictModel):
    source_id: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    content_sha256_at_access: str = Field(pattern=r"^[0-9a-f]{64}$")
    accessed_at: datetime
    evidence_role: Literal[
        "official_index_code_and_derivatives",
        "official_index_methodology",
        "official_total_return_history_recovery",
        "tushare_index_identity_schema",
        "tushare_index_daily_schema",
    ]

    @field_validator("accessed_at", mode="before")
    @classmethod
    def _parse_accessed_at(cls, value: object) -> datetime:
        return _aware_datetime(value, field_name="accessed_at")


class IndexIdentity(_StrictModel):
    role: Literal["market_risk_state", "performance_comparison", "diagnostic_only"]
    official_name: str = Field(min_length=1)
    official_code: str = Field(min_length=1)
    tushare_ts_code: str = Field(min_length=1)
    return_definition: Literal["price_index", "total_return", "net_return"]
    publisher: Literal["中证指数有限公司"] = "中证指数有限公司"
    market: Literal["CSI"] = "CSI"
    base_date: date
    base_point: float
    list_date: date
    index_basic_rows: int = Field(ge=1)
    small_daily_probe_rows: int = Field(ge=1)
    identity_verified: Literal[True] = True

    @model_validator(mode="after")
    def _positive_base(self) -> IndexIdentity:
        if self.base_point != 1000.0:
            raise ValueError("CSI All Share base_point must remain 1000.0")
        if self.index_basic_rows != 1:
            raise ValueError("each exact Tushare index_basic probe must return exactly one row")
        if self.small_daily_probe_rows != 5:
            raise ValueError("each fixed small index_daily probe must return exactly five rows")
        return self


class CoverageSeriesProbe(_StrictModel):
    tushare_ts_code: str = Field(min_length=1)
    requested_start: date
    requested_end: date
    returned_rows: int = Field(ge=1)
    first_trade_date: date
    last_trade_date: date
    duplicate_key_rows: int = Field(ge=0)
    required_field_null_counts: dict[str, int]
    optional_field_null_counts: dict[str, int]

    @model_validator(mode="after")
    def _ordered(self) -> CoverageSeriesProbe:
        if self.requested_end < self.requested_start:
            raise ValueError("coverage request end must be on or after start")
        if self.last_trade_date < self.first_trade_date:
            raise ValueError("coverage result dates must be ordered")
        if any(value < 0 for value in self.required_field_null_counts.values()):
            raise ValueError("required-field null counts must be non-negative")
        if any(value < 0 for value in self.optional_field_null_counts.values()):
            raise ValueError("optional-field null counts must be non-negative")
        return self


class CoverageCrossCheck(_StrictModel):
    common_trade_dates: int = Field(ge=1)
    price_only_trade_dates: list[date]
    total_return_only_trade_dates: list[date]
    missing_dates_must_not_be_synthesized: Literal[True] = True
    total_return_ohlc_is_not_required_for_close_to_close_benchmark: Literal[True] = True


class OfficialRecoveryProbe(_StrictModel):
    endpoint_index_code: Literal["H00985"] = "H00985"
    requested_start: date
    requested_end: date
    returned_rows: int = Field(ge=1)
    first_source_date: date
    last_source_date: date
    duplicate_source_dates: int = Field(ge=0)
    null_close_rows: int = Field(ge=0)
    source_dates_outside_sse_open_calendar: list[date]
    tushare_dates_missing_from_official: list[date]
    official_only_valid_trading_dates: list[date]
    common_dates: int = Field(ge=1)
    common_close_difference_rows: int = Field(ge=0)
    maximum_common_close_difference_bps: float = Field(ge=0.0)
    fixed_official_override_dates: list[date]
    repair_rule: Literal[
        "tushare_primary_exact_sse_calendar_official_override_only_on_2011_08_02_and_2011_08_03"
    ] = "tushare_primary_exact_sse_calendar_official_override_only_on_2011_08_02_and_2011_08_03"
    no_interpolation_or_forward_fill: Literal[True] = True
    official_source_rows_must_be_hash_bound_before_materialization: Literal[True] = True

    @model_validator(mode="after")
    def _fixed_repair_dates(self) -> OfficialRecoveryProbe:
        if self.fixed_official_override_dates != [date(2011, 8, 2), date(2011, 8, 3)]:
            raise ValueError("official override dates must remain exactly 2011-08-02 and 2011-08-03")
        if self.official_only_valid_trading_dates != [date(2011, 8, 2)]:
            raise ValueError("official-only valid trading date must remain 2011-08-02")
        return self


class ReadinessGates(_StrictModel):
    factual_identity_verified: Literal[True] = True
    price_series_ready_for_long_history_materialization: Literal[True] = True
    total_return_series_ready_for_strict_long_history_materialization: Literal[True] = True
    long_history_materializer_status: Literal["pending_implementation"] = "pending_implementation"
    remaining_blocker: Literal[
        "offline_long_history_materializer_and_hash_bound_raw_collection_not_yet_implemented"
    ] = "offline_long_history_materializer_and_hash_bound_raw_collection_not_yet_implemented"
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    ready_for_orders: Literal[False] = False
    auto_apply: Literal[False] = False


class CSIAllShareIndexIdentityContract(_StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    contract_version: Literal["csi-all-share-index-identity-v1"] = CONTRACT_VERSION
    contract_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmation_as_of: date
    observed_at: datetime
    purpose: Literal["factual_index_identity_and_source_coverage_probe_only"] = (
        "factual_index_identity_and_source_coverage_probe_only"
    )
    official_family_name: Literal["中证全指指数"] = "中证全指指数"
    evidence_sources: list[EvidenceSource]
    identities: list[IndexIdentity]
    fixed_small_probe_window: dict[str, date]
    coverage_probes: list[CoverageSeriesProbe]
    coverage_cross_check: CoverageCrossCheck
    official_recovery_probe: OfficialRecoveryProbe
    readiness: ReadinessGates
    no_token_value_recorded: Literal[True] = True
    no_market_values_recorded: Literal[True] = True
    no_consumed_oos_used: Literal[True] = True
    does_not_materialize_history: Literal[True] = True
    does_not_score: Literal[True] = True
    does_not_backtest: Literal[True] = True
    does_not_trade: Literal[True] = True
    note: str = Field(min_length=1)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _parse_observed_at(cls, value: object) -> datetime:
        return _aware_datetime(value, field_name="observed_at")

    @model_validator(mode="after")
    def _contract_invariants(self) -> CSIAllShareIndexIdentityContract:
        if self.confirmation_as_of != CONFIRMATION_AS_OF:
            raise ValueError("confirmation_as_of must remain 2026-08-27")
        if self.observed_at != OBSERVED_AT:
            raise ValueError("observed_at must match the sealed factual probe timestamp")
        if len(self.evidence_sources) != 5:
            raise ValueError("exactly five sealed evidence sources are required")
        if len(self.identities) != 3:
            raise ValueError("price, total-return, and net-return identities are required")
        if len(self.coverage_probes) != 2:
            raise ValueError("price and total-return coverage probes are required")
        if self.fixed_small_probe_window != {
            "start": date(2024, 7, 1),
            "end": date(2024, 7, 5),
        }:
            raise ValueError("fixed small probe window must remain 2024-07-01..2024-07-05")
        return self


class CSIAllShareIndexIdentityVerificationResult(_StrictModel):
    contract_id: str
    structural_ok: Literal[True] = True
    canonical_factory_binding_ok: Literal[True] = True
    disk_binding_ok: bool = False
    factual_identity_verified: Literal[True] = True
    price_series_ready_for_long_history_materialization: Literal[True] = True
    total_return_series_ready_for_strict_long_history_materialization: Literal[True] = True
    blocker: str
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False


def _canonical_payload(contract: CSIAllShareIndexIdentityContract) -> dict[str, Any]:
    return contract.model_dump(mode="json", exclude={"contract_id"})


def _canonical_bytes(contract: CSIAllShareIndexIdentityContract) -> bytes:
    return json.dumps(
        _canonical_payload(contract), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def compute_contract_id(contract: CSIAllShareIndexIdentityContract) -> str:
    return hashlib.sha256(_canonical_bytes(contract)).hexdigest()


def seal_contract(contract: CSIAllShareIndexIdentityContract) -> CSIAllShareIndexIdentityContract:
    return contract.model_copy(update={"contract_id": compute_contract_id(contract)})


def build_csi_all_share_index_identity_v1() -> CSIAllShareIndexIdentityContract:
    sources = [
        EvidenceSource(
            source_id="csi_000985_factsheet_20260731",
            publisher="中证指数有限公司",
            title="中证全指指数事实表（2026-07-31）",
            url=(
                "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/"
                "detail/files/zh_CN/000985factsheet.pdf"
            ),
            content_sha256_at_access="66e11b85aa8b62cf6a48e5cc930b04094740d49a7872bf547632e52eac34f92b",
            accessed_at=OBSERVED_AT,
            evidence_role="official_index_code_and_derivatives",
        ),
        EvidenceSource(
            source_id="csi_000985_methodology_20231208",
            publisher="中证指数有限公司",
            title="中证全指指数编制方案",
            url=(
                "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/"
                "detail/files/zh_CN/20231208175438-000985_Index_Methodology_cn.pdf"
            ),
            content_sha256_at_access="d6a4d24334f600fe2c80e4f3cbc01cffed931776ec61f7081dffcd72847969d1",
            accessed_at=OBSERVED_AT,
            evidence_role="official_index_methodology",
        ),
        EvidenceSource(
            source_id="csi_h00985_history_probe_2005_2024",
            publisher="中证指数有限公司",
            title="中证全指全收益指数 2005-2024 历史响应",
            url=(
                "https://www.csindex.com.cn/csindex-home/perf/index-perf?"
                "indexCode=H00985&startDate=20050101&endDate=20241231"
            ),
            content_sha256_at_access="af84485b6c5f2a864ab2c850cdf9197e3e622a5d9d50e41ec3ad55a37e79b792",
            accessed_at=OBSERVED_AT,
            evidence_role="official_total_return_history_recovery",
        ),
        EvidenceSource(
            source_id="tushare_index_basic_doc_94",
            publisher="Tushare",
            title="指数基本信息接口",
            url="https://tushare.pro/document/2?doc_id=94",
            content_sha256_at_access="8b8550dda1d1fb4f65b98458bd277dc2181323ec84833dc2aaeb9c57410517fe",
            accessed_at=OBSERVED_AT,
            evidence_role="tushare_index_identity_schema",
        ),
        EvidenceSource(
            source_id="tushare_index_daily_doc_95",
            publisher="Tushare",
            title="指数日线行情接口",
            url="https://tushare.pro/document/2?doc_id=95",
            content_sha256_at_access="368c211b45da5eec7c621f1962ec5d188ab16de052dbbebb0be1a7951b52fe5e",
            accessed_at=OBSERVED_AT,
            evidence_role="tushare_index_daily_schema",
        ),
    ]
    identities = [
        IndexIdentity(
            role="market_risk_state",
            official_name="中证全指指数",
            official_code="000985",
            tushare_ts_code=PRICE_TS_CODE,
            return_definition="price_index",
            base_date=date(2004, 12, 31),
            base_point=1000.0,
            list_date=date(2011, 8, 2),
            index_basic_rows=1,
            small_daily_probe_rows=5,
        ),
        IndexIdentity(
            role="performance_comparison",
            official_name="中证全指全收益指数",
            official_code="H00985",
            tushare_ts_code=TOTAL_RETURN_TS_CODE,
            return_definition="total_return",
            base_date=date(2004, 12, 31),
            base_point=1000.0,
            list_date=date(2011, 8, 2),
            index_basic_rows=1,
            small_daily_probe_rows=5,
        ),
        IndexIdentity(
            role="diagnostic_only",
            official_name="中证全指净收益指数",
            official_code="N00985",
            tushare_ts_code=NET_RETURN_TS_CODE,
            return_definition="net_return",
            base_date=date(2004, 12, 31),
            base_point=1000.0,
            list_date=date(2013, 2, 8),
            index_basic_rows=1,
            small_daily_probe_rows=5,
        ),
    ]
    coverage = [
        CoverageSeriesProbe(
            tushare_ts_code=PRICE_TS_CODE,
            requested_start=SOURCE_WINDOW_START,
            requested_end=SOURCE_WINDOW_END,
            returned_rows=4858,
            first_trade_date=PRICE_FIRST_DATE,
            last_trade_date=PRICE_LAST_DATE,
            duplicate_key_rows=0,
            required_field_null_counts={
                "trade_date": 0,
                "open": 0,
                "high": 0,
                "low": 0,
                "close": 0,
                "pre_close": 0,
            },
            optional_field_null_counts={},
        ),
        CoverageSeriesProbe(
            tushare_ts_code=TOTAL_RETURN_TS_CODE,
            requested_start=SOURCE_WINDOW_START,
            requested_end=SOURCE_WINDOW_END,
            returned_rows=4857,
            first_trade_date=TOTAL_RETURN_FIRST_DATE,
            last_trade_date=TOTAL_RETURN_LAST_DATE,
            duplicate_key_rows=0,
            required_field_null_counts={"trade_date": 0, "close": 0, "pre_close": 0},
            optional_field_null_counts={"open": 4857, "high": 4857, "low": 4857},
        ),
    ]
    contract = CSIAllShareIndexIdentityContract(
        confirmation_as_of=CONFIRMATION_AS_OF,
        observed_at=OBSERVED_AT,
        evidence_sources=sources,
        identities=identities,
        fixed_small_probe_window={"start": date(2024, 7, 1), "end": date(2024, 7, 5)},
        coverage_probes=coverage,
        coverage_cross_check=CoverageCrossCheck(
            common_trade_dates=4857,
            price_only_trade_dates=list(TOTAL_RETURN_MISSING_DATES),
            total_return_only_trade_dates=[],
        ),
        official_recovery_probe=OfficialRecoveryProbe(
            requested_start=SOURCE_WINDOW_START,
            requested_end=SOURCE_WINDOW_END,
            returned_rows=4860,
            first_source_date=date(2005, 1, 1),
            last_source_date=date(2024, 12, 31),
            duplicate_source_dates=0,
            null_close_rows=0,
            source_dates_outside_sse_open_calendar=[date(2005, 1, 1), date(2018, 6, 18)],
            tushare_dates_missing_from_official=[],
            official_only_valid_trading_dates=[date(2011, 8, 2)],
            common_dates=4857,
            common_close_difference_rows=3089,
            maximum_common_close_difference_bps=2.15205332788182,
            fixed_official_override_dates=[date(2011, 8, 2), date(2011, 8, 3)],
        ),
        readiness=ReadinessGates(),
        note=(
            "Official CSI documents and exact Tushare index_basic/index_daily probes confirm "
            "000985.CSI (price), H00985.CSI (total return), and N00985.CSI (net return). "
            "The Tushare price series exactly matches the 4,858-day SSE open calendar. The "
            "official H00985 response recovers the missing 2011-08-02 row and identifies a "
            "2011-08-03 source discrepancy; only those two dates may use official overrides. "
            "No interpolation or forward fill is permitted. The offline materializer remains pending."
        ),
    )
    return seal_contract(contract)


def verify_contract(
    contract: CSIAllShareIndexIdentityContract,
) -> CSIAllShareIndexIdentityVerificationResult:
    if contract.contract_id is None or contract.contract_id != compute_contract_id(contract):
        raise ValueError("index identity contract_id does not match canonical content hash")
    expected = build_csi_all_share_index_identity_v1()
    if _canonical_payload(contract) != _canonical_payload(expected):
        raise ValueError("index identity contract does not match canonical factual factory payload")
    return CSIAllShareIndexIdentityVerificationResult(
        contract_id=contract.contract_id,
        blocker=contract.readiness.remaining_blocker,
    )


def load_contract(path: Path) -> CSIAllShareIndexIdentityContract:
    try:
        return CSIAllShareIndexIdentityContract.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("index identity contract is missing or invalid") from exc


def verify_contract_file(
    *,
    repo_root: Path,
    contract_path: Path = DEFAULT_CONTRACT_PATH,
) -> tuple[CSIAllShareIndexIdentityContract, CSIAllShareIndexIdentityVerificationResult]:
    resolved = resolve_repo_regular_file(
        contract_path,
        repo_root=repo_root,
        field_name="index_identity_contract_path",
    )
    contract = load_contract(resolved)
    result = verify_contract(contract)
    if contract.contract_id != EXPECTED_CURRENT_CONTRACT_ID:
        raise ValueError("index identity contract_id does not match the disk-bound expected id")
    return contract, result.model_copy(update={"disk_binding_ok": True})


__all__ = [
    "CONFIRMATION_AS_OF",
    "CONTRACT_VERSION",
    "CSIAllShareIndexIdentityContract",
    "CSIAllShareIndexIdentityVerificationResult",
    "DEFAULT_CONTRACT_PATH",
    "EXPECTED_CURRENT_CONTRACT_ID",
    "NET_RETURN_TS_CODE",
    "OBSERVED_AT",
    "PRICE_TS_CODE",
    "TOTAL_RETURN_TS_CODE",
    "build_csi_all_share_index_identity_v1",
    "compute_contract_id",
    "load_contract",
    "seal_contract",
    "verify_contract",
    "verify_contract_file",
]
