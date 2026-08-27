from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import polars as pl
import pytest

from app.errors import DataQualityError, TushareFetchError
from app.providers import tushare_financial_negative_list_collection as collector
from app.research.layer_two_financial_negative_list_data_protocol import (
    BALANCESHEET_FIELDS,
    FINA_AUDIT_FIELDS,
    FINA_INDICATOR_FIELDS,
    INCOME_FIELDS,
    PROTOCOL_FILE_PATH,
)
from app.research.layer_two_financial_negative_list_finalization_authorization import (
    FinalizationAuthorizationVerificationResult,
)
from tests.tushare_fakes import FakeTushareClient

_AUTH_ID = "1" * 64
_ORIGINAL_COLLECT = collector.collect_tushare_financial_negative_list
_POLICY_ID = "9" * 64
_RUN_CONTRACT_ID = "8" * 64
_POLICY_FILE_SHA256 = "7" * 64


def _finalization_bindings() -> FinalizationAuthorizationVerificationResult:
    return FinalizationAuthorizationVerificationResult(
        authorization_id="a" * 64,
        authorization_file_sha256="b" * 64,
        policy_id="c" * 64,
        policy_file_sha256="d" * 64,
        policy_path="config/research/financial-negative-list-response-boundary-policy-v3.json",
        nullable_end_type_endpoints=("balancesheet", "income"),
    )


def _inject_default_verified_bindings(kwargs: dict[str, object]) -> None:
    kwargs.setdefault("verified_run_contract_id", _RUN_CONTRACT_ID)
    kwargs.setdefault("verified_run_contract_version", "financial-negative-list-collection-run-contract-v3")
    kwargs.setdefault("verified_response_boundary_policy_id", _POLICY_ID)
    kwargs.setdefault("verified_response_boundary_policy_file_sha256", _POLICY_FILE_SHA256)
    kwargs.setdefault("verified_response_boundary_reason_code", "FNLD-013")


def _collect_with_default_auth(**kwargs: object) -> collector.FinancialNegativeListCollectionResult:
    if "collection_authorization_id" not in kwargs:
        kwargs["collection_authorization_id"] = _AUTH_ID
    _inject_default_verified_bindings(kwargs)
    return _ORIGINAL_COLLECT(**kwargs)


collector.collect_tushare_financial_negative_list = _collect_with_default_auth


@dataclass(frozen=True)
class _FakePolicyResult:
    policy_id: str = _POLICY_ID
    policy_file_sha256: str = _POLICY_FILE_SHA256
    quarantine_reason_code: str = "FNLD-013"
    policy_version: str = "financial-negative-list-response-boundary-policy-v2"
    bound_base_protocol_id: str = "a" * 64
    bound_base_protocol_file_sha256: str = "b" * 64


collector.verify_policy_file = lambda repo_root: (_FakePolicyResult(), _FakePolicyResult())  # type: ignore[assignment]


@dataclass(frozen=True)
class _FakeRunContract:
    run_contract_id: str = _RUN_CONTRACT_ID
    run_contract_version: str = "financial-negative-list-collection-run-contract-v3"
    response_boundary_policy_id: str = _POLICY_ID
    response_boundary_policy_file_sha256: str = _POLICY_FILE_SHA256
    response_boundary_reason_code: str = "FNLD-013"


@dataclass(frozen=True)
class _FakeRunContractResult:
    run_contract_id: str = _RUN_CONTRACT_ID
    run_contract_version: str = "financial-negative-list-collection-run-contract-v3"


collector.verify_run_contract_file = lambda **kwargs: (_FakeRunContract(), _FakeRunContractResult())  # type: ignore[assignment]


def test_v3_finalization_allows_only_source_omitted_end_type(tmp_path: Path) -> None:
    request_id = "e" * 64
    receipt = {
        "schema_version": "1",
        "request_id": request_id,
        "endpoint": "balancesheet",
        "symbol": "000001.SZ",
        "ann_date": "20220422",
        "f_ann_date": "20250722",
        "end_date": "20211231",
        "report_type": 1,
        "comp_type": 1,
        "end_type": None,
        "update_flag": "1",
        "effective_disclosure_date": "20250722",
        "reason_code": "FNLD-013",
        "source_row_hash": "f" * 64,
    }
    receipt_dir = tmp_path / "response-boundary-receipts" / "balancesheet"
    receipt_dir.mkdir(parents=True)
    receipt_path = receipt_dir / f"{collector._response_boundary_receipt_identity_hash(receipt)}.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(TushareFetchError, match="invalid response-boundary receipt end_type"):
        collector._response_boundary_receipt_summary(tmp_path, request_id=request_id)

    summary = collector._response_boundary_receipt_summary(
        tmp_path,
        request_id=request_id,
        finalization_bindings=_finalization_bindings(),
    )
    assert summary["count"] == 1
    assert summary["null_end_type_receipt_count_by_endpoint"]["balancesheet"] == 1


def test_v3_finalization_does_not_allow_other_required_metadata_to_be_null(tmp_path: Path) -> None:
    request_id = "e" * 64
    receipt = {
        "schema_version": "1",
        "request_id": request_id,
        "endpoint": "income",
        "symbol": "000001.SZ",
        "ann_date": "20220422",
        "f_ann_date": "20250722",
        "end_date": "20211231",
        "report_type": None,
        "comp_type": 1,
        "end_type": None,
        "update_flag": "1",
        "effective_disclosure_date": "20250722",
        "reason_code": "FNLD-013",
        "source_row_hash": "f" * 64,
    }
    receipt_dir = tmp_path / "response-boundary-receipts" / "income"
    receipt_dir.mkdir(parents=True)
    receipt_path = receipt_dir / f"{collector._response_boundary_receipt_identity_hash(receipt)}.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(TushareFetchError, match="invalid response-boundary receipt report_type"):
        collector._response_boundary_receipt_summary(
            tmp_path,
            request_id=request_id,
            finalization_bindings=_finalization_bindings(),
        )


