from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class Instrument(BaseModel):
    symbol: str
    name: str
    sector: str
    listing_date: date
    is_index: bool = False
    is_global: bool = False


class DailyBar(BaseModel):
    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    turnover_rate: float = 0.0
    is_st: bool = False
    is_suspended: bool = False


class GlobalBar(BaseModel):
    symbol: str
    date: date
    close: float
    ret_1d: float = 0.0


class MarketBundle(BaseModel):
    """In-memory deterministic market snapshot used by demo/CSV providers."""

    instruments: list[Instrument]
    daily_bars: list[DailyBar]
    index_bars: list[DailyBar]
    global_bars: list[GlobalBar]
    calendar: list[date] = Field(default_factory=list)
