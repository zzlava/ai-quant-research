"""Attack-oriented tests for hypothetical position lifecycle open record (E10f-1)."""

from __future__ import annotations

import ast
import json
import math
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.research.layer_two_entry_execution_diagnostic import (
    LayerTwoEntryExecutionVerificationResult,
    diagnose_layer_two_entry_execution,
)
from app.research.layer_two_hypothetical_position_lifecycle import (
    BOUND_BOARD_LOT,
    BOUND_HOLDING_MARKET_BARS_ELAPSED_AT_OPEN,
    LayerTwoHypotheticalLifecycleFileBindings,
    LayerTwoHypotheticalLifecycleFileInput,
    LayerTwoHypotheticalLifecycleStructuralInput,
    LayerTwoHypotheticalPositionLifecycleRecord,
    LayerTwoHypotheticalPositionLifecycleVerificationResult,
    assert_record_self_hash,
    open_layer_two_hypothetical_position_lifecycle,
    seal_layer_two_hypothetical_position_lifecycle_record,
    verify_layer_two_hypothetical_position_lifecycle_record,
    verify_layer_two_hypothetical_position_lifecycle_record_file,
    write_layer_two_hypothetical_position_lifecycle_record,
)
from app.research.layer_two_tranche_phase_schedule import write_layer_two_tranche_phase_schedule_report
from tests.helpers import PROJECT_ROOT
from tests.test_layer_two_entry_execution_diagnostic import (
    FIXTURE_T1_OPEN,
    FIXTURE_T1_UP_LIMIT,
    _obs,
    _t1_bundle_inputs,
)

REPO_ROOT = PROJECT_ROOT
MODULE_PATH = REPO_ROOT / "src/app/research/layer_two_hypothetical_position_lifecycle.py"


def _fillable_bundle():
    bundle, eligibility, financials, cluster, constraint, state, ranking, allocator, t1 = _t1_bundle_inputs()
    entry = allocator.proposed_entry
    assert entry is not None
    observation = _obs(
        symbol=entry.symbol,
        execution_date=t1,
        snapshot=allocator.market_data_snapshot_id,
        status="tradable",
        raw_open=FIXTURE_T1_OPEN,
        up_limit=FIXTURE_T1_UP_LIMIT,
    )
    report = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=observation,
    )
    assert report.outcome == "hypothetically_fillable"
    structural = LayerTwoHypotheticalLifecycleStructuralInput(
        entry_execution_report=report,
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=observation,
    )
    return bundle, eligibility, financials, cluster, structural, report


def _file_input(structural, *, eligibility, financials, cluster, bundle, phase_path: Path):
    return LayerTwoHypotheticalLifecycleFileInput(
        structural=structural,
        file_bindings=LayerTwoHypotheticalLifecycleFileBindings(
            eligibility_report=eligibility,
            financial_reports=tuple(financials),
            cluster_report=cluster,
            store=bundle.store,
            repo_root=REPO_ROOT,
            phase_report_path=phase_path,
        ),
    )


