from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.errors import DataQualityError, TushareFetchError
from app.features.engine import required_history_bars
from app.models.config import StrategyConfig
from app.models.snapshot import DataSnapshot
from app.providers.tushare_client import TushareQueryClient
from app.providers.tushare_fetch import write_normalized_tables
from app.providers.tushare_normalize import (
    TushareRaw,
    is_st_name,
    normalize_tushare,
    open_trading_days,
    parse_ymd,
    require_ts_code,
    split_session_symbols,
    ymd,
)
from app.storage.import_market import import_market_data
from app.universe.membership import bind_membership_to_tables

_STOCK_BASIC_FIELDS = "ts_code,name,industry,list_date,delist_date,market,exchange,list_status"
_ALLOWED_EXCHANGES = frozenset({"SSE", "SZSE"})
_ALLOWED_MARKETS = frozenset({"主板", "创业板", "科创板"})
_CALENDAR_LOOKBACK_DAYS = 400


@dataclass(frozen=True)
class LatestAllAShareFetchResult:
    requested_as_of: date
    as_of: date
    candidate_count: int
    snapshot: DataSnapshot


def fetch_latest_all_a_share_and_import(
    *,
    requested_as_of: date,
    config: StrategyConfig,
    dest_dir: Path,
    client: TushareQueryClient,
    source_version: str | None = None,
    replace_existing: bool = False,
) -> LatestAllAShareFetchResult:
    """Create a single-date, auditable all-A-share research snapshot.

    It intentionally keeps only the current listed common-share universe and
    enough price history to calculate features.  It does not manufacture a
    historical all-market universe and is rejected by the backtest pipeline.
    """
    _require_latest_market_config(config)
    destination = Path(dest_dir)
    if destination.exists() and any(destination.iterdir()):
        if not replace_existing:
            raise TushareFetchError(
                "destination already contains a snapshot; use a new AIQ_DATA_DIR or pass --replace-existing explicitly"
            )
        if not (destination / "manifest.json").is_file():
            raise TushareFetchError(
                "destination is non-empty but is not a recognized market snapshot; refusing to replace unrelated files"
            )

    calendar_start = requested_as_of - timedelta(days=_CALENDAR_LOOKBACK_DAYS)
    trade_cal = client.query(
        "trade_cal",
        exchange="SSE",
        start_date=ymd(calendar_start),
        end_date=ymd(requested_as_of),
        is_open="1",
    )
    all_days = open_trading_days(trade_cal, calendar_start, requested_as_of)
    required = required_history_bars(config.data.min_history_bars)
    if len(all_days) < required:
        raise TushareFetchError(
            f"trade_cal has {len(all_days)} open dates through {requested_as_of}; need {required} for warm-up"
        )
    # Keep one preceding trading day in the raw fetch. A full-day suspension
    # can start on the first feature day; its official pre_close and the prior
    # adjustment factor are then available without extending snapshot coverage.
    source_days = all_days[-(required + 1) :]
    days = source_days[-required:]
    start, as_of = days[0], days[-1]

    stock_basic = client.query("stock_basic", list_status="L", fields=_STOCK_BASIC_FIELDS)
    candidate_stocks, current_st_symbols = _select_current_a_share(
        stock_basic,
        as_of=as_of,
        min_listing_days=config.universe.min_listing_days,
    )
    daily = _query_by_day(client, "daily", source_days)
    suspend_d = _query_by_day(client, "suspend_d", source_days, suspend_type="S")
    stk_limit = _query_by_day(
        client,
        "stk_limit",
        source_days,
        fields="ts_code,trade_date,pre_close,up_limit,down_limit",
    )
    stocks = _current_tradable_stocks(
        candidate_stocks,
        daily=daily,
        suspend_d=suspend_d,
        as_of=as_of,
    )
    stocks = _exclude_unseeded_warmup_suspensions(
        stocks,
        daily=daily,
        suspend_d=suspend_d,
        stk_limit=stk_limit,
        feature_start=start,
    )
    current_st_symbols.intersection_update(stocks)
    indices, globals_ = split_session_symbols(config, stocks)
    raw = TushareRaw(
        trade_cal=trade_cal,
        stock_basic=stock_basic,
        daily=daily,
        daily_basic=_query_by_day(
            client,
            "daily_basic",
            source_days,
            fields="ts_code,trade_date,turnover_rate",
        ),
        adj_factor=_query_by_day(client, "adj_factor", source_days),
        stk_limit=stk_limit,
        suspend_d=suspend_d,
        # Historical ST state would require a per-security history query.
        # This snapshot is only eligible for its resolved as_of date, so the
        # current stock_basic name is used only for that date below.
        namechange=pl.DataFrame(),
        index_daily=_query_symbol_history(client, "index_daily", indices, start, as_of),
        index_global=_query_symbol_history(client, "index_global", globals_, start, as_of),
        current_st_symbols=current_st_symbols,
    )
    tables = bind_membership_to_tables(
        normalize_tushare(raw, config, start, as_of, stocks),
        config=config,
        membership=None,
        stocks=stocks,
    )
    snapshot = _write_and_import(
        tables,
        destination,
        config=config,
        source_version=source_version,
    )
    return LatestAllAShareFetchResult(
        requested_as_of=requested_as_of,
        as_of=as_of,
        candidate_count=len(stocks),
        snapshot=snapshot,
    )


