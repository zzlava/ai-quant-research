from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.api.main import app
from app.cli import app as cli_app
from app.clock import available_at_utc
from app.demo.generator import generate_demo_market, write_demo_parquet
from app.errors import DataQualityError, MissingTushareTokenError, TushareFetchError, sanitize_error_message
from app.pipeline import run_backtest, run_score
from app.providers.tushare_client import TOKEN_ENV, read_tushare_token
from app.providers.tushare_fetch import fetch_tushare_and_import, read_symbols_file
from app.providers.tushare_normalize import require_ts_code, split_session_symbols
from app.storage.snapshot_io import load_verified_snapshot
from app.strategies.loader import load_strategy_config
from tests.helpers import PROJECT_ROOT, weekdays
from tests.tushare_fakes import STOCKS, FakeTushareClient, build_fake_tushare_api_tables


def _config():
    return load_strategy_config("baseline_real_cn_v1", PROJECT_ROOT / "config" / "strategies")


def test_missing_token_fails_without_env_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setenv("OTHER_SECRET", "should-not-appear")
    with pytest.raises(MissingTushareTokenError, match="not configured") as exc_info:
        read_tushare_token()
    message = str(exc_info.value)
    assert "should-not-appear" not in message
    assert "=" not in message


def test_token_is_redacted_from_cli_and_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = "super-secret-tushare-token"
    monkeypatch.setenv(TOKEN_ENV, secret)
    monkeypatch.setenv("AIQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "fetch-tushare",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-31",
            "--strategy",
            "baseline_real_cn_v1",
        ],
    )
    assert result.exit_code in {1, 2}
    combined = (result.stdout or "") + (result.stderr or "")
    assert secret not in combined
    assert sanitize_error_message(ValueError(f"token={secret}")) != f"token={secret}"
    assert secret not in sanitize_error_message(ValueError(f"token={secret}"))


def test_fake_tushare_builds_five_tables_and_imports(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    client = FakeTushareClient(tables)
    snapshot = fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "parquet",
        stocks=list(STOCKS),
        client=client,
        source_version="batch-1",
    )
    verified = load_verified_snapshot(tmp_path / "parquet")
    assert verified.snapshot_id == snapshot.snapshot_id
    assert snapshot.source_name == "tushare"
    assert snapshot.adjustment == "forward"
    for name in ("daily_bars", "index_bars", "global_bars", "instruments", "calendar", "universe_membership"):
        assert (tmp_path / "parquet" / f"{name}.parquet").exists()
    membership = pl.read_parquet(tmp_path / "parquet" / "universe_membership.parquet")
    assert set(membership["universe_id"].to_list()) == {"manual_real_cn"}
    assert set(membership["as_of_date"].to_list()) == set(calendar)
    assert set(membership["symbol"].to_list()) == set(STOCKS)
    daily = pl.read_parquet(tmp_path / "parquet" / "daily_bars.parquet")
    assert set(daily["symbol"].to_list()) == set(STOCKS)
    statuses = [params.get("list_status") for name, params in client.call_params if name == "stock_basic"]
    assert statuses == ["L", "D", "P", "G"]
    daily_calls = [params for name, params in client.call_params if name == "daily"]
    assert len(daily_calls) == len(STOCKS)
    assert all(isinstance(params.get("ts_code"), str) and "," not in str(params["ts_code"]) for params in daily_calls)
    daily_basic_calls = [params for name, params in client.call_params if name == "daily_basic"]
    assert len(daily_basic_calls) == len(STOCKS)
    assert all(
        isinstance(params.get("ts_code"), str) and "," not in str(params["ts_code"])
        for params in daily_basic_calls
    )
    index_codes = [params.get("ts_code") for name, params in client.call_params if name == "index_daily"]
    assert index_codes
    assert all(isinstance(code, str) and "," not in code for code in index_codes)
    _, expected_globals = split_session_symbols(_config(), list(STOCKS))
    global_codes = [params.get("ts_code") for name, params in client.call_params if name == "index_global"]
    assert global_codes
    assert all(isinstance(code, str) and "," not in code for code in global_codes)
    limit_calls = [params for name, params in client.call_params if name == "stk_limit"]
    assert len(limit_calls) == len(STOCKS)
    assert all(isinstance(params.get("ts_code"), str) and "," not in str(params["ts_code"]) for params in limit_calls)
    assert all(
        params.get("fields") == "ts_code,trade_date,pre_close,up_limit,down_limit" for params in limit_calls
    )
    factor_calls = [params for name, params in client.call_params if name == "adj_factor"]
    assert len(factor_calls) == len(STOCKS)
    assert all(isinstance(params.get("ts_code"), str) and "," not in str(params["ts_code"]) for params in factor_calls)
    if len(expected_globals) >= 2:
        assert set(global_codes) == set(expected_globals)