def test_open_fillable_happy_path_and_structural_bindings_false() -> None:
    bundle, eligibility, financials, cluster, structural, entry = _fillable_bundle()
    record = open_layer_two_hypothetical_position_lifecycle(
        entry_execution_report=structural.entry_execution_report,
        allocator_report=structural.allocator_report,
        constraint_report=structural.constraint_report,
        current_state=structural.current_state,
        ranking=structural.ranking,
        phase_report=structural.phase_report,
        execution_observation=structural.execution_observation,
    )
    base = entry.base_scenario
    assert base is not None
    proposed = structural.allocator_report.proposed_entry
    assert proposed is not None
    assert record.record_id is not None
    assert record.lifecycle_status == "hypothetical_open"
    assert record.holding_market_bars_elapsed == BOUND_HOLDING_MARKET_BARS_ELAPSED_AT_OPEN == 1
    assert record.entry_market_day_counts_as_holding_bar_one is True
    assert (
        record.entry_trade_date == entry.expected_t1_execution_date == structural.execution_observation.execution_date
    )
    assert record.shares == base.affordable_shares
    assert record.board_lots == base.affordable_lots
    assert record.shares % BOUND_BOARD_LOT == 0
    assert record.hypothetical_entry_price == base.hypothetical_fill_price
    assert record.stock_notional == base.stock_notional
    assert record.buy_commission == base.commission
    assert record.entry_total_cash_used == base.total_cash_used
    assert record.unused_target_cash == base.unused_target_cash
    assert record.entry_cost_basis_total == record.entry_total_cash_used
    assert abs(record.entry_cost_basis_per_share - record.entry_cost_basis_total / record.shares) <= 1e-9
    assert record.symbol == proposed.symbol
    assert record.tranche_id == proposed.tranche_id
    assert record.cluster_id == proposed.cluster_id
    assert record.ranking_position == proposed.ranking_position
    assert record.entry_execution_report_id == entry.report_id
    assert record.allocator_report_id == structural.allocator_report.report_id
    assert record.research_only is True
    assert record.hypothetical_not_fill is True
    assert record.diagnostic_only is True
    assert record.post_decision_label_only is True
    assert record.ready_for_lifecycle_diagnostic is False
    assert record.ready_for_scoring is False
    assert record.ready_for_backtest is False
    assert record.ready_for_portfolio_construction is False
    assert record.ready_for_orders is False
    assert record.ready_for_trading is False
    assert record.auto_apply is False
    field_names = set(LayerTwoHypotheticalPositionLifecycleRecord.model_fields)
    assert not {"exit_due", "exit_date", "exit_price", "mark_price", "unrealized_pnl", "realized_pnl"} & field_names
    structural_verify = verify_layer_two_hypothetical_position_lifecycle_record(record, structural=structural)
    assert structural_verify.structural_ok is True
    assert structural_verify.entry_execution_binding_ok is False
    assert structural_verify.allocator_binding_ok is False
    assert structural_verify.phase_binding_ok is False
    assert structural_verify.tranche_evaluation_protocol_binding_ok is False
    assert structural_verify.ready_for_lifecycle_diagnostic is False
    _ = bundle, eligibility, financials, cluster


def test_non_fillable_outcomes_fail_closed() -> None:
    bundle, eligibility, financials, cluster, constraint, state, ranking, allocator, t1 = _t1_bundle_inputs()
    entry = allocator.proposed_entry
    assert entry is not None
    for status, raw, up, expected in (
        ("unknown", None, None, "unknown_execution_observation"),
        ("known_full_day_suspension", None, None, "blocked_suspension"),
        ("tradable", 11.0, 11.0, "blocked_limit_up"),
        ("tradable", 200.0, 220.0, "unaffordable_board_lot_or_minimum_commission"),
    ):
        observation = _obs(
            symbol=entry.symbol,
            execution_date=t1,
            snapshot=allocator.market_data_snapshot_id,
            status=status,
            raw_open=raw,
            up_limit=up,
        )
        report = diagnose_layer_two_entry_execution(
            allocator_report=allocator,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            phase_report=bundle.phase,
            execution_observation=observation,
        )
        assert report.outcome == expected
        with pytest.raises(ValueError, match="hypothetically_fillable"):
            open_layer_two_hypothetical_position_lifecycle(
                entry_execution_report=report,
                allocator_report=allocator,
                constraint_report=constraint,
                current_state=state,
                ranking=ranking,
                phase_report=bundle.phase,
                execution_observation=observation,
            )
    _ = eligibility, financials, cluster


