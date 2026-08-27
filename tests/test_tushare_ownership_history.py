from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.demo.generator import generate_demo_market, write_demo_parquet
from app.errors import TushareFetchError
from app.providers.tushare_fundamentals import (
    normalize_daily_valuation,
    normalize_fundamental_reports,
)
from app.providers.tushare_ownership_history import (
    _query_with_retry,
    collect_tushare_all_a_share_ownership,
    materialize_tushare_all_a_share_ownership,
)
from app.storage.fundamental_io import (
    build_fundamental_snapshot,
    write_fundamental_snapshot_atomically,
)
from app.storage.ownership_io import load_verified_ownership_snapshot
from app.strategies.loader import load_strategy_config
from tests.helpers import CONFIG_DIR
from tests.tushare_fakes import FakeTushareClient


def _config():
    return load_strategy_config("all_a_share_balanced_multifactor_v1", CONFIG_DIR)


def _market_and_fundamental(tmp_path: Path) -> tuple[Path, Path, list[date]]:
    bundle = generate_demo_market(
        n_stocks=2,
        start=date(2022, 1, 3),
        end=date(2022, 7, 29),
    ).model_copy(update={"adjustment": "backward"})
    market = tmp_path / "market"
    market_snapshot = write_demo_parquet(bundle, market)
    reports = normalize_fundamental_reports(
        pl.DataFrame(
            {
                "ts_code": ["000001.SZ", "000002.SZ"],
                "ann_date": ["20220429", "20220429"],
                "end_date": ["20220331", "20220331"],
                "update_flag": ["0", "0"],
                "roe": [10.0, 8.0],
                "roic": [9.0, 7.0],
                "grossprofit_margin": [30.0, 20.0],
                "debt_to_assets": [40.0, 50.0],
                "ocf_to_or": [12.0, 10.0],
                "q_sales_yoy": [5.0, 4.0],
                "q_netprofit_yoy": [6.0, 3.0],
                "dt_netprofit_yoy": [5.0, 2.0],
            }
        )
    )
    valuation = normalize_daily_valuation(
        pl.DataFrame(
            {
                "ts_code": ["000001.SZ", "000002.SZ"],
                "trade_date": ["20220506", "20220506"],
                "turnover_rate": [2.0, 2.0],
                "pe_ttm": [10.0, 12.0],
                "pb": [1.0, 1.2],
                "ps_ttm": [2.0, 2.2],
                "total_mv": [1000.0, 900.0],
                "circ_mv": [800.0, 700.0],
            }
        )
    )
    tables = {"fundamental_reports": reports, "daily_valuation": valuation}
    snapshot = build_fundamental_snapshot(
        tables,
        source_name="test",
        base_market_snapshot_id=market_snapshot.snapshot_id,
    )
    fundamental = tmp_path / "fundamental"
    write_fundamental_snapshot_atomically(fundamental, tables, snapshot)
    return market, fundamental, bundle.calendar


def _holders() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "ts_code": symbol,
                "ann_date": "20220430",
                "end_date": "20220331",
                "holder_name": f"{symbol}-{index}",
                "hold_float_ratio": 5.0,
                "holder_type": "基金" if index < 5 else "个人",
            }
            for symbol in ("000001.SZ", "000002.SZ")
            for index in range(10)
        ]
    )


def test_ownership_query_retries_provider_failure_without_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeTushareClient({"top10_floatholders": _holders()})
    calls = 0

    def flaky_query(api_name: str, **params: object) -> pl.DataFrame:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TushareFetchError("temporary timeout")
        return client.query(api_name, **params)

    monkeypatch.setattr(
        "app.providers.tushare_ownership_history.sleep", lambda _seconds: None
    )
    frame = _query_with_retry(
        type("FlakyClient", (), {"query": staticmethod(flaky_query)})(),
        "top10_floatholders",
        {"ts_code": "000001.SZ"},
    )
    assert calls == 2
    assert frame.height == 10


def test_ownership_history_resume_materialize_and_bind(tmp_path: Path) -> None:
    market, fundamental, calendar = _market_and_fundamental(tmp_path)
    client = FakeTushareClient({"top10_floatholders": _holders()})
    staging = tmp_path / "staging"
    first = collect_tushare_all_a_share_ownership(
        client=client,
        market_dir=market,
        fundamental_dir=fundamental,
        config=_config(),
        start=calendar[80],
        end=calendar[100],
        staging_dir=staging,
    )
    call_count = len(client.calls)
    second = collect_tushare_all_a_share_ownership(
        client=client,
        market_dir=market,
        fundamental_dir=fundamental,
        config=_config(),
        start=calendar[80],
        end=calendar[100],
        staging_dir=staging,
    )
    assert first.completed_partitions == 2
    assert second.reused_partitions == 2
    assert len(client.calls) == call_count

    result = materialize_tushare_all_a_share_ownership(
        staging_dir=staging,
        market_dir=market,
        fundamental_dir=fundamental,
        config=_config(),
        dest_dir=tmp_path / "ownership",
    )
    stored, table = load_verified_ownership_snapshot(tmp_path / "ownership")
    assert stored.snapshot_id == result.snapshot.snapshot_id
    assert stored.base_market_snapshot_id == first.base_market_snapshot_id
    assert stored.fundamental_snapshot_id == first.fundamental_snapshot_id
    assert table.height == 20
    assert result.complete_groups == 2


def test_materializer_rejects_tampered_ownership_partition(tmp_path: Path) -> None:
    market, fundamental, calendar = _market_and_fundamental(tmp_path)
    staging = tmp_path / "staging"
    collect_tushare_all_a_share_ownership(
        client=FakeTushareClient({"top10_floatholders": _holders()}),
        market_dir=market,
        fundamental_dir=fundamental,
        config=_config(),
        start=calendar[80],
        end=calendar[100],
        staging_dir=staging,
    )
    path = next((staging / "partitions").glob("*.parquet"))
    pl.read_parquet(path).with_columns(
        (pl.col("hold_float_ratio") + 0.1).alias("hold_float_ratio")
    ).write_parquet(path)
    with pytest.raises(TushareFetchError, match="manifest hash"):
        materialize_tushare_all_a_share_ownership(
            staging_dir=staging,
            market_dir=market,
            fundamental_dir=fundamental,
            config=_config(),
            dest_dir=tmp_path / "ownership",
        )
