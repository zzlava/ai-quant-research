from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

import polars as pl

from app.clock import available_at_utc
from app.errors import DataQualityError
from app.models.config import SessionConfig, StrategyConfig
from app.providers._frames import DAILY_SCHEMA, GLOBAL_SCHEMA, INSTRUMENT_SCHEMA

Adjustment = Literal["forward", "backward", "none"]
STOCK_TS_CODE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
ST_NAME = re.compile(r"(S\*ST|\*ST|SST|ST|PT)")
CN_MARKETS = {"CN", "SSE", "SZSE", "BSE", "CSI"}


@dataclass
class TushareRaw:
    trade_cal: pl.DataFrame
    stock_basic: pl.DataFrame
    daily: pl.DataFrame
    daily_basic: pl.DataFrame
    adj_factor: pl.DataFrame
    stk_limit: pl.DataFrame | None
    suspend_d: pl.DataFrame | None
    namechange: pl.DataFrame | None
    index_daily: pl.DataFrame
    index_global: pl.DataFrame


def ymd(value: date) -> str:
    return value.strftime("%Y%m%d")


def parse_ymd(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("-", "")
    if len(text) < 8 or not text[:8].isdigit():
        raise DataQualityError(f"invalid Tushare date: {value!r}")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def require_ts_code(symbol: str, *, kind: str) -> str:
    code = symbol.strip()
    if not code:
        raise DataQualityError(f"empty {kind} symbol")
    if kind == "stock" and not STOCK_TS_CODE.match(code):
        raise DataQualityError(
            f"{kind} symbol '{code}' must already be a Tushare ts_code such as 000001.SZ; suffixes are not inferred"
        )
    return code


def session_is_global(session: SessionConfig) -> bool:
    return session.market.upper() not in CN_MARKETS


def split_session_symbols(config: StrategyConfig, stocks: list[str]) -> tuple[list[str], list[str]]:
    stock_set = set(stocks)
    indices: list[str] = []
    globals_: list[str] = []
    wanted = {config.data.market_index, config.data.global_symbol, *config.data.sessions}
    for symbol in sorted(wanted):
        session = config.data.sessions.get(symbol)
        if session is not None and session_is_global(session):
            if symbol not in globals_:
                globals_.append(symbol)
            continue
        if symbol in stock_set:
            continue
        if symbol not in indices:
            indices.append(symbol)
    return indices, globals_


def normalize_tushare(
    raw: TushareRaw,
    config: StrategyConfig,
    start: date,
    end: date,
    stocks: list[str],
) -> dict[str, pl.DataFrame]:
    if raw.stk_limit is None:
        raise DataQualityError("stk_limit records are missing; refusing to invent price_limit_pct")
    if raw.suspend_d is None:
        raise DataQualityError("suspend_d records are missing; refusing to invent trading status")
    if raw.namechange is None:
        raise DataQualityError("namechange records are missing; refusing to invent is_st")

    calendar = _normalize_calendar(raw.trade_cal, start, end)
    cal_days = [day for day in calendar["date"].to_list() if isinstance(day, date)]
    daily = _normalize_daily(raw, config.data.adjustment, stocks, cal_days, start, end)
    index_bars = _normalize_index(raw.index_daily, start, end)
    global_bars = _normalize_global(raw.index_global, config, start, end)
    instruments = _normalize_instruments(raw.stock_basic, stocks, config, index_bars, global_bars)
    _assert_strategy_symbols(config, daily, index_bars, global_bars)
    return {
        "daily_bars": daily.select(list(DAILY_SCHEMA)),
        "index_bars": index_bars.select(list(DAILY_SCHEMA)),
        "global_bars": global_bars.select(list(GLOBAL_SCHEMA)),
        "instruments": instruments.select(list(INSTRUMENT_SCHEMA)),
        "calendar": calendar,
    }


def _normalize_calendar(frame: pl.DataFrame, start: date, end: date) -> pl.DataFrame:
    if frame.is_empty() or "cal_date" not in frame.columns:
        raise DataQualityError("trade_cal returned no open dates")
    work = frame
    if "is_open" in work.columns:
        work = work.filter(pl.col("is_open").cast(pl.Utf8).is_in(["1", "1.0", "true", "True"]))
    dates = sorted({parse_ymd(v) for v in work["cal_date"].to_list() if v is not None})
    dates = [d for d in dates if start <= d <= end]
    if not dates:
        raise DataQualityError("trade_cal has no open dates in the requested range")
    return pl.DataFrame({"date": dates}).with_columns(pl.col("date").cast(pl.Date))


def _normalize_daily(
    raw: TushareRaw,
    adjustment: Adjustment,
    stocks: list[str],
    calendar: list[date],
    start: date,
    end: date,
) -> pl.DataFrame:
    if raw.stk_limit is None or raw.suspend_d is None or raw.namechange is None:
        raise DataQualityError("required Tushare reference tables are missing")
    if raw.daily.is_empty():
        raise DataQualityError("daily bars are empty")
    daily = _require_cols(raw.daily, ("ts_code", "trade_date", "open", "high", "low", "close"), "daily")
    listing_bounds = _listing_bounds(raw.stock_basic, stocks)
    stk_limit = raw.stk_limit
    suspend_d = raw.suspend_d
    namechange = raw.namechange
    rows: list[dict[str, object]] = []
    by_symbol: dict[str, list[dict[str, object]]] = {}
    for item in daily.to_dicts():
        symbol = str(item["ts_code"]).strip()
        if symbol not in stocks:
            continue
        dt = parse_ymd(item["trade_date"])
        if dt < start or dt > end:
            continue
        row = {
            "symbol": symbol,
            "date": dt,
            "open": _finite_number(item.get("open"), "open"),
            "high": _finite_number(item.get("high"), "high"),
            "low": _finite_number(item.get("low"), "low"),
            "close": _finite_number(item.get("close"), "close"),
            "volume": _hands_to_shares(item.get("vol")),
            "amount": _thousand_yuan_to_yuan(item.get("amount")),
            "pre_close": _optional_number(item.get("pre_close")),
        }
        by_symbol.setdefault(symbol, []).append(row)

    turnover = _turnover_map(raw.daily_basic)
    limits = _limit_map(stk_limit)
    factors = _factor_map(raw.adj_factor)
    latest_factor = {symbol: max(vals.items())[1] for symbol, vals in factors.items() if vals}
    st_periods = _st_periods(namechange)
    suspend_days = _full_day_suspends(suspend_d, stocks)

    for symbol, items in by_symbol.items():
        listed_from, delist_on = listing_bounds[symbol]
        items = [
            row
            for row in items
            if isinstance(row["date"], date) and _in_listing_window(row["date"], listed_from, delist_on)
        ]
        items.sort(key=lambda r: r["date"])  # type: ignore[arg-type, return-value]
        last_close = None
        present = {r["date"] for r in items}
        for day in calendar:
            if not _in_listing_window(day, listed_from, delist_on):
                continue
            if day in present:
                continue
            if (symbol, day) not in suspend_days:
                continue
            if last_close is None:
                prior = [r for r in items if isinstance(r["date"], date) and r["date"] < day]
                if not prior:
                    raise DataQualityError(
                        f"cannot synthesize suspended bar for {symbol} on {day}: no previous close"
                    )
                last_close = _as_float(prior[-1]["close"])
            items.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "open": last_close,
                    "high": last_close,
                    "low": last_close,
                    "close": last_close,
                    "volume": 0.0,
                    "amount": 0.0,
                    "pre_close": last_close,
                    "_synthesized_suspend": True,
                }
            )
        items.sort(key=lambda r: r["date"])  # type: ignore[arg-type, return-value]
        for row in items:
            raw_dt = row["date"]
            if not isinstance(raw_dt, date):
                raise DataQualityError("daily bar date is invalid")
            dt = raw_dt
            key = (symbol, dt)
            if key not in limits:
                raise DataQualityError(
                    f"stk_limit missing for {symbol} on {dt}; refusing to default price_limit_pct to 10%"
                )
            pre_close, up_limit, down_limit = limits[key]
            limit_pct = _price_limit_pct(pre_close, up_limit, down_limit)
            o = _as_float(row["open"])
            h = _as_float(row["high"])
            low = _as_float(row["low"])
            c = _as_float(row["close"])
            o, h, low, c = _adjust_ohlc(
                symbol,
                dt,
                o,
                h,
                low,
                c,
                adjustment,
                factors,
                latest_factor,
                allow_previous_factor=bool(row.get("_synthesized_suspend")),
            )
            rows.append(
                {
                    "symbol": symbol,
                    "date": dt,
                    "open": o,
                    "high": h,
                    "low": low,
                    "close": c,
                    "volume": _as_float(row["volume"]),
                    "amount": _as_float(row["amount"]),
                    "turnover_rate": turnover.get(key, 0.0),
                    "is_st": _is_st_on(symbol, dt, st_periods),
                    "is_suspended": key in suspend_days or bool(row.get("_synthesized_suspend")),
                    "price_limit_pct": limit_pct,
                }
            )
            last_close = _as_float(row["close"])
    present_dates: dict[str, set[date]] = {}
    for row in rows:
        raw_dt = row["date"]
        if isinstance(raw_dt, date):
            present_dates.setdefault(str(row["symbol"]), set()).add(raw_dt)
    _assert_no_unknown_listed_gaps(stocks, calendar, listing_bounds, present_dates)
    if not rows:
        raise DataQualityError("no daily bars remained after Tushare normalization")
    return pl.DataFrame(rows).with_columns(
        [
            pl.col("date").cast(pl.Date),
            pl.col("is_st").cast(pl.Boolean),
            pl.col("is_suspended").cast(pl.Boolean),
            pl.col("price_limit_pct").cast(pl.Float64),
        ]
    )


