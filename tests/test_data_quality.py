from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.errors import DataQualityError, MissingBenchmarkError
from app.features.engine import FeatureEngine
from app.models.config import DataConfig, StrategyConfig
from app.providers.csv_provider import CsvProvider
from app.strategies.loader import load_strategy_config
from tests.helpers import CONFIG_DIR, fill_quiet_bars, store_from_rows, weekdays


def test_duplicate_bars_fail_quality_check(tmp_path: Path) -> None:
    frame = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA"],
            "date": [date(2024, 1, 2), date(2024, 1, 2)],
            "open": [10.0, 10.0],
            "high": [10.2, 10.2],
            "low": [9.8, 9.8],
            "close": [10.1, 10.1],
            "volume": [1.0, 1.0],
            "amount": [10.0, 10.0],
            "turnover_rate": [0.01, 0.01],
            "is_st": [False, False],
            "is_suspended": [False, False],
        }
    )
    frame.write_csv(tmp_path / "daily_bars.csv")
    with pytest.raises(DataQualityError, match="duplicate"):
        CsvProvider(tmp_path)


def test_invalid_ohlc_fails_quality_check(tmp_path: Path) -> None:
    frame = pl.DataFrame(
        {
            "symbol": ["AAA"],
            "date": [date(2024, 1, 2)],
            "open": [10.0],
            "high": [9.0],
            "low": [11.0],
            "close": [10.0],
            "volume": [1.0],
            "amount": [10.0],
            "turnover_rate": [0.01],
            "is_st": [False],
            "is_suspended": [False],
        }
    )
    frame.write_csv(tmp_path / "daily_bars.csv")
    with pytest.raises(DataQualityError, match="invalid OHLC"):
        CsvProvider(tmp_path)


def test_missing_benchmark_raises() -> None:
    calendar = weekdays(date(2023, 10, 2), 80)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    base = load_strategy_config("baseline_v1", CONFIG_DIR)
    data = base.data.model_dump()
    data["market_index"] = "NO_SUCH_INDEX"
    data["sessions"]["NO_SUCH_INDEX"] = data["sessions"]["IDX_CSI300"]
    config = StrategyConfig(
        name=base.name,
        version=base.version,
        weights=base.weights,
        universe=base.universe,
        market_gate=base.market_gate,
        trade=base.trade,
        portfolio=base.portfolio,
        costs=base.costs,
        data=DataConfig.model_validate(data),
    )
    with pytest.raises(MissingBenchmarkError, match="NO_SUCH_INDEX"):
        FeatureEngine(store, config).compute_all(calendar[-1])
