from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.errors import TushareFetchError
from app.providers.tushare_all_market_history import (
    build_derived_liquid_membership,
    collect_tushare_all_a_share_history,
    materialize_tushare_all_a_share_history,
    select_historical_a_share,
)
from app.providers.tushare_normalize import is_full_day_suspend_timing
from app.storage.snapshot_io import load_verified_snapshot
from app.strategies.loader import load_strategy_config
from tests.helpers import CONFIG_DIR
from tests.tushare_fakes import FakeTushareClient, build_fake_tushare_api_tables


def _config():
    return load_strategy_config("all_a_share_historical_value_quality_v1", CONFIG_DIR)


def test_select_historical_a_share_keeps_window_delisting_and_excludes_bj_b() -> None:
    frame = pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "600001.SH", "430001.BJ", "900901.SH", "200001.SZ", "T600018.SH"],
            "name": ["A", "retired", "BJ", "SH B", "SZ B", "retired provider code"],
            "industry": ["x"] * 6,
            "list_date": ["20000101"] * 6,
            "delist_date": [None, "20240601", None, None, None, "20061020"],
            "market": ["主板", "主板", "北交所", "主板", "主板", None],
            "exchange": ["SZSE", "SSE", "BSE", "SSE", "SZSE", "SSE"],
            "list_status": ["L", "D", "L", "L", "L", "D"],
        }
    )

    selected = select_historical_a_share(
        frame,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )

    assert selected == ["000001.SZ", "600001.SH"]


def test_collection_resumes_and_materializes_pit_liquid_membership(tmp_path: Path) -> None:
    calendar, _ = build_fake_tushare_api_tables()
    st_day = calendar[35]
    suspend_day = calendar[36]
    calendar, tables = build_fake_tushare_api_tables(suspend_days={("000001.SZ", suspend_day)})
    tables["namechange"] = pl.DataFrame(
        {
            "ts_code": ["600000.SH"],
            "name": ["ST浦发"],
            "start_date": [st_day.strftime("%Y%m%d")],
            "end_date": [st_day.strftime("%Y%m%d")],
            "ann_date": [(st_day - timedelta(days=1)).strftime("%Y%m%d")],
            "change_reason": ["ST"],
        }
    )
    start = calendar[20]
    end = calendar[-1]
    staging = tmp_path / "staging"
    client = FakeTushareClient(tables)

    first = collect_tushare_all_a_share_history(
        client=client,
        config=_config(),
        start=start,
        end=end,
        staging_dir=staging,
    )
    call_count = len(client.calls)
    second = collect_tushare_all_a_share_history(
        client=client,
        config=_config(),
        start=start,
        end=end,
        staging_dir=staging,
    )

    assert first.selected_stocks == 2
    assert first.completed_partitions > 0
    assert second.completed_partitions == 0
    assert second.reused_partitions == first.completed_partitions
    # The second run reuses every date partition; no market-day endpoint is called again.
    assert len(client.calls) == call_count

    destination = tmp_path / "snapshot"
    result = materialize_tushare_all_a_share_history(
        staging_dir=staging,
        dest_dir=destination,
        config=_config(),
    )

    assert result.snapshot.source_name == "tushare_all_a_share_history"
    assert result.snapshot.adjustment == "backward"
    assert result.min_members == 1
    assert result.max_members == 2
    assert load_verified_snapshot(destination).snapshot_id == result.snapshot.snapshot_id
    membership = pl.read_parquet(destination / "universe_membership.parquet")
    st_members = set(membership.filter(pl.col("as_of_date") == st_day)["symbol"].to_list())
    suspended_members = set(
        membership.filter(pl.col("as_of_date") == suspend_day)["symbol"].to_list()
    )
    assert st_members == {"000001.SZ"}
    assert suspended_members == {"600000.SH"}
    available = membership["available_at"].to_list()[0]
    assert available.hour == 9 and available.minute == 30  # 17:30 Asia/Shanghai in UTC


