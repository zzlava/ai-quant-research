from __future__ import annotations

import ast
import json
import math
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.research.experiment_ledger import verify_research_trial_ledger
from app.research.layer_two_allocation_protocol import (
    load_layer_two_allocation_protocol,
    verify_layer_two_allocation_protocol,
)
from app.research.layer_two_alpha_development_protocol import (
    BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_ID,
    BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_PATH,
    BOUND_RESEARCH_TRIAL_LEDGER_ID,
    BOUND_RESEARCH_TRIAL_LEDGER_PATH,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
    CONFIRMED_FACTOR_FAMILIES,
    CONFIRMED_SIZE_BANDS,
    DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH,
    HOLM_TIE_BREAK_FACTOR_FAMILY_ORDER,
    REQUIRED_ALPHA_DEVELOPMENT_EVIDENCE_BLOCKERS,
    REQUIRED_INFERENCE_REPORT_FIELDS,
    ClusterCompanionPolicy,
    CrossSectionRankingPolicy,
    DefensiveLowVolFactorFormula,
    EligibilityDenominatorPolicy,
    ForwardLabelAndPoolingPolicy,
    HolmStepDownExactAlgorithm,
    InferencePolicy,
    LabelsAndEvidencePolicy,
    LayerTwoAlphaDevelopmentProtocolV1,
    LayerTwoAlphaDevelopmentProtocolVerificationResult,
    MediumMomentumFactorFormula,
    NeweyWestBartlettExactAlgorithm,
    PitSnapshotBindingPolicy,
    QuintileSemanticsPolicy,
    SizeBand,
    SizeBandDiagnosticSafeguards,
    SpearmanIcSemanticsPolicy,
    assert_matches_sealed_factory_canonical,
    assert_windows_non_overlapping,
    build_confirmed_layer_two_alpha_development_protocol_v1,
    compute_protocol_id,
    default_alpha_research_windows,
    load_layer_two_alpha_development_protocol,
    seal_layer_two_alpha_development_protocol,
    verify_layer_two_alpha_development_protocol,
    verify_layer_two_alpha_development_protocol_file,
    write_layer_two_alpha_development_protocol,
)
from app.research.tranche_evaluation_protocol import (
    load_tranche_evaluation_protocol_draft,
    verify_tranche_evaluation_protocol_draft,
)
from app.research.two_layer_contract import load_two_layer_decision_draft, verify_two_layer_decision_draft
from tests.helpers import PROJECT_ROOT

COMMITTED_PROTOCOL = PROJECT_ROOT / DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH
COMMITTED_LEDGER = PROJECT_ROOT / BOUND_RESEARCH_TRIAL_LEDGER_PATH
COMMITTED_TWO_LAYER = PROJECT_ROOT / BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
COMMITTED_TRANCHE = PROJECT_ROOT / BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
COMMITTED_ALLOCATION = PROJECT_ROOT / BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_PATH
MODULE_PATH = PROJECT_ROOT / "src/app/research/layer_two_alpha_development_protocol.py"


def test_committed_protocol_confirmed_not_ready_and_disk_bound() -> None:
    draft, result = verify_layer_two_alpha_development_protocol_file(
        protocol_path=COMMITTED_PROTOCOL,
        repo_root=PROJECT_ROOT,
    )
    assert draft.schema_version == "1"
    assert draft.protocol_version == "layer-two-alpha-development-protocol-v1"
    assert draft.status == "confirmed_for_development_but_not_ready"
    assert draft.confirmation_as_of.isoformat() == "2026-08-26"
    assert draft.readiness.research_only is True
    assert draft.readiness.ready_for_scoring is False
    assert draft.readiness.ready_for_backtest is False
    assert draft.readiness.ready_for_portfolio_construction is False
    assert draft.readiness.ready_for_data is False
    assert draft.readiness.ready_for_orders is False
    assert draft.readiness.ready_for_trading is False
    assert draft.readiness.auto_apply is False
    assert draft.readiness.does_not_run_data is True
    assert draft.readiness.does_not_score is True
    assert draft.readiness.does_not_wire_scoring is True
    assert draft.readiness.does_not_generate_strategy_config is True
    assert draft.pending_user_decisions == []
    assert result.user_decisions_resolved is True
    assert result.pending_user_decision_count == 0
    assert result.resolved is False
    assert result.research_trial_ledger_binding_ok is True
    assert result.two_layer_decision_contract_binding_ok is True
    assert result.tranche_evaluation_protocol_binding_ok is True
    assert result.layer_two_allocation_protocol_binding_ok is True
    assert [entry.family_id for entry in draft.factor_families] == list(CONFIRMED_FACTOR_FAMILIES)
    assert draft.windows.development.start.isoformat() == "2022-01-01"
    assert draft.windows.development.end.isoformat() == "2023-12-31"
    assert draft.windows.seen_robustness.start.isoformat() == "2024-01-01"
    assert draft.windows.seen_robustness.end.isoformat() == "2024-12-31"
    assert draft.windows.consumed_oos.start.isoformat() == "2025-01-01"
    assert draft.windows.consumed_oos.end.isoformat() == "2026-08-21"
    assert draft.windows.new_frozen_oos_begins.isoformat() == "2026-08-22"
    assert draft.labels_and_evidence.inference.primary_hac_lag == 39
    assert draft.labels_and_evidence.inference.holm_hypothesis_count == 4
    assert draft.ledger_registration.e11a_does_not_modify_ledger is True
    assert draft.protocol_id == compute_protocol_id(draft)

    ledger, _ = verify_research_trial_ledger(ledger_path=COMMITTED_LEDGER, repo_root=PROJECT_ROOT)
    assert ledger.ledger_id == draft.research_trial_ledger_id
    contract = load_two_layer_decision_draft(COMMITTED_TWO_LAYER)
    assert verify_two_layer_decision_draft(contract).contract_id == draft.two_layer_decision_contract_id
    tranche = load_tranche_evaluation_protocol_draft(COMMITTED_TRANCHE)
    assert verify_tranche_evaluation_protocol_draft(tranche).protocol_id == draft.tranche_evaluation_protocol_id
    allocation = load_layer_two_allocation_protocol(COMMITTED_ALLOCATION)
    assert verify_layer_two_allocation_protocol(allocation).protocol_id == draft.layer_two_allocation_protocol_id


