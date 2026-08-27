from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from app.models.config import StrategyConfig
from app.research.quantile_portfolios import (
    SPREAD_DEFINITION,
    QuantileDayObservation,
    QuantileFactorSummary,
    QuantilePeriodSummary,
    quantile_day_observation,
    summarize_quantile_observations,
    validate_quantile_count,
)
from app.scoring.engine import ScoringEngine
from app.storage.protocol import MarketStore


class ICFactorSummary(BaseModel):
    horizon_days: int
    factor: str
    observations: int
    scoring_days: int
    mean_spearman_ic: float | None
    std_spearman_ic: float | None
    t_stat: float | None
    icir: float | None = None
    hac_t_stat: float | None = None
    hac_lag: int | None = None


class ICPeriodSummary(BaseModel):
    label: str
    start: date
    end: date
    summaries: list[ICFactorSummary] = Field(default_factory=list)


class ICReport(BaseModel):
    strategy_config_hash: str
    data_snapshot_id: str
    start: date
    end: date
    horizons: list[int]
    decision_schedule: str = "all_trading_days"
    diagnostic_only: bool = True
    tradable_long_short: bool = False
    ready_for_scoring: bool = False
    ready_for_trading: bool = False
    quantile_count: int = 5
    spread_definition: str = SPREAD_DEFINITION
    skipped_no_label_dates: dict[int, int]
    summaries: list[ICFactorSummary] = Field(default_factory=list)
    annual_periods: list[ICPeriodSummary] = Field(default_factory=list)
    rolling_periods: list[ICPeriodSummary] = Field(default_factory=list)
    quantile_summaries: list[QuantileFactorSummary] = Field(default_factory=list)
    annual_quantile_periods: list[QuantilePeriodSummary] = Field(default_factory=list)
    rolling_quantile_periods: list[QuantilePeriodSummary] = Field(default_factory=list)


_TECHNICAL_FACTORS = (
    "final_score",
    "alpha_score",
    "stock_relative_strength",
    "ma20_distance",
    "ma60_distance",
    "ret_20d",
    "ret_5d",
    "ret_1d",
    "crowding_risk",
    "execution_risk",
    "attention_risk",
)
_FUNDAMENTAL_FACTORS = ("quality_score", "improvement_score", "value_score")
_BALANCED_FACTORS = ("momentum_score", "size_score", "institutional_score")


