from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator


class Instrument(BaseModel):
    symbol: str
    name: str
    sector: str
    listing_date: date
    is_index: bool = False
    is_global: bool = False
    market: str = "CN"
    timezone: str = "Asia/Shanghai"
    session_close: str = "15:00"


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
    price_limit_pct: float | None = None
    # Canonical OHLC is always the exchange's unadjusted price and is the only
    # price basis the execution engine may use.  These adjusted columns are
    # derived solely for return/technical-indicator calculations.
    adj_open: float | None = None
    adj_high: float | None = None
    adj_low: float | None = None
    adj_close: float | None = None
    adj_factor: float = 1.0
    pre_close: float | None = None
    up_limit: float | None = None
    down_limit: float | None = None

    @model_validator(mode="after")
    def default_adjusted_prices_to_raw(self) -> DailyBar:
        if self.adj_open is None:
            self.adj_open = self.open
        if self.adj_high is None:
            self.adj_high = self.high
        if self.adj_low is None:
            self.adj_low = self.low
        if self.adj_close is None:
            self.adj_close = self.close
        return self


class GlobalBar(BaseModel):
    symbol: str
    date: date
    close: float
    ret_1d: float = 0.0
    market: str = "US"
    timezone: str = "America/New_York"
    available_at: datetime


class MarketBundle(BaseModel):
    """In-memory deterministic market snapshot used by demo/CSV providers."""

    instruments: list[Instrument]
    daily_bars: list[DailyBar]
    index_bars: list[DailyBar]
    global_bars: list[GlobalBar]
    calendar: list[date] = Field(default_factory=list)
    adjustment: str = "forward"