def test_structural_verifier_does_not_claim_disk_bindings() -> None:
    structural = verify_layer_two_alpha_development_protocol(build_confirmed_layer_two_alpha_development_protocol_v1())
    assert structural.research_trial_ledger_binding_ok is False
    assert structural.two_layer_decision_contract_binding_ok is False
    assert structural.tranche_evaluation_protocol_binding_ok is False
    assert structural.layer_two_allocation_protocol_binding_ok is False
    assert structural.resolved is False
    assert structural.user_decisions_resolved is True

    _, file_result = verify_layer_two_alpha_development_protocol_file(
        protocol_path=COMMITTED_PROTOCOL,
        repo_root=PROJECT_ROOT,
    )
    assert file_result.research_trial_ledger_binding_ok is True
    assert file_result.two_layer_decision_contract_binding_ok is True
    assert file_result.tranche_evaluation_protocol_binding_ok is True
    assert file_result.layer_two_allocation_protocol_binding_ok is True


def test_protocol_hash_stable_and_mismatch_fails() -> None:
    first = build_confirmed_layer_two_alpha_development_protocol_v1()
    second = build_confirmed_layer_two_alpha_development_protocol_v1()
    assert first.protocol_id == second.protocol_id
    broken = first.model_copy(update={"protocol_id": "0" * 64})
    with pytest.raises(ValueError, match="protocol_id does not match"):
        verify_layer_two_alpha_development_protocol(broken)


def test_upstream_id_path_and_disk_content_drift_rejected(tmp_path: Path) -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for field, value in (
        ("research_trial_ledger_id", "a" * 64),
        ("two_layer_decision_contract_id", "b" * 64),
        ("tranche_evaluation_protocol_id", "c" * 64),
        ("layer_two_allocation_protocol_id", "d" * 64),
    ):
        bad = dict(payload)
        bad[field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    for field, value in (
        ("research_trial_ledger_path", "config/research/other-ledger.json"),
        ("two_layer_decision_contract_path", "config/research/other-contract.json"),
        ("tranche_evaluation_protocol_path", "config/research/other-tranche.json"),
        ("layer_two_allocation_protocol_path", "config/research/other-allocation.json"),
    ):
        bad = dict(payload)
        bad[field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    protocol_path = tmp_path / "protocol.json"
    write_layer_two_alpha_development_protocol(protocol_path, build_confirmed_layer_two_alpha_development_protocol_v1())

    class _FakeLedger:
        ledger_id = "f" * 64

    with patch(
        "app.research.layer_two_alpha_development_protocol.verify_research_trial_ledger",
        return_value=(_FakeLedger(), object()),
    ):
        with pytest.raises(ValueError, match="ledger_id"):
            verify_layer_two_alpha_development_protocol_file(protocol_path=protocol_path, repo_root=PROJECT_ROOT)

    class _FakeContractResult:
        schema_version = "2"
        contract_id = "e" * 64

    with (
        patch(
            "app.research.layer_two_alpha_development_protocol.verify_two_layer_decision_draft",
            return_value=_FakeContractResult(),
        ),
        patch(
            "app.research.layer_two_alpha_development_protocol.load_two_layer_decision_draft",
            return_value=object(),
        ),
        patch(
            "app.research.layer_two_alpha_development_protocol.verify_research_trial_ledger",
            return_value=(type("L", (), {"ledger_id": BOUND_RESEARCH_TRIAL_LEDGER_ID})(), object()),
        ),
    ):
        with pytest.raises(ValueError, match="contract_id"):
            verify_layer_two_alpha_development_protocol_file(protocol_path=protocol_path, repo_root=PROJECT_ROOT)

    class _FakeTrancheResult:
        schema_version = "2"
        protocol_id = "1" * 64

    with (
        patch(
            "app.research.layer_two_alpha_development_protocol.verify_tranche_evaluation_protocol_draft",
            return_value=_FakeTrancheResult(),
        ),
        patch(
            "app.research.layer_two_alpha_development_protocol.load_tranche_evaluation_protocol_draft",
            return_value=object(),
        ),
        patch(
            "app.research.layer_two_alpha_development_protocol.verify_research_trial_ledger",
            return_value=(type("L", (), {"ledger_id": BOUND_RESEARCH_TRIAL_LEDGER_ID})(), object()),
        ),
        patch(
            "app.research.layer_two_alpha_development_protocol.verify_two_layer_decision_draft",
            return_value=type("C", (), {"schema_version": "2", "contract_id": BOUND_TWO_LAYER_DECISION_CONTRACT_ID})(),
        ),
        patch(
            "app.research.layer_two_alpha_development_protocol.load_two_layer_decision_draft",
            return_value=object(),
        ),
    ):
        with pytest.raises(ValueError, match="protocol_id"):
            verify_layer_two_alpha_development_protocol_file(protocol_path=protocol_path, repo_root=PROJECT_ROOT)

    class _FakeAllocationResult:
        schema_version = "1"
        protocol_id = "2" * 64

    with (
        patch(
            "app.research.layer_two_alpha_development_protocol.verify_layer_two_allocation_protocol",
            return_value=_FakeAllocationResult(),
        ),
        patch(
            "app.research.layer_two_alpha_development_protocol.load_layer_two_allocation_protocol",
            return_value=object(),
        ),
        patch(
            "app.research.layer_two_alpha_development_protocol.verify_research_trial_ledger",
            return_value=(type("L", (), {"ledger_id": BOUND_RESEARCH_TRIAL_LEDGER_ID})(), object()),
        ),
        patch(
            "app.research.layer_two_alpha_development_protocol.verify_two_layer_decision_draft",
            return_value=type("C", (), {"schema_version": "2", "contract_id": BOUND_TWO_LAYER_DECISION_CONTRACT_ID})(),
        ),
        patch(
            "app.research.layer_two_alpha_development_protocol.load_two_layer_decision_draft",
            return_value=object(),
        ),
        patch(
            "app.research.layer_two_alpha_development_protocol.verify_tranche_evaluation_protocol_draft",
            return_value=type("T", (), {"schema_version": "2", "protocol_id": BOUND_TRANCHE_EVALUATION_PROTOCOL_ID})(),
        ),
        patch(
            "app.research.layer_two_alpha_development_protocol.load_tranche_evaluation_protocol_draft",
            return_value=object(),
        ),
    ):
        with pytest.raises(ValueError, match="protocol_id"):
            verify_layer_two_alpha_development_protocol_file(protocol_path=protocol_path, repo_root=PROJECT_ROOT)


def test_formula_window_horizon_holm_coverage_weight_tamper_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))

    bad = json.loads(json.dumps(payload))
    bad["factor_families"][0]["quality"]["formula"] = "attacker rewrote quality formula"
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["factor_families"][2]["medium_momentum_12_1"]["require_ordered_positive_finite_market_bars"] = 240
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["windows"]["development"]["end"] = "2024-06-30"
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["labels_and_evidence"]["inference"]["primary_hac_lag"] = 20
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["labels_and_evidence"]["inference"]["holm_hypothesis_count"] = 3
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["coverage_gates"]["min_factor_known_cs_fraction_of_eligible"] = 0.50
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["weighting"]["qualifying_factors_equal_weight_summing_to_one"] = False
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    reordered = json.loads(json.dumps(payload))
    families = list(reordered["factor_families"])
    reordered["factor_families"] = [families[1], families[0], families[2], families[3]]
    reordered.pop("protocol_id", None)
    with pytest.raises(ValidationError, match="order"):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(reordered)


