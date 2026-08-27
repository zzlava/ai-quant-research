from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.backtest.engine import BacktestEngine
from app.models.backtest import PositionUtilizationSummary
from app.models.config import StrategyConfig
from app.models.scores import ScoreResult
from app.research.position_utilization import summarize_position_utilization
from app.storage.protocol import MarketStore

ScoreFn = Callable[[date], list[ScoreResult]]
ProgressFn = Callable[[int, int, date], None]

_INDEPENDENCE_NOTE = (
    "All phases share one store, one evaluation window, and one read-only score "
    "function; they are not N independent out-of-sample samples."
)

_DEFAULT_WARNINGS = (
    "diagnostic_only: do not auto-select a phase by return or sharpe",
    "parameter_selection_forbidden: phase results must not change strategy config",
    "development-window diagnostic only; first 2025+ use requires new authorization",
    _INDEPENDENCE_NOTE,
)


class MetricRange(BaseModel):
    min: float | None
    median: float | None
    max: float | None
    range: float | None


class SignalPhaseResult(BaseModel):
    phase_offset: int
    signal_anchor_date: date
    runtime_config_hash: str
    total_return: float
    annualized_return: float | None
    sharpe_ratio: float | None
    max_drawdown: float | None
    trades: int
    costs: float
    orders_generated: int
    orders_filled: int
    planned_signal_dates: list[date] = Field(default_factory=list)
    average_open_positions: float
    peak_open_positions: int
    average_invested_fraction: float
    peak_invested_fraction: float
    average_cash_fraction: float
    zero_position_days: int
    underfilled_days: int
    fill_rate: float | None
    budget_utilization: float | None
    utilization: PositionUtilizationSummary


class SignalPhaseSummary(BaseModel):
    total_return: MetricRange
    sharpe_ratio: MetricRange
    max_drawdown: MetricRange
    average_invested_fraction: MetricRange
    trades: MetricRange
    independence_note: str = _INDEPENDENCE_NOTE


class SignalPhaseSensitivityWindow(BaseModel):
    """Evaluation bounds for the diagnostic.

    ``start`` / ``end`` are the evaluation window; ``end`` equals ``valuation_end``
    (last valuation / mark-to-market day). ``signal_end`` is the last day on which
    BacktestEngine may generate a signal (requires a next trading day on or before
    entry/valuation end), typically the second-to-last trading day.
    """

    start: date
    end: date
    signal_end: date
    valuation_end: date


class SignalPhaseSensitivityReport(BaseModel):
    base_config_hash: str
    data_snapshot_id: str
    window: SignalPhaseSensitivityWindow
    signal_interval_days: int
    original_anchor: date
    phase_count: int
    diagnostic_only: Literal[True] = True
    parameter_selection_forbidden: Literal[True] = True
    selected_phase: Literal[None] = None
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    phases: list[SignalPhaseResult] = Field(default_factory=list)
    summary: SignalPhaseSummary
    warnings: list[str] = Field(default_factory=list)


