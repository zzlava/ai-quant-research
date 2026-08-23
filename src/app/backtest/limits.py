from __future__ import annotations

from app.models.config import TradeConfig

PRICE_EPS = 0.01001


def limit_pct(bar: dict[str, object], trade: TradeConfig) -> float:
    if bool(bar.get("is_st")):
        return trade.st_limit_pct
    return trade.limit_pct


def limit_bounds(prev_close: float, pct: float) -> tuple[float, float]:
    return prev_close * (1.0 - pct), prev_close * (1.0 + pct)


def _near(price: float, target: float) -> bool:
    return abs(price - target) <= PRICE_EPS


def is_one_word_limit(
    bar: dict[str, object],
    prev_close: float | None,
    trade: TradeConfig,
    direction: str,
) -> bool:
    if prev_close is None or prev_close <= 0 or not trade.model_limit_moves:
        return False
    open_ = float(bar["open"])  # type: ignore[arg-type]
    high = float(bar["high"])  # type: ignore[arg-type]
    low = float(bar["low"])  # type: ignore[arg-type]
    close = float(bar["close"])  # type: ignore[arg-type]
    down, up = limit_bounds(prev_close, limit_pct(bar, trade))
    locked = _near(open_, high) and _near(high, low) and _near(low, close)
    if not locked:
        return False
    if direction == "up":
        return _near(close, up)
    return _near(close, down)
