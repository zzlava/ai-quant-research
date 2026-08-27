from __future__ import annotations

from datetime import date
from typing import Any

import polars as pl
import pytest

from app.models.market import Instrument
from app.models.snapshot import DataSnapshot
from app.research.statistical_risk_clusters import (
    RISK_PROXY_NOTE,
    assert_report_self_hash,
    candidates_hash,
    diagnose_statistical_risk_clusters,
    report_uses_only_as_of_or_earlier,
)
from app.storage.memory import InMemoryStore
from tests.helpers import bar, store_from_rows, weekdays


def _prices_from_returns(returns: list[float], start: float = 100.0) -> list[float]:
    prices = [start]
    for value in returns:
        prices.append(prices[-1] * (1.0 + value))
    return prices


def _rows_for_symbol(symbol: str, calendar: list[date], prices: list[float]) -> list[dict[str, object]]:
    assert len(calendar) == len(prices)
    rows: list[dict[str, object]] = []
    for day, price in zip(calendar, prices, strict=True):
        rows.append(bar(symbol, day, price, price + 0.05, price - 0.05, price))
    return rows


def _store(calendar: list[date], series: dict[str, list[float]]) -> InMemoryStore:
    rows: list[dict[str, object]] = []
    for symbol, prices in series.items():
        rows.extend(_rows_for_symbol(symbol, calendar, prices))
    return store_from_rows(calendar, rows)


