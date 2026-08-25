from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.demo.generator import generate_demo_market, write_demo_parquet
from app.errors import DataQualityError, TushareFetchError
from app.providers.tushare_event_collection import (
    _build_quality_report,
    _sha256_file,
    _verify_collection_manifest,
    collect_tushare_a_share_events,
)
from app.providers.tushare_event_history import materialize_tushare_event_overlay
from app.providers.tushare_events import EXPRESS_NUMERIC, FORECAST_NUMERIC, normalize_event_sources
from app.storage.event_io import load_verified_event_snapshot
from tests.tushare_fakes import FakeTushareClient


class _InterruptingClient(FakeTushareClient):
    def __init__(self, tables: dict[str, pl.DataFrame], *, fail_after: int) -> None:
        super().__init__(tables)
        self.fail_after = fail_after

    def query(self, api_name: str, **params: object) -> pl.DataFrame:
        if len(self.calls) >= self.fail_after:
            raise TushareFetchError("simulated event collection interruption")
        return super().query(api_name, **params)


class _ForeignShareFloatClient(FakeTushareClient):
    def query(self, api_name: str, **params: object) -> pl.DataFrame:
        if api_name != "share_float":
            return super().query(api_name, **params)
        if "ann_date" in params and "ts_code" not in params:
            self.calls.append(api_name)
            self.call_params.append((api_name, dict(params)))
            source = self.tables["share_float"].head(1)
            ann_date = params.get("ann_date")
            if ann_date != "20240315":
                return pl.DataFrame()
            offset = int(str(params.get("offset", 0)))
            if offset == 0:
                return pl.concat(
                    [source.with_columns(pl.lit("400251").alias("ts_code"))] * 6000,
                    how="vertical",
                    rechunk=True,
                )
            if offset != 6000:
                return pl.DataFrame()
            return pl.concat(
                [
                    source.with_columns(pl.lit("000002.SZ").alias("ts_code")),
                    source.with_columns(pl.lit("400251").alias("ts_code")),
                ],
                how="vertical",
            )
        if params.get("ts_code") != "000002.SZ":
            return super().query(api_name, **params)
        self.calls.append(api_name)
        self.call_params.append((api_name, dict(params)))
        source = self.tables["share_float"].head(1)
        # Reproduce the provider failure: an unknown/ignored stock filter
        # returns a capped all-market response instead of an empty frame.
        return pl.concat([source] * 6000, how="vertical", rechunk=True)


class _InterruptingForeignShareFloatClient(_ForeignShareFloatClient):
    def __init__(self, tables: dict[str, pl.DataFrame], *, fail_after_days: int) -> None:
        super().__init__(tables)
        self.fail_after_days = fail_after_days
        self.fallback_days = 0

    def query(self, api_name: str, **params: object) -> pl.DataFrame:
        if api_name == "share_float" and "ann_date" in params and "ts_code" not in params:
            if self.fallback_days >= self.fail_after_days:
                raise TushareFetchError("simulated share_float fallback interruption")
            self.fallback_days += 1
        return super().query(api_name, **params)


class _IgnoringOffsetShareFloatClient(_ForeignShareFloatClient):
    def query(self, api_name: str, **params: object) -> pl.DataFrame:
        if (
            api_name == "share_float"
            and params.get("ann_date") == "20240315"
            and "ts_code" not in params
            and int(str(params.get("offset", 0))) > 0
        ):
            self.calls.append(api_name)
            self.call_params.append((api_name, dict(params)))
            source = self.tables["share_float"].head(1)
            return pl.concat(
                [source.with_columns(pl.lit("400251").alias("ts_code"))] * 6000,
                how="vertical",
                rechunk=True,
            )
        return super().query(api_name, **params)


