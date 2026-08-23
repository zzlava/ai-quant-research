from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.errors import DataQualityError
from app.models.config import StrategyConfig
from app.providers.tushare_client import TOKEN_ENV
from app.storage.quality import parse_available_at_utc
from app.universe.materialize import read_universe_snapshots_file
from app.universe.provenance import load_universe_source_manifest, verify_universe_source
from tests.helpers import CONFIG_DIR, PROJECT_ROOT, load_test_config

A = "000001.SZ"
B = "600000.SH"

OFFLINE_NOTICE = "来源清单由用户/可信来源提供，本命令只验证，不下载/不生成/不把下载时间当 available_at"


def _hist_config(expected: int | None = 2) -> StrategyConfig:
    config = load_test_config()
    config.universe.mode = "historical_membership"
    config.universe.id = "csi300"
    config.universe.expected_constituents = expected
    return config


def _write_snapshots(path: Path, rows: list[str]) -> Path:
    path.write_text(
        "universe_id,effective_from,symbol,available_at,weight\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return path


def _fixture_rows() -> list[str]:
    return [
        f"csi300,2024-01-02,{A},2024-01-01T16:00:00Z,0.5",
        f"csi300,2024-01-02,{B},2024-01-01T16:00:00Z,0.5",
        f"csi300,2024-01-08,{A},2024-01-07T16:00:00.000001Z,0.51",
        f"csi300,2024-01-08,{B},2024-01-07T16:00:00.000001Z,0.49",
    ]


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_payload(snapshots: Path, **overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1",
        "universe_id": "csi300",
        "source_name": "synthetic-research-fixture",
        "snapshots_file_sha256": _file_sha(snapshots),
        "file_obtained_at": "2026-08-23T04:00:00Z",
        "effective_from_coverage": {"start": "2024-01-02", "end": "2024-01-08"},
        "available_at_definition": (
            "Synthetic fixture timestamp supplied with the CSV rows; "
            "not inferred from download or file_obtained_at."
        ),
        "available_at_evidence": "Row-level available_at in the snapshots CSV, independently UTC-parsed.",
        "source_note": "Synthetic research fixture for pipeline verification; not historical CSI300 constituents.",
        "expected_constituents": 2,
    }
    payload.update(overrides)
    return payload


def _write_manifest(path: Path, snapshots: Path, **overrides: object) -> Path:
    path.write_text(json.dumps(_manifest_payload(snapshots, **overrides), indent=2) + "\n", encoding="utf-8")
    return path


def _write_event_evidence_ledger(
    root: Path,
    *,
    first_available_at: str = "2024-01-01T16:00:00Z",
    second_available_at: str = "2024-01-07T16:00:00.000001Z",
    extra_row: str | None = None,
) -> Path:
    sources = root / "sources"
    sources.mkdir(exist_ok=True)
    first_document = sources / "notice-2024-01-02.txt"
    second_document = sources / "notice-2024-01-08.txt"
    first_document.write_text("official notice for fixture 2024-01-02\n", encoding="utf-8")
    second_document.write_text("official notice for fixture 2024-01-08\n", encoding="utf-8")
    lines = [
        (
            "effective_from,available_at,availability_basis,source_published_on,evidence_type,"
            "source_url,source_document,source_document_sha256"
        ),
        (
            f"2024-01-02,{first_available_at},conservative_next_cn_decision_after_notice_date,"
            f"2023-12-29,official_adjustment_notice,https://example.invalid/notice-1,"
            f"sources/{first_document.name},{_file_sha(first_document)}"
        ),
        (
            f"2024-01-08,{second_available_at},conservative_next_cn_decision_after_notice_date,"
            f"2024-01-05,official_adjustment_notice,https://example.invalid/notice-2,"
            f"sources/{second_document.name},{_file_sha(second_document)}"
        ),
    ]
    if extra_row is not None:
        lines.append(extra_row)
    ledger = root / "event_evidence.csv"
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ledger


def _write_v2_manifest(path: Path, snapshots: Path, ledger: Path, **overrides: object) -> Path:
    return _write_manifest(
        path,
        snapshots,
        schema_version="2",
        event_evidence_ledger={"path": ledger.name, "sha256": _file_sha(ledger)},
        **overrides,
    )


def _write_hist_yaml(dest: Path, expected: int = 2, mode: str = "historical_membership") -> Path:
    with (CONFIG_DIR / "baseline_v1.yaml").open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    payload["universe"]["mode"] = mode
    payload["universe"]["id"] = "csi300"
    payload["universe"]["expected_constituents"] = expected
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "hist_two.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return dest


def test_cli_success_is_offline_and_does_not_read_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> str:
        raise AssertionError("token must not be read")

    monkeypatch.setattr("app.providers.tushare_client.read_tushare_token", boom)
    monkeypatch.setenv(TOKEN_ENV, "should-not-be-read")
    strategies = _write_hist_yaml(tmp_path / "config" / "strategies")
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(tmp_path / "config"))
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    original = snapshots.read_bytes()
    provenance = _write_manifest(tmp_path / "manifest.json", snapshots)
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "verify-universe-source",
            "--snapshots-file",
            str(snapshots),
            "--provenance-file",
            str(provenance),
            "--strategy",
            "hist_two",
        ],
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    out = result.stdout or ""
    assert "universe_id=csi300" in out
    assert "source_name=synthetic-research-fixture" in out
    assert f"snapshots_file_sha256={_file_sha(snapshots)}" in out
    assert "effective_from_coverage=2024-01-02..2024-01-08" in out
    assert "snapshot_count=2" in out
    assert "expected_constituents=2" in out
    assert OFFLINE_NOTICE in out
    assert "should-not-be-read" not in (out + (result.stderr or ""))
    assert snapshots.read_bytes() == original
    loaded = read_universe_snapshots_file(snapshots)
    assert all(item.available_at.isoformat() != "2026-08-23T04:00:00" for item in loaded)
    assert loaded[1].available_at.microsecond == 1
    assert strategies.exists()


