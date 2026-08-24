from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from app.errors import DataQualityError, MissingBenchmarkError
from app.features.engine import FeatureEngine
from app.models.config import DataConfig, StrategyConfig
from app.providers.csv_provider import CsvProvider
from app.storage.quality import normalize_available_at, parse_available_at_utc, validate_global, validate_ohlcv
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
            "price_limit_pct": [0.10, 0.10],
            "adj_open": [10.0, 10.0],
            "adj_high": [10.2, 10.2],
            "adj_low": [9.8, 9.8],
            "adj_close": [10.1, 10.1],
            "adj_factor": [1.0, 1.0],
            "pre_close": [10.0, 10.0],
            "up_limit": [11.0, 11.0],
            "down_limit": [9.0, 9.0],
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
            "price_limit_pct": [0.10],
            "adj_open": [10.0],
            "adj_high": [9.0],
            "adj_low": [11.0],
            "adj_close": [10.0],
            "adj_factor": [1.0],
            "pre_close": [10.0],
            "up_limit": [11.0],
            "down_limit": [9.0],
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


def test_global_nan_and_infinity_close_are_rejected() -> None:
    base = {
        "symbol": ["GLB_SPX", "GLB_SPX"],
        "date": [date(2024, 1, 2), date(2024, 1, 3)],
        "available_at": [datetime(2024, 1, 2, 21, 0, 0), datetime(2024, 1, 3, 21, 0, 0)],
    }
    nan_frame = pl.DataFrame({**base, "close": [float("nan"), 100.0]})
    inf_frame = pl.DataFrame({**base, "close": [float("inf"), 100.0]})
    neg_inf = pl.DataFrame({**base, "close": [float("-inf"), 100.0]})
    missing_close = pl.DataFrame({**base, "close": [None, 100.0]})
    missing_ts = pl.DataFrame({**base, "close": [100.0, 101.0], "available_at": [None, datetime(2024, 1, 3, 21, 0, 0)]})
    with pytest.raises(DataQualityError, match="non-finite close"):
        validate_global(nan_frame)
    with pytest.raises(DataQualityError, match="non-finite close"):
        validate_global(inf_frame)
    with pytest.raises(DataQualityError, match="non-finite close"):
        validate_global(neg_inf)
    with pytest.raises(DataQualityError, match="non-finite close"):
        validate_global(missing_close)
    with pytest.raises(DataQualityError, match="available_at"):
        validate_global(missing_ts)


def test_ohlcv_rejects_non_finite_and_inconsistent_bounds() -> None:
    def frame(**overrides: object) -> pl.DataFrame:
        payload: dict[str, object] = {
            "symbol": ["AAA"],
            "date": [date(2024, 1, 2)],
            "open": [10.0],
            "high": [10.2],
            "low": [9.8],
            "close": [10.1],
            "volume": [1.0],
            "amount": [10.0],
        }
        payload.update(overrides)
        return pl.DataFrame(payload)

    with pytest.raises(DataQualityError, match="non-finite"):
        validate_ohlcv(frame(close=[float("nan")]), "daily_bars")
    with pytest.raises(DataQualityError, match="non-finite"):
        validate_ohlcv(frame(volume=[float("inf")]), "daily_bars")
    with pytest.raises(DataQualityError, match="invalid OHLC"):
        validate_ohlcv(frame(high=[9.0]), "daily_bars")
    with pytest.raises(DataQualityError, match="invalid OHLC"):
        validate_ohlcv(frame(low=[10.5]), "daily_bars")


def test_ohlcv_rejects_invalid_adjusted_prices() -> None:
    frame = pl.DataFrame(
        {
            "symbol": ["AAA"],
            "date": [date(2024, 1, 2)],
            "open": [10.0],
            "high": [10.2],
            "low": [9.8],
            "close": [10.1],
            "volume": [1.0],
            "amount": [10.0],
            "adj_open": [10.0],
            "adj_high": [9.0],
            "adj_low": [11.0],
            "adj_close": [10.0],
            "adj_factor": [1.0],
        }
    )
    with pytest.raises(DataQualityError, match="invalid adjusted OHLC"):
        validate_ohlcv(frame, "daily_bars")


def test_minus_five_offset_available_at_is_rejected() -> None:
    with pytest.raises(DataQualityError, match="non-zero offsets"):
        parse_available_at_utc("2024-01-02T16:00:00-05:00")
    aware = datetime(2024, 1, 2, 16, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    with pytest.raises(DataQualityError, match="non-zero offsets"):
        parse_available_at_utc(aware)
    frame = pl.DataFrame(
        {
            "symbol": ["GLB_SPX"],
            "date": [date(2024, 1, 2)],
            "close": [100.0],
            "available_at": ["2024-01-02T16:00:00-05:00"],
        }
    )
    with pytest.raises(DataQualityError, match="non-zero offsets"):
        normalize_available_at(frame)
    with pytest.raises(DataQualityError, match="non-zero offsets"):
        validate_global(frame)


def test_zulu_and_naive_available_at_are_utc() -> None:
    naive = parse_available_at_utc("2024-01-02T21:00:00")
    zulu = parse_available_at_utc("2024-01-02T21:00:00Z")
    plus_zero = parse_available_at_utc("2024-01-02T21:00:00+00:00")
    assert naive == datetime(2024, 1, 2, 21, 0, 0)
    assert zulu == naive
    assert plus_zero == naive
    assert zulu.tzinfo is None
