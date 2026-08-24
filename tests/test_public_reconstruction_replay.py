from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine
from app.models.config import StrategyConfig
from app.models.market import Instrument
from app.pipeline import load_research_store
from app.preflight import PUBLIC_RECONSTRUCTION_MODE_LABEL, preflight_research
from app.providers._frames import instruments_to_frame
from app.scoring.engine import ScoringEngine
from app.settings import Settings
from app.storage.memory import InMemoryStore
from app.universe.membership import build_manual_static_membership
from app.universe.public_replay import PublicReconstructionStore, load_public_reconstruction_pack
from tests.helpers import CONFIG_DIR, fill_quiet_bars, load_test_config, weekdays


def _write_pack(root: Path, calendar: list[date], symbols: list[str]) -> Path:
    root.mkdir(parents=True)
    source = root / "source_documents"
    source.mkdir()
    stamp = "2026-08-24T02:26:00.371938Z"
    raw_rows = []
    candidate_rows = []
    for day in calendar:
        for symbol in symbols:
            weight = 1.0 / len(symbols)
            raw_rows.append(
                {
                    "date": day,
                    "instrument": "000300.SH",
                    "name": "沪深300",
                    "member_code": symbol,
                    "member_name": symbol,
                    "weight": weight,
                }
            )
            candidate_rows.append(
                {
                    "source_date": day,
                    "index_code": "000300.SH",
                    "symbol": symbol,
                    "weight": weight,
                    "source_member_name": symbol,
                    "retrieved_at": stamp,
                }
            )
    raw_path = source / "bigquant_cn_stock_index_weight.csv"
    pl.DataFrame(raw_rows).write_csv(raw_path)
    pl.DataFrame(candidate_rows).write_csv(root / "candidate_membership.csv")
    quality = {
        "schema": "aiq.public_reconstruction_quality.v1",
        "expected_constituents": len(symbols),
        "source_dates": len(calendar),
        "complete_dates": len(calendar),
        "incomplete_dates": 0,
        "row_validation_errors": [],
        "eligible_for_public_reconstruction": True,
    }
    (root / "quality_report.json").write_text(json.dumps(quality), encoding="utf-8")
    manifest = {
        "schema": "aiq.public_reconstruction_collection.v1",
        "classification": "public_reconstructed_not_licensed_pit",
        "source_name": "BigQuant cn_stock_index_weight",
        "query_index_code": "000300.SH",
        "retrieved_at": stamp,
        "requested_coverage": {"start": calendar[0].isoformat(), "end": calendar[-1].isoformat()},
        "raw_response": {
            "path": "source_documents/bigquant_cn_stock_index_weight.csv",
            "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        },
    }
    (root / "collection_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _base_store(calendar: list[date], symbols: list[str]) -> InMemoryStore:
    daily = pl.DataFrame(
        [row for symbol in symbols for row in fill_quiet_bars(symbol, calendar)]
        + fill_quiet_bars("IDX_CSI300", calendar)
    ).with_columns(
        [
            pl.col("date").cast(pl.Date),
            pl.col("is_st").cast(pl.Boolean),
            pl.col("is_suspended").cast(pl.Boolean),
            pl.col("price_limit_pct").cast(pl.Float64),
        ]
    )
    globals_ = pl.DataFrame(
        {
            "symbol": ["GLB_SPX"] * len(calendar),
            "date": calendar,
            "close": [100.0] * len(calendar),
            "ret_1d": [0.0] * len(calendar),
            "market": ["US"] * len(calendar),
            "timezone": ["America/New_York"] * len(calendar),
            "available_at": [datetime(day.year, day.month, day.day, 6, 0) for day in calendar],
        }
    ).with_columns([pl.col("date").cast(pl.Date), pl.col("available_at").cast(pl.Datetime("us"))])
    instruments = instruments_to_frame(
        [Instrument(symbol=symbol, name=symbol, sector="test", listing_date=date(2018, 1, 1)) for symbol in symbols]
    )
    return InMemoryStore(
        instruments=instruments,
        daily=daily.filter(pl.col("symbol") != "IDX_CSI300"),
        index=daily.filter(pl.col("symbol") == "IDX_CSI300"),
        global_bars=globals_,
        calendar=calendar,
        universe_membership=build_manual_static_membership(symbols, calendar, universe_id="download_base"),
        universe_id="download_base",
    )


def _public_config(expected: int) -> StrategyConfig:
    config = load_test_config()
    config.research_scope = "public_reconstruction"
    config.universe.mode = "public_reconstruction"
    config.universe.id = "csi300_bigquant_public_reconstruction"
    config.universe.expected_constituents = expected
    config.universe.min_avg_turnover_20d = 0
    config.universe.min_listing_days = 1
    config.universe.exclude_st = False
    return config


def test_public_pack_rejects_candidate_edits_after_raw_hash_verification(tmp_path: Path) -> None:
    calendar = weekdays(date(2024, 1, 2), 2)
    pack_dir = _write_pack(tmp_path / "pack", calendar, ["000001.SZ", "600000.SH"])
    pack = load_public_reconstruction_pack(pack_dir, expected_constituents=2)
    assert pack.coverage_start == calendar[0]
    candidate = pl.read_csv(pack_dir / "candidate_membership.csv").with_columns(pl.lit(0.99).alias("weight"))
    candidate.write_csv(pack_dir / "candidate_membership.csv")
    with pytest.raises(Exception, match="candidate does not match"):
        load_public_reconstruction_pack(pack_dir, expected_constituents=2)


def test_public_overlay_is_required_and_labels_scores_and_simulations(tmp_path: Path) -> None:
    calendar = weekdays(date(2024, 1, 2), 66)
    symbols = ["000001.SZ", "600000.SH"]
    pack = load_public_reconstruction_pack(_write_pack(tmp_path / "pack", calendar, symbols), expected_constituents=2)
    base = _base_store(calendar, symbols)
    config = _public_config(expected=2)
    with pytest.raises(ValueError, match="verified public reconstruction overlay"):
        ScoringEngine(base, config).run(calendar[59])

    store = PublicReconstructionStore(base, pack, universe_id=config.universe.id)
    preflight = preflight_research(store=store, config=config, start=calendar[59], end=calendar[-1])
    assert preflight.research_mode == PUBLIC_RECONSTRUCTION_MODE_LABEL
    assert preflight.research_notice is not None
    scores = ScoringEngine(store, config).run(calendar[59])
    assert scores
    assert {score.research_scope for score in scores} == {"public_reconstruction"}
    assert {score.reconstruction_data_id for score in scores} == {pack.collection_id}
    result = BacktestEngine(store, config).run(calendar[59], calendar[-1])
    assert result.research_scope == "public_reconstruction"
    assert result.research_notice is not None
    assert result.reconstruction_data_id == pack.collection_id


def test_pipeline_loads_actual_public_strategy_only_with_verified_overlay(tmp_path: Path) -> None:
    symbols = [f"{number:06d}.SZ" for number in range(1, 301)]
    calendar = [date(2024, 1, 2)]
    pack_dir = _write_pack(tmp_path / "pack", calendar, symbols)
    base = InMemoryStore(
        instruments=instruments_to_frame(
            [Instrument(symbol=symbol, name=symbol, sector="test", listing_date=date(2018, 1, 1)) for symbol in symbols]
        ),
        calendar=calendar,
        universe_membership=build_manual_static_membership(symbols, calendar, universe_id="download_base"),
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=CONFIG_DIR.parent,
        public_reconstruction_dir=pack_dir,
    )
    store = load_research_store(settings, "csi300_bigquant_public_reconstruction_v1", base_store=base)
    assert isinstance(store, PublicReconstructionStore)
    assert store.public_reconstruction_id
