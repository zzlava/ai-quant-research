from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.features.ownership import enrich_ownership_features
from app.models.config import OwnershipDataConfig
from app.models.features import StockFeatureVector
from app.providers.tushare_ownership import normalize_top10_float_holders
from app.storage.ownership_io import (
    build_ownership_snapshot,
    load_verified_ownership_snapshot,
    write_ownership_snapshot_atomically,
)


class _Fixture:
    def __init__(self, table: pl.DataFrame) -> None:
        self.table = table

    def get_top10_float_holders(self, available_by: datetime) -> pl.DataFrame:
        return self.table.filter(pl.col("available_at") <= available_by)


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
        avg_turnover_20d=200_000_000,
        listing_days=1000,
        is_st=False,
        is_suspended=False,
    )


def _table() -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for symbol, institutional in (("000001.SZ", 8), ("600000.SH", 2)):
        for index in range(10):
            rows.append(
                {
                    "ts_code": symbol,
                    "ann_date": "20240430",
                    "end_date": "20240331",
                    "holder_name": f"{symbol}-{index}",
                    "hold_float_ratio": 5.0,
                    "holder_type": "基金" if index < institutional else "个人",
                }
            )
    return normalize_top10_float_holders(pl.DataFrame(rows))


def test_complete_pit_groups_rank_non_personal_sponsorship() -> None:
    table = _table()
    enriched = enrich_ownership_features(
        [_feature("000001.SZ"), _feature("600000.SH")],
        store=_Fixture(table),
        as_of=date(2024, 5, 6),
        available_by=datetime(2024, 5, 6, 9, 30),
        config=OwnershipDataConfig(min_cross_section_coverage=1.0),
    )
    by_symbol = {item.symbol: item for item in enriched}
    assert by_symbol["000001.SZ"].extra["institutional_proxy_ratio"] == 40.0
    assert by_symbol["600000.SH"].extra["institutional_proxy_ratio"] == 10.0
    assert by_symbol["000001.SZ"].extra["institutional_score"] == 100.0
    assert by_symbol["600000.SH"].extra["institutional_score"] == 0.0


def test_incomplete_group_and_missing_cross_section_fail_closed() -> None:
    table = _table().filter(
        ~((pl.col("symbol") == "600000.SH") & (pl.col("holder_name").str.ends_with("-9")))
    )
    with pytest.raises(ValueError, match="missing ownership cannot be treated as zero"):
        enrich_ownership_features(
            [_feature("000001.SZ"), _feature("600000.SH")],
            store=_Fixture(table),
            as_of=date(2024, 5, 6),
            available_by=datetime(2024, 5, 6, 9, 30),
            config=OwnershipDataConfig(min_cross_section_coverage=1.0),
        )


def test_optional_ownership_preserves_unknown_without_zero_fill() -> None:
    table = _table().filter(
        ~((pl.col("symbol") == "600000.SH") & (pl.col("holder_name").str.ends_with("-9")))
    )
    enriched = enrich_ownership_features(
        [_feature("000001.SZ"), _feature("600000.SH")],
        store=_Fixture(table),
        as_of=date(2024, 5, 6),
        available_by=datetime(2024, 5, 6, 9, 30),
        config=OwnershipDataConfig(
            required=False,
            min_cross_section_coverage=1.0,
        ),
    )
    by_symbol = {item.symbol: item for item in enriched}
    assert set(by_symbol) == {"000001.SZ", "600000.SH"}
    assert by_symbol["000001.SZ"].extra["ownership_proxy_known"] == 1.0
    assert by_symbol["600000.SH"].extra["ownership_proxy_known"] == 0.0
    assert "institutional_score" not in by_symbol["600000.SH"].extra