def _require_latest_market_config(config: StrategyConfig) -> None:
    if config.research_scope != "latest_market_snapshot":
        raise TushareFetchError(
            "fetch-tushare-latest-all-a-share requires a strategy with research_scope=latest_market_snapshot"
        )
    if config.universe.mode != "manual_static":
        raise TushareFetchError("latest market snapshot must use universe.mode=manual_static")


def _select_current_a_share(
    stock_basic: pl.DataFrame,
    *,
    as_of: date,
    min_listing_days: int,
) -> tuple[list[str], set[str]]:
    required = {"ts_code", "name", "list_date", "market", "exchange", "list_status"}
    missing = sorted(required - set(stock_basic.columns))
    if stock_basic.is_empty() or missing:
        detail = f" missing columns: {missing}" if missing else ""
        raise TushareFetchError(f"stock_basic returned no usable listed securities.{detail}")

    stocks: list[str] = []
    current_st: set[str] = set()
    seen: set[str] = set()
    for item in stock_basic.to_dicts():
        code = require_ts_code(str(item.get("ts_code") or ""), kind="stock")
        if code in seen:
            raise DataQualityError(f"stock_basic has duplicate ts_code {code}")
        seen.add(code)
        exchange = str(item.get("exchange") or "").strip().upper()
        market = str(item.get("market") or "").strip()
        status = str(item.get("list_status") or "").strip().upper()
        if status != "L":
            continue
        is_b_share = (code.endswith(".SH") and code.startswith("900")) or (
            code.endswith(".SZ") and code.startswith("200")
        )
        if (
            not code.endswith((".SH", ".SZ"))
            or is_b_share
            or exchange not in _ALLOWED_EXCHANGES
            or market not in _ALLOWED_MARKETS
        ):
            continue
        if not item.get("list_date"):
            raise DataQualityError(f"stock_basic missing list_date for {code}")
        if (as_of - parse_ymd(item["list_date"])).days < min_listing_days:
            continue
        stocks.append(code)
        if is_st_name(item.get("name")):
            current_st.add(code)
    if not stocks:
        raise TushareFetchError("stock_basic has no listed SSE/SZSE common A shares in the allowed markets")
    return sorted(stocks), current_st


