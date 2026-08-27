from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.errors import DataQualityError, TushareFetchError
from app.providers.tushare_normalize import require_ts_code

_TS_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ)$")
_DATE_YYYYMMDD_RE = re.compile(r"^\d{8}$")


def canonical_a_share_symbol_from_stock_basic_row(item: dict[str, Any]) -> str | None:
    raw_symbol = str(item.get("ts_code") or "").strip()
    if _TS_CODE_RE.fullmatch(raw_symbol) is None:
        return None
    code, suffix = raw_symbol.split(".")
    market_text = str(item.get("market") or "")
    name_text = str(item.get("name") or "")
    combined = f"{market_text} {name_text}".lower()
    if suffix == "SH" and code.startswith("9"):
        return None
    if suffix == "SZ" and code.startswith("2"):
        return None
    if "b股" in combined or "b-share" in combined or " b " in f" {combined} ":
        return None
    if "指数" in combined or "index" in combined:
        return None
    return require_ts_code(raw_symbol, kind="stock")


def parse_stock_basic_list_date(value: object, *, symbol: str) -> date:
    text = str(value or "").strip().replace("-", "")
    if _DATE_YYYYMMDD_RE.fullmatch(text) is None:
        raise DataQualityError(f"stock_basic list_date is invalid for {symbol}")
    try:
        return date(year=int(text[:4]), month=int(text[4:6]), day=int(text[6:8]))
    except ValueError as exc:
        raise DataQualityError(f"stock_basic list_date is invalid for {symbol}") from exc


def load_canonical_symbol_listing_dates(stock_basic_path: Path) -> tuple[list[str], dict[str, date]]:
    if not stock_basic_path.is_file():
        raise TushareFetchError(f"missing stock_basic source: {stock_basic_path}")
    frame = pl.read_parquet(stock_basic_path)
    if "ts_code" not in frame.columns or "list_date" not in frame.columns:
        raise DataQualityError("stock_basic missing required columns ts_code/list_date")

    listing_dates: dict[str, date] = {}
    for row in frame.iter_rows(named=True):
        symbol = canonical_a_share_symbol_from_stock_basic_row(row)
        if symbol is None:
            continue
        listed_on = parse_stock_basic_list_date(row.get("list_date"), symbol=symbol)
        existing = listing_dates.get(symbol)
        if existing is not None and existing != listed_on:
            raise DataQualityError(f"stock_basic has conflicting list_date for {symbol}")
        listing_dates[symbol] = listed_on
    symbols = sorted(listing_dates)
    if not symbols:
        raise DataQualityError("stock_basic produced zero canonical symbols")
    return symbols, listing_dates


def canonical_symbols_sha256(symbols: list[str]) -> str:
    return hashlib.sha256(("\n".join(symbols) + "\n").encode("utf-8")).hexdigest()


__all__ = [
    "canonical_a_share_symbol_from_stock_basic_row",
    "canonical_symbols_sha256",
    "load_canonical_symbol_listing_dates",
    "parse_stock_basic_list_date",
]