def analyze_signal_phase_sensitivity(
    *,
    store: MarketStore,
    config: StrategyConfig,
    start: date,
    end: date,
    score_fn: ScoreFn,
    progress: ProgressFn | None = None,
) -> SignalPhaseSensitivityReport:
    """Run fixed-interval full-phase sensitivity; never select a winning phase."""
    if start > end:
        raise ValueError("start must be on or before end")
    interval = config.trade.signal_interval_days
    if interval <= 1:
        raise ValueError("signal_interval_days must be > 1 for phase sensitivity")
    original_anchor = config.trade.signal_anchor_date
    if original_anchor is None:
        raise ValueError("signal_interval_days > 1 requires signal_anchor_date")

    window_start, signal_end, valuation_end = _executable_signal_bounds(store, start=start, end=end)
    phase_anchors = resolve_phase_anchors(store, original_anchor=original_anchor, interval=interval)

    prepared: list[tuple[int, date, list[date]]] = []
    for offset, anchor in phase_anchors:
        planned = planned_signal_dates(
            store,
            anchor=anchor,
            interval=interval,
            signal_end=signal_end,
            evaluation_start=start,
        )
        if not planned:
            raise ValueError(
                f"phase_offset={offset} has no planned signal dates in the evaluation "
                f"window [{start.isoformat()}, {signal_end.isoformat()}]; refusing empty cash path"
            )
        prepared.append((offset, anchor, planned))

    phases: list[SignalPhaseResult] = []
    for done, (offset, anchor, planned) in enumerate(prepared, start=1):
        runtime = apply_phase_anchor(config, anchor)
        result = BacktestEngine(store, runtime, signal_fn=score_fn).run(start, end)
        utilization = summarize_position_utilization(result, max_positions=runtime.portfolio.max_positions)
        if not utilization.available:
            reason = utilization.unavailable_reason or "position utilization unavailable"
            raise ValueError(f"phase_offset={offset} utilization unavailable; refusing to skip: {reason}")
        assert utilization.average_open_positions is not None
        assert utilization.peak_open_positions is not None
        assert utilization.average_invested_fraction is not None
        assert utilization.peak_invested_fraction is not None
        assert utilization.average_cash_fraction is not None
        assert utilization.zero_position_days is not None
        assert utilization.underfilled_days is not None
        phases.append(
            SignalPhaseResult(
                phase_offset=offset,
                signal_anchor_date=anchor,
                runtime_config_hash=runtime.config_hash(),
                total_return=result.metrics.total_return,
                annualized_return=result.metrics.annualized_return,
                sharpe_ratio=result.metrics.sharpe_ratio,
                max_drawdown=result.metrics.max_drawdown,
                trades=result.metrics.number_of_trades,
                costs=result.attribution.total_trading_costs,
                orders_generated=result.attribution.signal.orders_generated,
                orders_filled=result.attribution.signal.orders_filled,
                planned_signal_dates=planned,
                average_open_positions=utilization.average_open_positions,
                peak_open_positions=utilization.peak_open_positions,
                average_invested_fraction=utilization.average_invested_fraction,
                peak_invested_fraction=utilization.peak_invested_fraction,
                average_cash_fraction=utilization.average_cash_fraction,
                zero_position_days=utilization.zero_position_days,
                underfilled_days=utilization.underfilled_days,
                fill_rate=utilization.fill_rate,
                budget_utilization=utilization.budget_utilization,
                utilization=utilization,
            )
        )
        if progress is not None:
            progress(done, interval, anchor)

    phases.sort(key=lambda item: item.phase_offset)
    if [item.phase_offset for item in phases] != list(range(interval)):
        raise ValueError("phase_offset sequence must be exactly 0..N-1 with no gaps")

    return SignalPhaseSensitivityReport(
        base_config_hash=config.config_hash(),
        data_snapshot_id=store.snapshot().snapshot_id,
        window=SignalPhaseSensitivityWindow(
            start=window_start,
            end=valuation_end,
            signal_end=signal_end,
            valuation_end=valuation_end,
        ),
        signal_interval_days=interval,
        original_anchor=original_anchor,
        phase_count=interval,
        phases=phases,
        summary=_summarize_phases(phases),
        warnings=list(_DEFAULT_WARNINGS),
    )


def resolve_phase_anchors(
    store: MarketStore,
    *,
    original_anchor: date,
    interval: int,
) -> list[tuple[int, date]]:
    """Map phase_offset=0..N-1 onto trading days starting at the original anchor."""
    if interval <= 1:
        raise ValueError("signal_interval_days must be > 1 for phase sensitivity")
    probe = store.get_calendar(original_anchor, original_anchor)
    if not probe or probe[0] != original_anchor:
        raise ValueError(f"signal_anchor_date {original_anchor} is not a trading day in the snapshot")
    rest = store.trading_days_after(original_anchor, interval - 1)
    if len(rest) < interval - 1:
        raise ValueError(
            f"incomplete trading calendar for phase offsets 0..{interval - 1}; "
            f"need {interval} trading days from original anchor"
        )
    days = [original_anchor, *rest]
    return [(offset, days[offset]) for offset in range(interval)]


def apply_phase_anchor(base: StrategyConfig, anchor: date) -> StrategyConfig:
    """Return a copy that changes only trade.signal_anchor_date; fail otherwise."""
    runtime = base.model_copy(update={"trade": base.trade.model_copy(update={"signal_anchor_date": anchor})})
    diff = canonical_config_diff(base, runtime)
    if anchor == base.trade.signal_anchor_date:
        if diff:
            raise ValueError(f"phase config unexpectedly changed fields: {diff}")
    elif diff != ["trade.signal_anchor_date"]:
        raise ValueError(f"phase config diff is not anchor-only: {diff}")
    return runtime


