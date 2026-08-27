from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.research.two_layer_allocation import (
    LayerOneBudgetDecision,
    LayerTwoStockSleeve,
    StockTargetWeight,
    compose_two_layer_portfolio,
)
from app.research.two_layer_contract import (
    BOUND_RESEARCH_TRIAL_LEDGER_ID,
    BOUND_RESEARCH_TRIAL_LEDGER_PATH,
    CONFIRMED_INITIAL_CASH,
    DEFAULT_TWO_LAYER_DECISION_DRAFT_PATH,
    REQUIRED_DECISION_PATHS,
    CategorizedBlocker,
    ExecutionPendingDecisions,
    LayerOnePendingDecisions,
    LayerTwoPendingDecisions,
    TwoLayerStrategyDecisionContractV2,
    TwoLayerStrategyDecisionDraft,
    TwoLayerStrategyDecisionDraftV1,
    build_confirmed_contract_v2,
    build_unresolved_draft,
    collect_decision_blockers,
    compute_contract_id,
    compute_two_layer_v2_overall_resolved,
    default_evidence_blockers,
    load_two_layer_decision_draft,
    migrate_decision_contract_v1_to_v2,
    seal_two_layer_decision_draft,
    verify_two_layer_decision_draft,
    verify_two_layer_decision_draft_file,
    write_two_layer_decision_draft,
)
from tests.helpers import PROJECT_ROOT

COMMITTED_DRAFT = PROJECT_ROOT / DEFAULT_TWO_LAYER_DECISION_DRAFT_PATH
COMMITTED_LEDGER = PROJECT_ROOT / BOUND_RESEARCH_TRIAL_LEDGER_PATH
LEGACY_V1_FIXTURE = PROJECT_ROOT / "tests/fixtures/research/two-layer-strategy-decision-draft-v1-sealed.json"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _fully_resolved_layers() -> tuple[
    LayerOnePendingDecisions,
    LayerTwoPendingDecisions,
    ExecutionPendingDecisions,
]:
    layer_one = LayerOnePendingDecisions(
        objective="absolute_return",
        primary_benchmark="cash",
        cash_asset_scope=["CNY_CASH"],
        etf_asset_scope=[],
        max_acceptable_drawdown=-0.15,
        min_stock_budget=0.0,
        max_stock_budget=1.0,
        risk_budget_levels=[0.0, 0.3, 0.6, 1.0],
        trend_lookback=60,
        volatility_lookback=20,
        volatility_target=0.15,
    )
    layer_two = LayerTwoPendingDecisions(
        pit_industry_source_requirement="auditable_pit_industry_history_v1",
        statistical_risk_cluster_lookback=60,
        statistical_risk_cluster_correlation_threshold=0.7,
        statistical_risk_cluster_max_weight=0.3,
        max_positions_per_cluster=2,
        ownership_proxy_role="diagnostic",
        max_positions=10,
        holding_period_bars=20,
        tranche_count=1,
        rebalance_semantics="fixed_horizon_only",
        exit_semantics="fixed_horizon_exit",
    )
    execution = ExecutionPendingDecisions(
        suspension_holding_day_clock="count_suspended_days",
        delisting_settlement_contract="fail_closed_missing_bar",
        minimum_commission_lot_handling_policy="reject_unaffordable_target_lot",
    )
    return layer_one, layer_two, execution


def _seal_resolved_v1_draft() -> TwoLayerStrategyDecisionDraftV1:
    layer_one, layer_two, execution = _fully_resolved_layers()
    return seal_two_layer_decision_draft(
        TwoLayerStrategyDecisionDraftV1(
            research_trial_ledger_id=BOUND_RESEARCH_TRIAL_LEDGER_ID,
            research_trial_ledger_path=BOUND_RESEARCH_TRIAL_LEDGER_PATH,
            layer_one=layer_one,
            layer_two=layer_two,
            execution=execution,
        )
    )


