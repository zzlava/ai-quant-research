from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import polars as pl

from app.models.market import Instrument
from app.providers._frames import empty_daily, empty_global, empty_instruments


class DuckDBParquetStore:
    """Historical market access via DuckDB over Parquet files."""

    def __init__(self, parquet_dir: Path) -> None:
        self.parquet_dir = Path(parquet_dir)
        self.conn = duckdb.connect(database=":memory:")

    def available(self) -> bool:
        return (self.parquet_dir / "daily_bars.parquet").exists()

    def get_instruments(self) -> list[Instrument]:
        frame = self._read("instruments.parquet", empty_instruments)
        return [Instrument.model_validate(row) for row in frame.to_dicts()]

    def get_calendar(self, start: date, end: date) -> list[date]:
        path = self.parquet_dir / "calendar.parquet"
        if not path.exists():
            return []
        frame = self.conn.execute(
            "SELECT date FROM read_parquet(?) WHERE date BETWEEN ? AND ? ORDER BY date",
            [str(path), start, end],
        ).pl()
        return list(frame["date"].to_list())

    def get_daily_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        return self._query_ohlcv("daily_bars.parquet", as_of, symbol, start, empty_daily)

    def get_index_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        return self._query_ohlcv("index_bars.parquet", as_of, symbol, start, empty_daily)

    def get_global_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        return self._query_global(as_of, symbol, start)

    def next_trading_day(self, after: date) -> date | None:
        days = self.trading_days_after(after, 1)
        return days[0] if days else None

    def trading_days_after(self, after: date, n: int) -> list[date]:
        path = self.parquet_dir / "calendar.parquet"
        if not path.exists():
            return []
        frame = self.conn.execute(
            "SELECT date FROM read_parquet(?) WHERE date > ? ORDER BY date LIMIT ?",
            [str(path), after, n],
        ).pl()
        return list(frame["date"].to_list())

    def _query_ohlcv(
        self,
        filename: str,
        as_of: date,
        symbol: str | None,
        start: date | None,
        empty: object,
    ) -> pl.DataFrame:
        path = self.parquet_dir / filename
        if not path.exists():
            return empty()  # type: ignore[operator]
        sql = "SELECT * FROM read_parquet(?) WHERE date <= ?"
        params: list[object] = [str(path), as_of]
        if start is not None:
            sql += " AND date >= ?"
            params.append(start)
        if symbol is not None:
            sql += " AND symbol = ?"
            params.append(symbol)
        sql += " ORDER BY symbol, date"
        return self.conn.execute(sql, params).pl()

    def _query_global(
        self,
        as_of: date,
        symbol: str | None,
        start: date | None,
    ) -> pl.DataFrame:
        path = self.parquet_dir / "global_bars.parquet"
        if not path.exists():
            return empty_global()
        sql = "SELECT * FROM read_parquet(?) WHERE date <= ?"
        params: list[object] = [str(path), as_of]
        if start is not None:
            sql += " AND date >= ?"
            params.append(start)
        if symbol is not None:
            sql += " AND symbol = ?"
            params.append(symbol)
        sql += " ORDER BY symbol, date"
        return self.conn.execute(sql, params).pl()

    def _read(self, filename: str, empty: object) -> pl.DataFrame:
        path = self.parquet_dir / filename
        if not path.exists():
            return empty()  # type: ignore[operator]
        return self.conn.execute("SELECT * FROM read_parquet(?)", [str(path)]).pl()
