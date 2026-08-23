from __future__ import annotations

from datetime import date
from pathlib import Path

from app.features.engine import clip
from app.models.features import StockFeatureVector
from app.models.scores import StrategyContext
from app.strategies.baseline_v1 import BaselineV1Strategy
from app.strategies.loader import load_strategy_config
from tests.helpers import CONFIG_DIR


def _feature() -> StockFeatureVector:
    return StockFeatureVector(
        symbol="AAA",
        as_of=date(2024, 1, 15),
        sector="tech",
        close=10.0,
        ret_1d=0.01,
        ret_5d=0.02,
        ret_20d=0.04,
        ma20_distance=0.02,
        ma60_distance=0.03,
        volume_ratio_5d=1.2,
        turnover_rate=0.02,
        volatility_20d=0.02,
        atr_14=0.2,
        stock_relative_strength=0.03,
        sector_relative_strength=0.02,
        market_score=70.0,
        global_score=60.0,
        crowding_risk=20.0,
        execution_risk=15.0,
        avg_turnover_20d=200_000_000,
        listing_days=400,
        is_st=False,
        is_suspended=False,
    )


def test_baseline_weights_come_from_yaml() -> None:
    config = load_strategy_config("baseline_v1", CONFIG_DIR)
    assert config.weights.market_score == 0.25
    assert config.weights.global_score == 0.15
    assert config.weights.sector_score == 0.20
    assert config.weights.alpha_score == 0.40
    assert config.weights.crowding_risk == 0.10
    assert config.weights.execution_risk == 0.10
    text = (CONFIG_DIR / "baseline_v1.yaml").read_text(encoding="utf-8")
    assert "0.25" in text and "0.40" in text


def test_changing_yaml_weights_changes_final_score(tmp_path: Path) -> None:
    original = (CONFIG_DIR / "baseline_v1.yaml").read_text(encoding="utf-8")
    mutated = original.replace("market_score: 0.25", "market_score: 0.05").replace(
        "alpha_score: 0.40", "alpha_score: 0.60"
    )
    (tmp_path / "baseline_v1.yaml").write_text(mutated, encoding="utf-8")
    base = load_strategy_config("baseline_v1", CONFIG_DIR)
    alt = load_strategy_config("baseline_v1", tmp_path)
    feat = _feature()
    ctx = StrategyContext(as_of=feat.as_of, market_score=70.0, global_score=60.0)
    s1 = BaselineV1Strategy(base).score(feat, ctx)
    s2 = BaselineV1Strategy(alt).score(feat, ctx)
    assert s1.final_score != s2.final_score
    b = s1.breakdown
    expected = clip(
        0.25 * b.market_score
        + 0.15 * b.global_score
        + 0.20 * b.sector_score
        + 0.40 * b.alpha_score
        - 0.10 * b.crowding_risk
        - 0.10 * b.execution_risk,
        0.0,
        100.0,
    )
    assert abs(s1.final_score - expected) < 1e-9
    assert 0.0 <= s1.final_score <= 100.0
    assert 0.0 <= s2.final_score <= 100.0
    assert s1.strategy_config_hash != s2.strategy_config_hash