def _hex64(seed: str) -> str:
    return (seed * 64)[:64]


@dataclass(frozen=True)
class _FakeEndpointEntry:
    tushare_api: str
    official_doc: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class _FakeSourceEndpoints:
    balancesheet: _FakeEndpointEntry
    income: _FakeEndpointEntry
    fina_indicator: _FakeEndpointEntry
    fina_audit: _FakeEndpointEntry


@dataclass(frozen=True)
class _FakeBindings:
    candidate_pack_id: str
    candidate_pack_parquet_sha256: str
    raw_collection_dir: str
    raw_collection_request_id: str
    raw_collection_manifest_sha256: str
    raw_quality_report_sha256: str


@dataclass(frozen=True)
class _FakeWindow:
    start: str
    end: str


@dataclass(frozen=True)
class _FakeProtocol:
    protocol_id: str
    source_announcement_collection_window: _FakeWindow
    source_endpoints: _FakeSourceEndpoints
    bindings: _FakeBindings


def _setup_repo(tmp_path: Path) -> tuple[Path, _FakeProtocol]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    protocol_path = repo_root / PROTOCOL_FILE_PATH
    protocol_path.parent.mkdir(parents=True, exist_ok=True)
    protocol_path.write_text('{"protocol_id":"fixture"}\n', encoding="utf-8")

    raw_dir = repo_root / "raw-bound"
    (raw_dir / "reference").mkdir(parents=True, exist_ok=True)
    stock_basic = pl.DataFrame(
        {
            "ts_code": [
                "000001.SZ",
                "600000.SH",
                "003999.SZ",
                "900901.SH",
                "200001.SZ",
                "000001.SH",
                "430001.BJ",
            ],
            "name": ["平安银行", "浦发银行", "未来新股", "B股样本", "B股样本2", "上证指数", "北交所样本"],
            "market": ["主板", "主板", "主板", "B股", "B股", "主板", "北交所"],
            "list_date": [
                "19910403",
                "19991110",
                "20250102",
                "19930101",
                "19930101",
                "19900101",
                "20200101",
            ],
        }
    )
    stock_basic.write_parquet(raw_dir / "reference" / "stock_basic.parquet")

    protocol = _FakeProtocol(
        protocol_id=_hex64("a"),
        source_announcement_collection_window=_FakeWindow(start="2020-01-01", end="2024-12-31"),
        source_endpoints=_FakeSourceEndpoints(
            balancesheet=_FakeEndpointEntry(
                tushare_api="balancesheet",
                official_doc="https://tushare.pro/document/2?doc_id=36",
                fields=BALANCESHEET_FIELDS,
            ),
            income=_FakeEndpointEntry(
                tushare_api="income",
                official_doc="https://tushare.pro/document/2?doc_id=33",
                fields=INCOME_FIELDS,
            ),
            fina_indicator=_FakeEndpointEntry(
                tushare_api="fina_indicator",
                official_doc="https://tushare.pro/document/2?doc_id=79",
                fields=FINA_INDICATOR_FIELDS,
            ),
            fina_audit=_FakeEndpointEntry(
                tushare_api="fina_audit",
                official_doc="https://tushare.pro/document/2?doc_id=80",
                fields=FINA_AUDIT_FIELDS,
            ),
        ),
        bindings=_FakeBindings(
            candidate_pack_id=_hex64("b"),
            candidate_pack_parquet_sha256=_hex64("c"),
            raw_collection_dir="raw-bound",
            raw_collection_request_id=_hex64("d"),
            raw_collection_manifest_sha256=_hex64("e"),
            raw_quality_report_sha256=_hex64("f"),
        ),
    )
    return repo_root, protocol


