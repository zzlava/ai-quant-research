from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.backtest.costs import apply_slippage, commission, stamp_tax
from app.backtest.limits import is_open_at_limit
from app.models.config import StrategyConfig
from app.models.scores import ScoreResult
from app.scoring.engine import ScoringEngine
from app.storage.protocol import MarketStore

FactorName = Literal["final_score", "alpha_score"]
ScoreFn = Callable[[date], list[ScoreResult]]


class PortfolioSignalSummary(BaseModel):
    horizon_days: int
    factor: FactorName
    scoring_days: int
    average_labeled_names: float | None
    mean_top_k_gross_return: float | None
    mean_top_k_estimated_net_return: float | None
    top_k_net_t_stat: float | None
    top_k_gross_positive_day_rate: float | None
    top_k_net_positive_day_rate: float | None
    mean_universe_gross_return: float | None
    mean_top_quantile_gross_return: float | None
    mean_bottom_quantile_gross_return: float | None
    mean_top_minus_universe_return: float | None
    mean_long_short_spread: float | None
    mean_top_k_turnover: float | None


class PortfolioSignalReport(BaseModel):
    strategy_config_hash: str
    data_snapshot_id: str
    start: date
    end: date
    factor: FactorName
    horizons: list[int]
    top_k: int
    quantiles: int
    entry_rule: str = "next_trading_day_adjusted_open"
    exit_rule: str = "entry_plus_horizon_trading_days_adjusted_close"
    cost_model: str = "declared_slippage_commission_minimum_and_dated_stamp_tax_estimate"
    summaries: list[PortfolioSignalSummary] = Field(default_factory=list)


