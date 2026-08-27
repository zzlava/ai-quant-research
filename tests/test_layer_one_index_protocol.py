from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.research.experiment_ledger import verify_research_trial_ledger
from app.research.layer_one_index_protocol import (
    BOUND_RESEARCH_TRIAL_LEDGER_ID,
    BOUND_RESEARCH_TRIAL_LEDGER_PATH,
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
    CONFIRMED_LOOKBACKS,
    CONFIRMED_NEW_FROZEN_OOS_START,
    DEFAULT_LAYER_ONE_INDEX_PROTOCOL_DRAFT_PATH,
    REQUIRED_PROTOCOL_DECISION_PATHS,
    DateWindow,
    GoNoGoMetricsPending,
    IndexIdentityPending,
    LabeledDateWindow,
    LayerOneIndexDevelopmentProtocolDraft,
    LayerOneIndexDevelopmentProtocolDraftV1,
    LayerOneIndexDevelopmentProtocolV2,
    LookbacksPending,
    NewFrozenOosPlan,
    ProtocolEvidenceBlocker,
    ResearchWindowsPending,
    assert_no_consumed_oos_binding,
    assert_no_window_overlap,
    build_confirmed_layer_one_index_protocol_v2,
    build_unresolved_layer_one_index_protocol_draft,
    collect_protocol_decision_blockers,
    compute_protocol_id,
    default_layer_one_evidence_blockers,
    load_layer_one_index_protocol_draft,
    seal_layer_one_index_protocol_draft,
    verify_layer_one_index_protocol_draft,
    verify_layer_one_index_protocol_draft_file,
    write_layer_one_index_protocol_draft,
)
from app.research.two_layer_contract import load_two_layer_decision_draft, verify_two_layer_decision_draft
from tests.helpers import PROJECT_ROOT

COMMITTED_PROTOCOL = PROJECT_ROOT / DEFAULT_LAYER_ONE_INDEX_PROTOCOL_DRAFT_PATH
COMMITTED_LEDGER = PROJECT_ROOT / BOUND_RESEARCH_TRIAL_LEDGER_PATH
COMMITTED_TWO_LAYER = PROJECT_ROOT / BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
SEALED_V1_FIXTURE = PROJECT_ROOT / "tests/fixtures/research/layer-one-index-development-protocol-draft-v1-sealed.json"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _fully_resolved_v1_protocol() -> LayerOneIndexDevelopmentProtocolDraftV1:
    return cast(
        LayerOneIndexDevelopmentProtocolDraftV1,
        seal_layer_one_index_protocol_draft(
            LayerOneIndexDevelopmentProtocolDraft(
                research_trial_ledger_id=BOUND_RESEARCH_TRIAL_LEDGER_ID,
                research_trial_ledger_path=BOUND_RESEARCH_TRIAL_LEDGER_PATH,
                index=IndexIdentityPending(
                    source="local_index_bars",
                    symbol="000300.SH",
                    return_definition="price_index",
                ),
                windows=ResearchWindowsPending(
                    development=DateWindow(start=date(2015, 1, 5), end=date(2021, 12, 31)),
                    validation_oos=DateWindow(start=date(2022, 1, 4), end=date(2024, 12, 31)),
                ),
                lookbacks=LookbacksPending(
                    trend_lookback_bars=60,
                    volatility_lookback_bars=20,
                    drawdown_lookback_bars=60,
                ),
                annualization_trading_days_per_year=242,
                trend_thresholds={"close_to_sma_ratio_floor": 1.0},
                volatility_target_or_risk_budget_mapping={"mode": "volatility_target", "target": 0.12},
                risk_budget_levels=[0.0, 0.3, 0.6, 1.0],
                rebalance_frequency_phase_policy="monthly_phase0_unconfirmed",
                benchmark="cash",
                cost_assumptions={"commission_bps": 3.0, "slippage_bps": 5.0},
                go_no_go_metrics=GoNoGoMetricsPending(
                    primary_metric="max_drawdown",
                    secondary_metrics=["sharpe_after_costs"],
                    require_per_regime_occupancy=True,
                    require_regime_transition_counts=True,
                    notes="fixture only",
                ),
            )
        ),
    )


