from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import yaml

from app.models.config import StrategyConfig
from app.models.market import Instrument
from app.models.scores import ScoreBreakdown, ScoreResult
from app.providers._frames import empty_global, instruments_to_frame
from app.storage.memory import InMemoryStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config" / "strategies"


def weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    current = start
    while len(out) < n:
        if current.weekday() < 5:
            out.append(current)
        current += timedelta(days=1)
    return out


def load_test_config(**overrides: object) -> StrategyConfig:
    with (CONFIG_DIR / "baseline_v1.yaml").open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    payload.update(overrides)
    return StrategyConfig.model_validate(payload)


def zero_cost_config(**overrides: object) -> StrategyConfig:
    with (CONFIG_DIR / "baseline_v1.yaml").open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    payload["costs"] = {
        "commission_rate": 0.0,
        "min_commission": 0.0,
        "stamp_tax_rate": 0.0,
        "slippage_bps": 0.0,
    }
    payload["portfolio"] = {
        "initial_cash": 80_000,
        "max_positions": 3,
        "weighting": "equal_weight",
    }
    payload.update(overrides)
    return StrategyConfig.model_validate(payload)


def bar(
    symbol: str,
    dt: date,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 12_000_000,
    amount: float = 200_000_000,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "date": dt,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
        "turnover_rate": 0.03,
        "is_st": False,
        "is_suspended": False,
    }


def fill_quiet_bars(
    symbol: str,
    calendar: list[date],
    overrides: dict[date, dict[str, float]] | None = None,
) -> list[dict[str, object]]:
    rows = []
    price = 10.0
    extras = overrides or {}
    for dt in calendar:
        spec = extras.get(dt, {})
        o = spec.get("open", price)
        c = spec.get("close", o)
        h = spec.get("high", max(o, c) + 0.05)
        low = spec.get("low", min(o, c) - 0.05)
        rows.append(bar(symbol, dt, o, h, low, c))
        price = c
    return rows


def store_from_rows(calendar: list[date], rows: list[dict[str, object]]) -> InMemoryStore:
    symbols = sorted({str(r["symbol"]) for r in rows})
    instruments = [
        Instrument(symbol=s, name=s, sector="tech", listing_date=date(2018, 1, 1)) for s in symbols
    ]
    daily = pl.DataFrame(rows).with_columns(
        [
            pl.col("date").cast(pl.Date),
            pl.col("is_st").cast(pl.Boolean),
            pl.col("is_suspended").cast(pl.Boolean),
        ]
    )
    return InMemoryStore(
        instruments=instruments_to_frame(instruments),
        daily=daily,
        index=daily.clear(),
        global_bars=empty_global(),
        calendar=calendar,
    )


def constant_signal(symbols: list[str], market_score: float, as_of: date) -> list[ScoreResult]:
    results: list[ScoreResult] = []
    for i, symbol in enumerate(symbols):
        final = 90.0 - i
        results.append(
            ScoreResult(
                symbol=symbol,
                score_date=as_of,
                strategy_name="baseline_v1",
                strategy_version="1.0.0",
                strategy_config_hash="test",
                final_score=final,
                breakdown=ScoreBreakdown(
                    market_score=market_score,
                    global_score=60.0,
                    sector_score=60.0,
                    alpha_score=70.0,
                    crowding_risk=10.0,
                    execution_risk=10.0,
                    final_score=final,
                ),
            )
        )
    return results
