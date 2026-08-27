from __future__ import annotations

import json
import math
from datetime import date, timedelta
from typing import Any

import polars as pl
import pytest

from app.models.market import Instrument
from app.models.snapshot import DataSnapshot
from app.providers._frames import empty_global, empty_universe_membership, instruments_to_frame
from app.research.index_risk_features import (
    INDEX_RISK_ANNUALIZATION_TRADING_DAYS_PER_YEAR,
    IndexRiskFeatureReport,
    assert_report_self_hash,
    compute_report_id,
    diagnose_index_risk_features,
    report_uses_only_as_of_or_earlier,
    seal_index_risk_feature_report,
    verify_index_risk_feature_report_file,
    write_index_risk_feature_report,
)
from app.storage.memory import InMemoryStore
from tests.helpers import bar, weekdays


def _index_store(calendar: list[date], prices: list[float], *, symbol: str = "IDX") -> InMemoryStore:
    assert len(calendar) == len(prices)
    rows = [
        bar(symbol, day, price, price + 0.05, price - 0.05, price) for day, price in zip(calendar, prices, strict=True)
    ]
    index = pl.DataFrame(rows).with_columns(
        [
            pl.col("date").cast(pl.Date),
            pl.col("is_st").cast(pl.Boolean),
            pl.col("is_suspended").cast(pl.Boolean),
            pl.col("price_limit_pct").cast(pl.Float64),
        ]
    )
    instruments = [
        Instrument(
            symbol=symbol,
            name=symbol,
            sector="index",
            listing_date=date(2010, 1, 1),
            is_index=True,
        )
    ]
    return InMemoryStore(
        instruments=instruments_to_frame(instruments),
        daily=index.clear(),
        index=index,
        global_bars=empty_global(),
        calendar=calendar,
        universe_membership=empty_universe_membership(),
        universe_id="demo",
    )


class _FutureLeakIndexStore:
    def __init__(self, base: InMemoryStore, *, leak_day: date, symbol: str) -> None:
        self._base = base
        self._leak_day = leak_day
        self._symbol = symbol

    def get_instruments(self) -> list[Instrument]:
        return self._base.get_instruments()

    def get_calendar(self, start: date, end: date) -> list[date]:
        return self._base.get_calendar(start, end)

    def get_daily_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        return self._base.get_daily_bars(as_of=as_of, symbol=symbol, start=start)

    def get_index_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        frame = self._base.get_index_bars(as_of=as_of, symbol=symbol, start=start)
        leak = pl.DataFrame(
            [
                {
                    "symbol": self._symbol,
                    "date": self._leak_day,
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.0,
                    "volume": 1_000_000.0,
                    "amount": 10_000_000.0,
                    "turnover_rate": 0.01,
                    "is_st": False,
                    "is_suspended": False,
                    "price_limit_pct": 0.1,
                    "adj_open": 10.0,
                    "adj_high": 10.1,
                    "adj_low": 9.9,
                    "adj_close": 10.0,
                }
            ]
        ).with_columns(pl.col("date").cast(pl.Date))
        return pl.concat([frame, leak], how="diagonal_relaxed")

    def get_global_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        return self._base.get_global_bars(as_of=as_of, symbol=symbol, start=start)

    def get_universe_members(
        self,
        universe_id: str,
        as_of: date,
        available_by: Any,
        *,
        expected_constituents: int | None = None,
        require_available_cross_section: bool = False,
    ) -> set[str]:
        return self._base.get_universe_members(
            universe_id,
            as_of,
            available_by,
            expected_constituents=expected_constituents,
            require_available_cross_section=require_available_cross_section,
        )

    def next_trading_day(self, after: date) -> date | None:
        return self._base.next_trading_day(after)

    def trading_days_after(self, after: date, n: int) -> list[date]:
        return self._base.trading_days_after(after, n)

    def snapshot(self) -> DataSnapshot:
        return self._base.snapshot()


