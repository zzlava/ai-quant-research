from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.research.layer_one_index_protocol import (
    load_layer_one_index_protocol_draft,
    verify_layer_one_index_protocol_draft,
)
from app.research.layer_two_allocation_protocol import (
    BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
    BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
    CONFIRMED_MAX_ACTIVE_SLOTS_BY_BUDGET,
    CONFIRMED_MINIMUM_BASE_SLOT_NOTIONAL_CNY,
    DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH,
    REQUIRED_ALLOCATION_EVIDENCE_BLOCKERS,
    LayerTwoAllocationImplementationProtocolV1,
    WorkedExample,
    build_confirmed_layer_two_allocation_protocol_v1,
    cluster_notional_cap,
    compute_protocol_id,
    compute_sleeve_budget,
    load_layer_two_allocation_protocol,
    max_active_slots_for_budget,
    plan_base_slots,
    plan_final_target_notional,
    seal_layer_two_allocation_protocol,
    verify_layer_two_allocation_protocol,
    verify_layer_two_allocation_protocol_file,
    write_layer_two_allocation_protocol,
)
from app.research.tranche_evaluation_protocol import (
    load_tranche_evaluation_protocol_draft,
    verify_tranche_evaluation_protocol_draft,
)
from app.research.two_layer_contract import load_two_layer_decision_draft, verify_two_layer_decision_draft
from tests.helpers import PROJECT_ROOT

COMMITTED_PROTOCOL = PROJECT_ROOT / DEFAULT_LAYER_TWO_ALLOCATION_PROTOCOL_PATH
COMMITTED_TWO_LAYER = PROJECT_ROOT / BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
COMMITTED_LAYER_ONE = PROJECT_ROOT / BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH
COMMITTED_TRANCHE = PROJECT_ROOT / BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH


def test_committed_protocol_confirmed_not_ready_and_disk_bound() -> None:
    draft, result = verify_layer_two_allocation_protocol_file(
        protocol_path=COMMITTED_PROTOCOL,
        repo_root=PROJECT_ROOT,
    )
    assert draft.schema_version == "1"
    assert draft.status == "confirmed_for_implementation_but_not_ready"
    assert draft.readiness.research_only is True
    assert draft.readiness.ready_for_scoring is False
    assert draft.readiness.ready_for_backtest is False
    assert draft.readiness.ready_for_portfolio_construction is False
    assert draft.readiness.ready_for_orders is False
    assert draft.readiness.ready_for_trading is False
    assert draft.readiness.auto_apply is False
    assert draft.readiness.does_not_construct_portfolio is True
    assert draft.pending_user_decisions == []
    assert result.user_decisions_resolved is True
    assert result.pending_user_decision_count == 0
    assert result.resolved is False
    assert result.two_layer_decision_contract_binding_ok is True
    assert result.layer_one_index_protocol_binding_ok is True
    assert result.tranche_evaluation_protocol_binding_ok is True
    assert draft.two_layer_decision_contract_id == BOUND_TWO_LAYER_DECISION_CONTRACT_ID
    assert draft.layer_one_index_protocol_id == BOUND_LAYER_ONE_INDEX_PROTOCOL_ID
    assert draft.tranche_evaluation_protocol_id == BOUND_TRANCHE_EVALUATION_PROTOCOL_ID
    assert draft.protocol_id == compute_protocol_id(draft)
    assert draft.base_slot.minimum_base_slot_notional_cny == CONFIRMED_MINIMUM_BASE_SLOT_NOTIONAL_CNY
    assert draft.base_slot.minimum_applies_before_risk_multipliers is True
    assert draft.base_slot.minimum_is_not_post_multiplier_floor is True
    assert draft.released_capital.v1_risk_multiplier_released_capital_stays_cash is True
    assert draft.cluster_cap.cluster_cap_denominator == "sleeve_budget"
    assert draft.active_counts.holding_cycle_is_not_active_tranche_count is True
    assert draft.interpretation_inputs.bool_inputs_fail_closed is True
    assert draft.interpretation_inputs.zero_equity_allowed is True

    contract = load_two_layer_decision_draft(COMMITTED_TWO_LAYER)
    assert verify_two_layer_decision_draft(contract).contract_id == draft.two_layer_decision_contract_id
    layer_one = load_layer_one_index_protocol_draft(COMMITTED_LAYER_ONE)
    assert verify_layer_one_index_protocol_draft(layer_one).protocol_id == draft.layer_one_index_protocol_id
    tranche = load_tranche_evaluation_protocol_draft(COMMITTED_TRANCHE)
    assert verify_tranche_evaluation_protocol_draft(tranche).protocol_id == draft.tranche_evaluation_protocol_id