def analyze_ic(
    *,
    store: MarketStore,
    config: StrategyConfig,
    start: date,
    end: date,
    horizons: list[int],
    rolling_window_days: int = 0,
    rolling_step_days: int = 0,
    scheduled_only: bool = False,
    quantiles: int = 5,
    progress: Callable[[int, int, date], None] | None = None,
) -> ICReport:
    """Measure same-day factor ranks against later adjusted-close returns.

    Features and ranks are generated strictly as of each decision date.  The
    later close is a research label only; it is never supplied to the scorer
    or execution engine.  Missing future prices are counted and excluded from
    that day-factor observation rather than converted to zero.

    Quantile long/short spreads use the same as-of factors and forward-return
    labels. They diagnose factor separation only; A-share short legs are not
    treated as tradable.
    """
    quantile_count = validate_quantile_count(quantiles)
    normalized_horizons = sorted(set(horizons))
    if not normalized_horizons or normalized_horizons[0] <= 0:
        raise ValueError("horizons must contain positive trading-day counts")
    if end < start:
        raise ValueError("end must be on or after start")
    if rolling_window_days < 0 or rolling_step_days < 0:
        raise ValueError("rolling window and step must be non-negative")
    if (rolling_window_days == 0) != (rolling_step_days == 0):
        raise ValueError("rolling window and step must be configured together")
    if rolling_window_days > 0 and rolling_step_days > rolling_window_days:
        raise ValueError("rolling step cannot exceed rolling window")

    snapshot = store.snapshot()
    if snapshot.coverage_end is None:
        raise ValueError("snapshot coverage_end is required for IC labels")
    calendar = store.get_calendar(start, snapshot.coverage_end)
    decision_days = [day for day in calendar if start <= day <= end]
    if scheduled_only:
        decision_days = _strategy_signal_days(
            store=store,
            config=config,
            start=start,
            end=end,
        )
    if not decision_days:
        raise ValueError("requested IC window contains no trading days")
    day_index = {day: idx for idx, day in enumerate(calendar)}
    prices = _adjusted_close_map(store, snapshot.coverage_end, start)
    factors = (
        (*_TECHNICAL_FACTORS, *_FUNDAMENTAL_FACTORS)
        if config.fundamental is not None
        else _TECHNICAL_FACTORS
    )
    if config.balanced_ranking is not None:
        factors = (*factors, *_BALANCED_FACTORS)
    values: dict[tuple[int, str], list[tuple[date, float]]] = {
        (horizon, factor): []
        for horizon in normalized_horizons
        for factor in factors
    }
    quantile_values: dict[tuple[int, str], list[QuantileDayObservation]] = {
        (horizon, factor): []
        for horizon in normalized_horizons
        for factor in factors
    }
    quantile_skip_days: dict[tuple[int, str], list[date]] = {
        (horizon, factor): []
        for horizon in normalized_horizons
        for factor in factors
    }
    skipped = {horizon: 0 for horizon in normalized_horizons}
    engine = ScoringEngine(store, config)
    signal_interval_days = config.trade.signal_interval_days

    for progress_index, decision_day in enumerate(decision_days, start=1):
        idx = day_index[decision_day]
        results = engine.run(decision_day)
        for horizon in normalized_horizons:
            target_idx = idx + horizon
            if target_idx >= len(calendar):
                skipped[horizon] += 1
                continue
            target_day = calendar[target_idx]
            factor_rows: dict[str, list[tuple[float, float]]] = {factor: [] for factor in factors}
            for result in results:
                feature = result.feature
                if feature is None:
                    continue
                entry = prices.get((result.symbol, decision_day))
                future = prices.get((result.symbol, target_day))
                if (
                    entry is None
                    or future is None
                    or not isinstance(entry, int | float)
                    or not isinstance(future, int | float)
                    or not math.isfinite(float(entry))
                    or not math.isfinite(float(future))
                    or entry <= 0
                ):
                    continue
                forward_return = float(future) / float(entry) - 1.0
                if not math.isfinite(forward_return):
                    continue
                breakdown = result.breakdown
                observed = {
                    "final_score": result.final_score,
                    "alpha_score": breakdown.alpha_score,
                    "stock_relative_strength": feature.stock_relative_strength,
                    "ma20_distance": feature.ma20_distance,
                    "ma60_distance": feature.ma60_distance,
                    "ret_20d": feature.ret_20d,
                    "ret_5d": feature.ret_5d,
                    "ret_1d": feature.ret_1d,
                    "crowding_risk": breakdown.crowding_risk,
                    "execution_risk": breakdown.execution_risk,
                    "attention_risk": breakdown.attention_risk,
                    "quality_score": breakdown.quality_score,
                    "improvement_score": breakdown.improvement_score,
                    "value_score": breakdown.value_score,
                    "momentum_score": breakdown.momentum_score,
                    "size_score": breakdown.size_score,
                    "institutional_score": breakdown.institutional_score,
                }
                for factor, score in observed.items():
                    if (
                        factor in factor_rows
                        and isinstance(score, int | float)
                        and math.isfinite(float(score))
                    ):
                        factor_rows[factor].append((float(score), forward_return))
            for factor, pairs in factor_rows.items():
                ic = _spearman(pairs)
                if ic is not None:
                    values[(horizon, factor)].append((decision_day, ic))
                day_obs = quantile_day_observation(
                    pairs,
                    quantile_count=quantile_count,
                    decision_day=decision_day,
                )
                if day_obs is None:
                    quantile_skip_days[(horizon, factor)].append(decision_day)
                else:
                    quantile_values[(horizon, factor)].append(day_obs)
        if progress is not None:
            progress(progress_index, len(decision_days), decision_day)

    summaries = [
        _summary(
            horizon,
            factor,
            [value for _, value in values[(horizon, factor)]],
            scheduled_only=scheduled_only,
            signal_interval_days=signal_interval_days,
        )
        for horizon in normalized_horizons
        for factor in factors
    ]
    quantile_summaries = [
        summarize_quantile_observations(
            horizon=horizon,
            factor=factor,
            quantile_count=quantile_count,
            observations=quantile_values[(horizon, factor)],
            skipped_insufficient_cross_section=len(quantile_skip_days[(horizon, factor)]),
            scheduled_only=scheduled_only,
            signal_interval_days=signal_interval_days,
        )
        for horizon in normalized_horizons
        for factor in factors
    ]
    annual_periods = [
        _period_summary(
            label=str(year),
            start=max(start, date(year, 1, 1)),
            end=min(end, date(year, 12, 31)),
            horizons=normalized_horizons,
            values=values,
            factors=factors,
            scheduled_only=scheduled_only,
            signal_interval_days=signal_interval_days,
        )
        for year in range(start.year, end.year + 1)
    ]
    annual_quantile_periods = [
        _quantile_period_summary(
            label=str(year),
            start=max(start, date(year, 1, 1)),
            end=min(end, date(year, 12, 31)),
            horizons=normalized_horizons,
            factors=factors,
            quantile_count=quantile_count,
            quantile_values=quantile_values,
            quantile_skip_days=quantile_skip_days,
            scheduled_only=scheduled_only,
            signal_interval_days=signal_interval_days,
        )
        for year in range(start.year, end.year + 1)
    ]
    rolling_periods: list[ICPeriodSummary] = []
    rolling_quantile_periods: list[QuantilePeriodSummary] = []
    if rolling_window_days > 0:
        for offset in range(0, len(decision_days) - rolling_window_days + 1, rolling_step_days):
            period_days = decision_days[offset : offset + rolling_window_days]
            label = f"rolling_{period_days[0].isoformat()}_{period_days[-1].isoformat()}"
            rolling_periods.append(
                _period_summary(
                    label=label,
                    start=period_days[0],
                    end=period_days[-1],
                    horizons=normalized_horizons,
                    values=values,
                    factors=factors,
                    scheduled_only=scheduled_only,
                    signal_interval_days=signal_interval_days,
                )
            )
            rolling_quantile_periods.append(
                _quantile_period_summary(
                    label=label,
                    start=period_days[0],
                    end=period_days[-1],
                    horizons=normalized_horizons,
                    factors=factors,
                    quantile_count=quantile_count,
                    quantile_values=quantile_values,
                    quantile_skip_days=quantile_skip_days,
                    scheduled_only=scheduled_only,
                    signal_interval_days=signal_interval_days,
                )
            )
    return ICReport(
        strategy_config_hash=config.config_hash(),
        data_snapshot_id=snapshot.snapshot_id,
        start=start,
        end=end,
        horizons=normalized_horizons,
        decision_schedule=(
            "strategy_signal_schedule" if scheduled_only else "all_trading_days"
        ),
        diagnostic_only=True,
        tradable_long_short=False,
        ready_for_scoring=False,
        ready_for_trading=False,
        quantile_count=quantile_count,
        spread_definition=SPREAD_DEFINITION,
        skipped_no_label_dates=skipped,
        summaries=summaries,
        annual_periods=annual_periods,
        rolling_periods=rolling_periods,
        quantile_summaries=quantile_summaries,
        annual_quantile_periods=annual_quantile_periods,
        rolling_quantile_periods=rolling_quantile_periods,
    )


