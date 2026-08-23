from __future__ import annotations

from datetime import date
from typing import Protocol

import polars as pl

from app.models.market import Instrument


class MarketStore(Protocol):
    """Read-only historical store. Every query must honor as_of (no lookahead)."""

    def get_instruments(self) -> list[Instrument]: ...

    def get_calendar(self, start: date, end: date) -> list[date]: ...

    def get_daily_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame: ...

    def get_index_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame: ...

    def get_global_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame: ...

    def next_trading_day(self, after: date) -> date | None: ...

    def trading_days_after(self, after: date, n: int) -> list[date]: ...
