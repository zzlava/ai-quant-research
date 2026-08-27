from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.research.experiment_ledger import verify_research_trial_ledger
from app.research.layer_one_index_protocol import (
    load_layer_one_index_protocol_draft,
    verify_layer_one_index_protocol_draft,
)
from app.research.tranche_evaluation_protocol import (
    BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
    BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH,
    BOUND_RESEARCH_TRIAL_LEDGER_ID,
    BOUND_RESEARCH_TRIAL_LEDGER_PATH,
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
    CONFIRMED_CASH_OCCUPANCY_CAUSES,
    CONFIRMED_FACTOR_EVIDENCE_METHODS,
    CONFIRMED_INITIAL_CASH,
    DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH,
    REQUIRED_TRANCHE_EVIDENCE_BLOCKER_PATHS,
    REQUIRED_TRANCHE_EVIDENCE_BLOCKERS,
    REQUIRED_TRANCHE_PROTOCOL_DECISION_PATHS,
    DateWindow,
    ProtocolEvidenceBlocker,
    ResearchWindowsPending,
    TrancheEvaluationProtocolDraft,
    TrancheEvaluationProtocolDraftV1,
    TrancheEvaluationProtocolV2,
    assert_no_consumed_oos_binding,
    assert_no_window_overlap,
    build_confirmed_tranche_evaluation_protocol_v2,
    build_unresolved_tranche_evaluation_protocol_draft,
    collect_protocol_decision_blockers,
    compute_protocol_id,
    compute_tranche_v2_overall_resolved,
    default_tranche_evidence_blockers,
    load_tranche_evaluation_protocol_draft,
    seal_tranche_evaluation_protocol_draft,
    verify_tranche_evaluation_protocol_draft,
    verify_tranche_evaluation_protocol_draft_file,
    write_tranche_evaluation_protocol_draft,
)
from app.research.two_layer_contract import load_two_layer_decision_draft, verify_two_layer_decision_draft
from tests.helpers import PROJECT_ROOT

COMMITTED_PROTOCOL = PROJECT_ROOT / DEFAULT_TRANCHE_EVALUATION_PROTOCOL_DRAFT_PATH
COMMITTED_LEDGER = PROJECT_ROOT / BOUND_RESEARCH_TRIAL_LEDGER_PATH
COMMITTED_TWO_LAYER = PROJECT_ROOT / BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
COMMITTED_LAYER_ONE = PROJECT_ROOT / BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH
SEALED_V1_FIXTURE = PROJECT_ROOT / "tests/fixtures/research/tranche-evaluation-protocol-draft-v1-sealed.json"


def _fully_resolved_v1_protocol() -> TrancheEvaluationProtocolDraftV1:
    return cast(
        TrancheEvaluationProtocolDraftV1,
        seal_tranche_evaluation_protocol_draft(
            TrancheEvaluationProtocolDraft(
                research_trial_ledger_id=BOUND_RESEARCH_TRIAL_LEDGER_ID,
                research_trial_ledger_path=BOUND_RESEARCH_TRIAL_LEDGER_PATH,
                tranche_count=20,
                holding_period_bars=20,
                decision_entry_timing="next_open_unconfirmed",
                exit_timing="holding_period_end_unconfirmed",
                windows=ResearchWindowsPending(
                    development=DateWindow(start=date(2015, 1, 5), end=date(2021, 12, 31)),
                    validation_oos=DateWindow(start=date(2022, 1, 4), end=date(2024, 12, 31)),
                ),
                benchmark="cash",
                costs_minimum_commission_lot_handling="fail_closed_unconfirmed",
                capital_allocation_policy="equal_per_tranche_unconfirmed",
                candidate_availability_policy="require_affordable_lot_unconfirmed",
                go_no_go_metrics="max_drawdown_and_utilization_unconfirmed",
                phase_comparison_policy="full_phase_coverage_diagnostic_only_unconfirmed",
                trial_family_registration="register_before_any_evaluation_unconfirmed",
            )
        ),
    )


