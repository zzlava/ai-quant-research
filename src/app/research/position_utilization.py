from __future__ import annotations

from app.models.backtest import BacktestResult, PositionUtilizationSummary


def summarize_position_utilization(
    result: BacktestResult,
    *,
    max_positions: int,
) -> PositionUtilizationSummary:
    """Pure read-only utilization diagnosis from equity_curve + signal attribution.

    Fails closed when end-of-day position counts are absent from the equity curve
    (legacy JSON). Never fabricates zero counts from missing fields.
    """
    signal = result.attribution.signal
    funnel = dict(
        scheduled_signal_days=signal.scheduled_signal_days,
        empty_ranking_days=signal.empty_ranking_days,
        regime_blocked_days=signal.regime_blocked_days,
        capacity_blocked_days=signal.capacity_blocked_days,
        orders_generated=signal.orders_generated,
        entry_attempts=signal.entry_attempts,
        orders_filled=signal.orders_filled,
        target_entry_budget_total=signal.target_entry_budget_total,
        actual_entry_cash_used_total=signal.actual_entry_cash_used_total,
        unallocated_entry_budget_total=signal.unallocated_entry_budget_total,
        overallocated_entry_budget_total=signal.overallocated_entry_budget_total,
    )
    fill_rate = (
        signal.orders_filled / signal.entry_attempts if signal.entry_attempts > 0 else None
    )
    # Based only on successful-fill target/actual budgets; failed attempts are not treated as 0.
    # May exceed 1.0 when cash-fallback spends above the per-slot target allocation.
    budget_utilization = (
        signal.actual_entry_cash_used_total / signal.target_entry_budget_total
        if signal.target_entry_budget_total > 0
        else None
    )

    if max_positions <= 0:
        return PositionUtilizationSummary(
            available=False,
            unavailable_reason="max_positions must be positive",
            fill_rate=fill_rate,
            budget_utilization=budget_utilization,
            **funnel,
        )

    curve = result.equity_curve
    if not curve:
        return PositionUtilizationSummary(
            available=False,
            unavailable_reason="equity_curve is empty",
            fill_rate=fill_rate,
            budget_utilization=budget_utilization,
            **funnel,
        )

    if any(point.open_positions is None for point in curve):
        return PositionUtilizationSummary(
            available=False,
            unavailable_reason=(
                "equity_curve open_positions unavailable; "
                "legacy result lacks end-of-day position counts"
            ),
            fill_rate=fill_rate,
            budget_utilization=budget_utilization,
            **funnel,
        )

    open_counts = [int(point.open_positions) for point in curve]  # type: ignore[arg-type]
    trading_days = len(open_counts)
    zero_position_days = sum(1 for count in open_counts if count == 0)
    underfilled_days = sum(1 for count in open_counts if count < max_positions)
    invested_fractions = [
        point.market_value / point.equity if point.equity > 0 else 0.0 for point in curve
    ]
    cash_fractions = [
        point.cash / point.equity if point.equity > 0 else 0.0 for point in curve
    ]
    return PositionUtilizationSummary(
        available=True,
        unavailable_reason=None,
        trading_days=trading_days,
        zero_position_days=zero_position_days,
        underfilled_days=underfilled_days,
        average_open_positions=sum(open_counts) / trading_days,
        peak_open_positions=max(open_counts),
        average_invested_fraction=sum(invested_fractions) / trading_days,
        peak_invested_fraction=max(invested_fractions),
        average_cash_fraction=sum(cash_fractions) / trading_days,
        fill_rate=fill_rate,
        budget_utilization=budget_utilization,
        **funnel,
    )