def test_committed_contract_is_confirmed_v2_not_ready() -> None:
    draft, result = verify_two_layer_decision_draft_file(
        draft_path=COMMITTED_DRAFT,
        repo_root=PROJECT_ROOT,
    )
    assert isinstance(draft, TwoLayerStrategyDecisionContractV2)
    assert draft.schema_version == "2"
    assert draft.contract_version == "two-layer-strategy-decision-v2"
    assert draft.status == "confirmed_for_implementation_but_not_ready"
    assert draft.research_trial_ledger_id == BOUND_RESEARCH_TRIAL_LEDGER_ID
    assert draft.research_trial_ledger_path == BOUND_RESEARCH_TRIAL_LEDGER_PATH
    assert draft.confirmed.initial_cash == CONFIRMED_INITIAL_CASH
    assert draft.consumed_oos.reuse_forbidden is True
    assert draft.ready_for_scoring is False
    assert draft.ready_for_backtest is False
    assert draft.ready_for_trading is False
    assert draft.auto_deploy is False
    assert result.user_decisions_resolved is True
    assert result.pending_user_decision_count == 0
    assert result.resolved is False
    assert len(result.evidence_blockers) == 11
    assert result.research_trial_ledger_binding_ok is True
    assert draft.contract_id == compute_contract_id(draft)
    categories = {b.category for b in draft.evidence_blockers}
    assert "pending_user_decision" not in categories
    assert categories == {
        "pending_factual_source_verification",
        "pending_implementation",
        "pending_development_evidence",
        "future_enhancement",
    }
    assert draft.layer_one.objective == "absolute_return"
    assert draft.layer_one.performance_benchmark.symbol is None
    assert draft.layer_one.risk_state_index.symbol is None
    assert draft.layer_one.cash_asset_scope == ["CNY_CASH"]
    assert draft.layer_one.etf_asset_scope == []
    assert draft.layer_one.risk_budget_levels == [0.0, 0.3, 0.6, 0.9]
    assert draft.layer_two.ownership_proxy_role == "diagnostic"
    assert draft.layer_two.alpha_candidates.weight_selection_status == "pending_development_evidence"
    assert draft.layer_two.alpha_candidates.runnable_strategy_yaml_forbidden_now is True
    assert draft.layer_two.position_sizing.max_positions_by_budget == {"0.3": 3, "0.6": 6, "0.9": 9}
    assert draft.layer_two.position_sizing.absolute_max_positions == 9
    hold = draft.layer_two.tranche_hold
    assert "tranche_count" not in hold.model_dump()
    assert hold.holding_period_market_trading_days == 40
    assert hold.holding_cycle_market_trading_days == 40
    assert hold.max_active_tranches_by_budget == {"0.3": 3, "0.6": 6, "0.9": 9}
    assert hold.absolute_max_active_tranches == 9
    assert hold.active_tranche_count_equals_active_target_position_count is True
    assert hold.one_stock_per_tranche is True
    assert draft.execution.suspension_holding_day_clock == "count_suspended_days"


def test_legacy_v1_sealed_fixture_still_verifies() -> None:
    draft, result = verify_two_layer_decision_draft_file(
        draft_path=LEGACY_V1_FIXTURE,
        repo_root=PROJECT_ROOT,
    )
    assert isinstance(draft, TwoLayerStrategyDecisionDraftV1)
    assert draft.status == "blocked_pending_user_decisions"
    assert result.resolved is False
    assert result.pending_user_decision_count == 25
    assert result.blockers == list(REQUIRED_DECISION_PATHS)
    assert draft.contract_id == compute_contract_id(draft)


def test_contract_hash_is_stable_for_identical_payload() -> None:
    first = build_confirmed_contract_v2()
    second = build_confirmed_contract_v2()
    assert first.contract_id == second.contract_id
    assert first.contract_id == compute_contract_id(first.model_copy(update={"contract_id": None}))


def test_hash_mismatch_fails_for_v1_and_v2() -> None:
    for draft in (build_unresolved_draft(), build_confirmed_contract_v2()):
        broken = draft.model_copy(update={"contract_id": "0" * 64})
        with pytest.raises(ValueError, match="contract_id does not match"):
            verify_two_layer_decision_draft(broken)