def test_tushare_snapshot_reaches_score_backtest_and_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    monkeypatch.setenv("AIQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    monkeypatch.setenv("AIQ_DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    imported = fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "data" / "parquet",
        stocks=list(STOCKS),
        client=FakeTushareClient(tables),
    )
    as_of = date(2024, 1, 15)
    scores = run_score(as_of, "baseline_real_cn_v1")
    assert scores
    assert {row.data_snapshot_id for row in scores} == {imported.snapshot_id}
    result = run_backtest("baseline_real_cn_v1", date(2024, 1, 2), calendar[-1])
    assert result.data_snapshot_id == imported.snapshot_id
    client = TestClient(app)
    ranking = client.get("/ranking", params={"date": "2024-01-15", "strategy": "baseline_real_cn_v1", "top": 5})
    assert ranking.status_code == 200
    assert ranking.json()["data_snapshot_id"] == imported.snapshot_id
    created = client.post(
        "/backtests",
        json={"strategy": "baseline_real_cn_v1", "start": "2024-01-02", "end": calendar[-1].isoformat()},
    )
    assert created.status_code == 200
    assert created.json()["result"]["data_snapshot_id"] == imported.snapshot_id


def test_failed_tushare_import_keeps_existing_snapshot(tmp_path: Path) -> None:
    dest = tmp_path / "parquet"
    demo = generate_demo_market(seed=42, n_stocks=8, start=date(2023, 1, 3), end=date(2024, 3, 29))
    previous = write_demo_parquet(demo, dest)
    calendar, tables = build_fake_tushare_api_tables()
    client = FakeTushareClient(tables, absent={"stk_limit"})
    with pytest.raises(DataQualityError, match="stk_limit"):
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=_config(),
            dest_dir=dest,
            stocks=list(STOCKS),
            client=client,
        )
    assert load_verified_snapshot(dest).snapshot_id == previous.snapshot_id


def test_tushare_global_available_at_is_naive_utc(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "parquet",
        stocks=list(STOCKS),
        client=FakeTushareClient(tables),
    )
    glob = pl.read_parquet(tmp_path / "parquet" / "global_bars.parquet")
    sample = glob.filter((pl.col("symbol") == "SPX") & (pl.col("date") == date(2024, 1, 2))).to_dicts()
    assert sample
    value = sample[0]["available_at"]
    assert value.tzinfo is None
    expected = available_at_utc(date(2024, 1, 2), _config().data.sessions["SPX"])
    assert value == expected


def test_offset_global_timestamp_still_rejected_after_tushare_flow(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "good",
        stocks=list(STOCKS),
        client=FakeTushareClient(tables),
    )
    src = tmp_path / "tampered"
    src.mkdir()
    for name in ("daily_bars", "index_bars", "instruments", "calendar", "universe_membership"):
        pl.read_parquet(tmp_path / "good" / f"{name}.parquet").write_csv(src / f"{name}.csv")
    glob = pl.read_parquet(tmp_path / "good" / "global_bars.parquet").with_columns(
        pl.lit("2024-01-02T16:00:00-05:00").alias("available_at")
    )
    glob.write_csv(src / "global_bars.csv")
    from app.storage.import_market import import_market_data

    with pytest.raises(DataQualityError, match="non-zero offsets"):
        import_market_data(src, tmp_path / "bad", source_name="tushare", adjustment="forward")


