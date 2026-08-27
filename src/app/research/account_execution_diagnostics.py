"""Read-only account / execution structure diagnostics (E7b).

Separates minimum-commission, lot affordability, explicit costs/slippage, budget
underfill, and signal-funnel attribution from stock-selection alpha. Never
modifies strategy, costs, or execution rules. Never authorizes scoring, backtest,
or trading.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.backtest.costs import apply_slippage, buy_cost, commission, shares_affordable
from app.models.backtest import BacktestResult, TradeFill
from app.models.config import CostConfig

ACCOUNT_EXECUTION_SCHEMA_VERSION: Literal["1"] = "1"
ACCOUNT_EXECUTION_DIAGNOSTIC_VERSION: Literal["account-execution-diagnostic-v1"] = "account-execution-diagnostic-v1"
CANDIDATE_LOT_AFFORDABILITY_SCHEMA_VERSION: Literal["1"] = "1"
CANDIDATE_LOT_AFFORDABILITY_DIAGNOSTIC_VERSION: Literal["candidate-lot-affordability-diagnostic-v1"] = (
    "candidate-lot-affordability-diagnostic-v1"
)

INTEGRITY_ONLY_VERIFIER_NOTE: Literal[
    "File verifier is integrity-only: self-hash and sealed-field consistency. "
    "It does not embed or re-validate a full BacktestResult; call "
    "diagnose_account_execution on the source result for full fail-closed validation."
] = (
    "File verifier is integrity-only: self-hash and sealed-field consistency. "
    "It does not embed or re-validate a full BacktestResult; call "
    "diagnose_account_execution on the source result for full fail-closed validation."
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OptionalRatio(_StrictModel):
    """Nullable ratio with an explicit reason when mathematically undefined."""

    value: float | None
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def _value_or_reason(self) -> OptionalRatio:
        if self.value is None:
            if self.unavailable_reason is None or self.unavailable_reason.strip() == "":
                raise ValueError("unavailable ratio must include unavailable_reason")
            return self
        if not math.isfinite(self.value):
            raise ValueError("ratio value must be finite when present")
        if self.unavailable_reason is not None:
            raise ValueError("available ratio must not set unavailable_reason")
        return self


class AccountExecutionDiagnosticReport(_StrictModel):
    """Sealed account/execution attribution; never authorizes alpha or trading."""

    schema_version: Literal["1"] = ACCOUNT_EXECUTION_SCHEMA_VERSION
    diagnostic_version: Literal["account-execution-diagnostic-v1"] = ACCOUNT_EXECUTION_DIAGNOSTIC_VERSION
    report_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    strategy_name: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    strategy_config_hash: str = Field(min_length=1)
    data_snapshot_id: str
    result_start: date
    result_end: date
    window_start: date
    window_signal_end: date | None
    window_entry_end: date
    window_valuation_end: date
    source_result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    commission_rate: float
    minimum_commission: float
    slippage_bps: float
    lot_size: int = Field(ge=1)
    numerical_tolerance: float
    closed_trade_count: int = Field(ge=0)
    buy_side_count: int = Field(ge=0)
    sell_side_count: int = Field(ge=0)
    entry_notional_total: float
    exit_notional_total: float
    commission_parameter_semantics: str = Field(min_length=1)
    buy_minimum_commission_binding_count: int = Field(ge=0)
    sell_minimum_commission_binding_count: int = Field(ge=0)
    buy_minimum_commission_binding_fraction: OptionalRatio
    sell_minimum_commission_binding_fraction: OptionalRatio
    buy_effective_commission_bps_notional_weighted: OptionalRatio
    sell_effective_commission_bps_notional_weighted: OptionalRatio
    buy_effective_commission_bps_per_side: OptionalRatio
    sell_effective_commission_bps_per_side: OptionalRatio
    explicit_costs: float
    estimated_slippage: float
    total_trading_costs: float
    gross_realized_pnl: float
    net_realized_pnl: float
    cost_drag_vs_initial_capital: OptionalRatio
    cost_to_gross_ratio: OptionalRatio
    per_trade_gross_net_identity_status: Literal["verified", "unavailable_legacy"]
    per_trade_gross_net_identity_unavailable_reason: str | None = None
    rejected_unaffordable: int = Field(ge=0)
    rejected_insufficient_cash: int = Field(ge=0)
    orders_generated: int = Field(ge=0)
    entry_attempts: int = Field(ge=0)
    orders_filled: int = Field(ge=0)
    target_entry_budget_total: float
    actual_entry_cash_used_total: float
    unallocated_entry_budget_total: float
    overallocated_entry_budget_total: float
    lot_compliant_trade_count: int = Field(ge=0)
    lot_violation_count: int = Field(ge=0)
    file_verifier_scope: Literal["integrity_only"] = "integrity_only"
    file_verifier_note: Literal[
        "File verifier is integrity-only: self-hash and sealed-field consistency. "
        "It does not embed or re-validate a full BacktestResult; call "
        "diagnose_account_execution on the source result for full fail-closed validation."
    ] = INTEGRITY_ONLY_VERIFIER_NOTE
    diagnostic_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator(
        "commission_rate",
        "minimum_commission",
        "slippage_bps",
        "numerical_tolerance",
        "entry_notional_total",
        "exit_notional_total",
        "explicit_costs",
        "estimated_slippage",
        "total_trading_costs",
        "gross_realized_pnl",
        "net_realized_pnl",
        "target_entry_budget_total",
        "actual_entry_cash_used_total",
        "unallocated_entry_budget_total",
        "overallocated_entry_budget_total",
    )
    @classmethod
    def _finite_number(cls, value: float, info: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{info.field_name} must be a finite number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{info.field_name} must be finite")
        return number

    @field_validator("strategy_name", "strategy_version", "strategy_config_hash", "data_snapshot_id")
    @classmethod
    def _nonblank_binding(cls, value: str, info: Any) -> str:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError(f"{info.field_name} must be a nonblank string")
        return value

    @model_validator(mode="after")
    def _gate_flags_and_counts(self) -> AccountExecutionDiagnosticReport:
        if self.diagnostic_only is not True:
            raise ValueError("diagnostic_only must remain true")
        if self.ready_for_scoring or self.ready_for_backtest or self.ready_for_trading or self.auto_apply:
            raise ValueError("scoring/backtest/trading/auto_apply must remain false")
        for name in (
            "commission_rate",
            "minimum_commission",
            "slippage_bps",
            "numerical_tolerance",
            "entry_notional_total",
            "exit_notional_total",
            "explicit_costs",
            "estimated_slippage",
            "total_trading_costs",
            "target_entry_budget_total",
            "actual_entry_cash_used_total",
            "unallocated_entry_budget_total",
            "overallocated_entry_budget_total",
        ):
            if getattr(self, name) < 0.0:
                raise ValueError(f"{name} must be nonnegative")
        if self.numerical_tolerance < 0.0:
            raise ValueError("numerical_tolerance must be nonnegative")
        if self.buy_side_count != self.closed_trade_count or self.sell_side_count != self.closed_trade_count:
            raise ValueError("buy/sell side counts must equal closed_trade_count")
        if self.lot_compliant_trade_count + self.lot_violation_count != self.closed_trade_count:
            raise ValueError("lot compliance counts must partition closed_trade_count")
        if self.lot_violation_count != 0:
            raise ValueError("sealed report must not contain lot violations")
        if self.buy_minimum_commission_binding_count > self.closed_trade_count:
            raise ValueError("buy_minimum_commission_binding_count cannot exceed closed_trade_count")
        if self.sell_minimum_commission_binding_count > self.closed_trade_count:
            raise ValueError("sell_minimum_commission_binding_count cannot exceed closed_trade_count")
        _assert_optional_ratio_unit_interval(self.buy_minimum_commission_binding_fraction, "buy binding fraction")
        _assert_optional_ratio_unit_interval(self.sell_minimum_commission_binding_fraction, "sell binding fraction")
        if self.closed_trade_count == 0:
            for ratio, label in (
                (self.buy_minimum_commission_binding_fraction, "buy binding fraction"),
                (self.sell_minimum_commission_binding_fraction, "sell binding fraction"),
                (self.buy_effective_commission_bps_notional_weighted, "buy notional-weighted bps"),
                (self.sell_effective_commission_bps_notional_weighted, "sell notional-weighted bps"),
                (self.buy_effective_commission_bps_per_side, "buy per-side bps"),
                (self.sell_effective_commission_bps_per_side, "sell per-side bps"),
            ):
                if ratio.value is not None:
                    raise ValueError(f"zero-trade report requires null {label} with reason")
                if ratio.unavailable_reason is None or ratio.unavailable_reason.strip() == "":
                    raise ValueError(f"zero-trade report requires explicit reason for null {label}")
        if self.per_trade_gross_net_identity_status == "verified":
            if self.per_trade_gross_net_identity_unavailable_reason is not None:
                raise ValueError("verified identity must not set unavailable_reason")
            expected_net = self.gross_realized_pnl - self.total_trading_costs
            if not math.isclose(expected_net, self.net_realized_pnl, rel_tol=0.0, abs_tol=self.numerical_tolerance):
                raise ValueError(
                    "verified identity requires gross_realized_pnl - total_trading_costs == net_realized_pnl"
                )
        elif (
            self.per_trade_gross_net_identity_unavailable_reason is None
            or self.per_trade_gross_net_identity_unavailable_reason.strip() == ""
        ):
            raise ValueError("unavailable_legacy identity requires an explicit reason")
        left = self.target_entry_budget_total + self.overallocated_entry_budget_total
        right = self.actual_entry_cash_used_total + self.unallocated_entry_budget_total
        if not math.isclose(left, right, rel_tol=0.0, abs_tol=self.numerical_tolerance):
            raise ValueError(
                "entry budget identity drift: target + overallocated != actual + unallocated "
                f"within tolerance (left={left}, right={right})"
            )
        return self


class CandidatePriceRow(_StrictModel):
    symbol: str = Field(min_length=1)
    raw_price: float

    @field_validator("symbol")
    @classmethod
    def _canonical_symbol(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("symbol must be a string")
        symbol = value.strip()
        if symbol == "":
            raise ValueError("symbol must be nonblank after stripping whitespace")
        return symbol

    @field_validator("raw_price")
    @classmethod
    def _positive_finite_price(cls, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("raw_price must be a finite number")
        number = float(value)
        if not math.isfinite(number) or number <= 0.0:
            raise ValueError("raw_price must be finite and positive")
        return number


class CandidateLotAffordabilityRow(_StrictModel):
    symbol: str = Field(min_length=1)
    raw_price: float
    fill_price: float
    shares_affordable: int = Field(ge=0)
    lots_affordable: int = Field(ge=0)
    unused_cash: float
    can_afford_one_lot: bool

    @field_validator("symbol")
    @classmethod
    def _canonical_symbol(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("symbol must be a string")
        symbol = value.strip()
        if symbol == "":
            raise ValueError("symbol must be nonblank after stripping whitespace")
        return symbol

    @field_validator("raw_price", "fill_price", "unused_cash")
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{info.field_name} must be a finite number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{info.field_name} must be finite")
        return number


class CandidateLotAffordabilityReport(_StrictModel):
    """Sealed synthetic candidate-lot affordability diagnostic; embeds inputs."""

    schema_version: Literal["1"] = CANDIDATE_LOT_AFFORDABILITY_SCHEMA_VERSION
    diagnostic_version: Literal["candidate-lot-affordability-diagnostic-v1"] = (
        CANDIDATE_LOT_AFFORDABILITY_DIAGNOSTIC_VERSION
    )
    report_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidates: list[CandidatePriceRow]
    cash_per_slice: float
    commission_rate: float
    minimum_commission: float
    slippage_bps: float
    lot_size: int = Field(ge=1)
    rows: list[CandidateLotAffordabilityRow]
    stamp_tax_irrelevant_for_buys: Literal[True] = True
    diagnostic_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator(
        "cash_per_slice",
        "commission_rate",
        "minimum_commission",
        "slippage_bps",
    )
    @classmethod
    def _finite_nonnegative(cls, value: float, info: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{info.field_name} must be a finite number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{info.field_name} must be finite")
        if number < 0.0:
            raise ValueError(f"{info.field_name} must be nonnegative")
        return number

    @model_validator(mode="after")
    def _gate_and_unique(self) -> CandidateLotAffordabilityReport:
        if self.diagnostic_only is not True:
            raise ValueError("diagnostic_only must remain true")
        if self.ready_for_scoring or self.ready_for_backtest or self.ready_for_trading or self.auto_apply:
            raise ValueError("scoring/backtest/trading/auto_apply must remain false")
        if self.cash_per_slice <= 0.0:
            raise ValueError("cash_per_slice must be > 0")
        symbols = [row.symbol for row in self.candidates]
        if len(symbols) != len(set(symbols)):
            raise ValueError("candidate symbols must be unique")
        if len(self.rows) != len(self.candidates):
            raise ValueError("rows length must equal candidates length")
        for candidate, row in zip(self.candidates, self.rows, strict=True):
            if candidate.symbol != row.symbol or candidate.raw_price != row.raw_price:
                raise ValueError("rows must align with embedded candidates")
        return self


def diagnose_account_execution(
    result: BacktestResult,
    *,
    commission_rate: float,
    minimum_commission: float,
    slippage_bps: float,
    lot_size: int,
    numerical_tolerance: float,
) -> AccountExecutionDiagnosticReport:
    """Build a sealed account/execution diagnostic from an explicit BacktestResult.

    All economic parameters are required; there are no defaults. This function is
    read-only attribution and never mutates strategy or cost settings.
    """
    rate = _require_finite_nonnegative(commission_rate, field_name="commission_rate")
    minimum = _require_finite_nonnegative(minimum_commission, field_name="minimum_commission")
    slip_bps = _require_finite_nonnegative(slippage_bps, field_name="slippage_bps")
    lot = _require_positive_int(lot_size, field_name="lot_size")
    tol = _require_finite_nonnegative(numerical_tolerance, field_name="numerical_tolerance")

    strategy_name = _require_nonblank(result.strategy_name, field_name="strategy_name")
    strategy_version = _require_nonblank(result.strategy_version, field_name="strategy_version")
    strategy_config_hash = _require_nonblank(result.strategy_config_hash, field_name="strategy_config_hash")
    data_snapshot_id = _require_nonblank(result.data_snapshot_id, field_name="data_snapshot_id")
    _validate_result_window_dates(result)

    trades = list(result.trades)
    if result.metrics.number_of_trades != len(trades):
        raise ValueError(f"metrics.number_of_trades={result.metrics.number_of_trades} != len(trades)={len(trades)}")

    cost_config = _buy_cost_config(rate=rate, minimum=minimum, slip_bps=slip_bps)
    semantics = _commission_parameter_semantics(rate=rate, minimum=minimum)

    entry_notional_total = 0.0
    exit_notional_total = 0.0
    buy_binding = 0
    sell_binding = 0
    buy_commission_sum = 0.0
    sell_commission_sum = 0.0
    stamp_sum = 0.0
    slippage_sum = 0.0
    net_sum = 0.0
    gross_sum = 0.0
    identity_status: Literal["verified", "unavailable_legacy"] = "verified"
    identity_reason: str | None = None

    for index, trade in enumerate(trades):
        _validate_trade_basics(trade, lot_size=lot, tolerance=tol, trade_index=index)
        entry_notional = trade.entry_price * trade.shares
        exit_notional = trade.exit_price * trade.shares
        entry_notional_total += entry_notional
        exit_notional_total += exit_notional
        buy_commission_sum += trade.buy_commission
        sell_commission_sum += trade.sell_commission
        stamp_sum += trade.stamp_tax
        slippage_sum += trade.buy_slippage + trade.sell_slippage
        net_sum += trade.pnl

        _assert_commission_matches_params(
            notional=entry_notional,
            observed=trade.buy_commission,
            rate=rate,
            minimum=minimum,
            tolerance=tol,
            side="buy",
            trade_index=index,
        )
        _assert_commission_matches_params(
            notional=exit_notional,
            observed=trade.sell_commission,
            rate=rate,
            minimum=minimum,
            tolerance=tol,
            side="sell",
            trade_index=index,
        )
        if _minimum_commission_binds(notional=entry_notional, rate=rate, minimum=minimum, tolerance=tol):
            buy_binding += 1
        if _minimum_commission_binds(notional=exit_notional, rate=rate, minimum=minimum, tolerance=tol):
            sell_binding += 1

        identity = _trade_pnl_identity_availability(trade)
        if identity is not None:
            identity_status = "unavailable_legacy"
            identity_reason = identity
            continue

        assert trade.gross_pnl is not None
        assert trade.entry_raw_price is not None
        assert trade.exit_raw_price is not None
        _assert_slippage_matches_params(
            trade,
            cost_config=cost_config,
            tolerance=tol,
            trade_index=index,
        )
        trading_costs = (
            trade.buy_commission + trade.sell_commission + trade.stamp_tax + trade.buy_slippage + trade.sell_slippage
        )
        expected_net = trade.gross_pnl - trading_costs
        if not math.isclose(expected_net, trade.pnl, rel_tol=0.0, abs_tol=tol):
            raise ValueError(
                f"trade[{index}] gross - trading_costs != net pnl within tolerance "
                f"(expected_net={expected_net}, pnl={trade.pnl})"
            )
        gross_sum += trade.gross_pnl

    if identity_status == "unavailable_legacy":
        # Refuse to invent gross/slippage from legacy gaps; fail closed rather than guess.
        raise ValueError(
            "per-trade gross/net identity unavailable for one or more trades; "
            f"refusing to guess legacy fields ({identity_reason})"
        )

    attribution = result.attribution
    _assert_close(buy_commission_sum, attribution.buy_commission, tol, "buy_commission")
    _assert_close(sell_commission_sum, attribution.sell_commission, tol, "sell_commission")
    _assert_close(stamp_sum, attribution.stamp_tax, tol, "stamp_tax")
    _assert_close(slippage_sum, attribution.estimated_slippage, tol, "estimated_slippage")
    explicit = buy_commission_sum + sell_commission_sum + stamp_sum
    _assert_close(explicit, attribution.explicit_costs, tol, "explicit_costs")
    total_costs = explicit + slippage_sum
    _assert_close(total_costs, attribution.total_trading_costs, tol, "total_trading_costs")
    _assert_close(net_sum, attribution.net_realized_pnl, tol, "net_realized_pnl")
    _assert_close(gross_sum, attribution.gross_realized_pnl, tol, "gross_realized_pnl")

    n_trades = len(trades)
    signal = attribution.signal
    initial_capital = result.metrics.initial_capital
    if not math.isfinite(initial_capital) or initial_capital < 0.0:
        raise ValueError("metrics.initial_capital must be finite and nonnegative")

    target_budget = _require_finite_nonnegative(
        float(signal.target_entry_budget_total), field_name="target_entry_budget_total"
    )
    actual_budget = _require_finite_nonnegative(
        float(signal.actual_entry_cash_used_total), field_name="actual_entry_cash_used_total"
    )
    unallocated_budget = _require_finite_nonnegative(
        float(signal.unallocated_entry_budget_total), field_name="unallocated_entry_budget_total"
    )
    overallocated_budget = _require_finite_nonnegative(
        float(signal.overallocated_entry_budget_total), field_name="overallocated_entry_budget_total"
    )
    left = target_budget + overallocated_budget
    right = actual_budget + unallocated_budget
    if not math.isclose(left, right, rel_tol=0.0, abs_tol=tol):
        raise ValueError(
            "signal entry budget identity drift: target + overallocated != actual + unallocated "
            f"within tolerance (left={left}, right={right})"
        )

    report = AccountExecutionDiagnosticReport(
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        strategy_config_hash=strategy_config_hash,
        data_snapshot_id=data_snapshot_id,
        result_start=result.start,
        result_end=result.end,
        window_start=result.window.start,
        window_signal_end=result.window.signal_end,
        window_entry_end=result.window.entry_end,
        window_valuation_end=result.window.valuation_end,
        source_result_hash=canonical_backtest_result_hash(result),
        commission_rate=rate,
        minimum_commission=minimum,
        slippage_bps=slip_bps,
        lot_size=lot,
        numerical_tolerance=tol,
        closed_trade_count=n_trades,
        buy_side_count=n_trades,
        sell_side_count=n_trades,
        entry_notional_total=entry_notional_total,
        exit_notional_total=exit_notional_total,
        commission_parameter_semantics=semantics,
        buy_minimum_commission_binding_count=buy_binding,
        sell_minimum_commission_binding_count=sell_binding,
        buy_minimum_commission_binding_fraction=_binding_fraction(buy_binding, n_trades, side="buy"),
        sell_minimum_commission_binding_fraction=_binding_fraction(sell_binding, n_trades, side="sell"),
        buy_effective_commission_bps_notional_weighted=_notional_weighted_bps(
            buy_commission_sum, entry_notional_total, side="buy"
        ),
        sell_effective_commission_bps_notional_weighted=_notional_weighted_bps(
            sell_commission_sum, exit_notional_total, side="sell"
        ),
        buy_effective_commission_bps_per_side=_per_side_mean_bps(trades, side="buy", tolerance=tol),
        sell_effective_commission_bps_per_side=_per_side_mean_bps(trades, side="sell", tolerance=tol),
        explicit_costs=explicit,
        estimated_slippage=slippage_sum,
        total_trading_costs=total_costs,
        gross_realized_pnl=gross_sum,
        net_realized_pnl=net_sum,
        cost_drag_vs_initial_capital=_cost_drag(total_costs, initial_capital),
        cost_to_gross_ratio=_cost_to_gross_ratio(total_costs, gross_sum),
        per_trade_gross_net_identity_status=identity_status,
        per_trade_gross_net_identity_unavailable_reason=identity_reason,
        rejected_unaffordable=signal.rejected_unaffordable,
        rejected_insufficient_cash=signal.rejected_insufficient_cash,
        orders_generated=signal.orders_generated,
        entry_attempts=signal.entry_attempts,
        orders_filled=signal.orders_filled,
        target_entry_budget_total=target_budget,
        actual_entry_cash_used_total=actual_budget,
        unallocated_entry_budget_total=unallocated_budget,
        overallocated_entry_budget_total=overallocated_budget,
        lot_compliant_trade_count=n_trades,
        lot_violation_count=0,
    )
    return seal_account_execution_diagnostic_report(report)


def diagnose_candidate_lot_affordability(
    candidates: Sequence[tuple[str, float] | CandidatePriceRow],
    *,
    cash_per_slice: float,
    commission_rate: float,
    minimum_commission: float,
    slippage_bps: float,
    lot_size: int,
) -> CandidateLotAffordabilityReport:
    """Pure synthetic candidate-lot affordability diagnostic; no universe/market IO."""
    rate = _require_finite_nonnegative(commission_rate, field_name="commission_rate")
    minimum = _require_finite_nonnegative(minimum_commission, field_name="minimum_commission")
    slip_bps = _require_finite_nonnegative(slippage_bps, field_name="slippage_bps")
    lot = _require_positive_int(lot_size, field_name="lot_size")
    cash = _require_finite_nonnegative(cash_per_slice, field_name="cash_per_slice")
    if cash <= 0.0:
        raise ValueError("cash_per_slice must be > 0")

    normalized = _normalize_candidates(candidates)
    cost_config = _buy_cost_config(rate=rate, minimum=minimum, slip_bps=slip_bps)
    rows: list[CandidateLotAffordabilityRow] = []
    for item in normalized:
        fill_price = apply_slippage(item.raw_price, cost_config, "buy")
        shares = shares_affordable(cash, item.raw_price, cost_config, lot_size=lot)
        if shares % lot != 0:
            raise ValueError(f"shares_affordable for {item.symbol} is not lot-aligned")
        lots = shares // lot
        if shares > 0:
            total_cost, _ = buy_cost(fill_price, shares, cost_config)
            unused = cash - total_cost
        else:
            unused = cash
        if not math.isfinite(unused) or unused < -1e-12:
            raise ValueError(f"unused_cash for {item.symbol} must be finite and nonnegative")
        if unused < 0.0:
            unused = 0.0
        rows.append(
            CandidateLotAffordabilityRow(
                symbol=item.symbol,
                raw_price=item.raw_price,
                fill_price=fill_price,
                shares_affordable=shares,
                lots_affordable=lots,
                unused_cash=unused,
                can_afford_one_lot=lots >= 1,
            )
        )

    report = CandidateLotAffordabilityReport(
        candidates=normalized,
        cash_per_slice=cash,
        commission_rate=rate,
        minimum_commission=minimum,
        slippage_bps=slip_bps,
        lot_size=lot,
        rows=rows,
    )
    return seal_candidate_lot_affordability_report(report)


def canonical_backtest_result_hash(result: BacktestResult) -> str:
    payload = result.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def canonical_account_execution_payload(report: AccountExecutionDiagnosticReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_account_execution_bytes(report: AccountExecutionDiagnosticReport) -> bytes:
    return json.dumps(
        canonical_account_execution_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_account_execution_report_id(report: AccountExecutionDiagnosticReport) -> str:
    return hashlib.sha256(canonical_account_execution_bytes(report)).hexdigest()


def seal_account_execution_diagnostic_report(
    report: AccountExecutionDiagnosticReport,
) -> AccountExecutionDiagnosticReport:
    return report.model_copy(update={"report_id": compute_account_execution_report_id(report)})


def assert_account_execution_report_self_hash(report: AccountExecutionDiagnosticReport) -> None:
    if report.report_id is None:
        raise ValueError("account execution report_id is missing")
    expected = compute_account_execution_report_id(report)
    if report.report_id != expected:
        raise ValueError("account execution report_id does not match canonical content hash")


def assert_account_execution_report_internal_consistency(
    report: AccountExecutionDiagnosticReport,
) -> None:
    """Cheap sealed-field checks used by the integrity-only file verifier."""
    if report.buy_side_count != report.closed_trade_count or report.sell_side_count != report.closed_trade_count:
        raise ValueError("buy/sell side counts must equal closed_trade_count")
    if report.lot_compliant_trade_count + report.lot_violation_count != report.closed_trade_count:
        raise ValueError("lot compliance counts must partition closed_trade_count")
    if report.lot_violation_count != 0:
        raise ValueError("sealed report must not contain lot violations")
    if report.buy_minimum_commission_binding_count > report.closed_trade_count:
        raise ValueError("buy_minimum_commission_binding_count cannot exceed closed_trade_count")
    if report.sell_minimum_commission_binding_count > report.closed_trade_count:
        raise ValueError("sell_minimum_commission_binding_count cannot exceed closed_trade_count")
    if not math.isclose(
        report.explicit_costs + report.estimated_slippage,
        report.total_trading_costs,
        rel_tol=0.0,
        abs_tol=report.numerical_tolerance,
    ):
        raise ValueError("explicit_costs + estimated_slippage must equal total_trading_costs")
    left = report.target_entry_budget_total + report.overallocated_entry_budget_total
    right = report.actual_entry_cash_used_total + report.unallocated_entry_budget_total
    if not math.isclose(left, right, rel_tol=0.0, abs_tol=report.numerical_tolerance):
        raise ValueError("entry budget identity drift on sealed report")
    if report.per_trade_gross_net_identity_status == "verified":
        expected_net = report.gross_realized_pnl - report.total_trading_costs
        if not math.isclose(
            expected_net,
            report.net_realized_pnl,
            rel_tol=0.0,
            abs_tol=report.numerical_tolerance,
        ):
            raise ValueError("verified identity requires gross - total_costs == net on sealed report")
    if report.closed_trade_count == 0:
        if report.buy_minimum_commission_binding_count != 0 or report.sell_minimum_commission_binding_count != 0:
            raise ValueError("zero-trade report cannot have binding counts")
        if report.entry_notional_total != 0.0 or report.exit_notional_total != 0.0:
            raise ValueError("zero-trade report must have zero notional totals")
        for ratio, label in (
            (report.buy_minimum_commission_binding_fraction, "buy binding fraction"),
            (report.sell_minimum_commission_binding_fraction, "sell binding fraction"),
            (report.buy_effective_commission_bps_notional_weighted, "buy notional-weighted bps"),
            (report.sell_effective_commission_bps_notional_weighted, "sell notional-weighted bps"),
            (report.buy_effective_commission_bps_per_side, "buy per-side bps"),
            (report.sell_effective_commission_bps_per_side, "sell per-side bps"),
        ):
            if ratio.value is not None:
                raise ValueError(f"zero-trade report requires null {label} with reason")
            if ratio.unavailable_reason is None or ratio.unavailable_reason.strip() == "":
                raise ValueError(f"zero-trade report requires explicit reason for null {label}")
    else:
        buy_frac = report.buy_minimum_commission_binding_fraction
        sell_frac = report.sell_minimum_commission_binding_fraction
        if buy_frac.value is None or sell_frac.value is None:
            raise ValueError("binding fractions must be available when trades exist")
        _assert_optional_ratio_unit_interval(buy_frac, "buy binding fraction")
        _assert_optional_ratio_unit_interval(sell_frac, "sell binding fraction")
        expected_buy = report.buy_minimum_commission_binding_count / report.closed_trade_count
        expected_sell = report.sell_minimum_commission_binding_count / report.closed_trade_count
        if not math.isclose(buy_frac.value, expected_buy, rel_tol=0.0, abs_tol=1e-15):
            raise ValueError("buy binding fraction inconsistent with counts")
        if not math.isclose(sell_frac.value, expected_sell, rel_tol=0.0, abs_tol=1e-15):
            raise ValueError("sell binding fraction inconsistent with counts")
    if report.diagnostic_only is not True:
        raise ValueError("diagnostic_only must remain true")
    if report.ready_for_scoring or report.ready_for_backtest or report.ready_for_trading or report.auto_apply:
        raise ValueError("ready/auto flags must remain false")
    if report.file_verifier_scope != "integrity_only":
        raise ValueError("file_verifier_scope must remain integrity_only")


def load_account_execution_diagnostic_report(path: Path) -> AccountExecutionDiagnosticReport:
    try:
        return AccountExecutionDiagnosticReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("account execution diagnostic report is missing or invalid") from exc


def verify_account_execution_diagnostic_report_integrity_only(
    path: Path,
) -> AccountExecutionDiagnosticReport:
    """Integrity-only file verifier: self-hash + sealed consistency, no BacktestResult.

    Full economic validation lives in ``diagnose_account_execution`` against the
    source result. This verifier intentionally cannot recompute descriptive
    fields without embedding a potentially huge BacktestResult.
    """
    report = load_account_execution_diagnostic_report(path)
    assert_account_execution_report_self_hash(report)
    assert_account_execution_report_internal_consistency(report)
    return report


def write_account_execution_diagnostic_report(
    report: AccountExecutionDiagnosticReport,
    output: Path,
) -> None:
    sealed = seal_account_execution_diagnostic_report(report) if report.report_id is None else report
    assert_account_execution_report_self_hash(sealed)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def canonical_candidate_lot_payload(report: CandidateLotAffordabilityReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_candidate_lot_bytes(report: CandidateLotAffordabilityReport) -> bytes:
    return json.dumps(
        canonical_candidate_lot_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_candidate_lot_report_id(report: CandidateLotAffordabilityReport) -> str:
    return hashlib.sha256(canonical_candidate_lot_bytes(report)).hexdigest()


def seal_candidate_lot_affordability_report(
    report: CandidateLotAffordabilityReport,
) -> CandidateLotAffordabilityReport:
    return report.model_copy(update={"report_id": compute_candidate_lot_report_id(report)})


def assert_candidate_lot_report_self_hash(report: CandidateLotAffordabilityReport) -> None:
    if report.report_id is None:
        raise ValueError("candidate lot affordability report_id is missing")
    expected = compute_candidate_lot_report_id(report)
    if report.report_id != expected:
        raise ValueError("candidate lot affordability report_id does not match canonical content hash")


def assert_candidate_lot_report_matches_recomputed_inputs(
    report: CandidateLotAffordabilityReport,
) -> None:
    expected = diagnose_candidate_lot_affordability(
        [(row.symbol, row.raw_price) for row in report.candidates],
        cash_per_slice=report.cash_per_slice,
        commission_rate=report.commission_rate,
        minimum_commission=report.minimum_commission,
        slippage_bps=report.slippage_bps,
        lot_size=report.lot_size,
    )
    if canonical_candidate_lot_payload(report) != canonical_candidate_lot_payload(expected):
        raise ValueError(
            "candidate lot affordability report canonical payload does not match "
            "recomputed diagnose() output from embedded inputs"
        )
    if report.report_id != expected.report_id:
        raise ValueError("candidate lot affordability report_id does not match recomputed diagnose() report_id")


def load_candidate_lot_affordability_report(path: Path) -> CandidateLotAffordabilityReport:
    try:
        return CandidateLotAffordabilityReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("candidate lot affordability report is missing or invalid") from exc


def verify_candidate_lot_affordability_report_file(path: Path) -> CandidateLotAffordabilityReport:
    report = load_candidate_lot_affordability_report(path)
    assert_candidate_lot_report_self_hash(report)
    if report.diagnostic_only is not True:
        raise ValueError("diagnostic_only must remain true")
    if report.ready_for_scoring or report.ready_for_backtest or report.ready_for_trading or report.auto_apply:
        raise ValueError("ready/auto flags must remain false")
    assert_candidate_lot_report_matches_recomputed_inputs(report)
    return report


def write_candidate_lot_affordability_report(
    report: CandidateLotAffordabilityReport,
    output: Path,
) -> None:
    sealed = seal_candidate_lot_affordability_report(report) if report.report_id is None else report
    assert_candidate_lot_report_self_hash(sealed)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _buy_cost_config(*, rate: float, minimum: float, slip_bps: float) -> CostConfig:
    # Stamp tax is irrelevant for buys; keep a finite zero rate for CostConfig validity.
    return CostConfig(
        commission_rate=rate,
        min_commission=minimum,
        slippage_bps=slip_bps,
        stamp_tax_rate=0.0,
        stamp_tax_schedule=[],
    )


def _commission_parameter_semantics(*, rate: float, minimum: float) -> str:
    if rate == 0.0 and minimum == 0.0:
        return "zero_rate_and_zero_minimum: commission always 0; minimum never binds"
    if rate == 0.0 and minimum > 0.0:
        return "zero_rate_positive_minimum: every positive-notional side binds to minimum_commission"
    if rate > 0.0 and minimum == 0.0:
        return "positive_rate_zero_minimum: rate commission always applies; minimum never binds"
    return (
        "positive_rate_and_minimum: binds to minimum_commission when "
        "minimum_commission > notional * commission_rate (within tolerance)"
    )


def _minimum_commission_binds(
    *,
    notional: float,
    rate: float,
    minimum: float,
    tolerance: float,
) -> bool:
    if notional <= 0.0:
        return False
    if rate == 0.0 and minimum == 0.0:
        return False
    rate_portion = notional * rate
    # Bind when the floor strictly exceeds the rate commission beyond tolerance.
    return minimum > rate_portion + tolerance


def _assert_commission_matches_params(
    *,
    notional: float,
    observed: float,
    rate: float,
    minimum: float,
    tolerance: float,
    side: str,
    trade_index: int,
) -> None:
    expected = commission(
        notional,
        CostConfig(
            commission_rate=rate,
            min_commission=minimum,
            slippage_bps=0.0,
            stamp_tax_rate=0.0,
            stamp_tax_schedule=[],
        ),
    )
    if not math.isclose(observed, expected, rel_tol=0.0, abs_tol=tolerance):
        raise ValueError(
            f"trade[{trade_index}] {side} commission {observed} != expected {expected} "
            f"from explicit commission_rate/minimum_commission"
        )


def _trade_pnl_identity_availability(trade: TradeFill) -> str | None:
    if trade.gross_pnl is None:
        return "gross_pnl missing (legacy); refusing to invent from net + costs"
    if trade.entry_raw_price is None or trade.exit_raw_price is None:
        return (
            "entry_raw_price/exit_raw_price missing (legacy); slippage defaults must not masquerade as verified zeros"
        )
    return None


def _assert_slippage_matches_params(
    trade: TradeFill,
    *,
    cost_config: CostConfig,
    tolerance: float,
    trade_index: int,
) -> None:
    assert trade.entry_raw_price is not None
    assert trade.exit_raw_price is not None
    expected_entry = apply_slippage(trade.entry_raw_price, cost_config, "buy")
    expected_exit = apply_slippage(trade.exit_raw_price, cost_config, "sell")
    if not math.isclose(trade.entry_price, expected_entry, rel_tol=0.0, abs_tol=tolerance):
        raise ValueError(f"trade[{trade_index}] entry_price does not match apply_slippage(entry_raw_price)")
    if not math.isclose(trade.exit_price, expected_exit, rel_tol=0.0, abs_tol=tolerance):
        raise ValueError(f"trade[{trade_index}] exit_price does not match apply_slippage(exit_raw_price)")
    expected_buy_slip = (trade.entry_price - trade.entry_raw_price) * trade.shares
    expected_sell_slip = (trade.exit_raw_price - trade.exit_price) * trade.shares
    if not math.isclose(trade.buy_slippage, expected_buy_slip, rel_tol=0.0, abs_tol=tolerance):
        raise ValueError(f"trade[{trade_index}] buy_slippage inconsistent with raw/fill prices")
    if not math.isclose(trade.sell_slippage, expected_sell_slip, rel_tol=0.0, abs_tol=tolerance):
        raise ValueError(f"trade[{trade_index}] sell_slippage inconsistent with raw/fill prices")


def _validate_trade_basics(
    trade: TradeFill,
    *,
    lot_size: int,
    tolerance: float,
    trade_index: int,
) -> None:
    for name in ("entry_price", "exit_price"):
        price = getattr(trade, name)
        if not math.isfinite(price) or price <= 0.0:
            raise ValueError(f"trade[{trade_index}] {name} must be finite and positive")
    if type(trade.shares) is not int or isinstance(trade.shares, bool) or trade.shares <= 0:
        raise ValueError(f"trade[{trade_index}] shares must be a positive int")
    if trade.shares % lot_size != 0:
        raise ValueError(f"trade[{trade_index}] shares={trade.shares} not divisible by lot_size={lot_size}")
    for name in (
        "buy_commission",
        "sell_commission",
        "stamp_tax",
        "buy_slippage",
        "sell_slippage",
    ):
        value = float(getattr(trade, name))
        if not math.isfinite(value) or value < -tolerance:
            raise ValueError(f"trade[{trade_index}] {name} must be finite and nonnegative")
        if value < 0.0:
            raise ValueError(f"trade[{trade_index}] {name} must be nonnegative")
    if not math.isfinite(trade.pnl):
        raise ValueError(f"trade[{trade_index}] pnl must be finite")
    if trade.gross_pnl is not None and not math.isfinite(trade.gross_pnl):
        raise ValueError(f"trade[{trade_index}] gross_pnl must be finite when present")


def _binding_fraction(count: int, total: int, *, side: str) -> OptionalRatio:
    if total == 0:
        return OptionalRatio(
            value=None,
            unavailable_reason=f"no closed trades; {side} minimum-commission binding fraction undefined",
        )
    return OptionalRatio(value=count / total, unavailable_reason=None)


def _notional_weighted_bps(commission_sum: float, notional: float, *, side: str) -> OptionalRatio:
    if notional <= 0.0:
        return OptionalRatio(
            value=None,
            unavailable_reason=f"{side} notional is zero; notional-weighted effective commission bps undefined",
        )
    return OptionalRatio(value=(commission_sum / notional) * 10_000.0, unavailable_reason=None)


def _per_side_mean_bps(
    trades: Sequence[TradeFill],
    *,
    side: Literal["buy", "sell"],
    tolerance: float,
) -> OptionalRatio:
    if not trades:
        return OptionalRatio(
            value=None,
            unavailable_reason=f"no closed trades; {side} per-side effective commission bps undefined",
        )
    values: list[float] = []
    for trade in trades:
        notional = (trade.entry_price if side == "buy" else trade.exit_price) * trade.shares
        commission_value = trade.buy_commission if side == "buy" else trade.sell_commission
        if notional <= tolerance:
            return OptionalRatio(
                value=None,
                unavailable_reason=(
                    f"{side} per-side effective commission bps undefined because a trade notional "
                    "is zero within tolerance"
                ),
            )
        values.append((commission_value / notional) * 10_000.0)
    return OptionalRatio(value=sum(values) / len(values), unavailable_reason=None)


def _cost_drag(total_costs: float, initial_capital: float) -> OptionalRatio:
    if initial_capital <= 0.0:
        return OptionalRatio(
            value=None,
            unavailable_reason="initial_capital is zero; cost drag vs capital undefined",
        )
    return OptionalRatio(value=total_costs / initial_capital, unavailable_reason=None)


def _cost_to_gross_ratio(total_costs: float, gross: float) -> OptionalRatio:
    if gross <= 0.0:
        return OptionalRatio(
            value=None,
            unavailable_reason=("gross_realized_pnl <= 0; cost/gross ratio not economically interpretable"),
        )
    return OptionalRatio(value=total_costs / gross, unavailable_reason=None)


def _assert_close(actual: float, expected: float, tolerance: float, label: str) -> None:
    if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=tolerance):
        raise ValueError(f"attribution {label} drift: recomputed={actual} attribution={expected} tolerance={tolerance}")


def _assert_optional_ratio_unit_interval(ratio: OptionalRatio, label: str) -> None:
    if ratio.value is None:
        return
    if ratio.value < 0.0 or ratio.value > 1.0:
        raise ValueError(f"{label} must lie in [0, 1] when available")


def _validate_result_window_dates(result: BacktestResult) -> None:
    if result.start > result.end:
        raise ValueError("result start must be on or before result end")
    window = result.window
    if window.start > window.entry_end:
        raise ValueError("window start must be on or before entry_end")
    if window.entry_end > window.valuation_end:
        raise ValueError("window entry_end must be on or before valuation_end")
    if window.signal_end is not None:
        if window.signal_end < window.start or window.signal_end > window.entry_end:
            raise ValueError("window signal_end must fall within [window.start, entry_end]")
    if window.start < result.start or window.valuation_end > result.end:
        raise ValueError("result date bounds are inconsistent with window start/valuation_end")


def _normalize_candidates(
    candidates: Sequence[tuple[str, float] | CandidatePriceRow],
) -> list[CandidatePriceRow]:
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("candidates must be a sequence of (symbol, raw_price) rows")
    if len(candidates) == 0:
        raise ValueError("candidates must be non-empty")
    normalized: list[CandidatePriceRow] = []
    seen: set[str] = set()
    for item in candidates:
        if isinstance(item, CandidatePriceRow):
            row = CandidatePriceRow(symbol=item.symbol, raw_price=item.raw_price)
        elif isinstance(item, tuple) and len(item) == 2:
            symbol, raw_price = item
            if not isinstance(symbol, str):
                raise ValueError("candidate symbol must be a string")
            canonical = symbol.strip()
            if canonical == "":
                raise ValueError("candidate symbol must be nonblank after stripping whitespace")
            if isinstance(raw_price, bool) or not isinstance(raw_price, int | float):
                raise ValueError("candidate raw_price must be a finite number (bool rejected)")
            row = CandidatePriceRow(symbol=canonical, raw_price=float(raw_price))
        else:
            raise ValueError("candidates entries must be (symbol, raw_price) or CandidatePriceRow")
        if row.symbol in seen:
            raise ValueError(f"duplicate candidate symbol: {row.symbol}")
        seen.add(row.symbol)
        normalized.append(row)
    return normalized


def _require_nonblank(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a nonblank string")
    return value


def _require_positive_int(value: int, *, field_name: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an int")
    if value < 1:
        raise ValueError(f"{field_name} must be >= 1")
    return value


def _require_finite_nonnegative(value: float, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    if number < 0.0:
        raise ValueError(f"{field_name} must be nonnegative")
    return number


__all__ = [
    "ACCOUNT_EXECUTION_DIAGNOSTIC_VERSION",
    "ACCOUNT_EXECUTION_SCHEMA_VERSION",
    "AccountExecutionDiagnosticReport",
    "CANDIDATE_LOT_AFFORDABILITY_DIAGNOSTIC_VERSION",
    "CANDIDATE_LOT_AFFORDABILITY_SCHEMA_VERSION",
    "CandidateLotAffordabilityReport",
    "CandidateLotAffordabilityRow",
    "CandidatePriceRow",
    "INTEGRITY_ONLY_VERIFIER_NOTE",
    "OptionalRatio",
    "assert_account_execution_report_internal_consistency",
    "assert_account_execution_report_self_hash",
    "assert_candidate_lot_report_matches_recomputed_inputs",
    "assert_candidate_lot_report_self_hash",
    "canonical_account_execution_bytes",
    "canonical_account_execution_payload",
    "canonical_backtest_result_hash",
    "canonical_candidate_lot_bytes",
    "canonical_candidate_lot_payload",
    "compute_account_execution_report_id",
    "compute_candidate_lot_report_id",
    "diagnose_account_execution",
    "diagnose_candidate_lot_affordability",
    "load_account_execution_diagnostic_report",
    "load_candidate_lot_affordability_report",
    "seal_account_execution_diagnostic_report",
    "seal_candidate_lot_affordability_report",
    "verify_account_execution_diagnostic_report_integrity_only",
    "verify_candidate_lot_affordability_report_file",
    "write_account_execution_diagnostic_report",
    "write_candidate_lot_affordability_report",
]