def test_committed_protocol_confirmed_not_ready_and_disk_bound() -> None:
    draft, result = verify_tranche_evaluation_protocol_draft_file(
        protocol_path=COMMITTED_PROTOCOL,
        repo_root=PROJECT_ROOT,
    )
    assert isinstance(draft, TrancheEvaluationProtocolV2)
    assert draft.schema_version == "2"
    assert draft.status == "confirmed_for_implementation_but_not_ready"
    assert draft.research_only is True
    assert draft.ready_for_scoring is False
    assert draft.ready_for_backtest is False
    assert draft.ready_for_trading is False
    assert draft.auto_apply is False
    assert draft.pending_user_decisions == []
    assert result.pending_user_decision_count == 0
    assert result.user_decisions_resolved is True
    assert result.resolved is False
    assert result.research_trial_ledger_binding_ok is True
    assert result.two_layer_decision_contract_binding_ok is True
    assert result.layer_one_index_protocol_binding_ok is True
    assert result.consumed_oos_reuse_check_ok is True
    assert draft.confirmed.initial_cash == CONFIRMED_INITIAL_CASH
    assert draft.confirmed.initial_cash_is_blocker is False
    assert "initial_cash" not in result.blockers
    assert draft.research_trial_ledger_id == BOUND_RESEARCH_TRIAL_LEDGER_ID
    assert draft.two_layer_decision_contract_id == BOUND_TWO_LAYER_DECISION_CONTRACT_ID
    assert draft.layer_one_index_protocol_id == BOUND_LAYER_ONE_INDEX_PROTOCOL_ID
    assert draft.protocol_id == compute_protocol_id(draft)
    assert collect_protocol_decision_blockers(draft) == []
    categories = {b.category for b in draft.evidence_blockers}
    assert "pending_factual_source_verification" in categories
    assert "pending_implementation" in categories
    assert "pending_development_evidence" in categories
    assert "future_oos_observation" in categories
    assert "pending_user_decision" not in categories
    paths = {b.path for b in draft.evidence_blockers}
    for required_path, required_category in REQUIRED_TRANCHE_EVIDENCE_BLOCKERS.items():
        assert required_path in paths
        matched = next(b for b in draft.evidence_blockers if b.path == required_path)
        assert matched.category == required_category
    assert draft.evaluation_machine.factor_evidence_methods == list(CONFIRMED_FACTOR_EVIDENCE_METHODS)
    assert draft.evaluation_machine.must_attribute_cash_occupancy_causes == list(CONFIRMED_CASH_OCCUPANCY_CAUSES)

    hold = draft.tranche_hold
    assert hold.holding_period_market_trading_days == 40
    assert hold.holding_cycle_market_trading_days == 40
    assert hold.max_active_tranches_by_budget == {"0.3": 3, "0.6": 6, "0.9": 9}
    assert hold.absolute_max_active_tranches == 9
    assert "tranche_count" not in hold.model_dump()
    assert draft.position_sizing.max_positions_by_budget == {"0.3": 3, "0.6": 6, "0.9": 9}
    assert draft.position_sizing.min_target_notional_cny == 8000

    ledger, _ = verify_research_trial_ledger(ledger_path=COMMITTED_LEDGER, repo_root=PROJECT_ROOT)
    assert ledger.ledger_id == draft.research_trial_ledger_id
    contract = load_two_layer_decision_draft(COMMITTED_TWO_LAYER)
    contract_result = verify_two_layer_decision_draft(contract)
    assert contract_result.schema_version == "2"
    assert contract_result.contract_id == draft.two_layer_decision_contract_id
    layer_one = load_layer_one_index_protocol_draft(COMMITTED_LAYER_ONE)
    layer_one_result = verify_layer_one_index_protocol_draft(layer_one)
    assert layer_one_result.schema_version == "2"
    assert layer_one_result.protocol_id == draft.layer_one_index_protocol_id


