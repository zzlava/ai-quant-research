from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.errors import MissingBigQuantCredentialsError
from app.providers.bigquant_client import ACCESS_KEY_ENV, SECRET_KEY_ENV, read_bigquant_credentials
from app.universe.public_reconstruction import collect_bigquant_public_reconstruction


class FakeBigQuantClient:
    def __init__(self, frame: pl.DataFrame) -> None:
        self.frame = frame
        self.calls: list[tuple[str, dict[str, list[str]]]] = []

    def query(self, sql: str, *, filters: dict[str, list[str]]) -> pl.DataFrame:
        self.calls.append((sql, filters))
        return self.frame.clone()


def _frame(*, rows_per_day: int = 2) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for day in (date(2024, 1, 2), date(2024, 1, 3)):
        for offset in range(rows_per_day):
            rows.append(
                {
                    "date": day,
                    "instrument": "000300.SH",
                    "name": "沪深300",
                    "member_code": f"{offset + 1:06d}.SZ",
                    "member_name": f"成员{offset + 1}",
                    "weight": 50.0,
                }
            )
    return pl.DataFrame(rows)


def test_collects_isolated_candidate_and_hashes_raw_response(tmp_path: Path) -> None:
    client = FakeBigQuantClient(_frame())
    result = collect_bigquant_public_reconstruction(
        client=client,
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        output_dir=tmp_path / "public-reconstruction",
        expected_constituents=2,
        retrieved_at=datetime(2026, 8, 23, 8, tzinfo=UTC),
    )

    assert result.eligible_for_public_reconstruction is True
    assert result.complete_dates == 2
    assert result.incomplete_dates == 0
    assert result.candidate_membership_path is not None
    candidate = pl.read_csv(result.candidate_membership_path)
    assert candidate.columns == [
        "source_date",
        "index_code",
        "symbol",
        "weight",
        "source_member_name",
        "retrieved_at",
    ]
    assert "available_at" not in candidate.columns
    assert "effective_from" not in candidate.columns
    manifest = json.loads(result.collection_manifest_path.read_text(encoding="utf-8"))
    assert manifest["classification"] == "public_reconstructed_not_licensed_pit"
    assert manifest["raw_response"]["sha256"]
    assert "available_at" in manifest["availability_boundary"]
    assert client.calls[0][1] == {"date": ["2024-01-01", "2024-01-31"]}


def test_marks_incomplete_dates_ineligible_but_keeps_auditable_raw_response(tmp_path: Path) -> None:
    result = collect_bigquant_public_reconstruction(
        client=FakeBigQuantClient(_frame(rows_per_day=1)),
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        output_dir=tmp_path / "public-reconstruction",
        expected_constituents=2,
    )

    assert result.eligible_for_public_reconstruction is False
    assert result.incomplete_dates == 2
    assert result.raw_response_path.is_file()
    report = json.loads(result.quality_report_path.read_text(encoding="utf-8"))
    assert report["eligible_for_public_reconstruction"] is False
    assert report["dates"][0]["constituent_count"] == 1


def test_rejects_missing_credentials_without_echoing_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ACCESS_KEY_ENV, raising=False)
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
    with pytest.raises(MissingBigQuantCredentialsError, match="not configured"):
        read_bigquant_credentials()

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "fetch-bigquant-public-membership",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-31",
            "--output-dir",
            "/tmp/should-not-be-created",
        ],
    )
    assert result.exit_code == 1
    assert "not configured" in result.output
