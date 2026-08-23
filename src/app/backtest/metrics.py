from __future__ import annotations

import math
from datetime import date

from app.models.backtest import BacktestMetrics, EquityPoint, TradeFill

TRADING_DAYS_PER_YEAR = 242


def compute_metrics(
    initial_capital: float,
    trades: list[TradeFill],
    equity_curve: list[EquityPoint],
    start: date,  # noqa: ARG001
    end: date,  # noqa: ARG001
) -> BacktestMetrics:
    final_equity = equity_curve[-1].equity if equity_curve else initial_capital
    total_return = final_equity / initial_capital - 1.0 if initial_capital else 0.0
    n_days = max(len(equity_curve) - 1, 0)
    if n_days > 0 and final_equity > 0 and initial_capital > 0:
        annualized = (final_equity / initial_capital) ** (TRADING_DAYS_PER_YEAR / n_days) - 1.0
    else:
        annualized = None

    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    n_trades = len(trades)
    win_rate = len(wins) / n_trades if n_trades else None
    average_win = sum(wins) / len(wins) if wins else None
    average_loss = sum(losses) / len(losses) if losses else None
    sum_win = sum(wins)
    sum_loss_abs = abs(sum(losses))
    if n_trades == 0:
        profit_factor = None
    elif sum_loss_abs == 0:
        profit_factor = None if sum_win == 0 else None
    else:
        profit_factor = sum_win / sum_loss_abs
    # If there are wins and no losses, profit factor is undefined; keep None.
    if n_trades and wins and not losses:
        profit_factor = None
    expectancy = (sum(t.pnl for t in trades) / n_trades) if n_trades else None
    avg_hold = (sum(t.holding_days for t in trades) / n_trades) if n_trades else None

    max_dd = _max_drawdown([p.equity for p in equity_curve])
    sharpe = _sharpe([p.equity for p in equity_curve])

    return BacktestMetrics(
        initial_capital=initial_capital,
        final_equity=final_equity,
        total_return=total_return,
        annualized_return=annualized,
        number_of_trades=n_trades,
        win_rate=win_rate,
        average_win=average_win,
        average_loss=average_loss,
        profit_factor=profit_factor,
        expectancy=expectancy,
        average_holding_days=avg_hold,
        max_drawdown=max_dd,
        sharpe_ratio=sharpe,
        tp_exit_count=sum(1 for t in trades if t.exit_reason == "take_profit"),
        sl_exit_count=sum(1 for t in trades if t.exit_reason == "stop_loss"),
        timeout_exit_count=sum(1 for t in trades if t.exit_reason == "timeout"),
    )


def _max_drawdown(equity: list[float]) -> float | None:
    if not equity:
        return None
    peak = equity[0]
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = min(max_dd, value / peak - 1.0)
    return max_dd


def _sharpe(equity: list[float]) -> float | None:
    if len(equity) < 3:
        return None
    rets: list[float] = []
    for prev, curr in zip(equity, equity[1:], strict=False):
        if prev > 0:
            rets.append(curr / prev - 1.0)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    return (mean / std) * math.sqrt(TRADING_DAYS_PER_YEAR)