def test_sealed_v1_fixture_still_verifies() -> None:
    draft, result = verify_tranche_evaluation_protocol_draft_file(
        protocol_path=SEALED_V1_FIXTURE,
        repo_root=PROJECT_ROOT,
    )
    assert isinstance(draft, TrancheEvaluationProtocolDraftV1)
    assert draft.schema_version == "1"
    assert draft.status == "blocked_pending_user_decisions"
    assert result.resolved is False
    assert result.user_decisions_resolved is False
    assert result.pending_user_decision_count == len(REQUIRED_TRANCHE_PROTOCOL_DECISION_PATHS)
    assert result.blockers == list(REQUIRED_TRANCHE_PROTOCOL_DECISION_PATHS)
    assert result.two_layer_decision_contract_binding_ok is False
    assert result.layer_one_index_protocol_binding_ok is False
    assert result.research_trial_ledger_binding_ok is True
    assert result.consumed_oos_reuse_check_ok is True


def test_structural_verifier_does_not_claim_disk_bindings() -> None:
    structural = verify_tranche_evaluation_protocol_draft(build_confirmed_tranche_evaluation_protocol_v2())
    assert structural.research_trial_ledger_binding_ok is False
    assert structural.two_layer_decision_contract_binding_ok is False
    assert structural.layer_one_index_protocol_binding_ok is False
    assert structural.consumed_oos_reuse_check_ok is False
    assert structural.user_decisions_resolved is True
    assert structural.pending_user_decision_count == 0
    assert structural.resolved is False
    assert structural.ready_for_scoring is False

    draft, file_result = verify_tranche_evaluation_protocol_draft_file(
        protocol_path=COMMITTED_PROTOCOL,
        repo_root=PROJECT_ROOT,
    )
    assert draft.protocol_id == structural.protocol_id
    assert file_result.research_trial_ledger_binding_ok is True
    assert file_result.two_layer_decision_contract_binding_ok is True
    assert file_result.layer_one_index_protocol_binding_ok is True
    assert file_result.consumed_oos_reuse_check_ok is True
    assert file_result.resolved is False


def test_protocol_hash_stable_and_mismatch_fails() -> None:
    first = build_confirmed_tranche_evaluation_protocol_v2()
    second = build_confirmed_tranche_evaluation_protocol_v2()
    assert first.protocol_id == second.protocol_id
    broken = first.model_copy(update={"protocol_id": "0" * 64})
    with pytest.raises(ValueError, match="protocol_id does not match"):
        verify_tranche_evaluation_protocol_draft(broken)


def test_ready_flags_remain_false_and_wrong_status_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for flag in ("ready_for_scoring", "ready_for_backtest", "ready_for_trading", "auto_apply"):
        bad = dict(payload)
        bad[flag] = True
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            TrancheEvaluationProtocolV2.model_validate(bad)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["status"] = "confirmed_for_implementation"
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        TrancheEvaluationProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["research_only"] = False
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        TrancheEvaluationProtocolV2.model_validate(payload)


def test_v1_fully_resolved_still_not_ready() -> None:
    draft = _fully_resolved_v1_protocol()
    result = verify_tranche_evaluation_protocol_draft(draft)
    assert result.resolved is True
    assert result.blockers == []
    assert result.research_trial_ledger_binding_ok is False
    assert result.consumed_oos_reuse_check_ok is False
    assert result.ready_for_scoring is False
    assert result.ready_for_backtest is False
    assert result.ready_for_trading is False
    assert result.auto_apply is False


def test_overlapping_windows_rejected() -> None:
    draft = build_confirmed_tranche_evaluation_protocol_v2()
    overlapping = draft.model_copy(
        update={
            "windows": draft.windows.model_copy(
                update={
                    "seen_robustness_check_only": DateWindow(
                        start=date(2023, 6, 1),
                        end=date(2024, 12, 31),
                    )
                }
            ),
            "protocol_id": None,
        }
    )
    with pytest.raises(ValidationError):
        TrancheEvaluationProtocolV2.model_validate(overlapping.model_dump(mode="json", exclude={"protocol_id"}))

    v1 = _fully_resolved_v1_protocol().model_copy(
        update={
            "windows": ResearchWindowsPending(
                development=DateWindow(start=date(2020, 1, 1), end=date(2023, 12, 31)),
                validation_oos=DateWindow(start=date(2023, 6, 1), end=date(2024, 12, 31)),
            ),
            "protocol_id": None,
        }
    )
    sealed = seal_tranche_evaluation_protocol_draft(v1)
    with pytest.raises(ValueError, match="must not overlap"):
        assert_no_window_overlap(sealed)
    with pytest.raises(ValueError, match="must not overlap"):
        verify_tranche_evaluation_protocol_draft(sealed)


