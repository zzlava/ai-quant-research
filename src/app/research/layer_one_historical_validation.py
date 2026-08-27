"""Frozen long-history evaluation for the layer-one CSI All-Share risk budget.

The evaluator is deliberately separate from the production backtest engine. It
replays only the confirmed 2013-2021 historical-validation windows after a
2005-2012 state warm-up, binds the exact sealed index snapshot and cost
contracts, and never authorizes scoring, orders, trading or an OOS claim.

Decisions use features through P and rebalance at the close of D (the next
market day). The return from P close through D close therefore remains on the
previous budget. This close-execution convention avoids claiming an unavailable
total-return open price. A historical risk lock is never auto-cleared: the
engine refuses to invent the explicit human confirmation required by contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.a_share_stamp_tax_schedule import (
    AShareStampTaxScheduleContract,
    stamp_tax_rate_for,
    verify_a_share_stamp_tax_schedule_file,
)
from app.research.layer_one_index_data_evidence import DEFAULT_SNAPSHOT_DIR
from app.research.layer_one_index_protocol import CONFIRMED_VALIDATION_SEGMENTS
from app.research.layer_one_regime import (
    ALLOWED_BUDGET_LEVELS,
    BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_ID,
    BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    REQUIRED_ANNUALIZATION_TRADING_DAYS,
    REQUIRED_DRAWDOWN_LOOKBACK_BARS,
    REQUIRED_TREND_LOOKBACK_BARS,
    REQUIRED_VOLATILITY_LOOKBACK_BARS,
    apply_weekly_budget_adjustment,
    bind_index_data_evidence,
    bind_upstream_contracts,
    map_account_drawdown_cap,
    map_index_drawdown_cap,
    map_trend_regime,
    map_volatility_cap,
)
from app.research.repo_file_safety import resolve_repo_regular_file

SCHEMA_VERSION: Literal["1"] = "1"
EVALUATOR_VERSION: Literal["layer-one-historical-validation-v1"] = (
    "layer-one-historical-validation-v1"
)
CONFIRMATION_AS_OF = date(2026, 8, 27)
DEFAULT_OUTPUT_DIR = Path("data/research/layer-one-historical-validation-v1")
DEFAULT_REPORT_PATH = DEFAULT_OUTPUT_DIR / "report.json"
DEFAULT_DAILY_PATH = DEFAULT_OUTPUT_DIR / "daily-path.parquet"

INITIAL_CAPITAL_CNY = 80_000.0
MANUAL_OPEN_CEILING = 0.9
BASE_COMMISSION_RATE = 0.00025
MINIMUM_COMMISSION_CNY = 5.0
BASE_SLIPPAGE_BPS = 5.0
STRESS_SLIPPAGE_BPS = 15.0
BASELINE_STOCK_WEIGHT = 0.9

COMBINED_START = CONFIRMED_VALIDATION_SEGMENTS[0][0]
COMBINED_END = CONFIRMED_VALIDATION_SEGMENTS[-1][1]
CONSUMED_OOS_START = date(2025, 1, 1)

_DAILY_COLUMNS: tuple[str, ...] = (
    "date",
    "as_of",
    "price_close",
    "total_return_close",
    "total_return_daily_return",
    "close_to_sma_ratio",
    "realized_volatility_annualized",
    "index_drawdown",
    "trend_regime",
    "raw_market_target_budget",
    "base_budget_before",
    "base_budget_after",
    "base_account_drawdown_before",
    "base_risk_lock_active",
    "base_risk_lock_triggered",
    "base_trade_cost",
    "base_cumulative_cost",
    "base_equity",
    "stress_budget_before",
    "stress_budget_after",
    "stress_account_drawdown_before",
    "stress_risk_lock_active",
    "stress_risk_lock_triggered",
    "stress_trade_cost",
    "stress_cumulative_cost",
    "stress_equity",
    "baseline_equity",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WindowMetrics(_StrictModel):
    label: str = Field(min_length=1)
    declared_start: date
    declared_end: date
    actual_start: date
    actual_end: date
    trading_days: int = Field(gt=0)
    start_equity: float = Field(gt=0)
    end_equity: float = Field(gt=0)
    annualized_return_after_cost: float
    max_drawdown: float
    calmar: float | None
    explicit_costs: float = Field(ge=0)
    trade_count: int = Field(ge=0)
    baseline_start_equity: float = Field(gt=0)
    baseline_end_equity: float = Field(gt=0)
    baseline_annualized_return: float
    baseline_max_drawdown: float
    max_drawdown_amplitude_improvement: float | None
    positive_baseline_cagr_retention: float | None

    @field_validator(
        "annualized_return_after_cost",
        "max_drawdown",
        "calmar",
        "explicit_costs",
        "baseline_annualized_return",
        "baseline_max_drawdown",
        "max_drawdown_amplitude_improvement",
        "positive_baseline_cagr_retention",
    )
    @classmethod
    def _finite_optional(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("metric must be finite when present")
        return value


class HistoricalValidationGates(_StrictModel):
    per_segment_max_drawdown_floor_pass: bool
    combined_max_drawdown_floor_pass: bool
    combined_positive_after_cost_annualized_return_pass: bool
    combined_calmar_pass: bool
    combined_baseline_drawdown_improvement_pass: bool
    combined_positive_baseline_cagr_retention_pass: bool
    stress_max_drawdown_floor_pass: bool
    all_hard_gates_pass: bool

    @model_validator(mode="after")
    def _all_is_conjunction(self) -> HistoricalValidationGates:
        expected = all(
            (
                self.per_segment_max_drawdown_floor_pass,
                self.combined_max_drawdown_floor_pass,
                self.combined_positive_after_cost_annualized_return_pass,
                self.combined_calmar_pass,
                self.combined_baseline_drawdown_improvement_pass,
                self.combined_positive_baseline_cagr_retention_pass,
                self.stress_max_drawdown_floor_pass,
            )
        )
        if self.all_hard_gates_pass != expected:
            raise ValueError("all_hard_gates_pass must equal the conjunction of hard gates")
        return self


class LayerOneHistoricalValidationReport(_StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    evaluator_version: Literal["layer-one-historical-validation-v1"] = EVALUATOR_VERSION
    report_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmation_as_of: date = CONFIRMATION_AS_OF
    layer_one_index_data_evidence_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    layer_one_index_protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    two_layer_decision_contract_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    risk_state_symbol: Literal["000985.CSI"]
    performance_benchmark_symbol: Literal["H00985.CSI"]
    stamp_tax_contract_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    daily_path: str = Field(min_length=1)
    daily_table_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    daily_row_count: int = Field(gt=0)
    first_action_day: date
    last_action_day: date
    validation_start: date
    validation_end: date
    initial_capital_cny: float = Field(gt=0)
    manual_open_ceiling: float = MANUAL_OPEN_CEILING
    commission_per_side: float = BASE_COMMISSION_RATE
    minimum_commission_cny: float = MINIMUM_COMMISSION_CNY
    base_slippage_bps_per_side: float = BASE_SLIPPAGE_BPS
    stress_slippage_bps_per_side: float = STRESS_SLIPPAGE_BPS
    baseline_stock_weight: float = BASELINE_STOCK_WEIGHT
    execution_convention: Literal[
        "features_through_P_rebalance_at_D_close_previous_budget_earns_P_to_D_close_return"
    ]
    baseline_convention: Literal["cost_free_daily_90pct_total_return_exposure_plus_10pct_cash"]
    no_manual_unlock_simulated: Literal[True] = True
    terminal_lock_if_triggered: Literal[True] = True
    validation_segments: list[WindowMetrics]
    combined: WindowMetrics
    stress_validation_segments: list[WindowMetrics]
    stress_combined: WindowMetrics
    budget_occupancy: dict[str, int]
    regime_budget_occupancy: dict[str, dict[str, int]]
    regime_transition_counts: dict[str, int]
    risk_lock_trigger_dates: list[date]
    stress_risk_lock_trigger_dates: list[date]
    gates: HistoricalValidationGates
    historical_validation_evidence_pass: bool
    historical_validation_only: Literal[True] = True
    oos_claim: Literal[False] = False
    consumed_oos_reused: Literal[False] = False
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @model_validator(mode="after")
    def _frozen_semantics(self) -> LayerOneHistoricalValidationReport:
        if self.confirmation_as_of != CONFIRMATION_AS_OF:
            raise ValueError("confirmation_as_of drifted")
        if self.layer_one_index_data_evidence_id != BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_ID:
            raise ValueError("layer-one index evidence binding drifted")
        if self.layer_one_index_protocol_id != BOUND_LAYER_ONE_INDEX_PROTOCOL_ID:
            raise ValueError("layer-one protocol binding drifted")
        if self.two_layer_decision_contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
            raise ValueError("two-layer decision contract binding drifted")
        if self.validation_start != COMBINED_START or self.validation_end != COMBINED_END:
            raise ValueError("historical validation window must remain 2013-01-01..2021-12-31")
        frozen_costs = (
            (self.manual_open_ceiling, MANUAL_OPEN_CEILING),
            (self.commission_per_side, BASE_COMMISSION_RATE),
            (self.minimum_commission_cny, MINIMUM_COMMISSION_CNY),
            (self.base_slippage_bps_per_side, BASE_SLIPPAGE_BPS),
            (self.stress_slippage_bps_per_side, STRESS_SLIPPAGE_BPS),
            (self.baseline_stock_weight, BASELINE_STOCK_WEIGHT),
        )
        if any(abs(actual - expected) > 1e-15 for actual, expected in frozen_costs):
            raise ValueError("frozen budget/cost assumptions drifted")
        if self.last_action_day >= CONSUMED_OOS_START:
            raise ValueError("historical validation must not enter consumed OOS")
        if self.historical_validation_evidence_pass != self.gates.all_hard_gates_pass:
            raise ValueError("historical_validation_evidence_pass must equal hard-gate conjunction")
        if len(self.validation_segments) != len(CONFIRMED_VALIDATION_SEGMENTS):
            raise ValueError("all three frozen historical-validation segments are required")
        if len(self.stress_validation_segments) != len(CONFIRMED_VALIDATION_SEGMENTS):
            raise ValueError("all three frozen stress segments are required")
        for metrics, expected in zip(self.validation_segments, CONFIRMED_VALIDATION_SEGMENTS, strict=True):
            if (metrics.declared_start, metrics.declared_end) != expected:
                raise ValueError("historical-validation segment boundaries drifted")
        for metrics, expected in zip(
            self.stress_validation_segments, CONFIRMED_VALIDATION_SEGMENTS, strict=True
        ):
            if (metrics.declared_start, metrics.declared_end) != expected:
                raise ValueError("stress segment boundaries drifted")
        if any((self.ready_for_scoring, self.ready_for_backtest, self.ready_for_orders, self.ready_for_trading)):
            raise ValueError("historical validation cannot authorize production research or trading")
        return self


def _json_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonical_report(report: LayerOneHistoricalValidationReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def compute_report_id(report: LayerOneHistoricalValidationReport) -> str:
    return _json_sha256(_canonical_report(report))


def seal_report(report: LayerOneHistoricalValidationReport) -> LayerOneHistoricalValidationReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: LayerOneHistoricalValidationReport) -> None:
    if report.report_id is None or report.report_id != compute_report_id(report):
        raise ValueError("layer-one historical validation report_id mismatch")


def _frame_content_sha256(frame: pl.DataFrame) -> str:
    if frame.columns != list(_DAILY_COLUMNS):
        raise ValueError("daily path columns do not match frozen schema")
    records: list[dict[str, Any]] = []
    for row in frame.to_dicts():
        normalized: dict[str, Any] = {}
        for key in _DAILY_COLUMNS:
            value = row[key]
            normalized[key] = value.isoformat() if type(value) is date else value
        records.append(normalized)
    return _json_sha256({"columns": list(_DAILY_COLUMNS), "rows": records})


def _load_index_snapshot(snapshot_dir: Path) -> tuple[list[date], list[float], list[float]]:
    root = Path(snapshot_dir)
    calendar = pl.read_parquet(root / "calendar.parquet")
    price = pl.read_parquet(root / "price_index.parquet")
    total = pl.read_parquet(root / "total_return_index.parquet")
    if calendar.schema != {"date": pl.Date}:
        raise ValueError("calendar snapshot schema drifted")
    required_price = {"date", "symbol", "close", "available_at"}
    required_total = {"date", "symbol", "close", "available_at"}
    if not required_price.issubset(price.columns) or not required_total.issubset(total.columns):
        raise ValueError("index snapshot is missing required columns")
    dates = calendar.get_column("date").to_list()
    if any(type(day) is not date for day in dates):
        raise ValueError("calendar contains non-date values")
    if dates != sorted(set(dates)):
        raise ValueError("calendar must be strictly increasing and unique")
    price = price.sort("date")
    total = total.sort("date")
    if price.get_column("date").to_list() != dates or total.get_column("date").to_list() != dates:
        raise ValueError("price/total-return dates must exactly match the sealed calendar")
    if price.get_column("symbol").unique().to_list() != ["000985.CSI"]:
        raise ValueError("price-index symbol drifted")
    if total.get_column("symbol").unique().to_list() != ["H00985.CSI"]:
        raise ValueError("total-return symbol drifted")
    price_close = [float(value) for value in price.get_column("close").to_list()]
    total_close = [float(value) for value in total.get_column("close").to_list()]
    if any(not math.isfinite(value) or value <= 0 for value in price_close + total_close):
        raise ValueError("index closes must be finite and strictly positive")
    for frame_name, frame in (("price", price), ("total_return", total)):
        available_dates = [value.date() for value in frame.get_column("available_at").to_list()]
        if available_dates != dates:
            raise ValueError(f"{frame_name} available_at must remain on its own trade date")
    return dates, price_close, total_close


def _sample_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        raise ValueError("sample standard deviation requires at least two values")
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _market_features(price_close: Sequence[float], p_index: int) -> tuple[float, float, float, str, float]:
    trend = price_close[p_index - REQUIRED_TREND_LOOKBACK_BARS + 1 : p_index + 1]
    volatility_prices = price_close[
        p_index - REQUIRED_VOLATILITY_LOOKBACK_BARS : p_index + 1
    ]
    drawdown_prices = price_close[p_index - REQUIRED_DRAWDOWN_LOOKBACK_BARS + 1 : p_index + 1]
    if (
        len(trend) != REQUIRED_TREND_LOOKBACK_BARS
        or len(volatility_prices) != REQUIRED_VOLATILITY_LOOKBACK_BARS + 1
        or len(drawdown_prices) != REQUIRED_DRAWDOWN_LOOKBACK_BARS
    ):
        raise ValueError("insufficient feature lookback")
    latest = float(price_close[p_index])
    ratio = latest / (sum(trend) / len(trend))
    returns = [
        volatility_prices[index] / volatility_prices[index - 1] - 1.0
        for index in range(1, len(volatility_prices))
    ]
    realized_vol = _sample_std(returns) * math.sqrt(float(REQUIRED_ANNUALIZATION_TRADING_DAYS))
    index_drawdown = latest / max(drawdown_prices) - 1.0
    regime, trend_budget = map_trend_regime(ratio)
    raw_market_target = min(
        trend_budget,
        map_volatility_cap(realized_vol),
        map_index_drawdown_cap(index_drawdown),
        MANUAL_OPEN_CEILING,
    )
    return ratio, realized_vol, index_drawdown, regime, raw_market_target


def _first_trading_day_of_week(dates: Sequence[date], index: int) -> bool:
    if index <= 0:
        return True
    return dates[index - 1].isocalendar()[:2] != dates[index].isocalendar()[:2]


def _trade_cost(
    *,
    equity: float,
    previous_budget: float,
    target_budget: float,
    trade_date: date,
    slippage_bps: float,
    stamp_contract: AShareStampTaxScheduleContract,
) -> float:
    notional = abs(target_budget - previous_budget) * equity
    if notional <= 1e-12:
        return 0.0
    commission = max(notional * BASE_COMMISSION_RATE, MINIMUM_COMMISSION_CNY)
    slippage = notional * slippage_bps / 10_000.0
    stamp_tax = 0.0
    if target_budget < previous_budget:
        stamp_tax = notional * stamp_tax_rate_for(trade_date, "sell", contract=stamp_contract)
    return commission + slippage + stamp_tax


def _advance_path(
    *,
    equity: float,
    peak: float,
    previous_budget: float,
    risk_lock_active: bool,
    account_return: float,
    raw_market_target: float,
    first_of_week: bool,
    trade_date: date,
    slippage_bps: float,
    stamp_contract: AShareStampTaxScheduleContract,
) -> tuple[float, float, float, bool, bool, float, float]:
    account_drawdown = equity / peak - 1.0
    account_cap, triggers_lock, _ = map_account_drawdown_cap(account_drawdown)
    new_lock = bool(risk_lock_active or triggers_lock)
    raw_target = 0.0 if new_lock else min(raw_market_target, account_cap)
    target_budget, _, _ = apply_weekly_budget_adjustment(
        raw_target_budget=raw_target,
        previous_applied_stock_budget=previous_budget,
        target_day_is_first_market_trading_day_of_week=first_of_week,
        risk_lock_active=new_lock,
    )
    gross_equity = equity * (1.0 + previous_budget * account_return)
    if not math.isfinite(gross_equity) or gross_equity <= 0:
        raise ValueError("portfolio equity became non-positive before costs")
    cost = _trade_cost(
        equity=gross_equity,
        previous_budget=previous_budget,
        target_budget=target_budget,
        trade_date=trade_date,
        slippage_bps=slippage_bps,
        stamp_contract=stamp_contract,
    )
    next_equity = gross_equity - cost
    if not math.isfinite(next_equity) or next_equity <= 0:
        raise ValueError("portfolio equity became non-positive after costs")
    next_peak = max(peak, next_equity)
    return (
        next_equity,
        next_peak,
        target_budget,
        new_lock,
        bool(triggers_lock and not risk_lock_active),
        cost,
        account_drawdown,
    )


def _build_daily_path(
    *,
    dates: Sequence[date],
    price_close: Sequence[float],
    total_close: Sequence[float],
    stamp_contract: AShareStampTaxScheduleContract,
) -> pl.DataFrame:
    covered_indices = [
        index for index, day in enumerate(dates) if day >= stamp_contract.schedule_coverage_start
    ]
    if not covered_indices:
        raise ValueError("index history does not reach stamp-tax schedule coverage")
    start_index = max(REQUIRED_DRAWDOWN_LOOKBACK_BARS, covered_indices[0])
    if len(dates) <= start_index:
        raise ValueError("sealed index history is too short for frozen lookbacks")
    base_equity = INITIAL_CAPITAL_CNY
    base_peak = INITIAL_CAPITAL_CNY
    base_budget = 0.0
    base_lock = False
    base_cumulative_cost = 0.0
    stress_equity = INITIAL_CAPITAL_CNY
    stress_peak = INITIAL_CAPITAL_CNY
    stress_budget = 0.0
    stress_lock = False
    stress_cumulative_cost = 0.0
    baseline_equity = INITIAL_CAPITAL_CNY
    rows: list[dict[str, Any]] = []
    for index in range(start_index, len(dates)):
        d = dates[index]
        p_index = index - 1
        ratio, realized_vol, index_dd, regime, raw_market_target = _market_features(
            price_close, p_index
        )
        market_return = total_close[index] / total_close[p_index] - 1.0
        if not math.isfinite(market_return) or market_return <= -1.0:
            raise ValueError("total-return daily return must be finite and greater than -100%")
        first_of_week = _first_trading_day_of_week(dates, index)
        base_before = base_budget
        stress_before = stress_budget
        (
            base_equity,
            base_peak,
            base_budget,
            base_lock,
            base_triggered,
            base_cost,
            base_dd,
        ) = _advance_path(
            equity=base_equity,
            peak=base_peak,
            previous_budget=base_before,
            risk_lock_active=base_lock,
            account_return=market_return,
            raw_market_target=raw_market_target,
            first_of_week=first_of_week,
            trade_date=d,
            slippage_bps=BASE_SLIPPAGE_BPS,
            stamp_contract=stamp_contract,
        )
        (
            stress_equity,
            stress_peak,
            stress_budget,
            stress_lock,
            stress_triggered,
            stress_cost,
            stress_dd,
        ) = _advance_path(
            equity=stress_equity,
            peak=stress_peak,
            previous_budget=stress_before,
            risk_lock_active=stress_lock,
            account_return=market_return,
            raw_market_target=raw_market_target,
            first_of_week=first_of_week,
            trade_date=d,
            slippage_bps=STRESS_SLIPPAGE_BPS,
            stamp_contract=stamp_contract,
        )
        base_cumulative_cost += base_cost
        stress_cumulative_cost += stress_cost
        baseline_equity *= 1.0 + BASELINE_STOCK_WEIGHT * market_return
        if not math.isfinite(baseline_equity) or baseline_equity <= 0:
            raise ValueError("baseline equity became invalid")
        rows.append(
            {
                "date": d,
                "as_of": dates[p_index],
                "price_close": float(price_close[index]),
                "total_return_close": float(total_close[index]),
                "total_return_daily_return": market_return,
                "close_to_sma_ratio": ratio,
                "realized_volatility_annualized": realized_vol,
                "index_drawdown": index_dd,
                "trend_regime": regime,
                "raw_market_target_budget": raw_market_target,
                "base_budget_before": base_before,
                "base_budget_after": base_budget,
                "base_account_drawdown_before": base_dd,
                "base_risk_lock_active": base_lock,
                "base_risk_lock_triggered": base_triggered,
                "base_trade_cost": base_cost,
                "base_cumulative_cost": base_cumulative_cost,
                "base_equity": base_equity,
                "stress_budget_before": stress_before,
                "stress_budget_after": stress_budget,
                "stress_account_drawdown_before": stress_dd,
                "stress_risk_lock_active": stress_lock,
                "stress_risk_lock_triggered": stress_triggered,
                "stress_trade_cost": stress_cost,
                "stress_cumulative_cost": stress_cumulative_cost,
                "stress_equity": stress_equity,
                "baseline_equity": baseline_equity,
            }
        )
    frame = pl.DataFrame(rows).select(list(_DAILY_COLUMNS))
    if frame.get_column("date").to_list() != sorted(frame.get_column("date").to_list()):
        raise ValueError("daily path is not strictly ordered")
    return frame


def _max_drawdown(values: Sequence[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def _window_metrics(
    frame: pl.DataFrame,
    *,
    label: str,
    declared_start: date,
    declared_end: date,
    equity_column: str,
    cumulative_cost_column: str,
    trade_cost_column: str,
) -> WindowMetrics:
    if declared_end >= CONSUMED_OOS_START:
        raise ValueError("evaluation window reaches consumed OOS")
    dates = frame.get_column("date").to_list()
    start_indices = [index for index, day in enumerate(dates) if day >= declared_start]
    end_indices = [index for index, day in enumerate(dates) if day <= declared_end]
    if not start_indices or not end_indices:
        raise ValueError(f"{label} has no observations")
    first = start_indices[0]
    last = end_indices[-1]
    if first <= 0 or last < first:
        raise ValueError(f"{label} requires a prior-day anchor and non-empty body")
    rows = frame.slice(first, last - first + 1)
    anchor = frame.row(first - 1, named=True)
    final = frame.row(last, named=True)
    equities = [float(anchor[equity_column])] + [
        float(value) for value in rows.get_column(equity_column).to_list()
    ]
    baseline_equities = [float(anchor["baseline_equity"])] + [
        float(value) for value in rows.get_column("baseline_equity").to_list()
    ]
    trading_days = rows.height
    years = trading_days / REQUIRED_ANNUALIZATION_TRADING_DAYS
    start_equity = equities[0]
    end_equity = equities[-1]
    annualized = (end_equity / start_equity) ** (1.0 / years) - 1.0
    max_dd = _max_drawdown(equities)
    calmar = annualized / abs(max_dd) if max_dd < 0.0 else None
    baseline_start = baseline_equities[0]
    baseline_end = baseline_equities[-1]
    baseline_annualized = (baseline_end / baseline_start) ** (1.0 / years) - 1.0
    baseline_dd = _max_drawdown(baseline_equities)
    improvement = None
    if baseline_dd < 0.0:
        improvement = (abs(baseline_dd) - abs(max_dd)) / abs(baseline_dd)
    retention = annualized / baseline_annualized if baseline_annualized > 0.0 else None
    explicit_costs = float(final[cumulative_cost_column]) - float(anchor[cumulative_cost_column])
    trade_count = sum(float(value) > 0.0 for value in rows.get_column(trade_cost_column).to_list())
    return WindowMetrics(
        label=label,
        declared_start=declared_start,
        declared_end=declared_end,
        actual_start=rows.get_column("date")[0],
        actual_end=rows.get_column("date")[-1],
        trading_days=trading_days,
        start_equity=start_equity,
        end_equity=end_equity,
        annualized_return_after_cost=annualized,
        max_drawdown=max_dd,
        calmar=calmar,
        explicit_costs=explicit_costs,
        trade_count=trade_count,
        baseline_start_equity=baseline_start,
        baseline_end_equity=baseline_end,
        baseline_annualized_return=baseline_annualized,
        baseline_max_drawdown=baseline_dd,
        max_drawdown_amplitude_improvement=improvement,
        positive_baseline_cagr_retention=retention,
    )


def _occupancy(frame: pl.DataFrame) -> tuple[dict[str, int], dict[str, dict[str, int]], dict[str, int]]:
    window = frame.filter(pl.col("date").is_between(COMBINED_START, COMBINED_END, closed="both"))
    occupancy = {f"{level:.1f}": 0 for level in ALLOWED_BUDGET_LEVELS}
    regime_occupancy = {
        regime: {f"{level:.1f}": 0 for level in ALLOWED_BUDGET_LEVELS}
        for regime in ("positive", "neutral", "negative")
    }
    transitions: dict[str, int] = {}
    for row in window.iter_rows(named=True):
        budget = float(row["base_budget_after"])
        before = float(row["base_budget_before"])
        key = f"{budget:.1f}"
        occupancy[key] += 1
        regime_occupancy[str(row["trend_regime"])][key] += 1
        transition = f"{before:.1f}->{budget:.1f}"
        transitions[transition] = transitions.get(transition, 0) + 1
    return occupancy, regime_occupancy, dict(sorted(transitions.items()))


def build_layer_one_historical_validation(
    *,
    repo_root: Path,
    daily_path: Path = DEFAULT_DAILY_PATH,
) -> tuple[LayerOneHistoricalValidationReport, pl.DataFrame]:
    root = Path(repo_root).resolve(strict=True)
    contract_id, _, protocol_id, _ = bind_upstream_contracts(repo_root=root)
    evidence_id, _, snapshot_id, risk_symbol = bind_index_data_evidence(repo_root=root)
    if contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID or protocol_id != BOUND_LAYER_ONE_INDEX_PROTOCOL_ID:
        raise ValueError("upstream frozen contract binding drifted")
    if evidence_id != BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_ID or risk_symbol != "000985.CSI":
        raise ValueError("index data evidence binding drifted")
    stamp_contract, stamp_result = verify_a_share_stamp_tax_schedule_file(repo_root=root)
    if not stamp_result.disk_binding_ok:
        raise ValueError("stamp-tax contract disk binding failed")
    snapshot_dir = root / DEFAULT_SNAPSHOT_DIR
    dates, price_close, total_close = _load_index_snapshot(snapshot_dir)
    validation_history_end_index = max(
        index for index, day in enumerate(dates) if day <= COMBINED_END
    )
    frame = _build_daily_path(
        dates=dates[: validation_history_end_index + 1],
        price_close=price_close[: validation_history_end_index + 1],
        total_close=total_close[: validation_history_end_index + 1],
        stamp_contract=stamp_contract,
    )
    if frame.get_column("date")[-1] > COMBINED_END:
        raise ValueError("daily evaluation path must stop at the historical-validation boundary")
    segments = [
        _window_metrics(
            frame,
            label=f"historical_validation_{start.year}_{end.year}",
            declared_start=start,
            declared_end=end,
            equity_column="base_equity",
            cumulative_cost_column="base_cumulative_cost",
            trade_cost_column="base_trade_cost",
        )
        for start, end in CONFIRMED_VALIDATION_SEGMENTS
    ]
    combined = _window_metrics(
        frame,
        label="historical_validation_combined_2013_2021",
        declared_start=COMBINED_START,
        declared_end=COMBINED_END,
        equity_column="base_equity",
        cumulative_cost_column="base_cumulative_cost",
        trade_cost_column="base_trade_cost",
    )
    stress_segments = [
        _window_metrics(
            frame,
            label=f"stress_historical_validation_{start.year}_{end.year}",
            declared_start=start,
            declared_end=end,
            equity_column="stress_equity",
            cumulative_cost_column="stress_cumulative_cost",
            trade_cost_column="stress_trade_cost",
        )
        for start, end in CONFIRMED_VALIDATION_SEGMENTS
    ]
    stress_combined = _window_metrics(
        frame,
        label="stress_historical_validation_combined_2013_2021",
        declared_start=COMBINED_START,
        declared_end=COMBINED_END,
        equity_column="stress_equity",
        cumulative_cost_column="stress_cumulative_cost",
        trade_cost_column="stress_trade_cost",
    )
    occupancy, regime_occupancy, transitions = _occupancy(frame)
    base_retention_pass = (
        combined.positive_baseline_cagr_retention is None
        or combined.positive_baseline_cagr_retention >= 0.6
    )
    gate_values = {
        "per_segment_max_drawdown_floor_pass": all(
            item.max_drawdown >= -0.2 for item in segments
        ),
        "combined_max_drawdown_floor_pass": combined.max_drawdown >= -0.2,
        "combined_positive_after_cost_annualized_return_pass": (
            combined.annualized_return_after_cost > 0.0
        ),
        "combined_calmar_pass": combined.calmar is not None and combined.calmar >= 0.5,
        "combined_baseline_drawdown_improvement_pass": (
            combined.max_drawdown_amplitude_improvement is not None
            and combined.max_drawdown_amplitude_improvement >= 0.25
        ),
        "combined_positive_baseline_cagr_retention_pass": base_retention_pass,
        "stress_max_drawdown_floor_pass": (
            stress_combined.max_drawdown >= -0.2
            and all(item.max_drawdown >= -0.2 for item in stress_segments)
        ),
    }
    gates = HistoricalValidationGates(
        **gate_values,
        all_hard_gates_pass=all(gate_values.values()),
    )
    relative_daily = Path(daily_path)
    if relative_daily.is_absolute():
        try:
            relative_daily = relative_daily.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError("daily_path must be inside repo_root") from exc
    resolved_daily = (root / relative_daily).resolve()
    try:
        resolved_daily.relative_to(root)
    except ValueError as exc:
        raise ValueError("daily_path escapes repo_root") from exc
    report = LayerOneHistoricalValidationReport(
        layer_one_index_data_evidence_id=evidence_id,
        layer_one_index_protocol_id=protocol_id,
        two_layer_decision_contract_id=contract_id,
        data_snapshot_id=snapshot_id,
        risk_state_symbol="000985.CSI",
        performance_benchmark_symbol="H00985.CSI",
        stamp_tax_contract_id=str(stamp_contract.contract_id),
        daily_path=relative_daily.as_posix(),
        daily_table_content_sha256=_frame_content_sha256(frame),
        daily_row_count=frame.height,
        first_action_day=frame.get_column("date")[0],
        last_action_day=frame.get_column("date")[-1],
        validation_start=COMBINED_START,
        validation_end=COMBINED_END,
        initial_capital_cny=INITIAL_CAPITAL_CNY,
        execution_convention=(
            "features_through_P_rebalance_at_D_close_previous_budget_earns_P_to_D_close_return"
        ),
        baseline_convention="cost_free_daily_90pct_total_return_exposure_plus_10pct_cash",
        validation_segments=segments,
        combined=combined,
        stress_validation_segments=stress_segments,
        stress_combined=stress_combined,
        budget_occupancy=occupancy,
        regime_budget_occupancy=regime_occupancy,
        regime_transition_counts=transitions,
        risk_lock_trigger_dates=frame.filter(pl.col("base_risk_lock_triggered"))
        .get_column("date")
        .to_list(),
        stress_risk_lock_trigger_dates=frame.filter(pl.col("stress_risk_lock_triggered"))
        .get_column("date")
        .to_list(),
        gates=gates,
        historical_validation_evidence_pass=gates.all_hard_gates_pass,
    )
    return seal_report(report), frame


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_parquet_atomic(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    frame.write_parquet(temporary)
    temporary.replace(path)


def write_layer_one_historical_validation(
    *,
    repo_root: Path,
    report_path: Path = DEFAULT_REPORT_PATH,
    daily_path: Path = DEFAULT_DAILY_PATH,
) -> LayerOneHistoricalValidationReport:
    root = Path(repo_root).resolve(strict=True)
    report, frame = build_layer_one_historical_validation(repo_root=root, daily_path=daily_path)
    resolved_daily = root / report.daily_path
    report_candidate = Path(report_path)
    resolved_report = report_candidate if report_candidate.is_absolute() else root / report_candidate
    try:
        resolved_report.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError("report_path must be inside repo_root") from exc
    _write_parquet_atomic(resolved_daily, frame)
    _write_json_atomic(resolved_report, report.model_dump(mode="json"))
    return report


def load_report(path: Path) -> LayerOneHistoricalValidationReport:
    try:
        return LayerOneHistoricalValidationReport.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError("layer-one historical validation report is missing or invalid") from exc


def verify_layer_one_historical_validation_file(
    *,
    repo_root: Path,
    report_path: Path = DEFAULT_REPORT_PATH,
) -> LayerOneHistoricalValidationReport:
    root = Path(repo_root).resolve(strict=True)
    report_file = resolve_repo_regular_file(report_path, repo_root=root, field_name="report_path")
    report = load_report(report_file)
    assert_report_self_hash(report)
    daily_file = resolve_repo_regular_file(
        Path(report.daily_path), repo_root=root, field_name="daily_path"
    )
    frame = pl.read_parquet(daily_file)
    if frame.height != report.daily_row_count:
        raise ValueError("daily path row count does not match sealed report")
    if _frame_content_sha256(frame) != report.daily_table_content_sha256:
        raise ValueError("daily path content hash does not match sealed report")
    expected, expected_frame = build_layer_one_historical_validation(
        repo_root=root,
        daily_path=Path(report.daily_path),
    )
    if expected.model_dump(mode="json") != report.model_dump(mode="json"):
        raise ValueError("historical validation report does not match full disk recomputation")
    if _frame_content_sha256(expected_frame) != _frame_content_sha256(frame):
        raise ValueError("daily path does not match full disk recomputation")
    return report


__all__ = [
    "DEFAULT_DAILY_PATH",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_REPORT_PATH",
    "HistoricalValidationGates",
    "LayerOneHistoricalValidationReport",
    "WindowMetrics",
    "assert_report_self_hash",
    "build_layer_one_historical_validation",
    "compute_report_id",
    "load_report",
    "seal_report",
    "verify_layer_one_historical_validation_file",
    "write_layer_one_historical_validation",
]
