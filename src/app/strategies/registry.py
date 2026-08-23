from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from pathlib import Path

from app.models.config import StrategyConfig
from app.strategies.base import BaseStrategy


class StrategyRegistry:
    _factories: dict[str, Callable[[StrategyConfig], BaseStrategy]] = {}
    _discovered: bool = False

    @classmethod
    def register(cls, name: str) -> Callable[[type[BaseStrategy]], type[BaseStrategy]]:
        def decorator(strategy_cls: type[BaseStrategy]) -> type[BaseStrategy]:
            cls._factories[name] = strategy_cls
            return strategy_cls

        return decorator

    @classmethod
    def discover(cls) -> None:
        package_dir = Path(__file__).resolve().parent
        for module in pkgutil.iter_modules([str(package_dir)]):
            if module.name.startswith("_") or module.name in {"base", "registry", "loader"}:
                continue
            importlib.import_module(f"app.strategies.{module.name}")
        cls._discovered = True

    @classmethod
    def _ensure_discovered(cls) -> None:
        if not cls._discovered:
            cls.discover()

    @classmethod
    def create(cls, name: str, config: StrategyConfig) -> BaseStrategy:
        cls._ensure_discovered()
        if name not in cls._factories:
            known = ", ".join(sorted(cls._factories)) or "<empty>"
            raise KeyError(f"unknown strategy '{name}'. registered: {known}")
        return cls._factories[name](config)

    @classmethod
    def names(cls) -> list[str]:
        cls._ensure_discovered()
        return sorted(cls._factories)

    @classmethod
    def contains(cls, name: str) -> bool:
        cls._ensure_discovered()
        return name in cls._factories
