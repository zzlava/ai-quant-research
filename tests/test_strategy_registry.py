from __future__ import annotations

import pytest

from app.strategies.base import BaseStrategy
from app.strategies.baseline_v1 import BaselineV1Strategy
from app.strategies.loader import load_strategy_config
from app.strategies.registry import StrategyRegistry
from tests.helpers import CONFIG_DIR


def test_registry_creates_baseline_v1() -> None:
    config = load_strategy_config("baseline_v1", CONFIG_DIR)
    strategy = StrategyRegistry.create("baseline_v1", config)
    assert isinstance(strategy, BaselineV1Strategy)
    assert isinstance(strategy, BaseStrategy)
    assert "baseline_v1" in StrategyRegistry.names()


def test_registry_rejects_unknown_strategy() -> None:
    config = load_strategy_config("baseline_v1", CONFIG_DIR)
    with pytest.raises(KeyError, match="unknown strategy"):
        StrategyRegistry.create("does_not_exist", config)