def _base_tables() -> dict[str, pl.DataFrame]:
    return {
        "balancesheet": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240320",
                    "f_ann_date": "20240321",
                    "end_date": "20231231",
                    "report_type": "1",
                    "comp_type": "1",
                    "end_type": "4",
                    "money_cap": -100.0,
                    "notes_receiv": 10.0,
                    "accounts_receiv": 11.0,
                    "oth_receiv": 3.0,
                    "inventories": 8.0,
                    "goodwill": 4.0,
                    "total_assets": 200.0,
                    "st_borr": 1.0,
                    "lt_borr": 2.0,
                    "st_bonds_payable": 3.0,
                    "non_cur_liab_due_1y": 4.0,
                    "bond_payable": 5.0,
                    "total_hldr_eqy_exc_min_int": 90.0,
                    "update_flag": "0",
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240401",
                    "f_ann_date": None,
                    "end_date": "20231231",
                    "report_type": "1",
                    "comp_type": "1",
                    "end_type": "4",
                    "money_cap": -99.0,
                    "notes_receiv": 10.0,
                    "accounts_receiv": 11.0,
                    "oth_receiv": 3.0,
                    "inventories": 8.0,
                    "goodwill": 4.0,
                    "total_assets": 200.0,
                    "st_borr": 1.0,
                    "lt_borr": 2.0,
                    "st_bonds_payable": 3.0,
                    "non_cur_liab_due_1y": 4.0,
                    "bond_payable": 5.0,
                    "total_hldr_eqy_exc_min_int": 90.0,
                    "update_flag": "1",
                },
                {
                    "ts_code": "600000.SH",
                    "ann_date": None,
                    "f_ann_date": "20240302",
                    "end_date": "20231231",
                    "report_type": 1,
                    "comp_type": 1,
                    "end_type": 4,
                    "money_cap": 1.0,
                    "notes_receiv": 1.0,
                    "accounts_receiv": 1.0,
                    "oth_receiv": 1.0,
                    "inventories": 1.0,
                    "goodwill": 1.0,
                    "total_assets": 100.0,
                    "st_borr": 1.0,
                    "lt_borr": 1.0,
                    "st_bonds_payable": 1.0,
                    "non_cur_liab_due_1y": 1.0,
                    "bond_payable": 1.0,
                    "total_hldr_eqy_exc_min_int": 50.0,
                    "update_flag": "0",
                },
            ]
        ),
        "income": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240320",
                    "f_ann_date": "20240321",
                    "end_date": "20231231",
                    "report_type": "1",
                    "comp_type": "1",
                    "end_type": "4",
                    "revenue": 100.0,
                    "total_revenue": 101.0,
                    "update_flag": "0",
                },
                {
                    "ts_code": "600000.SH",
                    "ann_date": "20240322",
                    "f_ann_date": None,
                    "end_date": "20231231",
                    "report_type": 1,
                    "comp_type": 1,
                    "end_type": 4,
                    "revenue": 50.0,
                    "total_revenue": 51.0,
                    "update_flag": "0",
                },
            ]
        ),
        "fina_indicator": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240321",
                    "end_date": "20231231",
                    "interestdebt": 12.0,
                    "update_flag": "0",
                },
                {
                    "ts_code": "600000.SH",
                    "ann_date": "20240322",
                    "end_date": "20231231",
                    "interestdebt": 8.0,
                    "update_flag": "0",
                },
            ]
        ),
        "fina_audit": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240430",
                    "end_date": "20231231",
                    "audit_result": "标准无保留意见",
                    "audit_fees": 1.0,
                    "audit_agency": "A",
                    "audit_sign": "B",
                },
                {
                    "ts_code": "600000.SH",
                    "ann_date": "20240430",
                    "end_date": "20231231",
                    "audit_result": "保留意见",
                    "audit_fees": 2.0,
                    "audit_agency": "C",
                    "audit_sign": "D",
                },
            ]
        ),
    }


class _ForeignSymbolClient(FakeTushareClient):
    def query(self, api_name: str, **params: object) -> pl.DataFrame:
        frame = super().query(api_name, **params)
        if api_name == "balancesheet" and not frame.is_empty():
            return frame.with_columns(pl.lit("000002.SZ").alias("ts_code"))
        return frame


class _FailAfterNQueriesClient(FakeTushareClient):
    def __init__(self, tables: dict[str, pl.DataFrame], *, fail_after: int) -> None:
        super().__init__(tables)
        self._count = 0
        self._fail_after = fail_after

    def query(self, api_name: str, **params: object) -> pl.DataFrame:
        if self._count >= self._fail_after:
            raise RuntimeError("synthetic interrupted collection")
        self._count += 1
        return super().query(api_name, **params)


class _SecretBearingClient(FakeTushareClient):
    def __init__(self, tables: dict[str, pl.DataFrame], *, token: str) -> None:
        super().__init__(tables)
        self.api_token = token
        self.session_token = f"Bearer {token}"
        self.headers = {"Authorization": self.session_token}


def _rewrite_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _recompute_collection_id(payload: dict[str, object]) -> str:
    return collector._json_sha256({k: v for k, v in payload.items() if k != "collection_id"})


def _endpoint_fields(endpoint: str) -> tuple[str, ...]:
    mapping: dict[str, tuple[str, ...]] = {
        "balancesheet": BALANCESHEET_FIELDS,
        "income": INCOME_FIELDS,
        "fina_indicator": FINA_INDICATOR_FIELDS,
        "fina_audit": FINA_AUDIT_FIELDS,
    }
    return mapping[endpoint]


def _tamper_partition_with_rehashed_float(
    path: Path,
    *,
    endpoint: str,
    field_name: str,
    field_value: float,
) -> None:
    frame = pl.read_parquet(path)
    rows = frame.to_dicts()
    fields = _endpoint_fields(endpoint)
    for row in rows:
        row[field_name] = field_value
        payload = {field: row[field] for field in fields}
        row["source_row_hash"] = collector._json_sha256(payload)
    tampered = pl.DataFrame(rows, schema=frame.schema).select(frame.columns)
    tampered.write_parquet(path)


