from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.demo.generator import DEMO_SEED, generate_demo_market, write_demo_parquet
from app.providers.tushare_event_history import materialize_tushare_event_overlay
from app.providers.tushare_events import (
    EXPRESS_NUMERIC,
    normalize_earnings_forecast,
    normalize_event_sources,
    normalize_holder_count,
    normalize_share_unlock,
)
from app.storage.event_io import build_event_snapshot, load_verified_event_snapshot
from app.storage.snapshot_io import load_verified_snapshot


def test_event_normalization_is_point_in_time_and_preserves_revisions() -> None:
    tables = normalize_event_sources(_raw_sources())
    forecast = tables["earnings_forecast_events"]

    assert forecast.height == 2
    assert forecast["ann_date"].to_list() == [date(2024, 1, 20), date(2024, 2, 5)]
    assert forecast["available_at"].to_list() == [
        datetime(2024, 1, 20, 15, 59),
        datetime(2024, 2, 5, 15, 59),
    ]
    assert forecast["source_row_hash"].n_unique() == 2


def test_event_snapshot_is_bound_to_market_and_rejects_tampering(tmp_path: Path) -> None:
    market_dir = _market(tmp_path / "market")
    source_dir = _source_dir(tmp_path / "source")
    output = tmp_path / "event-overlay"

    result = materialize_tushare_event_overlay(
        source_dir=source_dir,
        market_dir=market_dir,
        dest_dir=output,
    )
    market = load_verified_snapshot(market_dir)
    assert result.snapshot.base_market_snapshot_id == market.snapshot_id
    assert result.snapshot.row_counts == {
        "earnings_forecast_events": 2,
        "earnings_express_events": 1,
        "holder_count_events": 1,
        "share_unlock_events": 1,
        "audit_opinion_events": 1,
    }
    loaded, _ = load_verified_event_snapshot(
        output,
        expected_market_snapshot_id=market.snapshot_id,
    )
    assert loaded.snapshot_id == result.snapshot.snapshot_id

    path = output / "holder_count_events.parquet"
    tampered = pl.read_parquet(path).with_columns(pl.lit(1).alias("holder_num"))
    tampered.write_parquet(path)
    with pytest.raises(ValueError, match="source_row_hash mismatch"):
        load_verified_event_snapshot(output)


def test_event_snapshot_rejects_wrong_market_binding(tmp_path: Path) -> None:
    first_market = _market(tmp_path / "market-a", seed=DEMO_SEED)
    second_market = _market(tmp_path / "market-b", seed=DEMO_SEED + 1)
    output = tmp_path / "event-overlay"
    materialize_tushare_event_overlay(
        source_dir=_source_dir(tmp_path / "source"),
        market_dir=first_market,
        dest_dir=output,
    )
    second = load_verified_snapshot(second_market)

    with pytest.raises(ValueError, match="different market snapshot"):
        load_verified_event_snapshot(
            output,
            expected_market_snapshot_id=second.snapshot_id,
        )


def test_event_materializer_rejects_source_file_hash_mismatch(tmp_path: Path) -> None:
    market_dir = _market(tmp_path / "market")
    source_dir = _source_dir(tmp_path / "source")
    with (source_dir / "forecast.csv").open("a", encoding="utf-8") as handle:
        handle.write("\n")

    with pytest.raises(ValueError, match="source file hash mismatch for forecast"):
        materialize_tushare_event_overlay(
            source_dir=source_dir,
            market_dir=market_dir,
            dest_dir=tmp_path / "event-overlay",
        )


def test_forecast_rejects_first_announcement_after_revision() -> None:
    raw = _raw_sources()["forecast"].with_columns(
        pl.lit("20240206").alias("first_ann_date")
    )
    with pytest.raises(ValueError, match="first_ann_date exceeds ann_date"):
        normalize_earnings_forecast(raw)