def test_consumed_oos_overlap_with_development_rejected(tmp_path: Path) -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["windows"]["consumed_oos"]["start"] = "2023-01-01"
    payload.pop("protocol_id", None)
    overlap_path = tmp_path / "overlap.json"
    overlap_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="overlap|does not match sealed factory"):
        load_layer_two_alpha_development_protocol(overlap_path)

    windows = default_alpha_research_windows()
    windows = windows.model_copy(
        update={"consumed_oos": windows.consumed_oos.model_copy(update={"start": windows.development.start})}
    )
    with pytest.raises(ValueError, match="overlap"):
        assert_windows_non_overlapping(windows)


def test_path_escape_and_mixed_repo_root_rejected(tmp_path: Path) -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for bad_path in (
        "../secrets/ledger.json",
        "/etc/passwd",
        "config/research/other-ledger.json",
    ):
        bad = dict(payload)
        bad["research_trial_ledger_path"] = bad_path
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    protocol_path = tmp_path / "protocol.json"
    write_layer_two_alpha_development_protocol(protocol_path, build_confirmed_layer_two_alpha_development_protocol_v1())
    with pytest.raises(ValueError, match="does not exist|escapes"):
        verify_layer_two_alpha_development_protocol_file(protocol_path=protocol_path, repo_root=tmp_path)


def test_bool_nan_inf_extra_fields_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    bad = dict(payload)
    bad["readiness"]["ready_for_scoring"] = True
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = dict(payload)
    bad["coverage_gates"]["min_factor_known_cs_fraction_of_eligible"] = True
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    with pytest.raises(ValidationError):
        InferencePolicy(holm_family_wise_alpha=float("nan"))

    with pytest.raises(ValidationError):
        InferencePolicy(holm_family_wise_alpha=float("inf"))

    bad = dict(payload)
    bad["attacker_extra_field"] = True
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)


def test_ready_flag_injection_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for flag in (
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_portfolio_construction",
        "ready_for_data",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
    ):
        bad = json.loads(json.dumps(payload))
        bad["readiness"][flag] = True
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = dict(payload)
    bad["status"] = "ready_for_development"
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)


def test_outer_reseal_cannot_mask_semantic_drift(tmp_path: Path) -> None:
    factory = build_confirmed_layer_two_alpha_development_protocol_v1()
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))

    note_drift = dict(payload)
    note_drift["ranking"] = dict(payload["ranking"])
    note_drift["ranking"]["note"] = "allow zero fill after rank"
    note_drift.pop("protocol_id", None)
    note_resealed = seal_layer_two_alpha_development_protocol(
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(note_drift)
    )
    assert note_resealed.protocol_id != factory.protocol_id
    with pytest.raises(ValueError, match="does not match sealed factory"):
        verify_layer_two_alpha_development_protocol(note_resealed)

    blocker_drift = dict(payload)
    blockers = [dict(b) for b in payload["evidence_blockers"]]
    blockers[0]["detail"] = "attacker rewrote blocker detail after outer reseal"
    blocker_drift["evidence_blockers"] = blockers
    blocker_drift.pop("protocol_id", None)
    blocker_resealed = seal_layer_two_alpha_development_protocol(
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(blocker_drift)
    )
    with pytest.raises(ValueError, match="does not match sealed factory"):
        verify_layer_two_alpha_development_protocol(blocker_resealed)