def test_limit_pct_from_stk_limit_not_st_guess(tmp_path: Path) -> None:
    calendar, _base = build_fake_tushare_api_tables()
    day_20 = calendar[-5]
    day_null = calendar[-4]
    _, tables = build_fake_tushare_api_tables(
        limit_override={
            ("000001.SZ", day_20): (10.0, 12.0, 8.0),
            ("000001.SZ", day_null): (10.0, None, None),
        }
    )
    fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "parquet",
        stocks=list(STOCKS),
        client=FakeTushareClient(tables),
    )
    daily = pl.read_parquet(tmp_path / "parquet" / "daily_bars.parquet")
    twenty = daily.filter((pl.col("symbol") == "000001.SZ") & (pl.col("date") == day_20)).to_dicts()[0]
    empty = daily.filter((pl.col("symbol") == "000001.SZ") & (pl.col("date") == day_null)).to_dicts()[0]
    assert twenty["price_limit_pct"] == pytest.approx(0.20)
    assert empty["price_limit_pct"] is None
    st_row = daily.filter(pl.col("symbol") == "600000.SH").to_dicts()[0]
    assert st_row["is_st"] is True
    assert st_row["price_limit_pct"] == pytest.approx(0.10)


def test_missing_stk_limit_does_not_default_to_ten_percent() -> None:
    calendar, tables = build_fake_tushare_api_tables(drop_limit_keys={("000001.SZ", date(2024, 1, 15))})
    with pytest.raises(DataQualityError, match="refusing to default price_limit_pct"):
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=_config(),
            dest_dir=Path("/unused"),
            stocks=list(STOCKS),
            client=FakeTushareClient(tables),
        )


def test_missing_st_or_suspend_records_are_not_invented() -> None:
    calendar, tables = build_fake_tushare_api_tables()
    with pytest.raises(DataQualityError, match="namechange"):
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=_config(),
            dest_dir=Path("/unused"),
            stocks=list(STOCKS),
            client=FakeTushareClient(tables, absent={"namechange"}),
        )
    with pytest.raises(DataQualityError, match="suspend_d"):
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=_config(),
            dest_dir=Path("/unused"),
            stocks=list(STOCKS),
            client=FakeTushareClient(tables, absent={"suspend_d"}),
        )


def test_unknown_daily_gap_is_rejected() -> None:
    calendar, tables = build_fake_tushare_api_tables(skip_daily={("000001.SZ", date(2024, 1, 16))})
    with pytest.raises(DataQualityError, match="unknown daily gap"):
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=_config(),
            dest_dir=Path("/unused"),
            stocks=list(STOCKS),
            client=FakeTushareClient(tables),
        )


def test_explicit_suspend_synthesizes_untradeable_bar(tmp_path: Path) -> None:
    day = date(2024, 1, 16)
    calendar, tables = build_fake_tushare_api_tables(suspend_days={("000001.SZ", day)})
    fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "parquet",
        stocks=list(STOCKS),
        client=FakeTushareClient(tables),
    )
    daily = pl.read_parquet(tmp_path / "parquet" / "daily_bars.parquet")
    row = daily.filter((pl.col("symbol") == "000001.SZ") & (pl.col("date") == day)).to_dicts()[0]
    assert row["is_suspended"] is True
    assert row["volume"] == 0.0
    assert row["amount"] == 0.0
    assert row["open"] == row["close"]


def test_symbol_suffix_is_not_inferred(tmp_path: Path) -> None:
    with pytest.raises(DataQualityError, match="suffixes are not inferred"):
        require_ts_code("000001", kind="stock")
    (tmp_path / "symbols.txt").write_text("000001\n", encoding="utf-8")
    with pytest.raises(DataQualityError, match="suffixes are not inferred"):
        read_symbols_file(tmp_path / "symbols.txt")


def test_index_universe_cli_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "unused-token")
    monkeypatch.setenv("AIQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    (tmp_path / "symbols.txt").write_text("000001.SZ\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "fetch-tushare",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-31",
            "--strategy",
            "baseline_real_cn_v1",
            "--symbols-file",
            str(tmp_path / "symbols.txt"),
            "--index-universe",
            "000300.SH",
        ],
    )
    assert result.exit_code != 0
    combined = ((result.stdout or "") + (result.stderr or "")).lower()
    assert "no such option" in combined
    assert "index-universe" in combined


