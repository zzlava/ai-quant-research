from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any, cast

import polars as pl

from app.errors import DataQualityError
from app.models.market import Instrument
from app.providers._frames import (
    DAILY_SCHEMA,
    GLOBAL_SCHEMA,
    INSTRUMENT_SCHEMA,
    empty_daily,
    empty_global,
    empty_instruments,
    filter_dates,
)
from app.providers.base import MarketDataProvider
from app.storage.quality import validate_global, validate_ohlcv


class CsvProvider(MarketDataProvider):
    """Offline CSV provider. Directory layout:

    daily_bars.csv, index_bars.csv, global_bars.csv, instruments.csv, calendar.csv

    Required contract:
    - OHLC must be valid
    - no duplicate (symbol, date)
    - global_bars must include available_at
    - adjustment is declared by the caller / snapshot, not inferred here
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._instruments = self._read_csv("instruments.csv", INSTRUMENT_SCHEMA, empty_instruments)
        self._daily = self._read_csv("daily_bars.csv", DAILY_SCHEMA, empty_daily)
        self._index = self._read_csv("index_bars.csv", DAILY_SCHEMA, empty_daily)
        self._global = self._read_csv("global_bars.csv", GLOBAL_SCHEMA, empty_global)
        self._calendar = self._read_calendar()
        if not self._daily.is_empty():
            validate_ohlcv(self._daily, "daily_bars.csv")
        if not self._index.is_empty():
            validate_ohlcv(self._index, "index_bars.csv")
        if not self._global.is_empty():
            validate_global(self._global, "global_bars.csv")

    def get_instruments(self) -> list[Instrument]:
        rows = self._instruments.to_dicts()
        return [Instrument.model_validate(row) for row in rows]

    def get_calendar(self, start: date, end: date) -> list[date]:
        return [d for d in self._calendar if start <= d <= end]

    def get_daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        return filter_dates(self._daily, start, end, symbol)

    def get_all_daily_bars(self, start: date | None = None, end: date | None = None) -> pl.DataFrame:
        return filter_dates(self._daily, start, end)

    def get_index_bars(
        self,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        return filter_dates(self._index, start, end, symbol)

    def get_global_bars(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        return filter_dates(self._global, start, end)

    def _read_csv(
        self,
        name: str,
        schema: Mapping[str, object],
        empty: Callable[[], pl.DataFrame],
    ) -> pl.DataFrame:
        path = self.root / name
        if not path.exists():
            return empty()
        frame = pl.read_csv(path, try_parse_dates=True)
        if name == "global_bars.csv":
            if "ret_1d" not in frame.columns:
                frame = frame.with_columns(pl.lit(0.0).alias("ret_1d"))
            if "market" not in frame.columns:
                frame = frame.with_columns(pl.lit("US").alias("market"))
            if "timezone" not in frame.columns:
                frame = frame.with_columns(pl.lit("America/New_York").alias("timezone"))
        missing = [col for col in schema if col not in frame.columns]
        if missing:
            raise DataQualityError(f"{name} missing required columns: {missing}")
        for col in schema:
            if frame[col].dtype in (pl.Utf8, pl.String):
                frame = frame.with_columns(
                    pl.when(pl.col(col).str.strip_chars().is_in(["", "null", "None", "NA"]))
                    .then(None)
                    .otherwise(pl.col(col))
                    .alias(col)
                )
        casts = []
        for col, dtype in schema.items():
            casts.append(pl.col(col).cast(cast(Any, dtype), strict=True))
        try:
            return frame.with_columns(casts)
        except Exception as exc:
            raise DataQualityError(f"{name} failed strict type conversion") from exc

    def _read_calendar(self) -> list[date]:
        path = self.root / "calendar.csv"
        if not path.exists():
            dates = self._daily.select("date").unique().sort("date")
            return [row[0] for row in dates.iter_rows()]
        frame = pl.read_csv(path, try_parse_dates=True).with_columns(pl.col("date").cast(pl.Date, strict=True))
        return list(frame["date"].to_list())
