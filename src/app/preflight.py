from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from app.clock import decision_at_utc
from app.errors import DataQualityError, PreflightError
from app.features.engine import required_history_bars
from app.models.config import StrategyConfig
from app.models.snapshot import RAW_PLUS_ADJUSTED_PRICE_BASIS
from app.research_scope import (
    HISTORICAL_ALL_A_SHARE_SCOPE,
    PUBLIC_RECONSTRUCTION_SCOPE,
    research_notice,
)
from app.storage.protocol import MarketStore
from app.universe.membership import membership_lookup_options

MANUAL_STATIC_MODE_LABEL = "受控样本，非全市场/指数研究"
CONTROLLED_SAMPLE_MODE_LABEL = "受控历史成员样本，非完整指数研究"
HISTORICAL_MODE_LABEL = "历史指数研究（数据通过点时校验）"
LATEST_MARKET_SNAPSHOT_MODE_LABEL = "当日沪深全市场快照研究，仅可做当日排行，禁止历史回测"
PUBLIC_RECONSTRUCTION_MODE_LABEL = "公开重建 CSI300 说明性模拟，非严格 PIT，不能与正式回测比较"
HISTORICAL_ALL_A_SHARE_MODE_LABEL = "历史沪深普通 A 股点时派生流动性股票池研究"
SECTOR_DISABLED_LABEL = "行业因子未启用"


@dataclass(frozen=True)
class PreflightResult:
    universe_id: str
    universe_mode: str
    research_mode: str
    sector_status: str | None
    signal_ready_start: date
    coverage_start: date
    coverage_end: date
    snapshot_id: str
    min_history_bars: int
    required_history_bars: int
    trading_days: int
    research_notice: str | None = None


def research_mode_label(mode: str, research_scope: str) -> str:
    if research_scope == "latest_market_snapshot":
        return LATEST_MARKET_SNAPSHOT_MODE_LABEL
    if research_scope == "controlled_sample":
        return CONTROLLED_SAMPLE_MODE_LABEL
    if research_scope == PUBLIC_RECONSTRUCTION_SCOPE:
        return PUBLIC_RECONSTRUCTION_MODE_LABEL
    if research_scope == HISTORICAL_ALL_A_SHARE_SCOPE:
        return HISTORICAL_ALL_A_SHARE_MODE_LABEL
    if mode == "historical_membership":
        return HISTORICAL_MODE_LABEL
    return MANUAL_STATIC_MODE_LABEL


def _require_coverage(store: MarketStore, start: date, end: date) -> tuple[date, date]:
    if end < start:
        raise PreflightError("end date must be on or after start date")
    snap = store.snapshot()
    coverage_start = snap.coverage_start
    coverage_end = snap.coverage_end
    if coverage_start is None or coverage_end is None:
        raise PreflightError("snapshot coverage is missing; refusing to infer a research window")
    if start < coverage_start or end > coverage_end:
        raise PreflightError(
            f"request window {start.isoformat()}..{end.isoformat()} "
            f"is outside snapshot coverage {coverage_start.isoformat()}..{coverage_end.isoformat()}"
        )
    return coverage_start, coverage_end


def _require_price_contract(store: MarketStore, config: StrategyConfig) -> None:
    snap = store.snapshot()
    if snap.price_basis != RAW_PLUS_ADJUSTED_PRICE_BASIS:
        raise PreflightError(
            "snapshot price basis is not raw_ohlc_plus_adjusted_features; "
            "refusing to use adjusted prices for execution. Re-fetch or re-import the data"
        )
    if snap.adjustment != config.data.adjustment:
        raise PreflightError(
            f"snapshot adjustment={snap.adjustment} does not match strategy adjustment={config.data.adjustment}; "
            "re-fetch the snapshot with the matching price-transform contract"
        )


def _require_point_in_time_adjustment(config: StrategyConfig) -> None:
    if config.data.require_point_in_time_adjustment and config.data.adjustment == "forward":
        raise PreflightError(
            "this strategy requires point-in-time adjusted features; forward adjustment is collection-end anchored. "
            "Use backward adjustment with the raw OHLC plus adjustment-factor snapshot contract"
        )


def _calendar_dates(frame_dates: Iterable[object]) -> list[date]:
    days = [day for day in frame_dates if isinstance(day, date)]
    return sorted(set(days))


