from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.errors import DataQualityError
from app.providers import tushare_financial_negative_list_collection as collector_module
from app.research.layer_two_financial_negative_list_collection_run_contract import (
    recompute_symbol_bindings_from_stock_basic,
)
from app.research.layer_two_financial_negative_list_stock_basic import (
    canonical_symbols_sha256,
    load_canonical_symbol_listing_dates,
)


def _write_stock_basic(path: Path, *, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path)


def test_invalid_stock_basic_list_date_fails_closed(tmp_path: Path) -> None:
    stock_basic = tmp_path / "reference" / "stock_basic.parquet"
    _write_stock_basic(
        stock_basic,
        rows=[
            {"ts_code": "000001.SZ", "list_date": "20241340", "market": "主板", "name": "平安银行"},
        ],
    )
    with pytest.raises(DataQualityError, match="list_date is invalid"):
        load_canonical_symbol_listing_dates(stock_basic)


def test_run_contract_and_collector_symbol_bindings_match(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    stock_basic = raw_dir / "reference" / "stock_basic.parquet"
    _write_stock_basic(
        stock_basic,
        rows=[
            {"ts_code": "000001.SZ", "list_date": "1991-04-03", "market": "主板", "name": "平安银行"},
            {"ts_code": "600000.SH", "list_date": "1999-11-10", "market": "主板", "name": "浦发银行"},
            {"ts_code": "900901.SH", "list_date": "1992-01-01", "market": "B股", "name": "上电B股"},
        ],
    )
    symbols, listing_dates = load_canonical_symbol_listing_dates(stock_basic)
    count, symbols_sha = recompute_symbol_bindings_from_stock_basic(stock_basic)

    protocol = SimpleNamespace(bindings=SimpleNamespace(raw_collection_dir="raw"))
    collector_symbols, collector_listing_dates = collector_module._load_bound_canonical_symbols(tmp_path, protocol)

    assert count == len(symbols) == len(collector_symbols)
    assert symbols_sha == canonical_symbols_sha256(symbols)
    assert collector_symbols == symbols
    assert collector_listing_dates == listing_dates
    assert collector_listing_dates["000001.SZ"] == date(1991, 4, 3)