def test_consumed_oos_binding_and_window_reuse_rejected(tmp_path: Path) -> None:
    ledger, _summary = verify_research_trial_ledger(
        ledger_path=COMMITTED_LEDGER,
        repo_root=PROJECT_ROOT,
    )
    consumed = [trial for trial in ledger.trials if trial.oos_consumed]
    assert consumed
    sample = consumed[0]
    assert sample.evaluation_window is not None
    assert sample.evaluation_window.start is not None
    assert sample.evaluation_window.end is not None

    overlapping = _fully_resolved_v1_protocol().model_copy(
        update={
            "windows": ResearchWindowsPending(
                development=DateWindow(start=date(2015, 1, 5), end=date(2021, 12, 31)),
                validation_oos=DateWindow(
                    start=sample.evaluation_window.start,
                    end=sample.evaluation_window.end,
                ),
            ),
            "protocol_id": None,
        }
    )
    overlapping = cast(
        TrancheEvaluationProtocolDraftV1,
        seal_tranche_evaluation_protocol_draft(overlapping),
    )
    with pytest.raises(ValueError, match="overlaps consumed OOS"):
        assert_no_consumed_oos_binding(overlapping, ledger)

    assert sample.receipt_path is not None
    receipt_bound = _fully_resolved_v1_protocol().model_copy(
        update={
            "benchmark": sample.receipt_path,
            "protocol_id": None,
        }
    )
    receipt_bound = cast(
        TrancheEvaluationProtocolDraftV1,
        seal_tranche_evaluation_protocol_draft(receipt_bound),
    )
    with pytest.raises(ValueError, match="binds consumed OOS receipt"):
        assert_no_consumed_oos_binding(receipt_bound, ledger)

    path = tmp_path / "bad-protocol.json"
    write_tranche_evaluation_protocol_draft(path, overlapping)
    with pytest.raises(ValueError, match="overlaps consumed OOS"):
        verify_tranche_evaluation_protocol_draft_file(protocol_path=path, repo_root=PROJECT_ROOT)


def test_v2_seen_windows_cannot_reuse_consumed_oos() -> None:
    ledger, _ = verify_research_trial_ledger(ledger_path=COMMITTED_LEDGER, repo_root=PROJECT_ROOT)
    sample = next(trial for trial in ledger.trials if trial.oos_consumed)
    assert sample.evaluation_window is not None
    assert sample.evaluation_window.start is not None
    assert sample.evaluation_window.end is not None

    draft = build_confirmed_tranche_evaluation_protocol_v2()
    # Force seen_development onto a consumed ledger window via dump mutation after freeze
    # is bypassed: assert_no_consumed_oos_binding must still reject.
    mutated = draft.model_copy(
        update={
            "windows": draft.windows.model_copy(
                update={
                    "seen_development": DateWindow(
                        start=sample.evaluation_window.start,
                        end=sample.evaluation_window.end,
                    )
                }
            ),
            "protocol_id": None,
        }
    )
    # Confirmed window freeze rejects this shape before seal; exercise binding helper
    # by constructing a minimal dump that still carries the overlapping window object.
    with pytest.raises(ValidationError):
        TrancheEvaluationProtocolV2.model_validate(mutated.model_dump(mode="json", exclude={"protocol_id"}))

    # Direct helper path with a hand-built overlapping window object.
    overlapping_helper = draft.model_copy(deep=True)
    object.__setattr__(
        overlapping_helper.windows,
        "seen_development",
        DateWindow(start=sample.evaluation_window.start, end=sample.evaluation_window.end),
    )
    with pytest.raises(ValueError, match="overlaps consumed OOS"):
        assert_no_consumed_oos_binding(overlapping_helper, ledger)