class _SharedAllMarketShareFloatClient(FakeTushareClient):
    def query(self, api_name: str, **params: object) -> pl.DataFrame:
        if api_name != "share_float":
            return super().query(api_name, **params)
        self.calls.append(api_name)
        self.call_params.append((api_name, dict(params)))
        source = self.tables["share_float"].head(1)
        if "ann_date" in params and "ts_code" not in params:
            if params.get("ann_date") != "20240315":
                return pl.DataFrame()
            return pl.concat(
                [
                    source.with_columns(pl.lit("000001.SZ").alias("ts_code")),
                    source.with_columns(pl.lit("000002.SZ").alias("ts_code")),
                ],
                how="vertical",
            )
        return pl.concat([source] * 6000, how="vertical", rechunk=True)


def _market(tmp_path: Path) -> Path:
    path = tmp_path / "market"
    write_demo_parquet(generate_demo_market(n_stocks=2), path)
    return path


def _event_tables() -> dict[str, pl.DataFrame]:
    express: dict[str, object] = {
        "ts_code": "000001.SZ",
        "ann_date": "20240220",
        "end_date": "20231231",
        "perf_summary": "official express summary",
    }
    express.update({name: float(index + 1) for index, name in enumerate(EXPRESS_NUMERIC)})
    return {
        "forecast": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240120",
                    "end_date": "20231231",
                    "type": "略增",
                    "p_change_min": 5.0,
                    "p_change_max": 10.0,
                    "net_profit_min": 100.0,
                    "net_profit_max": 110.0,
                    "last_parent_net": 95.0,
                    "first_ann_date": "20240120",
                    "summary": "initial",
                    "change_reason": "operations",
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240205",
                    "end_date": "20231231",
                    "type": "预增",
                    "p_change_min": 8.0,
                    "p_change_max": 12.0,
                    "net_profit_min": 108.0,
                    "net_profit_max": 112.0,
                    "last_parent_net": 95.0,
                    "first_ann_date": "20240120",
                    "summary": "revision",
                    "change_reason": "operations revised",
                },
            ]
        ),
        "express": pl.DataFrame([express]),
        "stk_holdernumber": pl.DataFrame(
            {
                "ts_code": ["000001.SZ", "000001.SZ"],
                "ann_date": ["20240301", "2024-04-01 15:09:08"],
                "end_date": ["20240229", "20240331"],
                "holder_num": [12345, None],
            }
        ),
        "share_float": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240315",
                    "float_date": "20240630",
                    "float_share": 1_000_000.0,
                    "float_ratio": 1.5,
                    "holder_name": "holder-a",
                    "share_type": "定增股份",
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20200101",
                    "float_date": "20250101",
                    "float_share": 2_000_000.0,
                    "float_ratio": None,
                    "holder_name": "out-of-window",
                    "share_type": "首发股",
                },
            ]
        ),
        "fina_audit": pl.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "ann_date": ["20240430"],
                "end_date": ["20231231"],
                "audit_result": ["标准无保留意见"],
                "audit_fees": [100.0],
                "audit_agency": ["agency-a"],
                "audit_sign": ["auditor-a"],
            }
        ),
    }


