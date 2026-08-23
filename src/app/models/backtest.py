from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

ExitReason = Literal["take_profit", "stop_loss", "timeout"]


class TradeFill(BaseModel):
    symbol: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    return_pct: float
    holding_days: int
    exit_reason: ExitReason
    buy_commission: float
    sell_commission: float
    stamp_tax: float


class EquityPoint(BaseModel):
    date: date
    cash: float
    market_value: float
    equity: float


class BacktestMetrics(BaseModel):
    initial_capital: float
    final_equity: float
    total_return: float
    annualized_return: float | None
    number_of_trades: int
    win_rate: float | None
    average_win: float | None
    average_loss: float | None
    profit_factor: float | None
    expectancy: float | None
    average_holding_days: float | None
    max_drawdown: float | None
    sharpe_ratio: float | None
    tp_exit_count: int
    sl_exit_count: int
    timeout_exit_count: int


class BacktestResult(BaseModel):
    strategy_name: str
    strategy_version: str
    strategy_config_hash: str
    start: date
    end: date
    metrics: BacktestMetrics
    trades: list[TradeFill] = Field(default_factory=list)
    equity_curve: list[EquityPoint] = Field(default_factory=list)
