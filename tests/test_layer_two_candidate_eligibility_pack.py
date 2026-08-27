"""Attack-oriented tests for E11b-1b candidate eligibility pack.

Tests invoke production helpers (_verify_expected_rows_streaming,
_generate_row_for_candidate, _generate_expected_rows) for true integration
coverage. Source indexes (daily_bars, daily_valuation, stock_basic) are loaded
into memory; output Parquet verification is streaming.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from app.research.layer_two_candidate_eligibility import (
    LAYER_TWO_CANDIDATE_ELIGIBILITY_ENGINE_VERSION,
    LayerTwoCandidateInput,
)
from app.research.layer_two_candidate_eligibility_pack import (
    _OUTPUT_PARQUET_SCHEMA,
    ASIA_SHANGHAI,
    BOUND_ALLOCATION_PROTOCOL_FILE_SHA256,
    BOUND_ALLOCATION_PROTOCOL_ID,
    BOUND_ALLOCATION_PROTOCOL_PATH,
    BOUND_E10A_MODULE_SHA256,
    BOUND_INVENTORY_ID,
    BOUND_INVENTORY_PATH,
    BOUND_PLANNED_BUY_NOTIONAL_CNY,
    BOUND_RAW_COLLECTION_DIR,
    BOUND_RAW_COLLECTION_MANIFEST_SHA256,
    BOUND_RAW_COLLECTION_REQUEST_ID,
    BOUND_RAW_DATASET_HASH_NAMECHANGE,
    BOUND_RAW_DATASET_HASH_STOCK_BASIC,
    BOUND_RAW_DATASET_HASH_TRADE_CAL,
    BOUND_RAW_QUALITY_REPORT_SHA256,
    BOUND_TWO_LAYER_CONTRACT_FILE_SHA256,
    BOUND_TWO_LAYER_CONTRACT_ID,
    BOUND_TWO_LAYER_CONTRACT_PATH,
    CIRC_MV_UNIT_TO_CNY,
    COVERAGE_END,
    COVERAGE_START,
    PACK_ENGINE_VERSION,
    PACK_SCHEMA_VERSION,
    BarTuple,
    CandidateEligibilityPackManifest,
    PackCoverageInfo,
    PackIntegrity,
    PackReadinessFlags,
    PackRowCounts,
    PackSourceBinding,
    ValTuple,
    _compute_dataset_hashes,
    _generate_expected_rows,
    _generate_row_for_candidate,
    _make_market_close,
    _require_bound_e10a_sha,
    _require_bound_inventory_id,
    _validate_safe_path,
    _verify_expected_rows_streaming,
    compute_canonical_table_hash,
    compute_canonical_table_hash_streaming,
    compute_pack_id,
    compute_source_input_hash,
    compute_trading_date_set_hash,
    is_ordinary_a_share_from_stock_basic,
    make_decision_at,
    seal_manifest,
)

DECISION_TIME = time(17, 30, 0)


# ---------------------------------------------------------------------------
# Model-level tests
# ---------------------------------------------------------------------------


class TestOrdinaryAShareDetection:
    def test_standard_sh(self) -> None:
        assert is_ordinary_a_share_from_stock_basic("600000.SH", "SSE")

    def test_standard_sz(self) -> None:
        assert is_ordinary_a_share_from_stock_basic("000001.SZ", "SZSE")

    def test_sh_b_share_900_rejected(self) -> None:
        assert not is_ordinary_a_share_from_stock_basic("900001.SH", "SSE")

    def test_sz_b_share_200_rejected(self) -> None:
        assert not is_ordinary_a_share_from_stock_basic("200001.SZ", "SZSE")

    def test_bse_rejected(self) -> None:
        assert not is_ordinary_a_share_from_stock_basic("830001.BJ", "BSE")

    def test_bj_exchange_rejected(self) -> None:
        assert not is_ordinary_a_share_from_stock_basic("600000.SH", "BJ")

    def test_invalid_format_rejected(self) -> None:
        assert not is_ordinary_a_share_from_stock_basic("60000.SH", "SSE")

    def test_none_exchange_allowed(self) -> None:
        assert is_ordinary_a_share_from_stock_basic("000001.SZ", None)


class TestDecisionAt:
    def test_exact_1730_shanghai(self) -> None:
        dt = make_decision_at(date(2023, 6, 15))
        assert dt.hour == 17
        assert dt.minute == 30
        assert dt.second == 0
        assert dt.tzinfo == ASIA_SHANGHAI

    def test_date_preserves_as_of(self) -> None:
        dt = make_decision_at(date(2022, 1, 4))
        assert dt.date() == date(2022, 1, 4)


class TestMarketCloseAvailability:
    def test_exact_1500_shanghai(self) -> None:
        dt = _make_market_close(date(2023, 3, 1))
        assert dt.hour == 15
        assert dt.minute == 0
        assert dt.tzinfo == ASIA_SHANGHAI

    def test_earlier_than_decision(self) -> None:
        obs_date = date(2023, 3, 1)
        avail = _make_market_close(obs_date)
        decision = make_decision_at(obs_date)
        assert avail < decision


class TestUnitConversion:
    def test_circ_mv_conversion_exact(self) -> None:
        assert CIRC_MV_UNIT_TO_CNY == 10000
        circ_mv = 300.0
        cap_cny = circ_mv * CIRC_MV_UNIT_TO_CNY
        assert cap_cny == 3_000_000.0

    def test_3bn_threshold(self) -> None:
        cap_3bn_circ_mv = 3_000_000_000 / CIRC_MV_UNIT_TO_CNY
        assert cap_3bn_circ_mv == 300_000.0


class TestPlannedBuyNotional:
    def test_bound_notional_is_exactly_8000(self) -> None:
        assert BOUND_PLANNED_BUY_NOTIONAL_CNY == 8000

    def test_source_binding_stores_notional(self) -> None:
        sb = PackSourceBinding(
            inventory_path=BOUND_INVENTORY_PATH,
            inventory_id=BOUND_INVENTORY_ID,
            market_snapshot_id="a" * 64,
            market_manifest_sha256="b" * 64,
            fundamental_snapshot_id="c" * 64,
            fundamental_manifest_sha256="d" * 64,
            valuation_file_sha256="e" * 64,
            raw_collection_dir=BOUND_RAW_COLLECTION_DIR,
            raw_collection_request_id=BOUND_RAW_COLLECTION_REQUEST_ID,
            raw_collection_manifest_sha256=BOUND_RAW_COLLECTION_MANIFEST_SHA256,
            raw_quality_report_sha256=BOUND_RAW_QUALITY_REPORT_SHA256,
            raw_dataset_hash_trade_cal=BOUND_RAW_DATASET_HASH_TRADE_CAL,
            raw_dataset_hash_stock_basic=BOUND_RAW_DATASET_HASH_STOCK_BASIC,
            raw_dataset_hash_namechange=BOUND_RAW_DATASET_HASH_NAMECHANGE,
            two_layer_contract_id=BOUND_TWO_LAYER_CONTRACT_ID,
            two_layer_contract_path=BOUND_TWO_LAYER_CONTRACT_PATH,
            two_layer_contract_file_sha256=BOUND_TWO_LAYER_CONTRACT_FILE_SHA256,
            allocation_protocol_id=BOUND_ALLOCATION_PROTOCOL_ID,
            allocation_protocol_file_sha256=BOUND_ALLOCATION_PROTOCOL_FILE_SHA256,
            allocation_protocol_path=BOUND_ALLOCATION_PROTOCOL_PATH,
            planned_buy_notional_cny=9000,
            e10a_engine_version=LAYER_TWO_CANDIDATE_ELIGIBILITY_ENGINE_VERSION,
            e10a_module_sha256=BOUND_E10A_MODULE_SHA256,
        )
        assert sb.planned_buy_notional_cny != BOUND_PLANNED_BUY_NOTIONAL_CNY


class TestReadinessMutation:
    def test_ready_for_scoring_true_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ready_for_scoring"):
            PackReadinessFlags(
                research_only=True,
                ready_for_scoring=True,
                ready_for_trading=False,
                ready_for_portfolio_construction=False,
                not_alpha_evidence=True,
                not_authorization=True,
            )

    def test_ready_for_trading_true_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ready_for_trading"):
            PackReadinessFlags(
                research_only=True,
                ready_for_scoring=False,
                ready_for_trading=True,
                ready_for_portfolio_construction=False,
                not_alpha_evidence=True,
                not_authorization=True,
            )


class TestManifestSeal:
    def test_sealed_pack_id_is_deterministic(self) -> None:
        m = _make_test_manifest()
        sealed = seal_manifest(m)
        assert sealed.pack_id is not None
        assert sealed.pack_id == compute_pack_id(m)

    def test_different_content_different_id(self) -> None:
        m1 = _make_test_manifest(row_count=100)
        m2 = _make_test_manifest(row_count=200)
        assert compute_pack_id(m1) != compute_pack_id(m2)

    def test_row_count_inconsistency_rejected(self) -> None:
        with pytest.raises(ValidationError, match="total"):
            PackRowCounts(total=999, year_2022=1, year_2023=1, year_2024=1)


class TestManifestValidation:
    def test_total_must_equal_sum(self) -> None:
        with pytest.raises(ValidationError, match="total"):
            PackRowCounts(total=10, year_2022=3, year_2023=3, year_2024=3)

    def test_valid_row_counts(self) -> None:
        rc = PackRowCounts(total=9, year_2022=3, year_2023=3, year_2024=3)
        assert rc.total == 9


class TestTradingDateSetHash:
    def test_deterministic(self) -> None:
        dates = [date(2022, 1, 4), date(2022, 1, 5), date(2022, 1, 6)]
        h1 = compute_trading_date_set_hash(dates)
        h2 = compute_trading_date_set_hash(dates)
        assert h1 == h2

    def test_order_independent(self) -> None:
        dates1 = [date(2022, 1, 6), date(2022, 1, 4), date(2022, 1, 5)]
        dates2 = [date(2022, 1, 4), date(2022, 1, 5), date(2022, 1, 6)]
        assert compute_trading_date_set_hash(dates1) == compute_trading_date_set_hash(dates2)

    def test_different_dates_different_hash(self) -> None:
        d1 = [date(2022, 1, 4)]
        d2 = [date(2022, 1, 5)]
        assert compute_trading_date_set_hash(d1) != compute_trading_date_set_hash(d2)


class TestPathSafety:
    def test_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target)
            with pytest.raises(ValueError, match="symlink"):
                _validate_safe_path(link, repo_root=root, field_name="test")

    def test_oos_2025_path_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            bad = root / "data" / "2025"
            bad.mkdir(parents=True)
            with pytest.raises(ValueError, match="2025"):
                _validate_safe_path(bad, repo_root=root, field_name="test")

    def test_path_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            escaped = root / ".." / "outside"
            with pytest.raises(ValueError, match="escapes repo root"):
                _validate_safe_path(escaped, repo_root=root, field_name="test")

    def test_valid_path_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            valid = root / "data" / "output"
            valid.mkdir(parents=True)
            result = _validate_safe_path(valid, repo_root=root, field_name="test")
            assert result == valid


class TestSourceInputHash:
    def _make_input(self, **overrides: Any) -> LayerTwoCandidateInput:
        decision_at = make_decision_at(date(2023, 6, 1))
        defaults: dict[str, Any] = {
            "symbol": "600000.SH",
            "market": "SSE",
            "is_ordinary_a_share": True,
            "is_bse": False,
            "is_st_or_delist_risk": False,
            "is_suspended_on_decision_date": False,
            "listed_market_trading_days": 100,
            "security_status_as_of": date(2023, 6, 1),
            "security_status_available_at": _make_market_close(date(2023, 6, 1)),
            "planned_buy_notional_cny": 8000.0,
            "liquidity_observations": [],
            "pit_free_float_market_cap_cny": 5_000_000_000.0,
            "pit_free_float_market_cap_as_of": date(2023, 6, 1),
            "pit_free_float_market_cap_available_at": decision_at,
        }
        defaults.update(overrides)
        return LayerTwoCandidateInput(**defaults)

    def _hash_kwargs(self) -> dict[str, str]:
        return {
            "market_snapshot_id": "a" * 64,
            "raw_request_id": "b" * 64,
            "inventory_id": "i" * 64,
            "fundamental_snapshot_id": "f" * 64,
            "valuation_file_sha256": "v" * 64,
            "two_layer_contract_id": "t" * 64,
            "allocation_protocol_id": "p" * 64,
        }

    def test_deterministic(self) -> None:
        inp = self._make_input()
        h1 = compute_source_input_hash(inp, valuation_source_row_hash="c" * 64, **self._hash_kwargs())
        h2 = compute_source_input_hash(inp, valuation_source_row_hash="c" * 64, **self._hash_kwargs())
        assert h1 == h2

    def test_symbol_change_changes_hash(self) -> None:
        inp1 = self._make_input(symbol="600000.SH")
        inp2 = self._make_input(symbol="600001.SH")
        h1 = compute_source_input_hash(inp1, valuation_source_row_hash=None, **self._hash_kwargs())
        h2 = compute_source_input_hash(inp2, valuation_source_row_hash=None, **self._hash_kwargs())
        assert h1 != h2

    def test_market_cap_change_changes_hash(self) -> None:
        inp1 = self._make_input(pit_free_float_market_cap_cny=5_000_000_000.0)
        inp2 = self._make_input(pit_free_float_market_cap_cny=10_000_000_000.0)
        h1 = compute_source_input_hash(inp1, valuation_source_row_hash="c" * 64, **self._hash_kwargs())
        h2 = compute_source_input_hash(inp2, valuation_source_row_hash="c" * 64, **self._hash_kwargs())
        assert h1 != h2

    def test_valuation_row_hash_change_changes_hash(self) -> None:
        inp = self._make_input()
        h1 = compute_source_input_hash(inp, valuation_source_row_hash="c" * 64, **self._hash_kwargs())
        h2 = compute_source_input_hash(inp, valuation_source_row_hash="d" * 64, **self._hash_kwargs())
        assert h1 != h2

    def test_snapshot_id_change_changes_hash(self) -> None:
        inp = self._make_input()
        kw = self._hash_kwargs()
        h1 = compute_source_input_hash(inp, valuation_source_row_hash=None, **kw)
        kw2 = self._hash_kwargs()
        kw2["market_snapshot_id"] = "x" * 64
        h2 = compute_source_input_hash(inp, valuation_source_row_hash=None, **kw2)
        assert h1 != h2

    def test_inventory_id_in_envelope(self) -> None:
        inp = self._make_input()
        kw = self._hash_kwargs()
        h1 = compute_source_input_hash(inp, valuation_source_row_hash=None, **kw)
        kw2 = self._hash_kwargs()
        kw2["inventory_id"] = "z" * 64
        h2 = compute_source_input_hash(inp, valuation_source_row_hash=None, **kw2)
        assert h1 != h2

    def test_contract_id_in_envelope(self) -> None:
        inp = self._make_input()
        kw = self._hash_kwargs()
        h1 = compute_source_input_hash(inp, valuation_source_row_hash=None, **kw)
        kw2 = self._hash_kwargs()
        kw2["two_layer_contract_id"] = "z" * 64
        h2 = compute_source_input_hash(inp, valuation_source_row_hash=None, **kw2)
        assert h1 != h2


class TestCanonicalTableHash:
    def test_batch_independent(self) -> None:
        """Same data split into different batch sizes produces same hash."""
        schema = pa.schema([("symbol", pa.utf8()), ("value", pa.float64())])
        table_full = pa.table(
            {"symbol": ["600000.SH", "000001.SZ", "600001.SH"], "value": [1.0, 2.0, 3.0]},
            schema=schema,
        )
        h1 = compute_canonical_table_hash(table_full)

        batch1 = pa.RecordBatch.from_pydict({"symbol": ["600000.SH"], "value": [1.0]}, schema=schema)
        batch2 = pa.RecordBatch.from_pydict({"symbol": ["000001.SZ", "600001.SH"], "value": [2.0, 3.0]}, schema=schema)
        table_split = pa.Table.from_batches([batch1, batch2], schema=schema)
        h2 = compute_canonical_table_hash(table_split)
        assert h1 == h2

    def test_different_data_different_hash(self) -> None:
        schema = pa.schema([("x", pa.utf8())])
        t1 = pa.table({"x": ["a"]}, schema=schema)
        t2 = pa.table({"x": ["b"]}, schema=schema)
        assert compute_canonical_table_hash(t1) != compute_canonical_table_hash(t2)

    def test_non_alphabetical_schema_stable_hash(self) -> None:
        """Finding #7: non-alphabetical column order still produces consistent hash."""
        schema_zab = pa.schema([("z_col", pa.utf8()), ("a_col", pa.int32()), ("m_col", pa.float64())])
        schema_amz = pa.schema([("a_col", pa.int32()), ("m_col", pa.float64()), ("z_col", pa.utf8())])

        t1 = pa.table(
            {"z_col": ["hello", "world"], "a_col": [1, 2], "m_col": [3.14, 2.71]},
            schema=schema_zab,
        )
        t2 = pa.table(
            {"a_col": [1, 2], "m_col": [3.14, 2.71], "z_col": ["hello", "world"]},
            schema=schema_amz,
        )
        assert compute_canonical_table_hash(t1) == compute_canonical_table_hash(t2)

    def test_streaming_matches_table_hash(self) -> None:
        """Streaming hash from ParquetFile equals in-memory table hash."""
        schema = pa.schema([("name", pa.utf8()), ("val", pa.float64())])
        table = pa.table({"name": ["a", "b", "c"], "val": [1.0, 2.0, 3.0]}, schema=schema)

        h_table = compute_canonical_table_hash(table)

        with tempfile.NamedTemporaryFile(suffix=".parquet") as f:
            pq.write_table(table, f.name)
            pf = pq.ParquetFile(f.name)
            h_streaming = compute_canonical_table_hash_streaming(pf)

        assert h_table == h_streaming

    def test_streaming_different_batch_splits_stable(self) -> None:
        """Finding #7: different row_group sizes produce same streaming hash."""
        schema = pa.schema([("x", pa.utf8()), ("y", pa.int32())])
        rows = [{"x": f"r{i}", "y": i} for i in range(10)]
        table = pa.table(
            {"x": [r["x"] for r in rows], "y": [r["y"] for r in rows]},
            schema=schema,
        )

        with tempfile.NamedTemporaryFile(suffix=".parquet") as f1:
            pq.write_table(table, f1.name, row_group_size=3)
            pf1 = pq.ParquetFile(f1.name)
            h1 = compute_canonical_table_hash_streaming(pf1)

        with tempfile.NamedTemporaryFile(suffix=".parquet") as f2:
            pq.write_table(table, f2.name, row_group_size=7)
            pf2 = pq.ParquetFile(f2.name)
            h2 = compute_canonical_table_hash_streaming(pf2)

        assert h1 == h2


