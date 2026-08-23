from __future__ import annotations

from datetime import date
from typing import Any

import polars as pl

from app.errors import TushareFetchError
from app.models.config import StrategyConfig
from app.models.market import Instrument
from app.providers._frames import filter_dates
from app.providers.base import MarketDataProvider
from app.providers.tushare_client import TushareQueryClient, read_tushare_token
from app.providers.tushare_normalize import (
    TushareRaw,
    normalize_tushare,
    require_ts_code,
    split_session_symbols,
    ymd,
)

_CODE_BATCH = 80
_STOCK_BASIC_FIELDS = "ts_code,name,industry,list_date,delist_date,market,exchange,list_status"
# Official stock_basic list_status values. Default is L, so D/P/G must be queried separately.
_STOCK_BASIC_STATUSES = ("L", "D", "P", "G")


class TushareProvider(MarketDataProvider):
    """Pull Tushare history and normalize it to the existing five-table contract.

    Network happens only when fetch() runs with a live client. Importing this
    module does not open sockets or read the token.
    """

    def __init__(self, client: TushareQueryClient | None = None) -> None:
        self._client = client
        self._tables: dict[str, pl.DataFrame] | None = None

    def __repr__(self) -> str:
        return "TushareProvider(client=<redacted>)"

    def fetch(
        self,
        start: date,
        end: date,
        *,
        config: StrategyConfig,
        stocks: list[str],
    ) -> dict[str, pl.DataFrame]:
        if end < start:
            raise TushareFetchError("end date must be on or after start date")
        stock_codes = [require_ts_code(code, kind="stock") for code in stocks]
        if not stock_codes:
            raise TushareFetchError("stock universe is empty")
        indices, globals_ = split_session_symbols(config, stock_codes)
        raw = self._pull(start, end, stock_codes, indices, globals_)
        tables = normalize_tushare(raw, config, start, end, stock_codes)
        self._tables = tables
        return tables

    def get_instruments(self) -> list[Instrument]:
        frame = self._require_tables()["instruments"]
        return [Instrument.model_validate(row) for row in frame.to_dicts()]

    def get_calendar(self, start: date, end: date) -> list[date]:
        days = self._require_tables()["calendar"]["date"].to_list()
        return [d for d in days if start <= d <= end]

    def get_daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        return filter_dates(self._require_tables()["daily_bars"], start, end, symbol)

    def get_all_daily_bars(self, start: date | None = None, end: date | None = None) -> pl.DataFrame:
        return filter_dates(self._require_tables()["daily_bars"], start, end)

    def get_index_bars(
        self,
        symbol: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        return filter_dates(self._require_tables()["index_bars"], start, end, symbol)

    def get_global_bars(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> pl.DataFrame:
        return filter_dates(self._require_tables()["global_bars"], start, end)

    def _require_tables(self) -> dict[str, pl.DataFrame]:
        if self._tables is None:
            raise TushareFetchError("TushareProvider.fetch() has not been called")
        return self._tables

    def _client_or_live(self) -> TushareQueryClient:
        if self._client is None:
            from app.providers.tushare_client import LiveTushareClient

            self._client = LiveTushareClient(read_tushare_token())
        return self._client

    def _pull(
        self,
        start: date,
        end: date,
        stocks: list[str],
        indices: list[str],
        globals_: list[str],
    ) -> TushareRaw:
        client = self._client_or_live()
        start_s, end_s = ymd(start), ymd(end)
        trade_cal = client.query("trade_cal", exchange="SSE", start_date=start_s, end_date=end_s, is_open="1")
        stock_basic = self._query_stock_basic(client)
        daily = self._query_codes(client, "daily", stocks, start_s, end_s)
        daily_basic = self._query_codes(
            client,
            "daily_basic",
            stocks,
            start_s,
            end_s,
            extra={"fields": "ts_code,trade_date,turnover_rate"},
        )
        adj_factor = self._query_codes(client, "adj_factor", stocks, start_s, end_s)
        stk_limit = self._query_codes(client, "stk_limit", stocks, start_s, end_s)
        suspend_d = client.query("suspend_d", start_date=start_s, end_date=end_s, suspend_type="S")
        namechange = self._query_codes(client, "namechange", stocks, None, None)
        index_daily = self._query_codes(client, "index_daily", indices, start_s, end_s)
        index_global = self._query_codes(client, "index_global", globals_, start_s, end_s)
        return TushareRaw(
            trade_cal=trade_cal,
            stock_basic=stock_basic,
            daily=daily,
            daily_basic=daily_basic,
            adj_factor=adj_factor,
            stk_limit=stk_limit,
            suspend_d=suspend_d,
            namechange=namechange,
            index_daily=index_daily,
            index_global=index_global,
        )

    def _query_stock_basic(self, client: TushareQueryClient) -> pl.DataFrame:
        frames: list[pl.DataFrame] = []
        for status in _STOCK_BASIC_STATUSES:
            frames.append(client.query("stock_basic", list_status=status, fields=_STOCK_BASIC_FIELDS))
        nonempty = [frame for frame in frames if not frame.is_empty()]
        if not nonempty:
            raise TushareFetchError("stock_basic returned no rows")
        return pl.concat(nonempty, how="diagonal_relaxed")

    def _query_codes(
        self,
        client: TushareQueryClient,
        api_name: str,
        codes: list[str],
        start_s: str | None,
        end_s: str | None,
        extra: dict[str, Any] | None = None,
    ) -> pl.DataFrame:
        if not codes:
            return pl.DataFrame()
        frames: list[pl.DataFrame] = []
        for offset in range(0, len(codes), _CODE_BATCH):
            chunk = codes[offset : offset + _CODE_BATCH]
            params: dict[str, Any] = {"ts_code": ",".join(chunk)}
            if start_s is not None:
                params["start_date"] = start_s
            if end_s is not None:
                params["end_date"] = end_s
            if extra:
                params.update(extra)
            frames.append(client.query(api_name, **params))
        nonempty = [frame for frame in frames if not frame.is_empty()]
        if not nonempty:
            return frames[0] if frames else pl.DataFrame()
        return pl.concat(nonempty, how="diagonal_relaxed")
