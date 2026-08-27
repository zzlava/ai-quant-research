from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.research.industry_history_contract import (
    REQUIRED_HISTORY_COLUMNS,
    IndustryHistoryManifest,
    IndustryHistoryRecord,
    compute_manifest_id,
    seal_industry_history_manifest,
    select_industry_as_of,
    verify_industry_history_source,
)


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_history(path: Path, rows: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(REQUIRED_HISTORY_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in REQUIRED_HISTORY_COLUMNS})
    return path


def _base_rows() -> list[dict[str, object]]:
    return [
        {
            "symbol": "000001.SZ",
            "industry_scheme": "demo_scheme",
            "industry_version": "demo_v1",
            "industry_code": "D01",
            "industry_name": "Demo Banks",
            "effective_from": "2020-01-02",
            "effective_to": "2021-12-31",
            "announced_at": "2019-12-20T08:00:00Z",
            "available_at": "2019-12-20T16:00:00Z",
            "source_reference": "synthetic-fixture-row-1",
        },
        {
            "symbol": "000001.SZ",
            "industry_scheme": "demo_scheme",
            "industry_version": "demo_v1",
            "industry_code": "D02",
            "industry_name": "Demo Brokers",
            "effective_from": "2022-01-04",
            "effective_to": "2023-12-29",
            "announced_at": "2021-12-15T08:00:00Z",
            "available_at": "2021-12-15T16:00:00Z",
            "source_reference": "synthetic-fixture-row-2",
        },
        {
            "symbol": "600000.SH",
            "industry_scheme": "demo_scheme",
            "industry_version": "demo_v1",
            "industry_code": "D01",
            "industry_name": "Demo Banks",
            "effective_from": "2020-01-02",
            "effective_to": "2023-12-29",
            "announced_at": "2019-12-20T08:00:00Z",
            "available_at": "2019-12-20T16:00:00Z",
            "source_reference": "synthetic-fixture-row-3",
        },
    ]


def _manifest_payload(history: Path, **overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1",
        "contract_version": "pit-industry-history-contract-v1",
        "source_name": "synthetic-research-fixture",
        "industry_scheme": "demo_scheme",
        "industry_version": "demo_v1",
        "history_file": history.name,
        "history_file_sha256": _file_sha(history),
        "coverage": {"start": "2020-01-02", "end": "2023-12-29"},
        "available_at_definition": (
            "Synthetic fixture timestamp supplied with CSV rows; "
            "not inferred from download or generated_at."
        ),
        "available_at_evidence": "Row-level available_at in the history CSV, UTC-parsed.",
        "generated_at": "2026-08-25T12:00:00Z",
        "retrieved_at": "2026-08-25T12:05:00Z",
        "pit_semantics": "point_in_time_history",
        "complete": False,
        "universe_notes": None,
        "row_count": 3,
        "covered_symbols": 2,
        "ready_for_scoring": False,
        "ready_for_backtest": False,
        "ready_for_trading": False,
        "does_not_score": True,
        "does_not_backtest": True,
        "does_not_trade": True,
    }
    payload.update(overrides)
    sealed = seal_industry_history_manifest(IndustryHistoryManifest.model_validate(payload))
    return sealed.model_dump(mode="json")


def _write_manifest(path: Path, history: Path, **overrides: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_manifest_payload(history, **overrides), indent=2) + "\n", encoding="utf-8")
    return path


def test_verify_accepts_synthetic_incomplete_fixture(tmp_path: Path) -> None:
    history = _write_history(tmp_path / "industry_history.csv", _base_rows())
    manifest = _write_manifest(tmp_path / "manifest.json", history)
    sealed, records, summary = verify_industry_history_source(
        history_file=history,
        manifest_file=manifest,
    )
    assert sealed.complete is False
    assert len(records) == 3
    assert summary.does_not_score is True
    assert summary.does_not_backtest is True
    assert summary.does_not_trade is True
    assert summary.manifest_id == compute_manifest_id(sealed)