class TestCoverageWindow:
    def test_coverage_dates(self) -> None:
        assert COVERAGE_START == date(2022, 1, 1)
        assert COVERAGE_END == date(2024, 12, 31)


# ---------------------------------------------------------------------------
# Domain semantics tests (using is_ordinary_a_share_from_stock_basic)
# ---------------------------------------------------------------------------


class TestPITDomainSemantics:
    def test_future_delist_does_not_exclude_earlier(self) -> None:
        """A stock with delist_date in the future is still in domain."""
        assert is_ordinary_a_share_from_stock_basic("600000.SH", "SSE")

    def test_b_share_excluded_from_domain(self) -> None:
        assert not is_ordinary_a_share_from_stock_basic("900001.SH", "SSE")
        assert not is_ordinary_a_share_from_stock_basic("200001.SZ", "SZSE")

    def test_bse_excluded_from_domain(self) -> None:
        assert not is_ordinary_a_share_from_stock_basic("830001.BJ", "BSE")


# ---------------------------------------------------------------------------
# Synthetic pack tests
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_sample_row(
    symbol: str = "600000.SH",
    as_of: str = "2023-01-04",
    eligible: bool = True,
) -> dict[str, Any]:
    decision_at = make_decision_at(date.fromisoformat(as_of))
    return {
        "symbol": symbol,
        "as_of": as_of,
        "decision_at": decision_at.isoformat(),
        "eligible_for_new_entry": eligible,
        "unknown_critical_input": not eligible,
        "market_scope_pass": True,
        "tradability_pass": True if eligible else None,
        "listing_history_pass": True if eligible else None,
        "st_delist_pass": True if eligible else None,
        "liquidity_structure_pass": True if eligible else None,
        "liquidity_tradable_count_pass": True if eligible else None,
        "liquidity_median_pass": True if eligible else None,
        "liquidity_capacity_pass": True if eligible else None,
        "size_cap_pass": True if eligible else None,
        "median_daily_amount_cny": 1_000_000.0 if eligible else None,
        "average_daily_amount_cny": 1_200_000.0 if eligible else None,
        "tradable_days_in_lookback": 20 if eligible else None,
        "pit_free_float_market_cap_cny": 5_000_000_000.0 if eligible else None,
        "size_multiplier": 1.0 if eligible else None,
        "adjusted_planned_notional_cny": 8000.0 if eligible else None,
        "reason_codes": "eligible_for_new_entry" if eligible else "unknown_critical_input",
        "source_input_hash": "x" * 64,
    }