def test_collect_resume_and_core_semantics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    client = FakeTushareClient(_base_tables())
    staging = repo_root / "staging"

    first = collector.collect_tushare_financial_negative_list(
        client=client,
        repo_root=repo_root,
        staging_dir=staging,
    )
    calls = len(client.calls)
    second = collector.collect_tushare_financial_negative_list(
        client=client,
        repo_root=repo_root,
        staging_dir=staging,
    )

    assert first.partition_count == 12
    assert first.completed_partitions == 12
    assert first.reused_partitions == 0
    assert second.completed_partitions == 0
    assert second.reused_partitions == 12
    assert len(client.calls) == calls
    for api_name, params in client.call_params:
        assert params["start_date"] == "20200101", api_name
        assert params["end_date"] == "20241231", api_name

    bs = pl.read_parquet(staging / "partitions" / "balancesheet" / "000001_SZ.parquet")
    assert bs.height == 2
    assert bs["money_cap"].to_list()[0] < 0
    assert bs["available_at"].to_list()[0] == "2024-03-21T23:59:59+08:00"
    assert bs["source_row_hash"].n_unique() == 2
    missing_ann = pl.read_parquet(staging / "partitions" / "balancesheet" / "600000_SH.parquet")
    assert missing_ann["availability_status"].to_list() == ["missing_ann_date"]
    assert missing_ann["available_at"].to_list() == [None]
    for endpoint in ("balancesheet", "income", "fina_indicator", "fina_audit"):
        future = pl.read_parquet(staging / "partitions" / endpoint / "003999_SZ.parquet")
        assert future.is_empty()
    request_text = (staging / "collection_request.json").read_text(encoding="utf-8")
    assert "2025" not in request_text


def test_future_restatement_is_quarantined_with_sealed_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging-quarantine"
    tables = _base_tables()
    balancesheet_rows = tables["balancesheet"].to_dicts()
    balancesheet_rows.append(
        {
            "ts_code": "000001.SZ",
            "ann_date": "20240320",
            "f_ann_date": "20260429",
            "end_date": "20231231",
            "report_type": "1",
            "comp_type": "1",
            "end_type": "4",
            "money_cap": 777.0,
            "notes_receiv": 10.0,
            "accounts_receiv": 11.0,
            "oth_receiv": 3.0,
            "inventories": 8.0,
            "goodwill": 4.0,
            "total_assets": 200.0,
            "st_borr": 1.0,
            "lt_borr": 2.0,
            "st_bonds_payable": 3.0,
            "non_cur_liab_due_1y": 4.0,
            "bond_payable": 5.0,
            "total_hldr_eqy_exc_min_int": 90.0,
            "update_flag": "1",
        }
    )
    tables["balancesheet"] = pl.DataFrame(balancesheet_rows)
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(tables),
        repo_root=repo_root,
        staging_dir=staging,
    )
    kept = pl.read_parquet(staging / "partitions" / "balancesheet" / "000001_SZ.parquet")
    assert kept.height == 2
    assert 777.0 not in kept["money_cap"].to_list()

    receipt_dir = staging / "response-boundary-receipts" / "balancesheet"
    receipt_files = sorted(receipt_dir.glob("*.json"))
    assert len(receipt_files) == 1
    receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
    assert receipt["reason_code"] == "FNLD-013"
    assert receipt["f_ann_date"] == "20260429"
    assert receipt["effective_disclosure_date"] == "20260429"
    assert "money_cap" not in receipt
    assert "audit_result" not in receipt
    expected_source_row = {
        "ts_code": "000001.SZ",
        "ann_date": "20240320",
        "f_ann_date": "20260429",
        "end_date": "20231231",
        "report_type": 1,
        "comp_type": 1,
        "end_type": 4,
        "money_cap": 777.0,
        "notes_receiv": 10.0,
        "accounts_receiv": 11.0,
        "oth_receiv": 3.0,
        "inventories": 8.0,
        "goodwill": 4.0,
        "total_assets": 200.0,
        "st_borr": 1.0,
        "lt_borr": 2.0,
        "st_bonds_payable": 3.0,
        "non_cur_liab_due_1y": 4.0,
        "bond_payable": 5.0,
        "total_hldr_eqy_exc_min_int": 90.0,
        "update_flag": "1",
    }
    assert receipt["source_row_hash"] == collector._json_sha256(expected_source_row)


@pytest.mark.parametrize(
    ("endpoint", "future_row", "forbidden_payload_field"),
    [
        (
            "fina_indicator",
            {
                "ts_code": "000001.SZ",
                "ann_date": "20250315",
                "end_date": "20241231",
                "interestdebt": 999.25,
                "update_flag": "1",
            },
            "interestdebt",
        ),
        (
            "fina_audit",
            {
                "ts_code": "000001.SZ",
                "ann_date": "20250430",
                "end_date": "20241231",
                "audit_result": "future-value-must-not-persist",
                "audit_fees": 999.25,
                "audit_agency": "future-agency",
                "audit_sign": "future-sign",
            },
            "audit_result",
        ),
    ],
)
def test_future_report_period_rows_are_quarantined_without_payload_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    future_row: dict[str, object],
    forbidden_payload_field: str,
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / f"staging-future-{endpoint}"
    tables = _base_tables()
    rows = tables[endpoint].to_dicts()
    rows.append(future_row)
    tables[endpoint] = pl.DataFrame(rows)

    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(tables),
        repo_root=repo_root,
        staging_dir=staging,
    )

    kept = pl.read_parquet(staging / "partitions" / endpoint / "000001_SZ.parquet")
    assert all(value <= "20241231" for value in kept["ann_date"].drop_nulls().to_list())
    receipt_files = sorted((staging / "response-boundary-receipts" / endpoint).glob("*.json"))
    assert len(receipt_files) == 1
    receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
    assert receipt["ann_date"] == future_row["ann_date"]
    assert receipt["f_ann_date"] is None
    assert receipt["effective_disclosure_date"] == future_row["ann_date"]
    assert receipt["report_type"] is None
    assert receipt["comp_type"] is None
    assert receipt["end_type"] is None
    assert forbidden_payload_field not in receipt
    assert 999.25 not in receipt.values()
    collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