def _strategy_signal_days(
    *,
    store: MarketStore,
    config: StrategyConfig,
    start: date,
    end: date,
) -> list[date]:
    trade = config.trade
    if trade.signal_interval_days == 1:
        return store.get_calendar(start, end)
    anchor = trade.signal_anchor_date
    if anchor is None:
        raise ValueError("scheduled IC requires signal_anchor_date")
    if anchor > end:
        return []
    schedule = store.get_calendar(anchor, end)
    if not schedule or schedule[0] != anchor:
        raise ValueError(
            f"signal_anchor_date {anchor} is not a trading day in the snapshot"
        )
    return [
        day
        for day in schedule[:: trade.signal_interval_days]
        if start <= day <= end
    ]


def write_ic_report(report: ICReport, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")


def _adjusted_close_map(store: MarketStore, as_of: date, start: date) -> dict[tuple[str, date], float]:
    daily = store.get_daily_bars(as_of=as_of, start=start)
    if "adj_close" not in daily.columns:
        raise ValueError("daily_bars is missing adj_close; re-import a raw-plus-adjusted snapshot")
    rows = daily.select(["symbol", "date", "adj_close"]).drop_nulls().to_dicts()
    return {
        (str(row["symbol"]), row["date"]): float(row["adj_close"])
        for row in rows
        if isinstance(row["date"], date) and isinstance(row["adj_close"], int | float)
    }


def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs = [item[0] for item in pairs]
    ys = [item[1] for item in pairs]
    x_rank = _average_ranks(xs)
    y_rank = _average_ranks(ys)
    x_mean = sum(x_rank) / len(x_rank)
    y_mean = sum(y_rank) / len(y_rank)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_rank, y_rank, strict=True))
    x_var = sum((x - x_mean) ** 2 for x in x_rank)
    y_var = sum((y - y_mean) ** 2 for y in y_rank)
    denominator = math.sqrt(x_var * y_var)
    return numerator / denominator if denominator > 0 else None


