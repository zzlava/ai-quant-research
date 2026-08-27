"""Cross-sectional quantile portfolio diagnostics for factor evaluation.

These long/short quantile spreads are research labels only. On A-shares the
short leg is generally not an executable trade; spreads must not authorize
scoring or trading.
"""

from __future__ import annotations

import math
from datetime import date

from pydantic import BaseModel, Field, field_validator, model_validator

SPREAD_DEFINITION = (
    "highest_factor_quantile_return_minus_lowest_factor_quantile_return"
)
# Strict but float-safe: reject construction drift without failing exact arithmetic.
_SPREAD_REL_TOL = 1e-12
_SPREAD_ABS_TOL = 1e-12


class QuantileFactorSummary(BaseModel):
    horizon_days: int
    factor: str
    quantile_count: int
    spread_definition: str = SPREAD_DEFINITION
    scoring_days: int
    average_names: float | None
    minimum_names: int | None
    mean_highest_quantile_return: float | None
    mean_lowest_quantile_return: float | None
    mean_spread: float | None
    std_spread: float | None
    t_stat: float | None
    spread_ir: float | None = None
    hac_t_stat: float | None = None
    hac_lag: int | None = None
    skipped_insufficient_cross_section: int = 0


class QuantilePeriodSummary(BaseModel):
    label: str
    start: date
    end: date
    summaries: list[QuantileFactorSummary] = Field(default_factory=list)


class QuantileDayObservation(BaseModel):
    """One scoring-day quantile portfolio observation (no per-name detail)."""

    decision_day: date
    names: int
    highest_quantile_return: float
    lowest_quantile_return: float
    spread: float

    @field_validator("names", mode="before")
    @classmethod
    def _positive_names(cls, value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("names must be a positive integer")
        return value

    @field_validator(
        "highest_quantile_return",
        "lowest_quantile_return",
        "spread",
        mode="before",
    )
    @classmethod
    def _finite_return_fields(cls, value: object) -> float:
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(float(value))
        ):
            raise ValueError("quantile return fields must be finite")
        return float(value)

    @model_validator(mode="after")
    def _spread_matches_legs(self) -> QuantileDayObservation:
        expected = self.highest_quantile_return - self.lowest_quantile_return
        if not math.isclose(
            self.spread,
            expected,
            rel_tol=_SPREAD_REL_TOL,
            abs_tol=_SPREAD_ABS_TOL,
        ):
            raise ValueError(
                "spread must equal highest_quantile_return - lowest_quantile_return"
            )
        return self


def validate_quantile_count(quantile_count: int) -> int:
    if not isinstance(quantile_count, int) or isinstance(quantile_count, bool):
        raise ValueError("quantile_count must be an integer from 2 to 10")
    if quantile_count < 2 or quantile_count > 10:
        raise ValueError("quantile_count must be an integer from 2 to 10")
    return quantile_count


def average_ranks(values: list[float]) -> list[float]:
    """Deterministic average ranks; ties share one rank (order-insensitive)."""
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


def _require_finite_pair_values(pairs: list[tuple[float, float]]) -> None:
    """Fail closed on non-finite factor/return; callers must not fill unknown with 0."""
    for factor, forward_return in pairs:
        if (
            not isinstance(factor, int | float)
            or isinstance(factor, bool)
            or not math.isfinite(float(factor))
        ):
            raise ValueError("quantile factor values must be finite")
        if (
            not isinstance(forward_return, int | float)
            or isinstance(forward_return, bool)
            or not math.isfinite(float(forward_return))
        ):
            raise ValueError("quantile forward returns must be finite")


def quantile_day_observation(
    pairs: list[tuple[float, float]],
    *,
    quantile_count: int,
    decision_day: date,
) -> QuantileDayObservation | None:
    """Equal-weight high/low factor quantiles from as-of scores and forward returns.

    Missing/non-finite values must already be excluded by the caller (analyze_ic
    excludes unknown; never fill with 0). Non-finite pairs fail closed here.
    Ties keep a shared average rank so identical factor values are never split
    across opposite ends. Fully equal cross-sections are skipped.
    """
    validate_quantile_count(quantile_count)
    _require_finite_pair_values(pairs)
    if len(pairs) < quantile_count:
        return None
    factors = [score for score, _ in pairs]
    if max(factors) == min(factors):
        return None
    ranks = average_ranks(factors)
    n = len(pairs)
    buckets: list[list[float]] = [[] for _ in range(quantile_count)]
    for rank, (_, forward_return) in zip(ranks, pairs, strict=True):
        bucket = min(int((rank - 1.0) / n * quantile_count), quantile_count - 1)
        buckets[bucket].append(forward_return)
    highest = buckets[-1]
    lowest = buckets[0]
    if not highest or not lowest:
        return None
    high_mean = sum(highest) / len(highest)
    low_mean = sum(lowest) / len(lowest)
    spread = high_mean - low_mean
    if not all(math.isfinite(value) for value in (high_mean, low_mean, spread)):
        return None
    return QuantileDayObservation(
        decision_day=decision_day,
        names=n,
        highest_quantile_return=high_mean,
        lowest_quantile_return=low_mean,
        spread=spread,
    )


