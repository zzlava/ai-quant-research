"""Pure two-layer portfolio composition (math only).

Explicit Layer-1 budgets and Layer-2 stock sleeves are composed into a total
portfolio target. This module never scores, backtests, loads StrategyConfig,
or places orders.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_WEIGHT_ABS_TOL = 1e-12


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_nonempty_id(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} evidence/id is required and must be non-empty")
    return value


def _finite_nonnegative(value: float, *, field_name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if value < 0.0:
        raise ValueError(f"{field_name} must be >= 0")
    return value


def _finite_positive(value: float, *, field_name: str) -> float:
    value = _finite_nonnegative(value, field_name=field_name)
    if value <= 0.0:
        raise ValueError(f"{field_name} must be > 0")
    return value


class LayerOneBudgetDecision(_StrictModel):
    """Explicit first-layer risk budgets. No defaults: caller must pass all legs."""

    as_of: date
    contract_id: str = Field(min_length=1)
    data_evidence_id: str = Field(min_length=1)
    config_evidence_id: str = Field(min_length=1)
    stock_budget: float
    cash_budget: float
    etf_budget: float | None = None
    etf_symbol: str | None = None

    @field_validator("contract_id", "data_evidence_id", "config_evidence_id", mode="before")
    @classmethod
    def _require_ids(cls, value: object, info: object) -> object:
        field_name = getattr(info, "field_name", "id")
        return _require_nonempty_id(value, field_name=str(field_name))

    @field_validator("stock_budget", "cash_budget")
    @classmethod
    def _validate_required_budgets(cls, value: float, info: object) -> float:
        field_name = getattr(info, "field_name", "budget")
        return _finite_nonnegative(value, field_name=str(field_name))

    @field_validator("etf_budget")
    @classmethod
    def _validate_etf_budget(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite_nonnegative(value, field_name="etf_budget")

    @field_validator("etf_symbol", mode="before")
    @classmethod
    def _reject_blank_etf_symbol(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and value.strip() == "":
            raise ValueError("etf_symbol must be null when unused, not empty string")
        return value

    @model_validator(mode="after")
    def _validate_budget_conservation(self) -> LayerOneBudgetDecision:
        etf = 0.0 if self.etf_budget is None else self.etf_budget
        total = self.stock_budget + self.cash_budget + etf
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=_WEIGHT_ABS_TOL):
            raise ValueError("layer-one budgets must be non-negative and sum to 1")
        if self.etf_budget is None:
            if self.etf_symbol is not None:
                raise ValueError("etf_symbol requires etf_budget")
        else:
            if self.etf_budget > 0.0 and self.etf_symbol is None:
                raise ValueError("positive etf_budget requires etf_symbol")
            if self.etf_budget == 0.0 and self.etf_symbol is not None:
                raise ValueError("etf_symbol requires positive etf_budget")
        return self


class StockTargetWeight(_StrictModel):
    symbol: str = Field(min_length=1)
    weight: float

    @field_validator("symbol", mode="before")
    @classmethod
    def _reject_blank_symbol(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            raise ValueError("symbol must be non-empty")
        return value

    @field_validator("weight")
    @classmethod
    def _validate_weight(cls, value: float) -> float:
        return _finite_positive(value, field_name="weight")


class LayerTwoStockSleeve(_StrictModel):
    """Explicit second-layer stock sleeve weights inside the stock budget."""

    as_of: date
    contract_id: str = Field(min_length=1)
    data_evidence_id: str = Field(min_length=1)
    config_evidence_id: str = Field(min_length=1)
    target_weights: list[StockTargetWeight] = Field(min_length=1)

    @field_validator("contract_id", "data_evidence_id", "config_evidence_id", mode="before")
    @classmethod
    def _require_ids(cls, value: object, info: object) -> object:
        field_name = getattr(info, "field_name", "id")
        return _require_nonempty_id(value, field_name=str(field_name))

    @model_validator(mode="after")
    def _validate_sleeve(self) -> LayerTwoStockSleeve:
        symbols = [row.symbol for row in self.target_weights]
        if len(symbols) != len(set(symbols)):
            raise ValueError("layer-two stock sleeve symbols must be unique")
        total = sum(row.weight for row in self.target_weights)
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=_WEIGHT_ABS_TOL):
            raise ValueError("layer-two stock sleeve weights must be finite positive and sum to 1")
        return self


class ComposedPortfolioTarget(_StrictModel):
    as_of: date
    contract_id: str
    data_evidence_id: str
    config_evidence_id: str
    stock_budget: float
    cash_weight: float
    etf_weight: float
    etf_symbol: str | None
    stock_target_weights: dict[str, float]
    diagnostic_only: Literal[True] = True
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False


def compose_two_layer_portfolio(
    *,
    layer_one: LayerOneBudgetDecision,
    layer_two: LayerTwoStockSleeve,
) -> ComposedPortfolioTarget:
    """Math-only composition: stock_i = sleeve_i * stock_budget (+ cash/ETF)."""
    if not layer_one.data_evidence_id or not layer_one.config_evidence_id:
        raise ValueError("layer-one data/config evidence IDs are required")
    if not layer_two.data_evidence_id or not layer_two.config_evidence_id:
        raise ValueError("layer-two data/config evidence IDs are required")
    if layer_one.as_of != layer_two.as_of:
        raise ValueError("layer-one and layer-two as_of dates are inconsistent")
    if layer_one.contract_id != layer_two.contract_id:
        raise ValueError("layer-one and layer-two contract_id are inconsistent")
    if layer_one.data_evidence_id != layer_two.data_evidence_id:
        raise ValueError("layer-one and layer-two data_evidence_id are inconsistent")
    if layer_one.config_evidence_id != layer_two.config_evidence_id:
        raise ValueError("layer-one and layer-two config_evidence_id are inconsistent")

    defensive_symbols: set[str] = set()
    etf_weight = 0.0 if layer_one.etf_budget is None else layer_one.etf_budget
    etf_symbol = layer_one.etf_symbol
    if etf_symbol is not None:
        defensive_symbols.add(etf_symbol)

    stock_weights: dict[str, float] = {}
    for row in layer_two.target_weights:
        if row.symbol in defensive_symbols:
            raise ValueError(
                f"stock sleeve symbol conflicts with defensive asset: {row.symbol}"
            )
        if row.symbol in stock_weights:
            raise ValueError(f"duplicate stock symbol in composition: {row.symbol}")
        stock_weights[row.symbol] = row.weight * layer_one.stock_budget

    total = sum(stock_weights.values()) + layer_one.cash_budget + etf_weight
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=_WEIGHT_ABS_TOL):
        raise ValueError("composed portfolio weights must conserve to 1")

    return ComposedPortfolioTarget(
        as_of=layer_one.as_of,
        contract_id=layer_one.contract_id,
        data_evidence_id=layer_one.data_evidence_id,
        config_evidence_id=layer_one.config_evidence_id,
        stock_budget=layer_one.stock_budget,
        cash_weight=layer_one.cash_budget,
        etf_weight=etf_weight,
        etf_symbol=etf_symbol,
        stock_target_weights=stock_weights,
    )


__all__ = [
    "ComposedPortfolioTarget",
    "LayerOneBudgetDecision",
    "LayerTwoStockSleeve",
    "StockTargetWeight",
    "compose_two_layer_portfolio",
]
