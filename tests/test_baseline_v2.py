from __future__ import annotations

from app.models.scores import StrategyContext
from app.strategies.baseline_v2 import BaselineV2Strategy
from app.strategies.loader import load_strategy_config
from tests.helpers import CONFIG_DIR
from tests.test_strategy_config import _feature


def test_v2_rank_is_independent_of_market_wide_inputs() -> None:
    config = load_strategy_config("csi300_bigquant_public_reconstruction_v2", CONFIG_DIR)
    feature = _feature()
    low = BaselineV2Strategy(config).score(
        feature, StrategyContext(as_of=feature.as_of, market_score=30.0, global_score=40.0)
    )
    high = BaselineV2Strategy(config).score(
        feature, StrategyContext(as_of=feature.as_of, market_score=90.0, global_score=80.0)
    )
    assert low.final_score == high.final_score
    assert low.breakdown.regime_score == 34.0
    assert high.breakdown.regime_score == 86.0


def test_v2_requires_its_explicit_ranking_contract() -> None:
    config = load_strategy_config("csi300_bigquant_public_reconstruction_v2", CONFIG_DIR)
    assert config.ranking is not None
    assert config.ranking.min_score == 60.0
    assert config.regime.market_weight == 0.60
    assert config.regime.global_weight == 0.40


def test_v2_base_collector_is_not_a_historical_csi300_membership_claim() -> None:
    config = load_strategy_config("baseline_real_cn_raw_backward_v2", CONFIG_DIR)
    assert config.research_scope == "controlled_sample"
    assert config.universe.mode == "manual_static"
    assert config.data.adjustment == "backward"
    assert config.data.require_point_in_time_adjustment is True


def test_v2_medium_term_reversal_only_inverts_momentum_alpha() -> None:
    momentum_config = load_strategy_config("csi300_bigquant_public_reconstruction_v2", CONFIG_DIR)
    reversal_config = load_strategy_config(
        "csi300_bigquant_public_reconstruction_reversal_v3", CONFIG_DIR
    )
    feature = _feature()
    context = StrategyContext(as_of=feature.as_of, market_score=50.0, global_score=50.0)

    momentum = BaselineV2Strategy(momentum_config).score(feature, context)
    reversal = BaselineV2Strategy(reversal_config).score(feature, context)

    assert reversal.breakdown.alpha_score == 100.0 - momentum.breakdown.alpha_score
    assert reversal.breakdown.crowding_risk == momentum.breakdown.crowding_risk
    assert reversal.breakdown.execution_risk == momentum.breakdown.execution_risk
    assert reversal.breakdown.attention_risk == momentum.breakdown.attention_risk


def test_v3_attention_candidate_only_adds_declared_penalty() -> None:
    reversal = load_strategy_config(
        "csi300_bigquant_public_reconstruction_reversal_v3", CONFIG_DIR
    )
    attention = load_strategy_config(
        "csi300_bigquant_public_reconstruction_reversal_attention_v3", CONFIG_DIR
    )

    assert reversal.ranking is not None
    assert attention.ranking is not None
    assert reversal.ranking.attention_penalty == 0.0
    assert attention.ranking.attention_penalty == 0.25
    assert attention.ranking.model_copy(update={"attention_penalty": 0.0}) == reversal.ranking
    assert attention.trade == reversal.trade
    assert attention.costs == reversal.costs


def test_optional_execution_controls_preserve_existing_config_hashes() -> None:
    v2 = load_strategy_config("csi300_bigquant_public_reconstruction_v2", CONFIG_DIR)
    reversal = load_strategy_config(
        "csi300_bigquant_public_reconstruction_reversal_v3", CONFIG_DIR
    )
    fixed = load_strategy_config(
        "csi300_bigquant_public_reconstruction_fixed_rebalance_v4", CONFIG_DIR
    )
    selected = load_strategy_config(
        "all_a_share_historical_value_portfolio_selected_v2", CONFIG_DIR
    )

    assert v2.config_hash() == "21e0c0ed3fa82677"
    assert reversal.config_hash() == "3e8101157b44aff5"
    assert fixed.config_hash() == "a311e33948bf4c56"
    assert selected.config_hash() == "796b793856dcd02a"
