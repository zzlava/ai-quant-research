from __future__ import annotations

from datetime import date

import polars as pl

from app.demo.generator import GLOBAL_SPX, generate_demo_market
from app.features.engine import FeatureEngine
from app.models.market import Instrument
from app.providers.demo_provider import DemoProvider
from app.scoring.engine import ScoringEngine
from app.storage.memory import InMemoryStore
from app.strategies.loader import load_strategy_config
from tests.helpers import CONFIG_DIR


class LeakyStore:
    """Intentionally returns history after as_of. Feature/score must still ignore it."""

    def __init__(self, inner: InMemoryStore) -> None:
        self.inner = inner

    def get_instruments(self) -> list[Instrument]:
        return self.inner.get_instruments()

    def get_calendar(self, start: date, end: date) -> list[date]:
        return self.inner.get_calendar(start, end)

    def get_daily_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        frame = self.inner.daily
        if symbol:
            frame = frame.filter(pl.col("symbol") == symbol)
        if start:
            frame = frame.filter(pl.col("date") >= start)
        return frame.sort(["symbol", "date"])

    def get_index_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        frame = self.inner.index
        if symbol:
            frame = frame.filter(pl.col("symbol") == symbol)
        if start:
            frame = frame.filter(pl.col("date") >= start)
        return frame.sort(["symbol", "date"])

    def get_global_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        frame = self.inner.global_bars
        if symbol:
            frame = frame.filter(pl.col("symbol") == symbol)
        if start:
            frame = frame.filter(pl.col("date") >= start)
        return frame.sort(["symbol", "date"])

    def next_trading_day(self, after: date) -> date | None:
        return self.inner.next_trading_day(after)

    def trading_days_after(self, after: date, n: int) -> list[date]:
        return self.inner.trading_days_after(after, n)

    def snapshot(self):
        return self.inner.snapshot()

    def get_universe_members(
        self,
        universe_id: str,
        as_of: date,
        available_by,
        *,
        expected_constituents: int | None = None,
        require_available_cross_section: bool = False,
    ):
        return self.inner.get_universe_members(
            universe_id,
            as_of,
            available_by,
            expected_constituents=expected_constituents,
            require_available_cross_section=require_available_cross_section,
        )


def _shock_future(frame: pl.DataFrame, as_of: date, factor: float = 7.5) -> pl.DataFrame:
    cols = [c for c in ("open", "high", "low", "close", "volume", "amount") if c in frame.columns]
    return frame.with_columns(
        [
            pl.when(pl.col("date") > as_of).then(pl.col(col) * factor).otherwise(pl.col(col)).alias(col)
            for col in cols
        ]
    )


def test_features_and_scores_ignore_future_prices() -> None:
    bundle = generate_demo_market(
        seed=42,
        n_stocks=12,
        start=date(2023, 1, 3),
        end=date(2024, 3, 29),
    )
    provider = DemoProvider(bundle=bundle)
    clean = InMemoryStore.from_provider(provider)
    as_of = date(2024, 1, 15)
    assert as_of in clean.get_calendar(date(2023, 1, 3), date(2024, 3, 29))

    leaky_clean = LeakyStore(clean)
    config = load_strategy_config("baseline_v1", CONFIG_DIR)
    features_before = FeatureEngine(leaky_clean, config).compute_all(as_of)
    scores_before = ScoringEngine(leaky_clean, config).run(as_of)
    assert features_before
    assert scores_before

    poisoned = clean.clone()
    poisoned.replace_daily(_shock_future(poisoned.daily, as_of))
    poisoned.index = _shock_future(poisoned.index, as_of)
    poisoned.global_bars = _shock_future(poisoned.global_bars, as_of)
    leaky_poisoned = LeakyStore(poisoned)

    features_after = FeatureEngine(leaky_poisoned, config).compute_all(as_of)
    scores_after = ScoringEngine(leaky_poisoned, config).run(as_of)

    before_map = {f.symbol: f.model_dump() for f in features_before}
    after_map = {f.symbol: f.model_dump() for f in features_after}
    assert before_map.keys() == after_map.keys()
    for symbol in before_map:
        assert before_map[symbol] == after_map[symbol]

    score_before = {s.symbol: s.model_dump(exclude={"feature", "data_snapshot_id"}) for s in scores_before}
    score_after = {s.symbol: s.model_dump(exclude={"feature", "data_snapshot_id"}) for s in scores_after}
    assert score_before == score_after


def test_same_day_us_close_does_not_change_a_share_score() -> None:
    bundle = generate_demo_market(
        seed=42,
        n_stocks=12,
        start=date(2023, 1, 3),
        end=date(2024, 3, 29),
    )
    store = InMemoryStore.from_provider(DemoProvider(bundle=bundle))
    as_of = date(2024, 1, 15)
    config = load_strategy_config("baseline_v1", CONFIG_DIR)
    before = ScoringEngine(store, config).run(as_of)
    assert before

    poisoned = store.clone()
    poisoned.global_bars = poisoned.global_bars.with_columns(
        pl.when((pl.col("date") == as_of) & (pl.col("symbol") == GLOBAL_SPX))
        .then(pl.col("close") * 10.0)
        .otherwise(pl.col("close"))
        .alias("close")
    )
    after = ScoringEngine(LeakyStore(poisoned), config).run(as_of)
    assert {s.symbol: s.model_dump(exclude={"feature", "data_snapshot_id"}) for s in before} == {
        s.symbol: s.model_dump(exclude={"feature", "data_snapshot_id"}) for s in after
    }
