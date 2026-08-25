from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.models.events import EventSourceManifest
from app.providers.tushare_event_history import materialize_tushare_event_overlay
from app.research.event_overlay_review import (
    build_event_overlay_review,
    load_verified_collection_quality_provenance,
    load_verified_event_overlay_review,
    write_event_overlay_review_atomically,
)
from app.storage.event_io import load_verified_event_snapshot
from app.storage.snapshot_io import load_verified_snapshot
from tests.helpers import load_test_config
from tests.test_event_overlay import _market, _raw_sources, _sha256

RAW_HOLDER_BLANK_ROWS = 3
RAW_HOLDER_ROWS = 4
RAW_UNLOCK_ROWS = 2
RAW_FLOAT_RATIO_BLANK_ROWS = 1


def _write_collection_quality_artifacts(
    path: Path,
    *,
    holder_blank_rows: int = RAW_HOLDER_BLANK_ROWS,
    holder_raw_rows: int = RAW_HOLDER_ROWS,
    unlock_raw_rows: int = RAW_UNLOCK_ROWS,
    float_ratio_blank_rows: int = RAW_FLOAT_RATIO_BLANK_ROWS,
) -> None:
    canonical_holder_rows = holder_raw_rows - holder_blank_rows
    quality: dict[str, Any] = {
        "schema_version": "1",
        "complete": True,
        "request_id": "fixture-request",
        "base_market_snapshot_id": "fixture-market",
        "coverage": {"start": "2024-01-01", "end": "2024-12-31"},
        "requested_stocks": 2,
        "expected_partitions": 10,
        "sources": {
            "forecast": {
                "raw_rows": 2,
                "normalized_rows": 2,
                "field_missing_counts": {},
            },
            "express": {
                "raw_rows": 1,
                "normalized_rows": 1,
                "field_missing_counts": {},
            },
            "stk_holdernumber": {
                "raw_rows": holder_raw_rows,
                "normalized_rows": canonical_holder_rows,
                "unusable_rows_excluded_from_canonical_overlay": holder_blank_rows,
                "field_missing_counts": {
                    "ts_code": 0,
                    "ann_date": 0,
                    "end_date": 0,
                    "holder_num": holder_blank_rows,
                },
            },
            "share_float": {
                "raw_rows": unlock_raw_rows,
                "normalized_rows": unlock_raw_rows,
                "unusable_rows_excluded_from_canonical_overlay": 0,
                "field_missing_counts": {
                    "ts_code": 0,
                    "ann_date": 0,
                    "float_date": 0,
                    "float_share": 0,
                    "float_ratio": float_ratio_blank_rows,
                    "holder_name": 0,
                    "share_type": 0,
                },
            },
            "fina_audit": {
                "raw_rows": 2,
                "normalized_rows": 2,
                "field_missing_counts": {},
            },
        },
        "share_unlock": {
            "float_share_unit": "shares",
            "float_ratio_unit": "percent",
            "float_ratio_non_null_rows": unlock_raw_rows - float_ratio_blank_rows,
            "float_ratio_null_rows": float_ratio_blank_rows,
        },
        "research_boundary": {
            "ready_for_materialization": True,
            "ready_for_scoring": False,
            "ready_for_trading": False,
            "statement": "fixture quality only",
        },
    }
    quality_path = path / "quality_report.json"
    quality_path.write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    collection_manifest = {
        "schema_version": "1",
        "request_id": "fixture-request",
        "source_name": "tushare_a_share_events",
        "source_version": "fixture-v1",
        "source_manifest_sha256": _sha256(path / "source_manifest.json"),
        "quality_report_sha256": _sha256(quality_path),
        "research_boundary": "collection fixture only",
    }
    (path / "collection_manifest.json").write_text(
        json.dumps(collection_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _source_dir_with_review_edges(path: Path) -> Path:
    path.mkdir(parents=True)
    raw = _raw_sources()
    holder = raw["stk_holdernumber"].vstack(
        pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240308",
                    "end_date": "20240307",
                    "holder_num": None,
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240309",
                    "end_date": "20240308",
                    "holder_num": None,
                },
                {
                    "ts_code": "000002.SZ",
                    "ann_date": "20240310",
                    "end_date": "20240309",
                    "holder_num": None,
                },
            ]
        )
    )
    unlock = raw["share_float"].vstack(
        pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20240318",
                    "float_date": "20240715",
                    "float_share": 500000.0,
                    "float_ratio": None,
                    "holder_name": "holder-b",
                    "share_type": "定向增发机构配售股份",
                }
            ]
        )
    )
    audit = raw["fina_audit"].vstack(
        pl.DataFrame(
            [
                {
                    "ts_code": "000002.SZ",
                    "ann_date": "20240430",
                    "end_date": "20231231",
                    "audit_result": "带强调事项段的无保留意见",
                    "audit_fees": 80.0,
                    "audit_agency": "agency-b",
                    "audit_sign": "auditor-b",
                }
            ]
        )
    )
    sources = {
        **raw,
        "stk_holdernumber": holder,
        "share_float": unlock,
        "fina_audit": audit,
    }
    filenames = {
        "forecast": "forecast.csv",
        "express": "express.csv",
        "stk_holdernumber": "stk_holdernumber.csv",
        "share_float": "share_float.csv",
        "fina_audit": "fina_audit.csv",
    }
    files: dict[str, dict[str, str]] = {}
    for source, frame in sources.items():
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
        "notes": "offline review fixture",
    }
    (path / "source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_collection_quality_artifacts(path)
    return path


def _event_bundle(tmp_path: Path) -> tuple[Path, Path, Path]:
    market = _market(tmp_path / "market")
    source = _source_dir_with_review_edges(tmp_path / "source")
    event = tmp_path / "event-overlay"
    materialize_tushare_event_overlay(
        source_dir=source,
        market_dir=market,
        dest_dir=event,
    )
    return market, event, source


def _build(tmp_path: Path, *, start: date, end: date):
    market, event, source = _event_bundle(tmp_path)
    snapshot, tables = load_verified_event_snapshot(event)
    source_manifest = EventSourceManifest.model_validate_json(
        (event / "source_manifest.json").read_bytes()
    )
    report, annual = build_event_overlay_review(
        market_dir=market,
        event_snapshot=snapshot,
        event_source_manifest=source_manifest,
        event_tables=tables,
        config=load_test_config(),
        window_start=start,
        window_end=end,
        source_collection_dir=source,
    )
    return market, event, source, report, annual


def test_review_excludes_same_day_announcement_at_decision_close(tmp_path: Path) -> None:
    _, _, _, report, _ = _build(
        tmp_path,
        start=date(2024, 2, 1),
        end=date(2024, 2, 29),
    )
    by_date = {probe.as_of_date: probe for probe in report.pit_availability_probes}

    assert date(2024, 2, 5) in by_date
    same_day = by_date[date(2024, 2, 5)]
    assert same_day.same_day_announcement_rows_not_yet_visible["forecast"] == 1
    assert same_day.visible_event_rows["forecast"] == 1

    next_day = by_date[date(2024, 2, 6)]
    assert next_day.same_day_announcement_rows_not_yet_visible["forecast"] == 0
    assert next_day.visible_event_rows["forecast"] == 2


def test_review_reports_raw_holder_blanks_separately_from_canonical(
    tmp_path: Path,
) -> None:
    _, event, _, report, annual = _build(
        tmp_path,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    forecast = next(
        item
        for item in report.annual_by_source
        if item.source_name == "forecast" and item.year == 2024
    )
    assert forecast.announcement_row_count == 2
    assert forecast.groups_with_multiple_announcement_dates == 1
    assert forecast.max_announcement_versions == 2
    assert report.forecast_type_transition_counts == {"略增 -> 预增": 1}

    assert report.audit_result_distribution == {
        "带强调事项段的无保留意见": 1,
        "标准无保留意见": 1,
    }
    assert report.audit_results_requiring_manual_classification == [
        "带强调事项段的无保留意见"
    ]

    holder = report.holder_count_missingness
    assert holder.raw_collection_holder_rows == RAW_HOLDER_ROWS
    assert holder.raw_collection_holder_num_blank_rows == RAW_HOLDER_BLANK_ROWS
    assert holder.raw_collection_holder_num_blank_rows != 0
    assert holder.canonical_holder_rows_in_window == 1
    assert holder.symbols_with_canonical_holder_observation == 1
    assert holder.symbols_with_no_observable_canonical_holder_data >= 1
    assert "verified collector" in holder.semantics
    assert "never be reported as 0" in holder.semantics
    assert "holder_num_null_rows" not in type(holder).model_fields

    unlock = report.unlock_ratio_coverage
    assert unlock.raw_collection_unlock_rows == RAW_UNLOCK_ROWS
    assert unlock.raw_collection_float_ratio_blank_rows == RAW_FLOAT_RATIO_BLANK_ROWS
    assert unlock.canonical_unlock_rows_in_window == 2
    assert unlock.canonical_float_ratio_known_rows == 1
    assert unlock.canonical_float_ratio_missing_rows == 1
    assert unlock.canonical_float_ratio_known_ratio == pytest.approx(0.5)
    assert "not a zero unlock risk" in unlock.semantics
    assert "float_ratio_missing_rows" not in type(unlock).model_fields

    snapshot, tables = load_verified_event_snapshot(event)
    assert tables["holder_count_events"].height == 1
    assert int(tables["holder_count_events"]["holder_num"].null_count()) == 0
    assert report.collection_source_manifest_sha256 == snapshot.source_manifest_sha256
    assert report.collection_quality_report_sha256

    annual_forecast = annual.filter(pl.col("source_name") == "forecast").row(0, named=True)
    assert annual_forecast["announcement_row_count"] == 2
    assert annual_forecast["groups_with_multiple_announcement_dates"] == 1


def test_review_binds_snapshots_and_rejects_wrong_market(tmp_path: Path) -> None:
    market, event, source = _event_bundle(tmp_path / "bound")
    snapshot, tables = load_verified_event_snapshot(event)
    source_manifest = EventSourceManifest.model_validate_json(
        (event / "source_manifest.json").read_bytes()
    )
    report, _ = build_event_overlay_review(
        market_dir=market,
        event_snapshot=snapshot,
        event_source_manifest=source_manifest,
        event_tables=tables,
        config=load_test_config(),
        window_start=date(2024, 1, 1),
        window_end=date(2024, 12, 31),
        source_collection_dir=source,
    )
    market_snapshot = load_verified_snapshot(market)
    assert report.market_snapshot_id == market_snapshot.snapshot_id
    assert report.event_snapshot_id == snapshot.snapshot_id
    assert report.source_manifest_sha256 == snapshot.source_manifest_sha256
    assert report.collection_source_manifest_sha256 == snapshot.source_manifest_sha256
    assert report.strategy_config_hash == load_test_config().config_hash()
    assert report.window_start == date(2024, 1, 1)
    assert report.window_end == date(2024, 12, 31)

    other_market = _market(tmp_path / "other-market", seed=99)
    with pytest.raises(ValueError, match="different market snapshot"):
        build_event_overlay_review(
            market_dir=other_market,
            event_snapshot=snapshot,
            event_source_manifest=source_manifest,
            event_tables=tables,
            config=load_test_config(),
            window_start=date(2024, 1, 1),
            window_end=date(2024, 12, 31),
            source_collection_dir=source,
        )


def test_review_rejects_collection_quality_tamper_and_binding_mismatch(
    tmp_path: Path,
) -> None:
    market, event, source = _event_bundle(tmp_path / "tamper")
    snapshot, tables = load_verified_event_snapshot(event)
    source_manifest = EventSourceManifest.model_validate_json(
        (event / "source_manifest.json").read_bytes()
    )

    quality_path = source / "quality_report.json"
    tampered = json.loads(quality_path.read_text(encoding="utf-8"))
    tampered["sources"]["stk_holdernumber"]["field_missing_counts"]["holder_num"] = 0
    quality_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="quality_report_sha256"):
        build_event_overlay_review(
            market_dir=market,
            event_snapshot=snapshot,
            event_source_manifest=source_manifest,
            event_tables=tables,
            config=load_test_config(),
            window_start=date(2024, 1, 1),
            window_end=date(2024, 12, 31),
            source_collection_dir=source,
        )

    _write_collection_quality_artifacts(source)
    other = tmp_path / "other-collection"
    other.mkdir()
    (other / "source_manifest.json").write_text(
        json.dumps({"schema_version": "1", "note": "unrelated"}),
        encoding="utf-8",
    )
    _write_collection_quality_artifacts(
        other,
        holder_blank_rows=41980,
        holder_raw_rows=145702,
    )
    with pytest.raises(ValueError, match="does not match the event snapshot"):
        load_verified_collection_quality_provenance(
            other,
            expected_source_manifest_sha256=snapshot.source_manifest_sha256,
        )


def test_review_fails_closed_when_raw_holder_blank_counter_missing(
    tmp_path: Path,
) -> None:
    market, event, source = _event_bundle(tmp_path / "missing-counter")
    snapshot, tables = load_verified_event_snapshot(event)
    source_manifest = EventSourceManifest.model_validate_json(
        (event / "source_manifest.json").read_bytes()
    )
    quality_path = source / "quality_report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    del quality["sources"]["stk_holdernumber"]["field_missing_counts"]["holder_num"]
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    manifest = json.loads((source / "collection_manifest.json").read_text(encoding="utf-8"))
    manifest["quality_report_sha256"] = _sha256(quality_path)
    (source / "collection_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="refusing to treat raw blank holder rows as zero"):
        build_event_overlay_review(
            market_dir=market,
            event_snapshot=snapshot,
            event_source_manifest=source_manifest,
            event_tables=tables,
            config=load_test_config(),
            window_start=date(2024, 1, 1),
            window_end=date(2024, 12, 31),
            source_collection_dir=source,
        )


def test_review_artifact_is_deterministic_and_rejects_tampering(tmp_path: Path) -> None:
    _, _, _, report, annual = _build(
        tmp_path / "first",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    _, _, _, again, annual_again = _build(
        tmp_path / "second",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert report.model_dump(exclude={"report_id", "annual_review_file_sha256"}) == again.model_dump(
        exclude={"report_id", "annual_review_file_sha256"}
    )
    assert annual.equals(annual_again, null_equal=True)
    assert report.collection_quality_report_sha256 == again.collection_quality_report_sha256

    output = tmp_path / "review"
    sealed = write_event_overlay_review_atomically(output, report, annual)
    loaded, stored = load_verified_event_overlay_review(output)
    assert loaded == sealed
    assert stored.equals(annual, null_equal=True)

    report_path = output / "report.json"
    tampered = json.loads(report_path.read_text(encoding="utf-8"))
    tampered["window_end"] = "2024-06-30"
    report_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="report ID"):
        load_verified_event_overlay_review(output)
    report_path.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")

    path = output / "annual_source_review.parquet"
    pl.read_parquet(path).with_columns(
        (pl.col("announcement_row_count") + 1).alias("announcement_row_count")
    ).write_parquet(path)
    with pytest.raises(ValueError, match="hash does not match"):
        load_verified_event_overlay_review(output)


def test_review_cli_remains_read_only(tmp_path: Path) -> None:
    market, event, source = _event_bundle(tmp_path)
    output = tmp_path / "review"
    result = CliRunner().invoke(
        cli_app,
        [
            "review-a-share-event-overlay",
            "--strategy",
            "baseline_v1",
            "--start",
            "2024-01-01",
            "--end",
            "2024-12-31",
            "--market-dir",
            str(market),
            "--event-dir",
            str(event),
            "--source-collection-dir",
            str(source),
            "--output-dir",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "ready_for_scoring=false" in result.output
    assert "ready_for_trading=false" in result.output
    assert "source_manifest_sha256=" in result.output
    assert "collection_quality_report_sha256=" in result.output
    assert f"raw_collection_holder_num_blank_rows={RAW_HOLDER_BLANK_ROWS}" in result.output
    assert "canonical_holder_rows_in_window=1" in result.output
    assert "holder_num_null_rows=" not in result.output
    assert "output=" in result.output
    assert (output / "report.json").is_file()
    assert (output / "annual_source_review.parquet").is_file()
    digest = hashlib.sha256((output / "annual_source_review.parquet").read_bytes()).hexdigest()
    sealed = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert sealed["annual_review_file_sha256"] == digest
    assert sealed["holder_count_missingness"]["raw_collection_holder_num_blank_rows"] == (
        RAW_HOLDER_BLANK_ROWS
    )
    assert sealed["collection_quality_report_sha256"]
    assert sealed["ready_for_scoring"] is False
    assert sealed["ready_for_trading"] is False
