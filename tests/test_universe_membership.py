from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from app.backtest.engine import BacktestEngine
from app.cli import app as cli_app
from app.clock import decision_at_utc
from app.demo.generator import generate_demo_market
from app.errors import DataQualityError, SnapshotError, TushareFetchError
from app.models.market import Instrument
from app.providers._frames import bars_to_frame, global_to_frame, instruments_to_frame
from app.providers.demo_provider import DemoProvider
from app.providers.tushare_client import TOKEN_ENV
from app.providers.tushare_fetch import fetch_tushare_and_import
from app.scoring.engine import ScoringEngine
from app.storage.duckdb_store import DuckDBParquetStore
from app.storage.hashing import build_snapshot
from app.storage.import_market import import_market_data
from app.storage.memory import InMemoryStore
from app.storage.quality import validate_universe_membership
from app.storage.snapshot_io import load_verified_snapshot
from app.strategies.loader import load_strategy_config
from app.universe.membership import (
    build_manual_static_membership,
    read_universe_membership_file,
    resolve_fetch_universe,
)
from tests.helpers import (
    PROJECT_ROOT,
    constant_signal,
    fill_quiet_bars,
    load_test_config,
    store_from_rows,
    weekdays,
    zero_cost_config,
)
from tests.tushare_fakes import STOCKS, FakeTushareClient, build_fake_tushare_api_tables

A = "000001.SZ"
B = "600000.SH"


