from __future__ import annotations

from collections.abc import Callable
from datetime import date
from time import monotonic, sleep
from typing import Any

import polars as pl

from app.errors import TushareFetchError, sanitize_error_message
from app.models.config import StrategyConfig
from app.models.market import Instrument
from app.providers._frames import filter_dates
from app.providers.base import MarketDataProvider
from app.providers.tushare_client import TushareQueryClient, read_tushare_token
from app.providers.tushare_normalize import (
    TushareRaw,
    format_stock_basic_status_failures,
    normalize_tushare,
    open_trading_days,
    require_ts_code,
    split_session_symbols,
    ymd,
)
from app.universe.membership import assert_membership_covers_calendar

_CODE_BATCH = 80
# Fetch selected security data one security at a time.  `daily` and
# `daily_basic` cap each response at 6,000 rows, so a multi-security request
# can truncate silently; `adj_factor` can likewise return an incomplete
# result set for a comma-separated security list.  Querying `suspend_d` by
# selected security also ensures that full-day halts correspond to the daily
# inputs.  A missing row must never be mistaken for a genuine market-data gap.
_SINGLE_CODE_APIS = frozenset(
    {"adj_factor", "daily", "daily_basic", "index_daily", "index_global", "stk_limit", "suspend_d"}
)
# A live Tushare query has a 300-requests/minute ceiling for endpoints such
# as `daily`.  Keep a material margin below it instead of making a large
# universe request fail after several minutes of work.  This is per endpoint:
# `daily`, `daily_basic`, and so on have independent request streams.
_SINGLE_CODE_REQUEST_MIN_INTERVAL_SECONDS = 0.31
_STOCK_BASIC_FIELDS = "ts_code,name,industry,list_date,delist_date,market,exchange,list_status"
# Official stock_basic list_status values. Default is L, so D/P/G must be queried separately.
_STOCK_BASIC_STATUSES = ("L", "D", "P", "G")


class TushareProvider(MarketDataProvider):
    """Pull Tushare history and normalize it to the six-table snapshot contract.

    Network happens only when fetch() runs with a live client. Importing this
    module does not open sockets or read the token.
    """

    def __init__(
        self,
        client: TushareQueryClient | None = None,
        *,
        pace_single_code_requests: bool | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._client = client
        self._tables: dict[str, pl.DataFrame] | None = None
        # `None` means "pace the official live client, but do not make
        # offline fakes sleep".  Explicit injection keeps the pacing rule
        # deterministic and unit-testable.
        self._pace_single_code_requests = pace_single_code_requests
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self._next_single_code_request_at: dict[str, float] = {}

    def __repr__(self) -> str:
        return "TushareProvider(client=<redacted>)"

    def fetch(
        self,
        start: date,
        end: date,
        *,
        config: StrategyConfig,
        stocks: list[str],
        membership: pl.DataFrame | None = None,
    ) -> dict[str, pl.DataFrame]:
        if end < start:
            raise TushareFetchError("end date must be on or after start date")
        stock_codes = [require_ts_code(code, kind="stock") for code in stocks]
        if not stock_codes:
            raise TushareFetchError("stock universe is empty")
        indices, globals_ = split_session_symbols(config, stock_codes)
        raw = self._pull(
            start,
            end,
            stock_codes,
            indices,
            globals_,
            membership=membership,
            universe_id=config.universe.id,
            expected_constituents=config.universe.expected_constituents,
        )
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
        membership: pl.DataFrame | None = None,
        universe_id: str | None = None,
        expected_constituents: int | None = None,
    ) -> TushareRaw:
        client = self._client_or_live()
        start_s, end_s = ymd(start), ymd(end)
        trade_cal = client.query("trade_cal", exchange="SSE", start_date=start_s, end_date=end_s, is_open="1")
        if membership is not None:
            assert_membership_covers_calendar(
                membership,
                open_trading_days(trade_cal, start, end),
                universe_id=universe_id,
                expected_constituents=expected_constituents,
            )
        stock_basic, status_errors = self._query_stock_basic(client)
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
        # Tushare stk_limit accepts one ts_code per request. Request pre_close explicitly because
        # the provider's default payload omits it, and we must not guess price_limit_pct.
        stk_limit = self._query_codes(
            client,
            "stk_limit",
            stocks,
            start_s,
            end_s,
            extra={"fields": "ts_code,trade_date,pre_close,up_limit,down_limit"},
        )
        suspend_d = self._query_codes(
            client,
            "suspend_d",
            stocks,
            start_s,
            end_s,
            extra={"suspend_type": "S"},
        )
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
            stock_basic_status_errors=status_errors,
        )

    def _query_stock_basic(self, client: TushareQueryClient) -> tuple[pl.DataFrame, dict[str, str]]:
        frames: list[pl.DataFrame] = []
        failed: dict[str, str] = {}
        for status in _STOCK_BASIC_STATUSES:
            try:
                frame = client.query("stock_basic", list_status=status, fields=_STOCK_BASIC_FIELDS)
            except Exception as exc:  # noqa: BLE001
                failed[status] = sanitize_error_message(exc)
                continue
            if not frame.is_empty():
                frames.append(frame)
        if not frames:
            detail = format_stock_basic_status_failures(failed)
            suffix = f"; {detail}" if detail else ""
            raise TushareFetchError(f"stock_basic returned no rows{suffix}")
        return pl.concat(frames, how="diagonal_relaxed"), failed

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
        batch = 1 if api_name in _SINGLE_CODE_APIS else _CODE_BATCH
        for offset in range(0, len(codes), batch):
            chunk = codes[offset : offset + batch]
            params: dict[str, Any] = {"ts_code": ",".join(chunk)}
            if start_s is not None:
                params["start_date"] = start_s
            if end_s is not None:
                params["end_date"] = end_s
            if extra:
                params.update(extra)
            if batch == 1:
                self._pace_single_code_request(client, api_name)
            frames.append(client.query(api_name, **params))
        nonempty = [frame for frame in frames if not frame.is_empty()]
        if not nonempty:
            return frames[0] if frames else pl.DataFrame()
        return pl.concat(nonempty, how="diagonal_relaxed")

    def _pace_single_code_request(self, client: TushareQueryClient, api_name: str) -> None:
        if self._pace_single_code_requests is None:
            enabled = bool(getattr(client, "requires_single_code_rate_limit", False))
        else:
            enabled = self._pace_single_code_requests
        if not enabled:
            return
        next_request_at = self._next_single_code_request_at.get(api_name)
        if next_request_at is not None:
            delay = next_request_at - self._monotonic_clock()
            if delay > 0:
                self._sleeper(delay)
        self._next_single_code_request_at[api_name] = (
            self._monotonic_clock() + _SINGLE_CODE_REQUEST_MIN_INTERVAL_SECONDS
        )