def test_any_byte_change_mismatches_hash(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    provenance = _write_manifest(tmp_path / "manifest.json", snapshots)
    snapshots.write_bytes(snapshots.read_bytes() + b" ")
    with pytest.raises(DataQualityError, match="does not match the exact bytes"):
        verify_universe_source(
            snapshots_file=snapshots,
            provenance_file=provenance,
            config=_hist_config(),
        )


def test_schema_v2_binds_each_snapshot_to_hashed_event_evidence(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    ledger = _write_event_evidence_ledger(tmp_path)
    provenance = _write_v2_manifest(tmp_path / "manifest.json", snapshots, ledger)
    result = verify_universe_source(
        snapshots_file=snapshots,
        provenance_file=provenance,
        config=_hist_config(),
    )
    assert result.schema_version == "2"
    assert result.event_evidence_count == 2
    assert result.event_evidence_ledger_sha256 == _file_sha(ledger)


def test_schema_v2_requires_event_evidence_ledger(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    provenance = _write_manifest(tmp_path / "manifest.json", snapshots, schema_version="2")
    with pytest.raises(DataQualityError, match="requires event_evidence_ledger"):
        load_universe_source_manifest(provenance)


def test_schema_v2_rejects_changed_ledger_bytes(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    ledger = _write_event_evidence_ledger(tmp_path)
    provenance = _write_v2_manifest(tmp_path / "manifest.json", snapshots, ledger)
    ledger.write_bytes(ledger.read_bytes() + b" ")
    with pytest.raises(DataQualityError, match="event_evidence_ledger.sha256"):
        verify_universe_source(
            snapshots_file=snapshots,
            provenance_file=provenance,
            config=_hist_config(),
        )


def test_schema_v2_rejects_snapshot_available_at_that_differs_from_event_evidence(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    ledger = _write_event_evidence_ledger(tmp_path, second_available_at="2024-01-07T16:00:01Z")
    provenance = _write_v2_manifest(tmp_path / "manifest.json", snapshots, ledger)
    with pytest.raises(DataQualityError, match="available_at does not match"):
        verify_universe_source(
            snapshots_file=snapshots,
            provenance_file=provenance,
            config=_hist_config(),
        )


def test_schema_v2_rejects_orphan_event_evidence(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    extra_document = tmp_path / "sources" / "notice-extra.txt"
    extra_document.parent.mkdir()
    extra_document.write_text("extra event fixture\n", encoding="utf-8")
    extra_row = (
        "2024-01-15,2024-01-14T16:00:00Z,conservative_next_cn_decision_after_notice_date,"
        "2024-01-12,official_adjustment_notice,https://example.invalid/notice-extra,"
        f"sources/{extra_document.name},{_file_sha(extra_document)}"
    )
    ledger = _write_event_evidence_ledger(tmp_path, extra_row=extra_row)
    provenance = _write_v2_manifest(tmp_path / "manifest.json", snapshots, ledger)
    with pytest.raises(DataQualityError, match="orphaned ledger dates"):
        verify_universe_source(
            snapshots_file=snapshots,
            provenance_file=provenance,
            config=_hist_config(),
        )


def test_schema_v2_rejects_source_document_hash_change_and_path_escape(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    ledger = _write_event_evidence_ledger(tmp_path)
    provenance = _write_v2_manifest(tmp_path / "manifest.json", snapshots, ledger)
    document = tmp_path / "sources" / "notice-2024-01-02.txt"
    document.write_text("changed notice fixture\n", encoding="utf-8")
    with pytest.raises(DataQualityError, match="source_document_sha256"):
        verify_universe_source(
            snapshots_file=snapshots,
            provenance_file=provenance,
            config=_hist_config(),
        )

    ledger = _write_event_evidence_ledger(tmp_path)
    escaped = ledger.read_text(encoding="utf-8").replace(
        "sources/notice-2024-01-02.txt", "../notice-2024-01-02.txt"
    )
    ledger.write_text(escaped, encoding="utf-8")
    provenance = _write_v2_manifest(tmp_path / "escaped.json", snapshots, ledger)
    with pytest.raises(DataQualityError, match="relative path inside the provenance directory"):
        verify_universe_source(
            snapshots_file=snapshots,
            provenance_file=provenance,
            config=_hist_config(),
        )


def test_wrong_manifest_hash_is_rejected(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    provenance = _write_manifest(tmp_path / "manifest.json", snapshots, snapshots_file_sha256="a" * 64)
    with pytest.raises(DataQualityError, match="does not match the exact bytes"):
        verify_universe_source(
            snapshots_file=snapshots,
            provenance_file=provenance,
            config=_hist_config(),
        )


def test_universe_mismatch_is_rejected(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    provenance = _write_manifest(tmp_path / "manifest.json", snapshots, universe_id="other")
    with pytest.raises(DataQualityError, match="universe_id"):
        verify_universe_source(
            snapshots_file=snapshots,
            provenance_file=provenance,
            config=_hist_config(),
        )


def test_missing_available_at_definition_or_evidence_is_rejected(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    missing_def = _manifest_payload(snapshots)
    del missing_def["available_at_definition"]
    (tmp_path / "no_def.json").write_text(json.dumps(missing_def), encoding="utf-8")
    with pytest.raises(DataQualityError, match="available_at_definition"):
        load_universe_source_manifest(tmp_path / "no_def.json")
    missing_ev = _manifest_payload(snapshots)
    del missing_ev["available_at_evidence"]
    (tmp_path / "no_ev.json").write_text(json.dumps(missing_ev), encoding="utf-8")
    with pytest.raises(DataQualityError, match="available_at_evidence"):
        load_universe_source_manifest(tmp_path / "no_ev.json")
    blank = _write_manifest(tmp_path / "blank.json", snapshots, available_at_definition="   ")
    with pytest.raises(DataQualityError, match="available_at_definition"):
        load_universe_source_manifest(blank)


def test_coverage_mismatch_is_rejected(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    provenance = _write_manifest(
        tmp_path / "manifest.json",
        snapshots,
        effective_from_coverage={"start": "2024-01-02", "end": "2024-01-05"},
    )
    with pytest.raises(DataQualityError, match="effective_from_coverage"):
        verify_universe_source(
            snapshots_file=snapshots,
            provenance_file=provenance,
            config=_hist_config(),
        )


@pytest.mark.parametrize(
    "rows",
    [
        [f"csi300,2024-01-02,{A},2024-01-01T16:00:00Z,1.0"],
        [
            f"csi300,2024-01-02,{A},2024-01-01T16:00:00Z,0.5",
            f"csi300,2024-01-02,{B},2024-01-01T16:00:00Z,0.5",
        ],
        [
            f"csi300,2024-01-02,{A},2024-01-01T16:00:00Z,0.5",
            f"csi300,2024-01-02,{B},2024-01-01T16:00:00Z,0.5",
            f"csi300,2024-01-08,{A},2024-01-07T16:00:00Z,1.0",
        ],
    ],
    ids=["one_member", "two_members", "incomplete_second_cross_section"],
)
def test_csi300_expected_300_rejects_small_or_incomplete_fixtures(tmp_path: Path, rows: list[str]) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", rows)
    days = sorted({row.split(",")[1] for row in rows})
    provenance = _write_manifest(
        tmp_path / "manifest.json",
        snapshots,
        expected_constituents=300,
        effective_from_coverage={"start": days[0], "end": days[-1]},
    )
    with (CONFIG_DIR / "baseline_csi300_pit_v1.yaml").open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    config = StrategyConfig.model_validate(payload)
    assert config.universe.expected_constituents == 300
    with pytest.raises(DataQualityError, match="expected_constituents"):
        verify_universe_source(
            snapshots_file=snapshots,
            provenance_file=provenance,
            config=config,
        )


def test_manual_static_is_rejected(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    provenance = _write_manifest(tmp_path / "manifest.json", snapshots)
    config = load_test_config()
    config.universe.mode = "manual_static"
    config.universe.id = "csi300"
    with pytest.raises(DataQualityError, match="historical_membership"):
        verify_universe_source(
            snapshots_file=snapshots,
            provenance_file=provenance,
            config=config,
        )


def test_cli_manual_static_does_not_read_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> str:
        raise AssertionError("token must not be read")

    monkeypatch.setattr("app.providers.tushare_client.read_tushare_token", boom)
    monkeypatch.setenv(TOKEN_ENV, "should-not-be-read")
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    provenance = _write_manifest(tmp_path / "manifest.json", snapshots)
    result = CliRunner().invoke(
        cli_app,
        [
            "verify-universe-source",
            "--snapshots-file",
            str(snapshots),
            "--provenance-file",
            str(provenance),
            "--strategy",
            "baseline_real_cn_v1",
        ],
    )
    assert result.exit_code != 0
    combined = ((result.stdout or "") + (result.stderr or "")).lower()
    assert "manual_static" in combined or "historical_membership" in combined
    assert "should-not-be-read" not in combined


def test_missing_source_reference_is_rejected(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    payload = _manifest_payload(snapshots)
    del payload["source_note"]
    (tmp_path / "no_src.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DataQualityError, match="source_url|announcement_id|source_note"):
        load_universe_source_manifest(tmp_path / "no_src.json")


def test_file_obtained_at_rejects_date_only_that_parse_available_at_utc_would_accept(
    tmp_path: Path,
) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    midnight = parse_available_at_utc("2026-08-23")
    assert midnight.hour == 0 and midnight.minute == 0 and midnight.second == 0
    provenance = _write_manifest(tmp_path / "date_only.json", snapshots, file_obtained_at="2026-08-23")
    with pytest.raises(DataQualityError, match="file_obtained_at|date-only"):
        load_universe_source_manifest(provenance)


def test_illegal_obtained_at_and_hash_are_rejected(tmp_path: Path) -> None:
    snapshots = _write_snapshots(tmp_path / "snap.csv", _fixture_rows())
    offset = _write_manifest(
        tmp_path / "offset.json",
        snapshots,
        file_obtained_at="2026-08-23T04:00:00-05:00",
    )
    with pytest.raises(DataQualityError, match="file_obtained_at|UTC"):
        load_universe_source_manifest(offset)
    blank_time = _write_manifest(tmp_path / "blank_time.json", snapshots, file_obtained_at="  ")
    with pytest.raises(DataQualityError, match="file_obtained_at"):
        load_universe_source_manifest(blank_time)
    short_hash = _write_manifest(tmp_path / "short.json", snapshots, snapshots_file_sha256="abc")
    with pytest.raises(DataQualityError, match="sha256|SHA-256"):
        load_universe_source_manifest(short_hash)
    extra = _manifest_payload(snapshots)
    extra["downloaded_at"] = "2026-08-23T04:00:00Z"
    (tmp_path / "extra.json").write_text(json.dumps(extra), encoding="utf-8")
    with pytest.raises(DataQualityError, match="unknown field"):
        load_universe_source_manifest(tmp_path / "extra.json")