def test_verification_result_partial_bindings_forbidden() -> None:
    pid = "ab" * 32
    LayerTwoAlphaDevelopmentProtocolVerificationResult(
        protocol_id=pid,
        protocol_version="layer-two-alpha-development-protocol-v1",
        status="confirmed_for_development_but_not_ready",
        structural_ok=True,
        research_trial_ledger_id=BOUND_RESEARCH_TRIAL_LEDGER_ID,
        research_trial_ledger_path=BOUND_RESEARCH_TRIAL_LEDGER_PATH,
        research_trial_ledger_binding_ok=False,
        two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
        two_layer_decision_contract_path=BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
        two_layer_decision_contract_binding_ok=False,
        tranche_evaluation_protocol_id=BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
        tranche_evaluation_protocol_path=BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
        tranche_evaluation_protocol_binding_ok=False,
        layer_two_allocation_protocol_id=BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_ID,
        layer_two_allocation_protocol_path=BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_PATH,
        layer_two_allocation_protocol_binding_ok=False,
        resolved=False,
        user_decisions_resolved=True,
        pending_user_decision_count=0,
        blockers=[],
    )
    with pytest.raises(ValidationError, match="partial bindings"):
        LayerTwoAlphaDevelopmentProtocolVerificationResult(
            protocol_id=pid,
            protocol_version="layer-two-alpha-development-protocol-v1",
            status="confirmed_for_development_but_not_ready",
            structural_ok=True,
            research_trial_ledger_id=BOUND_RESEARCH_TRIAL_LEDGER_ID,
            research_trial_ledger_path=BOUND_RESEARCH_TRIAL_LEDGER_PATH,
            research_trial_ledger_binding_ok=True,
            two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            two_layer_decision_contract_path=BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
            two_layer_decision_contract_binding_ok=False,
            tranche_evaluation_protocol_id=BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
            tranche_evaluation_protocol_path=BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
            tranche_evaluation_protocol_binding_ok=False,
            layer_two_allocation_protocol_id=BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_ID,
            layer_two_allocation_protocol_path=BOUND_LAYER_TWO_ALLOCATION_PROTOCOL_PATH,
            layer_two_allocation_protocol_binding_ok=False,
            resolved=False,
            user_decisions_resolved=True,
            pending_user_decision_count=0,
            blockers=[],
        )


def test_file_roundtrip_and_factory_matches_committed(tmp_path: Path) -> None:
    factory = build_confirmed_layer_two_alpha_development_protocol_v1()
    path = tmp_path / "alpha-protocol.json"
    write_layer_two_alpha_development_protocol(path, factory)
    loaded = load_layer_two_alpha_development_protocol(path)
    assert loaded.protocol_id == factory.protocol_id
    assert loaded.model_dump(exclude={"protocol_id"}) == factory.model_dump(exclude={"protocol_id"})

    committed = load_layer_two_alpha_development_protocol(COMMITTED_PROTOCOL)
    assert committed.protocol_id == factory.protocol_id
    assert committed.model_dump(exclude={"protocol_id"}) == factory.model_dump(exclude={"protocol_id"})


def test_required_evidence_blockers_present() -> None:
    draft = build_confirmed_layer_two_alpha_development_protocol_v1()
    paths = {b.path: b.category for b in draft.evidence_blockers}
    for path, category in REQUIRED_ALPHA_DEVELOPMENT_EVIDENCE_BLOCKERS.items():
        assert paths[path] == category


def test_forward_label_and_pooling_semantics_frozen() -> None:
    default = ForwardLabelAndPoolingPolicy()
    assert default.same_window_endpoint_required is True
    assert default.never_shorten_horizon is True
    assert default.exact_label_endpoint == "market_calendar_observation_t_plus_h_for_same_symbol"
    assert default.missing_or_unverified_endpoint_is_unknown is True
    assert default.horizon_never_shifts is True
    assert default.calendar_every_eligible_market_trading_day_not_tranche_phases is True
    assert default.pool_arithmetic_mean_of_per_decision_day_observations is True
    assert default.never_pool_at_name_row_level is True
    assert default.development_labels_must_not_cross_2023_12_31 is True
    assert default.robustness_2024_labels_must_not_cross_2024_12_31 is True
    assert default.robustness_2024_must_never_read_consumed_oos is True
    assert default.missing_exact_endpoint_is_unknown is True

    frozen_fields = (
        "same_window_endpoint_required",
        "never_shorten_horizon",
        "missing_or_unverified_endpoint_is_unknown",
        "horizon_never_shifts",
        "calendar_every_eligible_market_trading_day_not_tranche_phases",
        "pool_arithmetic_mean_of_per_decision_day_observations",
        "never_pool_at_name_row_level",
        "development_labels_must_not_cross_2023_12_31",
        "robustness_2024_labels_must_not_cross_2024_12_31",
        "robustness_2024_must_never_read_consumed_oos",
        "missing_exact_endpoint_is_unknown",
    )
    for field in frozen_fields:
        with pytest.raises(ValidationError):
            ForwardLabelAndPoolingPolicy(**{field: False})

    with pytest.raises(ValidationError):
        ForwardLabelAndPoolingPolicy(exact_label_endpoint="next_available_bar_after_t_plus_h")

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for field in frozen_fields:
        bad = json.loads(json.dumps(payload))
        bad["labels_and_evidence"]["forward_label_and_pooling"][field] = False
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["labels_and_evidence"]["forward_label_and_pooling"]["exact_label_endpoint"] = "shifted_available_bar"
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)