def _query_by_day(
    client: TushareQueryClient,
    api_name: str,
    days: list[date],
    **extra: str,
) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for day in days:
        frame = client.query(api_name, trade_date=ymd(day), **extra)
        if not frame.is_empty():
            frames.append(frame)
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def _current_tradable_stocks(
    candidates: list[str],
    *,
    daily: pl.DataFrame,
    suspend_d: pl.DataFrame,
    as_of: date,
) -> list[str]:
    """Exclude only explicitly full-day suspended securities on the score date.

    A missing daily row without a matching full-day suspension is a data-quality
    failure, not evidence that the security may be silently removed.
    """
    if daily.is_empty() or "ts_code" not in daily.columns or "trade_date" not in daily.columns:
        raise DataQualityError("daily has no rows for current all-A-share eligibility")
    daily_on_as_of = {
        str(item["ts_code"]).strip()
        for item in daily.to_dicts()
        if parse_ymd(item["trade_date"]) == as_of
    }
    suspended_on_as_of = _full_day_suspended_symbols(suspend_d, as_of)
    missing = set(candidates) - daily_on_as_of
    unknown = sorted(missing - suspended_on_as_of)
    if unknown:
        raise DataQualityError(
            f"daily is missing {len(unknown)} current listed securities on {as_of}; "
            f"first={unknown[0]}; refusing to treat missing data as a suspension"
        )
    tradable = sorted(set(candidates) & daily_on_as_of)
    if not tradable:
        raise DataQualityError(f"no current tradable securities have daily bars on {as_of}")
    return tradable


def _full_day_suspended_symbols(frame: pl.DataFrame, as_of: date) -> set[str]:
    if frame.is_empty():
        return set()
    required = {"ts_code", "trade_date", "suspend_type"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataQualityError(f"suspend_d missing columns: {missing}")
    out: set[str] = set()
    for item in frame.to_dicts():
        if parse_ymd(item["trade_date"]) != as_of:
            continue
        if str(item.get("suspend_type") or "").upper() != "S":
            continue
        if item.get("suspend_timing") not in (None, "", "None"):
            continue
        out.add(str(item["ts_code"]).strip())
    return out


def _exclude_unseeded_warmup_suspensions(
    candidates: list[str],
    *,
    daily: pl.DataFrame,
    suspend_d: pl.DataFrame,
    stk_limit: pl.DataFrame,
    feature_start: date,
) -> list[str]:
    """Exclude a current stock only when its feature window has no auditable seed.

    A full-day halt on the first 60-bar day needs either an actual daily bar or
    an official `stk_limit.pre_close` to synthesize a flat bar.  Without one,
    the stock cannot meet the declared warm-up requirement and is not ranked.
    """
    daily_on_start = {
        str(item["ts_code"]).strip()
        for item in daily.to_dicts()
        if parse_ymd(item["trade_date"]) == feature_start
    }
    suspended_on_start = _full_day_suspended_symbols(suspend_d, feature_start)
    missing = set(candidates) - daily_on_start
    unknown = sorted(missing - suspended_on_start)
    if unknown:
        raise DataQualityError(
            f"daily is missing {len(unknown)} securities at warm-up start {feature_start}; "
            f"first={unknown[0]}; refusing to treat missing data as a suspension"
        )
    pre_close: dict[str, float] = {}
    if not stk_limit.is_empty():
        required = {"ts_code", "trade_date", "pre_close"}
        missing_columns = sorted(required - set(stk_limit.columns))
        if missing_columns:
            raise DataQualityError(f"stk_limit missing columns: {missing_columns}")
        for item in stk_limit.to_dicts():
            if parse_ymd(item["trade_date"]) != feature_start:
                continue
            value = item.get("pre_close")
            if isinstance(value, int | float) and float(value) > 0:
                pre_close[str(item["ts_code"]).strip()] = float(value)
    return sorted(set(candidates) - {symbol for symbol in missing if symbol not in pre_close})


def _query_symbol_history(
    client: TushareQueryClient,
    api_name: str,
    symbols: list[str],
    start: date,
    end: date,
) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for symbol in symbols:
        frame = client.query(api_name, ts_code=symbol, start_date=ymd(start), end_date=ymd(end))
        if not frame.is_empty():
            frames.append(frame)
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def _write_and_import(
    tables: dict[str, pl.DataFrame],
    destination: Path,
    *,
    config: StrategyConfig,
    source_version: str | None,
) -> DataSnapshot:
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".tushare-latest-market-{uuid.uuid4().hex}"
    try:
        write_normalized_tables(tables, temporary)
        return import_market_data(
            temporary,
            destination,
            source_name="tushare_latest_all_a_share",
            adjustment=config.data.adjustment,
            source_version=source_version,
            market_index=config.data.market_index,
            global_symbol=config.data.global_symbol,
        )
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