def test_feature_arithmetic_on_tiny_fixture() -> None:
    calendar = weekdays(date(2024, 1, 2), 6)
    # closes: 100,110,121,100,90,99
    prices = [100.0, 110.0, 121.0, 100.0, 90.0, 99.0]
    store = _index_store(calendar, prices)
    as_of = calendar[-1]
    report = diagnose_index_risk_features(
        store,
        index_symbol="IDX",
        as_of=as_of,
        trend_lookback_bars=3,
        volatility_lookback_bars=2,
        drawdown_lookback_bars=4,
    )
    assert report.latest_close == 99.0
    assert report.simple_moving_average == pytest.approx((100.0 + 90.0 + 99.0) / 3.0)
    assert report.close_to_sma_ratio == pytest.approx(99.0 / report.simple_moving_average)
    returns = [90.0 / 100.0 - 1.0, 99.0 / 90.0 - 1.0]
    mean = sum(returns) / 2.0
    sample_std = math.sqrt(sum((value - mean) ** 2 for value in returns) / 1.0)
    assert report.realized_volatility_annualized == pytest.approx(
        sample_std * math.sqrt(INDEX_RISK_ANNUALIZATION_TRADING_DAYS_PER_YEAR)
    )
    assert report.rolling_peak == 121.0
    assert report.drawdown == pytest.approx(99.0 / 121.0 - 1.0)
    assert report.observation_count_trend == 3
    assert report.observation_count_volatility_returns == 2
    assert report.observation_count_drawdown == 4
    assert report.trend_window_dates == calendar[-3:]
    assert report.volatility_price_window_dates == calendar[-3:]
    assert report.drawdown_window_dates == calendar[-4:]


def test_no_lookahead_and_future_row_rejection() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    prices = [100.0 + index for index in range(8)]
    base = _index_store(calendar, prices)
    as_of = calendar[4]
    # Full history exists after as_of in store; diagnosis must only use <= as_of.
    report = diagnose_index_risk_features(
        base,
        index_symbol="IDX",
        as_of=as_of,
        trend_lookback_bars=3,
        volatility_lookback_bars=2,
        drawdown_lookback_bars=3,
    )
    report_uses_only_as_of_or_earlier(report, as_of=as_of)
    assert max(report.trend_window_dates) <= as_of
    assert max(report.volatility_price_window_dates) <= as_of
    assert max(report.drawdown_window_dates) <= as_of
    assert report.latest_close == prices[4]

    leak = _FutureLeakIndexStore(base, leak_day=as_of + timedelta(days=1), symbol="IDX")
    with pytest.raises(ValueError, match="after as_of"):
        diagnose_index_risk_features(
            leak,
            index_symbol="IDX",
            as_of=as_of,
            trend_lookback_bars=3,
            volatility_lookback_bars=2,
            drawdown_lookback_bars=3,
        )


def test_insufficient_missing_duplicate_bad_close_failures() -> None:
    calendar = weekdays(date(2024, 1, 2), 4)
    prices = [100.0, 101.0, 102.0, 103.0]
    store = _index_store(calendar, prices)
    with pytest.raises(ValueError, match="insufficient market calendar"):
        diagnose_index_risk_features(
            store,
            index_symbol="IDX",
            as_of=calendar[-1],
            trend_lookback_bars=5,
            volatility_lookback_bars=2,
            drawdown_lookback_bars=3,
        )

    # Missing required day: declare full calendar but omit the last index bar.
    short_store = _index_store(calendar[:-1], prices[:-1])
    short_store._calendar = calendar  # noqa: SLF001
    short_store._snapshot = None  # noqa: SLF001
    with pytest.raises(ValueError, match="missing required trading days"):
        diagnose_index_risk_features(
            short_store,
            index_symbol="IDX",
            as_of=calendar[-1],
            trend_lookback_bars=3,
            volatility_lookback_bars=2,
            drawdown_lookback_bars=3,
        )

    calendar2 = weekdays(date(2024, 2, 1), 5)
    prices2 = [10.0, 11.0, 12.0, 13.0, 14.0]
    store2 = _index_store(calendar2, prices2)
    rows = store2.index.to_dicts()
    rows.append(dict(rows[-1]))
    store2.index = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    store2._snapshot = None  # noqa: SLF001
    with pytest.raises(ValueError, match="duplicate date"):
        diagnose_index_risk_features(
            store2,
            index_symbol="IDX",
            as_of=calendar2[-1],
            trend_lookback_bars=3,
            volatility_lookback_bars=2,
            drawdown_lookback_bars=3,
        )

    bad = _index_store(calendar2, [10.0, 11.0, 0.0, 13.0, 14.0])
    with pytest.raises(ValueError, match="non-finite or non-positive close"):
        diagnose_index_risk_features(
            bad,
            index_symbol="IDX",
            as_of=calendar2[-1],
            trend_lookback_bars=3,
            volatility_lookback_bars=2,
            drawdown_lookback_bars=3,
        )