def test_event_collection_resumes_reports_quality_and_materializes(tmp_path: Path) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    client = FakeTushareClient(_event_tables())
    first = collect_tushare_a_share_events(
        client=client,
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
        source_version="events-fixture-v1",
    )
    calls = len(client.calls)
    second = collect_tushare_a_share_events(
        client=client,
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
        source_version="events-fixture-v1",
    )

    assert first.completed_partitions == 10
    assert first.reused_partitions == 0
    assert second.completed_partitions == 0
    assert second.reused_partitions == 10
    assert len(client.calls) == calls
    assert first.source_manifest_path.is_file()

    quality = json.loads(first.quality_report_path.read_text(encoding="utf-8"))
    assert quality["complete"] is True
    assert quality["expected_partitions"] == 10
    assert quality["sources"]["forecast"]["revision_diagnostics"] == {
        "groups_with_multiple_announcement_dates": 1,
        "logical_groups": 1,
        "max_announcement_versions": 2,
    }
    assert quality["forecast_type_transition_counts"] == {"略增 -> 预增": 1}
    assert quality["audit_result_distribution"] == {"标准无保留意见": 1}
    assert quality["share_unlock"]["float_ratio_null_rows"] == 0
    holder_quality = quality["sources"]["stk_holdernumber"]
    assert holder_quality["raw_rows"] == 2
    assert holder_quality["normalized_rows"] == 1
    assert holder_quality["field_missing_counts"]["holder_num"] == 1
    assert holder_quality["unusable_rows_excluded_from_canonical_overlay"] == 1
    holder_raw = pl.read_parquet(
        staging / "partitions" / "stk_holdernumber" / "000001_SZ.parquet"
    )
    assert "2024-04-01 15:09:08" in holder_raw["ann_date"].to_list()
    assert quality["research_boundary"]["ready_for_scoring"] is False

    share_calls = [params for name, params in client.call_params if name == "share_float"]
    assert len(share_calls) == 2
    assert all("start_date" not in params and "end_date" not in params for params in share_calls)
    other_calls = [
        params for name, params in client.call_params if name != "share_float"
    ]
    assert all(params["start_date"] == "20240101" for params in other_calls)
    assert all(params["end_date"] == "20241231" for params in other_calls)

    materialized = materialize_tushare_event_overlay(
        source_dir=staging,
        market_dir=market,
        dest_dir=tmp_path / "events-overlay",
    )
    stored, tables = load_verified_event_snapshot(
        tmp_path / "events-overlay",
        expected_market_snapshot_id=materialized.snapshot.base_market_snapshot_id,
    )
    assert stored.snapshot_id == materialized.snapshot.snapshot_id
    assert tables["earnings_express_events"]["summary"].to_list() == [
        "official express summary"
    ]
    assert tables["share_unlock_events"].height == 1


def test_holder_timestamp_does_not_enter_a_usable_event(tmp_path: Path) -> None:
    tables = _event_tables()
    tables["stk_holdernumber"] = pl.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "ann_date": ["2024-04-01 15:09:08"],
            "end_date": ["20240331"],
            "holder_num": [12345],
        }
    )

    with pytest.raises(
        DataQualityError,
        match="stk_holdernumber ann_date is invalid for 000001.SZ",
    ):
        collect_tushare_a_share_events(
            client=FakeTushareClient(tables),
            market_dir=_market(tmp_path),
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            staging_dir=tmp_path / "events-staging",
        )


def test_event_collection_rejects_tampered_completed_partition(tmp_path: Path) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
    )
    partition = staging / "partitions" / "forecast" / "000001_SZ.parquet"
    pl.read_parquet(partition).with_columns(pl.lit("首亏").alias("type")).write_parquet(
        partition
    )

    with pytest.raises(TushareFetchError, match="manifest hashes"):
        collect_tushare_a_share_events(
            client=FakeTushareClient(_event_tables()),
            market_dir=market,
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            staging_dir=staging,
        )


def test_event_collection_resumes_after_partial_interruption(tmp_path: Path) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    with pytest.raises(TushareFetchError, match="simulated event collection interruption"):
        collect_tushare_a_share_events(
            client=_InterruptingClient(_event_tables(), fail_after=3),
            market_dir=market,
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            staging_dir=staging,
        )
    assert len(list((staging / "partitions").glob("*/*.parquet"))) == 3
    assert not (staging / "collection_manifest.json").exists()

    resumed = collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
    )
    assert resumed.reused_partitions == 3
    assert resumed.completed_partitions == 7


