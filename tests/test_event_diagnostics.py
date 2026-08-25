from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.models.events import EventSourceManifest
from app.providers.tushare_event_history import materialize_tushare_event_overlay
from app.research.event_diagnostics import (
    build_event_diagnostics,
    load_verified_event_diagnostics,
    write_event_diagnostics_atomically,
)
from app.storage.event_io import load_verified_event_snapshot
from tests.helpers import load_test_config
from tests.test_event_overlay import _market, _source_dir


def _event_bundle(tmp_path: Path) -> tuple[Path, Path]:
    market = _market(tmp_path / "market")
    event = tmp_path / "event-overlay"
    materialize_tushare_event_overlay(
        source_dir=_source_dir(tmp_path / "source"),
        market_dir=market,
        dest_dir=event,
    )
    return market, event


def _build(tmp_path: Path, as_of: date):
    market, event = _event_bundle(tmp_path)
    snapshot, tables = load_verified_event_snapshot(event)
    source = EventSourceManifest.model_validate_json(
        (event / "source_manifest.json").read_bytes()
    )
    report, frame = build_event_diagnostics(
        market_dir=market,
        event_snapshot=snapshot,
        event_source_manifest=source,
        event_tables=tables,
        config=load_test_config(),
        as_of=as_of,
    )
    return market, event, report, frame


def test_event_diagnostic_excludes_unavailable_announcement_and_future_revision(
    tmp_path: Path,
) -> None:
    _, _, before_first, before_first_frame = _build(
        tmp_path / "before-first", date(2024, 1, 19)
    )
    assert before_first.visible_event_rows["earnings_forecast_events"] == 0
    assert before_first_frame["latest_forecast_ann_date"].drop_nulls().len() == 0

    _, _, before_revision, before_frame = _build(
        tmp_path / "before-revision", date(2024, 2, 5)
    )
    before = before_frame.filter(pl.col("symbol") == "000001.SZ").row(0, named=True)
    assert before_revision.visible_event_rows["earnings_forecast_events"] == 1
    assert before["latest_forecast_type"] == "略增"
    assert before["latest_forecast_versions_seen"] == 1

    _, _, after_revision, after_frame = _build(
        tmp_path / "after-revision", date(2024, 2, 6)
    )
    after = after_frame.filter(pl.col("symbol") == "000001.SZ").row(0, named=True)
    assert after_revision.visible_event_rows["earnings_forecast_events"] == 2
    assert after["latest_forecast_type"] == "预增"
    assert after["latest_forecast_versions_seen"] == 2


def test_event_diagnostic_reports_only_announced_upcoming_unlocks(tmp_path: Path) -> None:
    _, _, report, frame = _build(tmp_path, date(2024, 5, 31))
    row = frame.filter(pl.col("symbol") == "000001.SZ").row(0, named=True)

    assert report.ready_for_scoring is False
    assert report.ready_for_trading is False
    assert report.observed_symbol_counts["announced_unlock_within_30d"] == 1
    assert row["announced_unlock_events_next_30d"] == 1
    assert row["announced_unlock_earliest_date_next_30d"] == date(2024, 6, 30)
    assert row["announced_unlock_ratio_sum_next_30d"] == pytest.approx(1.5)
    assert row["latest_audit_result"] == "标准无保留意见"
    assert row["latest_audit_is_exact_standard_unqualified"] is True


def test_event_diagnostic_artifact_is_hashed_and_rejects_tampering(tmp_path: Path) -> None:
    _, _, report, frame = _build(tmp_path / "source", date(2024, 5, 31))
    output = tmp_path / "diagnostic"
    sealed = write_event_diagnostics_atomically(output, report, frame)
    loaded, stored = load_verified_event_diagnostics(output)

    assert loaded == sealed
    assert stored.equals(frame, null_equal=True)
    report_path = output / "report.json"
    tampered_report = json.loads(report_path.read_text(encoding="utf-8"))
    tampered_report["rows"] += 1
    report_path.write_text(json.dumps(tampered_report), encoding="utf-8")
    with pytest.raises(ValueError, match="report ID"):
        load_verified_event_diagnostics(output)
    report_path.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")

    path = output / "event_diagnostics.parquet"
    pl.read_parquet(path).with_columns(pl.lit(999).alias("latest_holder_num")).write_parquet(
        path
    )
    with pytest.raises(ValueError, match="hash does not match"):
        load_verified_event_diagnostics(output)


def test_event_diagnostic_cli_remains_read_only(tmp_path: Path) -> None:
    market, event = _event_bundle(tmp_path)
    output = tmp_path / "diagnostic"
    result = CliRunner().invoke(
        cli_app,
        [
            "diagnose-a-share-event-overlay",
            "--strategy",
            "baseline_v1",
            "--as-of",
            "2024-05-31",
            "--market-dir",
            str(market),
            "--event-dir",
            str(event),
            "--output-dir",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "ready_for_scoring=false" in result.output
    assert "ready_for_trading=false" in result.output
    assert "output=" in result.output
    assert (output / "report.json").is_file()
    assert (output / "event_diagnostics.parquet").is_file()
