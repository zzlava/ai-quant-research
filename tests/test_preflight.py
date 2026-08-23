from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
import yaml
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.clock import decision_at_utc
from app.demo.generator import generate_demo_market, write_demo_parquet
from app.errors import PreflightError, SnapshotError
from app.features.engine import STOCK_FEATURE_HISTORY_BARS, required_history_bars
from app.models.config import StrategyConfig
from app.models.market import Instrument
from app.pipeline import run_backtest, run_score
from app.preflight import (
    HISTORICAL_MODE_LABEL,
    MANUAL_STATIC_MODE_LABEL,
    SECTOR_DISABLED_LABEL,
    preflight_research,
)
from app.providers._frames import instruments_to_frame
from app.providers.tushare_client import TOKEN_ENV
from app.storage.memory import InMemoryStore
from app.universe.membership import build_manual_static_membership
from tests.helpers import CONFIG_DIR, PROJECT_ROOT, fill_quiet_bars, load_test_config, weekdays

A = "000001.SZ"
B = "600000.SH"
INDEX = "IDX_CSI300"
GLOB = "GLB_SPX"
READY_OFFSET = STOCK_FEATURE_HISTORY_BARS - 1


def _hist_config(expected: int | None = 2) -> StrategyConfig:
    config = load_test_config()
    config.universe.mode = "historical_membership"
    config.universe.id = "csi300"
    config.universe.expected_constituents = expected
    config.weights.sector_score = 0.0
    return config


def _daily(symbols: list[str], calendar: list[date]) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        rows.extend(fill_quiet_bars(symbol, calendar))
    return pl.DataFrame(rows).with_columns(
        [
            pl.col("date").cast(pl.Date),
            pl.col("is_st").cast(pl.Boolean),
            pl.col("is_suspended").cast(pl.Boolean),
            pl.col("price_limit_pct").cast(pl.Float64),
        ]
    )


def _global(calendar: list[date], symbol: str = GLOB) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(calendar),
            "date": calendar,
            "close": [100.0] * len(calendar),
            "available_at": [datetime(day.year, day.month, day.day, 6, 0) for day in calendar],
        }
    ).with_columns([pl.col("date").cast(pl.Date), pl.col("available_at").cast(pl.Datetime("us"))])