def _normalize_index(frame: pl.DataFrame, start: date, end: date) -> pl.DataFrame:
    if frame.is_empty():
        raise DataQualityError("index_daily is empty")
    _require_cols(frame, ("ts_code", "trade_date", "open", "high", "low", "close"), "index_daily")
    rows = []
    for item in frame.to_dicts():
        dt = parse_ymd(item["trade_date"])
        if dt < start or dt > end:
            continue
        rows.append(
            {
                "symbol": str(item["ts_code"]).strip(),
                "date": dt,
                "open": _finite_number(item.get("open"), "open"),
                "high": _finite_number(item.get("high"), "high"),
                "low": _finite_number(item.get("low"), "low"),
                "close": _finite_number(item.get("close"), "close"),
                "volume": _hands_to_shares(item.get("vol")),
                "amount": _thousand_yuan_to_yuan(item.get("amount")),
                "turnover_rate": 0.0,
                "is_st": False,
                "is_suspended": False,
                "price_limit_pct": None,
            }
        )
    if not rows:
        raise DataQualityError("index_daily has no rows in the requested range")
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date), pl.col("price_limit_pct").cast(pl.Float64))


def _normalize_global(frame: pl.DataFrame, config: StrategyConfig, start: date, end: date) -> pl.DataFrame:
    if frame.is_empty():
        raise DataQualityError("index_global is empty")
    _require_cols(frame, ("ts_code", "trade_date", "close"), "index_global")
    rows = []
    for item in frame.to_dicts():
        symbol = str(item["ts_code"]).strip()
        dt = parse_ymd(item["trade_date"])
        if dt < start or dt > end:
            continue
        if symbol not in config.data.sessions:
            raise DataQualityError(f"global symbol '{symbol}' has no data.sessions contract")
        session = config.data.sessions[symbol]
        known = available_at_utc(dt, session)
        if known.tzinfo is not None:
            raise DataQualityError("available_at must be naive UTC")
        close = _finite_number(item.get("close"), "close")
        pre = _optional_number(item.get("pre_close"))
        ret = ((close / pre) - 1.0) if pre is not None and pre > 0 else 0.0
        rows.append(
            {
                "symbol": symbol,
                "date": dt,
                "close": close,
                "ret_1d": ret,
                "market": session.market,
                "timezone": session.timezone,
                "available_at": known,
            }
        )
    if not rows:
        raise DataQualityError("index_global has no rows in the requested range")
    return pl.DataFrame(rows).with_columns(
        [pl.col("date").cast(pl.Date), pl.col("available_at").cast(pl.Datetime("us"))]
    )