def test_committed_protocol_confirmed_not_ready_and_disk_bound() -> None:
    draft, result = verify_layer_one_index_protocol_draft_file(
        protocol_path=COMMITTED_PROTOCOL,
        repo_root=PROJECT_ROOT,
    )
    assert isinstance(draft, LayerOneIndexDevelopmentProtocolV2)
    assert draft.schema_version == "2"
    assert draft.status == "confirmed_for_implementation_but_not_ready"
    assert draft.ready_for_scoring is False
    assert draft.ready_for_backtest is False
    assert draft.ready_for_trading is False
    assert draft.auto_apply is False
    assert result.pending_user_decision_count == 0
    assert result.user_decisions_resolved is True
    assert result.resolved is False
    assert result.research_trial_ledger_binding_ok is True
    assert result.two_layer_decision_contract_binding_ok is True
    assert result.consumed_oos_reuse_check_ok is True
    assert draft.research_trial_ledger_id == BOUND_RESEARCH_TRIAL_LEDGER_ID
    assert draft.two_layer_decision_contract_id == BOUND_TWO_LAYER_DECISION_CONTRACT_ID
    assert draft.protocol_id == compute_protocol_id(draft)
    assert collect_protocol_decision_blockers(draft) == []
    categories = {b.category for b in draft.evidence_blockers}
    assert "pending_factual_source_verification" in categories
    assert "pending_implementation" in categories
    assert "pending_development_evidence" in categories
    assert "future_oos_observation" in categories
    assert "pending_user_decision" not in categories

    # Disk binding must match live sealed contract/ledger — not claim-only constants.
    ledger, _ = verify_research_trial_ledger(ledger_path=COMMITTED_LEDGER, repo_root=PROJECT_ROOT)
    assert ledger.ledger_id == draft.research_trial_ledger_id
    contract = load_two_layer_decision_draft(COMMITTED_TWO_LAYER)
    contract_result = verify_two_layer_decision_draft(contract)
    assert contract_result.schema_version == "2"
    assert contract_result.contract_id == draft.two_layer_decision_contract_id


def test_sealed_v1_fixture_still_verifies() -> None:
    draft, result = verify_layer_one_index_protocol_draft_file(
        protocol_path=SEALED_V1_FIXTURE,
        repo_root=PROJECT_ROOT,
    )
    assert isinstance(draft, LayerOneIndexDevelopmentProtocolDraftV1)
    assert draft.schema_version == "1"
    assert draft.status == "blocked_pending_user_decisions"
    assert result.resolved is False
    assert result.pending_user_decision_count == len(REQUIRED_PROTOCOL_DECISION_PATHS)
    assert result.blockers == list(REQUIRED_PROTOCOL_DECISION_PATHS)
    assert result.two_layer_decision_contract_binding_ok is False


def test_structural_verifier_does_not_claim_disk_bindings() -> None:
    structural = verify_layer_one_index_protocol_draft(build_confirmed_layer_one_index_protocol_v2())
    assert structural.research_trial_ledger_binding_ok is False
    assert structural.two_layer_decision_contract_binding_ok is False
    assert structural.consumed_oos_reuse_check_ok is False
    assert structural.user_decisions_resolved is True
    assert structural.pending_user_decision_count == 0
    assert structural.resolved is False
    assert structural.ready_for_scoring is False

    draft, file_result = verify_layer_one_index_protocol_draft_file(
        protocol_path=COMMITTED_PROTOCOL,
        repo_root=PROJECT_ROOT,
    )
    assert draft.protocol_id == structural.protocol_id
    assert file_result.research_trial_ledger_binding_ok is True
    assert file_result.two_layer_decision_contract_binding_ok is True
    assert file_result.consumed_oos_reuse_check_ok is True
    assert file_result.resolved is False