def _membership(
    calendar: list[date],
    symbols: list[str],
    *,
    universe_id: str,
    available_at: datetime | None = None,
    late_available: dict[tuple[date, str], datetime] | None = None,
    drop: dict[date, set[str]] | None = None,
) -> pl.DataFrame:
    extras = late_available or {}
    skipped = drop or {}
    rows: list[dict[str, object]] = []
    for day in calendar:
        blocked = skipped.get(day, set())
        for symbol in symbols:
            if symbol in blocked:
                continue
            stamp = extras.get((day, symbol), available_at) or datetime(day.year, day.month, day.day, 6, 0)
            rows.append(
                {
                    "universe_id": universe_id,
                    "as_of_date": day,
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


def _store(
    calendar: list[date],
    *,
    stocks: list[str] | None = None,
    stock_calendar: list[date] | None = None,
    index_calendar: list[date] | None = None,
    global_calendar: list[date] | None = None,
    membership: pl.DataFrame | None = None,
    universe_id: str = "demo",
) -> InMemoryStore:
    symbols = stocks or [A, B]
    daily = _daily(symbols, stock_calendar or calendar)
    index = _daily([INDEX], index_calendar or calendar)
    glob = _global(global_calendar or calendar)
    instruments = [
        Instrument(symbol=code, name=code, sector="tech", listing_date=date(2018, 1, 1)) for code in symbols
    ]
    frame = membership
    if frame is None:
        frame = build_manual_static_membership(symbols, calendar, universe_id=universe_id)
    return InMemoryStore(
        instruments=instruments_to_frame(instruments),
        daily=daily,
        index=index,
        global_bars=glob,
        calendar=calendar,
        universe_membership=frame,
        universe_id=universe_id,
    )


def test_short_calendar_cannot_become_ready() -> None:
    calendar = weekdays(date(2024, 1, 2), 10)
    store = _store(calendar)
    with pytest.raises(PreflightError, match="signal_ready_start|consecutive"):
        preflight_research(store=store, config=load_test_config(), start=calendar[0], end=calendar[-1])


def test_complete_members_but_short_benchmark_history_fails() -> None:
    calendar = weekdays(date(2024, 1, 2), 30)
    store = _store(calendar, index_calendar=calendar[:8])
    with pytest.raises(PreflightError, match="market index"):
        preflight_research(store=store, config=load_test_config(), start=calendar[20], end=calendar[-1])


def test_member_count_shortfall_is_rejected() -> None:
    calendar = weekdays(date(2024, 1, 2), STOCK_FEATURE_HISTORY_BARS + 10)
    store = _store(
        calendar,
        membership=_membership(calendar, [A, B], universe_id="csi300", drop={calendar[-1]: {B}}),
        universe_id="csi300",
    )
    with pytest.raises(PreflightError, match="expected_constituents"):
        preflight_research(
            store=store, config=_hist_config(2), start=calendar[READY_OFFSET], end=calendar[-1]
        )


def test_late_member_fails_historical_preflight() -> None:
    calendar = weekdays(date(2024, 1, 2), STOCK_FEATURE_HISTORY_BARS + 10)
    as_of = calendar[READY_OFFSET + 2]
    config = _hist_config(2)
    late = decision_at_utc(as_of, config.data) + timedelta(hours=2)
    store = _store(
        calendar,
        membership=_membership(
            calendar,
            [A, B],
            universe_id="csi300",
            late_available={(as_of, B): late},
        ),
        universe_id="csi300",
    )
    with pytest.raises(PreflightError, match="partial universe"):
        preflight_research(store=store, config=config, start=calendar[READY_OFFSET], end=calendar[-1])


def test_qualified_window_emits_signal_ready_start() -> None:
    calendar = weekdays(date(2024, 1, 2), STOCK_FEATURE_HISTORY_BARS + 10)
    store = _store(calendar)
    config = load_test_config()
    result = preflight_research(store=store, config=config, start=calendar[READY_OFFSET], end=calendar[-1])
    assert result.signal_ready_start == calendar[READY_OFFSET]
    assert result.required_history_bars == required_history_bars(config.data.min_history_bars)
    assert result.research_mode == MANUAL_STATIC_MODE_LABEL
    assert result.sector_status is None
    hist = _store(
        calendar,
        membership=_membership(calendar, [A, B], universe_id="csi300"),
        universe_id="csi300",
    )
    hist_result = preflight_research(
        store=hist, config=_hist_config(2), start=calendar[READY_OFFSET], end=calendar[-1]
    )
    assert hist_result.research_mode == HISTORICAL_MODE_LABEL
    assert hist_result.sector_status == SECTOR_DISABLED_LABEL
    assert hist_result.signal_ready_start == calendar[READY_OFFSET]


def test_bar_59_rejected_bar_60_produces_features_not_empty_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    calendar = weekdays(date(2024, 1, 2), STOCK_FEATURE_HISTORY_BARS + 10)
    store = _store(calendar)
    bar_59 = calendar[READY_OFFSET - 1]
    bar_60 = calendar[READY_OFFSET]
    assert calendar.index(bar_59) + 1 == 59
    assert calendar.index(bar_60) + 1 == 60
    with pytest.raises(PreflightError, match="signal_ready_start"):
        preflight_research(store=store, config=load_test_config(), start=bar_59, end=calendar[-1])
    with pytest.raises(PreflightError, match="signal_ready_start"):
        run_score(bar_59, "baseline_v1", store=store)
    with pytest.raises(PreflightError, match="signal_ready_start"):
        run_backtest("baseline_v1", bar_59, calendar[-1], store=store)
    ready = preflight_research(store=store, config=load_test_config(), start=bar_60, end=calendar[-1])
    assert ready.signal_ready_start == bar_60
    scores = run_score(bar_60, "baseline_v1", store=store)
    assert scores
    assert all(item.symbol in {A, B} for item in scores)
    result = run_backtest("baseline_v1", bar_60, calendar[-1], store=store)
    assert result.equity_curve
    assert result.window.valuation_end == calendar[-1]


def test_global_holiday_gap_on_cn_calendar_is_not_a_preflight_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    calendar = weekdays(date(2024, 1, 2), STOCK_FEATURE_HISTORY_BARS + 10)
    holiday = calendar[45]
    assert holiday < calendar[READY_OFFSET]
    store = _store(calendar, global_calendar=[day for day in calendar if day != holiday])
    ready = preflight_research(
        store=store, config=load_test_config(), start=calendar[READY_OFFSET], end=calendar[-1]
    )
    assert ready.signal_ready_start == calendar[READY_OFFSET]
    scores = run_score(calendar[READY_OFFSET], "baseline_v1", store=store)
    assert scores


def test_global_history_shorter_than_min_history_bars_fails() -> None:
    calendar = weekdays(date(2024, 1, 2), STOCK_FEATURE_HISTORY_BARS + 10)
    store = _store(calendar, global_calendar=calendar[:10])
    with pytest.raises(PreflightError, match="global series"):
        preflight_research(
            store=store, config=load_test_config(), start=calendar[READY_OFFSET], end=calendar[-1]
        )


def test_cli_success_and_manual_static_label_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> str:
        raise AssertionError("token must not be read")

    monkeypatch.setattr("app.providers.tushare_client.read_tushare_token", boom)
    monkeypatch.setenv(TOKEN_ENV, "should-not-be-read")
    data_dir = tmp_path / "data"
    monkeypatch.setenv("AIQ_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    bundle = generate_demo_market(seed=42, n_stocks=8, start=date(2023, 1, 3), end=date(2024, 3, 29))
    write_demo_parquet(bundle, data_dir / "parquet")
    ready = date(2024, 1, 15)
    runner = CliRunner()
    early = runner.invoke(
        cli_app,
        [
            "preflight-research",
            "--strategy",
            "baseline_v1",
            "--start",
            bundle.calendar[0].isoformat(),
            "--end",
            bundle.calendar[5].isoformat(),
        ],
    )
    assert early.exit_code != 0
    assert "signal_ready_start" in ((early.stdout or "") + (early.stderr or ""))
    ok = runner.invoke(
        cli_app,
        [
            "preflight-research",
            "--strategy",
            "baseline_v1",
            "--start",
            ready.isoformat(),
            "--end",
            "2024-01-31",
        ],
    )
    assert ok.exit_code == 0, ok.stdout + (ok.stderr or "")
    out = ok.stdout or ""
    assert f"research_mode={MANUAL_STATIC_MODE_LABEL}" in out
    assert "signal_ready_start=" in out
    assert "预检只读，不能证明策略收益有效" in out
    assert "should-not-be-read" not in (out + (ok.stderr or ""))


def test_cli_missing_snapshot_does_not_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> str:
        raise AssertionError("token must not be read")

    monkeypatch.setattr("app.providers.tushare_client.read_tushare_token", boom)
    monkeypatch.setenv("AIQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    result = CliRunner().invoke(
        cli_app,
        ["preflight-research", "--strategy", "baseline_v1", "--start", "2024-01-15", "--end", "2024-01-31"],
    )
    assert result.exit_code != 0
    combined = ((result.stdout or "") + (result.stderr or "")).lower()
    assert "manifest" in combined or "snapshot" in combined
    assert "should-not-be-read" not in combined
    with pytest.raises(SnapshotError, match="manifest"):
        run_score(date(2024, 1, 15), "baseline_v1")


def test_historical_cli_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> str:
        raise AssertionError("token must not be read")

    monkeypatch.setattr("app.providers.tushare_client.read_tushare_token", boom)
    calendar = weekdays(date(2024, 1, 2), STOCK_FEATURE_HISTORY_BARS + 10)
    store = _store(
        calendar,
        membership=_membership(calendar, [A, B], universe_id="csi300"),
        universe_id="csi300",
    )
    with (CONFIG_DIR / "baseline_v1.yaml").open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    payload["universe"]["mode"] = "historical_membership"
    payload["universe"]["id"] = "csi300"
    payload["universe"]["expected_constituents"] = 2
    payload["weights"]["sector_score"] = 0.0
    dest = tmp_path / "config" / "strategies"
    dest.mkdir(parents=True)
    (dest / "hist_two.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(tmp_path / "config"))
    result = preflight_research(
        store=store, config=_hist_config(2), start=calendar[READY_OFFSET], end=calendar[-1]
    )
    assert result.research_mode == HISTORICAL_MODE_LABEL
    assert result.sector_status == SECTOR_DISABLED_LABEL
    assert dest.exists()