def _normalize_instruments(
    basic: pl.DataFrame,
    stocks: list[str],
    config: StrategyConfig,
    index_bars: pl.DataFrame,
    global_bars: pl.DataFrame,
) -> pl.DataFrame:
    by_code: dict[str, dict[str, object]] = {}
    if not basic.is_empty() and "ts_code" in basic.columns:
        for basic_row in basic.to_dicts():
            by_code[str(basic_row["ts_code"]).strip()] = basic_row
    rows: list[dict[str, object]] = []
    for symbol in stocks:
        item = by_code.get(symbol)
        if item is None:
            raise DataQualityError(f"stock_basic missing {symbol}")
        list_raw = item.get("list_date")
        if not list_raw:
            raise DataQualityError(f"stock_basic missing list_date for {symbol}")
        listing = parse_ymd(list_raw)
        session = config.data.sessions.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "name": str(item.get("name") or symbol),
                "sector": str(item.get("industry") or "unknown"),
                "listing_date": listing,
                "is_index": False,
                "is_global": False,
                "market": "CN",
                "timezone": session.timezone if session else "Asia/Shanghai",
                "session_close": session.session_close if session else "15:00",
            }
        )
    for symbol in sorted(set(index_bars["symbol"].to_list())):
        session = config.data.sessions.get(str(symbol))
        rows.append(
            {
                "symbol": str(symbol),
                "name": str(symbol),
                "sector": "index",
                "listing_date": date(1990, 1, 1),
                "is_index": True,
                "is_global": False,
                "market": session.market if session else "CN",
                "timezone": session.timezone if session else "Asia/Shanghai",
                "session_close": session.session_close if session else "15:00",
            }
        )
    for symbol in sorted(set(global_bars["symbol"].to_list())):
        session = config.data.sessions[str(symbol)]
        rows.append(
            {
                "symbol": str(symbol),
                "name": str(symbol),
                "sector": "global",
                "listing_date": date(1990, 1, 1),
                "is_index": False,
                "is_global": True,
                "market": session.market,
                "timezone": session.timezone,
                "session_close": session.session_close,
            }
        )
    return pl.DataFrame(rows).with_columns(pl.col("listing_date").cast(pl.Date))


