from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from app.models.config import StrategyConfig
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
    skipped_no_label_dates: dict[int, int]
    summaries: list[ICFactorSummary] = Field(default_factory=list)
    annual_periods: list[ICPeriodSummary] = Field(default_factory=list)
    rolling_periods: list[ICPeriodSummary] = Field(default_factory=list)


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


def analyze_ic(
    *,
    store: MarketStore,
    config: StrategyConfig,
    start: date,
    end: date,
    horizons: list[int],
    rolling_window_days: int = 0,
    rolling_step_days: int = 0,
    progress: Callable[[int, int, date], None] | None = None,
) -> ICReport:
    """Measure same-day factor ranks against later adjusted-close returns.

    Features and ranks are generated strictly as of each decision date.  The
    later close is a research label only; it is never supplied to the scorer
    or execution engine.  Missing future prices are counted and excluded from
    that day-factor observation rather than converted to zero.
    """
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
    if not decision_days:
        raise ValueError("requested IC window contains no trading days")
    day_index = {day: idx for idx, day in enumerate(calendar)}
    prices = _adjusted_close_map(store, snapshot.coverage_end, start)
    factors = (
        (*_TECHNICAL_FACTORS, *_FUNDAMENTAL_FACTORS)
        if config.fundamental is not None
        else _TECHNICAL_FACTORS
    )
    values: dict[tuple[int, str], list[tuple[date, float]]] = {
        (horizon, factor): []
        for horizon in normalized_horizons
        for factor in factors
    }
    skipped = {horizon: 0 for horizon in normalized_horizons}
    engine = ScoringEngine(store, config)

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
                if entry is None or future is None or entry <= 0:
                    continue
                forward_return = future / entry - 1.0
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
                }
                for factor, score in observed.items():
                    if factor in factor_rows:
                        factor_rows[factor].append((score, forward_return))
            for factor, pairs in factor_rows.items():
                ic = _spearman(pairs)
                if ic is not None:
                    values[(horizon, factor)].append((decision_day, ic))
        if progress is not None:
            progress(progress_index, len(decision_days), decision_day)

    summaries = [
        _summary(horizon, factor, [value for _, value in values[(horizon, factor)]])
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
        )
        for year in range(start.year, end.year + 1)
    ]
    rolling_periods: list[ICPeriodSummary] = []
    if rolling_window_days > 0:
        for offset in range(0, len(decision_days) - rolling_window_days + 1, rolling_step_days):
            period_days = decision_days[offset : offset + rolling_window_days]
            rolling_periods.append(
                _period_summary(
                    label=f"rolling_{period_days[0].isoformat()}_{period_days[-1].isoformat()}",
                    start=period_days[0],
                    end=period_days[-1],
                    horizons=normalized_horizons,
                    values=values,
                    factors=factors,
                )
            )
    return ICReport(
        strategy_config_hash=config.config_hash(),
        data_snapshot_id=snapshot.snapshot_id,
        start=start,
        end=end,
        horizons=normalized_horizons,
        skipped_no_label_dates=skipped,
        summaries=summaries,
        annual_periods=annual_periods,
        rolling_periods=rolling_periods,
    )


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


def _summary(horizon: int, factor: str, observations: list[float]) -> ICFactorSummary:
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
        )
    mean = sum(observations) / n
    if n < 2:
        std = None
        t_stat = None
    else:
        std = math.sqrt(sum((value - mean) ** 2 for value in observations) / (n - 1))
        t_stat = (mean / (std / math.sqrt(n))) if std > 0 else None
    return ICFactorSummary(
        horizon_days=horizon,
        factor=factor,
        observations=n,
        scoring_days=n,
        mean_spearman_ic=mean,
        std_spearman_ic=std,
        t_stat=t_stat,
    )


def _period_summary(
    *,
    label: str,
    start: date,
    end: date,
    horizons: list[int],
    values: dict[tuple[int, str], list[tuple[date, float]]],
    factors: tuple[str, ...],
) -> ICPeriodSummary:
    summaries = [
        _summary(
            horizon,
            factor,
            [value for observed_on, value in values[(horizon, factor)] if start <= observed_on <= end],
        )
        for horizon in horizons
        for factor in factors
    ]
    return ICPeriodSummary(label=label, start=start, end=end, summaries=summaries)
