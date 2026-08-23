from __future__ import annotations

from datetime import date

import polars as pl

from app.errors import DataQualityError
from tests.helpers import weekdays

STOCKS = ("000001.SZ", "600000.SH")
INDICES = ("000300.SH", "000001.SH")
GLOBALS = ("SPX", "HSI")


class FakeTushareClient:
    def __init__(self, tables: dict[str, pl.DataFrame], absent: set[str] | None = None) -> None:
        self.tables = tables
        self.absent = set(absent or [])
        self.calls: list[str] = []
        self.call_params: list[tuple[str, dict[str, object]]] = []

    def query(self, api_name: str, **params: object) -> pl.DataFrame:
        self.calls.append(api_name)
        self.call_params.append((api_name, dict(params)))
        if api_name in self.absent:
            raise DataQualityError(f"tushare {api_name} records are missing")
        frame = self.tables.get(api_name)
        if frame is None:
            return pl.DataFrame()
        if api_name == "stock_basic" and "list_status" in params and "list_status" in frame.columns:
            frame = frame.filter(pl.col("list_status") == str(params["list_status"]))
        return frame


def build_fake_tushare_api_tables(
    start: date = date(2023, 10, 2),
    n_days: int = 80,
    *,
    limit_override: dict[tuple[str, date], tuple[float | None, float | None, float | None]] | None = None,
    skip_daily: set[tuple[str, date]] | None = None,
    suspend_days: set[tuple[str, date]] | None = None,
    drop_limit_keys: set[tuple[str, date]] | None = None,
    list_dates: dict[str, date] | None = None,
    delist_dates: dict[str, date] | None = None,
) -> tuple[list[date], dict[str, pl.DataFrame]]:
    calendar = weekdays(start, n_days)
    skip = set(skip_daily or [])
    suspend = set(suspend_days or [])
    drop_limits = set(drop_limit_keys or [])
    overrides = limit_override or {}

    trade_cal = pl.DataFrame(
        {
            "exchange": ["SSE"] * len(calendar),
            "cal_date": [d.strftime("%Y%m%d") for d in calendar],
            "is_open": ["1"] * len(calendar),
        }
    )
    default_lists = {"000001.SZ": date(1991, 4, 3), "600000.SH": date(1999, 11, 10)}
    listed = {**default_lists, **(list_dates or {})}
    delisted = delist_dates or {}
    stock_basic = pl.DataFrame(
        {
            "ts_code": list(STOCKS),
            "name": ["平安银行", "ST浦发"],
            "industry": ["bank", "bank"],
            "list_date": [listed[symbol].strftime("%Y%m%d") for symbol in STOCKS],
            "delist_date": [
                delisted[symbol].strftime("%Y%m%d") if symbol in delisted else None for symbol in STOCKS
            ],
            "market": ["主板", "主板"],
            "exchange": ["SZSE", "SSE"],
            "list_status": ["D" if symbol in delisted else "L" for symbol in STOCKS],
        }
    )

    daily_rows: list[dict[str, object]] = []
    basic_rows: list[dict[str, object]] = []
    factor_rows: list[dict[str, object]] = []
    limit_rows: list[dict[str, object]] = []
    for symbol in STOCKS:
        price = 10.0
        for dt in calendar:
            if (symbol, dt) in skip or (symbol, dt) in suspend:
                if (symbol, dt) not in drop_limits:
                    pre, up, down = overrides.get((symbol, dt), (price, price * 1.1, price * 0.9))
                    limit_rows.append(_limit_row(symbol, dt, pre, up, down))
                continue
            o = price
            c = price
            daily_rows.append(
                {
                    "ts_code": symbol,
                    "trade_date": dt.strftime("%Y%m%d"),
                    "open": o,
                    "high": o + 0.05,
                    "low": o - 0.05,
                    "close": c,
                    "pre_close": price,
                    "vol": 200000.0,
                    "amount": 200000.0,
                }
            )
            basic_rows.append({"ts_code": symbol, "trade_date": dt.strftime("%Y%m%d"), "turnover_rate": 5.0})
            factor_rows.append({"ts_code": symbol, "trade_date": dt.strftime("%Y%m%d"), "adj_factor": 1.0})
            if (symbol, dt) not in drop_limits:
                pre, up, down = overrides.get((symbol, dt), (price, price * 1.1, price * 0.9))
                limit_rows.append(_limit_row(symbol, dt, pre, up, down))

    suspend_rows = [
        {
            "ts_code": symbol,
            "trade_date": dt.strftime("%Y%m%d"),
            "suspend_type": "S",
            "suspend_timing": None,
        }
        for symbol, dt in sorted(suspend)
    ]
    namechange = pl.DataFrame(
        {
            "ts_code": ["600000.SH"],
            "name": ["ST浦发"],
            "start_date": ["20200101"],
            "end_date": [None],
            "change_reason": ["ST"],
        }
    )
    index_daily = _index_like(INDICES, calendar, 3800.0)
    index_global = _index_like(GLOBALS, calendar, 4200.0)
    tables = {
        "trade_cal": trade_cal,
        "stock_basic": stock_basic,
        "daily": pl.DataFrame(daily_rows),
        "daily_basic": pl.DataFrame(basic_rows),
        "adj_factor": pl.DataFrame(factor_rows),
        "stk_limit": pl.DataFrame(limit_rows),
        "suspend_d": pl.DataFrame(suspend_rows) if suspend_rows else pl.DataFrame(
            {"ts_code": [], "trade_date": [], "suspend_type": [], "suspend_timing": []}
        ),
        "namechange": namechange,
        "index_daily": index_daily,
        "index_global": index_global,
    }
    return calendar, tables


def _limit_row(symbol: str, dt: date, pre: float | None, up: float | None, down: float | None) -> dict[str, object]:
    return {
        "ts_code": symbol,
        "trade_date": dt.strftime("%Y%m%d"),
        "pre_close": pre,
        "up_limit": up,
        "down_limit": down,
    }


def _index_like(symbols: tuple[str, ...], calendar: list[date], start_price: float) -> pl.DataFrame:
    rows = []
    for symbol in symbols:
        price = start_price
        for dt in calendar:
            rows.append(
                {
                    "ts_code": symbol,
                    "trade_date": dt.strftime("%Y%m%d"),
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price,
                    "pre_close": price,
                    "vol": 1000.0,
                    "amount": 1000.0,
                }
            )
    return pl.DataFrame(rows)