def _assert_strategy_symbols(
    config: StrategyConfig,
    daily: pl.DataFrame,
    index_bars: pl.DataFrame,
    global_bars: pl.DataFrame,
) -> None:
    index_syms = set(index_bars["symbol"].to_list())
    global_syms = set(global_bars["symbol"].to_list())
    stock_syms = set(daily["symbol"].to_list())
    if config.data.market_index not in index_syms:
        raise DataQualityError(f"strategy market_index '{config.data.market_index}' is not in index_bars")
    if config.data.global_symbol not in global_syms:
        raise DataQualityError(f"strategy global_symbol '{config.data.global_symbol}' is not in global_bars")
    for symbol, session in config.data.sessions.items():
        if session_is_global(session):
            if symbol not in global_syms:
                raise DataQualityError(f"session '{symbol}' is not in global_bars")
        elif symbol in stock_syms or symbol in index_syms:
            continue
        else:
            raise DataQualityError(f"session '{symbol}' is not present in daily_bars or index_bars")


def _require_cols(frame: pl.DataFrame, cols: tuple[str, ...], name: str) -> pl.DataFrame:
    missing = [col for col in cols if col not in frame.columns]
    if missing:
        raise DataQualityError(f"{name} missing columns: {missing}")
    return frame


def _finite_number(value: object, field: str) -> float:
    number = _optional_number(value)
    if number is None:
        raise DataQualityError(f"{field} is missing or not finite")
    return number


def _as_float(value: object) -> float:
    return _finite_number(value, "numeric")