def analyze_portfolio_signal(
    *,
    store: MarketStore,
    config: StrategyConfig,
    start: date,
    end: date,
    horizons: list[int],
    factor: FactorName = "final_score",
    top_k: int = 3,
    quantiles: int = 10,
    score_fn: ScoreFn | None = None,
) -> PortfolioSignalReport:
    """Diagnose whether a cross-sectional score survives entry timing and costs.

    Scores are generated strictly on the decision day. Labels begin at the
    next trading day's adjusted open. Future prices are research labels only.
    The cost estimate uses a standalone equal-weight allocation and is not a
    substitute for the path-dependent backtest.
    """
    normalized_horizons = sorted(set(horizons))
    if not normalized_horizons or normalized_horizons[0] <= 0:
        raise ValueError("horizons must contain positive trading-day counts")
    if end < start:
        raise ValueError("end must be on or after start")
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if quantiles < 2:
        raise ValueError("quantiles must be at least 2")

    snapshot = store.snapshot()
    if snapshot.coverage_end is None:
        raise ValueError("snapshot coverage_end is required for portfolio labels")
    calendar = store.get_calendar(start, snapshot.coverage_end)
    decision_days = [day for day in calendar if start <= day <= end]
    if not decision_days:
        raise ValueError("requested portfolio-signal window contains no trading days")
    day_index = {day: idx for idx, day in enumerate(calendar)}
    bars = _bar_map(store, start, snapshot.coverage_end)
    run_scores = score_fn or ScoringEngine(store, config).run
    allocation = config.portfolio.initial_cash / config.portfolio.max_positions

    daily: dict[int, list[dict[str, float]]] = {horizon: [] for horizon in normalized_horizons}
    previous_top: dict[int, set[str] | None] = {horizon: None for horizon in normalized_horizons}

    for decision_day in decision_days:
        idx = day_index[decision_day]
        results = run_scores(decision_day)
        for horizon in normalized_horizons:
            entry_idx = idx + 1
            exit_idx = entry_idx + horizon
            if exit_idx >= len(calendar):
                continue
            entry_day = calendar[entry_idx]
            exit_day = calendar[exit_idx]
            labeled: list[tuple[str, float, float, float]] = []
            for result in results:
                entry_bar = bars.get((result.symbol, entry_day))
                exit_bar = bars.get((result.symbol, exit_day))
                if entry_bar is None or exit_bar is None:
                    continue
                if bool(entry_bar.get("is_suspended")) or bool(exit_bar.get("is_suspended")):
                    continue
                prev_close = _optional_float(entry_bar.get("pre_close"))
                if is_open_at_limit(entry_bar, prev_close, config.trade, "up"):
                    continue
                entry_price = _optional_float(entry_bar.get("adj_open"))
                exit_price = _optional_float(exit_bar.get("adj_close"))
                if entry_price is None or exit_price is None or entry_price <= 0 or exit_price <= 0:
                    continue
                gross = exit_price / entry_price - 1.0
                net = _estimated_net_return(gross, allocation, config, exit_day)
                score = result.final_score if factor == "final_score" else result.breakdown.alpha_score
                labeled.append((result.symbol, score, gross, net))
            if len(labeled) < max(top_k, quantiles):
                continue
            labeled.sort(key=lambda item: (-item[1], item[0]))
            selected = labeled[:top_k]
            bucket_size = max(1, len(labeled) // quantiles)
            top_bucket = labeled[:bucket_size]
            bottom_bucket = labeled[-bucket_size:]
            top_symbols = {item[0] for item in selected}
            prior = previous_top[horizon]
            turnover = None if prior is None else 1.0 - len(top_symbols & prior) / top_k
            previous_top[horizon] = top_symbols
            daily[horizon].append(
                {
                    "names": float(len(labeled)),
                    "top_gross": _mean([item[2] for item in selected]),
                    "top_net": _mean([item[3] for item in selected]),
                    "universe": _mean([item[2] for item in labeled]),
                    "top_quantile": _mean([item[2] for item in top_bucket]),
                    "bottom_quantile": _mean([item[2] for item in bottom_bucket]),
                    "turnover": math.nan if turnover is None else turnover,
                }
            )

    summaries = [
        _summarize(horizon, factor, daily[horizon]) for horizon in normalized_horizons
    ]
    return PortfolioSignalReport(
        strategy_config_hash=config.config_hash(),
        data_snapshot_id=snapshot.snapshot_id,
        start=start,
        end=end,
        factor=factor,
        horizons=normalized_horizons,
        top_k=top_k,
        quantiles=quantiles,
        summaries=summaries,
    )


def write_portfolio_signal_report(report: PortfolioSignalReport, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")


def _bar_map(store: MarketStore, start: date, end: date) -> dict[tuple[str, date], dict[str, object]]:
    daily = store.get_daily_bars(as_of=end, start=start)
    required = {"symbol", "date", "adj_open", "adj_close", "is_suspended"}
    missing = sorted(required - set(daily.columns))
    if missing:
        raise ValueError(f"daily_bars is missing portfolio-label columns: {missing}")
    return {
        (str(row["symbol"]), row["date"]): row
        for row in daily.to_dicts()
        if isinstance(row.get("date"), date)
    }


def _estimated_net_return(
    gross_return: float,
    allocation: float,
    config: StrategyConfig,
    exit_day: date,
) -> float:
    raw_entry_notional = allocation
    buy_fill_notional = apply_slippage(raw_entry_notional, config.costs, "buy")
    total_buy = buy_fill_notional + commission(buy_fill_notional, config.costs)
    raw_exit_notional = raw_entry_notional * (1.0 + gross_return)
    sell_fill_notional = apply_slippage(raw_exit_notional, config.costs, "sell")
    net_sell = (
        sell_fill_notional
        - commission(sell_fill_notional, config.costs)
        - stamp_tax(sell_fill_notional, config.costs, exit_day)
    )
    return net_sell / total_buy - 1.0


def _summarize(
    horizon: int,
    factor: FactorName,
    rows: list[dict[str, float]],
) -> PortfolioSignalSummary:
    if not rows:
        return PortfolioSignalSummary(
            horizon_days=horizon,
            factor=factor,
            scoring_days=0,
            average_labeled_names=None,
            mean_top_k_gross_return=None,
            mean_top_k_estimated_net_return=None,
            top_k_net_t_stat=None,
            top_k_gross_positive_day_rate=None,
            top_k_net_positive_day_rate=None,
            mean_universe_gross_return=None,
            mean_top_quantile_gross_return=None,
            mean_bottom_quantile_gross_return=None,
            mean_top_minus_universe_return=None,
            mean_long_short_spread=None,
            mean_top_k_turnover=None,
        )
    top_gross = [row["top_gross"] for row in rows]
    top_net = [row["top_net"] for row in rows]
    universe = [row["universe"] for row in rows]
    top_quantile = [row["top_quantile"] for row in rows]
    bottom_quantile = [row["bottom_quantile"] for row in rows]
    turnover = [row["turnover"] for row in rows if math.isfinite(row["turnover"])]
    return PortfolioSignalSummary(
        horizon_days=horizon,
        factor=factor,
        scoring_days=len(rows),
        average_labeled_names=_mean([row["names"] for row in rows]),
        mean_top_k_gross_return=_mean(top_gross),
        mean_top_k_estimated_net_return=_mean(top_net),
        top_k_net_t_stat=_t_stat(top_net),
        top_k_gross_positive_day_rate=_positive_rate(top_gross),
        top_k_net_positive_day_rate=_positive_rate(top_net),
        mean_universe_gross_return=_mean(universe),
        mean_top_quantile_gross_return=_mean(top_quantile),
        mean_bottom_quantile_gross_return=_mean(bottom_quantile),
        mean_top_minus_universe_return=_mean(
            [top - broad for top, broad in zip(top_quantile, universe, strict=True)]
        ),
        mean_long_short_spread=_mean(
            [top - bottom for top, bottom in zip(top_quantile, bottom_quantile, strict=True)]
        ),
        mean_top_k_turnover=_mean(turnover) if turnover else None,
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _positive_rate(values: list[float]) -> float:
    return sum(value > 0 for value in values) / len(values)


def _t_stat(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = _mean(values)
    std = math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))
    return mean / (std / math.sqrt(len(values))) if std > 0 else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
