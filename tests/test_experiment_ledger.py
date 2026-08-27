from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.research.experiment_ledger import (
    DEFAULT_RESEARCH_TRIAL_LEDGER_PATH,
    DateWindow,
    DeflatedSharpeRatioInputs,
    ResearchTrial,
    ResearchTrialLedger,
    assess_deflated_sharpe_ratio,
    compute_ledger_id,
    seal_research_trial_ledger,
    summarize_research_trial_ledger,
    verify_research_trial_ledger,
    write_research_trial_ledger,
)
from tests.helpers import PROJECT_ROOT

COMMITTED_LEDGER = PROJECT_ROOT / DEFAULT_RESEARCH_TRIAL_LEDGER_PATH

PORTFOLIO_FREEZE_ID = "e5cdb0ff04e5eb78c331d6e4af77d4f8932a683e3f1558f83945708d48d00cc0"
PORTFOLIO_AUTHORIZATION_ID = "f27fcb1fe8b1a3b1f4d2cb02633911fe61f37f00c37fe94301e320a7f79e16c5"
PORTFOLIO_FREEZE_PATH = "config/research/all-a-share-portfolio-oos-freeze-v1.json"
PORTFOLIO_AUTHORIZATION_PATH = "config/research/all-a-share-portfolio-oos-one-shot-authorization-v1.json"
PORTFOLIO_RECEIPT_PATH = (
    "data/all-a-share-oos-20241001-20260821-v1/portfolio-oos-evaluations/one-shot-v1.consumption-receipt.json"
)
EVIDENCE_DOC = "docs/research/experiment-governance-and-multiple-testing.md"


def _minimal_trial(
    *,
    trial_id: str = "t1",
    family_id: str = "fam",
    parent_trial_id: str | None = None,
    evidence_doc: str = EVIDENCE_DOC,
    oos_consumed: bool = False,
    freeze_id: str | None = None,
    authorization_id: str | None = None,
    freeze_path: str | None = None,
    authorization_path: str | None = None,
    receipt_path: str | None = None,
    oos_reuse_claim: str = "not_applicable",
    stage: str = "development",
    status: str = "recorded",
    declared_before_observation: str = "unknown",
    config_hash: str | None = None,
    data_snapshot_id: str | None = None,
    development_window: DateWindow | None = None,
    evaluation_window: DateWindow | None = None,
) -> ResearchTrial:
    return ResearchTrial(
        trial_id=trial_id,
        family_id=family_id,
        parent_trial_id=parent_trial_id,
        hypothesis="test hypothesis",
        stage=stage,
        status=status,
        strategy_config_id=None,
        config_hash=config_hash,
        data_snapshot_id=data_snapshot_id,
        development_window=development_window,
        evaluation_window=evaluation_window,
        primary_endpoint=None,
        result_direction="unknown",
        result_status="unknown",
        evidence_doc=evidence_doc,
        declared_before_observation=declared_before_observation,
        oos_consumed=oos_consumed,
        freeze_id=freeze_id,
        authorization_id=authorization_id,
        freeze_path=freeze_path,
        authorization_path=authorization_path,
        receipt_path=receipt_path,
        oos_reuse_claim=oos_reuse_claim,
    )


def _ledger(trials: list[ResearchTrial], *, complete: bool = False) -> ResearchTrialLedger:
    return ResearchTrialLedger(
        complete=complete,
        historical_backfill=not complete,
        counting_notes="test ledger",
        trials=trials,
    )


