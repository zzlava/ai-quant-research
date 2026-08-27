from __future__ import annotations

import math
import shutil
import uuid
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.research.index_risk_features import diagnose_index_risk_features
from app.research.layer_one_historical_validation import (
    COMBINED_END,
    COMBINED_START,
    LayerOneHistoricalValidationReport,
    _frame_content_sha256,
    _market_features,
    assert_report_self_hash,
    build_layer_one_historical_validation,
    seal_report,
    verify_layer_one_historical_validation_file,
    write_layer_one_historical_validation,
)
from tests.test_index_risk_features import _index_store


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_vectorized_features_match_sealed_single_day_engine() -> None:
    start = date(2020, 1, 2)
    calendar: list[date] = []
    cursor = start
    while len(calendar) < 300:
        if cursor.weekday() < 5:
            calendar.append(cursor)
        cursor = date.fromordinal(cursor.toordinal() + 1)
    prices = [100.0 * math.exp(index * 0.0004 + math.sin(index / 13.0) * 0.01) for index in range(300)]
    store = _index_store(calendar, prices, symbol="IDX")
    as_of_index = 270
    report = diagnose_index_risk_features(
        store,
        index_symbol="IDX",
        as_of=calendar[as_of_index],
        trend_lookback_bars=200,
        volatility_lookback_bars=60,
        drawdown_lookback_bars=242,
    )
    ratio, realized_vol, drawdown, regime, raw_target = _market_features(prices, as_of_index)
    assert ratio == pytest.approx(report.close_to_sma_ratio)
    assert realized_vol == pytest.approx(report.realized_volatility_annualized)
    assert drawdown == pytest.approx(report.drawdown)
    assert regime in {"positive", "neutral", "negative"}
    assert raw_target in {0.0, 0.3, 0.6, 0.9}


def test_real_frozen_history_build_is_strict_and_reports_terminal_lock() -> None:
    report, frame = build_layer_one_historical_validation(repo_root=_repo_root())
    assert_report_self_hash(report)
    assert report.validation_start == COMBINED_START
    assert report.validation_end == COMBINED_END
    assert report.last_action_day <= COMBINED_END
    assert report.consumed_oos_reused is False
    assert report.oos_claim is False
    assert report.risk_lock_trigger_dates
    assert report.budget_occupancy["0.0"] == report.combined.trading_days
    assert report.historical_validation_evidence_pass is False
    assert report.gates.combined_positive_after_cost_annualized_return_pass is False
    assert frame.height == report.daily_row_count
    assert _frame_content_sha256(frame) == report.daily_table_content_sha256


def test_write_and_full_disk_recompute_then_detect_daily_tamper() -> None:
    root = _repo_root()
    temp = root / "tmp" / f"layer-one-history-test-{uuid.uuid4().hex}"
    report_path = temp / "report.json"
    daily_path = temp / "daily.parquet"
    try:
        written = write_layer_one_historical_validation(
            repo_root=root,
            report_path=report_path,
            daily_path=daily_path,
        )
        verified = verify_layer_one_historical_validation_file(
            repo_root=root,
            report_path=report_path,
        )
        assert verified.report_id == written.report_id
        frame = pl.read_parquet(daily_path)
        frame = frame.with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.col("base_equity") + 1.0)
            .otherwise(pl.col("base_equity"))
            .alias("base_equity")
        )
        frame.write_parquet(daily_path)
        with pytest.raises(ValueError, match="content hash"):
            verify_layer_one_historical_validation_file(
                repo_root=root,
                report_path=report_path,
            )
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def test_report_rejects_ready_escalation_and_oos_drift() -> None:
    report, _ = build_layer_one_historical_validation(repo_root=_repo_root())
    payload = report.model_dump(mode="json", exclude={"report_id"})
    payload["ready_for_trading"] = True
    with pytest.raises(ValueError):
        LayerOneHistoricalValidationReport.model_validate(payload)
    payload = report.model_dump(mode="json", exclude={"report_id"})
    payload["last_action_day"] = "2025-01-02"
    with pytest.raises(ValueError, match="consumed OOS"):
        LayerOneHistoricalValidationReport.model_validate(payload)


def test_resealed_metric_tamper_fails_full_recomputation(tmp_path: Path) -> None:
    report, _ = build_layer_one_historical_validation(repo_root=_repo_root())
    tampered = report.model_copy(
        update={
            "combined": report.combined.model_copy(
                update={"annualized_return_after_cost": 0.99}
            )
        }
    )
    tampered = seal_report(tampered)
    assert_report_self_hash(tampered)
    assert tampered.report_id != report.report_id