def test_legacy_tranche_count_field_rejected_on_v2(tmp_path: Path) -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["tranche_count"] = 40
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        TrancheEvaluationProtocolV2.model_validate(payload)

    path_tmp = tmp_path / "tranche_count_reject.json"
    path_tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rejects tranche_count"):
        load_tranche_evaluation_protocol_draft(path_tmp)


def test_budget_position_map_3_6_9_drift_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["tranche_hold"]["max_active_tranches_by_budget"] = {
        "0.3": 3,
        "0.6": 6,
        "0.9": 8,
    }
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        TrancheEvaluationProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["position_sizing"]["max_positions_by_budget"] = {
        "0.3": 4,
        "0.6": 6,
        "0.9": 9,
    }
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        TrancheEvaluationProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["position_sizing"]["absolute_max_positions"] = 10
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        TrancheEvaluationProtocolV2.model_validate(payload)


def test_upstream_and_ledger_disk_binding_forgery_rejected(tmp_path: Path) -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["research_trial_ledger_id"] = "a" * 64
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="research_trial_ledger_id"):
        TrancheEvaluationProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["two_layer_decision_contract_id"] = "b" * 64
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="two_layer_decision_contract_id"):
        TrancheEvaluationProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["layer_one_index_protocol_id"] = "c" * 64
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="layer_one_index_protocol_id"):
        TrancheEvaluationProtocolV2.model_validate(payload)

    protocol_path = tmp_path / "protocol.json"
    write_tranche_evaluation_protocol_draft(protocol_path, build_confirmed_tranche_evaluation_protocol_v2())

    class _FakeContractResult:
        schema_version = "2"
        contract_id = "f" * 64

    with (
        patch(
            "app.research.tranche_evaluation_protocol.verify_two_layer_decision_draft",
            return_value=_FakeContractResult(),
        ),
        patch(
            "app.research.tranche_evaluation_protocol.load_two_layer_decision_draft",
            return_value=object(),
        ),
    ):
        with pytest.raises(ValueError, match="contract_id"):
            verify_tranche_evaluation_protocol_draft_file(
                protocol_path=protocol_path,
                repo_root=PROJECT_ROOT,
            )

    class _FakeLayerOneResult:
        schema_version = "2"
        protocol_id = "e" * 64

    with (
        patch(
            "app.research.tranche_evaluation_protocol.verify_layer_one_index_protocol_draft",
            return_value=_FakeLayerOneResult(),
        ),
        patch(
            "app.research.tranche_evaluation_protocol.load_layer_one_index_protocol_draft",
            return_value=object(),
        ),
    ):
        with pytest.raises(ValueError, match="protocol_id"):
            verify_tranche_evaluation_protocol_draft_file(
                protocol_path=protocol_path,
                repo_root=PROJECT_ROOT,
            )


def test_resolved_false_while_evidence_blockers_exist() -> None:
    draft, result = verify_tranche_evaluation_protocol_draft_file(
        protocol_path=COMMITTED_PROTOCOL,
        repo_root=PROJECT_ROOT,
    )
    assert isinstance(draft, TrancheEvaluationProtocolV2)
    assert result.user_decisions_resolved is True
    assert result.pending_user_decision_count == 0
    assert draft.pending_user_decisions == []
    assert len(result.evidence_blockers) > 0
    assert result.resolved is False
    assert result.ready_for_scoring is False
    assert result.ready_for_backtest is False
    assert result.ready_for_trading is False

    blocker = ProtocolEvidenceBlocker(
        path="benchmark.csi_all_share_total_return_symbol",
        category="pending_factual_source_verification",
        detail="still pending",
    )
    assert (
        compute_tranche_v2_overall_resolved(
            evidence_blockers=[blocker],
            status="confirmed_for_implementation_but_not_ready",
            ready_for_scoring=False,
            ready_for_backtest=False,
            ready_for_trading=False,
        )
        is False
    )
    assert (
        compute_tranche_v2_overall_resolved(
            evidence_blockers=[],
            status="confirmed_for_implementation_but_not_ready",
            ready_for_scoring=False,
            ready_for_backtest=False,
            ready_for_trading=False,
        )
        is False
    )
    assert (
        compute_tranche_v2_overall_resolved(
            evidence_blockers=[],
            status="ready",
            ready_for_scoring=True,
            ready_for_backtest=True,
            ready_for_trading=True,
        )
        is True
    )