def test_stress_scenario_must_not_masquerade_as_base_amounts() -> None:
    _bundle, _e, _f, _c, structural, entry = _fillable_bundle()
    assert entry.stress_scenario is not None and entry.base_scenario is not None
    record = open_layer_two_hypothetical_position_lifecycle(
        entry_execution_report=structural.entry_execution_report,
        allocator_report=structural.allocator_report,
        constraint_report=structural.constraint_report,
        current_state=structural.current_state,
        ranking=structural.ranking,
        phase_report=structural.phase_report,
        execution_observation=structural.execution_observation,
    )
    stress = entry.stress_scenario
    assert record.shares == entry.base_scenario.affordable_shares
    if stress.affordable_shares != entry.base_scenario.affordable_shares:
        assert record.shares != stress.affordable_shares
    payload = record.model_dump(mode="json")
    payload["shares"] = int(stress.affordable_shares) if stress.affordable_shares > 0 else record.shares + 100
    payload["board_lots"] = payload["shares"] // 100
    payload["hypothetical_entry_price"] = float(stress.hypothetical_fill_price)
    payload["stock_notional"] = float(stress.stock_notional)
    payload["buy_commission"] = float(stress.commission)
    payload["entry_total_cash_used"] = float(stress.total_cash_used)
    payload["unused_target_cash"] = float(record.target_notional - stress.total_cash_used)
    payload["entry_cost_basis_total"] = float(stress.total_cash_used)
    payload["entry_cost_basis_per_share"] = float(stress.total_cash_used) / float(payload["shares"])
    payload.pop("record_id", None)
    resealed = seal_layer_two_hypothetical_position_lifecycle_record(
        LayerTwoHypotheticalPositionLifecycleRecord.model_validate(payload)
    )
    assert_record_self_hash(resealed)
    with pytest.raises(ValueError, match="recompute|canonical payload"):
        verify_layer_two_hypothetical_position_lifecycle_record(resealed, structural=structural)


def test_outer_reseal_amount_id_date_holding_bar_rejected() -> None:
    _bundle, _e, _f, _c, structural, _entry = _fillable_bundle()
    record = open_layer_two_hypothetical_position_lifecycle(
        entry_execution_report=structural.entry_execution_report,
        allocator_report=structural.allocator_report,
        constraint_report=structural.constraint_report,
        current_state=structural.current_state,
        ranking=structural.ranking,
        phase_report=structural.phase_report,
        execution_observation=structural.execution_observation,
    )

    for field, value in (
        ("shares", record.shares + 100),
        ("hypothetical_entry_price", record.hypothetical_entry_price + 0.01),
        ("buy_commission", record.buy_commission + 1.0),
        ("unused_target_cash", record.unused_target_cash + 1.0),
        ("entry_cost_basis_total", record.entry_cost_basis_total + 1.0),
        ("holding_market_bars_elapsed", 2),
        ("tranche_id", record.tranche_id + 1),
        ("cluster_id", "tampered-cluster"),
        ("market_data_snapshot_id", "snap-tampered"),
        ("entry_trade_date", (record.entry_trade_date + timedelta(days=1)).isoformat()),
        ("entry_execution_report_id", "ab" * 32),
        ("allocator_report_id", "cd" * 32),
        ("phase_report_id", "ef" * 32),
        ("current_state_id", "11" * 32),
        ("constraint_assembler_report_id", "22" * 32),
    ):
        payload = record.model_dump(mode="json")
        payload[field] = value
        if field == "shares":
            payload["board_lots"] = payload["shares"] // 100
            payload["entry_cost_basis_per_share"] = payload["entry_cost_basis_total"] / payload["shares"]
        if field == "entry_cost_basis_total":
            payload["entry_total_cash_used"] = value
            payload["unused_target_cash"] = payload["target_notional"] - value
            payload["entry_cost_basis_per_share"] = value / payload["shares"]
            payload["stock_notional"] = value - payload["buy_commission"]
        if field == "unused_target_cash":
            payload["entry_total_cash_used"] = payload["target_notional"] - value
            payload["entry_cost_basis_total"] = payload["entry_total_cash_used"]
            payload["entry_cost_basis_per_share"] = payload["entry_total_cash_used"] / payload["shares"]
            payload["stock_notional"] = payload["entry_total_cash_used"] - payload["buy_commission"]
        if field == "buy_commission":
            payload["entry_total_cash_used"] = payload["stock_notional"] + value
            payload["entry_cost_basis_total"] = payload["entry_total_cash_used"]
            payload["unused_target_cash"] = payload["target_notional"] - payload["entry_total_cash_used"]
            payload["entry_cost_basis_per_share"] = payload["entry_total_cash_used"] / payload["shares"]
        if field == "holding_market_bars_elapsed":
            payload.pop("record_id", None)
            with pytest.raises(ValidationError, match="holding_market_bars|Literal"):
                LayerTwoHypotheticalPositionLifecycleRecord.model_validate(payload)
            continue
        payload.pop("record_id", None)
        try:
            resealed = seal_layer_two_hypothetical_position_lifecycle_record(
                LayerTwoHypotheticalPositionLifecycleRecord.model_validate(payload)
            )
        except ValidationError:
            continue
        assert_record_self_hash(resealed)
        with pytest.raises(ValueError, match="recompute|canonical payload|report_id|record_id"):
            verify_layer_two_hypothetical_position_lifecycle_record(resealed, structural=structural)

    missing_id = record.model_copy(update={"record_id": None})
    with pytest.raises(ValueError, match="record_id is missing"):
        verify_layer_two_hypothetical_position_lifecycle_record(missing_id, structural=structural)
    bad_hash = record.model_copy(update={"record_id": "ab" * 32})
    with pytest.raises(ValueError, match="record_id"):
        verify_layer_two_hypothetical_position_lifecycle_record(bad_hash, structural=structural)


