from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.demo.generator import generate_demo_market, write_demo_parquet
from app.errors import TushareFetchError
from app.providers.tushare_fundamental_history import (
    collect_tushare_all_a_share_fundamentals,
    materialize_tushare_all_a_share_fundamentals,
)
from app.storage.fundamental_io import load_verified_fundamental_snapshot
from app.strategies.loader import load_strategy_config
from tests.helpers import CONFIG_DIR
from tests.tushare_fakes import FakeTushareClient


def _config():
    return load_strategy_config("all_a_share_historical_value_quality_v1", CONFIG_DIR)


def _market(tmp_path: Path) -> tuple[Path, list[date]]:
    bundle = generate_demo_market(
        n_stocks=2,
        start=date(2022, 1, 3),
        end=date(2022, 7, 29),
    ).model_copy(update={"adjustment": "backward"})
    path = tmp_path / "market"
    write_demo_parquet(bundle, path)
    return path, bundle.calendar


def _tables(days: list[date]) -> dict[str, pl.DataFrame]:
    reports = pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ", "000002.SZ"],
            "ann_date": ["20220429", "20220429", "20220429"],
            "end_date": ["20220331", "20220331", "20220331"],
            "update_flag": ["0", "1", "0"],
            "roe": [10.0, 999.0, 8.0],
            "roic": [9.0, 999.0, 7.0],
            "grossprofit_margin": [30.0, 999.0, 20.0],
            "debt_to_assets": [40.0, 1.0, 50.0],
            "ocf_to_or": [12.0, 999.0, 10.0],
            "q_sales_yoy": [5.0, 999.0, 4.0],
            "q_netprofit_yoy": [6.0, 999.0, 3.0],
            "dt_netprofit_yoy": [5.0, 999.0, 2.0],
        }
    )
    valuation = pl.DataFrame(
        [
            {
                "ts_code": symbol,
                "trade_date": day.strftime("%Y%m%d"),
                "turnover_rate": 2.0,
                "pe_ttm": 10.0,
                "pb": 1.0,
                "ps_ttm": 2.0,
                "total_mv": 1000.0,
                "circ_mv": 800.0,
            }
            for day in days
            for symbol in ("000001.SZ", "000002.SZ")
        ]
    )
    return {"fina_indicator": reports, "daily_basic": valuation}


def test_full_market_fundamentals_resume_materialize_and_bind(tmp_path: Path) -> None:
    market, calendar = _market(tmp_path)
    days = calendar[80:90]
    client = FakeTushareClient(_tables(days))
    staging = tmp_path / "staging"
    first = collect_tushare_all_a_share_fundamentals(
        client=client,
        market_dir=market,
        config=_config(),
        start=days[0],
        end=days[-1],
        staging_dir=staging,
    )
    calls = len(client.calls)
    second = collect_tushare_all_a_share_fundamentals(
        client=client,
        market_dir=market,
        config=_config(),
        start=days[0],
        end=days[-1],
        staging_dir=staging,
    )
    assert first.completed_partitions == 12
    assert second.completed_partitions == 0
    assert second.reused_partitions == 12
    assert len(client.calls) == calls

    result = materialize_tushare_all_a_share_fundamentals(
        staging_dir=staging,
        market_dir=market,
        config=_config(),
        dest_dir=tmp_path / "overlay",
    )
    stored, tables = load_verified_fundamental_snapshot(tmp_path / "overlay")
    assert stored.snapshot_id == result.snapshot.snapshot_id
    assert stored.base_market_snapshot_id == first.base_market_snapshot_id
    assert stored.collection_request_id == first.request_id
    assert stored.requested_symbols == 2
    assert stored.covered_report_symbols == 2
    assert stored.covered_valuation_symbols == 2
    assert tables["daily_valuation"].height == 20


def test_materializer_rejects_tampered_fundamental_partition(tmp_path: Path) -> None:
    market, calendar = _market(tmp_path)
    days = calendar[80:82]
    staging = tmp_path / "staging"
    collect_tushare_all_a_share_fundamentals(
        client=FakeTushareClient(_tables(days)),
        market_dir=market,
        config=_config(),
        start=days[0],
        end=days[-1],
        staging_dir=staging,
    )
    partition = next((staging / "partitions" / "daily_valuation").glob("*.parquet"))
    frame = pl.read_parquet(partition).with_columns((pl.col("pb") + 1.0).alias("pb"))
    frame.write_parquet(partition)
    with pytest.raises(TushareFetchError, match="manifest hashes"):
        materialize_tushare_all_a_share_fundamentals(
            staging_dir=staging,
            market_dir=market,
            config=_config(),
            dest_dir=tmp_path / "overlay",
        )


def test_collection_rejects_market_snapshot_or_request_drift(tmp_path: Path) -> None:
    market, calendar = _market(tmp_path)
    days = calendar[80:82]
    staging = tmp_path / "staging"
    collect_tushare_all_a_share_fundamentals(
        client=FakeTushareClient(_tables(days)),
        market_dir=market,
        config=_config(),
        start=days[0],
        end=days[-1],
        staging_dir=staging,
    )
    with pytest.raises(TushareFetchError, match="different fundamental request"):
        collect_tushare_all_a_share_fundamentals(
            client=FakeTushareClient(_tables(days)),
            market_dir=market,
            config=_config(),
            start=days[0],
            end=days[-1] + timedelta(days=1),
            staging_dir=staging,
        )