def test_pending_user_decision_blocker_and_nonempty_pending_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["evidence_blockers"] = [
        {
            "path": "tranche_hold.holding_period",
            "category": "pending_user_decision",
            "detail": "should not remain after confirmation",
        },
        *payload["evidence_blockers"],
    ]
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="pending_user_decision"):
        TrancheEvaluationProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["pending_user_decisions"] = ["tranche_hold.holding_period_market_trading_days"]
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="pending_user_decisions"):
        TrancheEvaluationProtocolV2.model_validate(payload)


def test_missing_required_blocker_categories_and_paths_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["evidence_blockers"] = [
        b for b in payload["evidence_blockers"] if b["category"] != "future_oos_observation"
    ]
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="missing required categories"):
        TrancheEvaluationProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["evidence_blockers"] = [
        b for b in payload["evidence_blockers"] if b["path"] != "execution_cash_attribution"
    ]
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="missing required paths"):
        TrancheEvaluationProtocolV2.model_validate(payload)


def test_required_blocker_path_category_swap_and_duplicate_rejected() -> None:
    """Former mutation: reclassify hard path + keep category set via another blocker."""
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for blocker in payload["evidence_blockers"]:
        if blocker["path"] == "benchmark.csi_all_share_total_return_symbol":
            blocker["category"] = "future_enhancement"
        elif blocker["path"] == "ownership_proxy_scoring_integration":
            # Keep overall category set non-empty for factual if someone only checked sets.
            blocker["category"] = "pending_factual_source_verification"
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="path->category mismatch"):
        TrancheEvaluationProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["evidence_blockers"] = [
        *payload["evidence_blockers"],
        {
            "path": "execution_cash_attribution",
            "category": "pending_implementation",
            "detail": "duplicate path must fail",
        },
    ]
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="duplicate path"):
        TrancheEvaluationProtocolV2.model_validate(payload)


def test_evaluation_machine_confirmed_sequences_frozen() -> None:
    """Former mutation: rewrite methods/causes lists then reseal must fail."""
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["evaluation_machine"]["factor_evidence_methods"] = [
        "ic_icir_hac_overlap_corrected",
        "full_cross_section_quantile_portfolios",
    ]
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="factor_evidence_methods"):
        TrancheEvaluationProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["evaluation_machine"]["factor_evidence_methods"] = [
        *CONFIRMED_FACTOR_EVIDENCE_METHODS,
        "made_up_method",
    ]
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="factor_evidence_methods"):
        TrancheEvaluationProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["evaluation_machine"]["must_attribute_cash_occupancy_causes"] = [
        c for c in CONFIRMED_CASH_OCCUPANCY_CAUSES if c != "risk_budget"
    ]
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="must_attribute_cash_occupancy_causes"):
        TrancheEvaluationProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["evaluation_machine"]["must_attribute_cash_occupancy_causes"] = [
        *CONFIRMED_CASH_OCCUPANCY_CAUSES,
        "extra_cause",
    ]
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="must_attribute_cash_occupancy_causes"):
        TrancheEvaluationProtocolV2.model_validate(payload)


def test_future_oos_not_confused_with_consumed_window() -> None:
    blockers = default_tranche_evidence_blockers()
    future = next(b for b in blockers if b.category == "future_oos_observation")
    assert "2026-08-22" in future.detail
    assert "2025-01-01" in future.detail
    draft = build_confirmed_tranche_evaluation_protocol_v2()
    assert draft.windows.consumed_oos.end == date(2026, 8, 21)
    assert draft.windows.new_frozen_oos.start == date(2026, 8, 22)
    assert draft.windows.new_frozen_oos.start > draft.windows.consumed_oos.end