def test_non_lot_zero_share_bool_nan_inf_rejected() -> None:
    _bundle, _e, _f, _c, structural, _entry = _fillable_bundle()
    record = open_layer_two_hypothetical_position_lifecycle(
        entry_execution_report=structural.entry_execution_report,
        allocator_report=structural.allocator_report,
        constraint_report=structural.constraint_report,
        current_state=structural.current_state,
        ranking=structural.ranking,
        phase_report=structural.phase_report,
        execution_observation=structural.execution_observation,
    )
    base_payload = record.model_dump(mode="json")
    base_payload.pop("record_id", None)

    odd_lot = json.loads(json.dumps(base_payload))
    odd_lot["shares"] = 150
    odd_lot["board_lots"] = 1
    with pytest.raises(ValidationError, match="board lot|board_lots|shares"):
        LayerTwoHypotheticalPositionLifecycleRecord.model_validate(odd_lot)

    zero = json.loads(json.dumps(base_payload))
    zero["shares"] = 0
    zero["board_lots"] = 0
    with pytest.raises(ValidationError, match="shares|board_lots|positive|minimum"):
        LayerTwoHypotheticalPositionLifecycleRecord.model_validate(zero)

    for field, bad in (
        ("hypothetical_entry_price", True),
        ("buy_commission", math.nan),
        ("stock_notional", math.inf),
        ("unused_target_cash", -1.0),
    ):
        bad_payload = json.loads(json.dumps(base_payload))
        bad_payload[field] = bad
        with pytest.raises(ValidationError):
            LayerTwoHypotheticalPositionLifecycleRecord.model_validate(bad_payload)


def test_record_ready_true_tamper_rejected() -> None:
    _bundle, _e, _f, _c, structural, _entry = _fillable_bundle()
    record = open_layer_two_hypothetical_position_lifecycle(
        entry_execution_report=structural.entry_execution_report,
        allocator_report=structural.allocator_report,
        constraint_report=structural.constraint_report,
        current_state=structural.current_state,
        ranking=structural.ranking,
        phase_report=structural.phase_report,
        execution_observation=structural.execution_observation,
    )
    payload = record.model_dump(mode="json")
    payload["ready_for_lifecycle_diagnostic"] = True
    payload.pop("record_id", None)
    with pytest.raises(ValidationError, match="ready_for_lifecycle_diagnostic"):
        LayerTwoHypotheticalPositionLifecycleRecord.model_validate(payload)


