from __future__ import annotations

from datetime import date

import polars as pl

from app.models.market import Instrument
from app.providers.base import MarketDataProvider


class AKShareProvider(MarketDataProvider):
    """Adapter only. Live AKShare fetch is not implemented in this MVP."""

    def __init__(self) -> None:
        return

    def get_instruments(self) -> list[Instrument]:
        raise NotImplementedError(self._todo())

    def get_calendar(self, start: date, end: date) -> list[date]:
        raise NotImplementedError(self._todo())

    def get_daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        raise NotImplementedError(self._todo())

    def get_all_daily_bars(self, start: date | None = None, end: date | None = None) -> pl.DataFrame:
        raise NotImplementedError(self._todo())

    def get_index_bars(
        self,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        raise NotImplementedError(self._todo())

    def get_global_bars(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        raise NotImplementedError(self._todo())

    @staticmethod
    def _todo() -> str:
        return (
            "TODO: implement AKShareProvider network fetch. "
            "MVP tests must use CsvProvider or DemoProvider."
        )