def _membership_rows(
    calendar: list[date],
    members_by_day: dict[date, list[str]],
    *,
    universe_id: str = "demo",
    available_at: datetime | None = None,
    late_available: dict[tuple[date, str], datetime] | None = None,
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    extras = late_available or {}
    for as_of in calendar:
        for symbol in members_by_day[as_of]:
            stamp = extras.get((as_of, symbol), available_at) or datetime(as_of.year, as_of.month, as_of.day, 7, 0)
            rows.append(
                {
                    "universe_id": universe_id,
                    "as_of_date": as_of,
                    "symbol": symbol,
                    "available_at": stamp,
                    "weight": None,
                }
            )
    return pl.DataFrame(rows).with_columns(
        [
            pl.col("as_of_date").cast(pl.Date),
            pl.col("available_at").cast(pl.Datetime("us")),
            pl.col("weight").cast(pl.Float64),
        ]
    )


def _write_membership_csv(path: Path, rows: list[str]) -> Path:
    path.write_text(
        "universe_id,as_of_date,symbol,available_at,weight\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return path


def test_csi300_yaml_is_historical_membership() -> None:
    config = load_strategy_config("baseline_csi300_pit_v1", PROJECT_ROOT / "config" / "strategies")
    assert config.universe.mode == "historical_membership"
    assert config.universe.id == "csi300"
    assert config.universe.expected_constituents == 300
    real = load_strategy_config("baseline_real_cn_v1", PROJECT_ROOT / "config" / "strategies")
    assert real.universe.mode == "manual_static"
    assert real.universe.id == "manual_real_cn"


def test_membership_change_limits_new_scores_and_entries() -> None:
    bundle = generate_demo_market(
        seed=42,
        n_stocks=12,
        start=date(2023, 1, 3),
        end=date(2024, 3, 29),
    )
    store = InMemoryStore.from_provider(DemoProvider(bundle=bundle))
    config = load_test_config()
    config.universe.min_avg_turnover_20d = 0
    config.universe.min_listing_days = 1
    config.universe.exclude_st = False
    calendar = store.get_calendar(date(2023, 1, 3), date(2024, 3, 29))
    scored = ScoringEngine(store, config).run(date(2024, 1, 15))
    assert len(scored) >= 2
    first, second = scored[0].symbol, scored[1].symbol
    switch = date(2024, 2, 1)
    store.universe_membership = _membership_rows(
        calendar,
        {day: [first] if day < switch else [second] for day in calendar},
    )
    store._snapshot = None

    early = ScoringEngine(store, config).run(date(2024, 1, 15))
    late = ScoringEngine(store, config).run(date(2024, 2, 15))
    assert {row.symbol for row in early} == {first}
    assert {row.symbol for row in late} == {second}

    def signals(as_of: date) -> list:
        return constant_signal([first, second], 80.0, as_of)

    result = BacktestEngine(store, zero_cost_config(), signal_fn=signals).run(date(2024, 1, 15), date(2024, 2, 20))
    first_entries = [trade.entry_date for trade in result.trades if trade.symbol == first]
    second_entries = [trade.entry_date for trade in result.trades if trade.symbol == second]
    assert first_entries
    assert second_entries
    last_first_entry = store.next_trading_day(date(2024, 1, 31))
    assert last_first_entry is not None
    assert max(first_entries) <= last_first_entry
    assert min(second_entries) >= switch


def test_future_membership_and_late_available_at_are_ignored() -> None:
    bundle = generate_demo_market(
        seed=42,
        n_stocks=12,
        start=date(2023, 1, 3),
        end=date(2024, 3, 29),
    )
    store = InMemoryStore.from_provider(DemoProvider(bundle=bundle))
    config = load_test_config()
    config.universe.min_avg_turnover_20d = 0
    config.universe.min_listing_days = 1
    config.universe.exclude_st = False
    calendar = store.get_calendar(date(2023, 1, 3), date(2024, 3, 29))
    as_of = date(2024, 1, 15)
    scored = ScoringEngine(store, config).run(as_of)
    first, second = scored[0].symbol, scored[1].symbol
    late = decision_at_utc(as_of, config.data) + timedelta(hours=3)
    store.universe_membership = _membership_rows(
        calendar,
        {day: [first, second] for day in calendar},
        late_available={(as_of, second): late},
    )
    store._snapshot = None
    now_scores = ScoringEngine(store, config).run(as_of)
    assert {row.symbol for row in now_scores} == {first}

    future_only = {day: [first] if day <= as_of else [first, second] for day in calendar}
    store.universe_membership = _membership_rows(calendar, future_only)
    store._snapshot = None
    again = ScoringEngine(store, config).run(as_of)
    assert second not in {row.symbol for row in again}

    def signals(day: date):
        return constant_signal([first, second], 80.0, day)

    result = BacktestEngine(store, zero_cost_config(), signal_fn=signals).run(as_of, date(2024, 1, 22))
    assert all(trade.symbol != second or trade.entry_date > as_of for trade in result.trades)
    assert second not in {trade.symbol for trade in result.trades if trade.entry_date == store.next_trading_day(as_of)}


def test_exclusion_does_not_force_exit() -> None:
    calendar = weekdays(date(2024, 1, 2), 18)
    signal_day = calendar[0]
    drop_day = calendar[4]
    members = {day: [A] if day < drop_day else [B] for day in calendar}
    rows = fill_quiet_bars(A, calendar) + fill_quiet_bars(B, calendar)
    store = store_from_rows(calendar, rows, membership=_membership_rows(calendar, members))
    config = zero_cost_config()

    def signals(as_of: date):
        return constant_signal([A, B], 80.0, as_of)

    result = BacktestEngine(store, config, signal_fn=signals).run(signal_day, calendar[12])
    a_exits = [trade for trade in result.trades if trade.symbol == A]
    assert a_exits
    assert all(trade.exit_date != drop_day for trade in a_exits)
    assert all(trade.exit_reason in {"take_profit", "stop_loss", "timeout"} for trade in a_exits)
    assert all(trade.exit_reason == "timeout" for trade in a_exits)


def test_membership_file_integrity_rejections(tmp_path: Path) -> None:
    calendar = weekdays(date(2024, 1, 2), 3)
    good = [
        f"csi300,{calendar[0].isoformat()},{A},2024-01-01T16:00:00Z,0.5",
        f"csi300,{calendar[0].isoformat()},{B},2024-01-01T16:00:00Z,0.5",
        f"csi300,{calendar[1].isoformat()},{A},2024-01-02T16:00:00Z,0.5",
        f"csi300,{calendar[1].isoformat()},{B},2024-01-02T16:00:00Z,0.5",
        f"csi300,{calendar[2].isoformat()},{A},2024-01-03T16:00:00Z,0.5",
        f"csi300,{calendar[2].isoformat()},{B},2024-01-03T16:00:00Z,0.5",
    ]
    path = _write_membership_csv(tmp_path / "good.csv", good)
    frame = read_universe_membership_file(path)
    assert frame.height == 6

    missing_day = good[:-2]
    with pytest.raises(DataQualityError, match="missing complete cross-section"):
        import_market_data(
            _source_with_membership(tmp_path / "miss", calendar, missing_day),
            tmp_path / "out-miss",
            source_name="local",
            adjustment="forward",
        )

    dup = good + [good[0]]
    with pytest.raises(DataQualityError, match="duplicate primary key"):
        read_universe_membership_file(_write_membership_csv(tmp_path / "dup.csv", dup))

    wrong_id = [row.replace("csi300", "other", 1) for row in good]
    instruments = pl.DataFrame(
        [{"symbol": A, "is_index": False, "is_global": False}, {"symbol": B, "is_index": False, "is_global": False}]
    )
    with pytest.raises(DataQualityError, match="does not match"):
        validate_universe_membership(
            read_universe_membership_file(_write_membership_csv(tmp_path / "wrong-id.csv", wrong_id)),
            calendar,
            instruments,
            universe_id="csi300",
        )

    with pytest.raises(DataQualityError, match="suffixes are not inferred"):
        read_universe_membership_file(
            _write_membership_csv(
                tmp_path / "bad-sym.csv",
                [f"csi300,{calendar[0].isoformat()},000001,2024-01-01T16:00:00Z,"],
            )
        )

    short = [
        f"csi300,{day.isoformat()},{A},2024-01-01T16:00:00Z,0.5"
        for day in calendar
    ]
    with pytest.raises(DataQualityError, match="expected_constituents"):
        instruments = pl.DataFrame(
            [{"symbol": A, "is_index": False, "is_global": False}, {"symbol": B, "is_index": False, "is_global": False}]
        )
        validate_universe_membership(
            read_universe_membership_file(_write_membership_csv(tmp_path / "short.csv", short)),
            calendar,
            instruments,
            universe_id="csi300",
            expected_constituents=2,
        )

    unknown = [row.replace(B, "000002.SZ") for row in good]
    with pytest.raises(DataQualityError, match="not in instruments"):
        import_market_data(
            _source_with_membership(tmp_path / "unknown", calendar, unknown),
            tmp_path / "out-unknown",
            source_name="local",
            adjustment="forward",
        )

    outside = good + [
        f"csi300,2024-02-01,{A},2024-01-31T16:00:00Z,0.5",
        f"csi300,2024-02-01,{B},2024-01-31T16:00:00Z,0.5",
    ]
    with pytest.raises(DataQualityError, match="outside snapshot coverage"):
        import_market_data(
            _source_with_membership(tmp_path / "outside", calendar, outside),
            tmp_path / "out-out",
            source_name="local",
            adjustment="forward",
        )


def _source_with_membership(
    dest: Path,
    calendar: list[date],
    membership_rows: list[str],
) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    rows = fill_quiet_bars(A, calendar) + fill_quiet_bars(B, calendar)
    daily = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    instruments = pl.DataFrame(
        [
            {
                "symbol": A,
                "name": "A",
                "sector": "bank",
                "listing_date": date(2018, 1, 1),
                "is_index": False,
                "is_global": False,
                "market": "CN",
                "timezone": "Asia/Shanghai",
                "session_close": "15:00",
            },
            {
                "symbol": B,
                "name": "B",
                "sector": "bank",
                "listing_date": date(2018, 1, 1),
                "is_index": False,
                "is_global": False,
                "market": "CN",
                "timezone": "Asia/Shanghai",
                "session_close": "15:00",
            },
        ]
    )
    index = daily.filter(pl.col("symbol") == A).with_columns(pl.lit("000300.SH").alias("symbol"))
    glob = pl.DataFrame(
        {
            "symbol": ["SPX"] * len(calendar),
            "date": calendar,
            "close": [100.0] * len(calendar),
            "ret_1d": [0.0] * len(calendar),
            "market": ["US"] * len(calendar),
            "timezone": ["America/New_York"] * len(calendar),
            "available_at": [datetime(2024, 1, 1, 21, 0)] * len(calendar),
        }
    ).with_columns([pl.col("date").cast(pl.Date), pl.col("available_at").cast(pl.Datetime("us"))])
    daily.write_csv(dest / "daily_bars.csv")
    index.write_csv(dest / "index_bars.csv")
    glob.write_csv(dest / "global_bars.csv")
    instruments.write_csv(dest / "instruments.csv")
    pl.DataFrame({"date": calendar}).write_csv(dest / "calendar.csv")
    _write_membership_csv(dest / "universe_membership.csv", membership_rows)
    return dest


def test_legacy_five_table_snapshot_is_rejected(tmp_path: Path) -> None:
    bundle = generate_demo_market(seed=42, n_stocks=8, start=date(2023, 1, 3), end=date(2024, 3, 29))
    dest = tmp_path / "parquet"
    dest.mkdir()
    tables = {
        "daily_bars": bars_to_frame(bundle.daily_bars),
        "index_bars": bars_to_frame(bundle.index_bars),
        "global_bars": global_to_frame(bundle.global_bars),
        "instruments": instruments_to_frame(bundle.instruments),
        "calendar": pl.DataFrame({"date": bundle.calendar}).with_columns(pl.col("date").cast(pl.Date)),
    }
    for name, frame in tables.items():
        frame.write_parquet(dest / f"{name}.parquet")
    snapshot = build_snapshot(
        {**tables, "universe_membership": build_manual_static_membership(
            [i.symbol for i in bundle.instruments if not i.is_index and not i.is_global],
            bundle.calendar,
            universe_id="demo",
        )},
        adjustment="forward",
        source_name="demo",
    )
    (dest / "manifest.json").write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    with pytest.raises(SnapshotError, match="universe_membership"):
        load_verified_snapshot(dest)


def test_membership_weight_change_changes_snapshot_id() -> None:
    calendar = weekdays(date(2024, 1, 2), 3)
    base_members = _membership_rows(calendar, {day: [A] for day in calendar})
    changed = base_members.with_columns(pl.lit(0.42).alias("weight"))
    daily = pl.DataFrame(fill_quiet_bars(A, calendar)).with_columns(pl.col("date").cast(pl.Date))
    instruments = instruments_to_frame(
        [Instrument(symbol=A, name="A", sector="bank", listing_date=date(2018, 1, 1))]
    )
    empty_index = daily.clear()
    glob = pl.DataFrame(
        {
            "symbol": ["SPX"] * 3,
            "date": calendar,
            "close": [1.0, 1.0, 1.0],
            "ret_1d": [0.0, 0.0, 0.0],
            "market": ["US"] * 3,
            "timezone": ["UTC"] * 3,
            "available_at": [datetime(2024, 1, 1, 21, 0)] * 3,
        }
    ).with_columns([pl.col("date").cast(pl.Date), pl.col("available_at").cast(pl.Datetime("us"))])
    common = {
        "daily_bars": daily,
        "index_bars": empty_index,
        "global_bars": glob,
        "instruments": instruments,
        "calendar": pl.DataFrame({"date": calendar}).with_columns(pl.col("date").cast(pl.Date)),
    }
    first = build_snapshot({**common, "universe_membership": base_members}, adjustment="forward", source_name="t")
    second = build_snapshot({**common, "universe_membership": changed}, adjustment="forward", source_name="t")
    assert first.snapshot_id != second.snapshot_id


def test_symbols_and_membership_files_are_exclusive(tmp_path: Path) -> None:
    config = load_test_config()
    symbols = tmp_path / "symbols.txt"
    symbols.write_text(f"{A}\n", encoding="utf-8")
    members = _write_membership_csv(
        tmp_path / "members.csv",
        [f"demo,2024-01-02,{A},2024-01-01T16:00:00Z,"],
    )
    with pytest.raises(TushareFetchError, match="mutually exclusive"):
        resolve_fetch_universe(config, symbols_file=symbols, membership_file=members)


def test_historical_membership_fails_before_live_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str] = []

    def boom(token: str) -> None:
        created.append(token)
        raise AssertionError("live client must not be created")

    def no_token() -> str:
        raise AssertionError("token must not be read")

    monkeypatch.setattr("app.providers.tushare_client.LiveTushareClient", boom)
    monkeypatch.setattr("app.providers.tushare_client.read_tushare_token", no_token)
    monkeypatch.setenv(TOKEN_ENV, "should-not-be-read")
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
            "baseline_csi300_pit_v1",
        ],
    )
    assert result.exit_code != 0
    combined = ((result.stdout or "") + (result.stderr or "")).lower()
    assert "historical_membership" in combined
    assert "should-not-be-read" not in combined
    assert created == []