def test_verification_result_state_machine_rejects_inconsistent_shapes() -> None:
    rid = "ab" * 32
    # Legal structural-path shape.
    LayerTwoHypotheticalPositionLifecycleVerificationResult(
        record_id=rid,
        structural_ok=True,
        entry_execution_binding_ok=False,
        allocator_binding_ok=False,
        phase_binding_ok=False,
        tranche_evaluation_protocol_binding_ok=False,
        ready_for_lifecycle_diagnostic=False,
    )
    # Legal file-path shape (shape only — not provenance; only real file verifier output is authoritative).
    LayerTwoHypotheticalPositionLifecycleVerificationResult(
        record_id=rid,
        structural_ok=True,
        entry_execution_binding_ok=True,
        allocator_binding_ok=True,
        phase_binding_ok=True,
        tranche_evaluation_protocol_binding_ok=True,
        ready_for_lifecycle_diagnostic=True,
    )
    # structural_ok=false with all clear is allowed as a failed-shape container.
    LayerTwoHypotheticalPositionLifecycleVerificationResult(
        record_id=rid,
        structural_ok=False,
        entry_execution_binding_ok=False,
        allocator_binding_ok=False,
        phase_binding_ok=False,
        tranche_evaluation_protocol_binding_ok=False,
        ready_for_lifecycle_diagnostic=False,
    )

    with pytest.raises(ValidationError, match="partial disk bindings|forbidden"):
        LayerTwoHypotheticalPositionLifecycleVerificationResult(
            record_id=rid,
            structural_ok=True,
            entry_execution_binding_ok=True,
            allocator_binding_ok=False,
            phase_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
            ready_for_lifecycle_diagnostic=False,
        )
    with pytest.raises(ValidationError, match="ready_for_lifecycle_diagnostic=true requires"):
        LayerTwoHypotheticalPositionLifecycleVerificationResult(
            record_id=rid,
            structural_ok=True,
            entry_execution_binding_ok=False,
            allocator_binding_ok=False,
            phase_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
            ready_for_lifecycle_diagnostic=True,
        )
    with pytest.raises(ValidationError, match="all four bindings true requires ready"):
        LayerTwoHypotheticalPositionLifecycleVerificationResult(
            record_id=rid,
            structural_ok=True,
            entry_execution_binding_ok=True,
            allocator_binding_ok=True,
            phase_binding_ok=True,
            tranche_evaluation_protocol_binding_ok=True,
            ready_for_lifecycle_diagnostic=False,
        )
    with pytest.raises(ValidationError, match="structural_ok=false forbids"):
        LayerTwoHypotheticalPositionLifecycleVerificationResult(
            record_id=rid,
            structural_ok=False,
            entry_execution_binding_ok=True,
            allocator_binding_ok=True,
            phase_binding_ok=True,
            tranche_evaluation_protocol_binding_ok=True,
            ready_for_lifecycle_diagnostic=True,
        )
    with pytest.raises(ValidationError, match="structural_ok=false forbids"):
        LayerTwoHypotheticalPositionLifecycleVerificationResult(
            record_id=rid,
            structural_ok=False,
            entry_execution_binding_ok=True,
            allocator_binding_ok=False,
            phase_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
            ready_for_lifecycle_diagnostic=False,
        )


def test_structural_path_must_not_claim_disk_ready(tmp_path: Path) -> None:
    bundle, eligibility, financials, cluster, structural, _entry = _fillable_bundle()
    record = open_layer_two_hypothetical_position_lifecycle(
        entry_execution_report=structural.entry_execution_report,
        allocator_report=structural.allocator_report,
        constraint_report=structural.constraint_report,
        current_state=structural.current_state,
        ranking=structural.ranking,
        phase_report=structural.phase_report,
        execution_observation=structural.execution_observation,
    )
    result = verify_layer_two_hypothetical_position_lifecycle_record(record, structural=structural)
    assert result.ready_for_lifecycle_diagnostic is False
    assert result.entry_execution_binding_ok is False
    assert result.allocator_binding_ok is False
    assert result.phase_binding_ok is False
    assert result.tranche_evaluation_protocol_binding_ok is False

    # File-path shape is model-legal but is not provenance; only the real file verifier is.
    shape_only_file_path = LayerTwoHypotheticalPositionLifecycleVerificationResult(
        record_id=record.record_id or ("ab" * 32),
        structural_ok=True,
        entry_execution_binding_ok=True,
        allocator_binding_ok=True,
        phase_binding_ok=True,
        tranche_evaluation_protocol_binding_ok=True,
        ready_for_lifecycle_diagnostic=True,
    )
    assert shape_only_file_path.ready_for_lifecycle_diagnostic is True
    again = verify_layer_two_hypothetical_position_lifecycle_record(record, structural=structural)
    assert again.ready_for_lifecycle_diagnostic is False
    assert again.model_dump() != shape_only_file_path.model_dump()

    missing = tmp_path / "missing-phase.json"
    file_input = _file_input(
        structural,
        eligibility=eligibility,
        financials=financials,
        cluster=cluster,
        bundle=bundle,
        phase_path=missing,
    )
    with pytest.raises(ValueError, match="phase report file missing|phase"):
        verify_layer_two_hypothetical_position_lifecycle_record_file(record=record, file_input=file_input)