def test_deterministic_hash_and_serialization(tmp_path: Any) -> None:
    calendar = weekdays(date(2024, 3, 1), 6)
    prices = [100.0, 101.0, 102.5, 101.5, 103.0, 104.0]
    store = _index_store(calendar, prices)
    first = diagnose_index_risk_features(
        store,
        index_symbol="IDX",
        as_of=calendar[-1],
        trend_lookback_bars=3,
        volatility_lookback_bars=2,
        drawdown_lookback_bars=4,
    )
    second = diagnose_index_risk_features(
        store,
        index_symbol="IDX",
        as_of=calendar[-1],
        trend_lookback_bars=3,
        volatility_lookback_bars=2,
        drawdown_lookback_bars=4,
    )
    assert first.report_id == second.report_id
    assert first.report_id == compute_report_id(first.model_copy(update={"report_id": None}))
    assert_report_self_hash(first)
    path = tmp_path / "report.json"
    write_index_risk_feature_report(first, path)
    loaded = IndexRiskFeatureReport.model_validate_json(path.read_text(encoding="utf-8"))
    reloaded = seal_index_risk_feature_report(loaded.model_copy(update={"report_id": None}))
    assert reloaded.report_id == first.report_id


def test_no_regime_or_risk_budget_output() -> None:
    calendar = weekdays(date(2024, 4, 1), 6)
    prices = [100.0, 101.0, 102.0, 101.0, 100.0, 99.0]
    store = _index_store(calendar, prices)
    report = diagnose_index_risk_features(
        store,
        index_symbol="IDX",
        as_of=calendar[-1],
        trend_lookback_bars=3,
        volatility_lookback_bars=2,
        drawdown_lookback_bars=3,
    )
    payload = report.model_dump(mode="json")
    forbidden = (
        "regime",
        "risk_budget",
        "stock_budget",
        "cash_budget",
        "label",
        "signal",
    )
    for key in payload:
        lowered = key.lower()
        assert all(token not in lowered for token in forbidden)
    assert report.diagnostic_only is True
    assert report.ready_for_scoring is False
    assert report.ready_for_backtest is False
    assert report.ready_for_trading is False
    assert report.auto_apply is False


def test_duplicate_and_unsorted_calendar_rejected() -> None:
    calendar = weekdays(date(2024, 5, 1), 6)
    prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    store = _index_store(calendar, prices)
    store._calendar = list(calendar) + [calendar[-1]]  # noqa: SLF001
    store._snapshot = None  # noqa: SLF001
    with pytest.raises(ValueError, match="strictly increasing"):
        diagnose_index_risk_features(
            store,
            index_symbol="IDX",
            as_of=calendar[-1],
            trend_lookback_bars=3,
            volatility_lookback_bars=2,
            drawdown_lookback_bars=3,
        )

    unsorted = _index_store(calendar, prices)
    unsorted._calendar = list(reversed(calendar))  # noqa: SLF001
    unsorted._snapshot = None  # noqa: SLF001
    with pytest.raises(ValueError, match="strictly increasing"):
        diagnose_index_risk_features(
            unsorted,
            index_symbol="IDX",
            as_of=calendar[-1],
            trend_lookback_bars=3,
            volatility_lookback_bars=2,
            drawdown_lookback_bars=3,
        )


def test_file_verifier_rejects_future_or_duplicate_window_report(tmp_path: Any) -> None:
    calendar = weekdays(date(2024, 1, 2), 6)
    # calendar[0]=2024-01-02 ... includes 2024-01-05 and later weekdays
    prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    store = _index_store(calendar, prices)
    as_of = date(2024, 1, 5)
    assert as_of in calendar
    good = diagnose_index_risk_features(
        store,
        index_symbol="IDX",
        as_of=as_of,
        trend_lookback_bars=3,
        volatility_lookback_bars=2,
        drawdown_lookback_bars=3,
    )
    future_day = date(2024, 1, 8)
    assert future_day > as_of

    # Adversarial: self-hashed report with a post-as_of window date.
    future_payload = good.model_dump(mode="python")
    future_payload["report_id"] = None
    future_payload["trend_window_dates"] = [
        calendar[calendar.index(as_of) - 2],
        calendar[calendar.index(as_of) - 1],
        future_day,
    ]
    future_report = IndexRiskFeatureReport.model_construct(**future_payload)
    future_sealed = seal_index_risk_feature_report(future_report)
    assert future_sealed.report_id == compute_report_id(future_sealed)
    future_path = tmp_path / "future-window.json"
    future_path.write_text(
        json.dumps(future_sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing or invalid|after as_of"):
        verify_index_risk_feature_report_file(future_path)

    # Adversarial: self-hashed report with a duplicate (non-increasing) window.
    dup_payload = good.model_dump(mode="python")
    dup_payload["report_id"] = None
    terminal = good.trend_window_dates[-1]
    prior = good.trend_window_dates[-2]
    dup_payload["trend_window_dates"] = [prior, terminal, terminal]
    dup_report = IndexRiskFeatureReport.model_construct(**dup_payload)
    dup_sealed = seal_index_risk_feature_report(dup_report)
    dup_path = tmp_path / "duplicate-window.json"
    dup_path.write_text(
        json.dumps(dup_sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing or invalid|strictly increasing"):
        verify_index_risk_feature_report_file(dup_path)