def test_protocol_hash_stable_and_mismatch_fails() -> None:
    first = build_confirmed_layer_one_index_protocol_v2()
    second = build_confirmed_layer_one_index_protocol_v2()
    assert first.protocol_id == second.protocol_id
    broken = first.model_copy(update={"protocol_id": "0" * 64})
    with pytest.raises(ValueError, match="protocol_id does not match"):
        verify_layer_one_index_protocol_draft(broken)


def test_ready_flags_remain_false_and_wrong_status_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for flag in ("ready_for_scoring", "ready_for_backtest", "ready_for_trading", "auto_apply"):
        bad = dict(payload)
        bad[flag] = True
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerOneIndexDevelopmentProtocolV2.model_validate(bad)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["status"] = "confirmed_for_implementation"
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)


def test_window_overlap_rejected() -> None:
    draft = build_confirmed_layer_one_index_protocol_v2()
    overlapping = draft.model_copy(
        update={
            "windows": draft.windows.model_copy(
                update={
                    "seen_robustness_check_only": LabeledDateWindow(
                        start=date(2021, 6, 1),
                        end=date(2024, 12, 31),
                        role="seen_robustness_check_only",
                    )
                }
            ),
            "protocol_id": None,
        }
    )
    # Threshold/window freeze validators fire before overlap check when sealing via model.
    with pytest.raises(ValidationError):
        LayerOneIndexDevelopmentProtocolV2.model_validate(
            overlapping.model_dump(mode="json", exclude={"protocol_id"})
        )

    v1 = _fully_resolved_v1_protocol().model_copy(
        update={
            "windows": ResearchWindowsPending(
                development=DateWindow(start=date(2020, 1, 1), end=date(2023, 12, 31)),
                validation_oos=DateWindow(start=date(2023, 6, 1), end=date(2024, 12, 31)),
            ),
            "protocol_id": None,
        }
    )
    sealed = seal_layer_one_index_protocol_draft(v1)
    with pytest.raises(ValueError, match="must not overlap"):
        assert_no_window_overlap(sealed)
    with pytest.raises(ValueError, match="must not overlap"):
        verify_layer_one_index_protocol_draft(sealed)


def test_validation_labeled_as_oos_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["windows"]["historical_validation_segments"][0]["role"] = "consumed_oos"
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="historical_validation"):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)

    # Narrative/key masquerade as validation_oos is also rejected on verify.
    draft = build_confirmed_layer_one_index_protocol_v2()
    dumped = draft.model_dump(mode="json", exclude={"protocol_id"})
    dumped["windows"]["note"] = "treat 2013-2016 as validation_oos secretly"
    # Re-seal after note change via model_copy path.
    rebuilt = LayerOneIndexDevelopmentProtocolV2.model_validate(dumped)
    sealed = seal_layer_one_index_protocol_draft(rebuilt)
    with pytest.raises(ValueError, match="must not be labeled or keyed as validation_oos"):
        verify_layer_one_index_protocol_draft(sealed)


def test_consumed_oos_reuse_rejected() -> None:
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
        LayerOneIndexDevelopmentProtocolDraftV1,
        seal_layer_one_index_protocol_draft(overlapping),
    )
    with pytest.raises(ValueError, match="overlaps consumed OOS"):
        assert_no_consumed_oos_binding(overlapping, ledger)

    assert sample.receipt_path is not None
    receipt_bound = _fully_resolved_v1_protocol().model_copy(
        update={
            "cost_assumptions": {"note": sample.receipt_path},
            "protocol_id": None,
        }
    )
    receipt_bound = cast(
        LayerOneIndexDevelopmentProtocolDraftV1,
        seal_layer_one_index_protocol_draft(receipt_bound),
    )
    with pytest.raises(ValueError, match="binds consumed OOS receipt"):
        assert_no_consumed_oos_binding(receipt_bound, ledger)