def test_structural_verifier_does_not_claim_disk_bindings() -> None:
    structural = verify_layer_two_allocation_protocol(build_confirmed_layer_two_allocation_protocol_v1())
    assert structural.two_layer_decision_contract_binding_ok is False
    assert structural.layer_one_index_protocol_binding_ok is False
    assert structural.tranche_evaluation_protocol_binding_ok is False
    assert structural.resolved is False
    assert structural.user_decisions_resolved is True

    _, file_result = verify_layer_two_allocation_protocol_file(
        protocol_path=COMMITTED_PROTOCOL,
        repo_root=PROJECT_ROOT,
    )
    assert file_result.two_layer_decision_contract_binding_ok is True
    assert file_result.layer_one_index_protocol_binding_ok is True
    assert file_result.tranche_evaluation_protocol_binding_ok is True


def test_protocol_hash_stable_and_mismatch_fails() -> None:
    first = build_confirmed_layer_two_allocation_protocol_v1()
    second = build_confirmed_layer_two_allocation_protocol_v1()
    assert first.protocol_id == second.protocol_id
    broken = first.model_copy(update={"protocol_id": "0" * 64})
    with pytest.raises(ValueError, match="protocol_id does not match"):
        verify_layer_two_allocation_protocol(broken)


def test_upstream_id_path_and_disk_content_drift_rejected(tmp_path: Path) -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for field, value in (
        ("two_layer_decision_contract_id", "a" * 64),
        ("layer_one_index_protocol_id", "b" * 64),
        ("tranche_evaluation_protocol_id", "c" * 64),
    ):
        bad = dict(payload)
        bad[field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAllocationImplementationProtocolV1.model_validate(bad)

    for field, value in (
        ("two_layer_decision_contract_path", "config/research/other-contract.json"),
        ("layer_one_index_protocol_path", "config/research/other-layer-one.json"),
        ("tranche_evaluation_protocol_path", "config/research/other-tranche.json"),
    ):
        bad = dict(payload)
        bad[field] = value
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAllocationImplementationProtocolV1.model_validate(bad)

    protocol_path = tmp_path / "protocol.json"
    write_layer_two_allocation_protocol(protocol_path, build_confirmed_layer_two_allocation_protocol_v1())

    class _FakeContractResult:
        schema_version = "2"
        contract_id = "f" * 64

    with (
        patch(
            "app.research.layer_two_allocation_protocol.verify_two_layer_decision_draft",
            return_value=_FakeContractResult(),
        ),
        patch(
            "app.research.layer_two_allocation_protocol.load_two_layer_decision_draft",
            return_value=object(),
        ),
    ):
        with pytest.raises(ValueError, match="contract_id"):
            verify_layer_two_allocation_protocol_file(protocol_path=protocol_path, repo_root=PROJECT_ROOT)

    class _FakeLayerOneResult:
        schema_version = "2"
        protocol_id = "e" * 64

    with (
        patch(
            "app.research.layer_two_allocation_protocol.verify_layer_one_index_protocol_draft",
            return_value=_FakeLayerOneResult(),
        ),
        patch(
            "app.research.layer_two_allocation_protocol.load_layer_one_index_protocol_draft",
            return_value=object(),
        ),
    ):
        with pytest.raises(ValueError, match="protocol_id"):
            verify_layer_two_allocation_protocol_file(protocol_path=protocol_path, repo_root=PROJECT_ROOT)

    class _FakeTrancheResult:
        schema_version = "2"
        protocol_id = "d" * 64

    with (
        patch(
            "app.research.layer_two_allocation_protocol.verify_tranche_evaluation_protocol_draft",
            return_value=_FakeTrancheResult(),
        ),
        patch(
            "app.research.layer_two_allocation_protocol.load_tranche_evaluation_protocol_draft",
            return_value=object(),
        ),
    ):
        with pytest.raises(ValueError, match="protocol_id"):
            verify_layer_two_allocation_protocol_file(protocol_path=protocol_path, repo_root=PROJECT_ROOT)


def test_8000_is_not_post_multiplier_floor() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["base_slot"]["minimum_is_not_post_multiplier_floor"] = False
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAllocationImplementationProtocolV1.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["risk_multipliers"]["lift_post_multiplier_notional_back_to_minimum_forbidden"] = False
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAllocationImplementationProtocolV1.model_validate(payload)

    plan = plan_base_slots(current_account_equity=80_000.0, risk_budget=0.3)
    assert plan.base_slot_notional == 8000.0
    final = plan_final_target_notional(
        base_slot_notional=plan.base_slot_notional or 0.0,
        size_multiplier=0.5,
        financial_multiplier=0.5,
    )
    assert final.final_target_notional == 2000.0
    assert final.final_target_notional < CONFIRMED_MINIMUM_BASE_SLOT_NOTIONAL_CNY


def test_released_capital_must_not_be_redistributed() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["released_capital"]["v1_risk_multiplier_released_capital_stays_cash"] = False
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAllocationImplementationProtocolV1.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["released_capital"]["same_day_transfer_to_other_candidates_forbidden"] = False
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAllocationImplementationProtocolV1.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["released_capital"]["small_cap_backfill_forbidden"] = False
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAllocationImplementationProtocolV1.model_validate(payload)


def test_cluster_denominator_must_be_sleeve_budget_not_invested(tmp_path: Path) -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["cluster_cap"]["cluster_cap_denominator"] = "invested_notional"
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAllocationImplementationProtocolV1.model_validate(payload)

    raw = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    raw["cluster_cap"]["cluster_cap_denominator"] = "invested_notional"
    raw.pop("protocol_id", None)
    tmp = tmp_path / "invested_denom_reject.json"
    tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sleeve_budget"):
        load_layer_two_allocation_protocol(tmp)

    assert cluster_notional_cap(sleeve_budget=24_000.0) == 8400.0


def test_holding_cycle_40_is_not_active_count(tmp_path: Path) -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["active_counts"]["holding_cycle_is_not_active_tranche_count"] = False
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAllocationImplementationProtocolV1.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["base_slot"]["max_active_slots_by_budget"] = {
        "0.0": 0,
        "0.3": 40,
        "0.6": 6,
        "0.9": 9,
    }
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAllocationImplementationProtocolV1.model_validate(payload)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["tranche_count"] = 40
    payload.pop("protocol_id", None)
    bad_path = tmp_path / "tranche_count.json"
    bad_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rejects tranche_count"):
        load_layer_two_allocation_protocol(bad_path)


def test_budget_to_slot_cap_mapping_0_3_6_9() -> None:
    assert CONFIRMED_MAX_ACTIVE_SLOTS_BY_BUDGET == {"0.0": 0, "0.3": 3, "0.6": 6, "0.9": 9}
    assert max_active_slots_for_budget(0.0) == 0
    assert max_active_slots_for_budget(0.3) == 3
    assert max_active_slots_for_budget(0.6) == 6
    assert max_active_slots_for_budget(0.9) == 9
    with pytest.raises(ValueError, match="risk_budget"):
        max_active_slots_for_budget(0.45)

    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload["base_slot"]["max_active_slots_by_budget"] = {
        "0.0": 0,
        "0.3": 3,
        "0.6": 6,
        "0.9": 8,
    }
    payload.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAllocationImplementationProtocolV1.model_validate(payload)


def test_deterministic_equity_examples_70k_80k_100k() -> None:
    p70 = plan_base_slots(current_account_equity=70_000.0, risk_budget=0.3)
    assert p70.sleeve_budget == 21_000.0
    assert p70.budget_slot_cap == 3
    assert p70.base_slot_count == 2
    assert p70.base_slot_notional == 10_500.0

    p80 = plan_base_slots(current_account_equity=80_000.0, risk_budget=0.3)
    assert p80.sleeve_budget == 24_000.0
    assert p80.base_slot_count == 3
    assert p80.base_slot_notional == 8_000.0

    p100 = plan_base_slots(current_account_equity=100_000.0, risk_budget=0.3)
    assert p100.sleeve_budget == 30_000.0
    assert p100.base_slot_count == 3
    assert p100.base_slot_notional == 10_000.0

    p0 = plan_base_slots(current_account_equity=80_000.0, risk_budget=0.0)
    assert p0.base_slot_count == 0
    assert p0.cash_retention_reason == "zero_risk_budget"

    p_short = plan_base_slots(current_account_equity=20_000.0, risk_budget=0.3)
    assert p_short.sleeve_budget == 6_000.0
    assert p_short.base_slot_count == 0
    assert p_short.cash_retention_reason == "insufficient_capital_for_minimum_base_slot"

    p60 = plan_base_slots(current_account_equity=80_000.0, risk_budget=0.6)
    assert p60.base_slot_count == 6
    assert p60.base_slot_notional == 8_000.0

    p90 = plan_base_slots(current_account_equity=80_000.0, risk_budget=0.9)
    assert p90.base_slot_count == 9
    assert p90.base_slot_notional == 8_000.0


def test_size_financial_combinations_and_unknown_zero() -> None:
    base = 8_000.0
    cases = [
        (0.5, 1.0, 4_000.0),
        (0.75, 1.0, 6_000.0),
        (1.0, 1.0, 8_000.0),
        (0.5, 0.5, 2_000.0),
        (0.75, 0.5, 3_000.0),
        (1.0, 0.5, 4_000.0),
    ]
    for size, financial, expected in cases:
        plan = plan_final_target_notional(
            base_slot_notional=base,
            size_multiplier=size,
            financial_multiplier=financial,
        )
        assert plan.final_target_notional == expected
        assert plan.hard_excluded is False
        assert plan.released_capital_stays_cash is True

    hard = plan_final_target_notional(
        base_slot_notional=base,
        size_multiplier=1.0,
        financial_multiplier=0.0,
    )
    assert hard.hard_excluded is True
    assert hard.final_target_notional is None
    assert hard.cash_retention_reason == "financial_hard_exclude"

    unknown = plan_final_target_notional(
        base_slot_notional=base,
        size_multiplier=1.0,
        financial_multiplier="unknown",
    )
    assert unknown.retain_cash is True
    assert unknown.final_target_notional is None
    assert unknown.cash_retention_reason == "financial_unknown"

    with pytest.raises(ValueError, match="size_multiplier"):
        plan_final_target_notional(base_slot_notional=base, size_multiplier=0.6, financial_multiplier=1.0)
    with pytest.raises(ValueError, match="financial_multiplier"):
        plan_final_target_notional(base_slot_notional=base, size_multiplier=1.0, financial_multiplier=0.25)


def test_ready_flag_injection_rejected() -> None:
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    for flag in (
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_portfolio_construction",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
    ):
        bad = json.loads(json.dumps(payload))
        bad["readiness"][flag] = True
        bad.pop("protocol_id", None)
        with pytest.raises(ValidationError):
            LayerTwoAllocationImplementationProtocolV1.model_validate(bad)

    bad = dict(payload)
    bad["status"] = "ready_for_implementation"
    bad.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAllocationImplementationProtocolV1.model_validate(bad)


def test_outer_reseal_cannot_mask_semantic_drift(tmp_path: Path) -> None:
    """Mutate semantics/notes/blockers/examples, reseal; verifiers must fail factory match."""
    factory = build_confirmed_layer_two_allocation_protocol_v1()
    payload = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))

    note_drift = dict(payload)
    note_drift["risk_multipliers"] = dict(payload["risk_multipliers"])
    note_drift["risk_multipliers"]["note"] = "silently redistribute released capital to other names"
    note_drift["released_capital"] = dict(payload["released_capital"])
    note_drift["released_capital"]["note"] = "allow same-day redistribution"
    note_drift.pop("protocol_id", None)
    note_resealed = seal_layer_two_allocation_protocol(
        LayerTwoAllocationImplementationProtocolV1.model_validate(note_drift)
    )
    assert note_resealed.protocol_id != factory.protocol_id
    with pytest.raises(ValueError, match="does not match sealed factory"):
        verify_layer_two_allocation_protocol(note_resealed)
    note_path = tmp_path / "note-drift.json"
    write_layer_two_allocation_protocol(note_path, note_resealed)
    with pytest.raises(ValueError, match="does not match sealed factory"):
        verify_layer_two_allocation_protocol_file(protocol_path=note_path, repo_root=PROJECT_ROOT)

    blocker_drift = dict(payload)
    blockers = [dict(b) for b in payload["evidence_blockers"]]
    blockers[0]["detail"] = "attacker rewrote blocker detail after outer reseal"
    blocker_drift["evidence_blockers"] = blockers
    blocker_drift.pop("protocol_id", None)
    blocker_resealed = seal_layer_two_allocation_protocol(
        LayerTwoAllocationImplementationProtocolV1.model_validate(blocker_drift)
    )
    with pytest.raises(ValueError, match="does not match sealed factory"):
        verify_layer_two_allocation_protocol(blocker_resealed)

    example_drift = dict(payload)
    examples = [dict(ex) for ex in payload["worked_examples"]]
    examples[0]["detail"] = "attacker rewrote worked example detail"
    example_drift["worked_examples"] = examples
    example_drift.pop("protocol_id", None)
    example_resealed = seal_layer_two_allocation_protocol(
        LayerTwoAllocationImplementationProtocolV1.model_validate(example_drift)
    )
    with pytest.raises(ValueError, match="does not match sealed factory"):
        verify_layer_two_allocation_protocol(example_resealed)

    # Structural verifier still does not claim disk bindings on the canonical factory.
    structural = verify_layer_two_allocation_protocol(factory)
    assert structural.two_layer_decision_contract_binding_ok is False
    assert structural.layer_one_index_protocol_binding_ok is False
    assert structural.tranche_evaluation_protocol_binding_ok is False
    _, file_result = verify_layer_two_allocation_protocol_file(
        protocol_path=COMMITTED_PROTOCOL,
        repo_root=PROJECT_ROOT,
    )
    assert file_result.two_layer_decision_contract_binding_ok is True
    assert file_result.layer_one_index_protocol_binding_ok is True
    assert file_result.tranche_evaluation_protocol_binding_ok is True

    # Boolean semantic attack still fails validation even before reseal.
    payload_bool = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    payload_bool["cluster_cap"]["cluster_cap_denominator_is_not_invested_notional"] = False
    payload_bool.pop("protocol_id", None)
    with pytest.raises(ValidationError):
        LayerTwoAllocationImplementationProtocolV1.model_validate(payload_bool)