def test_tampered_content_fails_self_hash() -> None:
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["confirmed"]["note"] = "tampered note must break self-hash"
    # Keep stale contract_id so hash check fails after successful parse.
    draft = TwoLayerStrategyDecisionContractV2.model_validate(payload)
    with pytest.raises(ValueError, match="contract_id does not match"):
        verify_two_layer_decision_draft(draft)


def test_extra_field_rejected() -> None:
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["unexpected_field"] = "nope"
    with pytest.raises(ValidationError):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)


def test_legacy_tranche_count_field_rejected_on_v2() -> None:
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["layer_two"]["tranche_hold"]["tranche_count"] = 40
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)


def test_active_tranche_caps_must_match_position_sizing() -> None:
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["layer_two"]["tranche_hold"]["max_active_tranches_by_budget"] = {
        "0.3": 3,
        "0.6": 6,
        "0.9": 40,  # cycle length wrongly reused as active count
    }
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError, match="max_active_tranches_by_budget|holding/phase cycle length"):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)

    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["layer_two"]["tranche_hold"]["max_active_tranches_by_budget"] = {
        "0.3": 3,
        "0.6": 6,
        "0.9": 8,  # drift from confirmed 3/6/9 mapping
    }
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError, match="max_active_tranches_by_budget"):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)

    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["layer_two"]["tranche_hold"]["absolute_max_active_tranches"] = 40
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)

    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["layer_two"]["position_sizing"]["max_positions_by_budget"] = {
        "0.3": 4,
        "0.6": 6,
        "0.9": 9,
    }
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError, match="max_positions_by_budget"):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)


def test_ready_flags_cannot_be_true_on_confirmed_contract() -> None:
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    for flag in ("ready_for_scoring", "ready_for_backtest", "ready_for_trading", "auto_deploy"):
        bad = dict(payload)
        bad[flag] = True
        bad.pop("contract_id", None)
        with pytest.raises(ValidationError):
            TwoLayerStrategyDecisionContractV2.model_validate(bad)


def test_wrong_status_with_false_ready_still_rejected() -> None:
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["status"] = "confirmed_for_implementation"
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)


def test_pending_user_decision_blocker_rejected_on_v2() -> None:
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["evidence_blockers"] = [
        {
            "path": "layer_one.objective",
            "category": "pending_user_decision",
            "detail": "should not remain after confirmation",
        },
        *payload["evidence_blockers"],
    ]
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError, match="pending_user_decision"):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)


def test_wrong_blocker_classification_missing_required_categories() -> None:
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    # Drop future_enhancement entirely -> required category missing.
    payload["evidence_blockers"] = [b for b in payload["evidence_blockers"] if b["category"] != "future_enhancement"]
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError, match="missing required categories"):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)


def test_alpha_weight_must_not_be_classified_as_user_decision() -> None:
    blockers = default_evidence_blockers()
    alpha = next(b for b in blockers if b.path == "layer_two.alpha_weight_selection")
    assert alpha.category == "pending_development_evidence"
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["evidence_blockers"] = [
        {
            "path": "layer_two.alpha_weight_selection",
            "category": "pending_user_decision",
            "detail": "wrong classification",
        },
        *[b for b in payload["evidence_blockers"] if b["path"] != "layer_two.alpha_weight_selection"],
    ]
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError, match="pending_user_decision"):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)


def test_missing_must_not_be_treated_as_false_or_zero() -> None:
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    # Guessing a symbol while status is still pending factual verification.
    payload["layer_one"]["performance_benchmark"]["symbol"] = "000985.CSI"
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError, match="pending factual index symbol must remain null"):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)

    # Confirmed symbol status without an actual symbol.
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["layer_one"]["risk_state_index"]["symbol_status"] = "confirmed"
    payload["layer_one"]["risk_state_index"]["symbol"] = None
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError, match="confirmed index symbol cannot be null"):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)

    # Ownership / negative-list missing must stay unknown, not coerce to false miss.
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["layer_two"]["ownership_missing_stays_unknown"] = False
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)

    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["layer_two"]["financial_negative_list"]["missing_stays_unknown_and_is_not_a_miss"] = False
    payload.pop("contract_id", None)
    with pytest.raises(ValidationError):
        TwoLayerStrategyDecisionContractV2.model_validate(payload)

    # Null/unknown list masquerade as empty string is rejected on v1 pending fields.
    with pytest.raises(ValidationError, match="null when unknown"):
        LayerOnePendingDecisions(primary_benchmark="")