def test_share_float_capped_foreign_response_uses_audited_ann_date_fallback(
    tmp_path: Path,
) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    client = _ForeignShareFloatClient(_event_tables())
    result = collect_tushare_a_share_events(
        client=client,
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
    )

    fallback_calls = [
        params
        for name, params in client.call_params
        if name == "share_float"
        and "ts_code" not in params
        and "ann_date" in params
    ]
    assert len(fallback_calls) == 367
    cache_dir = staging / "query-cache" / "share_float" / "000002_SZ"
    assert len(list(cache_dir.glob("*.json"))) == 366
    partition = pl.read_parquet(
        staging / "partitions" / "share_float" / "000002_SZ.parquet"
    )
    assert partition.height == 1
    assert partition["ts_code"].to_list() == ["000002.SZ"]
    audit_path = staging / "query-audit" / "share_float" / "000002_SZ.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["primary_response_rows"] == 6000
    assert audit["announcement_dates_examined"] == 366
    assert audit["pre_listing_dates_skipped"] == 0
    assert audit["announcement_dates_queried"] == 366
    assert audit["fallback_rows_before_symbol_filter"] == 6002
    assert audit["retained_target_rows"] == 1
    paged_cache = json.loads(
        (cache_dir / "20240315.json").read_text(encoding="utf-8")
    )
    assert paged_cache["query_mode"] == "ann_date_offset_pages"
    assert paged_cache["page_rows"] == [6000, 2]
    quality = json.loads(result.quality_report_path.read_text(encoding="utf-8"))
    assert quality["query_fallbacks"] == {
        "all_market_cache_hits": 0,
        "all_market_network_queries": 366,
        "announcement_dates_examined": 366,
        "announcement_dates_queried": 366,
        "count": 1,
        "local_day_cache_files": 366,
        "pre_listing_dates_skipped": 0,
        "retained_target_rows": 1,
        "symbols": ["000002.SZ"],
    }

    calls_before_rerun = len(client.calls)
    resumed = collect_tushare_a_share_events(
        client=client,
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
    )
    assert resumed.reused_partitions == 10
    assert len(client.calls) == calls_before_rerun


def test_share_float_ann_date_fallback_resumes_from_per_day_cache(tmp_path: Path) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    with pytest.raises(TushareFetchError, match="simulated share_float fallback interruption"):
        collect_tushare_a_share_events(
            client=_InterruptingForeignShareFloatClient(
                _event_tables(),
                fail_after_days=10,
            ),
            market_dir=market,
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            staging_dir=staging,
        )
    cache_dir = staging / "query-cache" / "share_float" / "000002_SZ"
    assert len(list(cache_dir.glob("*.json"))) == 10

    resumed_client = _ForeignShareFloatClient(_event_tables())
    result = collect_tushare_a_share_events(
        client=resumed_client,
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
    )
    fallback_calls = [
        params
        for name, params in resumed_client.call_params
        if name == "share_float" and "ann_date" in params and "ts_code" not in params
    ]
    assert len(fallback_calls) == 357
    assert result.collection_manifest_path.is_file()
    assert len(list(cache_dir.glob("*.json"))) == 366


def test_share_float_fallback_skips_verified_pre_listing_dates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    monkeypatch.setattr(
        "app.providers.tushare_event_collection._market_stock_listing_dates",
        lambda _market_dir, _stocks: {
            "000001.SZ": date(1991, 4, 3),
            "000002.SZ": date(2024, 10, 11),
        },
    )
    client = _ForeignShareFloatClient(_event_tables())
    collect_tushare_a_share_events(
        client=client,
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
    )

    fallback_calls = [
        params
        for name, params in client.call_params
        if name == "share_float" and "ann_date" in params and "ts_code" not in params
    ]
    assert len(fallback_calls) == 82
    cache_dir = staging / "query-cache" / "share_float" / "000002_SZ"
    pre_listing = json.loads((cache_dir / "20241010.json").read_text(encoding="utf-8"))
    assert pre_listing == {
        "ann_date": "2024-10-10",
        "listing_date": "2024-10-11",
        "query_mode": "pre_listing_skip",
        "response_rows": 0,
        "rows": [],
        "schema_version": "1",
        "source_name": "share_float",
        "symbol": "000002.SZ",
    }
    audit = json.loads(
        (staging / "query-audit" / "share_float" / "000002_SZ.json").read_text(
            encoding="utf-8"
        )
    )
    assert audit["announcement_dates_examined"] == 366
    assert audit["pre_listing_dates_skipped"] == 284
    assert audit["announcement_dates_queried"] == 82


