from __future__ import annotations

from datetime import date

import polars as pl

from app.models.market import DailyBar, GlobalBar, Instrument, MarketBundle

DAILY_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "turnover_rate": pl.Float64,
    "is_st": pl.Boolean,
    "is_suspended": pl.Boolean,
    "price_limit_pct": pl.Float64,
}

GLOBAL_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "close": pl.Float64,
    "ret_1d": pl.Float64,
    "market": pl.String,
    "timezone": pl.String,
    "available_at": pl.Datetime("us"),
}

INSTRUMENT_SCHEMA = {
    "symbol": pl.String,
    "name": pl.String,
    "sector": pl.String,
    "listing_date": pl.Date,
    "is_index": pl.Boolean,
    "is_global": pl.Boolean,
    "market": pl.String,
    "timezone": pl.String,
    "session_close": pl.String,
}


def empty_daily() -> pl.DataFrame:
    return pl.DataFrame(schema=DAILY_SCHEMA)


def empty_global() -> pl.DataFrame:
    return pl.DataFrame(schema=GLOBAL_SCHEMA)  # type: ignore[arg-type]


def empty_instruments() -> pl.DataFrame:
    return pl.DataFrame(schema=INSTRUMENT_SCHEMA)


def bars_to_frame(bars: list[DailyBar]) -> pl.DataFrame:
    if not bars:
        return empty_daily()
    return pl.DataFrame([b.model_dump() for b in bars]).with_columns(pl.col("date").cast(pl.Date))


def global_to_frame(bars: list[GlobalBar]) -> pl.DataFrame:
    if not bars:
        return empty_global()
    return pl.DataFrame([b.model_dump() for b in bars]).with_columns(
        [
            pl.col("date").cast(pl.Date),
            pl.col("available_at").cast(pl.Datetime("us")),
        ]
    )


def instruments_to_frame(items: list[Instrument]) -> pl.DataFrame:
    if not items:
        return empty_instruments()
    return pl.DataFrame([i.model_dump() for i in items]).with_columns(pl.col("listing_date").cast(pl.Date))


def filter_dates(
    frame: pl.DataFrame,
    start: date | None = None,
    end: date | None = None,
    symbol: str | None = None,
    symbol_col: str = "symbol",
) -> pl.DataFrame:
    out = frame
    if symbol is not None:
        out = out.filter(pl.col(symbol_col) == symbol)
    if start is not None:
        out = out.filter(pl.col("date") >= start)
    if end is not None:
        out = out.filter(pl.col("date") <= end)
    return out.sort("date")


def bundle_calendar(bundle: MarketBundle, start: date, end: date) -> list[date]:
    return [d for d in bundle.calendar if start <= d <= end]