def test_materializer_rejects_tampered_partition(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    start = calendar[20]
    staging = tmp_path / "staging"
    collect_tushare_all_a_share_history(
        client=FakeTushareClient(tables),
        config=_config(),
        start=start,
        end=calendar[-1],
        staging_dir=staging,
    )
    partition = next((staging / "partitions" / "daily").glob("*.parquet"))
    frame = pl.read_parquet(partition).with_columns((pl.col("close") + 1.0).alias("close"))
    frame.write_parquet(partition)

    with pytest.raises(TushareFetchError, match="manifest hashes"):
        materialize_tushare_all_a_share_history(
            staging_dir=staging,
            dest_dir=tmp_path / "snapshot",
            config=_config(),
        )


def test_derived_membership_excludes_recent_listing_and_low_liquidity() -> None:
    config = _config()
    days = [date(2024, 1, 2) + timedelta(days=offset) for offset in range(25)]
    target_days = days[-5:]
    symbols = ["000001.SZ", "000002.SZ", "600001.SH"]
    amounts = {"000001.SZ": 200_000_000.0, "000002.SZ": 200_000_000.0, "600001.SH": 10_000_000.0}
    amount_history = pl.DataFrame(
        [
            {"symbol": symbol, "date": day, "amount": amounts[symbol]}
            for symbol in symbols
            for day in days
        ]
    ).with_columns(pl.col("date").cast(pl.Date))
    daily = pl.DataFrame(
        [
            {
                "symbol": symbol,
                "date": day,
                "amount": amounts[symbol],
                "is_st": False,
                "is_suspended": False,
            }
            for symbol in symbols
            for day in target_days
        ]
    ).with_columns(pl.col("date").cast(pl.Date))
    instruments = pl.DataFrame(
        {
            "symbol": symbols,
            "listing_date": [date(2000, 1, 1), date(2023, 12, 1), date(2000, 1, 1)],
            "is_index": [False, False, False],
            "is_global": [False, False, False],
        }
    ).with_columns(pl.col("listing_date").cast(pl.Date))
    tables = {
        "daily_bars": daily,
        "instruments": instruments,
        "calendar": pl.DataFrame({"date": target_days}).with_columns(pl.col("date").cast(pl.Date)),
    }

    membership = build_derived_liquid_membership(
        tables,
        config=config,
        amount_history=amount_history,
    )

    assert set(membership["symbol"].to_list()) == {"000001.SZ"}
    assert membership.group_by("as_of_date").len()["len"].to_list() == [1] * 5


def test_is_full_day_suspend_timing_accepts_zero_width_open_window() -> None:
    assert is_full_day_suspend_timing(None) is True
    assert is_full_day_suspend_timing("") is True
    assert is_full_day_suspend_timing("None") is True
    assert is_full_day_suspend_timing("09:30-09:30") is True
    assert is_full_day_suspend_timing(" 09:30-09:30 ") is True
    assert is_full_day_suspend_timing("10:00-10:00") is False
    assert is_full_day_suspend_timing("10:24-10:34") is False
    assert is_full_day_suspend_timing("09:30-09:40,10:41-10:51") is False
    assert is_full_day_suspend_timing("11:30-11:30") is False


def test_zero_width_suspend_timing_covers_missing_daily_without_legacy_interval(
    tmp_path: Path,
) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    halted = calendar[35]
    calendar, tables = build_fake_tushare_api_tables(
        skip_daily={("000001.SZ", halted)},
        drop_limit_keys={("000001.SZ", halted)},
    )
    tables["suspend_d"] = pl.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": [halted.strftime("%Y%m%d")],
            "suspend_type": ["S"],
            "suspend_timing": ["09:30-09:30"],
        }
    )
    tables["namechange"] = pl.DataFrame(
        {
            "ts_code": ["600000.SH"],
            "name": ["浦发银行"],
            "start_date": ["19991110"],
            "end_date": [None],
            "ann_date": ["19991110"],
            "change_reason": ["上市"],
        }
    )
    # Stale null-resume interval from a prior incomplete attempt must not poison
    # later trading days once suspend_d already explains the gap.
    staging = tmp_path / "staging"
    (staging / "reference").mkdir(parents=True)
    pl.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "suspend_date": [halted.strftime("%Y%m%d")],
            "resume_date": [None],
            "suspend_reason": ["stale"],
        }
    ).write_parquet(staging / "reference" / "suspend_intervals.parquet")

    collect_tushare_all_a_share_history(
        client=FakeTushareClient(tables),
        config=_config(),
        start=calendar[20],
        end=calendar[-1],
        staging_dir=staging,
    )
    intervals = pl.read_parquet(staging / "reference" / "suspend_intervals.parquet")
    assert intervals.is_empty()

    destination = tmp_path / "snapshot"
    materialize_tushare_all_a_share_history(
        staging_dir=staging,
        dest_dir=destination,
        config=_config(),
    )
    daily = pl.read_parquet(destination / "daily_bars.parquet")
    row = daily.filter(
        (pl.col("symbol") == "000001.SZ") & (pl.col("date") == halted)
    ).to_dicts()
    assert len(row) == 1
    assert row[0]["is_suspended"] is True
    assert row[0]["amount"] == 0.0
    membership = pl.read_parquet(destination / "universe_membership.parquet")
    assert "000001.SZ" not in set(
        membership.filter(pl.col("as_of_date") == halted)["symbol"].to_list()
    )