def test_bool_nan_inf_numeric_inputs_fail_closed() -> None:
    with pytest.raises(ValueError, match="bool rejected"):
        plan_base_slots(current_account_equity=True, risk_budget=0.3)
    with pytest.raises(ValueError, match="bool rejected"):
        plan_base_slots(current_account_equity=80_000.0, risk_budget=False)
    with pytest.raises(ValueError, match="bool rejected"):
        plan_final_target_notional(base_slot_notional=8_000.0, size_multiplier=True, financial_multiplier=1.0)
    with pytest.raises(ValueError, match="bool rejected"):
        plan_final_target_notional(base_slot_notional=8_000.0, size_multiplier=1.0, financial_multiplier=True)
    with pytest.raises(ValueError, match="bool rejected"):
        cluster_notional_cap(sleeve_budget=True)
    with pytest.raises(ValueError, match="bool rejected"):
        compute_sleeve_budget(current_account_equity=False, risk_budget=0.3)

    with pytest.raises(ValueError, match="NaN/Inf rejected"):
        plan_base_slots(current_account_equity=float("nan"), risk_budget=0.3)
    with pytest.raises(ValueError, match="NaN/Inf rejected"):
        plan_base_slots(current_account_equity=80_000.0, risk_budget=float("inf"))
    with pytest.raises(ValueError, match="NaN/Inf rejected"):
        plan_final_target_notional(
            base_slot_notional=8_000.0,
            size_multiplier=float("nan"),
            financial_multiplier=1.0,
        )
    with pytest.raises(ValueError, match="NaN/Inf rejected"):
        cluster_notional_cap(sleeve_budget=float("-inf"))

    with pytest.raises(ValueError, match="must be >= 0"):
        plan_base_slots(current_account_equity=-1.0, risk_budget=0.3)
    with pytest.raises(ValueError, match="must be >= 0"):
        cluster_notional_cap(sleeve_budget=-100.0)

    zero = plan_base_slots(current_account_equity=0.0, risk_budget=0.3)
    assert zero.sleeve_budget == 0.0
    assert zero.base_slot_count == 0
    assert zero.cash_retention_reason == "insufficient_capital_for_minimum_base_slot"

    with pytest.raises(ValidationError):
        WorkedExample(
            label="bool-equity",
            current_account_equity=True,  # type: ignore[arg-type]
            risk_budget=0.3,
            sleeve_budget=0.0,
            budget_slot_cap=3,
            base_slot_count=0,
            base_slot_notional=None,
            detail="bool equity must fail closed",
        )
    with pytest.raises(ValidationError):
        WorkedExample(
            label="bool-budget",
            current_account_equity=0.0,
            risk_budget=False,  # type: ignore[arg-type]
            sleeve_budget=0.0,
            budget_slot_cap=0,
            base_slot_count=0,
            base_slot_notional=None,
            detail="bool risk_budget must fail closed",
        )


