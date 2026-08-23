from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.features.engine import clip
from app.models.features import StockFeatureVector
from app.models.scores import ScoreBreakdown, ScoreResult, StrategyContext
from app.scoring.engine import ScoringEngine
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


def test_real_config_has_separate_run_id_and_disabled_sector() -> None:
    real = load_strategy_config("baseline_real_cn_v1", CONFIG_DIR)
    demo = load_strategy_config("baseline_v1", CONFIG_DIR)
    assert real.name == "baseline_v1"
    assert real.config_id == "baseline_real_cn_v1"
    assert real.run_id() != demo.run_id()
    assert real.config_hash() != demo.config_hash()
    assert real.weights.sector_score == 0.0
    feat = _feature()
    ctx = StrategyContext(as_of=feat.as_of, market_score=70.0, global_score=60.0)
    scored = BaselineV1Strategy(real).score(feat, ctx)
    assert scored.config_id == "baseline_real_cn_v1"
    assert scored.strategy_name == "baseline_v1"


def _score_row(*, config_hash: str, snapshot_id: str, config_id: str, final: float) -> ScoreResult:
    return ScoreResult(
        symbol="AAA",
        score_date=date(2024, 1, 15),
        strategy_name="baseline_v1",
        config_id=config_id,
        strategy_version="1.0.0",
        strategy_config_hash=config_hash,
        final_score=final,
        breakdown=ScoreBreakdown(
            market_score=70.0,
            global_score=60.0,
            sector_score=0.0,
            alpha_score=70.0,
            crowding_risk=10.0,
            execution_risk=10.0,
            final_score=final,
        ),
        data_snapshot_id=snapshot_id,
    )


def test_same_day_scores_from_different_runs_do_not_overwrite(tmp_path: Path) -> None:
    dest = tmp_path / "scores.parquet"
    engine = object.__new__(ScoringEngine)
    demo = _score_row(config_hash="hash-demo", snapshot_id="snap-demo", config_id="baseline_v1", final=80.0)
    real = _score_row(config_hash="hash-real", snapshot_id="snap-real", config_id="baseline_real_cn_v1", final=70.0)
    engine.persist([demo], dest)
    engine.persist([real], dest)
    stored = pl.read_parquet(dest)
    assert stored.height == 2
    ids = set(zip(stored["strategy_config_hash"].to_list(), stored["data_snapshot_id"].to_list(), strict=True))
    assert ids == {("hash-demo", "snap-demo"), ("hash-real", "snap-real")}

    replacement = _score_row(config_hash="hash-demo", snapshot_id="snap-demo", config_id="baseline_v1", final=11.0)
    engine.persist([replacement], dest)
    stored = pl.read_parquet(dest)
    assert stored.height == 2
    demo_row = stored.filter(pl.col("strategy_config_hash") == "hash-demo").to_dicts()[0]
    real_row = stored.filter(pl.col("strategy_config_hash") == "hash-real").to_dicts()[0]
    assert demo_row["final_score"] == 11.0
    assert real_row["final_score"] == 70.0
    assert set(stored["config_id"].to_list()) == {"baseline_v1", "baseline_real_cn_v1"}


def test_legacy_scores_parquet_without_config_id_can_upgrade(tmp_path: Path) -> None:
    dest = tmp_path / "scores.parquet"
    pl.DataFrame(
        {
            "symbol": ["AAA"],
            "score_date": [date(2024, 1, 14)],
            "strategy_name": ["baseline_v1"],
            "strategy_version": ["1.0.0"],
            "strategy_config_hash": ["hash-old"],
            "final_score": [80.0],
            "market_score": [70.0],
            "global_score": [60.0],
            "sector_score": [50.0],
            "alpha_score": [70.0],
            "crowding_risk": [10.0],
            "execution_risk": [10.0],
            "sector": ["tech"],
            "data_snapshot_id": ["snap-old"],
        }
    ).write_parquet(dest)
    engine = object.__new__(ScoringEngine)
    engine.persist(
        [_score_row(config_hash="hash-new", snapshot_id="snap-new", config_id="baseline_real_cn_v1", final=65.0)],
        dest,
    )
    stored = pl.read_parquet(dest)
    assert "config_id" in stored.columns
    assert stored.height == 2
    old_row = stored.filter(pl.col("strategy_config_hash") == "hash-old").to_dicts()[0]
    new_row = stored.filter(pl.col("strategy_config_hash") == "hash-new").to_dicts()[0]
    assert old_row["config_id"] == ""
    assert new_row["config_id"] == "baseline_real_cn_v1"
    assert new_row["final_score"] == 65.0
