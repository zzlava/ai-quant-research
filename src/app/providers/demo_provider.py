from __future__ import annotations

from datetime import date

import polars as pl

from app.demo.generator import DEMO_SEED, generate_demo_market
from app.models.market import Instrument, MarketBundle
from app.providers._frames import bars_to_frame, bundle_calendar, filter_dates, global_to_frame
from app.providers.base import MarketDataProvider


class DemoProvider(MarketDataProvider):
    """Fully offline deterministic provider. Safe for tests."""

    def __init__(
        self,
        seed: int = DEMO_SEED,
        bundle: MarketBundle | None = None,
        n_stocks: int = 50,
    ) -> None:
        self.bundle = bundle or generate_demo_market(seed=seed, n_stocks=n_stocks)

    def get_instruments(self) -> list[Instrument]:
        return list(self.bundle.instruments)

    def get_calendar(self, start: date, end: date) -> list[date]:
        return bundle_calendar(self.bundle, start, end)

    def get_daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        return filter_dates(bars_to_frame(self.bundle.daily_bars), start, end, symbol)

    def get_all_daily_bars(self, start: date | None = None, end: date | None = None) -> pl.DataFrame:
        return filter_dates(bars_to_frame(self.bundle.daily_bars), start, end)

    def get_index_bars(
        self,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        return filter_dates(bars_to_frame(self.bundle.index_bars), start, end, symbol)

    def get_global_bars(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        return filter_dates(global_to_frame(self.bundle.global_bars), start, end)
