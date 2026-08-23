from __future__ import annotations

from datetime import date

import polars as pl

from app.models.market import Instrument
from app.providers._frames import (
    empty_daily,
    empty_global,
    empty_instruments,
    filter_dates,
    instruments_to_frame,
)
from app.providers.base import MarketDataProvider


class InMemoryStore:
    def __init__(
        self,
        instruments: pl.DataFrame | None = None,
        daily: pl.DataFrame | None = None,
        index: pl.DataFrame | None = None,
        global_bars: pl.DataFrame | None = None,
        calendar: list[date] | None = None,
    ) -> None:
        self.instruments_frame = instruments if instruments is not None else empty_instruments()
        self.daily = daily if daily is not None else empty_daily()
        self.index = index if index is not None else empty_daily()
        self.global_bars = global_bars if global_bars is not None else empty_global()
        self._calendar = calendar or []

    @classmethod
    def from_provider(cls, provider: MarketDataProvider) -> InMemoryStore:
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
        return cls(instruments, daily, index, global_bars, dates)

    def clone(self) -> InMemoryStore:
        return InMemoryStore(
            instruments=self.instruments_frame.clone(),
            daily=self.daily.clone(),
            index=self.index.clone(),
            global_bars=self.global_bars.clone(),
            calendar=list(self._calendar),
        )

    def replace_daily(self, daily: pl.DataFrame) -> None:
        self.daily = daily

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

    def next_trading_day(self, after: date) -> date | None:
        for d in self._calendar:
            if d > after:
                return d
        return None

    def trading_days_after(self, after: date, n: int) -> list[date]:
        days = [d for d in self._calendar if d > after]
        return days[:n]