def test_collection_skips_all_endpoints_for_a_stock_listed_after_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    monkeypatch.setattr(
        "app.providers.tushare_event_collection._market_stock_listing_dates",
        lambda _market_dir, _stocks: {
            "000001.SZ": date(1991, 4, 3),
            "000002.SZ": date(2025, 1, 1),
        },
    )
    client = FakeTushareClient(_event_tables())
    result = collect_tushare_a_share_events(
        client=client,
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
    )

    queried_symbols = [
        str(params.get("ts_code"))
        for _name, params in client.call_params
        if "ts_code" in params
    ]
    assert queried_symbols == ["000001.SZ"] * 5
    for source_name in ("forecast", "express", "stk_holdernumber", "share_float", "fina_audit"):
        frame = pl.read_parquet(
            staging / "partitions" / source_name / "000002_SZ.parquet"
        )
        assert frame.is_empty()
    assert result.completed_partitions == 10


def test_share_float_fallback_reuses_all_market_cache_across_symbols(
    tmp_path: Path,
) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    client = _SharedAllMarketShareFloatClient(_event_tables())
    collect_tushare_a_share_events(
        client=client,
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
    )

    fallback_calls = [
        params
        for name, params in client.call_params
        if name == "share_float" and "ann_date" in params and "ts_code" not in params
    ]
    assert len(fallback_calls) == 366
    shared_dir = staging / "query-cache" / "share_float" / "all-market"
    assert len(list(shared_dir.glob("*.json"))) == 366
    second_audit = json.loads(
        (staging / "query-audit" / "share_float" / "000002_SZ.json").read_text(
            encoding="utf-8"
        )
    )
    assert second_audit["all_market_network_queries"] == 0
    assert second_audit["all_market_cache_hits"] == 366
    assert second_audit["local_day_cache_files"] == 0


def test_quality_report_supports_legacy_query_audits_without_listing_metrics() -> None:
    raw = _event_tables()
    report = _build_quality_report(
        raw,
        normalize_event_sources(raw),
        request_id="request-id",
        base_market_snapshot_id="market-id",
        stocks=["000001.SZ"],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        query_audits=[
            {
                "schema_version": "1",
                "source_name": "share_float",
                "symbol": "000001.SZ",
                "coverage_start": "2024-01-01",
                "coverage_end": "2024-12-31",
                "reasons": ["primary_response_reached_row_limit"],
                "primary_response_rows": 6000,
                "primary_unique_symbols": ["000001.SZ"],
                "fallback_policy": "legacy fixture",
                "announcement_dates_queried": 366,
                "fallback_rows_before_symbol_filter": 1,
                "retained_target_rows": 1,
            }
        ],
    )

    assert report["query_fallbacks"]["local_day_cache_files"] == 366


def test_share_float_ann_date_fallback_rejects_ignored_offset(tmp_path: Path) -> None:
    market = _market(tmp_path)
    with pytest.raises(DataQualityError, match="refusing an ignored offset"):
        collect_tushare_a_share_events(
            client=_IgnoringOffsetShareFloatClient(_event_tables()),
            market_dir=market,
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            staging_dir=tmp_path / "events-staging",
        )


def test_event_collection_rejects_request_drift(tmp_path: Path) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
    )

    with pytest.raises(TushareFetchError, match="different event request"):
        collect_tushare_a_share_events(
            client=FakeTushareClient(_event_tables()),
            market_dir=market,
            start=date(2024, 2, 1),
            end=date(2024, 12, 31),
            staging_dir=staging,
        )


def test_event_collection_rejects_an_entirely_empty_endpoint(tmp_path: Path) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    with pytest.raises(DataQualityError, match="forecast returned no rows"):
        collect_tushare_a_share_events(
            client=FakeTushareClient(
                {
                    "forecast": pl.DataFrame(),
                    "express": pl.DataFrame(),
                    "stk_holdernumber": pl.DataFrame(),
                    "share_float": pl.DataFrame(),
                    "fina_audit": pl.DataFrame(),
                }
            ),
            market_dir=market,
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            staging_dir=staging,
        )
    assert not (staging / "collection_manifest.json").exists()