def _average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    pos = 0
    while pos < len(ordered):
        end = pos + 1
        while end < len(ordered) and ordered[end][1] == ordered[pos][1]:
            end += 1
        average = (pos + 1 + end) / 2.0
        for index, _ in ordered[pos:end]:
            ranks[index] = average
        pos = end
    return ranks


def _target_overlap_lag(
    *,
    horizon_days: int,
    scheduled_only: bool,
    signal_interval_days: int,
) -> int:
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    if signal_interval_days <= 0:
        raise ValueError("signal_interval_days must be positive")
    if scheduled_only:
        return max(math.ceil(horizon_days / signal_interval_days) - 1, 0)
    return horizon_days - 1


def _newey_west_bartlett_long_run_variance(centered: list[float], lag: int) -> float:
    """Deterministic Newey-West / Bartlett long-run variance of a scalar series."""
    n = len(centered)
    if n < 1:
        raise ValueError("centered series must be non-empty")
    if lag < 0:
        raise ValueError("lag must be non-negative")
    if lag >= n:
        raise ValueError("lag must be strictly less than the observation count")
    gamma0 = sum(value * value for value in centered) / n
    long_run = gamma0
    for order in range(1, lag + 1):
        weight = 1.0 - order / (lag + 1)
        gamma = (
            sum(centered[index] * centered[index - order] for index in range(order, n)) / n
        )
        long_run += 2.0 * weight * gamma
    return long_run


