from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.config import StrategyConfig
from app.models.features import StockFeatureVector
from app.models.scores import ScoreResult, StrategyContext


class BaseStrategy(ABC):
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def version(self) -> str:
        return self.config.version

    @abstractmethod
    def score(self, feature: StockFeatureVector, context: StrategyContext) -> ScoreResult:
        raise NotImplementedError
