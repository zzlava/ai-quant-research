from __future__ import annotations

from datetime import date

import polars as pl

from app.models.market import Instrument
from app.providers.base import MarketDataProvider


class TushareProvider(MarketDataProvider):
    """Adapter only. Live Tushare fetch is not implemented in this MVP.

    TODO: when implementing, the adapter must:
    - declare adjustment (forward/backward/none) on the snapshot
    - attach market/timezone/available_at for every global bar
    - run storage.quality checks before serving data
    - never be used by tests (offline CsvProvider / DemoProvider only)
    """

    def __init__(self, token: str | None = None) -> None:
        self.token = token

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
            "TODO: implement TushareProvider with an official token. "
            "MVP tests must use CsvProvider or DemoProvider."
        )