def _has_ending_history(bar_dates: set[date], calendar: list[date], as_of: date, needed: int) -> bool:
    prefix = [day for day in calendar if day <= as_of]
    if len(prefix) < needed:
        return False
    return all(day in bar_dates for day in prefix[-needed:])


def _global_ready(
    pairs: list[tuple[date, datetime]],
    as_of: date,
    cutoff: datetime,
    needed: int,
) -> bool:
    """Match FeatureEngine: count available closes, do not require CN-calendar continuity."""
    available = {day for day, stamp in pairs if day <= as_of and stamp <= cutoff}
    return len(available) >= needed


def _members_on(store: MarketStore, config: StrategyConfig, as_of: date) -> set[str]:
    lookup = membership_lookup_options(config.universe)
    expected = lookup["expected_constituents"]
    return store.get_universe_members(
        config.universe.id,
        as_of,
        decision_at_utc(as_of, config.data),
        expected_constituents=expected if isinstance(expected, int) else None,
        require_available_cross_section=bool(lookup["require_available_cross_section"]),
    )


def _listed_members(store: MarketStore, members: set[str], as_of: date) -> set[str]:
    listing = {
        inst.symbol: inst.listing_date
        for inst in store.get_instruments()
        if not inst.is_index and not inst.is_global
    }
    checked: set[str] = set()
    for symbol in members:
        listed = listing.get(symbol)
        if listed is not None and listed > as_of:
            continue
        checked.add(symbol)
    return checked


def _day_ready_reason(
    *,
    as_of: date,
    calendar: list[date],
    config: StrategyConfig,
    store: MarketStore,
    stock_dates: dict[str, set[date]],
    index_dates: set[date],
    global_pairs: list[tuple[date, datetime]],
    members: set[str],
) -> str | None:
    bench_needed = config.data.min_history_bars
    stock_needed = required_history_bars(config.data.min_history_bars)
    if not _has_ending_history(index_dates, calendar, as_of, bench_needed):
        return (
            f"market index '{config.data.market_index}' needs {bench_needed} "
            f"consecutive bars as of {as_of.isoformat()}"
        )
    cutoff = decision_at_utc(as_of, config.data)
    if not _global_ready(global_pairs, as_of, cutoff, bench_needed):
        return (
            f"global series '{config.data.global_symbol}' needs {bench_needed} "
            f"available bars as of {as_of.isoformat()}"
        )
    tradable = _listed_members(store, members, as_of)
    if not tradable:
        return f"universe {config.universe.id} has no available members on {as_of.isoformat()}"
    for symbol in sorted(tradable):
        if not _has_ending_history(stock_dates.get(symbol, set()), calendar, as_of, stock_needed):
            return (
                f"stock '{symbol}' needs {stock_needed} consecutive bars "
                f"as of {as_of.isoformat()}"
            )
    return None


def _first_ready_date(
    *,
    calendar: list[date],
    config: StrategyConfig,
    store: MarketStore,
    stock_dates: dict[str, set[date]],
    index_dates: set[date],
    global_pairs: list[tuple[date, datetime]],
) -> tuple[date, str | None]:
    last_reason = (
        f"no date has {required_history_bars(config.data.min_history_bars)} consecutive available bars"
    )
    for day in calendar:
        try:
            members = _members_on(store, config, day)
        except DataQualityError as exc:
            last_reason = str(exc)
            continue
        reason = _day_ready_reason(
            as_of=day,
            calendar=calendar,
            config=config,
            store=store,
            stock_dates=stock_dates,
            index_dates=index_dates,
            global_pairs=global_pairs,
            members=members,
        )
        if reason is None:
            return day, None
        last_reason = reason
    raise PreflightError(
        f"cannot determine signal_ready_start; {last_reason}. "
        "Dates before warm-up cannot be treated as zero-signal"
    )