def test_conflicting_same_holder_variants_are_preserved_and_fail_closed() -> None:
    raw = pl.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "ann_date": "20240430",
                "end_date": "20240331",
                "holder_name": f"holder-{index}",
                "hold_amount": 1_000_000.0 + index,
                "hold_ratio": 1.0,
                "hold_float_ratio": 5.0,
                "hold_change": 0.0,
                "holder_type": "基金" if index < 5 else "个人",
            }
            for index in range(10)
        ]
        + [
            {
                "ts_code": "000001.SZ",
                "ann_date": "20240430",
                "end_date": "20240331",
                "holder_name": "holder-0",
                "hold_amount": 1_100_000.0,
                "hold_ratio": 1.1,
                "hold_float_ratio": 5.5,
                "hold_change": 100_000.0,
                "holder_type": "基金",
            }
        ]
    )
    table = normalize_top10_float_holders(raw)
    assert table.height == 11
    assert table["holder_name"].n_unique() == 10
    with pytest.raises(ValueError, match="missing ownership cannot be treated as zero"):
        enrich_ownership_features(
            [_feature("000001.SZ")],
            store=_Fixture(table),
            as_of=date(2024, 5, 6),
            available_by=datetime(2024, 5, 6, 9, 30),
            config=OwnershipDataConfig(min_cross_section_coverage=1.0),
        )


def test_semantically_invalid_source_ratio_is_preserved_and_fails_closed() -> None:
    raw = pl.DataFrame(
        [
            {
                "ts_code": "600295.SH",
                "ann_date": "20230429",
                "end_date": "20230331",
                "holder_name": f"holder-{index}",
                "hold_amount": 1_000_000.0,
                "hold_ratio": 5.0,
                "hold_float_ratio": 118.8289 if index == 0 else 1.0,
                "hold_change": 0.0,
                "holder_type": "一般企业",
            }
            for index in range(10)
        ]
    )
    table = normalize_top10_float_holders(raw)
    assert table.filter(pl.col("hold_float_ratio") > 100).height == 1
    with pytest.raises(ValueError, match="missing ownership cannot be treated as zero"):
        enrich_ownership_features(
            [_feature("600295.SH")],
            store=_Fixture(table),
            as_of=date(2024, 5, 6),
            available_by=datetime(2024, 5, 6, 9, 30),
            config=OwnershipDataConfig(min_cross_section_coverage=1.0),
        )


def test_newer_incomplete_disclosure_does_not_fall_back_to_stale_group() -> None:
    older = _table().filter(pl.col("symbol") == "000001.SZ")
    newer = older.with_columns(
        pl.lit(date(2024, 5, 1)).cast(pl.Date).alias("ann_date"),
        pl.lit(datetime(2024, 5, 1, 15, 59))
        .cast(pl.Datetime("us"))
        .alias("available_at"),
        pl.when(pl.col("holder_name").str.ends_with("-9"))
        .then(pl.lit(""))
        .otherwise(pl.col("holder_type"))
        .alias("holder_type"),
    )
    with pytest.raises(ValueError, match="missing ownership cannot be treated as zero"):
        enrich_ownership_features(
            [_feature("000001.SZ")],
            store=_Fixture(pl.concat([older, newer], how="vertical_relaxed")),
            as_of=date(2024, 5, 6),
            available_by=datetime(2024, 5, 6, 9, 30),
            config=OwnershipDataConfig(min_cross_section_coverage=1.0),
        )


def test_ownership_snapshot_hash_rejects_tampering(tmp_path: Path) -> None:
    table = _table()
    snapshot = build_ownership_snapshot(
        table,
        source_name="test",
        base_market_snapshot_id="a" * 64,
        fundamental_snapshot_id="b" * 64,
    )
    dest = tmp_path / "ownership"
    write_ownership_snapshot_atomically(dest, table, snapshot)
    loaded, _ = load_verified_ownership_snapshot(dest)
    assert loaded.snapshot_id == snapshot.snapshot_id

    table.with_columns(
        (pl.col("hold_float_ratio") + 0.1).alias("hold_float_ratio")
    ).write_parquet(dest / "top10_float_holders.parquet")
    with pytest.raises(ValueError, match="source_row_hash|does not match parquet content"):
        load_verified_ownership_snapshot(dest)