def test_file_roundtrip(tmp_path: Path) -> None:
    factory = build_confirmed_layer_two_allocation_protocol_v1()
    path = tmp_path / "allocation-protocol.json"
    write_layer_two_allocation_protocol(path, factory)
    loaded = load_layer_two_allocation_protocol(path)
    assert loaded.protocol_id == factory.protocol_id
    assert loaded.model_dump(exclude={"protocol_id"}) == factory.model_dump(exclude={"protocol_id"})
    _, result = verify_layer_two_allocation_protocol_file(protocol_path=path, repo_root=PROJECT_ROOT)
    assert result.tranche_evaluation_protocol_binding_ok is True
    assert result.protocol_id == factory.protocol_id


def test_factory_matches_committed_file() -> None:
    loaded = load_layer_two_allocation_protocol(COMMITTED_PROTOCOL)
    factory = build_confirmed_layer_two_allocation_protocol_v1()
    assert loaded.protocol_id == factory.protocol_id
    assert loaded.model_dump(exclude={"protocol_id"}) == factory.model_dump(exclude={"protocol_id"})


def test_worked_example_80k_30pct_documents_2k_downweight() -> None:
    draft = load_layer_two_allocation_protocol(COMMITTED_PROTOCOL)
    labels = {ex.label: ex for ex in draft.worked_examples}
    base = labels["equity_80k_budget_30pct_three_base_slots"]
    assert base.sleeve_budget == 24_000.0
    assert base.base_slot_count == 3
    assert base.base_slot_notional == 8_000.0
    down = labels["size_0_5_financial_0_5_post_multiplier_2k"]
    assert down.final_target_notional == 2_000.0
    assert "不得抬回" in down.detail or "must not lift" in down.detail
    assert down.cash_retention_reason == "risk_multiplier_released_capital"


def test_required_evidence_blockers_present() -> None:
    draft = build_confirmed_layer_two_allocation_protocol_v1()
    paths = {b.path: b.category for b in draft.evidence_blockers}
    for path, category in REQUIRED_ALLOCATION_EVIDENCE_BLOCKERS.items():
        assert paths[path] == category


def test_no_performance_or_trading_claim_fields() -> None:
    payload: dict[str, Any] = json.loads(COMMITTED_PROTOCOL.read_text(encoding="utf-8"))
    blob = json.dumps(payload)
    for forbidden in ("sharpe", "pnl", "winner", "broker", "ready_for_live"):
        assert forbidden not in blob
    assert payload["readiness"]["ready_for_scoring"] is False
    assert payload["readiness"]["ready_for_portfolio_construction"] is False
    assert payload["readiness"]["ready_for_orders"] is False
    assert payload["readiness"]["ready_for_trading"] is False