def preflight_research(
    *,
    store: MarketStore,
    config: StrategyConfig,
    start: date,
    end: date,
) -> PreflightResult:
    if config.research_scope == PUBLIC_RECONSTRUCTION_SCOPE and not hasattr(store, "public_reconstruction_id"):
        raise PreflightError(
            "public_reconstruction requires a verified public reconstruction overlay; "
            "set AIQ_PUBLIC_RECONSTRUCTION_DIR and use the public reconstruction strategy"
        )
    if config.research_scope == "latest_market_snapshot" and start != end:
        raise PreflightError(
            "latest_market_snapshot supports one as-of date only; historical windows and backtests are disabled"
        )
    _require_price_contract(store, config)
    _require_point_in_time_adjustment(config)
    coverage_start, coverage_end = _require_coverage(store, start, end)
    calendar = store.get_calendar(coverage_start, coverage_end)
    if not calendar:
        raise PreflightError("snapshot calendar has no trading days")
    window = store.get_calendar(start, end)
    if not window:
        raise PreflightError(
            f"request window {start.isoformat()}..{end.isoformat()} contains no trade-calendar dates"
        )

    bench_needed = config.data.min_history_bars
    stock_needed = required_history_bars(bench_needed)
    daily = store.get_daily_bars(as_of=coverage_end)
    index = store.get_index_bars(as_of=coverage_end, symbol=config.data.market_index)
    glob = store.get_global_bars(as_of=coverage_end, symbol=config.data.global_symbol)
    stock_dates: dict[str, set[date]] = {}
    if not daily.is_empty() and "symbol" in daily.columns:
        for row in daily.select(["symbol", "date"]).iter_rows(named=True):
            day = row["date"]
            if isinstance(day, date):
                stock_dates.setdefault(str(row["symbol"]), set()).add(day)
    index_dates = set(_calendar_dates(index["date"].to_list() if not index.is_empty() else []))
    global_pairs: list[tuple[date, datetime]] = []
    if not glob.is_empty() and "available_at" in glob.columns:
        for row in glob.select(["date", "available_at"]).iter_rows(named=True):
            day = row["date"]
            stamp = row["available_at"]
            if isinstance(day, date) and isinstance(stamp, datetime):
                global_pairs.append((day, stamp))

    ready, _ = _first_ready_date(
        calendar=calendar,
        config=config,
        store=store,
        stock_dates=stock_dates,
        index_dates=index_dates,
        global_pairs=global_pairs,
    )
    if start < ready:
        raise PreflightError(
            f"request start {start.isoformat()} is before signal_ready_start {ready.isoformat()}; "
            f"need {stock_needed} consecutive member bars and {bench_needed} "
            f"index/global bars (market index '{config.data.market_index}', "
            f"global '{config.data.global_symbol}'). "
            "Dates before warm-up cannot be treated as zero-signal"
        )

    for day in window:
        try:
            members = _members_on(store, config, day)
        except DataQualityError as exc:
            raise PreflightError(str(exc)) from exc
        reason = _day_ready_reason(
            as_of=day,
            calendar=calendar,
            config=config,
            store=store,
            stock_dates=stock_dates,
            index_dates=index_dates,
            global_pairs=global_pairs,
            members=members,
        )
        if reason is not None:
            raise PreflightError(
                f"{reason}; refusing to score a gap or treat it as zero-signal"
            )

    if config.fundamental is not None:
        if not hasattr(store, "fundamental_snapshot_id"):
            raise PreflightError("fundamental strategy requires a verified fundamental overlay")
        from app.features.engine import FeatureEngine

        for day in dict.fromkeys((window[0], window[-1])):
            try:
                feature_count = len(FeatureEngine(store, config).compute_all(day))
            except ValueError as exc:
                raise PreflightError(str(exc)) from exc
            if feature_count == 0:
                raise PreflightError(
                    f"fundamental overlay has no complete PIT quality/value cross-section on {day}; "
                    "refusing to treat missing fundamentals as zero-signal"
                )

    sector_weight = (
        config.ranking.sector_weight
        if config.ranking is not None
        else config.weights.sector_score
        if config.weights
        else 0.0
    )
    sector_status = SECTOR_DISABLED_LABEL if sector_weight == 0 else None
    snap = store.snapshot()
    return PreflightResult(
        universe_id=config.universe.id,
        universe_mode=config.universe.mode,
        research_mode=research_mode_label(config.universe.mode, config.research_scope),
        sector_status=sector_status,
        signal_ready_start=ready,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        snapshot_id=snap.snapshot_id,
        min_history_bars=bench_needed,
        required_history_bars=stock_needed,
        trading_days=len(window),
        research_notice=research_notice(config.research_scope),
    )