def _summary(
    horizon: int,
    factor: str,
    observations: list[float],
    *,
    scheduled_only: bool = False,
    signal_interval_days: int = 1,
) -> ICFactorSummary:
    for value in observations:
        if not isinstance(value, int | float) or not math.isfinite(float(value)):
            raise ValueError("IC observations must be finite")
    n = len(observations)
    if not observations:
        return ICFactorSummary(
            horizon_days=horizon,
            factor=factor,
            observations=0,
            scoring_days=0,
            mean_spearman_ic=None,
            std_spearman_ic=None,
            t_stat=None,
            icir=None,
            hac_t_stat=None,
            hac_lag=None,
        )
    mean = sum(observations) / n
    if n < 2:
        return ICFactorSummary(
            horizon_days=horizon,
            factor=factor,
            observations=n,
            scoring_days=n,
            mean_spearman_ic=mean,
            std_spearman_ic=None,
            t_stat=None,
            icir=None,
            hac_t_stat=None,
            hac_lag=None,
        )
    target_lag = _target_overlap_lag(
        horizon_days=horizon,
        scheduled_only=scheduled_only,
        signal_interval_days=signal_interval_days,
    )
    hac_lag = min(target_lag, n - 1)
    # Exact equal values are zero variance; avoid float-noise "significance".
    if max(observations) == min(observations):
        return ICFactorSummary(
            horizon_days=horizon,
            factor=factor,
            observations=n,
            scoring_days=n,
            mean_spearman_ic=mean,
            std_spearman_ic=None,
            t_stat=None,
            icir=None,
            hac_t_stat=None,
            hac_lag=hac_lag,
        )
    variance = sum((value - mean) ** 2 for value in observations) / (n - 1)
    std = math.sqrt(variance) if math.isfinite(variance) and variance > 0 else None
    # Historical naive IID t-stat; keep this field's meaning for JSON compatibility.
    t_stat = (mean / (std / math.sqrt(n))) if std is not None and std > 0 else None
    if t_stat is not None and not math.isfinite(t_stat):
        t_stat = None
    icir = (mean / std) if std is not None and std > 0 else None
    if icir is not None and not math.isfinite(icir):
        icir = None
    centered = [value - mean for value in observations]
    long_run = _newey_west_bartlett_long_run_variance(centered, hac_lag)
    hac_t_stat: float | None
    if math.isfinite(long_run) and long_run > 0:
        candidate = mean / math.sqrt(long_run / n)
        hac_t_stat = candidate if math.isfinite(candidate) else None
    else:
        hac_t_stat = None
    return ICFactorSummary(
        horizon_days=horizon,
        factor=factor,
        observations=n,
        scoring_days=n,
        mean_spearman_ic=mean,
        std_spearman_ic=std,
        t_stat=t_stat,
        icir=icir,
        hac_t_stat=hac_t_stat,
        hac_lag=hac_lag,
    )


def _period_summary(
    *,
    label: str,
    start: date,
    end: date,
    horizons: list[int],
    values: dict[tuple[int, str], list[tuple[date, float]]],
    factors: tuple[str, ...],
    scheduled_only: bool,
    signal_interval_days: int,
) -> ICPeriodSummary:
    summaries = [
        _summary(
            horizon,
            factor,
            [value for observed_on, value in values[(horizon, factor)] if start <= observed_on <= end],
            scheduled_only=scheduled_only,
            signal_interval_days=signal_interval_days,
        )
        for horizon in horizons
        for factor in factors
    ]
    return ICPeriodSummary(label=label, start=start, end=end, summaries=summaries)


def _quantile_period_summary(
    *,
    label: str,
    start: date,
    end: date,
    horizons: list[int],
    factors: tuple[str, ...],
    quantile_count: int,
    quantile_values: dict[tuple[int, str], list[QuantileDayObservation]],
    quantile_skip_days: dict[tuple[int, str], list[date]],
    scheduled_only: bool,
    signal_interval_days: int,
) -> QuantilePeriodSummary:
    summaries = [
        summarize_quantile_observations(
            horizon=horizon,
            factor=factor,
            quantile_count=quantile_count,
            observations=[
                item
                for item in quantile_values[(horizon, factor)]
                if start <= item.decision_day <= end
            ],
            skipped_insufficient_cross_section=sum(
                1
                for day in quantile_skip_days[(horizon, factor)]
                if start <= day <= end
            ),
            scheduled_only=scheduled_only,
            signal_interval_days=signal_interval_days,
        )
        for horizon in horizons
        for factor in factors
    ]
    return QuantilePeriodSummary(label=label, start=start, end=end, summaries=summaries)
