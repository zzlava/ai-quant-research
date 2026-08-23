from __future__ import annotations

from app.models.config import UniverseConfig
from app.models.features import StockFeatureVector


class UniverseFilter:
    def __init__(self, config: UniverseConfig) -> None:
        self.config = config

    def apply(self, features: list[StockFeatureVector]) -> list[StockFeatureVector]:
        out: list[StockFeatureVector] = []
        for feat in features:
            if self.config.exclude_st and feat.is_st:
                continue
            if self.config.exclude_suspended and feat.is_suspended:
                continue
            if feat.listing_days < self.config.min_listing_days:
                continue
            if feat.avg_turnover_20d < self.config.min_avg_turnover_20d:
                continue
            out.append(feat)
        return out