def test_response_boundary_receipt_tamper_or_missing_fails_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging-receipt-tamper"
    tables = _base_tables()
    rows = tables["income"].to_dicts()
    rows.append(
        {
            "ts_code": "000001.SZ",
            "ann_date": "20240320",
            "f_ann_date": "20260429",
            "end_date": "20231231",
            "report_type": "1",
            "comp_type": "1",
            "end_type": "4",
            "revenue": 123.0,
            "total_revenue": 124.0,
            "update_flag": "1",
        }
    )
    tables["income"] = pl.DataFrame(rows)
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(tables),
        repo_root=repo_root,
        staging_dir=staging,
    )
    receipt_dir = staging / "response-boundary-receipts" / "income"
    receipt_path = next(iter(sorted(receipt_dir.glob("*.json"))))
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["reason_code"] = "FNLD-999"
    _rewrite_json(receipt_path, payload)
    with pytest.raises(TushareFetchError, match="reason_code mismatch|content drift detected"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)

    staging_missing = repo_root / "staging-receipt-missing"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(tables),
        repo_root=repo_root,
        staging_dir=staging_missing,
    )
    next(iter(sorted((staging_missing / "response-boundary-receipts" / "income").glob("*.json")))).unlink()
    with pytest.raises(TushareFetchError, match="content drift detected"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging_missing)


def test_response_boundary_receipt_planted_or_conflicting_identity_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging-receipt-planted"
    tables = _base_tables()
    rows = tables["income"].to_dicts()
    rows.append(
        {
            "ts_code": "000001.SZ",
            "ann_date": "20240320",
            "f_ann_date": "20260429",
            "end_date": "20231231",
            "report_type": "1",
            "comp_type": "1",
            "end_type": "4",
            "revenue": 123.0,
            "total_revenue": 124.0,
            "update_flag": "1",
        }
    )
    tables["income"] = pl.DataFrame(rows)
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(tables),
        repo_root=repo_root,
        staging_dir=staging,
    )
    receipt_dir = staging / "response-boundary-receipts" / "income"
    original = json.loads(next(iter(sorted(receipt_dir.glob("*.json")))).read_text(encoding="utf-8"))

    planted = dict(original)
    planted["source_row_hash"] = "f" * 64
    planted_path = receipt_dir / "planted-conflict.json"
    _rewrite_json(planted_path, planted)
    with pytest.raises(TushareFetchError, match="filename hash mismatch|identity conflict|content drift detected"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


@pytest.mark.parametrize(
    ("field_name", "replacement", "expected_error"),
    [
        ("ann_date", "20240230", "ann_date is invalid"),
        ("effective_disclosure_date", "20260430", "effective_disclosure_date mismatch"),
        ("endpoint", "balancesheet", "endpoint directory mismatch"),
        ("symbol", "430047.BJ", "stock symbol|invalid response-boundary receipt symbol"),
        ("report_type", None, "report_type"),
        ("update_flag", "2", "update_flag"),
    ],
)
def test_response_boundary_receipt_strict_identity_fields_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    replacement: object,
    expected_error: str,
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / f"staging-receipt-{field_name}"
    tables = _base_tables()
    rows = tables["income"].to_dicts()
    rows.append(
        {
            "ts_code": "000001.SZ",
            "ann_date": "20240320",
            "f_ann_date": "20260429",
            "end_date": "20231231",
            "report_type": "1",
            "comp_type": "1",
            "end_type": "4",
            "revenue": 123.0,
            "total_revenue": 124.0,
            "update_flag": "1",
        }
    )
    tables["income"] = pl.DataFrame(rows)
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(tables),
        repo_root=repo_root,
        staging_dir=staging,
    )
    receipt_dir = staging / "response-boundary-receipts" / "income"
    receipt_path = next(iter(sorted(receipt_dir.glob("*.json"))))
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload[field_name] = replacement
    replacement_path = receipt_dir / f"{collector._response_boundary_receipt_identity_hash(payload)}.json"
    receipt_path.unlink()
    _rewrite_json(replacement_path, payload)

    with pytest.raises(ValueError, match=expected_error):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


def test_response_boundary_receipt_symlink_directory_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging-receipt-symlink"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )
    receipt_root = staging / "response-boundary-receipts"
    external = repo_root / "external-receipts"
    external.mkdir()
    receipt_root.symlink_to(external, target_is_directory=True)

    with pytest.raises(TushareFetchError, match="symlink"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


def test_no_token_leak_in_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    secret = "secret-token-should-not-appear"
    collector.collect_tushare_financial_negative_list(
        client=_SecretBearingClient(_base_tables(), token=secret),
        repo_root=repo_root,
        staging_dir=staging,
    )
    raw = secret.encode("utf-8")
    for path in staging.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".json", ".parquet"}:
            continue
        assert raw not in path.read_bytes()


def test_foreign_symbol_and_malformed_values_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    with pytest.raises(DataQualityError, match="returned another symbol"):
        collector.collect_tushare_financial_negative_list(
            client=_ForeignSymbolClient(_base_tables()),
            repo_root=repo_root,
            staging_dir=repo_root / "staging-foreign",
        )

    malformed = _base_tables()
    malformed["income"] = malformed["income"].with_columns(pl.lit("bad-date").alias("ann_date"))
    with pytest.raises(DataQualityError, match="ann_date is invalid"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(malformed),
            repo_root=repo_root,
            staging_dir=repo_root / "staging-malformed-date",
        )

    malformed_numeric = _base_tables()
    malformed_numeric["fina_indicator"] = malformed_numeric["fina_indicator"].with_columns(
        pl.lit("not-a-number").alias("interestdebt")
    )
    with pytest.raises(DataQualityError, match="is not numeric"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(malformed_numeric),
            repo_root=repo_root,
            staging_dir=repo_root / "staging-malformed-num",
        )


