from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import polars as pl

from app.models.market import Instrument


class MarketDataProvider(ABC):
    """Market data source. Strategies must never call a provider directly."""

    @abstractmethod
    def get_instruments(self) -> list[Instrument]:
        raise NotImplementedError

    @abstractmethod
    def get_calendar(self, start: date, end: date) -> list[date]:
        raise NotImplementedError

    @abstractmethod
    def get_daily_bars(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> pl.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def get_all_daily_bars(self, start: date | None = None, end: date | None = None) -> pl.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def get_index_bars(
        self,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def get_global_bars(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        raise NotImplementedError

    def get_sector(self, symbol: str) -> str:
        for item in self.get_instruments():
            if item.symbol == symbol:
                return item.sector
        raise KeyError(symbol)
