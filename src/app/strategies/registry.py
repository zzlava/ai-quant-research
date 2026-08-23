from __future__ import annotations

from collections.abc import Callable

from app.models.config import StrategyConfig
from app.strategies.base import BaseStrategy


class StrategyRegistry:
    _factories: dict[str, Callable[[StrategyConfig], BaseStrategy]] = {}

    @classmethod
    def register(
        cls, name: str
    ) -> Callable[[type[BaseStrategy]], type[BaseStrategy]]:
        def decorator(strategy_cls: type[BaseStrategy]) -> type[BaseStrategy]:
            cls._factories[name] = strategy_cls
            return strategy_cls

        return decorator

    @classmethod
    def create(cls, name: str, config: StrategyConfig) -> BaseStrategy:
        if name not in cls._factories:
            known = ", ".join(sorted(cls._factories)) or "<empty>"
            raise KeyError(f"unknown strategy '{name}'. registered: {known}")
        return cls._factories[name](config)

    @classmethod
    def names(cls) -> list[str]:
        return sorted(cls._factories)

    @classmethod
    def contains(cls, name: str) -> bool:
        return name in cls._factories
