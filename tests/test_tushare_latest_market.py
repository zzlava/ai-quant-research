from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.errors import DataQualityError, PreflightError, TushareFetchError
from app.pipeline import run_backtest, run_score
from app.preflight import LATEST_MARKET_SNAPSHOT_MODE_LABEL, preflight_research
from app.providers.tushare_client import TOKEN_ENV
from app.providers.tushare_latest_market import fetch_latest_all_a_share_and_import
from app.settings import Settings
from app.storage.duckdb_store import DuckDBParquetStore
from app.strategies.loader import load_strategy_config
from tests.helpers import CONFIG_DIR, PROJECT_ROOT
from tests.tushare_fakes import STOCKS, FakeTushareClient, build_fake_tushare_api_tables


def _config():
    return load_strategy_config("all_a_share_latest_v1", CONFIG_DIR)


def test_latest_all_a_share_fetch_uses_date_batches_and_filters_current_st(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    tables["stock_basic"] = pl.concat(
        [
            tables["stock_basic"],
            pl.DataFrame(
                {
                    "ts_code": ["430001.BJ", "900901.SH", "301999.SZ"],
                    "name": ["北交所样本", "B 股样本", "新上市样本"],
                    "industry": ["bank", "bank", "bank"],
                    "list_date": ["20200101", "20200101", calendar[-1].strftime("%Y%m%d")],
                    "delist_date": [None, None, None],
                    "market": ["北交所", "主板", "创业板"],
                    "exchange": ["BSE", "SSE", "SZSE"],
                    "list_status": ["L", "L", "L"],
                }
            ),
        ],
        how="vertical_relaxed",
    )
    client = FakeTushareClient(tables)
    destination = tmp_path / "data" / "parquet"

    result = fetch_latest_all_a_share_and_import(
        requested_as_of=calendar[-1],
        config=_config(),
        dest_dir=destination,
        client=client,
    )

    assert result.requested_as_of == calendar[-1]
    assert result.as_of == calendar[-1]
    assert result.candidate_count == len(STOCKS)
    expected_days = calendar[-60:]
    assert result.snapshot.coverage_start == expected_days[0]
    assert result.snapshot.coverage_end == expected_days[-1]
    assert result.snapshot.source_name == "tushare_latest_all_a_share"

    daily_calls = [params for name, params in client.call_params if name == "daily"]
    assert [params["trade_date"] for params in daily_calls] == [
        day.strftime("%Y%m%d") for day in calendar[-61:]
    ]
    assert all("ts_code" not in params for params in daily_calls)
    stock_basic_calls = [params for name, params in client.call_params if name == "stock_basic"]
    assert stock_basic_calls == [
        {
            "list_status": "L",
            "fields": "ts_code,name,industry,list_date,delist_date,market,exchange,list_status",
        }
    ]
    assert "namechange" not in client.calls

    daily = pl.read_parquet(destination / "daily_bars.parquet")
    assert set(daily["symbol"].to_list()) == set(STOCKS)
    current_st = daily.filter((pl.col("symbol") == "600000.SH") & (pl.col("date") == result.as_of))
    assert current_st["is_st"].to_list() == [True]
    membership = pl.read_parquet(destination / "universe_membership.parquet")
    assert membership.height == len(expected_days) * len(STOCKS)
    assert set(membership["universe_id"].to_list()) == {"all_a_share_latest_cn"}

    settings = Settings(data_dir=tmp_path / "data", config_dir=PROJECT_ROOT / "config")
    checked = preflight_research(
        store=DuckDBParquetStore(destination, snapshot=result.snapshot),
        config=_config(),
        start=result.as_of,
        end=result.as_of,
    )
    assert checked.research_mode == LATEST_MARKET_SNAPSHOT_MODE_LABEL
    scores = run_score(result.as_of, "all_a_share_latest_v1", settings=settings)
    assert {item.symbol for item in scores} == {"000001.SZ"}


def test_latest_market_uses_official_pre_close_when_the_feature_window_starts_suspended(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    first_feature_day = calendar[-60]
    calendar, tables = build_fake_tushare_api_tables(
        suspend_days={("000001.SZ", first_feature_day)},
    )

    result = fetch_latest_all_a_share_and_import(
        requested_as_of=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "data" / "parquet",
        client=FakeTushareClient(tables),
    )

    daily = pl.read_parquet(tmp_path / "data" / "parquet" / "daily_bars.parquet")
    first_bar = daily.filter((pl.col("symbol") == "000001.SZ") & (pl.col("date") == first_feature_day))
    assert first_bar.height == 1
    assert first_bar["is_suspended"].to_list() == [True]
    assert first_bar["close"].to_list() == [pytest.approx(10.0)]
    assert result.snapshot.coverage_start == first_feature_day


def test_latest_market_rejects_backtests_and_multi_day_preflight(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    result = fetch_latest_all_a_share_and_import(
        requested_as_of=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "data" / "parquet",
        client=FakeTushareClient(tables),
    )
    store = DuckDBParquetStore(tmp_path / "data" / "parquet", snapshot=result.snapshot)
    with pytest.raises(PreflightError, match="one as-of date"):
        preflight_research(
            store=store,
            config=_config(),
            start=calendar[-2],
            end=calendar[-1],
        )

    settings = Settings(data_dir=tmp_path / "data", config_dir=PROJECT_ROOT / "config")
    with pytest.raises(PreflightError, match="backtests are disabled"):
        run_backtest("all_a_share_latest_v1", calendar[-2], calendar[-1], settings=settings)


def test_latest_market_requires_full_warmup_and_new_destination(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables(n_days=20)
    with pytest.raises(TushareFetchError, match="need 60 for warm-up"):
        fetch_latest_all_a_share_and_import(
            requested_as_of=calendar[-1],
            config=_config(),
            dest_dir=tmp_path / "short" / "parquet",
            client=FakeTushareClient(tables),
        )

    full_calendar, full_tables = build_fake_tushare_api_tables()
    destination = tmp_path / "occupied" / "parquet"
    destination.mkdir(parents=True)
    (destination / "unrelated.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(TushareFetchError, match="destination already contains"):
        fetch_latest_all_a_share_and_import(
            requested_as_of=full_calendar[-1],
            config=_config(),
            dest_dir=destination,
            client=FakeTushareClient(full_tables),
        )
    with pytest.raises(TushareFetchError, match="refusing to replace unrelated files"):
        fetch_latest_all_a_share_and_import(
            requested_as_of=full_calendar[-1],
            config=_config(),
            dest_dir=destination,
            client=FakeTushareClient(full_tables),
            replace_existing=True,
        )


def test_latest_market_rejects_missing_turnover_instead_of_using_zero(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    target = calendar[-1]
    tables["daily_basic"] = tables["daily_basic"].filter(
        ~((pl.col("ts_code") == "000001.SZ") & (pl.col("trade_date") == target.strftime("%Y%m%d")))
    )
    with pytest.raises(DataQualityError, match="daily_basic missing turnover_rate"):
        fetch_latest_all_a_share_and_import(
            requested_as_of=target,
            config=_config(),
            dest_dir=tmp_path / "data" / "parquet",
            client=FakeTushareClient(tables),
        )


def test_latest_market_cli_rejects_wrong_scope_before_reading_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def token_read_must_not_run() -> str:
        raise AssertionError("Tushare token must not be read for the wrong strategy scope")

    monkeypatch.setattr("app.providers.tushare_client.read_tushare_token", token_read_must_not_run)
    monkeypatch.setenv(TOKEN_ENV, "not-used")
    monkeypatch.setenv("AIQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    result = CliRunner().invoke(
        cli_app,
        [
            "fetch-tushare-latest-all-a-share",
            "--as-of",
            "2024-01-19",
            "--strategy",
            "baseline_v1",
        ],
    )
    assert result.exit_code != 0
    combined = (result.stdout or "") + (result.stderr or "")
    assert "research_scope=latest_market_snapshot" in combined