def test_consumed_oos_reuse_file_verifier(tmp_path: Path) -> None:
    ledger, _summary = verify_research_trial_ledger(
        ledger_path=COMMITTED_LEDGER,
        repo_root=PROJECT_ROOT,
    )
    sample = next(trial for trial in ledger.trials if trial.oos_consumed)
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
    path = tmp_path / "bad-protocol.json"
    write_layer_one_index_protocol_draft(path, overlapping)
    with pytest.raises(ValueError, match="overlaps consumed OOS"):
        verify_layer_one_index_protocol_draft_file(protocol_path=path, repo_root=PROJECT_ROOT)


def test_future_oos_start_drift_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["windows"]["new_frozen_oos"]["start"] = "2026-08-23"
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="2026-08-22"):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)
    assert CONFIRMED_NEW_FROZEN_OOS_START == date(2026, 8, 22)


def test_contract_and_ledger_disk_binding_forgery_rejected(tmp_path: Path) -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["research_trial_ledger_id"] = "a" * 64
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="research_trial_ledger_id"):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["two_layer_decision_contract_id"] = "b" * 64
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="two_layer_decision_contract_id"):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)

    # Disk path: seal a protocol into tmp, then rewrite its bound contract_id field and
    # recompute a fake matching self-hash is impossible without passing model validate.
    # Instead, point file verifier at a temp copy of the protocol while substituting a
    # wrong ledger_id via a rewritten sealed payload that bypasses pydantic by calling
    # verify after manually constructing a structural result path: mutate the on-disk
    # protocol's two_layer_decision_contract_id after write, keeping protocol_id stale so
    # self-hash fails first — and separately assert disk contract id equals binding via
    # a monkeypatched load that returns mismatched id.
    from unittest.mock import patch

    protocol_path = tmp_path / "protocol.json"
    write_layer_one_index_protocol_draft(protocol_path, build_confirmed_layer_one_index_protocol_v2())

    class _FakeContractResult:
        schema_version = "2"
        contract_id = "f" * 64

    with patch(
        "app.research.layer_one_index_protocol.verify_two_layer_decision_draft",
        return_value=_FakeContractResult(),
    ), patch(
        "app.research.layer_one_index_protocol.load_two_layer_decision_draft",
        return_value=object(),
    ):
        with pytest.raises(ValueError, match="contract_id"):
            verify_layer_one_index_protocol_draft_file(
                protocol_path=protocol_path,
                repo_root=PROJECT_ROOT,
            )


def test_threshold_drift_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["lookbacks"] = {**CONFIRMED_LOOKBACKS, "trend_lookback_bars": 199}
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="lookbacks"):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["trend"]["neutral_band_pct"] = 0.05
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["volatility"]["target"] = 0.20
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["hard_gates"]["calmar_min"] = 0.4
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)


def test_resolved_and_ready_false_positives_blocked() -> None:
    draft, result = verify_layer_one_index_protocol_draft_file(
        protocol_path=COMMITTED_PROTOCOL,
        repo_root=PROJECT_ROOT,
    )
    assert result.user_decisions_resolved is True
    assert result.pending_user_decision_count == 0
    assert result.resolved is False  # evidence blockers / not-ready status
    assert len(result.evidence_blockers) > 0
    assert result.ready_for_scoring is False
    assert result.ready_for_backtest is False
    assert result.ready_for_trading is False
    assert result.auto_apply is False
    assert draft.ready_for_scoring is False


def test_flat_stamp_schedule_masquerade_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    # Attempt to claim completion with a flat schedule status.
    payload["cost_assumptions"]["stamp_tax_schedule_status"] = "complete_flat_0_1_pct_since_1900"
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["cost_assumptions"]["stamp_tax"] = "flat_0_001_since_1900"
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)