def test_materializer_still_rejects_unsanitized_forecast_first_ann_contradiction(
    tmp_path: Path,
) -> None:
    market_dir = _market(tmp_path / "market")
    source_dir = _source_dir(tmp_path / "source")
    forecast_path = source_dir / "forecast.csv"
    raw = pl.read_csv(forecast_path).with_columns(pl.lit("20240206").alias("first_ann_date"))
    raw.write_csv(forecast_path)
    manifest_path = source_dir / "source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["forecast"]["sha256"] = _sha256(forecast_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    with pytest.raises(ValueError, match="first_ann_date exceeds ann_date"):
        materialize_tushare_event_overlay(
            source_dir=source_dir,
            market_dir=market_dir,
            dest_dir=tmp_path / "event-overlay",
        )


def test_holder_count_excludes_documented_blank_but_rejects_non_numeric_value() -> None:
    blank = pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "ann_date": ["20240301", "20240401"],
            "end_date": ["20240229", "20240331"],
            "holder_num": [12345, None],
        }
    )
    normalized = normalize_holder_count(blank)
    assert normalized.height == 1
    assert normalized["holder_num"].to_list() == [12345]

    invalid = blank.with_columns(
        pl.when(pl.col("ann_date") == "20240401")
        .then(pl.lit("not-a-number"))
        .otherwise(pl.col("holder_num").cast(pl.String))
        .alias("holder_num")
    )
    with pytest.raises(ValueError, match="holder_num is not numeric"):
        normalize_holder_count(invalid)


def test_share_unlock_normalization_handles_a_decimal_after_the_first_100_rows() -> None:
    raw = pl.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "ann_date": "20231211",
                "float_date": "20240630",
                "float_share": 1_000_000.0,
                "float_ratio": 1 if index < 101 else 1.0373,
                "holder_name": f"holder-{index:03d}",
                "share_type": "定向增发机构配售股份",
            }
            for index in range(102)
        ],
        infer_schema_length=None,
    )

    normalized = normalize_share_unlock(raw)

    assert normalized.height == 102
    assert normalized.filter(pl.col("holder_name") == "holder-101")[
        "float_ratio"
    ].item() == pytest.approx(1.0373)


def test_share_unlock_preserves_distinct_same_day_tranches_for_one_holder() -> None:
    raw = pl.DataFrame(
        [
            {
                "ts_code": "002167.SZ",
                "ann_date": "20230424",
                "float_date": "20230426",
                "float_share": 800_000.0,
                "float_ratio": 0.1032,
                "holder_name": "甘学贤",
                "share_type": "股权激励限售流通",
            },
            {
                "ts_code": "002167.SZ",
                "ann_date": "20230424",
                "float_date": "20230426",
                "float_share": 1_000_000.0,
                "float_ratio": 0.129,
                "holder_name": "甘学贤",
                "share_type": "股权激励限售流通",
            },
        ]
    )

    normalized = normalize_share_unlock(raw)

    assert normalized.height == 2
    assert normalized["float_share"].to_list() == [800_000.0, 1_000_000.0]


def test_event_hash_is_stable_across_row_order() -> None:
    tables = normalize_event_sources(_raw_sources())
    shuffled = {
        name: frame.reverse() if frame.height > 1 else frame
        for name, frame in tables.items()
    }
    source_hash = "a" * 64
    first = build_event_snapshot(
        tables,
        source_name="test",
        source_version="v1",
        base_market_snapshot_id="market-snapshot",
        source_manifest_sha256=source_hash,
    )
    second = build_event_snapshot(
        shuffled,
        source_name="test",
        source_version="v1",
        base_market_snapshot_id="market-snapshot",
        source_manifest_sha256=source_hash,
    )
    assert first.snapshot_id == second.snapshot_id
    assert first.table_hashes == second.table_hashes


