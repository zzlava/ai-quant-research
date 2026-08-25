from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.models.snapshot import DataSnapshot

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
    entry_raw_price: float | None = None
    exit_raw_price: float | None = None
    buy_slippage: float = 0.0
    sell_slippage: float = 0.0
    gross_pnl: float | None = None


class EquityPoint(BaseModel):
    date: date
    cash: float
    market_value: float
    equity: float


class BacktestWindow(BaseModel):
    start: date
    signal_end: date | None
    entry_end: date
    valuation_end: date


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


class SignalAttribution(BaseModel):
    scoring_days: int = 0
    names_ranked: int = 0
    orders_generated: int = 0
    orders_filled: int = 0
    orders_deferred: int = 0
    entry_deferral_days: int = 0
    orders_filled_after_deferral: int = 0
    deferred_orders_expired: int = 0
    rejected_by_regime_gate: int = 0
    rejected_by_ranking_threshold: int = 0
    rejected_by_cooldown: int = 0
    rejected_suspended: int = 0
    rejected_at_limit: int = 0
    rejected_unaffordable: int = 0


class BacktestAttribution(BaseModel):
    gross_realized_pnl: float = 0.0
    net_realized_pnl: float = 0.0
    buy_commission: float = 0.0
    sell_commission: float = 0.0
    stamp_tax: float = 0.0
    estimated_slippage: float = 0.0
    explicit_costs: float = 0.0
    total_trading_costs: float = 0.0
    signal: SignalAttribution = Field(default_factory=SignalAttribution)


class BacktestResult(BaseModel):
    strategy_name: str
    strategy_version: str
    strategy_config_hash: str
    start: date
    end: date
    window: BacktestWindow
    metrics: BacktestMetrics
    trades: list[TradeFill] = Field(default_factory=list)
    equity_curve: list[EquityPoint] = Field(default_factory=list)
    open_positions_at_end: int = 0
    data_snapshot: DataSnapshot | None = None
    data_snapshot_id: str = ""
    research_scope: str = "historical_index"
    research_notice: str | None = None
    reconstruction_data_id: str | None = None
    attribution: BacktestAttribution = Field(default_factory=BacktestAttribution)
    # Optional one-shot portfolio OOS scenario cost bindings (absent outside that writer).
    portfolio_oos_scenario_id: str | None = None
    portfolio_oos_commission_rate: float | None = None
    portfolio_oos_minimum_commission: float | None = None
    portfolio_oos_slippage_bps: float | None = None
    portfolio_oos_stamp_tax_unchanged: bool | None = None