def test_empty_symbols_are_rejected() -> None:
    calendar, tables = build_fake_tushare_api_tables()
    with pytest.raises(TushareFetchError, match="index-universe is disabled"):
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=_config(),
            dest_dir=Path("/unused"),
            stocks=[],
            client=FakeTushareClient(tables),
        )


def test_pre_listing_gap_is_allowed(tmp_path: Path) -> None:
    listed_on = date(2024, 1, 10)
    preview = weekdays(date(2023, 10, 2), 80)
    calendar, tables = build_fake_tushare_api_tables(
        list_dates={"000001.SZ": listed_on},
        skip_daily={("000001.SZ", day) for day in preview if day < listed_on},
    )
    fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "parquet",
        stocks=list(STOCKS),
        client=FakeTushareClient(tables),
    )
    daily = pl.read_parquet(tmp_path / "parquet" / "daily_bars.parquet")
    early = daily.filter((pl.col("symbol") == "000001.SZ") & (pl.col("date") < listed_on))
    listed = daily.filter((pl.col("symbol") == "000001.SZ") & (pl.col("date") >= listed_on))
    assert early.is_empty()
    assert not listed.is_empty()


def test_daily_bars_are_clipped_to_requested_range(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    start = date(2023, 10, 16)
    end = date(2024, 1, 5)
    assert calendar[0] < start
    assert calendar[-1] > end
    fetch_tushare_and_import(
        start=start,
        end=end,
        config=_config(),
        dest_dir=tmp_path / "parquet",
        stocks=list(STOCKS),
        client=FakeTushareClient(tables),
    )
    daily = pl.read_parquet(tmp_path / "parquet" / "daily_bars.parquet")
    assert daily["date"].min() >= start
    assert daily["date"].max() <= end
    cal = pl.read_parquet(tmp_path / "parquet" / "calendar.parquet")
    assert cal["date"].min() >= start
    assert cal["date"].max() <= end


def test_delisted_stock_basic_is_fetched_by_list_status(tmp_path: Path) -> None:
    delist_on = date(2024, 1, 22)
    preview = weekdays(date(2023, 10, 2), 80)
    calendar, tables = build_fake_tushare_api_tables(
        delist_dates={"000001.SZ": delist_on},
        skip_daily={("000001.SZ", day) for day in preview if day >= delist_on},
    )
    assert tables["stock_basic"].filter(pl.col("ts_code") == "000001.SZ")["list_status"].to_list() == ["D"]
    client = FakeTushareClient(tables)
    fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "parquet",
        stocks=list(STOCKS),
        client=client,
    )
    statuses = [params.get("list_status") for name, params in client.call_params if name == "stock_basic"]
    assert statuses == ["L", "D", "P", "G"]
    instruments = pl.read_parquet(tmp_path / "parquet" / "instruments.parquet")
    assert "000001.SZ" in instruments["symbol"].to_list()


def test_stock_basic_continues_when_one_list_status_fails(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()

    class RejectGClient(FakeTushareClient):
        def query(self, api_name: str, **params: object) -> pl.DataFrame:
            if api_name == "stock_basic" and params.get("list_status") == "G":
                raise TushareFetchError("tushare stock_basic query failed")
            return super().query(api_name, **params)

    client = RejectGClient(tables)
    fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "parquet",
        stocks=list(STOCKS),
        client=client,
    )
    statuses = [params.get("list_status") for name, params in client.call_params if name == "stock_basic"]
    assert statuses == ["L", "D", "P"]
    daily = pl.read_parquet(tmp_path / "parquet" / "daily_bars.parquet")
    assert set(daily["symbol"].to_list()) == set(STOCKS)


def test_stock_basic_all_list_status_failures_include_reasons() -> None:
    calendar, tables = build_fake_tushare_api_tables()

    class RejectAllClient(FakeTushareClient):
        def query(self, api_name: str, **params: object) -> pl.DataFrame:
            if api_name == "stock_basic":
                raise TushareFetchError("rate limit exceeded")
            return super().query(api_name, **params)

    with pytest.raises(TushareFetchError, match=r"L: rate limit exceeded") as exc_info:
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=_config(),
            dest_dir=Path("/unused"),
            stocks=list(STOCKS),
            client=RejectAllClient(tables),
        )
    message = str(exc_info.value)
    assert "D: rate limit exceeded" in message
    assert "P: rate limit exceeded" in message
    assert "G: rate limit exceeded" in message