def test_event_collection_cli_reports_artifacts_without_exposing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    fake = FakeTushareClient(_event_tables())
    monkeypatch.setattr(
        "app.providers.tushare_client.read_tushare_token",
        lambda: "secret-event-token",
    )
    monkeypatch.setattr(
        "app.providers.tushare_client.LiveTushareClient",
        lambda token: fake,
    )

    result = CliRunner().invoke(
        cli_app,
        [
            "collect-tushare-all-a-share-events",
            "--start",
            "2024-01-01",
            "--end",
            "2024-12-31",
            "--market-dir",
            str(market),
            "--staging-dir",
            str(staging),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "completed_partitions=10" in result.output
    assert "source_manifest=" in result.output
    assert "quality_report=" in result.output
    assert "secret-event-token" not in result.output


def _tables_with_impossible_first_ann() -> dict[str, pl.DataFrame]:
    tables = _event_tables()
    forecast = tables["forecast"].vstack(
        pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240310",
                    "end_date": "20231231",
                    "type": "不确定",
                    "p_change_min": 1.0,
                    "p_change_max": 2.0,
                    "net_profit_min": 10.0,
                    "net_profit_max": 20.0,
                    "last_parent_net": 9.0,
                    "first_ann_date": "20240311",
                    "summary": "provider contradiction",
                    "change_reason": "n/a",
                }
            ]
        )
    )
    tables["forecast"] = forecast
    return tables


def test_collector_quarantines_forecast_first_ann_after_ann_with_anomaly_evidence(
    tmp_path: Path,
) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    first = collect_tushare_a_share_events(
        client=FakeTushareClient(_tables_with_impossible_first_ann()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
        source_version="forecast-anomaly-fixture-v1",
    )

    partition = pl.read_parquet(staging / "partitions" / "forecast" / "000001_SZ.parquet")
    bad = partition.filter(
        (pl.col("ann_date") == "20240310") & (pl.col("end_date") == "20231231")
    )
    assert bad.height == 1
    assert bad["first_ann_date"].to_list() == [None]

    anomalies = list((staging / "anomalies" / "forecast").glob("*.json"))
    assert len(anomalies) == 1
    payload = json.loads(anomalies[0].read_text(encoding="utf-8"))
    assert payload["rule"] == "first_ann_date_after_ann_date"
    assert payload["original_first_ann_date"] == "20240311"
    assert payload["source_fields"]["ann_date"] == "20240310"
    assert payload["source_fields"]["first_ann_date"] == "20240311"
    assert payload["request_id"] == first.request_id
    assert payload["symbol"] == "000001.SZ"

    quality = json.loads(first.quality_report_path.read_text(encoding="utf-8"))
    assert quality["provider_field_anomalies"] == {
        "count": 1,
        "rule_distribution": {"first_ann_date_after_ann_date": 1},
        "affected_symbols": ["000001.SZ"],
    }
    manifest = json.loads(first.collection_manifest_path.read_text(encoding="utf-8"))
    relative = anomalies[0].relative_to(staging).as_posix()
    assert relative in manifest["anomaly_hashes"]

    materialized = materialize_tushare_event_overlay(
        source_dir=staging,
        market_dir=market,
        dest_dir=tmp_path / "events-overlay",
    )
    _stored, tables = load_verified_event_snapshot(
        tmp_path / "events-overlay",
        expected_market_snapshot_id=materialized.snapshot.base_market_snapshot_id,
    )
    canonical = tables["earnings_forecast_events"].filter(
        (pl.col("ann_date") == date(2024, 3, 10))
        & (pl.col("report_period") == date(2023, 12, 31))
    )
    assert canonical.height == 1
    assert canonical["first_ann_date"].to_list() == [None]
    assert canonical["ann_date"].to_list() == [date(2024, 3, 10)]


def test_collector_anomaly_artifact_tamper_is_rejected_on_resume(tmp_path: Path) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    collect_tushare_a_share_events(
        client=FakeTushareClient(_tables_with_impossible_first_ann()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
        source_version="forecast-anomaly-fixture-v1",
    )
    anomaly_path = next((staging / "anomalies" / "forecast").glob("*.json"))
    payload = json.loads(anomaly_path.read_text(encoding="utf-8"))
    payload["original_first_ann_date"] = "20990101"
    anomaly_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TushareFetchError, match="anomaly"):
        collect_tushare_a_share_events(
            client=FakeTushareClient(_tables_with_impossible_first_ann()),
            market_dir=market,
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            staging_dir=staging,
            source_version="forecast-anomaly-fixture-v1",
        )


def test_existing_collection_request_without_anomaly_policy_still_resumes(
    tmp_path: Path,
) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    first = collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
        source_version="events-fixture-v1",
    )
    request_path = staging / "collection_request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request.pop("forecast_first_ann_anomaly_policy", None)
    request_path.write_text(
        json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    second = collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
        source_version="events-fixture-v1",
    )
    assert second.request_id == first.request_id
    assert second.reused_partitions == 10
    assert second.completed_partitions == 0
    upgraded_request = json.loads(request_path.read_text(encoding="utf-8"))
    assert upgraded_request["forecast_first_ann_anomaly_policy"]


