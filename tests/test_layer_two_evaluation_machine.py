from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.research.layer_two_evaluation_machine import (
    EvaluationMachineReport,
    build_evaluation_machine,
    verify_evaluation_machine_file,
    write_evaluation_machine,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built() -> tuple[EvaluationMachineReport, object, object, object]:
    return build_evaluation_machine(repo_root=_root())


def test_real_development_evaluation_preserves_boundaries(
    built: tuple[EvaluationMachineReport, object, object, object],
) -> None:
    report = built[0]
    assert report.four_arm["anchor_count"] > 0
    assert report.four_arm["monte_carlo_repeats"] == 512
    assert report.left_tail["unknown_labels_remain_unknown"] is True
    assert len(report.ic_decay["factor_results"]) == 4
    assert report.confirmatory_status == "not_evaluable"
    assert report.readiness["ready_for_trading"] is False
    assert report.forbidden_consumed_oos == "2025-01-01..2026-08-21"


def test_report_refuses_readiness_escalation(
    built: tuple[EvaluationMachineReport, object, object, object],
) -> None:
    payload = built[0].model_dump(mode="json", exclude={"report_id"})
    payload["readiness"]["ready_for_trading"] = True
    with pytest.raises(ValueError, match="authorize"):
        EvaluationMachineReport.model_validate(payload)


def test_write_then_full_recompute() -> None:
    root = _root()
    destination = root / "data/all-a-share-historical-v1/research/layer-two-evaluation-machine-v1"
    existed = destination.exists()
    if existed:
        shutil.rmtree(destination)
    try:
        written = write_evaluation_machine(repo_root=root)
        checked = verify_evaluation_machine_file(repo_root=root)
        assert checked.report_id == written.report_id
    finally:
        if not existed:
            shutil.rmtree(destination, ignore_errors=True)
