from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.research.layer_two_alpha_diagnostic_v2 import (
    HORIZONS,
    _deduplicate_strict_initial_reports,
    _metric_row,
    verify_diagnostic,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SEALED_REPORT_DIR = (
    REPO_ROOT
    / "data"
    / "all-a-share-historical-v1"
    / "research"
    / "layer-two-alpha-diagnostic-v2"
)


def test_v2_horizons_remain_frozen() -> None:
    assert HORIZONS == (5, 20, 40)


def test_metric_coverage_uses_eligible_denominator_and_keeps_missing_unknown() -> None:
    factors: list[object] = [float(index) for index in range(600)] + [None] * 400
    labels: list[object] = [float(index % 17) / 100.0 for index in range(600)] + [0.0] * 400

    row = _metric_row(
        window="development",
        day=date(2023, 1, 3),
        factor_id="quality",
        horizon=40,
        companion=False,
        factors=factors,
        labels=labels,
    )

    assert row["eligible_count"] == 1000
    assert row["factor_known_count"] == 600
    assert row["factor_known_fraction"] == 0.6
    assert row["coverage_pass"] is True
    assert row["labeled_count"] == 600


def test_metric_coverage_fails_closed_below_count_gate() -> None:
    row = _metric_row(
        window="development",
        day=date(2023, 1, 3),
        factor_id="value",
        horizon=40,
        companion=False,
        factors=[1.0] * 499 + [None] * 101,
        labels=[0.01] * 600,
    )

    assert row["coverage_pass"] is False
    assert row["ic"] is None
    assert row["top_minus_bottom_spread"] is None
    assert row["top_quintile_mean_return"] is None


def test_same_availability_chooses_latest_report_period_deterministically() -> None:
    available_at = datetime(2023, 4, 30, 23, 59)
    reports = pl.DataFrame(
        {
            "symbol": ["000001.SZ", "000001.SZ", "000001.SZ"],
            "report_period": [
                date(2022, 12, 31),
                date(2023, 3, 31),
                date(2023, 3, 31),
            ],
            "available_at": [available_at, available_at, available_at],
            "source_row_hash": ["a" * 64, "b" * 64, "c" * 64],
            "roe": [1.0, 2.0, 3.0],
        }
    )

    result = _deduplicate_strict_initial_reports(reports)

    assert result.height == 1
    assert result.row(0, named=True)["report_period"] == date(2023, 3, 31)
    assert result.row(0, named=True)["source_row_hash"] == "c" * 64


def test_local_sealed_report_remains_fail_closed() -> None:
    if not (SEALED_REPORT_DIR / "report.json").is_file():
        pytest.skip("local sealed research artifact is not present")

    report = verify_diagnostic(
        repo_root=REPO_ROOT,
        output_dir=SEALED_REPORT_DIR,
        full_recomputation=False,
    )

    assert report.report_id == "bb9994f9108c7e7eb77121c15292c61fed27ed87f23273642fbee3b2a8ca842b"
    assert report.selected_factor_ids == ["defensive_low_vol"]
    assert report.robustness_2024["robustness_pass"] is False
    assert report.readiness.ready_for_scoring is False
    assert report.readiness.ready_for_backtest is False
    assert report.readiness.ready_for_trading is False
    assert report.readiness.new_oos_authorized is False
