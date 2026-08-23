from __future__ import annotations

from datetime import date

from app.backtest.engine import BacktestEngine
from app.models.config import StrategyConfig
from app.strategies.loader import load_strategy_config
from tests.helpers import (
    CONFIG_DIR,
    constant_signal,
    fill_quiet_bars,
    store_from_rows,
    weekdays,
    zero_cost_config,
)


def _run(market_score: float) -> int:
    calendar = weekdays(date(2024, 1, 2), 16)
    signal_day = calendar[0]
    rows: list[dict[str, object]] = []
    for symbol in ("AAA", "BBB", "CCC"):
        rows.extend(fill_quiet_bars(symbol, calendar, None))
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()

    def signals(as_of: date):
        if as_of != signal_day:
            return []
        return constant_signal(["AAA", "BBB", "CCC"], market_score, as_of)

    result = BacktestEngine(store, config, signal_fn=signals).run(signal_day, calendar[12])
    return result.metrics.number_of_trades


def test_market_gate_thresholds_from_yaml() -> None:
    config = load_strategy_config("baseline_v1", CONFIG_DIR)
    assert config.gate_max_new_positions(39.9) == 0
    assert config.gate_max_new_positions(40.0) == 1
    assert config.gate_max_new_positions(54.9) == 1
    assert config.gate_max_new_positions(55.0) == 2
    assert config.gate_max_new_positions(69.9) == 2
    assert config.gate_max_new_positions(70.0) == 3
    assert config.gate_max_new_positions(100.0) == 3


def test_market_gate_blocks_and_limits_new_positions() -> None:
    assert _run(30.0) == 0
    assert _run(50.0) == 1
    assert _run(60.0) == 2
    assert _run(80.0) == 3


def test_gate_bands_are_not_hardcoded_in_strategy_module() -> None:
    raw = (CONFIG_DIR / "baseline_v1.yaml").read_text(encoding="utf-8")
    assert "max_new_positions: 0" in raw
    config = StrategyConfig.model_validate(
        {
            **load_strategy_config("baseline_v1", CONFIG_DIR).model_dump(),
            "market_gate": [{"min": 0, "max": 100.1, "max_new_positions": 1}],
        }
    )
    assert config.gate_max_new_positions(90) == 1