def test_legacy_suspend_interval_covers_paused_listing_gap(tmp_path: Path) -> None:
    calendar, _ = build_fake_tushare_api_tables()
    pause_start = calendar[5]
    paused_day = calendar[35]
    paused_days = {("000001.SZ", day) for day in calendar[5:36]}
    calendar, tables = build_fake_tushare_api_tables(
        skip_daily=paused_days,
        drop_limit_keys=paused_days,
    )
    tables["suspend"] = pl.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "suspend_date": [pause_start.strftime("%Y%m%d")],
            "resume_date": [calendar[36].strftime("%Y%m%d")],
            "suspend_reason": ["暂停上市"],
        }
    )
    tables["namechange"] = pl.DataFrame(
        {
            "ts_code": ["600000.SH"],
            "name": ["浦发银行"],
            "start_date": ["19991110"],
            "end_date": [None],
            "ann_date": ["19991110"],
            "change_reason": ["上市"],
        }
    )
    staging = tmp_path / "staging"
    collect_tushare_all_a_share_history(
        client=FakeTushareClient(tables),
        config=_config(),
        start=calendar[20],
        end=calendar[-1],
        staging_dir=staging,
    )
    destination = tmp_path / "snapshot"
    materialize_tushare_all_a_share_history(
        staging_dir=staging,
        dest_dir=destination,
        config=_config(),
    )

    daily = pl.read_parquet(destination / "daily_bars.parquet")
    paused = daily.filter(
        (pl.col("symbol") == "000001.SZ") & (pl.col("date") == paused_day)
    ).to_dicts()
    assert len(paused) == 1
    assert paused[0]["is_suspended"] is True
    assert paused[0]["amount"] == 0.0
    assert paused[0]["price_limit_pct"] is None
    membership = pl.read_parquet(destination / "universe_membership.parquet")
    assert "000001.SZ" not in set(
        membership.filter(pl.col("as_of_date") == paused_day)["symbol"].to_list()
    )


def test_collection_request_mismatch_fails_before_reusing_checkpoints(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    staging = tmp_path / "staging"
    collect_tushare_all_a_share_history(
        client=FakeTushareClient(tables),
        config=_config(),
        start=calendar[20],
        end=calendar[-1],
        staging_dir=staging,
    )

    with pytest.raises(TushareFetchError, match="different collection request"):
        collect_tushare_all_a_share_history(
            client=FakeTushareClient(tables),
            config=_config(),
            start=calendar[20] + timedelta(days=1),
            end=calendar[-1],
            staging_dir=staging,
        )