def planned_signal_dates(
    store: MarketStore,
    *,
    anchor: date,
    interval: int,
    signal_end: date,
    evaluation_start: date,
) -> list[date]:
    """Plan signal days matching BacktestEngine scheduled signals inside the eval window.

    Schedule is built from ``anchor`` through ``signal_end`` (same as the engine), then
    restricted to ``evaluation_start <= day <= signal_end`` so dates before the evaluation
    start and the non-executable valuation_end day are excluded.
    """
    if interval <= 0:
        raise ValueError("signal_interval_days must be positive")
    if anchor > signal_end:
        return []
    schedule = store.get_calendar(anchor, signal_end)
    if not schedule or schedule[0] != anchor:
        raise ValueError(f"signal_anchor_date {anchor} is not a trading day in the snapshot")
    return [day for day in schedule[::interval] if evaluation_start <= day <= signal_end]


def canonical_config_diff(left: StrategyConfig, right: StrategyConfig) -> list[str]:
    return _walk_diff(left.model_dump(mode="json"), right.model_dump(mode="json"), prefix="")


def write_signal_phase_sensitivity_report(report: SignalPhaseSensitivityReport, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _executable_signal_bounds(
    store: MarketStore,
    *,
    start: date,
    end: date,
) -> tuple[date, date, date]:
    """Return (window_start, signal_end, valuation_end) identical to BacktestEngine._window.

    Fails closed when the window has no trading days or no executable signal day
    (typically fewer than two trading days).
    """
    calendar = store.get_calendar(start, end)
    if not calendar:
        raise ValueError("no trading days in evaluation window")
    valuation_end = calendar[-1]
    entry_end = valuation_end
    signal_end: date | None = None
    for day in calendar:
        nxt = store.next_trading_day(day)
        if nxt is not None and nxt <= entry_end:
            signal_end = day
    if signal_end is None:
        raise ValueError(
            "evaluation window has no executable signal day "
            "(need at least two trading days with a next day on or before valuation_end)"
        )
    return calendar[0], signal_end, valuation_end


def _summarize_phases(phases: list[SignalPhaseResult]) -> SignalPhaseSummary:
    return SignalPhaseSummary(
        total_return=_metric_range([item.total_return for item in phases]),
        sharpe_ratio=_metric_range([item.sharpe_ratio for item in phases], allow_missing=True),
        max_drawdown=_metric_range([item.max_drawdown for item in phases], allow_missing=True),
        average_invested_fraction=_metric_range([item.average_invested_fraction for item in phases]),
        trades=_metric_range([float(item.trades) for item in phases]),
        independence_note=_INDEPENDENCE_NOTE,
    )


def _metric_range(
    values: list[float | None],
    *,
    allow_missing: bool = False,
) -> MetricRange:
    if allow_missing and any(value is None for value in values):
        return MetricRange(min=None, median=None, max=None, range=None)
    cleaned: list[float] = []
    for value in values:
        if value is None:
            raise ValueError("summary metric contains a missing value")
        cleaned.append(float(value))
    if not cleaned:
        raise ValueError("summary metric has no values")
    low = min(cleaned)
    high = max(cleaned)
    return MetricRange(
        min=low,
        median=float(median(cleaned)),
        max=high,
        range=high - low,
    )


def _walk_diff(left: Any, right: Any, *, prefix: str) -> list[str]:
    if type(left) is not type(right):
        return [prefix or "<root>"]
    if isinstance(left, dict):
        keys = set(left) | set(right)
        out: list[str] = []
        for key in sorted(keys):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                out.append(path)
            else:
                out.extend(_walk_diff(left[key], right[key], prefix=path))
        return out
    if isinstance(left, list):
        if len(left) != len(right):
            return [prefix or "<root>"]
        out = []
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=True)):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            out.extend(_walk_diff(left_item, right_item, prefix=path))
        return out
    if left != right:
        return [prefix or "<root>"]
    return []