def test_percentile_formula_and_ranking_semantics_tamper_rejected() -> None:
    default = CrossSectionRankingPolicy()
    assert default.percentile_formula == "(average_rank_1_based - 1)/(n - 1)*100"
    assert default.n_equals_1_is_unknown is True
    assert default.no_winsorization_at_any_stage is True
    assert default.low_direction_inverted_percentile_formula == "100 - p"

    with pytest.raises(ValidationError):
        CrossSectionRankingPolicy(percentile_formula="rank/n*100")
    with pytest.raises(ValidationError):
        CrossSectionRankingPolicy(n_equals_1_is_unknown=False)
    with pytest.raises(ValidationError):
        CrossSectionRankingPolicy(no_winsorization_at_any_stage=False)
    with pytest.raises(ValidationError):
        CrossSectionRankingPolicy(low_direction_inverted_percentile_formula="p")

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for field, value in (
        ("percentile_formula", "rank/n*100"),
        ("n_equals_1_is_unknown", False),
        ("no_winsorization_at_any_stage", False),
        ("low_direction_inverted_percentile_formula", "p"),
        ("component_to_family_composite_rule", "mean_then_winsorize_then_rerank"),
    ):
        bad = json.loads(json.dumps(payload))
        bad["ranking"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)


def test_quintile_bucket_formula_and_spearman_all_equal_invalid_tamper_rejected() -> None:
    default_q = QuintileSemanticsPolicy()
    assert default_q.bucket_formula == "min(floor((rank-1)/n*5),4)"
    assert default_q.quantile_count == 5
    default_s = SpearmanIcSemanticsPolicy()
    assert default_s.all_equal_factor_or_label_invalid is True

    with pytest.raises(ValidationError):
        QuintileSemanticsPolicy(bucket_formula="floor(rank/n*5)")
    with pytest.raises(ValidationError):
        QuintileSemanticsPolicy(quantile_count=4)
    with pytest.raises(ValidationError):
        QuintileSemanticsPolicy(ties_never_split_across_buckets=False)
    with pytest.raises(ValidationError):
        QuintileSemanticsPolicy(all_equal_or_empty_extreme_bucket_invalid=False)
    with pytest.raises(ValidationError):
        SpearmanIcSemanticsPolicy(all_equal_factor_or_label_invalid=False)
    with pytest.raises(ValidationError):
        SpearmanIcSemanticsPolicy(pairwise_deletion_of_unknown_or_nonfinite=False)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for field, value in (
        ("bucket_formula", "floor(rank/n*5)"),
        ("quantile_count", 4),
        ("all_equal_or_empty_extreme_bucket_invalid", False),
        ("ties_never_split_across_buckets", False),
    ):
        bad = json.loads(json.dumps(payload))
        bad["labels_and_evidence"]["quintile_semantics"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["labels_and_evidence"]["spearman_ic_semantics"]["all_equal_factor_or_label_invalid"] = False
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)


def test_inference_hac_missing_evidence_qualify_report_fields_tamper_rejected() -> None:
    default = InferencePolicy()
    assert default.hac_kernel == "bartlett_newey_west"
    assert default.variance_of_sample_mean_definition == "LRV/n"
    assert default.missing_evidence_rule == "raw_hac_statistic_and_hac_p_null_holm_input_p_equals_1_rejection_false"
    assert default.qualification_rule == "qualify_only_if_own_null_rejected"
    assert default.undefined_variance_rule == "raw_statistic_and_raw_p_null_do_not_coerce_holm_input_p_equals_1"
    assert default.required_report_fields == list(REQUIRED_INFERENCE_REPORT_FIELDS)
    assert "holm_input_p_value" in default.required_report_fields
    assert "holm_sorted_position" in default.required_report_fields

    with pytest.raises(ValidationError):
        InferencePolicy(hac_kernel="parzen")
    with pytest.raises(ValidationError):
        InferencePolicy(missing_evidence_rule="missing_evidence_p_equals_0")
    with pytest.raises(ValidationError):
        InferencePolicy(qualification_rule="qualify_if_any_qualifies")
    with pytest.raises(ValidationError):
        InferencePolicy(undefined_variance_rule="report_t_anyway")
    with pytest.raises(ValidationError):
        InferencePolicy(variance_of_sample_mean_definition="sample_variance_only")
    with pytest.raises(ValidationError):
        InferencePolicy(required_report_fields=["sample_count"])
    with pytest.raises(ValidationError):
        InferencePolicy(required_report_fields=list(reversed(REQUIRED_INFERENCE_REPORT_FIELDS)))

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for field, value in (
        ("hac_kernel", "parzen"),
        ("missing_evidence_rule", "missing_evidence_p_equals_0"),
        ("qualification_rule", "qualify_if_any_qualifies"),
        ("undefined_variance_rule", "report_t_anyway"),
        ("variance_of_sample_mean_definition", "sample_variance_only"),
    ):
        bad = json.loads(json.dumps(payload))
        bad["labels_and_evidence"]["inference"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["labels_and_evidence"]["inference"]["required_report_fields"] = ["sample_count", "ic"]
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)


