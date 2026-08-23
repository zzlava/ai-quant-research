from __future__ import annotations

from app.models.config import TradeConfig

PRICE_EPS = 0.01001


def limit_pct(bar: dict[str, object], trade: TradeConfig) -> float | None:
    del trade
    raw = bar.get("price_limit_pct")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw)


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
    pct = limit_pct(bar, trade)
    if pct is None:
        return False
    open_ = float(bar["open"])  # type: ignore[arg-type]
    high = float(bar["high"])  # type: ignore[arg-type]
    low = float(bar["low"])  # type: ignore[arg-type]
    close = float(bar["close"])  # type: ignore[arg-type]
    down, up = limit_bounds(prev_close, pct)
    locked = _near(open_, high) and _near(high, low) and _near(low, close)
    if not locked:
        return False
    if direction == "up":
        return _near(close, up)
    return _near(close, down)
