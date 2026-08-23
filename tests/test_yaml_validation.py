from __future__ import annotations

import copy

import pytest
import yaml
from pydantic import ValidationError

from app.models.config import StrategyConfig
from tests.helpers import CONFIG_DIR


def _payload() -> dict:
    with (CONFIG_DIR / "baseline_v1.yaml").open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict)
    return data


def test_unknown_field_is_rejected() -> None:
    payload = _payload()
    payload["typo_weight"] = 1
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(payload)


def test_nested_typo_is_rejected() -> None:
    payload = _payload()
    payload["trade"]["take_prfit"] = 0.03
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(payload)


def test_stop_loss_must_be_negative() -> None:
    payload = _payload()
    payload["trade"]["stop_loss"] = 0.025
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(payload)


def test_holding_days_must_be_positive() -> None:
    payload = _payload()
    payload["trade"]["max_holding_days"] = 0
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(payload)


def test_overlapping_gate_is_rejected() -> None:
    payload = _payload()
    payload["market_gate"] = [
        {"min": 0.0, "max": 50.0, "max_new_positions": 0},
        {"min": 40.0, "max": 100.1, "max_new_positions": 1},
    ]
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(payload)


def test_valid_yaml_still_loads() -> None:
    config = StrategyConfig.model_validate(_payload())
    assert config.data.market_index == "IDX_CSI300"
    assert config.data.global_symbol == "GLB_SPX"
    assert config.config_id is None
    assert config.run_id() == config.name
    copied = copy.deepcopy(_payload())
    StrategyConfig.model_validate(copied)


def test_optional_config_id_is_allowed() -> None:
    payload = _payload()
    payload["config_id"] = "baseline_real_cn_v1"
    config = StrategyConfig.model_validate(payload)
    assert config.run_id() == "baseline_real_cn_v1"