def test_out_of_range_and_min_gt_max_fail() -> None:
    with pytest.raises(ValidationError):
        LayerOnePendingDecisions(max_acceptable_drawdown=-1.5)
    with pytest.raises(ValidationError):
        LayerOnePendingDecisions(min_stock_budget=0.8, max_stock_budget=0.2)
    with pytest.raises(ValidationError):
        LayerOnePendingDecisions(
            min_stock_budget=0.0,
            max_stock_budget=1.0,
            risk_budget_levels=[0.0, 0.5, 0.4],
        )


def test_v1_unknown_lists_must_be_null_not_empty_in_fixture() -> None:
    draft = load_two_layer_decision_draft(LEGACY_V1_FIXTURE)
    assert isinstance(draft, TwoLayerStrategyDecisionDraftV1)
    assert draft.layer_one.cash_asset_scope is None
    assert draft.layer_one.etf_asset_scope is None
    assert draft.layer_one.risk_budget_levels is None


def test_fully_populated_v1_draft_can_resolve() -> None:
    draft = _seal_resolved_v1_draft()
    result = verify_two_layer_decision_draft(draft)
    assert result.resolved is True
    assert result.blockers == []
    assert collect_decision_blockers(draft) == []


def test_blocker_order_is_deterministic_for_v1() -> None:
    draft = build_unresolved_draft()
    assert collect_decision_blockers(draft) == list(REQUIRED_DECISION_PATHS)
    assert collect_decision_blockers(draft) == collect_decision_blockers(draft)


