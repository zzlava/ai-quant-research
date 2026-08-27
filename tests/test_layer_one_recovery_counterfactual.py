from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import polars as pl
import pytest

from app.research.layer_one_recovery_counterfactual import (
    RecoveryCounterfactualReport,
    build_recovery_counterfactual,
    verify_recovery_counterfactual_file,
    write_recovery_counterfactual,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_real_counterfactual_preserves_audit_and_does_not_pass() -> None:
    report, frame = build_recovery_counterfactual(repo_root=_root())
    assert report.report_id is not None
    assert report.simulated_confirmation_count >= 1
    assert report.risk_lock_trigger_dates
    assert report.simulated_confirmation_is_not_observed_user_action is True
    assert report.upstream_loss_history_not_rewritten is True
    assert report.consumed_oos_reused is False
    assert report.historical_validation_evidence_pass is False
    assert report.gates.combined_max_drawdown_floor_pass is False
    assert frame.filter(pl.col("base_simulated_confirmation")).height == report.simulated_confirmation_count


def test_report_rejects_readiness_or_window_drift() -> None:
    report, _ = build_recovery_counterfactual(repo_root=_root())
    payload = report.model_dump(mode="json", exclude={"report_id"})
    payload["ready_for_trading"] = True
    with pytest.raises(ValueError):
        RecoveryCounterfactualReport.model_validate(payload)
    payload = report.model_dump(mode="json", exclude={"report_id"})
    payload["validation_end"] = "2025-01-01"
    with pytest.raises(ValueError, match="window"):
        RecoveryCounterfactualReport.model_validate(payload)


def test_write_verify_and_tamper_detection() -> None:
    root = _root()
    directory = root / "tmp" / f"recovery-counterfactual-{uuid.uuid4().hex}"
    report_path = directory / "report.json"
    daily_path = directory / "daily.parquet"
    try:
        written = write_recovery_counterfactual(repo_root=root, report_path=report_path, daily_path=daily_path)
        checked = verify_recovery_counterfactual_file(repo_root=root, report_path=report_path)
        assert checked.report_id == written.report_id
        frame = pl.read_parquet(daily_path).with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.col("base_equity") + 1.0)
            .otherwise(pl.col("base_equity"))
            .alias("base_equity")
        )
        frame.write_parquet(daily_path)
        with pytest.raises(ValueError, match="content hash"):
            verify_recovery_counterfactual_file(repo_root=root, report_path=report_path)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
