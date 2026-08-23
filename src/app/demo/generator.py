from __future__ import annotations

import math
import random
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.models.market import DailyBar, GlobalBar, Instrument, MarketBundle
from app.providers._frames import bars_to_frame, global_to_frame, instruments_to_frame

DEMO_SEED = 42
SECTORS = ("bank", "tech", "consumer")
INDEX_CSI300 = "IDX_CSI300"
INDEX_SSE = "IDX_SSE"
GLOBAL_SPX = "GLB_SPX"
GLOBAL_HSI = "GLB_HSI"


def weekday_calendar(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def generate_demo_market(
    seed: int = DEMO_SEED,
    n_stocks: int = 50,
    start: date = date(2022, 6, 1),
    end: date = date(2024, 12, 31),
) -> MarketBundle:
    """Deterministic synthetic A-share-like market. No network."""
    rng = random.Random(seed)
    calendar = weekday_calendar(start, end)
    if len(calendar) < 80:
        raise ValueError("demo calendar is too short for feature warmup")

    instruments: list[Instrument] = []
    daily_bars: list[DailyBar] = []

    market_returns = [_gauss(rng, 0.00025, 0.011) for _ in calendar]
    sector_returns = {
        sector: [_gauss(rng, 0.0001, 0.008) for _ in calendar] for sector in SECTORS
    }

    for i in range(1, n_stocks + 1):
        symbol = f"STK{i:04d}"
        sector = SECTORS[(i - 1) % len(SECTORS)]
        is_st = i >= n_stocks - 1
        newly_listed = i == n_stocks - 2
        illiquid = i in {n_stocks - 3, n_stocks - 4}
        listing_date = start + timedelta(days=400) if newly_listed else date(2018, 1, 1)
        name = f"{'ST' if is_st else ''}Demo{i:04d}"
        instruments.append(
            Instrument(
                symbol=symbol,
                name=name,
                sector=sector,
                listing_date=listing_date,
                is_index=False,
            )
        )

        price = 8.0 + (i % 17) * 0.7
        volume_base = 400_000.0 if illiquid else 12_000_000.0
        float_shares = 80_000_000.0 if illiquid else 400_000_000.0
        beta = 0.7 + (i % 8) * 0.1
        idio_vol = 0.018 + (i % 5) * 0.002

        for t, dt in enumerate(calendar):
            if dt < listing_date:
                continue
            is_suspended = (not is_st) and (i % 11 == 0) and (t % 97 == i % 97)
            ret = (
                0.15 * market_returns[t]
                + 0.35 * sector_returns[sector][t]
                + beta * 0.25 * market_returns[t]
                + _gauss(rng, 0.0, idio_vol)
            )
            if is_st:
                ret -= 0.0004
            prev = price
            if is_suspended:
                o = h = low = c = round(prev, 2)
                volume = 0.0
                amount = 0.0
                turnover_rate = 0.0
            else:
                o = max(0.5, prev * (1.0 + _gauss(rng, 0.0, 0.004)))
                c = max(0.5, prev * (1.0 + ret))
                h = max(o, c) * (1.0 + abs(_gauss(rng, 0.004, 0.003)))
                low = min(o, c) * (1.0 - abs(_gauss(rng, 0.004, 0.003)))
                low = max(0.5, low)
                volume = max(0.0, volume_base * (1.0 + _gauss(rng, 0.0, 0.25)))
                amount = volume * (o + c) / 2.0
                turnover_rate = min(0.25, volume / float_shares)
                o, h, low, c = (round(x, 2) for x in (o, h, low, c))
                price = c
            daily_bars.append(
                DailyBar(
                    symbol=symbol,
                    date=dt,
                    open=o,
                    high=h,
                    low=low,
                    close=c,
                    volume=round(volume, 0),
                    amount=round(amount, 2),
                    turnover_rate=round(turnover_rate, 6),
                    is_st=is_st,
                    is_suspended=is_suspended,
                )
            )

    index_bars = _build_index_bars(calendar, market_returns, rng)
    global_bars = _build_global_bars(calendar, rng)
    instruments.extend(
        [
            Instrument(
                symbol=INDEX_CSI300,
                name="Demo CSI300",
                sector="index",
                listing_date=date(2005, 1, 1),
                is_index=True,
            ),
            Instrument(
                symbol=INDEX_SSE,
                name="Demo SSE",
                sector="index",
                listing_date=date(1990, 1, 1),
                is_index=True,
            ),
            Instrument(
                symbol=GLOBAL_SPX,
                name="Demo SPX",
                sector="global",
                listing_date=date(1957, 1, 1),
                is_global=True,
            ),
            Instrument(
                symbol=GLOBAL_HSI,
                name="Demo HSI",
                sector="global",
                listing_date=date(1969, 1, 1),
                is_global=True,
            ),
        ]
    )
    return MarketBundle(
        instruments=instruments,
        daily_bars=daily_bars,
        index_bars=index_bars,
        global_bars=global_bars,
        calendar=calendar,
    )


def write_demo_parquet(bundle: MarketBundle, parquet_dir: Path) -> None:
    parquet_dir.mkdir(parents=True, exist_ok=True)
    bars_to_frame(bundle.daily_bars).write_parquet(parquet_dir / "daily_bars.parquet")
    bars_to_frame(bundle.index_bars).write_parquet(parquet_dir / "index_bars.parquet")
    global_to_frame(bundle.global_bars).write_parquet(parquet_dir / "global_bars.parquet")
    instruments_to_frame(bundle.instruments).write_parquet(parquet_dir / "instruments.parquet")
    pl.DataFrame({"date": bundle.calendar}).with_columns(pl.col("date").cast(pl.Date)).write_parquet(
        parquet_dir / "calendar.parquet"
    )


def _build_index_bars(
    calendar: list[date],
    market_returns: list[float],
    rng: random.Random,
) -> list[DailyBar]:
    out: list[DailyBar] = []
    prices = {INDEX_CSI300: 3800.0, INDEX_SSE: 3100.0}
    for t, dt in enumerate(calendar):
        for symbol, shock in (
            (INDEX_CSI300, market_returns[t]),
            (INDEX_SSE, market_returns[t] * 0.9 + _gauss(rng, 0.0, 0.003)),
        ):
            prev = prices[symbol]
            o = prev * (1.0 + _gauss(rng, 0.0, 0.002))
            c = prev * (1.0 + shock)
            h = max(o, c) * 1.004
            low = min(o, c) * 0.996
            prices[symbol] = c
            volume = 2.0e10
            out.append(
                DailyBar(
                    symbol=symbol,
                    date=dt,
                    open=round(o, 2),
                    high=round(h, 2),
                    low=round(low, 2),
                    close=round(c, 2),
                    volume=volume,
                    amount=volume * c,
                    turnover_rate=0.01,
                )
            )
    return out


def _build_global_bars(calendar: list[date], rng: random.Random) -> list[GlobalBar]:
    out: list[GlobalBar] = []
    prices = {GLOBAL_SPX: 4200.0, GLOBAL_HSI: 18000.0}
    for dt in calendar:
        for symbol, drift, vol in (
            (GLOBAL_SPX, 0.0003, 0.009),
            (GLOBAL_HSI, 0.0001, 0.012),
        ):
            prev = prices[symbol]
            ret = _gauss(rng, drift, vol)
            close = prev * (1.0 + ret)
            prices[symbol] = close
            out.append(GlobalBar(symbol=symbol, date=dt, close=round(close, 2), ret_1d=ret))
    return out


def _gauss(rng: random.Random, mu: float, sigma: float) -> float:
    # Box-Muller, deterministic via rng
    u1 = max(rng.random(), 1e-12)
    u2 = rng.random()
    z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
    return mu + sigma * z