def test_cli_still_rejects_index_universe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "unused-token")
    monkeypatch.setenv("AIQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    (tmp_path / "symbols.txt").write_text(f"{A}\n", encoding="utf-8")
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


def test_historical_membership_uses_file_not_union_every_day(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    config = load_strategy_config("baseline_real_cn_v1", PROJECT_ROOT / "config" / "strategies")
    config.universe.mode = "historical_membership"
    config.universe.id = "csi300"
    config.universe.expected_constituents = 1
    switch = calendar[40]
    rows = []
    for day in calendar:
        symbol = A if day < switch else B
        rows.append(f"csi300,{day.isoformat()},{symbol},2023-10-01T16:00:00Z,")
    membership = read_universe_membership_file(_write_membership_csv(tmp_path / "hist.csv", rows))
    snapshot = fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=config,
        dest_dir=tmp_path / "parquet",
        stocks=list(STOCKS),
        membership=membership,
        client=FakeTushareClient(tables),
    )
    stored = pl.read_parquet(tmp_path / "parquet" / "universe_membership.parquet")
    early = stored.filter(pl.col("as_of_date") == calendar[0])["symbol"].to_list()
    late = stored.filter(pl.col("as_of_date") == calendar[-1])["symbol"].to_list()
    assert early == [A]
    assert late == [B]
    assert snapshot.row_counts["universe_membership"] == len(calendar)


def test_imported_one_constituent_csi300_fails_csi300_score(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    fetch_tushare_and_import(
        start=calendar[0],
        end=calendar[-1],
        config=load_strategy_config("baseline_real_cn_v1", PROJECT_ROOT / "config" / "strategies"),
        dest_dir=tmp_path / "seed",
        stocks=list(STOCKS),
        client=FakeTushareClient(tables),
    )
    src = tmp_path / "src"
    src.mkdir()
    for name in ("daily_bars", "index_bars", "global_bars", "instruments", "calendar"):
        pl.read_parquet(tmp_path / "seed" / f"{name}.parquet").write_csv(src / f"{name}.csv")
    _write_membership_csv(
        src / "universe_membership.csv",
        [f"csi300,{day.isoformat()},{A},2023-10-01T16:00:00Z," for day in calendar],
    )
    dest = tmp_path / "pit"
    import_market_data(
        src,
        dest,
        source_name="local",
        adjustment="forward",
        market_index="000300.SH",
        global_symbol="SPX",
    )
    store = DuckDBParquetStore(dest, snapshot=load_verified_snapshot(dest))
    config = load_strategy_config("baseline_csi300_pit_v1", PROJECT_ROOT / "config" / "strategies")
    with pytest.raises(DataQualityError, match="expected_constituents=300"):
        ScoringEngine(store, config).run(calendar[40])


def test_historical_mode_rejects_partial_available_cross_section() -> None:
    bundle = generate_demo_market(
        seed=42,
        n_stocks=12,
        start=date(2023, 1, 3),
        end=date(2024, 3, 29),
    )
    store = InMemoryStore.from_provider(DemoProvider(bundle=bundle))
    config = load_test_config()
    config.universe.min_avg_turnover_20d = 0
    config.universe.min_listing_days = 1
    config.universe.exclude_st = False
    calendar = store.get_calendar(date(2023, 1, 3), date(2024, 3, 29))
    as_of = date(2024, 1, 15)
    scored = ScoringEngine(store, config).run(as_of)
    config.universe.mode = "historical_membership"
    config.universe.id = "demo"
    config.universe.expected_constituents = 2
    first, second = scored[0].symbol, scored[1].symbol
    late = decision_at_utc(as_of, config.data) + timedelta(hours=3)
    store.universe_membership = _membership_rows(
        calendar,
        {day: [first, second] for day in calendar},
        late_available={(as_of, second): late},
    )
    store._snapshot = None
    with pytest.raises(DataQualityError, match="partial universe"):
        ScoringEngine(store, config).run(as_of)


def test_generic_import_rejects_membership_symbol_without_suffix(tmp_path: Path) -> None:
    calendar = weekdays(date(2024, 1, 2), 3)
    good = [
        f"csi300,{day.isoformat()},{A},2024-01-01T16:00:00Z,0.5"
        for day in calendar
    ] + [
        f"csi300,{day.isoformat()},{B},2024-01-01T16:00:00Z,0.5"
        for day in calendar
    ]
    src = _source_with_membership(tmp_path / "src", calendar, good)
    _write_membership_csv(
        src / "universe_membership.csv",
        [row.replace(A, "000001") for row in good],
    )
    with pytest.raises(DataQualityError, match="suffixes are not inferred"):
        import_market_data(src, tmp_path / "out", source_name="local", adjustment="forward")


def test_membership_outside_window_fails_before_any_api(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    config = load_strategy_config("baseline_real_cn_v1", PROJECT_ROOT / "config" / "strategies")
    config.universe.mode = "historical_membership"
    config.universe.id = "csi300"
    config.universe.expected_constituents = 1
    outside = calendar[-1] + timedelta(days=3)
    rows = [f"csi300,{day.isoformat()},{A},2023-10-01T16:00:00Z," for day in calendar]
    rows.append(f"csi300,{outside.isoformat()},{A},2023-10-01T16:00:00Z,")
    membership = read_universe_membership_file(_write_membership_csv(tmp_path / "out.csv", rows))
    client = FakeTushareClient(tables)
    with pytest.raises(DataQualityError, match="outside the requested window"):
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=config,
            dest_dir=tmp_path / "parquet",
            stocks=list(STOCKS),
            membership=membership,
            client=client,
        )
    assert client.calls == []


def test_membership_coverage_and_count_fail_after_calendar_before_bars(tmp_path: Path) -> None:
    calendar, tables = build_fake_tushare_api_tables()
    config = load_strategy_config("baseline_real_cn_v1", PROJECT_ROOT / "config" / "strategies")
    config.universe.mode = "historical_membership"
    config.universe.id = "csi300"
    config.universe.expected_constituents = 1
    missing = [f"csi300,{day.isoformat()},{A},2023-10-01T16:00:00Z," for day in calendar[:-1]]
    client = FakeTushareClient(tables)
    with pytest.raises(DataQualityError, match="missing complete cross-section"):
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=config,
            dest_dir=tmp_path / "parquet",
            stocks=list(STOCKS),
            membership=read_universe_membership_file(_write_membership_csv(tmp_path / "miss.csv", missing)),
            client=client,
        )
    assert client.calls == ["trade_cal"]
    assert "daily" not in client.calls

    config.universe.expected_constituents = 300
    full = [f"csi300,{day.isoformat()},{A},2023-10-01T16:00:00Z," for day in calendar]
    client = FakeTushareClient(tables)
    with pytest.raises(DataQualityError, match="expected_constituents=300"):
        fetch_tushare_and_import(
            start=calendar[0],
            end=calendar[-1],
            config=config,
            dest_dir=tmp_path / "parquet2",
            stocks=list(STOCKS),
            membership=read_universe_membership_file(_write_membership_csv(tmp_path / "count.csv", full)),
            client=client,
        )
    assert client.calls == ["trade_cal"]
    assert "daily" not in client.calls
