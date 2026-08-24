from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.features.fundamental import enrich_fundamental_features
from app.models.config import FundamentalDataConfig
from app.models.features import StockFeatureVector
from app.models.scores import StrategyContext
from app.providers.tushare_fundamentals import (
    fetch_tushare_fundamentals,
    normalize_daily_valuation,
    normalize_fundamental_reports,
)
from app.storage.fundamental_io import (
    build_fundamental_snapshot,
    load_verified_fundamental_snapshot,
    write_fundamental_snapshot_atomically,
)
from app.strategies.loader import load_strategy_config
from app.strategies.quality_value_v1 import QualityValueV1Strategy
from tests.helpers import CONFIG_DIR
from tests.tushare_fakes import FakeTushareClient


def test_tushare_availability_contract_blocks_same_day_valuation() -> None:
    reports, valuation = _tables()
    assert reports["available_at"].min() == datetime(2024, 4, 30, 15, 59)
    may_six = valuation.filter(pl.col("date") == date(2024, 5, 6))
    assert may_six["available_at"].min() == datetime(2024, 5, 6, 9, 0)

    vectors = [_feature("000001.SZ"), _feature("600000.SH")]
    store = _FundamentalFixture(reports, valuation)
    enriched = enrich_fundamental_features(
        vectors,
        store=store,
        as_of=date(2024, 5, 6),
        available_by=datetime(2024, 5, 6, 7, 0),
        config=FundamentalDataConfig(),
    )
    assert len(enriched) == 2
    by_symbol = {item.symbol: item for item in enriched}
    assert by_symbol["000001.SZ"].extra["valuation_age_days"] == 3.0
    assert by_symbol["000001.SZ"].extra["quality_score"] > by_symbol["600000.SH"].extra[
        "quality_score"
    ]


def test_revised_report_without_revision_timestamp_uses_initial_record() -> None:
    report_raw, valuation_raw = _raw_tables()
    revised = report_raw.filter(pl.col("ts_code") == "000001.SZ").with_columns(
        pl.lit("1").alias("update_flag"),
        pl.lit(-999.0).alias("roe"),
        pl.lit(-999.0).alias("roic"),
        pl.lit(-999.0).alias("grossprofit_margin"),
    )
    initial = report_raw.with_columns(pl.lit("0").alias("update_flag"))
    reports = normalize_fundamental_reports(
        pl.concat([initial, revised], how="diagonal_relaxed")
    )
    valuation = normalize_daily_valuation(valuation_raw)
    enriched = enrich_fundamental_features(
        [_feature("000001.SZ"), _feature("600000.SH")],
        store=_FundamentalFixture(reports, valuation),
        as_of=date(2024, 5, 6),
        available_by=datetime(2024, 5, 6, 7, 0),
        config=FundamentalDataConfig(),
    )
    by_symbol = {item.symbol: item for item in enriched}
    assert by_symbol["000001.SZ"].extra["quality_score"] > by_symbol["600000.SH"].extra[
        "quality_score"
    ]


def test_strict_revision_policy_excludes_revision_only_report() -> None:
    report_raw, valuation_raw = _raw_tables()
    reports = normalize_fundamental_reports(
        report_raw.with_columns(
            pl.when(pl.col("ts_code") == "000001.SZ")
            .then(pl.lit("0"))
            .otherwise(pl.lit("1"))
            .alias("update_flag")
        )
    )
    config = FundamentalDataConfig(revision_policy="strict_initial_as_announced")
    enriched = enrich_fundamental_features(
        [_feature("000001.SZ"), _feature("600000.SH")],
        store=_FundamentalFixture(reports, normalize_daily_valuation(valuation_raw)),
        as_of=date(2024, 5, 6),
        available_by=datetime(2024, 5, 6, 7, 0),
        config=config,
    )
    assert [item.symbol for item in enriched] == ["000001.SZ"]


def test_fundamental_snapshot_hash_rejects_tampering(tmp_path: Path) -> None:
    reports, valuation = _tables()
    tables = {"fundamental_reports": reports, "daily_valuation": valuation}
    snapshot = build_fundamental_snapshot(tables, source_name="test")
    write_fundamental_snapshot_atomically(tmp_path / "fundamental", tables, snapshot)
    loaded, _ = load_verified_fundamental_snapshot(tmp_path / "fundamental")
    assert loaded.snapshot_id == snapshot.snapshot_id

    tampered = valuation.with_columns(
        pl.when(pl.col("symbol") == "000001.SZ")
        .then(999.0)
        .otherwise(pl.col("pb"))
        .alias("pb")
    )
    tampered.write_parquet(tmp_path / "fundamental" / "daily_valuation.parquet")
    with pytest.raises(ValueError, match="does not match parquet content hashes"):
        load_verified_fundamental_snapshot(tmp_path / "fundamental")