def test_write_and_reload_roundtrip_v2(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    sealed = write_two_layer_decision_draft(path, build_confirmed_contract_v2())
    loaded = load_two_layer_decision_draft(path)
    assert loaded.contract_id == sealed.contract_id
    result = verify_two_layer_decision_draft(loaded)
    assert result.pending_user_decision_count == 0
    assert result.user_decisions_resolved is True
    assert result.resolved is False


def test_v2_overall_resolved_formula_is_fail_closed() -> None:
    blocker = CategorizedBlocker(
        path="example.path",
        category="pending_implementation",
        detail="synthetic",
    )
    # Non-empty evidence blockers => overall unresolved even if ready flags were true.
    assert (
        compute_two_layer_v2_overall_resolved(
            evidence_blockers=[blocker],
            status="confirmed_and_ready",
            ready_for_scoring=True,
            ready_for_backtest=True,
            ready_for_trading=True,
        )
        is False
    )
    # Empty blockers but not-ready status => overall unresolved.
    assert (
        compute_two_layer_v2_overall_resolved(
            evidence_blockers=[],
            status="confirmed_for_implementation_but_not_ready",
            ready_for_scoring=False,
            ready_for_backtest=False,
            ready_for_trading=False,
        )
        is False
    )
    # Empty blockers + ready flags still false + non-not-ready status => still unresolved.
    assert (
        compute_two_layer_v2_overall_resolved(
            evidence_blockers=[],
            status="confirmed_for_implementation",
            ready_for_scoring=False,
            ready_for_backtest=True,
            ready_for_trading=True,
        )
        is False
    )
    # Conservative true only when blockers empty, status not not-ready/blocked, and all ready.
    assert (
        compute_two_layer_v2_overall_resolved(
            evidence_blockers=[],
            status="confirmed_and_ready",
            ready_for_scoring=True,
            ready_for_backtest=True,
            ready_for_trading=True,
        )
        is True
    )


def test_v2_verification_does_not_change_contract_hash() -> None:
    draft = build_confirmed_contract_v2()
    before = draft.contract_id
    result = verify_two_layer_decision_draft(draft)
    assert draft.contract_id == before
    assert result.contract_id == before
    assert compute_contract_id(draft) == before
    assert result.resolved is False
    assert result.user_decisions_resolved is True


def test_migrate_unresolved_v1_to_confirmed_v2() -> None:
    migrated = migrate_decision_contract_v1_to_v2(build_unresolved_draft())
    assert migrated.status == "confirmed_for_implementation_but_not_ready"
    result = verify_two_layer_decision_draft(migrated)
    assert result.pending_user_decision_count == 0
    assert result.user_decisions_resolved is True
    assert result.resolved is False


def test_migrate_partial_v1_rejected() -> None:
    layer_one, layer_two, execution = _fully_resolved_layers()
    partial = seal_two_layer_decision_draft(
        TwoLayerStrategyDecisionDraftV1(
            research_trial_ledger_id=BOUND_RESEARCH_TRIAL_LEDGER_ID,
            research_trial_ledger_path=BOUND_RESEARCH_TRIAL_LEDGER_PATH,
            layer_one=layer_one.model_copy(update={"objective": None}),
            layer_two=layer_two,
            execution=execution,
        )
    )
    with pytest.raises(ValueError, match="partially filled"):
        migrate_decision_contract_v1_to_v2(partial)


def test_wrong_ledger_id_rejected_by_object_verifier() -> None:
    draft = build_confirmed_contract_v2()
    wrong = seal_two_layer_decision_draft(
        draft.model_copy(update={"research_trial_ledger_id": "a" * 64, "contract_id": None})
    )
    with pytest.raises(ValueError, match="does not match bound research trial ledger"):
        verify_two_layer_decision_draft(wrong)


def test_ledger_path_escape_and_unknown_rejected() -> None:
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload.pop("contract_id", None)
    for bad_path in ("", "../secrets/ledger.json", "/etc/passwd", "config/research/other-ledger.json"):
        payload["research_trial_ledger_path"] = bad_path
        with pytest.raises(ValidationError):
            TwoLayerStrategyDecisionContractV2.model_validate(payload)


def test_file_verifier_rejects_missing_ledger(tmp_path: Path) -> None:
    draft_path = tmp_path / "draft.json"
    write_two_layer_decision_draft(draft_path, build_confirmed_contract_v2())
    with pytest.raises(ValueError, match="does not exist"):
        verify_two_layer_decision_draft_file(draft_path=draft_path, repo_root=tmp_path)


def test_file_verifier_rejects_tampered_ledger_hash(tmp_path: Path) -> None:
    ledger_payload = json.loads(COMMITTED_LEDGER.read_text(encoding="utf-8"))
    ledger_payload["ledger_id"] = "0" * 64
    ledger_dest = tmp_path / BOUND_RESEARCH_TRIAL_LEDGER_PATH
    _write_json(ledger_dest, ledger_payload)
    draft_path = tmp_path / "draft.json"
    write_two_layer_decision_draft(draft_path, build_confirmed_contract_v2())
    with pytest.raises(ValueError, match="ledger_id does not match"):
        verify_two_layer_decision_draft_file(draft_path=draft_path, repo_root=tmp_path)


def test_file_verifier_binds_real_ledger_content(tmp_path: Path) -> None:
    draft_path = tmp_path / "draft.json"
    write_two_layer_decision_draft(draft_path, build_confirmed_contract_v2())
    draft, result = verify_two_layer_decision_draft_file(
        draft_path=draft_path,
        repo_root=PROJECT_ROOT,
    )
    assert draft.research_trial_ledger_id == BOUND_RESEARCH_TRIAL_LEDGER_ID
    assert result.research_trial_ledger_binding_ok is True
    assert result.pending_user_decision_count == 0


def test_empty_defensive_assets_with_low_stock_budget_fail() -> None:
    with pytest.raises(ValidationError, match="cash_asset_scope or etf_asset_scope"):
        LayerOnePendingDecisions(
            cash_asset_scope=[],
            etf_asset_scope=[],
            min_stock_budget=0.0,
            max_stock_budget=1.0,
            risk_budget_levels=[0.0, 1.0],
        )


def test_future_seen_windows_rejected() -> None:
    contract = build_confirmed_contract_v2(confirmation_as_of=date(2023, 1, 1))
    with pytest.raises(ValueError, match="after reference_date"):
        verify_two_layer_decision_draft(contract)


def test_compose_happy_path_diagnostic_only() -> None:
    layer_one = LayerOneBudgetDecision(
        as_of=date(2024, 6, 28),
        contract_id="c" * 64,
        data_evidence_id="d" * 64,
        config_evidence_id="f" * 64,
        stock_budget=0.6,
        cash_budget=0.3,
        etf_budget=0.1,
        etf_symbol="510300.SH",
    )
    layer_two = LayerTwoStockSleeve(
        as_of=date(2024, 6, 28),
        contract_id="c" * 64,
        data_evidence_id="d" * 64,
        config_evidence_id="f" * 64,
        target_weights=[
            StockTargetWeight(symbol="000001.SZ", weight=0.4),
            StockTargetWeight(symbol="600000.SH", weight=0.6),
        ],
    )
    composed = compose_two_layer_portfolio(layer_one=layer_one, layer_two=layer_two)
    assert composed.diagnostic_only is True
    assert composed.ready_for_orders is False
    assert composed.ready_for_trading is False


def test_cli_verify_confirmed_two_layer_decision_contract() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "verify-two-layer-decision-contract",
            "--draft-file",
            str(COMMITTED_DRAFT),
            "--repo-root",
            str(PROJECT_ROOT),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "schema_version=2" in result.stdout
    assert "contract_version=two-layer-strategy-decision-v2" in result.stdout
    assert "status=confirmed_for_implementation_but_not_ready" in result.stdout
    assert "user_decisions_resolved=true" in result.stdout
    assert "pending_user_decision_count=0" in result.stdout
    assert "resolved=false" in result.stdout
    assert "evidence_blocker=pending_factual_source_verification:" in result.stdout
    assert "evidence_blocker=pending_implementation:" in result.stdout
    assert "evidence_blocker=pending_development_evidence:" in result.stdout
    assert "evidence_blocker=future_enhancement:" in result.stdout
    assert f"research_trial_ledger_path={BOUND_RESEARCH_TRIAL_LEDGER_PATH}" in result.stdout
    assert f"research_trial_ledger_id={BOUND_RESEARCH_TRIAL_LEDGER_ID}" in result.stdout
    assert "research_trial_ledger_binding_ok=true" in result.stdout
    assert "confirmed_initial_cash=80000" in result.stdout
    assert "initial_cash_is_blocker=false" in result.stdout
    assert "does_not_score=true" in result.stdout
    assert "does_not_backtest=true" in result.stdout
    assert "does_not_trade=true" in result.stdout
    assert "ready_for_scoring=false" in result.stdout
    assert "ready_for_backtest=false" in result.stdout
    assert "ready_for_trading=false" in result.stdout
    assert "auto_deploy=false" in result.stdout


def test_cli_verify_legacy_v1_fixture() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "verify-two-layer-decision-contract",
            "--draft-file",
            str(LEGACY_V1_FIXTURE),
            "--repo-root",
            str(PROJECT_ROOT),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "schema_version=1" in result.stdout
    assert "status=blocked_pending_user_decisions" in result.stdout
    assert "pending_user_decision_count=25" in result.stdout
    assert "resolved=false" in result.stdout
    assert "blocker=layer_one.objective" in result.stdout


def test_cli_verify_rejects_tampered_hash(tmp_path: Path) -> None:
    payload = json.loads(COMMITTED_DRAFT.read_text(encoding="utf-8"))
    payload["contract_id"] = "0" * 64
    path = tmp_path / "tampered.json"
    _write_json(path, payload)
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "verify-two-layer-decision-contract",
            "--draft-file",
            str(path),
            "--repo-root",
            str(PROJECT_ROOT),
        ],
    )
    assert result.exit_code == 1
    assert "contract_id" in (result.stdout + result.stderr)


# Keep alias import exercised for older callers.
def test_draft_alias_still_points_to_v1() -> None:
    assert TwoLayerStrategyDecisionDraft is TwoLayerStrategyDecisionDraftV1