def test_duplicates_conflicts_and_truncation_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)

    duplicate = _base_tables()
    duplicate["balancesheet"] = duplicate["balancesheet"].vstack(duplicate["balancesheet"].head(1))
    with pytest.raises(DataQualityError, match="exact duplicate"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(duplicate),
            repo_root=repo_root,
            staging_dir=repo_root / "staging-duplicate",
        )

    conflict = _base_tables()
    conflict_row = conflict["balancesheet"].head(1).with_columns(pl.lit(-77.0).alias("money_cap"))
    conflict["balancesheet"] = pl.concat([conflict["balancesheet"], conflict_row], how="vertical")
    with pytest.raises(DataQualityError, match="semantic key\\+availability conflict"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(conflict),
            repo_root=repo_root,
            staging_dir=repo_root / "staging-conflict",
        )

    capped = _base_tables()
    capped["fina_audit"] = pl.concat([capped["fina_audit"].head(1)] * 600, how="vertical")
    with pytest.raises(DataQualityError, match="may be truncated"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(capped),
            repo_root=repo_root,
            staging_dir=repo_root / "staging-capped",
        )


def test_distinct_update_flag_versions_are_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    rows = _base_tables()["balancesheet"].to_dicts()
    base = rows[0].copy()
    base["update_flag"] = "0"
    revised = rows[0].copy()
    revised["update_flag"] = "1"
    update_versions = _base_tables()
    update_versions["balancesheet"] = pl.DataFrame([base, revised, rows[2]])
    staging = repo_root / "staging-update-flag-versions"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(update_versions),
        repo_root=repo_root,
        staging_dir=staging,
    )
    balancesheet = pl.read_parquet(staging / "partitions" / "balancesheet" / "000001_SZ.parquet")
    assert balancesheet.height == 2
    assert sorted(balancesheet["update_flag"].to_list()) == ["0", "1"]
    assert balancesheet["source_row_hash"].n_unique() == 2


def test_same_complete_version_key_with_different_payload_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    rows = _base_tables()["balancesheet"].to_dicts()
    conflicted = rows[0].copy()
    conflicted["money_cap"] = -777.0
    conflict = _base_tables()
    conflict["balancesheet"] = pl.DataFrame([rows[0], conflicted, rows[2]])
    with pytest.raises(DataQualityError, match="semantic key\\+availability conflict"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(conflict),
            repo_root=repo_root,
            staging_dir=repo_root / "staging-complete-version-conflict",
        )


def test_manifest_request_partition_tamper_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )

    req_path = staging / "collection_request.json"
    req = json.loads(req_path.read_text(encoding="utf-8"))
    req["requested_symbols"] = 999
    req_path.write_text(json.dumps(req, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(TushareFetchError, match="different financial-negative-list request"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(_base_tables()),
            repo_root=repo_root,
            staging_dir=staging,
        )


def test_manifest_partition_shape_and_hash_tamper_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )

    removed = staging / "partitions" / "income" / "600000_SH.parquet"
    removed.unlink()
    with pytest.raises(TushareFetchError, match="incomplete or contains extras"):
        collector.verify_financial_negative_list_collection(
            repo_root=repo_root,
            staging_dir=staging,
        )

    staging_ok = repo_root / "staging-ok"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging_ok,
    )
    extra = staging_ok / "partitions" / "income" / "999999_SZ.parquet"
    pl.DataFrame(schema={"a": pl.Int64}).write_parquet(extra)
    with pytest.raises(TushareFetchError, match="incomplete or contains extras"):
        collector.verify_financial_negative_list_collection(
            repo_root=repo_root,
            staging_dir=staging_ok,
        )


def test_wrong_protocol_binding_and_unsupported_2025_shape_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    bad_protocol = _FakeProtocol(
        protocol_id=protocol.protocol_id,
        source_announcement_collection_window=_FakeWindow(start="2020-01-01", end="2025-12-31"),
        source_endpoints=protocol.source_endpoints,
        bindings=protocol.bindings,
    )
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: bad_protocol)
    with pytest.raises(TushareFetchError, match="announcement window binding mismatch"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(_base_tables()),
            repo_root=repo_root,
            staging_dir=repo_root / "staging-bad-protocol",
        )

    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    with_2025 = _base_tables()
    with_2025["balancesheet"] = with_2025["balancesheet"].with_columns(pl.lit("20250101").alias("ann_date"))
    with pytest.raises(DataQualityError, match="response boundary rejects"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(with_2025),
            repo_root=repo_root,
            staging_dir=repo_root / "staging-2025",
        )


def test_completed_rerun_is_immutable_and_verify_api_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    first = collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )
    manifest_before = (staging / "collection_manifest.json").read_bytes()

    verified = collector.verify_financial_negative_list_collection(
        repo_root=repo_root,
        staging_dir=staging,
    )
    second = collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )
    assert verified.request_id == first.request_id
    assert second.reused_partitions == first.partition_count
    assert second.completed_partitions == 0
    assert (staging / "collection_manifest.json").read_bytes() == manifest_before