class _FutureLeakStore:
    """MarketStore double that ignores as_of and returns a post-as_of bar."""

    def __init__(self, base: InMemoryStore, *, leak_symbol: str, leak_day: date) -> None:
        self._base = base
        self._leak_symbol = leak_symbol
        self._leak_day = leak_day

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
        frame = self._base.get_daily_bars(as_of=as_of, symbol=symbol, start=start)
        if symbol not in (None, self._leak_symbol):
            return frame
        leak = pl.DataFrame(
            [
                {
                    "symbol": self._leak_symbol,
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

    def get_index_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        return self._base.get_index_bars(as_of=as_of, symbol=symbol, start=start)

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


def test_no_future_dates_in_complete_report() -> None:
    calendar = weekdays(date(2024, 1, 2), 12)
    as_of = calendar[9]
    lookback = 5
    shared = [0.01, -0.02, 0.015, 0.0, 0.02, 0.01, -0.01, 0.005, 0.0, 0.01, -0.005]
    other = [-0.01, 0.03, -0.01, 0.02, -0.015, 0.0, 0.02, -0.01, 0.015, 0.0, 0.01]
    store = _store(
        calendar,
        {
            "AAA": _prices_from_returns(shared),
            "BBB": _prices_from_returns(shared),
            "CCC": _prices_from_returns(other),
            "DDD": _prices_from_returns(other),
        },
    )
    # Provide full calendar prices; diagnosis must still only use <= as_of.
    report = diagnose_statistical_risk_clusters(
        store,
        as_of,
        ["DDD", "AAA", "CCC", "BBB"],
        lookback,
        0.9,
    )
    report_uses_only_as_of_or_earlier(report, as_of=as_of)
    assert all(day <= as_of for day in report.required_trading_days)
    as_of_index = calendar.index(as_of)
    assert report.required_trading_days == calendar[as_of_index - lookback : as_of_index + 1]
    assert report.ready_for_portfolio_constraints is True
    assert max(report.required_trading_days) == as_of


def test_future_date_in_bars_is_rejected() -> None:
    calendar = weekdays(date(2024, 1, 2), 10)
    as_of = calendar[7]
    prices = _prices_from_returns([0.01] * 9)
    base = _store(calendar, {"AAA": prices, "BBB": prices})
    leak_day = calendar[9]
    assert leak_day > as_of
    store = _FutureLeakStore(base, leak_symbol="AAA", leak_day=leak_day)
    with pytest.raises(ValueError, match="after as_of"):
        diagnose_statistical_risk_clusters(store, as_of, ["AAA", "BBB"], 5, 0.8)


def test_two_complete_clusters_and_stable_candidate_order() -> None:
    calendar = weekdays(date(2024, 1, 2), 10)
    as_of = calendar[-1]
    lookback = 6
    group_a = [0.01, 0.02, -0.01, 0.015, 0.0, 0.02]
    group_b = [-0.02, 0.01, 0.03, -0.015, 0.01, -0.01]
    store = _store(
        calendar[: lookback + 1],
        {
            "AAA": _prices_from_returns(group_a),
            "BBB": _prices_from_returns(group_a),
            "CCC": _prices_from_returns(group_b),
            "DDD": _prices_from_returns(group_b),
        },
    )
    left = diagnose_statistical_risk_clusters(
        store,
        as_of,
        ["DDD", "AAA", "CCC", "BBB"],
        lookback,
        0.95,
    )
    right = diagnose_statistical_risk_clusters(
        store,
        as_of,
        ["BBB", "CCC", "AAA", "DDD"],
        lookback,
        0.95,
    )
    assert left.model_dump(mode="json") == right.model_dump(mode="json")
    assert left.candidates == ["AAA", "BBB", "CCC", "DDD"]
    assert left.candidates_hash == candidates_hash(["AAA", "BBB", "CCC", "DDD"])
    assert left.ready_for_portfolio_constraints is True
    assert left.diagnostic_only is True
    assert left.ready_for_scoring is False
    assert left.ready_for_trading is False
    assert left.auto_apply is False
    assert left.is_not_industry_classification is True
    assert left.risk_proxy_note == RISK_PROXY_NOTE
    assert [cluster.cluster_id for cluster in left.clusters] == ["cluster_001", "cluster_002"]
    assert [cluster.symbols for cluster in left.clusters] == [["AAA", "BBB"], ["CCC", "DDD"]]
    assert_report_self_hash(left)


def test_chain_connected_component_is_single_cluster() -> None:
    calendar = weekdays(date(2024, 1, 2), 9)
    as_of = calendar[-1]
    lookback = 8
    shared1 = [0.01 * value for value in [1, 2, 3, 4, 5, 6, 7, 8]]
    shared2 = [0.01 * value for value in [8, 1, 7, 2, 6, 3, 5, 4]]
    blended = [0.5 * left + 0.5 * right for left, right in zip(shared1, shared2, strict=True)]
    store = _store(
        calendar,
        {
            "AAA": _prices_from_returns(shared1),
            "BBB": _prices_from_returns(blended),
            "CCC": _prices_from_returns(shared2),
        },
    )
    report = diagnose_statistical_risk_clusters(store, as_of, ["CCC", "AAA", "BBB"], lookback, 0.5)
    pair_map = {(pair.symbol_a, pair.symbol_b): pair for pair in report.pairs}
    assert pair_map[("AAA", "BBB")].above_threshold is True
    assert pair_map[("BBB", "CCC")].above_threshold is True
    assert pair_map[("AAA", "CCC")].above_threshold is False
    assert len(report.clusters) == 1
    assert report.clusters[0].symbols == ["AAA", "BBB", "CCC"]
    assert report.ready_for_portfolio_constraints is True


def test_missing_history_is_unresolved_not_singleton() -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    as_of = calendar[-1]
    lookback = 5
    returns = [0.01, -0.01, 0.02, 0.0, 0.015]
    full = _prices_from_returns(returns)
    store = _store(calendar[: lookback + 1], {"AAA": full, "BBB": full})
    # Drop early bars for CCC by rebuilding store with short history.
    short_calendar = calendar[2 : lookback + 1]
    rows = (
        _rows_for_symbol("AAA", calendar[: lookback + 1], full)
        + _rows_for_symbol("BBB", calendar[: lookback + 1], full)
        + _rows_for_symbol("CCC", short_calendar, full[2:])
    )
    store = store_from_rows(calendar[: lookback + 1], rows)
    report = diagnose_statistical_risk_clusters(store, as_of, ["AAA", "BBB", "CCC"], lookback, 0.9)
    assert report.ready_for_portfolio_constraints is False
    assert any(
        item.symbol == "CCC" and item.reason == "missing_trading_day_history"
        for item in report.unresolved_symbols
    )
    clustered = {symbol for cluster in report.clusters for symbol in cluster.symbols}
    assert "CCC" not in clustered


def test_missing_trading_day_gap_is_unresolved() -> None:
    calendar = weekdays(date(2024, 1, 2), 7)
    as_of = calendar[-1]
    lookback = 5
    prices = _prices_from_returns([0.01] * lookback)
    rows = _rows_for_symbol("AAA", calendar[: lookback + 1], prices)
    # Remove one required trading day for BBB.
    gap_days = [day for day in calendar[: lookback + 1] if day != calendar[2]]
    gap_prices = prices[:2] + prices[3:]
    rows.extend(_rows_for_symbol("BBB", gap_days, gap_prices))
    store = store_from_rows(calendar[: lookback + 1], rows)
    report = diagnose_statistical_risk_clusters(store, as_of, ["AAA", "BBB"], lookback, 0.8)
    assert any(
        item.symbol == "BBB" and item.reason == "missing_trading_day_history"
        for item in report.unresolved_symbols
    )
    assert report.ready_for_portfolio_constraints is False


def test_constant_returns_pair_is_unresolved() -> None:
    calendar = weekdays(date(2024, 1, 2), 7)
    as_of = calendar[-1]
    lookback = 5
    constant = _prices_from_returns([0.0] * lookback)
    varying = _prices_from_returns([0.01, -0.02, 0.015, 0.0, 0.02])
    store = _store(calendar[: lookback + 1], {"AAA": constant, "BBB": varying, "CCC": constant})
    report = diagnose_statistical_risk_clusters(store, as_of, ["AAA", "BBB", "CCC"], lookback, 0.5)
    reasons = {(item.symbol_a, item.symbol_b, item.reason) for item in report.unresolved_pairs}
    assert ("AAA", "CCC", "constant_return_series") in reasons
    assert report.ready_for_portfolio_constraints is False


def test_duplicate_dates_and_bad_price_are_unresolved() -> None:
    calendar = weekdays(date(2024, 1, 2), 7)
    as_of = calendar[-1]
    lookback = 5
    prices = _prices_from_returns([0.01] * lookback)
    good = _rows_for_symbol("AAA", calendar[: lookback + 1], prices)
    dup = _rows_for_symbol("BBB", calendar[: lookback + 1], prices)
    dup.append(bar("BBB", calendar[1], 11.0, 11.1, 10.9, 11.0))
    bad = _rows_for_symbol("CCC", calendar[: lookback + 1], prices)
    bad[2] = bar("CCC", calendar[2], -1.0, -0.9, -1.1, -1.0)
    store = store_from_rows(calendar[: lookback + 1], good + dup + bad)
    report = diagnose_statistical_risk_clusters(store, as_of, ["AAA", "BBB", "CCC"], lookback, 0.8)
    by_symbol = {item.symbol: item.reason for item in report.unresolved_symbols}
    assert by_symbol["BBB"] == "duplicate_dates"
    assert by_symbol["CCC"] == "non_finite_or_non_positive_adj_close"
    assert report.ready_for_portfolio_constraints is False


def test_duplicate_candidates_rejected() -> None:
    calendar = weekdays(date(2024, 1, 2), 6)
    store = _store(
        calendar,
        {"AAA": _prices_from_returns([0.01] * 5), "BBB": _prices_from_returns([0.02] * 5)},
    )
    with pytest.raises(ValueError, match="duplicate candidate"):
        diagnose_statistical_risk_clusters(store, calendar[-1], ["AAA", "AAA"], 4, 0.7)


def test_diagnose_requires_all_arguments() -> None:
    with pytest.raises(TypeError):
        diagnose_statistical_risk_clusters()
