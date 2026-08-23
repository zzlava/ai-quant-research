from __future__ import annotations

import math
from datetime import date

import polars as pl

from app.clock import decision_at_utc
from app.errors import MissingBenchmarkError
from app.models.config import StrategyConfig
from app.models.features import StockFeatureVector
from app.storage.protocol import MarketStore

STOCK_FEATURE_HISTORY_BARS = 60


def required_history_bars(min_history_bars: int) -> int:
    """Longest lookback needed before a date can produce usable features."""
    return max(int(min_history_bars), STOCK_FEATURE_HISTORY_BARS)


def clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def scale(value: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return clip((value - lo) / (hi - lo) * 100.0, 0.0, 100.0)


class FeatureEngine:
    """Compute point-in-time features. Store queries always pass as_of."""

    def __init__(self, store: MarketStore, config: StrategyConfig) -> None:
        self.store = store
        self.config = config
        self.index_symbol = config.data.market_index
        self.global_symbol = config.data.global_symbol

    def compute_all(self, as_of: date) -> list[StockFeatureVector]:
        daily = self.store.get_daily_bars(as_of=as_of)
        if daily.is_empty():
            return []
        daily = daily.filter(pl.col("date") <= as_of)
        if daily.is_empty():
            return []

        instruments = {
            i.symbol: i for i in self.store.get_instruments() if not i.is_index and not i.is_global
        }
        enriched = self._stock_features(daily)
        as_of_rows = enriched.filter(pl.col("date") == as_of)
        if as_of_rows.is_empty():
            return []

        index_ret_20d, market_score = self._market_snapshot(as_of)
        global_ret_20d, global_score = self._global_snapshot(as_of)
        sector_ret = self._sector_returns(as_of_rows)

        vectors: list[StockFeatureVector] = []
        for row in as_of_rows.to_dicts():
            symbol = str(row["symbol"])
            inst = instruments.get(symbol)
            if inst is None:
                continue
            needed = (
                "ret_1d",
                "ret_5d",
                "ret_20d",
                "ma20_distance",
                "ma60_distance",
                "volume_ratio_5d",
                "volatility_20d",
                "atr_14",
                "avg_turnover_20d",
            )
            if any(not _finite(row.get(key)) for key in needed):
                continue

            stock_rs = float(row["ret_20d"]) - index_ret_20d
            sector_rs = float(sector_ret.get(inst.sector, 0.0)) - index_ret_20d
            close = float(row["close"])
            atr = float(row["atr_14"])
            turnover_rate = float(row["turnover_rate"])
            volume_ratio = float(row["volume_ratio_5d"])
            ret_5d = float(row["ret_5d"])

            crowding = 0.5 * scale(volume_ratio, 1.0, 4.0) + 0.5 * scale(ret_5d, 0.0, 0.15)
            execution = 0.5 * scale(0.03 - turnover_rate, 0.0, 0.03) + 0.5 * scale(
                atr / max(close, 1e-9), 0.01, 0.05
            )

            listing_days = (as_of - inst.listing_date).days
            vectors.append(
                StockFeatureVector(
                    symbol=symbol,
                    as_of=as_of,
                    sector=inst.sector,
                    close=close,
                    ret_1d=float(row["ret_1d"]),
                    ret_5d=ret_5d,
                    ret_20d=float(row["ret_20d"]),
                    ma20_distance=float(row["ma20_distance"]),
                    ma60_distance=float(row["ma60_distance"]),
                    volume_ratio_5d=volume_ratio,
                    turnover_rate=turnover_rate,
                    volatility_20d=float(row["volatility_20d"]),
                    atr_14=atr,
                    stock_relative_strength=stock_rs,
                    sector_relative_strength=sector_rs,
                    market_score=market_score,
                    global_score=global_score,
                    crowding_risk=crowding,
                    execution_risk=execution,
                    avg_turnover_20d=float(row["avg_turnover_20d"]),
                    listing_days=listing_days,
                    is_st=bool(row["is_st"]),
                    is_suspended=bool(row["is_suspended"]),
                    index_ret_20d=index_ret_20d,
                    global_ret_20d=global_ret_20d,
                )
            )
        return vectors

    def compute_one(self, symbol: str, as_of: date) -> StockFeatureVector | None:
        for vector in self.compute_all(as_of):
            if vector.symbol == symbol:
                return vector
        return None

    def _stock_features(self, daily: pl.DataFrame) -> pl.DataFrame:
        daily = daily.sort(["symbol", "date"])
        prev_close = pl.col("close").shift(1).over("symbol")
        true_range = pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - prev_close).abs(),
            (pl.col("low") - prev_close).abs(),
        )
        ret_1d = pl.col("close") / prev_close - 1.0
        prior_vol_5 = pl.col("volume").shift(1).over("symbol").rolling_mean(window_size=5).over("symbol")
        return daily.with_columns(
            [
                ret_1d.alias("ret_1d"),
                (pl.col("close") / pl.col("close").shift(5).over("symbol") - 1.0).alias("ret_5d"),
                (pl.col("close") / pl.col("close").shift(20).over("symbol") - 1.0).alias("ret_20d"),
                (pl.col("close") / pl.col("close").rolling_mean(window_size=20).over("symbol") - 1.0).alias(
                    "ma20_distance"
                ),
                (
                    pl.col("close")
                    / pl.col("close").rolling_mean(window_size=STOCK_FEATURE_HISTORY_BARS).over("symbol")
                    - 1.0
                ).alias("ma60_distance"),
                (pl.col("volume") / prior_vol_5).alias("volume_ratio_5d"),
                ret_1d.rolling_std(window_size=20).over("symbol").alias("volatility_20d"),
                true_range.rolling_mean(window_size=14).over("symbol").alias("atr_14"),
                pl.col("amount").rolling_mean(window_size=20).over("symbol").alias("avg_turnover_20d"),
            ]
        )

    def _market_snapshot(self, as_of: date) -> tuple[float, float]:
        index = self.store.get_index_bars(as_of=as_of, symbol=self.index_symbol)
        index = index.filter(pl.col("date") <= as_of).sort("date")
        needed = self.config.data.min_history_bars
        if index.height < needed:
            raise MissingBenchmarkError(
                f"market index '{self.index_symbol}' has {index.height} bars as of {as_of}, need {needed}"
            )
        closes = [float(x) for x in index["close"].to_list()]
        last = closes[-1]
        ret_20d = last / closes[-21] - 1.0
        ma20 = sum(closes[-20:]) / 20.0
        ma20_dist = last / ma20 - 1.0
        market_score = 0.6 * scale(ret_20d, -0.10, 0.10) + 0.4 * scale(ma20_dist, -0.08, 0.08)
        return ret_20d, market_score

    def _global_snapshot(self, as_of: date) -> tuple[float, float]:
        glob = self.store.get_global_bars(as_of=as_of, symbol=self.global_symbol)
        if glob.is_empty():
            raise MissingBenchmarkError(f"global series '{self.global_symbol}' is missing as of {as_of}")
        if "available_at" not in glob.columns:
            raise MissingBenchmarkError(
                f"global series '{self.global_symbol}' has no available_at; cannot apply the data clock"
            )
        cutoff = decision_at_utc(as_of, self.config.data)
        glob = glob.filter(pl.col("available_at") <= cutoff).sort("date")
        needed = self.config.data.min_history_bars
        if glob.height < needed:
            raise MissingBenchmarkError(
                f"global series '{self.global_symbol}' has {glob.height} available bars "
                f"at A-share decision {as_of} {self.config.data.decision_time}, need {needed}"
            )
        closes = [float(x) for x in glob["close"].to_list()]
        last = closes[-1]
        ret_20d = last / closes[-21] - 1.0
        return ret_20d, scale(ret_20d, -0.08, 0.08)

    def _sector_returns(self, as_of_rows: pl.DataFrame) -> dict[str, float]:
        if "ret_20d" not in as_of_rows.columns:
            return {}
        valid = as_of_rows.filter(pl.col("ret_20d").is_not_null())
        if valid.is_empty():
            return {}
        inst = pl.DataFrame(
            [
                {"symbol": i.symbol, "sector": i.sector}
                for i in self.store.get_instruments()
                if not i.is_index and not i.is_global
            ]
        )
        joined = valid.join(inst, on="symbol", how="left")
        grouped = joined.group_by("sector").agg(pl.col("ret_20d").mean().alias("sector_ret"))
        return {str(r["sector"]): float(r["sector_ret"]) for r in grouped.to_dicts() if r["sector"]}


def _finite(value: object) -> bool:
    return value is not None and isinstance(value, int | float) and math.isfinite(float(value))