def test_interrupted_resume_rejects_numeric_tamper_without_hash_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    with pytest.raises(RuntimeError, match="synthetic interrupted collection"):
        collector.collect_tushare_financial_negative_list(
            client=_FailAfterNQueriesClient(_base_tables(), fail_after=1),
            repo_root=repo_root,
            staging_dir=staging,
        )
    assert not (staging / "collection_manifest.json").exists()
    path = staging / "partitions" / "balancesheet" / "000001_SZ.parquet"
    tampered = pl.read_parquet(path).with_columns((pl.col("money_cap") + 999.0).alias("money_cap"))
    tampered.write_parquet(path)
    with pytest.raises(DataQualityError, match="source_row_hash mismatch"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(_base_tables()),
            repo_root=repo_root,
            staging_dir=staging,
        )


@pytest.mark.parametrize("bad_value", [math.nan, math.inf])
def test_interrupted_resume_rejects_non_finite_float_tamper_with_hash_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_value: float
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    with pytest.raises(RuntimeError, match="synthetic interrupted collection"):
        collector.collect_tushare_financial_negative_list(
            client=_FailAfterNQueriesClient(_base_tables(), fail_after=1),
            repo_root=repo_root,
            staging_dir=staging,
        )
    path = staging / "partitions" / "balancesheet" / "000001_SZ.parquet"
    _tamper_partition_with_rehashed_float(
        path,
        endpoint="balancesheet",
        field_name="money_cap",
        field_value=bad_value,
    )
    with pytest.raises(DataQualityError, match="not finite"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(_base_tables()),
            repo_root=repo_root,
            staging_dir=staging,
        )


def test_tampered_source_row_hash_fails_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )
    path = staging / "partitions" / "income" / "000001_SZ.parquet"
    tampered = pl.read_parquet(path).with_columns(pl.lit("0" * 64).alias("source_row_hash"))
    tampered.write_parquet(path)
    with pytest.raises(DataQualityError, match="source_row_hash mismatch"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


def test_post_window_empty_partition_wrong_schema_or_dtype_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )
    bad_path = staging / "partitions" / "fina_indicator" / "003999_SZ.parquet"
    pl.DataFrame(schema={"ts_code": pl.Int64}).write_parquet(bad_path)
    with pytest.raises(DataQualityError, match="columns are noncanonical|schema is noncanonical"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


def test_hyphenated_stored_date_fails_partition_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )
    path = staging / "partitions" / "income" / "600000_SH.parquet"
    tampered = pl.read_parquet(path).with_columns(pl.lit("2024-03-22").alias("ann_date"))
    tampered.write_parquet(path)
    with pytest.raises(DataQualityError, match="ann_date is invalid|ann_date formatting mismatch"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


def test_invalid_calendar_date_is_data_quality_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    malformed = _base_tables()
    malformed["income"] = malformed["income"].with_columns(pl.lit("20241340").alias("ann_date"))
    with pytest.raises(DataQualityError, match="ann_date is invalid"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(malformed),
            repo_root=repo_root,
            staging_dir=repo_root / "staging-invalid-calendar-date",
        )


@pytest.mark.parametrize("bad_value", [math.nan, math.inf])
def test_verify_rejects_non_finite_float_tamper_with_hash_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_value: float
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )
    path = staging / "partitions" / "income" / "000001_SZ.parquet"
    _tamper_partition_with_rehashed_float(
        path,
        endpoint="income",
        field_name="revenue",
        field_value=bad_value,
    )
    with pytest.raises(DataQualityError, match="not finite"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


def test_staging_escape_or_symlink_path_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)

    with pytest.raises(TushareFetchError, match="staging directory must stay within repo_root"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(_base_tables()),
            repo_root=repo_root,
            staging_dir=tmp_path / "outside-staging",
        )

    staging_real = repo_root / "staging-real"
    staging_real.mkdir(parents=True, exist_ok=True)
    staging_link = repo_root / "staging-link"
    staging_link.symlink_to(staging_real, target_is_directory=True)
    with pytest.raises(TushareFetchError, match="must not contain symlink"):
        collector.collect_tushare_financial_negative_list(
            client=FakeTushareClient(_base_tables()),
            repo_root=repo_root,
            staging_dir=staging_link,
        )


def test_manifest_or_partition_symlink_fails_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )

    manifest_external = tmp_path / "external-manifest.json"
    manifest_external.write_text("{}", encoding="utf-8")
    manifest_path = staging / "source_manifest.json"
    manifest_path.unlink()
    manifest_path.symlink_to(manifest_external)
    with pytest.raises(TushareFetchError, match="source_manifest.json must not be a symlink path"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)

    staging_partition = repo_root / "staging-partition-symlink"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging_partition,
    )
    external_parquet = tmp_path / "external.parquet"
    pl.DataFrame({"x": [1]}).write_parquet(external_parquet)
    partition_path = staging_partition / "partitions" / "income" / "000001_SZ.parquet"
    partition_path.unlink()
    partition_path.symlink_to(external_parquet)
    with pytest.raises(TushareFetchError, match="must not be a symlink path"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging_partition)


