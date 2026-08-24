from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from app.models.backtest import BacktestResult, TradeFill
from app.models.config import StrategyConfig
from app.models.market import Instrument
from app.models.scores import ScoreResult
from app.research.portfolio_construction import (
    BenchmarkResult,
    PeriodResult,
    PortfolioConstructionReport,
    evaluate_price_index_benchmark,
    run_backtest_with_signal_cutoff,
)
from app.storage.protocol import MarketStore

ScoreFn = Callable[[date], list[ScoreResult]]
ProgressFn = Callable[[str, int, int], None]


class ExposureSummary(BaseModel):
    trading_days: int
    invested_days: int
    invested_day_rate: float
    average_invested_fraction: float
    peak_invested_fraction: float
    average_open_positions: float
    peak_open_positions: int


class DrawdownEpisode(BaseModel):
    peak_date: date
    trough_date: date
    recovery_date: date | None
    drawdown: float


class SymbolAttribution(BaseModel):
    symbol: str
    name: str
    sector: str
    trades: int
    wins: int
    entry_notional: float
    gross_pnl: float
    net_pnl: float
    trading_costs: float
    contribution_to_initial_cash: float


class SectorAttribution(BaseModel):
    sector: str
    trades: int
    symbols: int
    entry_notional: float
    entry_notional_share: float
    net_pnl: float
    contribution_to_initial_cash: float


class RegimeAttribution(BaseModel):
    regime: str
    trades: int
    entry_notional: float
    net_pnl: float
    contribution_to_initial_cash: float


class MonthlyAttribution(BaseModel):
    month: str
    trades: int
    net_pnl: float
    contribution_to_initial_cash: float


class PeriodRobustness(BaseModel):
    label: str
    strategy: PeriodResult
    benchmark: BenchmarkResult
    return_minus_benchmark: float
    exposure: ExposureSummary
    maximum_drawdown_episode: DrawdownEpisode
    symbols: list[SymbolAttribution] = Field(default_factory=list)
    sectors: list[SectorAttribution] = Field(default_factory=list)
    regimes: list[RegimeAttribution] = Field(default_factory=list)
    months: list[MonthlyAttribution] = Field(default_factory=list)
    largest_symbol_loss_to_initial_cash: float
    top_three_symbol_loss_share: float
    symbol_absolute_pnl_hhi: float
    largest_sector_entry_notional_share: float
    sector_entry_notional_hhi: float
    pnl_reconciliation_error: float


class CostScenarioResult(BaseModel):
    scenario_id: str
    commission_rate: float
    minimum_commission: float
    slippage_bps: float
    stamp_tax_unchanged: bool = True
    periods: list[PeriodResult] = Field(default_factory=list)


class RobustnessGate(BaseModel):
    gate: str
    passed: bool
    observed: float | str
    threshold: float | str
    note: str