def test_quality_value_strategy_uses_fundamentals_not_momentum() -> None:
    config = load_strategy_config("csi300_bigquant_public_quality_value_v1", CONFIG_DIR)
    feature = _feature("000001.SZ").model_copy(
        update={
            "ret_20d": -0.20,
            "stock_relative_strength": -0.20,
            "extra": {"quality_score": 90.0, "improvement_score": 80.0, "value_score": 70.0},
        }
    )
    scored = QualityValueV1Strategy(config).score(
        feature,
        StrategyContext(as_of=feature.as_of, market_score=60.0, global_score=55.0),
    )
    assert scored.breakdown.quality_score == 90.0
    assert scored.breakdown.alpha_score > 80.0
    assert scored.final_score > 75.0


def test_value_revision_uses_quality_and_improvement_as_fail_closed_floors() -> None:
    config = load_strategy_config("csi300_bigquant_public_value_quality_guard_v2", CONFIG_DIR)
    strategy = QualityValueV1Strategy(config)
    context = StrategyContext(as_of=date(2024, 5, 6), market_score=60.0, global_score=55.0)
    good = _feature("000001.SZ").model_copy(
        update={"extra": {"quality_score": 40.0, "improvement_score": 30.0, "value_score": 90.0}}
    )
    weak = good.model_copy(
        update={"extra": {"quality_score": 10.0, "improvement_score": 30.0, "value_score": 99.0}}
    )
    assert strategy.score(good, context).final_score > 75.0
    assert strategy.score(weak, context).final_score == 0.0


def test_offline_fetch_writes_verified_overlay(tmp_path: Path) -> None:
    report_raw, valuation_raw = _raw_tables()
    client = FakeTushareClient(
        {"fina_indicator": report_raw, "daily_basic": valuation_raw}
    )
    snapshot = fetch_tushare_fundamentals(
        client=client,
        symbols=["000001.SZ", "600000.SH"],
        start=date(2024, 1, 1),
        end=date(2024, 5, 6),
        dest_dir=tmp_path / "overlay",
        pace_requests=False,
    )
    assert snapshot.row_counts == {"fundamental_reports": 2, "daily_valuation": 4}
    assert client.calls.count("fina_indicator") == 2
    assert client.calls.count("daily_basic") == 2
    loaded, _ = load_verified_fundamental_snapshot(tmp_path / "overlay")
    assert loaded.snapshot_id == snapshot.snapshot_id


class _FundamentalFixture:
    def __init__(self, reports: pl.DataFrame, valuation: pl.DataFrame) -> None:
        self.reports = reports
        self.valuation = valuation

    def get_fundamental_reports(self, available_by: datetime) -> pl.DataFrame:
        return self.reports.filter(pl.col("available_at") <= available_by)

    def get_daily_valuation(self, available_by: datetime) -> pl.DataFrame:
        return self.valuation.filter(pl.col("available_at") <= available_by)


def _tables() -> tuple[pl.DataFrame, pl.DataFrame]:
    reports_raw, valuation_raw = _raw_tables()
    return normalize_fundamental_reports(reports_raw), normalize_daily_valuation(valuation_raw)


def _raw_tables() -> tuple[pl.DataFrame, pl.DataFrame]:
    reports = pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "600000.SH"],
            "ann_date": ["20240430", "20240430"],
            "end_date": ["20240331", "20240331"],
            "update_flag": ["1", "1"],
            "roe": [18.0, 6.0],
            "roic": [15.0, 5.0],
            "grossprofit_margin": [45.0, 15.0],
            "debt_to_assets": [30.0, 70.0],
            "ocf_to_or": [20.0, 3.0],
            "q_sales_yoy": [20.0, -5.0],
            "q_netprofit_yoy": [25.0, -10.0],
            "dt_netprofit_yoy": [22.0, -12.0],
        }
    )
    valuation = pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "600000.SH", "000001.SZ", "600000.SH"],
            "trade_date": ["20240503", "20240503", "20240506", "20240506"],
            "turnover_rate": [2.0, 2.0, 2.0, 2.0],
            "pe_ttm": [8.0, 16.0, 80.0, 1.0],
            "pb": [1.0, 2.0, 10.0, 0.2],
            "ps_ttm": [1.0, 2.0, 10.0, 0.2],
            "total_mv": [1000.0, 1000.0, 1000.0, 1000.0],
            "circ_mv": [800.0, 800.0, 800.0, 800.0],
        }
    )
    return reports, valuation


def _feature(symbol: str) -> StockFeatureVector:
    return StockFeatureVector(
        symbol=symbol,
        as_of=date(2024, 5, 6),
        sector="unknown",
        close=10.0,
        ret_1d=0.0,
        ret_5d=0.0,
        ret_20d=0.0,
        ma20_distance=0.0,
        ma60_distance=0.0,
        volume_ratio_5d=1.0,
        turnover_rate=0.02,
        volatility_20d=0.02,
        atr_14=0.2,
        stock_relative_strength=0.0,
        sector_relative_strength=0.0,
        market_score=60.0,
        global_score=55.0,
        crowding_risk=5.0,
        execution_risk=5.0,
        attention_risk=5.0,
        avg_turnover_20d=200_000_000,
        listing_days=1000,
        is_st=False,
        is_suspended=False,
    )
