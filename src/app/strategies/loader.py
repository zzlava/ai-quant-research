from __future__ import annotations

from pathlib import Path

import yaml

from app.models.config import StrategyConfig
from app.settings import get_settings


def load_strategy_config(name: str, config_dir: Path | None = None) -> StrategyConfig:
    root = config_dir or get_settings().strategies_dir
    path = Path(root) / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"strategy config not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"invalid strategy YAML: {path}")
    config = StrategyConfig.model_validate(payload)
    config.source_path = str(path)
    return config