def test_stock_basic_missing_delisted_stock_includes_d_failure() -> None:
    delist_on = date(2024, 1, 22)
    preview = weekdays(date(2023, 10, 2), 80)
    calendar, tables = build_fake_tushare_api_tables(
        delist_dates={"000001.SZ": delist_on},
        skip_daily={("000001.SZ", day) for day in preview if day >= delist_on},
    )

    class RejectDClient(FakeTushareClient):
        def query(self, api_name: str, **params: object) -> pl.DataFrame:
            if api_name == "stock_basic" and params.get("list_status") == "D":
                raise TushareFetchError("rate limit exceeded")
            return super().query(api_name, **params)

    with pytest.raises(DataQualityError, match="stock_basic missing 000001.SZ") as exc_info:
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=_config(),
            dest_dir=Path("/unused"),
            stocks=list(STOCKS),
            client=RejectDClient(tables),
        )
    assert "D: rate limit exceeded" in str(exc_info.value)


def test_stock_basic_status_failures_redact_token() -> None:
    calendar, tables = build_fake_tushare_api_tables()
    secret = "super-secret-tushare-token"

    class LeakTokenClient(FakeTushareClient):
        def query(self, api_name: str, **params: object) -> pl.DataFrame:
            if api_name == "stock_basic":
                raise RuntimeError(f"token={secret} AIQ_TUSHARE_TOKEN={secret}")
            return super().query(api_name, **params)

    with pytest.raises(TushareFetchError, match=r"L: token=<redacted>") as exc_info:
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=_config(),
            dest_dir=Path("/unused"),
            stocks=list(STOCKS),
            client=LeakTokenClient(tables),
        )
    message = str(exc_info.value)
    assert secret not in message
    assert "token=<redacted>" in message
    assert "AIQ_TUSHARE_TOKEN=<redacted>" in message


def test_listed_stock_with_no_daily_rows_is_rejected() -> None:
    preview = weekdays(date(2023, 10, 2), 80)
    calendar, tables = build_fake_tushare_api_tables(
        skip_daily={("000001.SZ", day) for day in preview},
    )
    with pytest.raises(DataQualityError, match="unknown daily gap"):
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=_config(),
            dest_dir=Path("/unused"),
            stocks=list(STOCKS),
            client=FakeTushareClient(tables),
        )


def test_daily_bars_outside_listing_window_are_dropped(tmp_path: Path) -> None:
    listed_on = date(2024, 1, 10)
    delist_on = date(2024, 1, 22)
    calendar, tables = build_fake_tushare_api_tables(
        list_dates={"000001.SZ": listed_on},
        delist_dates={"000001.SZ": delist_on},
    )
    fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "parquet",
        stocks=list(STOCKS),
        client=FakeTushareClient(tables),
    )
    daily = pl.read_parquet(tmp_path / "parquet" / "daily_bars.parquet")
    owned = daily.filter(pl.col("symbol") == "000001.SZ")
    assert owned.filter(pl.col("date") < listed_on).is_empty()
    assert owned.filter(pl.col("date") >= delist_on).is_empty()
    assert not owned.filter((pl.col("date") >= listed_on) & (pl.col("date") < delist_on)).is_empty()


def test_post_delist_gap_is_allowed(tmp_path: Path) -> None:
    delist_on = date(2024, 1, 22)
    preview = weekdays(date(2023, 10, 2), 80)
    calendar, tables = build_fake_tushare_api_tables(
        delist_dates={"000001.SZ": delist_on},
        skip_daily={("000001.SZ", day) for day in preview if day >= delist_on},
    )
    fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=_config(),
        dest_dir=tmp_path / "parquet",
        stocks=list(STOCKS),
        client=FakeTushareClient(tables),
    )
    daily = pl.read_parquet(tmp_path / "parquet" / "daily_bars.parquet")
    after = daily.filter((pl.col("symbol") == "000001.SZ") & (pl.col("date") >= delist_on))
    before = daily.filter((pl.col("symbol") == "000001.SZ") & (pl.col("date") < delist_on))
    assert after.is_empty()
    assert not before.is_empty()