def _portfolio_consumed_trial(**overrides: Any) -> ResearchTrial:
    kwargs: dict[str, Any] = {
        "trial_id": "oos-1",
        "stage": "oos",
        "status": "no_go",
        "oos_consumed": True,
        "freeze_id": PORTFOLIO_FREEZE_ID,
        "authorization_id": PORTFOLIO_AUTHORIZATION_ID,
        "freeze_path": PORTFOLIO_FREEZE_PATH,
        "authorization_path": PORTFOLIO_AUTHORIZATION_PATH,
        "receipt_path": PORTFOLIO_RECEIPT_PATH,
        "oos_reuse_claim": "consumed_terminal",
        "evidence_doc": EVIDENCE_DOC,
    }
    kwargs.update(overrides)
    return _minimal_trial(**kwargs)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _binding_fixture(
    tmp_path: Path,
    *,
    freeze_mutate: dict[str, Any] | None = None,
    auth_mutate: dict[str, Any] | None = None,
    receipt_mutate: dict[str, Any] | None = None,
    freeze_raw: str | None = None,
    auth_raw: str | None = None,
    receipt_raw: str | None = None,
) -> tuple[Path, ResearchTrial]:
    """Build a temporary repo with copied portfolio evidence, optionally mutated."""
    evidence_rel = "docs/evidence.md"
    freeze_rel = "config/freeze.json"
    auth_rel = "config/authorization.json"
    receipt_rel = "data/receipt.json"

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / evidence_rel).write_text("evidence\n", encoding="utf-8")

    freeze_src = PROJECT_ROOT / PORTFOLIO_FREEZE_PATH
    auth_src = PROJECT_ROOT / PORTFOLIO_AUTHORIZATION_PATH
    receipt_src = PROJECT_ROOT / PORTFOLIO_RECEIPT_PATH

    if freeze_raw is not None:
        (tmp_path / freeze_rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / freeze_rel).write_text(freeze_raw, encoding="utf-8")
    else:
        freeze_payload = json.loads(freeze_src.read_text(encoding="utf-8"))
        if freeze_mutate:
            freeze_payload.update(freeze_mutate)
        _write_json(tmp_path / freeze_rel, freeze_payload)

    if auth_raw is not None:
        (tmp_path / auth_rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / auth_rel).write_text(auth_raw, encoding="utf-8")
    else:
        auth_payload = json.loads(auth_src.read_text(encoding="utf-8"))
        if auth_mutate:
            auth_payload.update(auth_mutate)
        _write_json(tmp_path / auth_rel, auth_payload)

    if receipt_raw is not None:
        (tmp_path / receipt_rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / receipt_rel).write_text(receipt_raw, encoding="utf-8")
    else:
        receipt_payload = json.loads(receipt_src.read_text(encoding="utf-8"))
        if receipt_mutate:
            receipt_payload.update(receipt_mutate)
        _write_json(tmp_path / receipt_rel, receipt_payload)

    trial = _minimal_trial(
        trial_id="oos-binding",
        stage="oos",
        status="no_go",
        oos_consumed=True,
        freeze_id=PORTFOLIO_FREEZE_ID,
        authorization_id=PORTFOLIO_AUTHORIZATION_ID,
        freeze_path=freeze_rel,
        authorization_path=auth_rel,
        receipt_path=receipt_rel,
        oos_reuse_claim="consumed_terminal",
        evidence_doc=evidence_rel,
    )
    return tmp_path, trial


def _verify_binding_ledger(tmp_path: Path, trial: ResearchTrial) -> None:
    path = tmp_path / "ledger.json"
    write_research_trial_ledger(path, _ledger([trial]))
    verify_research_trial_ledger(ledger_path=path, repo_root=tmp_path)


def test_committed_ledger_verifies_and_summarizes() -> None:
    ledger, summary = verify_research_trial_ledger(
        ledger_path=COMMITTED_LEDGER,
        repo_root=PROJECT_ROOT,
    )
    assert ledger.complete is False
    assert ledger.historical_backfill is True
    assert summary.trial_count_is_lower_bound is True
    assert summary.trial_count == len(ledger.trials) == 36
    assert summary.oos_consumed_count == 2
    assert summary.declared_before_observation_yes == 29
    assert summary.declared_before_observation_no == 0
    assert summary.declared_before_observation_unknown == 7
    assert summary.counts_by_family["all-a-share-portfolio-construction-v2"] == 13
    assert summary.counts_by_family["a-share-event-candidate"] == 12
    assert ledger.ready_for_scoring is False
    assert ledger.ready_for_trading is False
    assert ledger.auto_deploy is False
    consumed = [t for t in ledger.trials if t.oos_consumed]
    assert len(consumed) == 2
    assert all(t.oos_reuse_claim == "consumed_terminal" for t in consumed)
    assert all(
        t.freeze_path is not None and t.authorization_path is not None and t.receipt_path is not None for t in consumed
    )
    by_id = {t.trial_id: t for t in consumed}
    assert by_id["all-a-share-portfolio-p10-h20-oos-2025plus"].freeze_path == PORTFOLIO_FREEZE_PATH
    assert by_id["all-a-share-portfolio-p10-h20-oos-2025plus"].authorization_path == PORTFOLIO_AUTHORIZATION_PATH
    assert (
        by_id["a-share-event-candidate-oos-2025plus"].freeze_path
        == "config/research/a-share-event-candidate-oos-freeze-v1.json"
    )
    assert (
        by_id["a-share-event-candidate-oos-2025plus"].authorization_path
        == "config/research/a-share-event-candidate-oos-one-shot-authorization-v1.json"
    )