def test_pending_user_decision_blocker_rejected_on_v2() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["evidence_blockers"] = [
        {
            "path": "trend.lookback",
            "category": "pending_user_decision",
            "detail": "should not remain after confirmation",
        },
        *payload["evidence_blockers"],
    ]
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="pending_user_decision"):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)


def test_missing_required_blocker_categories_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["evidence_blockers"] = [
        b for b in payload["evidence_blockers"] if b["category"] != "future_oos_observation"
    ]
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="missing required categories"):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)


def test_future_oos_not_confused_with_consumed_window() -> None:
    blockers = default_layer_one_evidence_blockers()
    future = next(b for b in blockers if b.category == "future_oos_observation")
    assert "2026-08-22" in future.detail
    assert "2025-01-01" in future.detail
    draft = build_confirmed_layer_one_index_protocol_v2()
    assert draft.windows.consumed_oos.end == date(2026, 8, 21)
    assert draft.windows.new_frozen_oos.start == date(2026, 8, 22)
    assert draft.windows.new_frozen_oos.start > draft.windows.consumed_oos.end


def test_cli_requires_explicit_protocol_file_and_reports_not_ready() -> None:
    runner = CliRunner()
    missing = runner.invoke(cli_app, ["verify-layer-one-index-protocol"])
    assert missing.exit_code != 0

    ok = runner.invoke(
        cli_app,
        [
            "verify-layer-one-index-protocol",
            "--protocol-file",
            str(COMMITTED_PROTOCOL),
            "--repo-root",
            str(PROJECT_ROOT),
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert "schema_version=2" in ok.output
    assert "status=confirmed_for_implementation_but_not_ready" in ok.output
    assert "user_decisions_resolved=true" in ok.output
    assert "pending_user_decision_count=0" in ok.output
    assert "resolved=false" in ok.output
    assert "research_trial_ledger_binding_ok=true" in ok.output
    assert "two_layer_decision_contract_binding_ok=true" in ok.output
    assert "consumed_oos_reuse_check_ok=true" in ok.output
    assert "ready_for_scoring=false" in ok.output
    assert "auto_apply=false" in ok.output
    assert "evidence_blocker=pending_factual_source_verification:" in ok.output
    assert "evidence_blocker=future_oos_observation:" in ok.output


def test_load_factory_matches_committed_draft() -> None:
    loaded = load_layer_one_index_protocol_draft(COMMITTED_PROTOCOL)
    factory = build_confirmed_layer_one_index_protocol_v2()
    assert loaded.protocol_id == factory.protocol_id
    assert loaded.model_dump(exclude={"protocol_id"}) == factory.model_dump(exclude={"protocol_id"})


def test_v1_unresolved_factory_matches_sealed_fixture() -> None:
    loaded = load_layer_one_index_protocol_draft(SEALED_V1_FIXTURE)
    factory = build_unresolved_layer_one_index_protocol_draft()
    assert loaded.protocol_id == factory.protocol_id
    assert isinstance(loaded, LayerOneIndexDevelopmentProtocolDraftV1)


def test_symbol_guessing_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["risk_state_index"]["symbol"] = "000985.CSI"
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="pending factual index symbol must remain null"):
        LayerOneIndexDevelopmentProtocolV2.model_validate(payload)


def test_new_frozen_oos_plan_literal_freeze() -> None:
    with pytest.raises(ValidationError, match="2026-08-22"):
        NewFrozenOosPlan(start=date(2026, 9, 1))


def test_evidence_blocker_paths_cover_required_implementation_items() -> None:
    paths = {b.path for b in default_layer_one_evidence_blockers()}
    assert "regime_budget_engine" in paths
    assert "risk_lock_persistence_and_ui" in paths
    assert "long_history_index_materializer" in paths
    assert "hard_gates.segment_and_combined_results" in paths
    assert "windows.new_frozen_oos" in paths
    # Ensure typed blockers construct.
    assert isinstance(default_layer_one_evidence_blockers()[0], ProtocolEvidenceBlocker)
