from __future__ import annotations

from datetime import date

from app.models.config import CostConfig

LOT_SIZE = 100


def commission(notional: float, config: CostConfig) -> float:
    if notional <= 0:
        return 0.0
    return max(notional * config.commission_rate, config.min_commission)


def stamp_tax_rate_for(trade_date: date | None, config: CostConfig) -> float:
    """Return the seller stamp-tax rate known for the execution date.

    The legacy flat rate remains available for old/demo configurations.  A
    dated schedule takes precedence whenever its first effective date has
    begun, so historical runs can model tax policy changes without mutating
    their other execution assumptions.
    """
    if trade_date is not None:
        effective = [band for band in config.stamp_tax_schedule if band.effective_from <= trade_date]
        if effective:
            return effective[-1].rate
    return config.stamp_tax_rate


def stamp_tax(notional: float, config: CostConfig, trade_date: date | None = None) -> float:
    if notional <= 0:
        return 0.0
    return notional * stamp_tax_rate_for(trade_date, config)


def apply_slippage(price: float, config: CostConfig, side: str) -> float:
    bps = config.slippage_bps / 10_000.0
    if side == "buy":
        return price * (1.0 + bps)
    return price * (1.0 - bps)


def buy_cost(price: float, shares: int, config: CostConfig) -> tuple[float, float]:
    notional = price * shares
    comm = commission(notional, config)
    return notional + comm, comm


def sell_cost(
    price: float,
    shares: int,
    config: CostConfig,
    trade_date: date | None = None,
) -> tuple[float, float, float]:
    notional = price * shares
    comm = commission(notional, config)
    tax = stamp_tax(notional, config, trade_date)
    net = notional - comm - tax
    return net, comm, tax


def shares_affordable(cash: float, raw_price: float, config: CostConfig, lot_size: int = LOT_SIZE) -> int:
    if cash <= 0 or raw_price <= 0:
        return 0
    price = apply_slippage(raw_price, config, "buy")
    max_lots = int(cash / (price * lot_size))
    for lots in range(max_lots, 0, -1):
        shares = lots * lot_size
        total, _ = buy_cost(price, shares, config)
        if total <= cash + 1e-9:
            return shares
    return 0