def target_overlap_lag(
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


def newey_west_bartlett_long_run_variance(centered: list[float], lag: int) -> float:
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


def _ordered_unique_observations(
    observations: list[QuantileDayObservation],
) -> list[QuantileDayObservation]:
    """Sort by decision_day; reject duplicate dates (significance unit is the day)."""
    ordered = sorted(observations, key=lambda item: item.decision_day)
    previous: date | None = None
    for item in ordered:
        if previous is not None and item.decision_day == previous:
            raise ValueError(
                "duplicate decision_day in quantile observations; "
                "each scoring day must appear at most once"
            )
        previous = item.decision_day
    return ordered


def summarize_quantile_observations(
    *,
    horizon: int,
    factor: str,
    quantile_count: int,
    observations: list[QuantileDayObservation],
    skipped_insufficient_cross_section: int,
    scheduled_only: bool,
    signal_interval_days: int,
) -> QuantileFactorSummary:
    validate_quantile_count(quantile_count)
    if skipped_insufficient_cross_section < 0:
        raise ValueError("skipped_insufficient_cross_section must be non-negative")
    observations = _ordered_unique_observations(observations)
    spreads = [item.spread for item in observations]
    for value in spreads:
        if not isinstance(value, int | float) or not math.isfinite(float(value)):
            raise ValueError("quantile spread observations must be finite")
    n = len(observations)
    if n == 0:
        return QuantileFactorSummary(
            horizon_days=horizon,
            factor=factor,
            quantile_count=quantile_count,
            spread_definition=SPREAD_DEFINITION,
            scoring_days=0,
            average_names=None,
            minimum_names=None,
            mean_highest_quantile_return=None,
            mean_lowest_quantile_return=None,
            mean_spread=None,
            std_spread=None,
            t_stat=None,
            spread_ir=None,
            hac_t_stat=None,
            hac_lag=None,
            skipped_insufficient_cross_section=skipped_insufficient_cross_section,
        )

    names = [item.names for item in observations]
    average_names = sum(names) / n
    minimum_names = min(names)
    mean_high = sum(item.highest_quantile_return for item in observations) / n
    mean_low = sum(item.lowest_quantile_return for item in observations) / n
    mean_spread = sum(spreads) / n
    if n < 2:
        return QuantileFactorSummary(
            horizon_days=horizon,
            factor=factor,
            quantile_count=quantile_count,
            spread_definition=SPREAD_DEFINITION,
            scoring_days=n,
            average_names=average_names,
            minimum_names=minimum_names,
            mean_highest_quantile_return=mean_high,
            mean_lowest_quantile_return=mean_low,
            mean_spread=mean_spread,
            std_spread=None,
            t_stat=None,
            spread_ir=None,
            hac_t_stat=None,
            hac_lag=None,
            skipped_insufficient_cross_section=skipped_insufficient_cross_section,
        )

    lag_target = target_overlap_lag(
        horizon_days=horizon,
        scheduled_only=scheduled_only,
        signal_interval_days=signal_interval_days,
    )
    hac_lag = min(lag_target, n - 1)
    if max(spreads) == min(spreads):
        return QuantileFactorSummary(
            horizon_days=horizon,
            factor=factor,
            quantile_count=quantile_count,
            spread_definition=SPREAD_DEFINITION,
            scoring_days=n,
            average_names=average_names,
            minimum_names=minimum_names,
            mean_highest_quantile_return=mean_high,
            mean_lowest_quantile_return=mean_low,
            mean_spread=mean_spread,
            std_spread=None,
            t_stat=None,
            spread_ir=None,
            hac_t_stat=None,
            hac_lag=hac_lag,
            skipped_insufficient_cross_section=skipped_insufficient_cross_section,
        )

    variance = sum((value - mean_spread) ** 2 for value in spreads) / (n - 1)
    std = math.sqrt(variance) if math.isfinite(variance) and variance > 0 else None
    t_stat = (
        (mean_spread / (std / math.sqrt(n))) if std is not None and std > 0 else None
    )
    if t_stat is not None and not math.isfinite(t_stat):
        t_stat = None
    spread_ir = (mean_spread / std) if std is not None and std > 0 else None
    if spread_ir is not None and not math.isfinite(spread_ir):
        spread_ir = None
    centered = [value - mean_spread for value in spreads]
    long_run = newey_west_bartlett_long_run_variance(centered, hac_lag)
    if math.isfinite(long_run) and long_run > 0:
        candidate = mean_spread / math.sqrt(long_run / n)
        hac_t_stat = candidate if math.isfinite(candidate) else None
    else:
        hac_t_stat = None
    return QuantileFactorSummary(
        horizon_days=horizon,
        factor=factor,
        quantile_count=quantile_count,
        spread_definition=SPREAD_DEFINITION,
        scoring_days=n,
        average_names=average_names,
        minimum_names=minimum_names,
        mean_highest_quantile_return=mean_high,
        mean_lowest_quantile_return=mean_low,
        mean_spread=mean_spread,
        std_spread=std,
        t_stat=t_stat,
        spread_ir=spread_ir,
        hac_t_stat=hac_t_stat,
        hac_lag=hac_lag,
        skipped_insufficient_cross_section=skipped_insufficient_cross_section,
    )