def test_source_manifest_tamper_with_updated_hash_still_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )
    source_path = staging / "source_manifest.json"
    source_manifest = json.loads(source_path.read_text(encoding="utf-8"))
    source_manifest["endpoints"]["income"]["rows"] += 1
    _rewrite_json(source_path, source_manifest)
    manifest_path = staging / "collection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_manifest_sha256"] = collector._sha256_file(source_path)
    manifest["collection_id"] = _recompute_collection_id(manifest)
    _rewrite_json(manifest_path, manifest)
    with pytest.raises(TushareFetchError, match="source manifest content drift detected"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


def test_quality_report_tamper_with_updated_hash_still_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )
    quality_path = staging / "quality_report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["sources"]["balancesheet"]["rows"] += 1
    _rewrite_json(quality_path, quality)
    manifest_path = staging / "collection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality_report_sha256"] = collector._sha256_file(quality_path)
    manifest["collection_id"] = _recompute_collection_id(manifest)
    _rewrite_json(manifest_path, manifest)
    with pytest.raises(TushareFetchError, match="quality report content drift detected"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


@pytest.mark.parametrize(
    ("field_name", "field_value", "recompute_id"),
    [
        ("protocol_id", _hex64("9"), True),
        ("candidate_pack_id", _hex64("8"), True),
        ("symbols_sha256", _hex64("7"), True),
        ("collected_at", "2024-01-01T00:00:00Z", True),
        ("collection_id", _hex64("6"), False),
    ],
)
def test_manifest_binding_protocol_symbol_collected_at_or_collection_id_tamper_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    field_value: str,
    recompute_id: bool,
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / f"staging-{field_name}"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )
    manifest_path = staging / "collection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field_name] = field_value
    if recompute_id:
        manifest["collection_id"] = _recompute_collection_id(manifest)
    _rewrite_json(manifest_path, manifest)
    with pytest.raises(TushareFetchError):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


def test_partition_tamper_after_completion_fails_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )
    path = staging / "partitions" / "fina_audit" / "000001_SZ.parquet"
    frame = pl.read_parquet(path).with_columns((pl.col("audit_fees") + 3.0).alias("audit_fees"))
    rows = frame.to_dicts()
    for row in rows:
        payload = {field: row[field] for field in FINA_AUDIT_FIELDS}
        row["source_row_hash"] = collector._json_sha256(payload)
    tampered = pl.DataFrame(rows, schema=frame.schema).select(frame.columns)
    tampered.write_parquet(path)
    with pytest.raises(TushareFetchError, match="source manifest content drift detected"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


def test_collection_id_is_canonical_self_seal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
    )
    manifest_path = staging / "collection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_collection_id = _recompute_collection_id(manifest)
    assert manifest["collection_id"] == expected_collection_id


def test_collection_authorization_id_is_bound_across_all_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging-auth"
    auth_id = "2" * 64
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
        collection_authorization_id=auth_id,
    )
    request = json.loads((staging / "collection_request.json").read_text(encoding="utf-8"))
    source_manifest = json.loads((staging / "source_manifest.json").read_text(encoding="utf-8"))
    quality_report = json.loads((staging / "quality_report.json").read_text(encoding="utf-8"))
    manifest = json.loads((staging / "collection_manifest.json").read_text(encoding="utf-8"))
    assert request["collection_authorization_id"] == auth_id
    assert source_manifest["collection_authorization_id"] == auth_id
    assert quality_report["collection_authorization_id"] == auth_id
    assert manifest["collection_authorization_id"] == auth_id


def test_collection_authorization_id_tamper_fails_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    staging = repo_root / "staging-auth-tamper"
    collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=staging,
        collection_authorization_id="3" * 64,
    )
    quality_path = staging / "quality_report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["collection_authorization_id"] = "4" * 64
    _rewrite_json(quality_path, quality)
    with pytest.raises(TushareFetchError, match="quality report content drift detected"):
        collector.verify_financial_negative_list_collection(repo_root=repo_root, staging_dir=staging)


def test_collect_progress_callback_receives_partition_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    seen: list[tuple[str, int, int, int, int]] = []

    def _progress(endpoint: str, done: int, total: int, endpoint_done: int, endpoint_total: int) -> None:
        seen.append((endpoint, done, total, endpoint_done, endpoint_total))

    result = collector.collect_tushare_financial_negative_list(
        client=FakeTushareClient(_base_tables()),
        repo_root=repo_root,
        staging_dir=repo_root / "staging-progress",
        collection_authorization_id="5" * 64,
        progress_callback=_progress,
    )
    assert result.partition_count == 12
    assert seen
    assert seen[-1][1] == 12
    assert seen[-1][2] == 12
    assert seen[-1][0] == "fina_audit"
    assert seen[-1][3] == seen[-1][4]


@pytest.mark.parametrize(
    ("override", "expected_error"),
    [
        ({"verified_run_contract_id": None}, "verified v3 run-contract/policy bindings are required"),
        (
            {"verified_run_contract_version": "financial-negative-list-collection-run-contract-v1"},
            "requires verified v3 run-contract/policy bindings",
        ),
        (
            {"verified_response_boundary_policy_file_sha256": "6" * 64},
            "policy_file_sha256 mismatch against sealed v3 run contract",
        ),
    ],
)
def test_invalid_v3_bindings_fail_before_any_api_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, object],
    expected_error: str,
) -> None:
    repo_root, protocol = _setup_repo(tmp_path)
    monkeypatch.setattr(collector, "verify_protocol_file", lambda _root: protocol)
    client = FakeTushareClient(_base_tables())
    kwargs: dict[str, object] = {
        "client": client,
        "repo_root": repo_root,
        "staging_dir": repo_root / "staging-invalid-binding",
        **override,
    }

    with pytest.raises(TushareFetchError, match=expected_error):
        collector.collect_tushare_financial_negative_list(**kwargs)
    assert client.calls == []