def test_resume_rewrites_only_a_legacy_forecast_schema_difference(tmp_path: Path) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    first = collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
        source_version="events-fixture-v1",
    )
    for path in (
        first.collection_manifest_path,
        first.quality_report_path,
        first.source_manifest_path,
        *(staging / "exports" / f"{source}.parquet" for source in _event_tables()),
    ):
        path.unlink()

    forecast_path = staging / "partitions" / "forecast" / "000001_SZ.parquet"
    legacy = pl.read_parquet(forecast_path).with_columns(
        [pl.col(name).cast(pl.String) for name in FORECAST_NUMERIC]
    )
    legacy.write_parquet(forecast_path)

    resumed = collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
        source_version="events-fixture-v1",
    )

    upgraded = pl.read_parquet(forecast_path)
    assert resumed.reused_partitions == 10
    assert upgraded.schema["p_change_min"] == pl.Float64
    assert upgraded.schema["net_profit_max"] == pl.Float64


def _rewrite_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _seal_legacy_zero_anomaly_collection(staging: Path) -> str:
    """Strip post-stamp anomaly fields while keeping every other seal intact."""
    quality_path = staging / "quality_report.json"
    manifest_path = staging / "collection_manifest.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "anomaly_hashes" in manifest
    assert "provider_field_anomalies" in quality
    anomaly_files = list((staging / "anomalies").rglob("*.json")) if (staging / "anomalies").is_dir() else []
    assert anomaly_files == []
    quality.pop("provider_field_anomalies")
    _rewrite_json(quality_path, quality)
    manifest.pop("anomaly_hashes")
    manifest["quality_report_sha256"] = _sha256_file(quality_path)
    _rewrite_json(manifest_path, manifest)
    request_id = str(manifest["request_id"])
    _verify_collection_manifest(staging, request_id=request_id)
    return request_id


def test_legacy_sealed_collection_without_anomaly_fields_verifies(tmp_path: Path) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    first = collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
        source_version="events-fixture-v1",
    )
    request_id = _seal_legacy_zero_anomaly_collection(staging)

    resumed = collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
        source_version="events-fixture-v1",
    )
    assert resumed.request_id == first.request_id == request_id
    assert resumed.reused_partitions == 10
    assert resumed.completed_partitions == 0
    manifest = json.loads((staging / "collection_manifest.json").read_text(encoding="utf-8"))
    quality = json.loads((staging / "quality_report.json").read_text(encoding="utf-8"))
    assert "anomaly_hashes" not in manifest
    assert "provider_field_anomalies" not in quality