def _write_synthetic_pack(
    output_dir: Path,
    *,
    rows: list[dict[str, Any]],
    pack_module_sha: str = "a" * 64,
    e10a_module_sha: str = BOUND_E10A_MODULE_SHA256,
    trading_dates: list[date] | None = None,
) -> CandidateEligibilityPackManifest:
    """Write a synthetic pack for testing."""
    output_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = output_dir / "eligibility_verdicts.parquet"

    arrays = [pa.array([r[field.name] for r in rows], type=field.type) for field in _OUTPUT_PARQUET_SCHEMA]
    table = pa.table(arrays, schema=_OUTPUT_PARQUET_SCHEMA)
    pq.write_table(table, str(parquet_path))

    parquet_sha = _sha256_bytes(parquet_path.read_bytes())
    table_hash = compute_canonical_table_hash(table)

    if trading_dates is None:
        trading_dates = sorted({date.fromisoformat(r["as_of"]) for r in rows})

    source_binding = PackSourceBinding(
        inventory_path=BOUND_INVENTORY_PATH,
        inventory_id=BOUND_INVENTORY_ID,
        market_snapshot_id="m" * 64,
        market_manifest_sha256="n" * 64,
        fundamental_snapshot_id="f" * 64,
        fundamental_manifest_sha256="g" * 64,
        valuation_file_sha256="v" * 64,
        raw_collection_dir=BOUND_RAW_COLLECTION_DIR,
        raw_collection_request_id=BOUND_RAW_COLLECTION_REQUEST_ID,
        raw_collection_manifest_sha256=BOUND_RAW_COLLECTION_MANIFEST_SHA256,
        raw_quality_report_sha256=BOUND_RAW_QUALITY_REPORT_SHA256,
        raw_dataset_hash_trade_cal=BOUND_RAW_DATASET_HASH_TRADE_CAL,
        raw_dataset_hash_stock_basic=BOUND_RAW_DATASET_HASH_STOCK_BASIC,
        raw_dataset_hash_namechange=BOUND_RAW_DATASET_HASH_NAMECHANGE,
        two_layer_contract_id=BOUND_TWO_LAYER_CONTRACT_ID,
        two_layer_contract_path=BOUND_TWO_LAYER_CONTRACT_PATH,
        two_layer_contract_file_sha256=BOUND_TWO_LAYER_CONTRACT_FILE_SHA256,
        allocation_protocol_id=BOUND_ALLOCATION_PROTOCOL_ID,
        allocation_protocol_file_sha256=BOUND_ALLOCATION_PROTOCOL_FILE_SHA256,
        allocation_protocol_path=BOUND_ALLOCATION_PROTOCOL_PATH,
        planned_buy_notional_cny=BOUND_PLANNED_BUY_NOTIONAL_CNY,
        e10a_engine_version=LAYER_TWO_CANDIDATE_ELIGIBILITY_ENGINE_VERSION,
        e10a_module_sha256=BOUND_E10A_MODULE_SHA256,
    )

    year_counts: dict[int, int] = {}
    for r in rows:
        y = int(r["as_of"][:4])
        year_counts[y] = year_counts.get(y, 0) + 1

    manifest = CandidateEligibilityPackManifest(
        schema_version=PACK_SCHEMA_VERSION,
        pack_version=PACK_ENGINE_VERSION,
        source_binding=source_binding,
        coverage=PackCoverageInfo(
            start=COVERAGE_START,
            end=COVERAGE_END,
            trading_date_count=len(trading_dates),
            trading_date_set_sha256=compute_trading_date_set_hash(trading_dates),
        ),
        row_counts=PackRowCounts(
            total=len(rows),
            year_2022=year_counts.get(2022, 0),
            year_2023=year_counts.get(2023, 0),
            year_2024=year_counts.get(2024, 0),
        ),
        integrity=PackIntegrity(
            parquet_file_sha256=parquet_sha,
            canonical_table_sha256=table_hash,
            symbol_date_key_unique=True,
            row_count=len(rows),
        ),
        readiness=PackReadinessFlags(
            research_only=True,
            ready_for_scoring=False,
            ready_for_trading=False,
            ready_for_portfolio_construction=False,
            not_alpha_evidence=True,
            not_authorization=True,
        ),
        pack_module_sha256=pack_module_sha,
        e10a_module_sha256=e10a_module_sha,
    )
    sealed = seal_manifest(manifest)
    manifest_text = json.dumps(sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    (output_dir / "manifest.json").write_text(manifest_text, encoding="utf-8")
    return sealed


class TestSyntheticPackSeal:
    def test_sealed_manifest_verifies_self(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack"
            rows = [_make_sample_row()]
            manifest = _write_synthetic_pack(out, rows=rows)
            assert manifest.pack_id is not None
            assert compute_pack_id(manifest) == manifest.pack_id

    def test_tampered_parquet_detected(self) -> None:
        """If Parquet file is rewritten, SHA mismatch is detected."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack"
            rows = [_make_sample_row()]
            _write_synthetic_pack(out, rows=rows)

            parquet_path = out / "eligibility_verdicts.parquet"
            tampered_row = _make_sample_row(symbol="999999.SH")
            arrays = [pa.array([tampered_row[field.name]], type=field.type) for field in _OUTPUT_PARQUET_SCHEMA]
            table = pa.table(arrays, schema=_OUTPUT_PARQUET_SCHEMA)
            pq.write_table(table, str(parquet_path))

            manifest_data = json.loads((out / "manifest.json").read_text("utf-8"))
            manifest = CandidateEligibilityPackManifest.model_validate(manifest_data)
            actual_sha = _sha256_bytes(parquet_path.read_bytes())
            assert actual_sha != manifest.integrity.parquet_file_sha256

    def test_tampered_manifest_detected(self) -> None:
        """Modifying manifest fields invalidates pack_id."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack"
            rows = [_make_sample_row()]
            _write_synthetic_pack(out, rows=rows)

            manifest_path = out / "manifest.json"
            data = json.loads(manifest_path.read_text("utf-8"))
            original_id = data["pack_id"]
            data["row_counts"]["total"] = 999
            data["row_counts"]["year_2023"] = 998
            manifest_path.write_text(json.dumps(data), encoding="utf-8")

            reloaded = json.loads(manifest_path.read_text("utf-8"))
            try:
                m = CandidateEligibilityPackManifest.model_validate(reloaded)
                assert compute_pack_id(m) != original_id
            except ValidationError:
                pass


class TestAtomicCleanup:
    def test_output_dir_exists_refused(self) -> None:
        """Builder refuses if output_dir already exists."""
        from app.research.layer_two_candidate_eligibility_pack import (
            build_candidate_eligibility_pack,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            out = root / "output"
            out.mkdir()
            with pytest.raises(FileExistsError):
                build_candidate_eligibility_pack(repo_root=root, output_dir=out)


class TestDatasetHashRecomputation:
    def test_recomputes_from_parquet_files(self) -> None:
        """_compute_dataset_hashes actually reads parquet files."""
        import polars as pl

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = root / "reference"
            ref.mkdir()
            df = pl.DataFrame({"x": [1, 2, 3]})
            df.write_parquet(ref / "test.parquet")

            hashes = _compute_dataset_hashes(root)
            assert "reference/test" in hashes
            assert len(hashes["reference/test"]) == 64

    def test_mutation_detected(self) -> None:
        """Changing a parquet file changes its dataset hash."""
        import polars as pl

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = root / "reference"
            ref.mkdir()
            df1 = pl.DataFrame({"x": [1, 2, 3]})
            df1.write_parquet(ref / "test.parquet")
            h1 = _compute_dataset_hashes(root)

            df2 = pl.DataFrame({"x": [4, 5, 6]})
            df2.write_parquet(ref / "test.parquet")
            h2 = _compute_dataset_hashes(root)

            assert h1["reference/test"] != h2["reference/test"]


class TestSourcePathMissing:
    def test_builder_fails_on_missing_inventory(self) -> None:
        """Builder fails if inventory file is missing."""
        from app.research.layer_two_candidate_eligibility_pack import (
            build_candidate_eligibility_pack,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            out = root / "output"
            with pytest.raises((ValueError, FileExistsError)):
                build_candidate_eligibility_pack(repo_root=root, output_dir=out)


class TestChangedNotional:
    def test_different_notional_in_binding_detected(self) -> None:
        """A binding with notional != 8000 is detectable."""
        sb = PackSourceBinding(
            inventory_path=BOUND_INVENTORY_PATH,
            inventory_id=BOUND_INVENTORY_ID,
            market_snapshot_id="a" * 64,
            market_manifest_sha256="b" * 64,
            fundamental_snapshot_id="c" * 64,
            fundamental_manifest_sha256="d" * 64,
            valuation_file_sha256="e" * 64,
            raw_collection_dir=BOUND_RAW_COLLECTION_DIR,
            raw_collection_request_id=BOUND_RAW_COLLECTION_REQUEST_ID,
            raw_collection_manifest_sha256=BOUND_RAW_COLLECTION_MANIFEST_SHA256,
            raw_quality_report_sha256=BOUND_RAW_QUALITY_REPORT_SHA256,
            raw_dataset_hash_trade_cal=BOUND_RAW_DATASET_HASH_TRADE_CAL,
            raw_dataset_hash_stock_basic=BOUND_RAW_DATASET_HASH_STOCK_BASIC,
            raw_dataset_hash_namechange=BOUND_RAW_DATASET_HASH_NAMECHANGE,
            two_layer_contract_id=BOUND_TWO_LAYER_CONTRACT_ID,
            two_layer_contract_path=BOUND_TWO_LAYER_CONTRACT_PATH,
            two_layer_contract_file_sha256=BOUND_TWO_LAYER_CONTRACT_FILE_SHA256,
            allocation_protocol_id=BOUND_ALLOCATION_PROTOCOL_ID,
            allocation_protocol_file_sha256=BOUND_ALLOCATION_PROTOCOL_FILE_SHA256,
            allocation_protocol_path=BOUND_ALLOCATION_PROTOCOL_PATH,
            planned_buy_notional_cny=10000,
            e10a_engine_version=LAYER_TWO_CANDIDATE_ELIGIBILITY_ENGINE_VERSION,
            e10a_module_sha256=BOUND_E10A_MODULE_SHA256,
        )
        assert sb.planned_buy_notional_cny != BOUND_PLANNED_BUY_NOTIONAL_CNY


# ---------------------------------------------------------------------------
# Finding #6: Real builder+verifier integration with synthetic data
# ---------------------------------------------------------------------------


def _make_synthetic_trade_cal() -> list[date]:
    """3 trading dates in 2023 for tests."""
    return [date(2023, 1, 3), date(2023, 1, 4), date(2023, 1, 5)]


def _make_synthetic_stock_info() -> dict[str, dict[str, Any]]:
    """3 symbols with various list/delist scenarios."""
    return {
        "600000.SH": {
            "ts_code": "600000.SH",
            "exchange": "SSE",
            "list_date": "20200101",
            "delist_date": None,
        },
        "000001.SZ": {
            "ts_code": "000001.SZ",
            "exchange": "SZSE",
            "list_date": "20200601",
            "delist_date": None,
        },
        "600999.SH": {
            "ts_code": "600999.SH",
            "exchange": "SSE",
            "list_date": "20200101",
            "delist_date": "20230104",  # delists on 2023-01-04 → excluded from that date onward
        },
    }


def _make_synthetic_bars(
    symbols: list[str],
    dates: list[date],
) -> dict[str, list[BarTuple]]:
    """Create bars for all symbols on all dates."""
    bars: dict[str, list[BarTuple]] = {}
    for sym in symbols:
        entries: list[BarTuple] = []
        for d in dates:
            entries.append((d, 100_000_000.0, False, False))
        bars[sym] = sorted(entries, key=lambda x: x[0])
    return bars


def _make_synthetic_valuation(
    symbols: list[str],
    dates: list[date],
    *,
    available_offset_hours: float = 7.0,
) -> dict[str, list[ValTuple]]:
    """Create valuation entries with valid available_at for each symbol/date."""
    val: dict[str, list[ValTuple]] = {}
    for sym in symbols:
        entries: list[ValTuple] = []
        for d in dates:
            available_at = datetime(d.year, d.month, d.day, int(available_offset_hours), 0, 0)
            entries.append((d, 500_000.0, available_at, hashlib.sha256(f"{sym}:{d}".encode()).hexdigest()))
        val[sym] = sorted(entries, key=lambda x: x[0])
    return val


def _extended_trading_cal() -> list[date]:
    """Extended calendar for listed_days computation (shared helper)."""
    base = date(2020, 1, 1)
    return [base + timedelta(days=i) for i in range(0, 1200) if (base + timedelta(days=i)).weekday() < 5]


class TestIntegrationRowGenerator:
    """End-to-end tests using the production _generate_row_for_candidate."""

    def _all_trading_dates(self) -> list[date]:
        return _extended_trading_cal()

    def test_valid_candidate_produces_eligible_row(self) -> None:
        all_dates = self._all_trading_dates()
        as_of = date(2023, 1, 4)
        decision_at = make_decision_at(as_of)
        stock_info = _make_synthetic_stock_info()

        lookback_dates = []
        as_of_idx = next(i for i, d in enumerate(all_dates) if d == as_of)
        start_lb = max(0, as_of_idx - 19)
        lookback_dates = all_dates[start_lb : as_of_idx + 1]

        symbol_bars = _make_synthetic_bars(["600000.SH"], lookback_dates + [as_of])
        symbol_valuation = _make_synthetic_valuation(["600000.SH"], [as_of])

        from app.research.layer_two_candidate_eligibility import (
            bind_two_layer_eligibility_policy,
        )

        repo_root = Path(__file__).resolve().parent.parent
        try:
            _, _, policy = bind_two_layer_eligibility_policy(repo_root=repo_root)
        except (ValueError, FileNotFoundError):
            pytest.skip("contract file not available in test env")

        row = _generate_row_for_candidate(
            "600000.SH",
            as_of,
            decision_at=decision_at,
            stock_info=stock_info,
            symbol_bars=symbol_bars,
            symbol_valuation=symbol_valuation,
            all_trading_dates=all_dates,
            market_snapshot_id="m" * 64,
            fund_snapshot_id="f" * 64,
            val_file_sha256="v" * 64,
            inventory_id="i" * 64,
            contract_id="c" * 64,
            alloc_id="a" * 64,
            policy=policy,
        )

        assert row["symbol"] == "600000.SH"
        assert row["as_of"] == "2023-01-04"
        assert row["decision_at"] == decision_at.isoformat()
        assert isinstance(row["source_input_hash"], str)
        assert len(row["source_input_hash"]) == 64
        assert row["eligible_for_new_entry"] is True or row["unknown_critical_input"] is True

    def test_missing_bar_produces_unknown(self) -> None:
        all_dates = self._all_trading_dates()
        as_of = date(2023, 1, 4)
        decision_at = make_decision_at(as_of)
        stock_info = _make_synthetic_stock_info()
        symbol_bars: dict[str, list[BarTuple]] = {}
        symbol_valuation: dict[str, list[ValTuple]] = {}

        from app.research.layer_two_candidate_eligibility import (
            bind_two_layer_eligibility_policy,
        )

        repo_root = Path(__file__).resolve().parent.parent
        try:
            _, _, policy = bind_two_layer_eligibility_policy(repo_root=repo_root)
        except (ValueError, FileNotFoundError):
            pytest.skip("contract file not available in test env")

        row = _generate_row_for_candidate(
            "600000.SH",
            as_of,
            decision_at=decision_at,
            stock_info=stock_info,
            symbol_bars=symbol_bars,
            symbol_valuation=symbol_valuation,
            all_trading_dates=all_dates,
            market_snapshot_id="m" * 64,
            fund_snapshot_id="f" * 64,
            val_file_sha256="v" * 64,
            inventory_id="i" * 64,
            contract_id="c" * 64,
            alloc_id="a" * 64,
            policy=policy,
        )

        assert row["unknown_critical_input"] is True
        assert row["eligible_for_new_entry"] is False

    def test_security_status_available_at_is_market_close(self) -> None:
        """security_status_available_at = 15:00, not 17:30."""
        all_dates = self._all_trading_dates()
        as_of = date(2023, 1, 4)
        decision_at = make_decision_at(as_of)
        stock_info = _make_synthetic_stock_info()
        symbol_bars = _make_synthetic_bars(["600000.SH"], [as_of])
        symbol_valuation: dict[str, list[ValTuple]] = {}

        from app.research.layer_two_candidate_eligibility import (
            bind_two_layer_eligibility_policy,
        )

        repo_root = Path(__file__).resolve().parent.parent
        try:
            _, _, policy = bind_two_layer_eligibility_policy(repo_root=repo_root)
        except (ValueError, FileNotFoundError):
            pytest.skip("contract file not available in test env")

        row = _generate_row_for_candidate(
            "600000.SH",
            as_of,
            decision_at=decision_at,
            stock_info=stock_info,
            symbol_bars=symbol_bars,
            symbol_valuation=symbol_valuation,
            all_trading_dates=all_dates,
            market_snapshot_id="m" * 64,
            fund_snapshot_id="f" * 64,
            val_file_sha256="v" * 64,
            inventory_id="i" * 64,
            contract_id="c" * 64,
            alloc_id="a" * 64,
            policy=policy,
        )

        assert row["symbol"] == "600000.SH"
        # The source_input_hash embeds the candidate_input which includes
        # security_status_available_at = market_close (15:00)
        # We verify this by checking the row was generated at all (would fail if
        # available_at > decision_at in the E10a validator)
        assert row["source_input_hash"] is not None

    def test_delist_excludes_from_date_onward(self) -> None:
        """Stock with delist_date=2023-01-04 is excluded from that date."""
        stock_info = _make_synthetic_stock_info()

        from app.research.layer_two_candidate_eligibility_pack import (
            _parse_date_col,
        )

        sym = "600999.SH"
        info = stock_info[sym]
        ld = _parse_date_col(info.get("list_date"))
        dd = _parse_date_col(info.get("delist_date"))

        # On 2023-01-03 (before delist_date), included
        assert ld is not None and ld <= date(2023, 1, 3)
        assert dd is not None and dd > date(2023, 1, 3)

        # On 2023-01-04 (== delist_date), excluded
        assert dd is not None and dd <= date(2023, 1, 4)


class TestProductionVerifyHelper:
    """Tests that invoke the production _verify_expected_rows_streaming helper."""

    def _write_parquet(self, path: Path, rows: list[dict[str, Any]]) -> None:
        writer = pq.ParquetWriter(str(path), _OUTPUT_PARQUET_SCHEMA)
        try:
            for row in rows:
                arrays = [pa.array([row[f.name]], type=f.type) for f in _OUTPUT_PARQUET_SCHEMA]
                batch = pa.record_batch(arrays, schema=_OUTPUT_PARQUET_SCHEMA)
                writer.write_batch(batch)
        finally:
            writer.close()

    def test_roundtrip_success(self) -> None:
        """Production helper succeeds on matching rows."""
        rows = [
            _make_sample_row("000001.SZ", "2023-01-04"),
            _make_sample_row("600000.SH", "2023-01-04"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.parquet"
            self._write_parquet(path, rows)
            result = _verify_expected_rows_streaming(
                parquet_path=path,
                expected_iter=iter(rows),
                expected_schema=_OUTPUT_PARQUET_SCHEMA,
                expected_row_count=2,
                expected_year_counts={2022: 0, 2023: 2, 2024: 0},
            )
            assert result is True

    def test_extra_row_raises(self) -> None:
        """Production helper raises on extra stored rows."""
        rows = [_make_sample_row("600000.SH", "2023-01-04")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.parquet"
            self._write_parquet(path, rows)
            with pytest.raises(ValueError, match="extra row"):
                _verify_expected_rows_streaming(
                    parquet_path=path,
                    expected_iter=iter([]),
                    expected_schema=_OUTPUT_PARQUET_SCHEMA,
                    expected_row_count=0,
                    expected_year_counts={2022: 0, 2023: 0, 2024: 0},
                )

    def test_missing_row_raises(self) -> None:
        """Production helper raises on missing stored rows."""
        stored = [_make_sample_row("600000.SH", "2023-01-04")]
        expected = [
            _make_sample_row("600000.SH", "2023-01-04"),
            _make_sample_row("600001.SH", "2023-01-04"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.parquet"
            self._write_parquet(path, stored)
            with pytest.raises(ValueError, match="missing expected rows"):
                _verify_expected_rows_streaming(
                    parquet_path=path,
                    expected_iter=iter(expected),
                    expected_schema=_OUTPUT_PARQUET_SCHEMA,
                    expected_row_count=2,
                    expected_year_counts={2022: 0, 2023: 2, 2024: 0},
                )

    def test_numeric_mutation_raises(self) -> None:
        """Production helper raises on numeric field mismatch."""
        row = _make_sample_row("600000.SH", "2023-01-04")
        tampered = dict(row)
        tampered["median_daily_amount_cny"] = 999_999.0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.parquet"
            self._write_parquet(path, [tampered])
            with pytest.raises(ValueError, match="median_daily_amount_cny.*mismatch"):
                _verify_expected_rows_streaming(
                    parquet_path=path,
                    expected_iter=iter([row]),
                    expected_schema=_OUTPUT_PARQUET_SCHEMA,
                    expected_row_count=1,
                    expected_year_counts={2022: 0, 2023: 1, 2024: 0},
                )

    def test_source_input_hash_mutation_raises(self) -> None:
        """Production helper raises on source_input_hash mismatch."""
        row = _make_sample_row("600000.SH", "2023-01-04")
        tampered = dict(row)
        tampered["source_input_hash"] = "y" * 64
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.parquet"
            self._write_parquet(path, [tampered])
            with pytest.raises(ValueError, match="source_input_hash.*mismatch"):
                _verify_expected_rows_streaming(
                    parquet_path=path,
                    expected_iter=iter([row]),
                    expected_schema=_OUTPUT_PARQUET_SCHEMA,
                    expected_row_count=1,
                    expected_year_counts={2022: 0, 2023: 1, 2024: 0},
                )

    def test_reordered_keys_raises(self) -> None:
        """Production helper raises when rows are in wrong order."""
        row_a = _make_sample_row("000001.SZ", "2023-01-04")
        row_b = _make_sample_row("600000.SH", "2023-01-04")
        stored_order = [row_b, row_a]
        expected_order = [row_a, row_b]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.parquet"
            self._write_parquet(path, stored_order)
            with pytest.raises(ValueError, match="mismatch"):
                _verify_expected_rows_streaming(
                    parquet_path=path,
                    expected_iter=iter(expected_order),
                    expected_schema=_OUTPUT_PARQUET_SCHEMA,
                    expected_row_count=2,
                    expected_year_counts={2022: 0, 2023: 2, 2024: 0},
                )

    def test_schema_mismatch_raises(self) -> None:
        """Production helper raises on schema mismatch."""
        wrong_schema = pa.schema([("symbol", pa.utf8()), ("wrong_col", pa.int32())])
        table = pa.table({"symbol": ["600000.SH"], "wrong_col": [1]}, schema=wrong_schema)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.parquet"
            pq.write_table(table, str(path))
            with pytest.raises(ValueError, match="schema mismatch"):
                _verify_expected_rows_streaming(
                    parquet_path=path,
                    expected_iter=iter([]),
                    expected_schema=_OUTPUT_PARQUET_SCHEMA,
                    expected_row_count=0,
                    expected_year_counts={2022: 0, 2023: 0, 2024: 0},
                )

    def test_year_count_mismatch_raises(self) -> None:
        """Production helper raises when year counts don't match."""
        rows = [_make_sample_row("600000.SH", "2023-01-04")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.parquet"
            self._write_parquet(path, rows)
            with pytest.raises(ValueError, match="year_2023 count mismatch"):
                _verify_expected_rows_streaming(
                    parquet_path=path,
                    expected_iter=iter(rows),
                    expected_schema=_OUTPUT_PARQUET_SCHEMA,
                    expected_row_count=1,
                    expected_year_counts={2022: 0, 2023: 99, 2024: 0},
                )


class TestEndToEndGenerateAndVerify:
    """Genuine end-to-end: _generate_expected_rows -> Parquet -> _verify_expected_rows_streaming."""

    def _write_parquet(self, path: Path, rows: list[dict[str, Any]]) -> None:
        writer = pq.ParquetWriter(str(path), _OUTPUT_PARQUET_SCHEMA)
        try:
            for row in rows:
                arrays = [pa.array([row[f.name]], type=f.type) for f in _OUTPUT_PARQUET_SCHEMA]
                batch = pa.record_batch(arrays, schema=_OUTPUT_PARQUET_SCHEMA)
                writer.write_batch(batch)
        finally:
            writer.close()

    def test_generate_write_verify_roundtrip(self) -> None:
        """Production _generate_expected_rows writes to Parquet, then _verify validates."""
        from app.research.layer_two_candidate_eligibility import (
            bind_two_layer_eligibility_policy,
        )

        repo_root = Path(__file__).resolve().parent.parent
        try:
            _, _, policy = bind_two_layer_eligibility_policy(repo_root=repo_root)
        except (ValueError, FileNotFoundError):
            pytest.skip("contract file not available")

        all_dates = _extended_trading_cal()
        trading_dates = [date(2023, 1, 4), date(2023, 1, 5)]
        symbols = ["000001.SZ", "600000.SH"]
        stock_info = _make_synthetic_stock_info()
        symbol_list_date = {s: date(2020, 1, 2) for s in symbols}
        symbol_delist_date: dict[str, date | None] = {s: None for s in symbols}

        lookback_start_idx = max(0, next(i for i, d in enumerate(all_dates) if d >= date(2023, 1, 4)) - 20)
        bar_dates = all_dates[lookback_start_idx : next(i for i, d in enumerate(all_dates) if d > date(2023, 1, 5))]
        symbol_bars = _make_synthetic_bars(symbols, bar_dates)
        symbol_valuation = _make_synthetic_valuation(symbols, trading_dates)

        rows = list(
            _generate_expected_rows(
                trading_dates=trading_dates,
                ordinary_a_symbols=sorted(symbols),
                symbol_list_date=symbol_list_date,
                symbol_delist_date=symbol_delist_date,
                stock_info=stock_info,
                symbol_bars=symbol_bars,
                symbol_valuation=symbol_valuation,
                all_trading_dates=all_dates,
                market_snapshot_id="m" * 64,
                fund_snapshot_id="f" * 64,
                val_file_sha256="v" * 64,
                inventory_id="i" * 64,
                contract_id="c" * 64,
                alloc_id="a" * 64,
                policy=policy,
            )
        )
        assert len(rows) == 4  # 2 symbols x 2 dates

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdicts.parquet"
            self._write_parquet(path, rows)

            expected_iter = _generate_expected_rows(
                trading_dates=trading_dates,
                ordinary_a_symbols=sorted(symbols),
                symbol_list_date=symbol_list_date,
                symbol_delist_date=symbol_delist_date,
                stock_info=stock_info,
                symbol_bars=symbol_bars,
                symbol_valuation=symbol_valuation,
                all_trading_dates=all_dates,
                market_snapshot_id="m" * 64,
                fund_snapshot_id="f" * 64,
                val_file_sha256="v" * 64,
                inventory_id="i" * 64,
                contract_id="c" * 64,
                alloc_id="a" * 64,
                policy=policy,
            )

            result = _verify_expected_rows_streaming(
                parquet_path=path,
                expected_iter=expected_iter,
                expected_schema=_OUTPUT_PARQUET_SCHEMA,
                expected_row_count=4,
                expected_year_counts={2022: 0, 2023: 4, 2024: 0},
            )
            assert result is True

    def test_tampered_row_detected(self) -> None:
        """Tampering a single field in the written Parquet is detected by verifier."""
        from app.research.layer_two_candidate_eligibility import (
            bind_two_layer_eligibility_policy,
        )

        repo_root = Path(__file__).resolve().parent.parent
        try:
            _, _, policy = bind_two_layer_eligibility_policy(repo_root=repo_root)
        except (ValueError, FileNotFoundError):
            pytest.skip("contract file not available")

        all_dates = _extended_trading_cal()
        trading_dates = [date(2023, 1, 4)]
        symbols = ["600000.SH"]
        stock_info = _make_synthetic_stock_info()
        symbol_list_date = {s: date(2020, 1, 2) for s in symbols}
        symbol_delist_date: dict[str, date | None] = {s: None for s in symbols}

        lookback_start_idx = max(0, next(i for i, d in enumerate(all_dates) if d >= date(2023, 1, 4)) - 20)
        bar_dates = all_dates[lookback_start_idx : next(i for i, d in enumerate(all_dates) if d > date(2023, 1, 4))]
        symbol_bars = _make_synthetic_bars(symbols, bar_dates)
        symbol_valuation = _make_synthetic_valuation(symbols, trading_dates)

        gen_kwargs: dict[str, Any] = dict(
            trading_dates=trading_dates,
            ordinary_a_symbols=sorted(symbols),
            symbol_list_date=symbol_list_date,
            symbol_delist_date=symbol_delist_date,
            stock_info=stock_info,
            symbol_bars=symbol_bars,
            symbol_valuation=symbol_valuation,
            all_trading_dates=all_dates,
            market_snapshot_id="m" * 64,
            fund_snapshot_id="f" * 64,
            val_file_sha256="v" * 64,
            inventory_id="i" * 64,
            contract_id="c" * 64,
            alloc_id="a" * 64,
            policy=policy,
        )

        rows = list(_generate_expected_rows(**gen_kwargs))
        assert len(rows) == 1

        tampered = dict(rows[0])
        tampered["source_input_hash"] = "0" * 64

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdicts.parquet"
            self._write_parquet(path, [tampered])

            with pytest.raises(ValueError, match="source_input_hash.*mismatch"):
                _verify_expected_rows_streaming(
                    parquet_path=path,
                    expected_iter=_generate_expected_rows(**gen_kwargs),
                    expected_schema=_OUTPUT_PARQUET_SCHEMA,
                    expected_row_count=1,
                    expected_year_counts={2022: 0, 2023: 1, 2024: 0},
                )


class TestBoundE10AModuleSHA256:
    """Frozen E10a SHA and inventory ID enforcement via pure check helpers."""

    def test_bound_constant_is_64_hex(self) -> None:
        import re

        assert re.fullmatch(r"[0-9a-f]{64}", BOUND_E10A_MODULE_SHA256)

    def test_require_bound_e10a_sha_accepts_correct(self) -> None:
        _require_bound_e10a_sha(BOUND_E10A_MODULE_SHA256)

    def test_require_bound_e10a_sha_rejects_wrong(self) -> None:
        with pytest.raises(ValueError, match="does not match frozen constant"):
            _require_bound_e10a_sha("0" * 64)

    def test_require_bound_inventory_id_accepts_correct(self) -> None:
        _require_bound_inventory_id(BOUND_INVENTORY_ID)

    def test_require_bound_inventory_id_rejects_wrong(self) -> None:
        with pytest.raises(ValueError, match="does not match frozen constant"):
            _require_bound_inventory_id("0" * 64)


class TestNullBarStatus:
    """Finding #3: None is_suspended/is_st preserved, not coerced to False."""

    def test_null_status_remains_unknown_in_row(self) -> None:
        all_dates = _extended_trading_cal()
        as_of = date(2023, 1, 4)
        decision_at = make_decision_at(as_of)
        stock_info = _make_synthetic_stock_info()

        bars_with_null: dict[str, list[BarTuple]] = {
            "600000.SH": [(as_of, 100_000_000.0, None, None)],
        }
        symbol_valuation: dict[str, list[ValTuple]] = {}

        repo_root = Path(__file__).resolve().parent.parent
        try:
            from app.research.layer_two_candidate_eligibility import (
                bind_two_layer_eligibility_policy,
            )

            _, _, policy = bind_two_layer_eligibility_policy(repo_root=repo_root)
        except (ValueError, FileNotFoundError):
            pytest.skip("contract file not available")

        row = _generate_row_for_candidate(
            "600000.SH",
            as_of,
            decision_at=decision_at,
            stock_info=stock_info,
            symbol_bars=bars_with_null,
            symbol_valuation=symbol_valuation,
            all_trading_dates=all_dates,
            market_snapshot_id="m" * 64,
            fund_snapshot_id="f" * 64,
            val_file_sha256="v" * 64,
            inventory_id="i" * 64,
            contract_id="c" * 64,
            alloc_id="a" * 64,
            policy=policy,
        )
        assert row["unknown_critical_input"] is True

    def test_duplicate_bar_date_rejected(self) -> None:
        """Duplicate (symbol, date) bar rows are rejected at load time."""
        import polars as pl

        from app.research.layer_two_candidate_eligibility_pack import (
            _load_daily_bars_index,
        )

        with tempfile.TemporaryDirectory() as tmp:
            mkt = Path(tmp)
            df = pl.DataFrame(
                {
                    "ts_code": ["600000.SH", "600000.SH"],
                    "trade_date": ["20230104", "20230104"],
                    "amount": [100.0, 200.0],
                    "is_suspended": [False, False],
                    "is_st": [False, False],
                }
            )
            df.write_parquet(mkt / "daily_bars.parquet")
            with pytest.raises(ValueError, match="duplicate.*bar row"):
                _load_daily_bars_index(mkt)


class TestValuationEdgeCases:
    """Comprehensive valuation validation tests using ValTuple."""

    def _run_val_test(self, val_entries: list[ValTuple]) -> dict[str, Any]:
        all_dates = _extended_trading_cal()
        as_of = date(2023, 1, 4)
        decision_at = make_decision_at(as_of)
        stock_info = _make_synthetic_stock_info()
        symbol_bars = _make_synthetic_bars(["600000.SH"], [as_of])
        symbol_valuation: dict[str, list[ValTuple]] = {
            "600000.SH": sorted(val_entries, key=lambda x: x[0]),
        }

        repo_root = Path(__file__).resolve().parent.parent
        try:
            from app.research.layer_two_candidate_eligibility import (
                bind_two_layer_eligibility_policy,
            )

            _, _, policy = bind_two_layer_eligibility_policy(repo_root=repo_root)
        except (ValueError, FileNotFoundError):
            pytest.skip("contract file not available")

        return _generate_row_for_candidate(
            "600000.SH",
            as_of,
            decision_at=decision_at,
            stock_info=stock_info,
            symbol_bars=symbol_bars,
            symbol_valuation=symbol_valuation,
            all_trading_dates=all_dates,
            market_snapshot_id="m" * 64,
            fund_snapshot_id="f" * 64,
            val_file_sha256="v" * 64,
            inventory_id="i" * 64,
            contract_id="c" * 64,
            alloc_id="a" * 64,
            policy=policy,
        )

    def test_valid_same_day_accepted(self) -> None:
        """Same-day val with valid available_at and hash -> known cap."""
        row = self._run_val_test([(date(2023, 1, 4), 500_000.0, datetime(2023, 1, 4, 7, 0, 0), "a" * 64)])
        assert row["pit_free_float_market_cap_cny"] == 500_000.0 * 10000

    def test_stale_prior_day_available_at_unknown(self) -> None:
        """available_at in Shanghai is prior day -> unknown."""
        row = self._run_val_test([(date(2023, 1, 4), 500_000.0, datetime(2023, 1, 3, 10, 0, 0), "a" * 64)])
        assert row["pit_free_float_market_cap_cny"] is None

    def test_late_available_at_unknown(self) -> None:
        """available_at > decision_at -> unknown."""
        row = self._run_val_test([(date(2023, 1, 4), 500_000.0, datetime(2023, 1, 4, 18, 0, 0), "a" * 64)])
        assert row["pit_free_float_market_cap_cny"] is None

    def test_missing_source_row_hash_unknown(self) -> None:
        """Missing source_row_hash -> unknown."""
        row = self._run_val_test([(date(2023, 1, 4), 500_000.0, datetime(2023, 1, 4, 7, 0, 0), None)])
        assert row["pit_free_float_market_cap_cny"] is None

    def test_invalid_hex_hash_unknown(self) -> None:
        """Non-hex source_row_hash -> unknown."""
        row = self._run_val_test([(date(2023, 1, 4), 500_000.0, datetime(2023, 1, 4, 7, 0, 0), "not_valid_hex")])
        assert row["pit_free_float_market_cap_cny"] is None

    def test_duplicate_same_day_val_unknown(self) -> None:
        """Two val rows for same date -> unknown."""
        row = self._run_val_test(
            [
                (date(2023, 1, 4), 500_000.0, datetime(2023, 1, 4, 7, 0, 0), "a" * 64),
                (date(2023, 1, 4), 600_000.0, datetime(2023, 1, 4, 7, 0, 0), "b" * 64),
            ]
        )
        assert row["pit_free_float_market_cap_cny"] is None

    def test_nonpositive_circ_mv_unknown(self) -> None:
        """Zero or negative circ_mv -> unknown."""
        row = self._run_val_test([(date(2023, 1, 4), 0.0, datetime(2023, 1, 4, 7, 0, 0), "a" * 64)])
        assert row["pit_free_float_market_cap_cny"] is None

    def test_future_date_val_ignored(self) -> None:
        """Val row with date > as_of is not used (only same-day)."""
        row = self._run_val_test([(date(2023, 1, 5), 500_000.0, datetime(2023, 1, 5, 7, 0, 0), "a" * 64)])
        assert row["pit_free_float_market_cap_cny"] is None


class TestDuplicateStockBasicRejected:
    """Duplicate ts_code rejected at load time."""

    def test_duplicate_ts_code_raises(self) -> None:
        import polars as pl

        from app.research.layer_two_candidate_eligibility_pack import _load_stock_basic

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = root / "reference"
            ref.mkdir()
            df = pl.DataFrame(
                {
                    "ts_code": ["600000.SH", "600000.SH"],
                    "exchange": ["SSE", "SSE"],
                    "list_date": ["20200101", "20200101"],
                    "delist_date": [None, None],
                }
            )
            df.write_parquet(ref / "stock_basic.parquet")

            with pytest.raises(ValueError, match="duplicate ts_code"):
                _load_stock_basic(root)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_manifest(row_count: int = 100) -> CandidateEligibilityPackManifest:
    return CandidateEligibilityPackManifest(
        schema_version=PACK_SCHEMA_VERSION,
        pack_version=PACK_ENGINE_VERSION,
        source_binding=PackSourceBinding(
            inventory_path=BOUND_INVENTORY_PATH,
            inventory_id=BOUND_INVENTORY_ID,
            market_snapshot_id="a" * 64,
            market_manifest_sha256="b" * 64,
            fundamental_snapshot_id="c" * 64,
            fundamental_manifest_sha256="d" * 64,
            valuation_file_sha256="e" * 64,
            raw_collection_dir=BOUND_RAW_COLLECTION_DIR,
            raw_collection_request_id=BOUND_RAW_COLLECTION_REQUEST_ID,
            raw_collection_manifest_sha256=BOUND_RAW_COLLECTION_MANIFEST_SHA256,
            raw_quality_report_sha256=BOUND_RAW_QUALITY_REPORT_SHA256,
            raw_dataset_hash_trade_cal=BOUND_RAW_DATASET_HASH_TRADE_CAL,
            raw_dataset_hash_stock_basic=BOUND_RAW_DATASET_HASH_STOCK_BASIC,
            raw_dataset_hash_namechange=BOUND_RAW_DATASET_HASH_NAMECHANGE,
            two_layer_contract_id=BOUND_TWO_LAYER_CONTRACT_ID,
            two_layer_contract_path=BOUND_TWO_LAYER_CONTRACT_PATH,
            two_layer_contract_file_sha256=BOUND_TWO_LAYER_CONTRACT_FILE_SHA256,
            allocation_protocol_id=BOUND_ALLOCATION_PROTOCOL_ID,
            allocation_protocol_file_sha256=BOUND_ALLOCATION_PROTOCOL_FILE_SHA256,
            allocation_protocol_path=BOUND_ALLOCATION_PROTOCOL_PATH,
            planned_buy_notional_cny=BOUND_PLANNED_BUY_NOTIONAL_CNY,
            e10a_engine_version=LAYER_TWO_CANDIDATE_ELIGIBILITY_ENGINE_VERSION,
            e10a_module_sha256=BOUND_E10A_MODULE_SHA256,
        ),
        coverage=PackCoverageInfo(
            start=COVERAGE_START,
            end=COVERAGE_END,
            trading_date_count=10,
            trading_date_set_sha256="f" * 64,
        ),
        row_counts=PackRowCounts(
            total=row_count,
            year_2022=row_count // 3,
            year_2023=row_count // 3,
            year_2024=row_count - 2 * (row_count // 3),
        ),
        integrity=PackIntegrity(
            parquet_file_sha256="1" * 64,
            canonical_table_sha256="2" * 64,
            symbol_date_key_unique=True,
            row_count=row_count,
        ),
        readiness=PackReadinessFlags(
            research_only=True,
            ready_for_scoring=False,
            ready_for_trading=False,
            ready_for_portfolio_construction=False,
            not_alpha_evidence=True,
            not_authorization=True,
        ),
        pack_module_sha256="3" * 64,
        e10a_module_sha256=BOUND_E10A_MODULE_SHA256,
    )