def test_file_verifier_requires_e10e0_bindings_and_rebuilds_ready(tmp_path: Path) -> None:
    bundle, eligibility, financials, cluster, structural, _entry = _fillable_bundle()
    record = open_layer_two_hypothetical_position_lifecycle(
        entry_execution_report=structural.entry_execution_report,
        allocator_report=structural.allocator_report,
        constraint_report=structural.constraint_report,
        current_state=structural.current_state,
        ranking=structural.ranking,
        phase_report=structural.phase_report,
        execution_observation=structural.execution_observation,
    )
    phase_path = tmp_path / "phase.json"
    write_layer_two_tranche_phase_schedule_report(phase_path, bundle.phase)
    file_input = _file_input(
        structural,
        eligibility=eligibility,
        financials=financials,
        cluster=cluster,
        bundle=bundle,
        phase_path=phase_path,
    )
    result = verify_layer_two_hypothetical_position_lifecycle_record_file(record=record, file_input=file_input)
    assert result.structural_ok is True
    assert result.entry_execution_binding_ok is True
    assert result.allocator_binding_ok is True
    assert result.phase_binding_ok is True
    assert result.tranche_evaluation_protocol_binding_ok is True
    assert result.ready_for_lifecycle_diagnostic is True
    assert result.ready_for_scoring is False
    assert result.ready_for_orders is False

    def _fake_e10e0(**kwargs):
        # Legal structural-path shape; file path must reject missing bindings (incl. observation).
        return LayerTwoEntryExecutionVerificationResult(
            report_id=kwargs["report"].report_id or ("ab" * 32),
            structural_ok=True,
            allocator_binding_ok=False,
            phase_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
            execution_observation_binding_ok=False,
        )

    with patch(
        "app.research.layer_two_hypothetical_position_lifecycle.verify_layer_two_entry_execution_diagnostic_report_file",
        side_effect=_fake_e10e0,
    ):
        with pytest.raises(ValueError, match="observation|allocator|structural_ok/allocator"):
            verify_layer_two_hypothetical_position_lifecycle_record_file(record=record, file_input=file_input)

    def _fake_e10e0_wrong_report_id(**kwargs):
        return LayerTwoEntryExecutionVerificationResult(
            report_id="ff" * 32,
            structural_ok=True,
            allocator_binding_ok=True,
            phase_binding_ok=True,
            tranche_evaluation_protocol_binding_ok=True,
            execution_observation_binding_ok=True,
        )

    with patch(
        "app.research.layer_two_hypothetical_position_lifecycle.verify_layer_two_entry_execution_diagnostic_report_file",
        side_effect=_fake_e10e0_wrong_report_id,
    ):
        with pytest.raises(ValueError, match="report_id must equal record.entry_execution_report_id"):
            verify_layer_two_hypothetical_position_lifecycle_record_file(record=record, file_input=file_input)

    bad_path = tmp_path / "not-the-phase.json"
    bad_path.write_text("{}\n", encoding="utf-8")
    bad_input = _file_input(
        structural,
        eligibility=eligibility,
        financials=financials,
        cluster=cluster,
        bundle=bundle,
        phase_path=bad_path,
    )
    with pytest.raises(ValueError):
        verify_layer_two_hypothetical_position_lifecycle_record_file(record=record, file_input=bad_input)

    out = tmp_path / "lifecycle.json"
    write_layer_two_hypothetical_position_lifecycle_record(out, record)
    assert out.exists()


def test_no_production_imports() -> None:
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
        "app.api",
        "app.cli",
        "app.persistence",
        "app.strategies",
        "app.backtest.engine",
    )
    for module in imported:
        assert not any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_prefixes)
    assert "ScoringEngine" not in source
    assert "BacktestEngine" not in source
    assert "seal_layer_two_stateful_portfolio_state" not in source
    assert "allocate_layer_two_stateful" not in source