def test_legacy_collection_rejects_disk_anomaly_without_declared_fields(
    tmp_path: Path,
) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
        source_version="events-fixture-v1",
    )
    request_id = _seal_legacy_zero_anomaly_collection(staging)
    planted = staging / "anomalies" / "forecast" / "planted.json"
    planted.parent.mkdir(parents=True, exist_ok=True)
    _rewrite_json(
        planted,
        {
            "schema_version": "1",
            "source_name": "forecast",
            "symbol": "000001.SZ",
            "rule": "first_ann_date_after_ann_date",
            "original_first_ann_date": "20240311",
            "source_fields": {"ts_code": "000001.SZ"},
            "row_hash": "0" * 64,
            "request_id": request_id,
        },
    )

    with pytest.raises(TushareFetchError, match="anomaly hashes do not match"):
        _verify_collection_manifest(staging, request_id=request_id)


def test_partial_or_mismatched_anomaly_fields_fail_closed(tmp_path: Path) -> None:
    market = _market(tmp_path)
    staging = tmp_path / "events-staging"
    first = collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging,
        source_version="events-fixture-v1",
    )
    request_id = first.request_id
    quality_path = staging / "quality_report.json"
    manifest_path = staging / "collection_manifest.json"

    # Modern anomaly_hashes present, but quality omits provider_field_anomalies.
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality.pop("provider_field_anomalies")
    _rewrite_json(quality_path, quality)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality_report_sha256"] = _sha256_file(quality_path)
    _rewrite_json(manifest_path, manifest)
    with pytest.raises(TushareFetchError, match="anomaly hashes do not match"):
        _verify_collection_manifest(staging, request_id=request_id)

    # Quality declares provider_field_anomalies, but manifest omits anomaly_hashes.
    market2 = _market(tmp_path / "case2")
    staging2 = tmp_path / "case2-staging"
    second = collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market2,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging2,
        source_version="events-fixture-v1",
    )
    manifest2 = json.loads((staging2 / "collection_manifest.json").read_text(encoding="utf-8"))
    manifest2.pop("anomaly_hashes")
    _rewrite_json(staging2 / "collection_manifest.json", manifest2)
    with pytest.raises(TushareFetchError, match="anomaly hashes do not match"):
        _verify_collection_manifest(staging2, request_id=second.request_id)

    # Both modern fields present, but quality count disagrees with disk evidence.
    market3 = _market(tmp_path / "case3")
    staging3 = tmp_path / "case3-staging"
    third = collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market3,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging3,
        source_version="events-fixture-v1",
    )
    quality3_path = staging3 / "quality_report.json"
    quality3 = json.loads(quality3_path.read_text(encoding="utf-8"))
    quality3["provider_field_anomalies"] = {
        "count": 1,
        "rule_distribution": {"first_ann_date_after_ann_date": 1},
        "affected_symbols": ["000001.SZ"],
    }
    _rewrite_json(quality3_path, quality3)
    manifest3 = json.loads((staging3 / "collection_manifest.json").read_text(encoding="utf-8"))
    manifest3["quality_report_sha256"] = _sha256_file(quality3_path)
    _rewrite_json(staging3 / "collection_manifest.json", manifest3)
    with pytest.raises(
        TushareFetchError,
        match="quality report violates research boundaries",
    ):
        _verify_collection_manifest(staging3, request_id=third.request_id)

    # Both modern fields present, but manifest anomaly_hashes disagree with disk.
    market4 = _market(tmp_path / "case4")
    staging4 = tmp_path / "case4-staging"
    fourth = collect_tushare_a_share_events(
        client=FakeTushareClient(_event_tables()),
        market_dir=market4,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        staging_dir=staging4,
        source_version="events-fixture-v1",
    )
    manifest4 = json.loads((staging4 / "collection_manifest.json").read_text(encoding="utf-8"))
    manifest4["anomaly_hashes"] = {"anomalies/forecast/bogus.json": "0" * 64}
    _rewrite_json(staging4 / "collection_manifest.json", manifest4)
    with pytest.raises(TushareFetchError, match="anomaly hashes do not match"):
        _verify_collection_manifest(staging4, request_id=fourth.request_id)
