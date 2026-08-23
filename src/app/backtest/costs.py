from __future__ import annotations

from app.models.config import CostConfig

LOT_SIZE = 100


def commission(notional: float, config: CostConfig) -> float:
    if notional <= 0:
        return 0.0
    return max(notional * config.commission_rate, config.min_commission)


def stamp_tax(notional: float, config: CostConfig) -> float:
    if notional <= 0:
        return 0.0
    return notional * config.stamp_tax_rate


def apply_slippage(price: float, config: CostConfig, side: str) -> float:
    bps = config.slippage_bps / 10_000.0
    if side == "buy":
        return price * (1.0 + bps)
    return price * (1.0 - bps)


def buy_cost(price: float, shares: int, config: CostConfig) -> tuple[float, float]:
    notional = price * shares
    comm = commission(notional, config)
    return notional + comm, comm


def sell_cost(price: float, shares: int, config: CostConfig) -> tuple[float, float, float]:
    notional = price * shares
    comm = commission(notional, config)
    tax = stamp_tax(notional, config)
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
