from __future__ import annotations

from datetime import date

import pytest

from app.backtest.engine import BacktestEngine, _pearson
from app.models.features import StockFeatureVector
from app.models.scores import StrategyContext
from app.strategies.balanced_multifactor_v1 import BalancedMultifactorV1Strategy
from app.strategies.loader import load_strategy_config
from tests.helpers import CONFIG_DIR, store_from_rows, weekdays


def _config():
    return load_strategy_config("all_a_share_balanced_multifactor_v1", CONFIG_DIR)


def _reversal_config():
    return load_strategy_config("all_a_share_balanced_value_reversal_v2", CONFIG_DIR)


def _defensive_config():
    return load_strategy_config("all_a_share_balanced_value_defensive_v3", CONFIG_DIR)


def _feature(*, size: float, institutional: float) -> StockFeatureVector:
    return StockFeatureVector(
        symbol="000001.SZ",
        as_of=date(2024, 6, 28),
        sector="unknown",
        close=10.0,
        ret_1d=0.0,
        ret_5d=0.02,
        ret_20d=0.08,
        ret_120d=0.20,
        ma20_distance=0.03,
        ma60_distance=0.05,
        volume_ratio_5d=1.0,
        turnover_rate=0.02,
        volatility_20d=0.02,
        atr_14=0.2,
        stock_relative_strength=0.03,
        sector_relative_strength=0.0,
        market_score=60.0,
        global_score=55.0,
        crowding_risk=5.0,
        execution_risk=5.0,
        attention_risk=5.0,
        avg_turnover_20d=200_000_000,
        listing_days=1000,
        is_st=False,
        is_suspended=False,
        index_ret_120d=0.10,
        extra={
            "quality_score": 70.0,
            "improvement_score": 65.0,
            "value_score": 60.0,
            "size_score": size,
            "institutional_score": institutional,
        },
    )


def test_small_cap_and_low_sponsorship_reduce_score() -> None:
    strategy = BalancedMultifactorV1Strategy(_config())
    context = StrategyContext(
        as_of=date(2024, 6, 28), market_score=60.0, global_score=55.0
    )
    supported = strategy.score(_feature(size=90.0, institutional=90.0), context)
    unsupported = strategy.score(_feature(size=10.0, institutional=10.0), context)
    assert supported.final_score - unsupported.final_score == pytest.approx(24.0)
    assert supported.breakdown.momentum_score > 50.0


def test_missing_sponsorship_is_omitted_from_weight_denominator() -> None:
    strategy = BalancedMultifactorV1Strategy(_config())
    feature = _feature(size=50.0, institutional=50.0)
    feature.extra.pop("institutional_score")
    result = strategy.score(
        feature,
        StrategyContext(
            as_of=feature.as_of, market_score=60.0, global_score=55.0
        ),
    )
    assert result.breakdown.institutional_score is None
    assert result.breakdown.alpha_score > 0


def test_correlation_cap_fails_closed_on_missing_or_high_history() -> None:
    calendar = weekdays(date(2024, 1, 2), 121)
    store = store_from_rows(
        calendar,
        [
            {
                "symbol": "000001.SZ",
                "date": day,
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "volume": 1.0,
                "amount": 200_000_000.0,
                "turnover_rate": 0.01,
                "is_st": False,
                "is_suspended": False,
                "price_limit_pct": 0.1,
            }
            for day in calendar
        ],
        universe_id="all_a_share_derived_liquid_cn",
    )
    engine = BacktestEngine(store, _config(), signal_fn=lambda _: [])
    identical = {day: float(index) for index, day in enumerate(calendar)}
    # Direct helper contract: incomplete history is rejected, not ignored.
    assert not engine._passes_correlation_cap("A", {"B"}, {})
    assert _pearson([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)
    assert not engine._passes_correlation_cap(
        "A", {"B"}, {"A": identical, "B": identical}
    )


def test_reversal_candidate_penalizes_high_recent_continuation() -> None:
    strategy = BalancedMultifactorV1Strategy(_reversal_config())
    context = StrategyContext(
        as_of=date(2024, 6, 28), market_score=60.0, global_score=55.0
    )
    high = _feature(size=50.0, institutional=50.0).model_copy(
        update={"ret_120d": 0.30, "ma60_distance": 0.15}
    )
    low = _feature(size=50.0, institutional=50.0).model_copy(
        update={"ret_120d": -0.20, "ma60_distance": -0.15}
    )
    assert strategy.score(low, context).final_score > strategy.score(
        high, context
    ).final_score


def test_defensive_candidate_uses_continuation_only_as_a_floor() -> None:
    strategy = BalancedMultifactorV1Strategy(_defensive_config())
    context = StrategyContext(
        as_of=date(2024, 6, 28), market_score=60.0, global_score=55.0
    )
    falling = _feature(size=50.0, institutional=50.0).model_copy(
        update={"ret_120d": -0.25, "ma60_distance": -0.15}
    )
    stable = _feature(size=50.0, institutional=50.0).model_copy(
        update={"ret_120d": 0.0, "ma60_distance": 0.0}
    )
    assert strategy.score(falling, context).final_score == 0.0
    assert strategy.score(stable, context).final_score > 0.0