def test_event_materialize_and_verify_cli(tmp_path: Path) -> None:
    market_dir = _market(tmp_path / "market")
    source_dir = _source_dir(tmp_path / "source")
    output = tmp_path / "event-overlay"
    runner = CliRunner()

    materialized = runner.invoke(
        cli_app,
        [
            "materialize-a-share-event-overlay",
            "--source-dir",
            str(source_dir),
            "--market-dir",
            str(market_dir),
            "--output-dir",
            str(output),
        ],
    )
    assert materialized.exit_code == 0, materialized.output
    assert "event_snapshot_id=" in materialized.output
    assert "earnings_forecast_events_rows=2" in materialized.output

    verified = runner.invoke(
        cli_app,
        [
            "verify-a-share-event-overlay",
            "--event-dir",
            str(output),
            "--market-dir",
            str(market_dir),
        ],
    )
    assert verified.exit_code == 0, verified.output
    assert "verified_event_snapshot_id=" in verified.output


def _market(path: Path, *, seed: int = DEMO_SEED) -> Path:
    write_demo_parquet(generate_demo_market(seed=seed), path)
    return path


def _source_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    files: dict[str, dict[str, str]] = {}
    filenames = {
        "forecast": "forecast.csv",
        "express": "express.csv",
        "stk_holdernumber": "stk_holdernumber.csv",
        "share_float": "share_float.csv",
        "fina_audit": "fina_audit.csv",
    }
    for source, frame in _raw_sources().items():
        target = path / filenames[source]
        frame.write_csv(target)
        files[source] = {"path": target.name, "sha256": _sha256(target)}
    evidence = {
        "forecast": "https://tushare.pro/document/2?doc_id=45 ann_date",
        "express": "https://tushare.pro/document/2?doc_id=46 ann_date",
        "stk_holdernumber": "https://tushare.pro/document/2?doc_id=166 ann_date",
        "share_float": "https://tushare.pro/document/2?doc_id=160 ann_date",
        "fina_audit": "https://tushare.pro/document/2?doc_id=80 ann_date",
    }
    manifest = {
        "schema_version": "1",
        "source_name": "tushare_offline_fixture",
        "source_version": "fixture-v1",
        "fetched_at": "2025-01-02T00:00:00Z",
        "coverage_start": "2024-01-01",
        "coverage_end": "2024-12-31",
        "files": files,
        "availability_evidence": evidence,
        "notes": "offline test fixture",
    }
    (path / "source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _raw_sources() -> dict[str, pl.DataFrame]:
    forecast = pl.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "ann_date": "20240120",
                "end_date": "20231231",
                "type": "略增",
                "p_change_min": 5.0,
                "p_change_max": 10.0,
                "net_profit_min": 100.0,
                "net_profit_max": 110.0,
                "last_parent_net": 95.0,
                "first_ann_date": "20240120",
                "summary": "initial",
                "change_reason": "operations",
            },
            {
                "ts_code": "000001.SZ",
                "ann_date": "20240205",
                "end_date": "20231231",
                "type": "预增",
                "p_change_min": 8.0,
                "p_change_max": 12.0,
                "net_profit_min": 108.0,
                "net_profit_max": 112.0,
                "last_parent_net": 95.0,
                "first_ann_date": "20240120",
                "summary": "revision",
                "change_reason": "operations revised",
            },
        ]
    )
    express_row: dict[str, object] = {
        "ts_code": "000001.SZ",
        "ann_date": "20240220",
        "end_date": "20231231",
        "summary": "express report",
    }
    express_row.update({name: float(index + 1) for index, name in enumerate(EXPRESS_NUMERIC)})
    return {
        "forecast": forecast,
        "express": pl.DataFrame([express_row]),
        "stk_holdernumber": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240301",
                    "end_date": "20240229",
                    "holder_num": 12345,
                }
            ]
        ),
        "share_float": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240315",
                    "float_date": "20240630",
                    "float_share": 1000000.0,
                    "float_ratio": 1.5,
                    "holder_name": "holder-a",
                    "share_type": "定向增发机构配售股份",
                }
            ]
        ),
        "fina_audit": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240430",
                    "end_date": "20231231",
                    "audit_result": "标准无保留意见",
                    "audit_fees": 100.0,
                    "audit_agency": "agency-a",
                    "audit_sign": "auditor-a",
                }
            ]
        ),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