def test_manifest_hash_mismatch_fails(tmp_path: Path) -> None:
    history = _write_history(tmp_path / "industry_history.csv", _base_rows())
    payload = _manifest_payload(history)
    payload["manifest_id"] = "0" * 64
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest_id"):
        verify_industry_history_source(history_file=history, manifest_file=manifest)


def test_history_hash_mismatch_fails(tmp_path: Path) -> None:
    history = _write_history(tmp_path / "industry_history.csv", _base_rows())
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        history,
        history_file_sha256="1" * 64,
    )
    # Re-seal with wrong hash still self-consistent but mismatches file bytes.
    with pytest.raises(ValueError, match="history_file_sha256"):
        verify_industry_history_source(history_file=history, manifest_file=manifest)


def test_missing_column_fails(tmp_path: Path) -> None:
    history = tmp_path / "industry_history.csv"
    history.write_text("symbol,industry_scheme\n000001.SZ,demo\n", encoding="utf-8")
    manifest = _write_manifest(tmp_path / "manifest.json", history, row_count=1, covered_symbols=1)
    with pytest.raises(ValueError, match="missing required columns"):
        verify_industry_history_source(history_file=history, manifest_file=manifest)


def test_coverage_mismatch_fails(tmp_path: Path) -> None:
    history = _write_history(tmp_path / "industry_history.csv", _base_rows())
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        history,
        coverage={"start": "2020-01-02", "end": "2022-01-01"},
    )
    with pytest.raises(ValueError, match="coverage"):
        verify_industry_history_source(history_file=history, manifest_file=manifest)


def test_overlapping_intervals_fail(tmp_path: Path) -> None:
    rows = _base_rows()
    rows[1]["effective_from"] = "2021-06-01"
    history = _write_history(tmp_path / "industry_history.csv", rows)
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        history,
        coverage={"start": "2020-01-02", "end": "2023-12-29"},
    )
    with pytest.raises(ValueError, match="overlapping"):
        verify_industry_history_source(history_file=history, manifest_file=manifest)


def test_scheme_version_mismatch_fails(tmp_path: Path) -> None:
    history = _write_history(tmp_path / "industry_history.csv", _base_rows())
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        history,
        industry_version="other_v1",
    )
    with pytest.raises(ValueError, match="industry_version"):
        verify_industry_history_source(history_file=history, manifest_file=manifest)


def test_announced_after_available_fails(tmp_path: Path) -> None:
    rows = _base_rows()
    rows[0]["announced_at"] = "2019-12-21T16:00:00Z"
    rows[0]["available_at"] = "2019-12-20T16:00:00Z"
    history = _write_history(tmp_path / "industry_history.csv", rows)
    manifest = _write_manifest(tmp_path / "manifest.json", history)
    with pytest.raises(ValueError, match="announced_at"):
        verify_industry_history_source(history_file=history, manifest_file=manifest)


def test_unknown_masquerade_empty_string_rejected() -> None:
    with pytest.raises(Exception, match="null when unknown"):
        IndustryHistoryManifest.model_validate(
            {
                "source_name": "x",
                "industry_scheme": "demo_scheme",
                "industry_version": "demo_v1",
                "history_file": "industry_history.csv",
                "history_file_sha256": "a" * 64,
                "coverage": {"start": "2020-01-02", "end": "2020-01-02"},
                "available_at_definition": "def",
                "available_at_evidence": "ev",
                "generated_at": "2026-08-25T12:00:00Z",
                "retrieved_at": "2026-08-25T12:00:00Z",
                "pit_semantics": "point_in_time_history",
                "complete": False,
                "universe_notes": "",
            }
        )