def _optional_number(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    else:
        try:
            number = float(str(value))
        except (TypeError, ValueError):
            return None
    if not math.isfinite(number):
        return None
    return number


def _hands_to_shares(value: object) -> float:
    number = _optional_number(value)
    return 0.0 if number is None else number * 100.0


def _thousand_yuan_to_yuan(value: object) -> float:
    number = _optional_number(value)
    return 0.0 if number is None else number * 1000.0


def _turnover_map(frame: pl.DataFrame) -> dict[tuple[str, date], float]:
    out: dict[tuple[str, date], float] = {}
    if frame.is_empty() or "ts_code" not in frame.columns:
        return out
    for item in frame.to_dicts():
        rate = _optional_number(item.get("turnover_rate"))
        if rate is None:
            continue
        out[(str(item["ts_code"]).strip(), parse_ymd(item["trade_date"]))] = rate / 100.0
    return out


def _limit_map(frame: pl.DataFrame) -> dict[tuple[str, date], tuple[float | None, float | None, float | None]]:
    _require_cols(frame, ("ts_code", "trade_date"), "stk_limit")
    out: dict[tuple[str, date], tuple[float | None, float | None, float | None]] = {}
    for item in frame.to_dicts():
        out[(str(item["ts_code"]).strip(), parse_ymd(item["trade_date"]))] = (
            _optional_number(item.get("pre_close")),
            _optional_number(item.get("up_limit")),
            _optional_number(item.get("down_limit")),
        )
    return out


def _price_limit_pct(pre_close: float | None, up_limit: float | None, down_limit: float | None) -> float | None:
    if pre_close is None or pre_close <= 0:
        return None
    if up_limit is not None and up_limit > 0:
        return (up_limit / pre_close) - 1.0
    if down_limit is not None and down_limit > 0:
        return 1.0 - (down_limit / pre_close)
    return None


def _factor_map(frame: pl.DataFrame) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    if frame.is_empty() or "ts_code" not in frame.columns:
        return out
    for item in frame.to_dicts():
        factor = _optional_number(item.get("adj_factor"))
        if factor is None:
            continue
        out.setdefault(str(item["ts_code"]).strip(), {})[parse_ymd(item["trade_date"])] = factor
    return out


def _adjust_ohlc(
    symbol: str,
    dt: date,
    open_: float,
    high: float,
    low: float,
    close: float,
    adjustment: Adjustment,
    factors: dict[str, dict[date, float]],
    latest_factor: dict[str, float],
    allow_previous_factor: bool = False,
) -> tuple[float, float, float, float]:
    if adjustment == "none":
        return open_, high, low, close
    factor = factors.get(symbol, {}).get(dt)
    if factor is None and allow_previous_factor:
        earlier = [day for day in factors.get(symbol, {}) if day < dt]
        factor = factors[symbol][max(earlier)] if earlier else None
    if factor is None:
        raise DataQualityError(f"adj_factor missing for {symbol} on {dt}")
    if adjustment == "backward":
        scale = factor
    else:
        latest = latest_factor.get(symbol)
        if latest is None or latest == 0:
            raise DataQualityError(f"latest adj_factor missing for {symbol}")
        scale = factor / latest
    return open_ * scale, high * scale, low * scale, close * scale


def _st_periods(frame: pl.DataFrame) -> dict[str, list[tuple[date, date | None]]]:
    out: dict[str, list[tuple[date, date | None]]] = {}
    if frame.is_empty():
        return out
    _require_cols(frame, ("ts_code", "name", "start_date"), "namechange")
    for item in frame.to_dicts():
        name = str(item.get("name") or "")
        if not ST_NAME.search(name):
            continue
        start = parse_ymd(item["start_date"])
        end_raw = item.get("end_date")
        end = parse_ymd(end_raw) if end_raw not in (None, "", "None") else None
        out.setdefault(str(item["ts_code"]).strip(), []).append((start, end))
    return out


def _is_st_on(symbol: str, dt: date, periods: dict[str, list[tuple[date, date | None]]]) -> bool:
    for start, end in periods.get(symbol, []):
        if start <= dt and (end is None or dt <= end):
            return True
    return False


def _full_day_suspends(frame: pl.DataFrame, stocks: list[str]) -> set[tuple[str, date]]:
    out: set[tuple[str, date]] = set()
    if frame.is_empty():
        return out
    _require_cols(frame, ("ts_code", "trade_date", "suspend_type"), "suspend_d")
    stock_set = set(stocks)
    for item in frame.to_dicts():
        symbol = str(item["ts_code"]).strip()
        if symbol not in stock_set:
            continue
        if str(item.get("suspend_type") or "").upper() != "S":
            continue
        timing = item.get("suspend_timing")
        if timing not in (None, "", "None"):
            continue
        out.add((symbol, parse_ymd(item["trade_date"])))
    return out


def _optional_ymd(value: object) -> date | None:
    if value in (None, "", "None"):
        return None
    return parse_ymd(value)


def _listing_bounds(basic: pl.DataFrame, stocks: list[str]) -> dict[str, tuple[date, date | None]]:
    by_code: dict[str, dict[str, object]] = {}
    if not basic.is_empty() and "ts_code" in basic.columns:
        for item in basic.to_dicts():
            by_code[str(item["ts_code"]).strip()] = item
    out: dict[str, tuple[date, date | None]] = {}
    for symbol in stocks:
        basic_row = by_code.get(symbol)
        if basic_row is None:
            raise DataQualityError(f"stock_basic missing {symbol}")
        list_raw = basic_row.get("list_date")
        if not list_raw:
            raise DataQualityError(f"stock_basic missing list_date for {symbol}")
        listed_from = parse_ymd(list_raw)
        delist_on = _optional_ymd(basic_row.get("delist_date"))
        if delist_on is not None and delist_on <= listed_from:
            raise DataQualityError(f"delist_date is not after list_date for {symbol}")
        out[symbol] = (listed_from, delist_on)
    return out


def _in_listing_window(day: date, listed_from: date, delist_on: date | None) -> bool:
    if day < listed_from:
        return False
    return delist_on is None or day < delist_on


def _assert_no_unknown_listed_gaps(
    stocks: list[str],
    calendar: list[date],
    listing_bounds: dict[str, tuple[date, date | None]],
    present_dates: dict[str, set[date]],
) -> None:
    for symbol in stocks:
        listed_from, delist_on = listing_bounds[symbol]
        have = present_dates.get(symbol, set())
        missing = [day for day in calendar if _in_listing_window(day, listed_from, delist_on) and day not in have]
        if missing:
            raise DataQualityError(
                f"unknown daily gap for {symbol} on {missing[0]}; "
                "refusing to skip listed trading days that are not full-day suspends"
            )