class FrozenPortfolioRobustnessReport(BaseModel):
    selected_config_hash: str
    selection_report_path: str
    data_snapshot_id: str
    initial_cash: float
    max_positions: int
    holding_days: int
    signal_interval_days: int
    diagnostic_policy: str = "frozen_config_no_parameter_selection_no_signal_changes"
    sector_taxonomy_note: str = (
        "Instrument sector labels are current stock-basic classifications, not historical "
        "point-in-time industry membership. They are diagnostic only and never trading inputs."
    )
    periods: list[PeriodRobustness] = Field(default_factory=list)
    cost_scenarios: list[CostScenarioResult] = Field(default_factory=list)
    gates: list[RobustnessGate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    status: str
    status_reason: str
    score_cache_hits: int = 0
    score_cache_misses: int = 0


def analyze_frozen_portfolio_robustness(
    *,
    store: MarketStore,
    config: StrategyConfig,
    selection_report: PortfolioConstructionReport,
    selection_report_path: Path,
    score_fn: ScoreFn,
    progress: ProgressFn | None = None,
) -> FrozenPortfolioRobustnessReport:
    windows = _validate_and_windows(store, config, selection_report)
    instruments = {item.symbol: item for item in store.get_instruments()}
    baseline_results: dict[str, BacktestResult] = {}
    diagnostics: list[PeriodRobustness] = []
    total_runs = len(windows) * 3
    completed = 0

    for label, start, end, cutoff, expected_return in windows:
        result = run_backtest_with_signal_cutoff(
            store=store,
            config=config,
            start=start,
            end=end,
            signal_cutoff=cutoff,
            score_fn=score_fn,
        )
        _verify_reproduction(label, result, expected_return)
        baseline_results[label] = result
        diagnostics.append(
            _analyze_period(
                store=store,
                config=config,
                label=label,
                signal_cutoff=cutoff,
                result=result,
                score_fn=score_fn,
                instruments=instruments,
            )
        )
        completed += 1
        if progress is not None:
            progress(f"baseline:{label}", completed, total_runs)

    cost_scenarios = [
        CostScenarioResult(
            scenario_id="declared_base",
            commission_rate=config.costs.commission_rate,
            minimum_commission=config.costs.min_commission,
            slippage_bps=config.costs.slippage_bps,
            periods=[
                _period_summary(label, cutoff, baseline_results[label])
                for label, _start, _end, cutoff, _expected in windows
            ],
        )
    ]
    for scenario_id, commission_multiplier, slippage_multiplier in (
        ("moderate_2x_commission_2x_slippage", 2.0, 2.0),
        ("severe_4x_commission_5x_slippage", 4.0, 5.0),
    ):
        scenario_config = _cost_stress_config(
            config,
            scenario_id=scenario_id,
            commission_multiplier=commission_multiplier,
            slippage_multiplier=slippage_multiplier,
        )
        period_results: list[PeriodResult] = []
        for label, start, end, cutoff, _expected_return in windows:
            result = run_backtest_with_signal_cutoff(
                store=store,
                config=scenario_config,
                start=start,
                end=end,
                signal_cutoff=cutoff,
                score_fn=score_fn,
            )
            if result.open_positions_at_end:
                raise ValueError(f"cost stress {scenario_id}:{label} ended with open positions")
            period_results.append(_period_summary(label, cutoff, result))
            completed += 1
            if progress is not None:
                progress(f"{scenario_id}:{label}", completed, total_runs)
        cost_scenarios.append(
            CostScenarioResult(
                scenario_id=scenario_id,
                commission_rate=scenario_config.costs.commission_rate,
                minimum_commission=scenario_config.costs.min_commission,
                slippage_bps=scenario_config.costs.slippage_bps,
                periods=period_results,
            )
        )

    gates = _robustness_gates(diagnostics, cost_scenarios)
    hard_failure = any(not gate.passed for gate in gates)
    training = next(item for item in diagnostics if item.label == "training")
    holdout = next(item for item in diagnostics if item.label == "holdout")
    warnings = _diagnostic_warnings(
        training, holdout, declared_max_positions=config.portfolio.max_positions
    )
    if hard_failure:
        status = "NO_GO"
        reason = "one or more predeclared risk or cost-stress guardrails failed"
    elif warnings:
        status = "CONDITIONAL_GO"
        reason = "; ".join(warnings)
    else:
        status = "GO"
        reason = "all hard guardrails passed without a negative training or benchmark warning"
    return FrozenPortfolioRobustnessReport(
        selected_config_hash=config.config_hash(),
        selection_report_path=str(selection_report_path),
        data_snapshot_id=store.snapshot().snapshot_id,
        initial_cash=config.portfolio.initial_cash,
        max_positions=config.portfolio.max_positions,
        holding_days=config.trade.max_holding_days,
        signal_interval_days=config.trade.signal_interval_days,
        periods=diagnostics,
        cost_scenarios=cost_scenarios,
        gates=gates,
        warnings=warnings,
        status=status,
        status_reason=reason,
    )


def write_frozen_portfolio_robustness_report(
    report: FrozenPortfolioRobustnessReport, output: Path
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")


def _validate_and_windows(
    store: MarketStore,
    config: StrategyConfig,
    report: PortfolioConstructionReport,
) -> list[tuple[str, date, date, date, float]]:
    if config.config_hash() != report.selected_config_hash:
        raise ValueError("frozen config hash does not match the portfolio selection report")
    if store.snapshot().snapshot_id != report.data_snapshot_id:
        raise ValueError("data snapshot does not match the portfolio selection report")
    if config.portfolio.initial_cash != report.initial_cash or report.initial_cash != 80_000:
        raise ValueError("frozen robustness milestone requires the declared 80000 initial cash")
    selected = next(
        (item for item in report.evaluations if item.candidate.candidate_id == report.selected_candidate_id),
        None,
    )
    if selected is None or report.holdout is None:
        raise ValueError("selection report is missing the selected development or holdout period")
    if selected.candidate.config_hash != config.config_hash():
        raise ValueError("selected candidate hash does not match the frozen config")
    return [
        (
            "training",
            selected.training.start,
            selected.training.end,
            selected.training.signal_cutoff,
            selected.training.total_return,
        ),
        (
            "validation",
            selected.validation.start,
            selected.validation.end,
            selected.validation.signal_cutoff,
            selected.validation.total_return,
        ),
        (
            "holdout",
            report.holdout.period.start,
            report.holdout.period.end,
            report.holdout.period.signal_cutoff,
            report.holdout.period.total_return,
        ),
    ]


def _verify_reproduction(label: str, result: BacktestResult, expected_return: float) -> None:
    if result.open_positions_at_end:
        raise ValueError(f"reproduced {label} period ended with open positions")
    if abs(result.metrics.total_return - expected_return) > 1e-12:
        raise ValueError(
            f"reproduced {label} return differs from the frozen selection report: "
            f"{result.metrics.total_return} != {expected_return}"
        )


def _analyze_period(
    *,
    store: MarketStore,
    config: StrategyConfig,
    label: str,
    signal_cutoff: date,
    result: BacktestResult,
    score_fn: ScoreFn,
    instruments: dict[str, Instrument],
) -> PeriodRobustness:
    initial_cash = config.portfolio.initial_cash
    symbols = _symbol_attribution(result.trades, instruments, initial_cash)
    sectors = _sector_attribution(symbols, initial_cash)
    regimes = _regime_attribution(store, result, score_fn, initial_cash)
    months = _monthly_attribution(result.trades, initial_cash)
    losses = sorted((-item.net_pnl for item in symbols if item.net_pnl < 0), reverse=True)
    total_losses = sum(losses)
    top_three_share = sum(losses[:3]) / total_losses if total_losses else 0.0
    absolute_pnl = [abs(item.net_pnl) for item in symbols]
    absolute_total = sum(absolute_pnl)
    symbol_hhi = (
        sum((value / absolute_total) ** 2 for value in absolute_pnl) if absolute_total else 0.0
    )
    sector_hhi = sum(item.entry_notional_share**2 for item in sectors)
    net_trade_pnl = sum(trade.pnl for trade in result.trades)
    equity_change = result.metrics.final_equity - initial_cash
    benchmark = evaluate_price_index_benchmark(
        store=store,
        symbol=config.data.market_index,
        start=result.start,
        end=result.end,
    )
    return PeriodRobustness(
        label=label,
        strategy=_period_summary(label, signal_cutoff, result),
        benchmark=benchmark,
        return_minus_benchmark=result.metrics.total_return - benchmark.total_return,
        exposure=_exposure_summary(result),
        maximum_drawdown_episode=_maximum_drawdown_episode(result),
        symbols=symbols,
        sectors=sectors,
        regimes=regimes,
        months=months,
        largest_symbol_loss_to_initial_cash=(losses[0] / initial_cash if losses else 0.0),
        top_three_symbol_loss_share=top_three_share,
        symbol_absolute_pnl_hhi=symbol_hhi,
        largest_sector_entry_notional_share=(
            max((item.entry_notional_share for item in sectors), default=0.0)
        ),
        sector_entry_notional_hhi=sector_hhi,
        pnl_reconciliation_error=equity_change - net_trade_pnl,
    )


def _symbol_attribution(
    trades: list[TradeFill], instruments: dict[str, Instrument], initial_cash: float
) -> list[SymbolAttribution]:
    grouped: dict[str, list[TradeFill]] = defaultdict(list)
    for trade in trades:
        grouped[trade.symbol].append(trade)
    out: list[SymbolAttribution] = []
    for symbol, rows in grouped.items():
        instrument = instruments.get(symbol)
        name = str(getattr(instrument, "name", symbol))
        sector = str(getattr(instrument, "sector", "unknown"))
        gross = sum(_gross_pnl(trade) for trade in rows)
        net = sum(trade.pnl for trade in rows)
        costs = sum(_trade_costs(trade) for trade in rows)
        out.append(
            SymbolAttribution(
                symbol=symbol,
                name=name,
                sector=sector,
                trades=len(rows),
                wins=sum(trade.pnl > 0 for trade in rows),
                entry_notional=sum(_entry_notional(trade) for trade in rows),
                gross_pnl=gross,
                net_pnl=net,
                trading_costs=costs,
                contribution_to_initial_cash=net / initial_cash,
            )
        )
    return sorted(out, key=lambda item: (item.net_pnl, item.symbol))


def _sector_attribution(
    symbols: list[SymbolAttribution], initial_cash: float
) -> list[SectorAttribution]:
    grouped: dict[str, list[SymbolAttribution]] = defaultdict(list)
    for item in symbols:
        grouped[item.sector].append(item)
    total_notional = sum(item.entry_notional for item in symbols)
    rows = [
        SectorAttribution(
            sector=sector,
            trades=sum(item.trades for item in items),
            symbols=len(items),
            entry_notional=sum(item.entry_notional for item in items),
            entry_notional_share=(
                sum(item.entry_notional for item in items) / total_notional if total_notional else 0.0
            ),
            net_pnl=sum(item.net_pnl for item in items),
            contribution_to_initial_cash=sum(item.net_pnl for item in items) / initial_cash,
        )
        for sector, items in grouped.items()
    ]
    return sorted(rows, key=lambda item: (-item.entry_notional_share, item.sector))


def _regime_attribution(
    store: MarketStore,
    result: BacktestResult,
    score_fn: ScoreFn,
    initial_cash: float,
) -> list[RegimeAttribution]:
    calendar = store.get_calendar(result.start, result.end)
    index = {day: offset for offset, day in enumerate(calendar)}
    grouped: dict[str, list[TradeFill]] = defaultdict(list)
    regime_cache: dict[date, str] = {}
    for trade in result.trades:
        offset = index.get(trade.entry_date)
        if offset is None or offset <= 0:
            raise ValueError(f"cannot identify signal day for trade {trade.symbol} {trade.entry_date}")
        signal_day = calendar[offset - 1]
        regime = regime_cache.get(signal_day)
        if regime is None:
            scores = score_fn(signal_day)
            if not scores:
                raise ValueError(f"missing cached score cross-section for signal day {signal_day}")
            value = scores[0].breakdown.regime_score
            regime = _regime_label(value if value is not None else scores[0].breakdown.market_score)
            regime_cache[signal_day] = regime
        grouped[regime].append(trade)
    rows = [
        RegimeAttribution(
            regime=regime,
            trades=len(trades),
            entry_notional=sum(_entry_notional(trade) for trade in trades),
            net_pnl=sum(trade.pnl for trade in trades),
            contribution_to_initial_cash=sum(trade.pnl for trade in trades) / initial_cash,
        )
        for regime, trades in grouped.items()
    ]
    return sorted(rows, key=lambda item: item.regime)


def _monthly_attribution(trades: list[TradeFill], initial_cash: float) -> list[MonthlyAttribution]:
    grouped: dict[str, list[TradeFill]] = defaultdict(list)
    for trade in trades:
        grouped[trade.exit_date.strftime("%Y-%m")].append(trade)
    return [
        MonthlyAttribution(
            month=month,
            trades=len(rows),
            net_pnl=sum(trade.pnl for trade in rows),
            contribution_to_initial_cash=sum(trade.pnl for trade in rows) / initial_cash,
        )
        for month, rows in sorted(grouped.items())
    ]


def _exposure_summary(result: BacktestResult) -> ExposureSummary:
    fractions = [
        point.market_value / point.equity if point.equity > 0 else 0.0
        for point in result.equity_curve
    ]
    counts = [
        sum(trade.entry_date <= point.date < trade.exit_date for trade in result.trades)
        for point in result.equity_curve
    ]
    invested = sum(value > 1e-12 for value in fractions)
    return ExposureSummary(
        trading_days=len(fractions),
        invested_days=invested,
        invested_day_rate=invested / len(fractions) if fractions else 0.0,
        average_invested_fraction=sum(fractions) / len(fractions) if fractions else 0.0,
        peak_invested_fraction=max(fractions, default=0.0),
        average_open_positions=sum(counts) / len(counts) if counts else 0.0,
        peak_open_positions=max(counts, default=0),
    )


def _maximum_drawdown_episode(result: BacktestResult) -> DrawdownEpisode:
    points = result.equity_curve
    if not points:
        raise ValueError("drawdown analysis requires an equity curve")
    peak_value = points[0].equity
    peak_date = points[0].date
    worst = 0.0
    worst_peak_date = peak_date
    trough_date = peak_date
    peak_for_recovery = peak_value
    for point in points:
        if point.equity > peak_value:
            peak_value = point.equity
            peak_date = point.date
        drawdown = point.equity / peak_value - 1.0 if peak_value > 0 else 0.0
        if drawdown < worst:
            worst = drawdown
            worst_peak_date = peak_date
            trough_date = point.date
            peak_for_recovery = peak_value
    recovery = next(
        (point.date for point in points if point.date > trough_date and point.equity >= peak_for_recovery),
        None,
    )
    return DrawdownEpisode(
        peak_date=worst_peak_date,
        trough_date=trough_date,
        recovery_date=recovery,
        drawdown=worst,
    )


def _cost_stress_config(
    config: StrategyConfig,
    *,
    scenario_id: str,
    commission_multiplier: float,
    slippage_multiplier: float,
) -> StrategyConfig:
    if commission_multiplier < 1 or slippage_multiplier < 1:
        raise ValueError("cost stress multipliers must be at least one")
    return config.model_copy(
        update={
            "config_id": f"{config.run_id()}__{scenario_id}",
            "costs": config.costs.model_copy(
                update={
                    "commission_rate": config.costs.commission_rate * commission_multiplier,
                    "slippage_bps": config.costs.slippage_bps * slippage_multiplier,
                }
            ),
        }
    )


def _robustness_gates(
    periods: list[PeriodRobustness], scenarios: list[CostScenarioResult]
) -> list[RobustnessGate]:
    training = next(item for item in periods if item.label == "training")
    holdout = next(item for item in periods if item.label == "holdout")
    severe = next(item for item in scenarios if item.scenario_id.startswith("severe_"))
    severe_validation = next(item for item in severe.periods if item.label == "validation")
    severe_holdout = next(item for item in severe.periods if item.label == "holdout")
    return [
        RobustnessGate(
            gate="all_periods_closed",
            passed=all(item.strategy.open_positions_at_end == 0 for item in periods),
            observed=str([item.strategy.open_positions_at_end for item in periods]),
            threshold="all equal 0",
            note="closed-window comparison must include all realized costs",
        ),
        RobustnessGate(
            gate="training_max_drawdown",
            passed=training.strategy.max_drawdown is not None
            and training.strategy.max_drawdown >= -0.15,
            observed=training.strategy.max_drawdown or 0.0,
            threshold=-0.15,
            note="training-period drawdown must not exceed 15 percent",
        ),
        RobustnessGate(
            gate="largest_holdout_symbol_loss",
            passed=holdout.largest_symbol_loss_to_initial_cash <= 0.03,
            observed=holdout.largest_symbol_loss_to_initial_cash,
            threshold=0.03,
            note="one symbol must not lose more than 3 percent of initial capital",
        ),
        RobustnessGate(
            gate="severe_cost_validation_positive",
            passed=severe_validation.total_return > 0,
            observed=severe_validation.total_return,
            threshold=0.0,
            note="validation return must remain positive under severe declared costs",
        ),
        RobustnessGate(
            gate="severe_cost_holdout_positive",
            passed=severe_holdout.total_return > 0,
            observed=severe_holdout.total_return,
            threshold=0.0,
            note="holdout return must remain positive under severe declared costs",
        ),
        RobustnessGate(
            gate="pnl_reconciliation",
            passed=all(abs(item.pnl_reconciliation_error) <= 1e-6 for item in periods),
            observed=max(abs(item.pnl_reconciliation_error) for item in periods),
            threshold=1e-6,
            note="closed trade PnL must reconcile to final equity",
        ),
    ]


def _diagnostic_warnings(
    training: PeriodRobustness,
    holdout: PeriodRobustness,
    *,
    declared_max_positions: int,
) -> list[str]:
    warnings: list[str] = []
    if training.strategy.total_return < 0:
        warnings.append("training window total return is negative")
    if holdout.return_minus_benchmark < 0:
        warnings.append("holdout return is below the declared price-index benchmark")
    if holdout.largest_sector_entry_notional_share > 0.50:
        warnings.append(
            "holdout current-sector attribution exceeds 50 percent in one sector; taxonomy is not PIT"
        )
    if holdout.exposure.average_invested_fraction < 0.35:
        warnings.append("holdout average invested capital is below 35 percent")
    if holdout.exposure.peak_open_positions < declared_max_positions:
        warnings.append(
            "holdout never reaches the declared "
            f"{declared_max_positions}-position capacity"
        )
    return warnings


def _period_summary(label: str, signal_cutoff: date, result: BacktestResult) -> PeriodResult:
    metrics = result.metrics
    return PeriodResult(
        label=label,
        start=result.start,
        end=result.end,
        signal_cutoff=signal_cutoff,
        total_return=metrics.total_return,
        annualized_return=metrics.annualized_return,
        sharpe_ratio=metrics.sharpe_ratio,
        max_drawdown=metrics.max_drawdown,
        number_of_trades=metrics.number_of_trades,
        win_rate=metrics.win_rate,
        final_equity=metrics.final_equity,
        total_trading_costs=result.attribution.total_trading_costs,
        orders_generated=result.attribution.signal.orders_generated,
        orders_filled=result.attribution.signal.orders_filled,
        open_positions_at_end=result.open_positions_at_end,
    )


def _entry_notional(trade: TradeFill) -> float:
    raw = trade.entry_raw_price if trade.entry_raw_price is not None else trade.entry_price
    return raw * trade.shares


def _gross_pnl(trade: TradeFill) -> float:
    if trade.gross_pnl is not None:
        return trade.gross_pnl
    return trade.pnl + trade.buy_commission + trade.sell_commission + trade.stamp_tax


def _trade_costs(trade: TradeFill) -> float:
    return (
        trade.buy_commission
        + trade.sell_commission
        + trade.stamp_tax
        + trade.buy_slippage
        + trade.sell_slippage
    )


def _regime_label(score: float) -> str:
    if score < 40:
        return "00_weak_lt_40"
    if score < 55:
        return "01_cautious_40_55"
    if score < 70:
        return "02_normal_55_70"
    return "03_strong_ge_70"
