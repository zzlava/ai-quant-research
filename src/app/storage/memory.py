from __future__ import annotations

from datetime import date, datetime

import polars as pl

from app.models.market import Instrument
from app.models.snapshot import DataSnapshot
from app.providers._frames import (
    empty_daily,
    empty_global,
    empty_instruments,
    empty_universe_membership,
    filter_dates,
    instruments_to_frame,
)
from app.providers.base import MarketDataProvider
from app.storage.hashing import build_snapshot
from app.universe.membership import build_manual_static_membership, members_available_on


class InMemoryStore:
    def __init__(
        self,
        instruments: pl.DataFrame | None = None,
        daily: pl.DataFrame | None = None,
        index: pl.DataFrame | None = None,
        global_bars: pl.DataFrame | None = None,
        calendar: list[date] | None = None,
        universe_membership: pl.DataFrame | None = None,
        snapshot: DataSnapshot | None = None,
        source_name: str = "memory",
        adjustment: str = "forward",
        universe_id: str = "demo",
    ) -> None:
        self.instruments_frame = instruments if instruments is not None else empty_instruments()
        self.daily = daily if daily is not None else empty_daily()
        self.index = index if index is not None else empty_daily()
        self.global_bars = global_bars if global_bars is not None else empty_global()
        self._calendar = calendar or []
        self.universe_membership = (
            universe_membership if universe_membership is not None else empty_universe_membership()
        )
        self._snapshot = snapshot
        self._source_name = source_name
        self._adjustment = adjustment
        self._universe_id = universe_id

    @classmethod
    def from_provider(cls, provider: MarketDataProvider, universe_id: str = "demo") -> InMemoryStore:
        instruments = instruments_to_frame(provider.get_instruments())
        daily = provider.get_all_daily_bars()
        index = provider.get_index_bars()
        global_bars = provider.get_global_bars()
        dates = []
        if not daily.is_empty():
            lo = daily["date"].min()
            hi = daily["date"].max()
            if isinstance(lo, date) and isinstance(hi, date):
                dates = provider.get_calendar(lo, hi)
        stocks = [
            str(row["symbol"])
            for row in instruments.to_dicts()
            if not row.get("is_index") and not row.get("is_global")
        ]
        membership = (
            build_manual_static_membership(stocks, dates, universe_id=universe_id)
            if stocks and dates
            else empty_universe_membership()
        )
        return cls(
            instruments,
            daily,
            index,
            global_bars,
            dates,
            universe_membership=membership,
            universe_id=universe_id,
        )

    def clone(self) -> InMemoryStore:
        return InMemoryStore(
            instruments=self.instruments_frame.clone(),
            daily=self.daily.clone(),
            index=self.index.clone(),
            global_bars=self.global_bars.clone(),
            calendar=list(self._calendar),
            universe_membership=self.universe_membership.clone(),
            snapshot=self._snapshot,
            source_name=self._source_name,
            adjustment=self._adjustment,
            universe_id=self._universe_id,
        )

    def replace_daily(self, daily: pl.DataFrame) -> None:
        self.daily = daily
        self._snapshot = None

    def get_instruments(self) -> list[Instrument]:
        return [Instrument.model_validate(row) for row in self.instruments_frame.to_dicts()]

    def get_calendar(self, start: date, end: date) -> list[date]:
        return [d for d in self._calendar if start <= d <= end]

    def get_daily_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        return filter_dates(self.daily, start, as_of, symbol)

    def get_index_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        return filter_dates(self.index, start, as_of, symbol)

    def get_global_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        return filter_dates(self.global_bars, start, as_of, symbol)

    def get_universe_members(
        self,
        universe_id: str,
        as_of: date,
        available_by: datetime,
        *,
        expected_constituents: int | None = None,
        require_available_cross_section: bool = False,
    ) -> set[str]:
        return members_available_on(
            self.universe_membership,
            universe_id=universe_id,
            as_of=as_of,
            available_by=available_by,
            expected_constituents=expected_constituents,
            require_available_cross_section=require_available_cross_section,
        )

    def next_trading_day(self, after: date) -> date | None:
        for d in self._calendar:
            if d > after:
                return d
        return None

    def trading_days_after(self, after: date, n: int) -> list[date]:
        days = [d for d in self._calendar if d > after]
        return days[:n]

    def snapshot(self) -> DataSnapshot:
        if self._snapshot is None:
            calendar = pl.DataFrame({"date": self._calendar}).with_columns(pl.col("date").cast(pl.Date))
            self._snapshot = build_snapshot(
                {
                    "daily_bars": self.daily,
                    "index_bars": self.index,
                    "global_bars": self.global_bars,
                    "instruments": self.instruments_frame,
                    "calendar": calendar,
                    "universe_membership": self.universe_membership,
                },
                adjustment=self._adjustment,
                source_name=self._source_name,
            )
        return self._snapshot