def test_benchmark_symbol_guessing_and_flat_stamp_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["benchmark"]["symbol"] = "000985.CSI"
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        TrancheEvaluationProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["cost_assumptions"]["stamp_tax_schedule_status"] = "complete_flat_0_1_pct_since_1900"
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        TrancheEvaluationProtocolV2.model_validate(payload)


def test_no_readiness_or_performance_claim_fields() -> None:
    payload: dict[str, Any] = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    blob = json.dumps(payload)
    for forbidden in ("sharpe", "pnl", "winner", "best_n", "ready_for_live"):
        assert forbidden not in blob
    assert payload["ready_for_scoring"] is False
    assert payload["ready_for_backtest"] is False
    assert payload["ready_for_trading"] is False
    assert payload["auto_apply"] is False
    assert payload["research_only"] is True
    assert payload["pending_user_decisions"] == []


def test_cli_requires_explicit_protocol_file_and_reports_not_ready() -> None:
    runner = CliRunner()
    missing = runner.invoke(cli_app, ["verify-tranche-evaluation-protocol"])
    assert missing.exit_code != 0

    ok = runner.invoke(
        cli_app,
        [
            "verify-tranche-evaluation-protocol",
            "--protocol-file",
            str(COMMITTED_PROTOCOL),
            "--repo-root",
            str(PROJECT_ROOT),
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert "schema_version=2" in ok.output
    assert "status=confirmed_for_implementation_but_not_ready" in ok.output
    assert "structural_ok=true" in ok.output
    assert "user_decisions_resolved=true" in ok.output
    assert "pending_user_decision_count=0" in ok.output
    assert "resolved=false" in ok.output
    assert "research_trial_ledger_binding_ok=true" in ok.output
    assert "two_layer_decision_contract_binding_ok=true" in ok.output
    assert "layer_one_index_protocol_binding_ok=true" in ok.output
    assert "consumed_oos_reuse_check_ok=true" in ok.output
    assert "initial_cash_is_blocker=false" in ok.output
    assert "ready_for_scoring=false" in ok.output
    assert "auto_apply=false" in ok.output
    assert "evidence_blocker=pending_factual_source_verification:" in ok.output
    assert "evidence_blocker=pending_implementation:" in ok.output
    assert "evidence_blocker=pending_development_evidence:" in ok.output
    assert "evidence_blocker=future_oos_observation:" in ok.output


def test_load_factory_matches_committed_draft() -> None:
    loaded = load_tranche_evaluation_protocol_draft(COMMITTED_PROTOCOL)
    factory = build_confirmed_tranche_evaluation_protocol_v2()
    assert loaded.protocol_id == factory.protocol_id
    assert loaded.protocol_id == compute_protocol_id(loaded)
    assert loaded.model_dump(exclude={"protocol_id"}) == factory.model_dump(exclude={"protocol_id"})


def test_v1_unresolved_factory_matches_sealed_fixture() -> None:
    loaded = load_tranche_evaluation_protocol_draft(SEALED_V1_FIXTURE)
    factory = build_unresolved_tranche_evaluation_protocol_draft()
    assert loaded.protocol_id == factory.protocol_id
    assert isinstance(loaded, TrancheEvaluationProtocolDraftV1)


def test_evidence_blocker_paths_cover_required_items() -> None:
    paths = {b.path for b in default_tranche_evidence_blockers()}
    for required_path, required_category in REQUIRED_TRANCHE_EVIDENCE_BLOCKERS.items():
        assert required_path in paths
        matched = next(b for b in default_tranche_evidence_blockers() if b.path == required_path)
        assert matched.category == required_category
    assert tuple(REQUIRED_TRANCHE_EVIDENCE_BLOCKER_PATHS) == tuple(REQUIRED_TRANCHE_EVIDENCE_BLOCKERS)
    assert isinstance(default_tranche_evidence_blockers()[0], ProtocolEvidenceBlocker)
    assert "execution_cash_attribution" in paths
    assert "tranche_evaluation_runner" in paths