def test_stable_serialization_and_self_hash(tmp_path: Path) -> None:
    ledger = seal_research_trial_ledger(_ledger([_minimal_trial()]))
    path = tmp_path / "ledger.json"
    write_research_trial_ledger(path, ledger.model_copy(update={"ledger_id": None}))
    again = seal_research_trial_ledger(_ledger([_minimal_trial()]))
    assert again.ledger_id == compute_ledger_id(again)
    verified, summary = verify_research_trial_ledger(ledger_path=path, repo_root=PROJECT_ROOT)
    assert verified.ledger_id == again.ledger_id
    assert summary.trial_count == 1
    assert summary.trial_count_is_lower_bound is True


def test_hash_tamper_fails(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    write_research_trial_ledger(path, _ledger([_minimal_trial()]))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["ledger_id"] = "0" * 64
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ledger_id does not match"):
        verify_research_trial_ledger(ledger_path=path, repo_root=PROJECT_ROOT)


def test_duplicate_trial_id_fails(tmp_path: Path) -> None:
    ledger = _ledger(
        [
            _minimal_trial(trial_id="dup"),
            _minimal_trial(trial_id="dup", family_id="other"),
        ]
    )
    path = tmp_path / "ledger.json"
    write_research_trial_ledger(path, ledger)
    with pytest.raises(ValueError, match="duplicate trial_id"):
        verify_research_trial_ledger(ledger_path=path, repo_root=PROJECT_ROOT)


def test_missing_parent_and_parent_order_fail(tmp_path: Path) -> None:
    missing = _ledger([_minimal_trial(parent_trial_id="missing-parent")])
    path = tmp_path / "missing.json"
    write_research_trial_ledger(path, missing)
    with pytest.raises(ValueError, match="parent_trial_id does not exist"):
        verify_research_trial_ledger(ledger_path=path, repo_root=PROJECT_ROOT)

    child_first = _ledger(
        [
            _minimal_trial(trial_id="child", parent_trial_id="parent"),
            _minimal_trial(trial_id="parent"),
        ]
    )
    path2 = tmp_path / "order.json"
    write_research_trial_ledger(path2, child_first)
    with pytest.raises(ValueError, match="must appear earlier"):
        verify_research_trial_ledger(ledger_path=path2, repo_root=PROJECT_ROOT)


def test_path_escape_and_missing_evidence_fail(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _minimal_trial(evidence_doc="")

    escape = _ledger([_minimal_trial(evidence_doc="../outside.md")])
    path = tmp_path / "escape.json"
    sealed = seal_research_trial_ledger(escape)
    path.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="relative path without parent traversal"):
        verify_research_trial_ledger(ledger_path=path, repo_root=PROJECT_ROOT)

    missing = _ledger([_minimal_trial(evidence_doc="docs/does-not-exist-evidence.md")])
    path2 = tmp_path / "missing-evidence.json"
    write_research_trial_ledger(path2, missing)
    with pytest.raises(ValueError, match="does not exist"):
        verify_research_trial_ledger(ledger_path=path2, repo_root=PROJECT_ROOT)


def test_oos_duplicate_and_missing_bindings_fail(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="require freeze_id"):
        _minimal_trial(
            oos_consumed=True,
            stage="oos",
            status="no_go",
            oos_reuse_claim="consumed_terminal",
        )

    with pytest.raises(ValidationError, match="freeze_path"):
        _minimal_trial(
            oos_consumed=True,
            stage="oos",
            status="no_go",
            oos_reuse_claim="consumed_terminal",
            freeze_id=PORTFOLIO_FREEZE_ID,
            authorization_id=PORTFOLIO_AUTHORIZATION_ID,
            receipt_path=PORTFOLIO_RECEIPT_PATH,
        )

    first = _portfolio_consumed_trial(trial_id="oos-1")
    second = _portfolio_consumed_trial(trial_id="oos-2")
    path = tmp_path / "dup-oos.json"
    write_research_trial_ledger(path, _ledger([first, second]))
    with pytest.raises(ValueError, match="consumed more than once"):
        verify_research_trial_ledger(ledger_path=path, repo_root=PROJECT_ROOT)


def test_unknown_semantics_reject_masquerades() -> None:
    with pytest.raises(ValidationError):
        _minimal_trial(config_hash="")
    with pytest.raises(ValidationError):
        _minimal_trial(data_snapshot_id="")
    with pytest.raises(ValidationError):
        ResearchTrial.model_validate(
            {
                **_minimal_trial().model_dump(mode="json"),
                "config_hash": 0,
            }
        )
    with pytest.raises(ValidationError):
        DateWindow(start=date(2024, 1, 2), end=date(2024, 1, 1))
    with pytest.raises(ValidationError):
        DateWindow(start=date(2024, 1, 1), end=None)


def test_consumed_oos_cannot_be_reusable(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="consumed_terminal"):
        _portfolio_consumed_trial(oos_reuse_claim="available")

    consumed = _portfolio_consumed_trial(trial_id="oos-consumed")
    claim_clean = _minimal_trial(
        trial_id="claim-clean",
        freeze_id=PORTFOLIO_FREEZE_ID,
        oos_consumed=False,
        oos_reuse_claim="available",
    )
    path = tmp_path / "reuse.json"
    write_research_trial_ledger(path, _ledger([consumed, claim_clean]))
    with pytest.raises(ValueError, match="already consumed"):
        verify_research_trial_ledger(ledger_path=path, repo_root=PROJECT_ROOT)


def test_available_claim_before_consumed_also_fails(tmp_path: Path) -> None:
    claim_first = _minimal_trial(
        trial_id="claim-first",
        freeze_id=PORTFOLIO_FREEZE_ID,
        oos_consumed=False,
        oos_reuse_claim="available",
    )
    consumed_later = _portfolio_consumed_trial(trial_id="oos-later")
    path = tmp_path / "reuse-order.json"
    write_research_trial_ledger(path, _ledger([claim_first, consumed_later]))
    with pytest.raises(ValueError, match="already consumed"):
        verify_research_trial_ledger(ledger_path=path, repo_root=PROJECT_ROOT)


def test_consumed_evidence_binding_wrong_freeze_id(tmp_path: Path) -> None:
    root, trial = _binding_fixture(
        tmp_path,
        freeze_mutate={"freeze_id": "0" * 64},
    )
    with pytest.raises(ValueError, match="freeze_path freeze_id does not match"):
        _verify_binding_ledger(root, trial)


def test_consumed_evidence_binding_wrong_authorization_id(tmp_path: Path) -> None:
    root, trial = _binding_fixture(
        tmp_path,
        auth_mutate={"authorization_id": "1" * 64},
    )
    with pytest.raises(ValueError, match="authorization_path authorization_id does not match"):
        _verify_binding_ledger(root, trial)


def test_consumed_evidence_binding_receipt_auth_mismatch(tmp_path: Path) -> None:
    root, trial = _binding_fixture(
        tmp_path,
        receipt_mutate={"authorization_id": "2" * 64},
    )
    with pytest.raises(ValueError, match="receipt_path authorization_id does not match"):
        _verify_binding_ledger(root, trial)


def test_consumed_evidence_binding_missing_paths(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="freeze_path"):
        _minimal_trial(
            stage="oos",
            status="no_go",
            oos_consumed=True,
            freeze_id=PORTFOLIO_FREEZE_ID,
            authorization_id=PORTFOLIO_AUTHORIZATION_ID,
            authorization_path=PORTFOLIO_AUTHORIZATION_PATH,
            receipt_path=PORTFOLIO_RECEIPT_PATH,
            oos_reuse_claim="consumed_terminal",
        )
    with pytest.raises(ValidationError, match="authorization_path"):
        _minimal_trial(
            stage="oos",
            status="no_go",
            oos_consumed=True,
            freeze_id=PORTFOLIO_FREEZE_ID,
            authorization_id=PORTFOLIO_AUTHORIZATION_ID,
            freeze_path=PORTFOLIO_FREEZE_PATH,
            receipt_path=PORTFOLIO_RECEIPT_PATH,
            oos_reuse_claim="consumed_terminal",
        )


def test_consumed_evidence_binding_non_json_or_non_object(tmp_path: Path) -> None:
    root, trial = _binding_fixture(tmp_path / "bad-json", freeze_raw="not-json")
    with pytest.raises(ValueError, match="freeze_path is invalid JSON"):
        _verify_binding_ledger(root, trial)

    root2, trial2 = _binding_fixture(tmp_path / "array-json", freeze_raw="[1, 2]\n")
    with pytest.raises(ValueError, match="freeze_path must be a JSON object"):
        _verify_binding_ledger(root2, trial2)


def test_consumed_evidence_binding_one_shot_false(tmp_path: Path) -> None:
    root, trial = _binding_fixture(tmp_path, auth_mutate={"one_shot": False})
    with pytest.raises(ValueError, match="authorization_path one_shot must be true"):
        _verify_binding_ledger(root, trial)

    root2, trial2 = _binding_fixture(
        tmp_path / "receipt-oneshot",
        receipt_mutate={"one_shot": False},
    )
    with pytest.raises(ValueError, match="receipt_path one_shot must be true"):
        _verify_binding_ledger(root2, trial2)


def test_consumed_evidence_binding_ready_flag_true(tmp_path: Path) -> None:
    root, trial = _binding_fixture(
        tmp_path,
        auth_mutate={"ready_for_scoring": True},
    )
    with pytest.raises(ValueError, match="authorization_path ready_for_scoring must be false"):
        _verify_binding_ledger(root, trial)

    root2, trial2 = _binding_fixture(
        tmp_path / "receipt-ready",
        receipt_mutate={"auto_deploy": True},
    )
    with pytest.raises(ValueError, match="receipt_path auto_deploy must be false"):
        _verify_binding_ledger(root2, trial2)


def test_consumed_evidence_binding_happy_path(tmp_path: Path) -> None:
    root, trial = _binding_fixture(tmp_path)
    _verify_binding_ledger(root, trial)


def test_summary_lower_bound_flag() -> None:
    sealed = seal_research_trial_ledger(_ledger([_minimal_trial()], complete=False))
    summary = summarize_research_trial_ledger(sealed)
    assert summary.trial_count_is_lower_bound is True
    assert summary.complete is False


def test_deflated_sharpe_not_evaluable_without_inputs() -> None:
    assessment = assess_deflated_sharpe_ratio(DeflatedSharpeRatioInputs())
    assert assessment.status == "not_evaluable"
    assert assessment.deflated_sharpe is None
    assert assessment.p_value is None
    assert any("unbound" in reason for reason in assessment.reasons)

    still = assess_deflated_sharpe_ratio(
        DeflatedSharpeRatioInputs(
            observed_sharpe=1.0,
            trial_sharpe_stddev=0.5,
            n_return_observations=100,
            return_skewness=0.0,
            return_kurtosis=3.0,
            n_effective_independent_trials=10.0,
        )
    )
    assert still.status == "not_evaluable"
    assert still.deflated_sharpe is None


def test_cli_verify_research_trial_ledger() -> None:
    result = CliRunner().invoke(
        cli_app,
        [
            "verify-research-trial-ledger",
            "--ledger-file",
            str(COMMITTED_LEDGER),
            "--repo-root",
            str(PROJECT_ROOT),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "ledger_id=" in result.stdout
    assert "trial_count=36" in result.stdout
    assert "trial_count_is_lower_bound=true" in result.stdout
    assert "oos_consumed_count=2" in result.stdout
    assert "does_not_score=true" in result.stdout
    assert "does_not_backtest=true" in result.stdout
    assert "does_not_trade=true" in result.stdout
    assert "ready_for_scoring=false" in result.stdout