def test_current_static_cannot_masquerade_as_pit() -> None:
    with pytest.raises(Exception, match="point_in_time_history"):
        IndustryHistoryManifest.model_validate(
            {
                "source_name": "x",
                "industry_scheme": "demo_scheme",
                "industry_version": "demo_v1",
                "history_file": "industry_history.csv",
                "history_file_sha256": "a" * 64,
                "coverage": {"start": "2020-01-02", "end": "2020-01-02"},
                "available_at_definition": "def",
                "available_at_evidence": "ev",
                "generated_at": "2026-08-25T12:00:00Z",
                "retrieved_at": "2026-08-25T12:00:00Z",
                "pit_semantics": "current_static",
                "complete": False,
            }
        )


def test_select_known_unknown_late_available_and_ambiguity() -> None:
    records = [IndustryHistoryRecord.model_validate(row) for row in _base_rows()]
    known = select_industry_as_of(
        records,
        "000001.SZ",
        date(2020, 6, 1),
        datetime(2019, 12, 20, 16, 0, 0),
    )
    assert known.status == "known"
    assert known.industry_code == "D01"

    unknown = select_industry_as_of(
        records,
        "000001.SZ",
        date(2020, 6, 1),
        datetime(2019, 12, 19, 16, 0, 0),
    )
    assert unknown.status == "unknown"
    assert unknown.unknown_reason == "no_observable_industry_interval"
    assert unknown.record is None

    late = select_industry_as_of(
        records,
        "000001.SZ",
        date(2022, 6, 1),
        datetime(2021, 12, 14, 16, 0, 0),
    )
    assert late.status == "unknown"

    ambiguous = list(records)
    ambiguous.append(
        IndustryHistoryRecord.model_validate(
            {
                **_base_rows()[0],
                "industry_code": "D01B",
                "industry_name": "Demo Banks Alt",
                "source_reference": "synthetic-duplicate",
            }
        )
    )
    with pytest.raises(ValueError, match="ambiguous"):
        select_industry_as_of(
            ambiguous,
            "000001.SZ",
            date(2020, 6, 1),
            datetime(2019, 12, 20, 16, 0, 0),
        )


def test_select_does_not_fallback_to_later_or_current_interval() -> None:
    records = [IndustryHistoryRecord.model_validate(row) for row in _base_rows()]
    # Decision after first interval available, but effective_date in second interval
    # before second available_at — must be unknown, not fall forward to D02 or back to D01 end.
    result = select_industry_as_of(
        records,
        "000001.SZ",
        date(2022, 2, 1),
        datetime(2021, 12, 14, 16, 0, 0),
    )
    assert result.status == "unknown"
    # Even with later decision, date outside first interval and before second starts
    # with observability of only first row still unknown for 2022-02-01 if second not available.
    early_decision_on_gap = select_industry_as_of(
        records,
        "000001.SZ",
        date(2022, 1, 3),
        datetime(2021, 12, 20, 16, 0, 0),
    )
    assert early_decision_on_gap.status == "unknown"


def test_missing_history_file_fails(tmp_path: Path) -> None:
    history = tmp_path / "missing.csv"
    # Create a manifest that points at a name, but file absent at verify time.
    placeholder = _write_history(tmp_path / "industry_history.csv", _base_rows())
    manifest = _write_manifest(tmp_path / "manifest.json", placeholder)
    placeholder.unlink()
    with pytest.raises(ValueError, match="does not exist"):
        verify_industry_history_source(history_file=history, manifest_file=manifest)


def test_cli_verify_pit_industry_source(tmp_path: Path) -> None:
    history = _write_history(tmp_path / "industry_history.csv", _base_rows())
    manifest = _write_manifest(tmp_path / "manifest.json", history)
    result = CliRunner().invoke(
        cli_app,
        [
            "verify-pit-industry-source",
            "--history-file",
            str(history),
            "--manifest-file",
            str(manifest),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "does_not_score=true" in result.output
    assert "does_not_backtest=true" in result.output
    assert "does_not_trade=true" in result.output
    assert "ready_for_scoring=false" in result.output
    assert "complete=false" in result.output