def test_newey_west_bartlett_and_holm_exact_algorithm_frozen() -> None:
    nw = NeweyWestBartlettExactAlgorithm()
    assert nw.input_series == "chronologically_ordered_finite_per_decision_day_metric_series_x_1_to_x_n_no_gap_filling"
    assert nw.mean_definition == "arithmetic_xbar"
    assert nw.gamma_k_formula == "gamma_k=(1/n)*sum_{t=k+1..n}(x_t-xbar)*(x_{t-k}-xbar)_including_gamma_0_divisor_n"
    assert nw.bartlett_weight_formula == "w_k=1-k/(L+1)"
    assert nw.long_run_variance_formula == "LRV=gamma_0+2*sum_{k=1..min(L,n-1)}w_k*gamma_k"
    assert nw.variance_of_mean_formula == "LRV/n"
    assert nw.undefined_when == "n_le_L_or_LRV_or_variance_nonfinite_or_le_0_then_statistic_and_raw_hac_p_null"
    assert nw.never_coerce_undefined_to_number is True
    assert nw.positive_test_statistic_formula == "xbar/sqrt(var_mean)"
    assert nw.positive_p_value_formula == "1-Phi(stat)_standard_normal_cdf"
    assert nw.negative_size_band_p_value_formula == "Phi(stat)_standard_normal_cdf"

    holm = HolmStepDownExactAlgorithm()
    assert holm.hypothesis_family == "exactly_four_pooled_h40_daily_ic_hypotheses"
    assert holm.spread_positivity_and_yearly_direction_are_gates_not_holm_members is True
    assert holm.tie_break_factor_family_order == list(HOLM_TIE_BREAK_FACTOR_FAMILY_ORDER)
    assert holm.threshold_at_sorted_position_i == "alpha/(4-i+1)_for_i_equals_1_to_4"
    assert holm.sequential_reject_until_first_failure_then_all_remaining_nonrejected is True
    assert holm.missing_or_undefined_raw_hac_leaves_raw_null_but_holm_input_p_equals_1 is True
    assert holm.missing_or_undefined_raw_hac_rejection_false is True

    with pytest.raises(ValidationError):
        NeweyWestBartlettExactAlgorithm(bartlett_weight_formula="w_k=1-k/L")
    with pytest.raises(ValidationError):
        NeweyWestBartlettExactAlgorithm(gamma_k_formula="gamma_k=(1/(n-k))*sum")
    with pytest.raises(ValidationError):
        NeweyWestBartlettExactAlgorithm(never_coerce_undefined_to_number=False)
    with pytest.raises(ValidationError):
        NeweyWestBartlettExactAlgorithm(positive_p_value_formula="2*(1-Phi(|stat|))")
    with pytest.raises(ValidationError):
        HolmStepDownExactAlgorithm(
            tie_break_factor_family_order=[
                "value",
                "quality",
                "medium_momentum_12_1",
                "defensive_low_vol",
            ]
        )
    with pytest.raises(ValidationError):
        HolmStepDownExactAlgorithm(hypothesis_family="include_spread_as_fifth")
    with pytest.raises(ValidationError):
        HolmStepDownExactAlgorithm(sequential_reject_until_first_failure_then_all_remaining_nonrejected=False)

    inference = InferencePolicy()
    assert inference.newey_west_bartlett_exact.variance_of_mean_formula == inference.variance_of_sample_mean_definition
    assert inference.holm_step_down_exact.tie_break_factor_family_order == list(CONFIRMED_FACTOR_FAMILIES)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for field, value in (
        ("bartlett_weight_formula", "w_k=1-k/L"),
        ("gamma_k_formula", "gamma_k=(1/(n-k))*sum"),
        ("never_coerce_undefined_to_number", False),
        ("positive_p_value_formula", "2*(1-Phi(|stat|))"),
        ("negative_size_band_p_value_formula", "1-Phi(stat)"),
    ):
        bad = json.loads(json.dumps(payload))
        bad["labels_and_evidence"]["inference"]["newey_west_bartlett_exact"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    for field, value in (
        ("hypothesis_family", "include_spread_as_fifth"),
        ("tie_break_factor_family_order", ["value", "quality", "medium_momentum_12_1", "defensive_low_vol"]),
        ("threshold_at_sorted_position_i", "alpha/4"),
        ("missing_or_undefined_raw_hac_leaves_raw_null_but_holm_input_p_equals_1", False),
        ("spread_positivity_and_yearly_direction_are_gates_not_holm_members", False),
    ):
        bad = json.loads(json.dumps(payload))
        bad["labels_and_evidence"]["inference"]["holm_step_down_exact"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["pre_freeze_selection"]["holm_family_members_are_pooled_h40_daily_ic_only"] = False
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)


def test_defensive_low_vol_ddof_returns_closes_frozen() -> None:
    default = DefensiveLowVolFactorFormula()
    assert default.return_count == 60
    assert default.close_count == 61
    assert default.sample_stdev_ddof == 1
    assert default.annualization_sqrt_242 is True
    assert default.sign == "negative"
    assert (
        default.window_definition == "exactly_latest_61_consecutive_market_calendar_observations_ending_at_decision_t"
    )
    assert default.any_missing_or_unverified_market_day_makes_factor_unknown is True
    assert default.never_skip_compress_gaps is True

    with pytest.raises(ValidationError):
        DefensiveLowVolFactorFormula(return_count=59)
    with pytest.raises(ValidationError):
        DefensiveLowVolFactorFormula(close_count=60)
    with pytest.raises(ValidationError):
        DefensiveLowVolFactorFormula(sample_stdev_ddof=0)
    with pytest.raises(ValidationError):
        DefensiveLowVolFactorFormula(sign="positive")
    with pytest.raises(ValidationError):
        DefensiveLowVolFactorFormula(annualization_sqrt_242=False)
    with pytest.raises(ValidationError):
        DefensiveLowVolFactorFormula(never_skip_compress_gaps=False)
    with pytest.raises(ValidationError):
        DefensiveLowVolFactorFormula(window_definition="latest_61_available_bars_skipping_missing_days")

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for field, value in (
        ("return_count", 59),
        ("close_count", 60),
        ("sample_stdev_ddof", 0),
        ("sign", "positive"),
        ("annualization_sqrt_242", False),
        ("never_skip_compress_gaps", False),
        ("any_missing_or_unverified_market_day_makes_factor_unknown", False),
        ("window_definition", "latest_61_available_bars_skipping_missing_days"),
    ):
        bad = json.loads(json.dumps(payload))
        bad["factor_families"][3]["defensive_low_vol"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)


def test_momentum_and_label_bar_window_endpoints_never_skip_compress() -> None:
    momentum = MediumMomentumFactorFormula()
    assert momentum.require_ordered_positive_finite_market_bars == 243
    assert (
        momentum.window_definition == "exactly_latest_243_consecutive_market_calendar_observations_ending_at_decision_t"
    )
    assert momentum.formula_uses_fixed_indices == "t-242_and_t-21_within_that_window"
    assert momentum.any_missing_or_unverified_market_day_makes_factor_unknown is True
    assert momentum.never_skip_compress_gaps is True

    with pytest.raises(ValidationError):
        MediumMomentumFactorFormula(never_skip_compress_gaps=False)
    with pytest.raises(ValidationError):
        MediumMomentumFactorFormula(window_definition="latest_243_available_bars_skipping_missing_days")
    with pytest.raises(ValidationError):
        MediumMomentumFactorFormula(formula_uses_fixed_indices="nearest_available_closes")

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for field, value in (
        ("never_skip_compress_gaps", False),
        ("any_missing_or_unverified_market_day_makes_factor_unknown", False),
        ("window_definition", "latest_243_available_bars_skipping_missing_days"),
        ("formula_uses_fixed_indices", "nearest_available_closes"),
        ("require_ordered_positive_finite_market_bars", 240),
    ):
        bad = json.loads(json.dumps(payload))
        bad["factor_families"][2]["medium_momentum_12_1"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    for field, value in (
        ("exact_endpoint_definition", "next_available_bar_after_t_plus_h"),
        ("missing_or_unverified_endpoint_is_unknown", False),
        ("horizon_never_shifts", False),
    ):
        bad = json.loads(json.dumps(payload))
        bad["labels_and_evidence"]["primary_label"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    for field, value in (
        ("exact_endpoint_definition", "next_available_bar_after_t_plus_h"),
        ("missing_or_unverified_endpoint_is_unknown", False),
        ("horizon_never_shifts", False),
    ):
        bad = json.loads(json.dumps(payload))
        bad["labels_and_evidence"]["secondary_horizons"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)


def test_size_band_exact_boundaries_and_rules_tamper_rejected() -> None:
    default = SizeBandDiagnosticSafeguards()
    assert [(b.label, b.min_inclusive, b.max_exclusive) for b in default.bands] == list(CONFIRMED_SIZE_BANDS)
    assert default.min_bands_positive == 2
    assert default.min_valid_primary_dates_per_band == 40

    with pytest.raises(ValidationError):
        SizeBand(label="3bn_5bn", min_inclusive=5e9, max_exclusive=5e9)
    with pytest.raises(ValidationError):
        SizeBand(label="3bn_5bn", min_inclusive=5e9, max_exclusive=3e9)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))

    bad = json.loads(json.dumps(payload))
    bad["size_bands"]["bands"][0]["min_inclusive"] = 2_500_000_000.0
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["size_bands"]["bands"][1]["max_exclusive"] = 9_000_000_000.0
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["size_bands"]["bands"][2]["max_exclusive"] = 20_000_000_000.0
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    for field, value in (
        ("positive_band_rule", "either_ic_or_spread_positive"),
        ("min_bands_positive", 1),
        ("min_valid_primary_dates_per_band", 20),
        ("negative_significance_rule", "two_sided_test"),
        ("negative_significance_alpha", 0.10),
        ("band_below_min_dates_is_unknown_and_not_positive", False),
        ("any_significant_negative_band_fails", False),
        ("unknown_stays_unknown", False),
    ):
        bad = json.loads(json.dumps(payload))
        bad["size_bands"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)


def test_cluster_companion_lookback_pearson_chain_singleton_holm_tamper_rejected() -> None:
    default = ClusterCompanionPolicy()
    assert default.statistical_risk_cluster.lookback_trading_days == 120
    assert default.required_close_points == 121
    assert default.correlation_threshold == 0.65
    assert default.linkage == "connected_components_chain"
    assert default.singleton_clusters_unknown is True
    assert default.never_fifth_holm_hypothesis is True
    assert default.companion_requires_both_pooled_h40_ic_and_spread_positive is True

    with pytest.raises(ValidationError):
        ClusterCompanionPolicy(correlation_threshold=0.5)
    with pytest.raises(ValidationError):
        ClusterCompanionPolicy(required_close_points=120)
    with pytest.raises(ValidationError):
        ClusterCompanionPolicy(linkage="mutual_knn")
    with pytest.raises(ValidationError):
        ClusterCompanionPolicy(singleton_clusters_unknown=False)
    with pytest.raises(ValidationError):
        ClusterCompanionPolicy(never_fifth_holm_hypothesis=False)
    with pytest.raises(ValidationError):
        ClusterCompanionPolicy(safeguard_only_never_independent_weight=False)
    with pytest.raises(ValidationError):
        ClusterCompanionPolicy(companion_requires_both_pooled_h40_ic_and_spread_positive=False)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for field, value in (
        ("correlation_threshold", 0.5),
        ("lookback_trading_days", 60),
        ("required_close_points", 120),
        ("linkage", "mutual_knn"),
        ("singleton_clusters_unknown", False),
        ("never_fifth_holm_hypothesis", False),
        ("safeguard_only_never_independent_weight", False),
        ("companion_requires_both_pooled_h40_ic_and_spread_positive", False),
        ("recompute_anchor", "every_trading_day"),
        ("carry_assignment_until_before_next_anchor", False),
        ("new_unassigned_or_incomplete_is_unknown_no_backfill", False),
        ("static_current_industry_labels_forbidden", False),
        ("no_current_industry_labels", False),
        ("not_automatic_weight_selector", False),
    ):
        bad = json.loads(json.dumps(payload))
        bad["cluster_companion"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["cluster_companion"]["statistical_risk_cluster"]["lookback_trading_days"] = 60
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    bad = json.loads(json.dumps(payload))
    bad["cluster_companion"]["statistical_risk_cluster"]["correlation_threshold"] = 0.5
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)


def test_eligibility_denominator_and_pit_snapshot_binding_tamper_rejected() -> None:
    default_elig = EligibilityDenominatorPolicy()
    assert default_elig.alpha_factor_must_not_determine_eligibility is True
    default_pit = PitSnapshotBindingPolicy()
    assert default_pit.must_share_exact_sealed_market_snapshot is True

    with pytest.raises(ValidationError):
        EligibilityDenominatorPolicy(alpha_factor_must_not_determine_eligibility=False)
    with pytest.raises(ValidationError):
        EligibilityDenominatorPolicy(entry_requires_eligible_for_new_entry_true=False)
    with pytest.raises(ValidationError):
        EligibilityDenominatorPolicy(entry_requires_financial_verdict_not_hard_exclude_or_unknown=False)
    with pytest.raises(ValidationError):
        PitSnapshotBindingPolicy(all_as_of_decision_at_available_at_must_be_pit=False)
    with pytest.raises(ValidationError):
        PitSnapshotBindingPolicy(must_share_exact_sealed_market_snapshot=False)
    with pytest.raises(ValidationError):
        PitSnapshotBindingPolicy(ready_flags_remain_false=False)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for field, value in (
        ("alpha_factor_must_not_determine_eligibility", False),
        ("entry_requires_eligible_for_new_entry_true", False),
        ("entry_requires_financial_verdict_not_hard_exclude_or_unknown", False),
    ):
        bad = json.loads(json.dumps(payload))
        bad["eligibility_denominator"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)

    for field, value in (
        ("all_as_of_decision_at_available_at_must_be_pit", False),
        ("must_share_exact_sealed_market_snapshot", False),
        ("protocol_describes_future_input_bindings_only", False),
        ("ready_flags_remain_false", False),
    ):
        bad = json.loads(json.dumps(payload))
        bad["pit_snapshot_binding"][field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAlphaDevelopmentProtocolV1.model_validate(bad)


def test_labels_and_evidence_nests_new_semantic_blocks() -> None:
    default = LabelsAndEvidencePolicy()
    assert isinstance(default.forward_label_and_pooling, ForwardLabelAndPoolingPolicy)
    assert isinstance(default.quintile_semantics, QuintileSemanticsPolicy)
    assert isinstance(default.spearman_ic_semantics, SpearmanIcSemanticsPolicy)
    assert isinstance(default.inference, InferencePolicy)


def test_canonical_factory_equality_after_reseal_of_each_semantic_block() -> None:
    factory = build_confirmed_layer_two_alpha_development_protocol_v1()
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))

    mutation_paths: list[tuple[str, ...]] = [
        ("ranking", "note"),
        ("cluster_companion", "note"),
        ("labels_and_evidence", "forward_label_and_pooling", "note"),
        ("labels_and_evidence", "inference", "note"),
        ("labels_and_evidence", "inference", "newey_west_bartlett_exact", "note"),
        ("labels_and_evidence", "inference", "holm_step_down_exact", "note"),
        ("labels_and_evidence", "quintile_semantics", "note"),
        ("labels_and_evidence", "spearman_ic_semantics", "note"),
        ("size_bands", "note"),
        ("eligibility_denominator", "note"),
        ("pit_snapshot_binding", "note"),
    ]

    for path in mutation_paths:
        drifted = json.loads(json.dumps(payload))
        cursor: dict[str, Any] = drifted
        for key in path[:-1]:
            cursor = cursor[key]
        cursor[path[-1]] = f"attacker rewrote {'.'.join(path)} after outer reseal"
        drifted.pop("protocol_id", None)
        resealed = seal_layer_two_alpha_development_protocol(LayerTwoAlphaDevelopmentProtocolV1.model_validate(drifted))
        assert resealed.protocol_id != factory.protocol_id
        with pytest.raises(ValueError, match="does not match sealed factory"):
            assert_matches_sealed_factory_canonical(resealed)

    mom_drift = json.loads(json.dumps(payload))
    mom_drift["factor_families"][2]["medium_momentum_12_1"]["note"] = "attacker skip-compress rewrite"
    mom_drift.pop("protocol_id", None)
    mom_resealed = seal_layer_two_alpha_development_protocol(
        LayerTwoAlphaDevelopmentProtocolV1.model_validate(mom_drift)
    )
    with pytest.raises(ValueError, match="does not match sealed factory"):
        assert_matches_sealed_factory_canonical(mom_resealed)


def test_no_production_scoring_backtest_imports_in_module_ast() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
    forbidden_prefixes = (
        "app.scoring",
        "app.pipeline",
        "app.strategies",
        "app.backtest",
    )
    for module in imported:
        assert not any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_prefixes)
    blob: dict[str, Any] = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    text = json.dumps(blob)
    for forbidden in ("sharpe", "ready_for_live", "broker"):
        assert forbidden not in text
    assert math.isfinite(blob["coverage_gates"]["min_factor_known_cs_fraction_of_eligible"])
