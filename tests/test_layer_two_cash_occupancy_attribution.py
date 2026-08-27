"""Attack-oriented tests for layer-two cash-occupancy attribution (E10e-1)."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.research.layer_two_candidate_eligibility import evaluate_layer_two_candidate_eligibility
from app.research.layer_two_cash_occupancy_attribution import (
    BOUND_OCCUPANCY_CAUSES,
    CashOccupancyCauseSummary,
    CashOccupancyRowAttribution,
    LayerTwoCashOccupancyAttributionReport,
    LayerTwoCashOccupancyAttributionVerificationResult,
    LayerTwoCashOccupancyFileRowBindings,
    LayerTwoCashOccupancyFileRowInput,
    LayerTwoCashOccupancyStructuralRowInput,
    attribute_layer_two_cash_occupancy,
    verify_layer_two_cash_occupancy_attribution_report,
    verify_layer_two_cash_occupancy_attribution_report_file,
    write_layer_two_cash_occupancy_attribution_report,
)
from app.research.layer_two_constraint_assembler import assemble_layer_two_constraints
from app.research.layer_two_entry_execution_diagnostic import (
    LayerTwoEntryExecutionObservation,
    diagnose_layer_two_entry_execution,
)
from app.research.layer_two_stateful_allocator import (
    LayerTwoActiveTranchePosition,
    LayerTwoStatefulPortfolioState,
    UnvalidatedDevelopmentRankingInput,
    allocate_layer_two_stateful_single_opportunity,
    seal_layer_two_stateful_portfolio_state,
)
from app.research.layer_two_statistical_risk_clusters import diagnose_layer_two_statistical_risk_clusters
from app.research.layer_two_tranche_phase_schedule import (
    plan_layer_two_tranche_phase_schedule,
    write_layer_two_tranche_phase_schedule_report,
)
from app.research.tranche_evaluation_protocol import CONFIRMED_CASH_OCCUPANCY_CAUSES
from tests.helpers import PROJECT_ROOT
from tests.test_layer_two_constraint_assembler import (
    _Bundle,
    _candidate,
    _cluster,
    _eligibility,
    _financial,
    _financials_for,
)
from tests.test_layer_two_entry_execution_diagnostic import (
    FIXTURE_T1_OPEN,
    FIXTURE_T1_UP_LIMIT,
    _extend_store_with_daily_bars,
    _obs,
    _t1_bundle_inputs,
)

REPO_ROOT = PROJECT_ROOT
MODULE_PATH = REPO_ROOT / "src/app/research/layer_two_cash_occupancy_attribution.py"


def _next_weekday(day):
    cursor = day + timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor += timedelta(days=1)
    return cursor


def _ranking(symbols: list[str]) -> UnvalidatedDevelopmentRankingInput:
    return UnvalidatedDevelopmentRankingInput(ranked_symbols=list(symbols))


def _empty_state(constraint) -> LayerTwoStatefulPortfolioState:
    return seal_layer_two_stateful_portfolio_state(
        LayerTwoStatefulPortfolioState(
            as_of=constraint.as_of,
            decision_at=constraint.decision_at,
            market_data_snapshot_id=constraint.market_data_snapshot_id,
            current_account_equity=constraint.current_account_equity,
            cash=constraint.current_account_equity,
            positions=[],
        )
    )


def _state_with_positions(constraint, positions: list[LayerTwoActiveTranchePosition], *, cash: float | None = None):
    gross = sum(row.current_market_notional for row in positions)
    equity = constraint.current_account_equity
    use_cash = equity - gross if cash is None else cash
    return seal_layer_two_stateful_portfolio_state(
        LayerTwoStatefulPortfolioState(
            as_of=constraint.as_of,
            decision_at=constraint.decision_at,
            market_data_snapshot_id=constraint.market_data_snapshot_id,
            current_account_equity=equity,
            cash=use_cash,
            positions=positions,
        )
    )


def _structural_from_diagnose(
    *,
    bundle,
    eligibility,
    financials,
    cluster,
    constraint,
    state,
    ranking,
    allocator,
    observation: LayerTwoEntryExecutionObservation | None,
) -> tuple[LayerTwoCashOccupancyStructuralRowInput, object]:
    entry = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=observation,
    )
    structural = LayerTwoCashOccupancyStructuralRowInput(
        entry_execution_report=entry,
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=observation,
    )
    _ = eligibility, financials, cluster
    return structural, entry


def _happy_attempt_row(
    *,
    status: str = "tradable",
    raw_open: float | None = 10.0,
    up_limit: float | None = 11.0,
) -> tuple[LayerTwoCashOccupancyStructuralRowInput, object]:
    bundle, eligibility, financials, cluster, constraint, state, ranking, allocator, t1 = _t1_bundle_inputs()
    entry = allocator.proposed_entry
    assert entry is not None
    observation = _obs(
        symbol=entry.symbol,
        execution_date=t1,
        snapshot=allocator.market_data_snapshot_id,
        status=status,
        raw_open=raw_open,
        up_limit=up_limit,
    )
    return _structural_from_diagnose(
        bundle=bundle,
        eligibility=eligibility,
        financials=financials,
        cluster=cluster,
        constraint=constraint,
        state=state,
        ranking=ranking,
        allocator=allocator,
        observation=observation,
    )


def _not_attempted_risk_budget_zero() -> LayerTwoCashOccupancyStructuralRowInput:
    bundle = _Bundle(risk_budget=0.0)
    t1 = _next_weekday(bundle.as_of)
    extended = [*bundle.calendar, t1]
    phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=extended,
        start=extended[0],
        end=extended[-1],
        anchor=extended[0],
        current_account_equity=bundle.equity,
        risk_budget=bundle.risk_budget,
        market_data_snapshot_id=bundle.market_snap,
    )
    bundle.phase = phase
    eligibility = _eligibility(bundle)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    financials = _financials_for(bundle, eligible)
    cluster = _cluster(bundle, eligible)
    constraint = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )
    state = _empty_state(constraint)
    ranking = _ranking(list(constraint.eligible_symbols))
    allocator = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    structural, _ = _structural_from_diagnose(
        bundle=bundle,
        eligibility=eligibility,
        financials=financials,
        cluster=cluster,
        constraint=constraint,
        state=state,
        ranking=ranking,
        allocator=allocator,
        observation=None,
    )
    return structural


def _shared_phase_longitudinal_rows() -> tuple[
    list[LayerTwoCashOccupancyStructuralRowInput],
    object,
    object,
    object,
    object,
    Path | None,
]:
    """Two as_ofs, same phase_report_id and market_data_snapshot_id."""
    bundle = _Bundle(anchor_index=0)
    t1 = _next_weekday(bundle.as_of)
    extended = [*bundle.calendar, t1]
    probe = plan_layer_two_tranche_phase_schedule(
        market_calendar=extended,
        start=extended[0],
        end=extended[-1],
        anchor=extended[0],
        current_account_equity=bundle.equity,
        risk_budget=bundle.risk_budget,
        market_data_snapshot_id=bundle.market_snap,
    )
    dates = [o.decision_date for o in probe.selected_schedule.opportunities]
    d1, d2 = dates[-2], dates[-1]
    exec_days = sorted({_next_weekday(d1), _next_weekday(d2)})
    # Only upsert the two execution dates (not the full history) so cluster returns survive.
    bundle.store = _extend_store_with_daily_bars(
        bundle.store,
        extra_days=exec_days,
        open_=FIXTURE_T1_OPEN,
        up_limit=FIXTURE_T1_UP_LIMIT,
        overwrite=True,
    )
    bundle.market_snap = bundle.store.snapshot().snapshot_id
    phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=extended,
        start=extended[0],
        end=extended[-1],
        anchor=extended[0],
        current_account_equity=bundle.equity,
        risk_budget=bundle.risk_budget,
        market_data_snapshot_id=bundle.market_snap,
    )
    assert [o.decision_date for o in phase.selected_schedule.opportunities][-2:] == [d1, d2]
    rows: list[LayerTwoCashOccupancyStructuralRowInput] = []
    file_meta = None
    for as_of in (d1, d2):
        decision_at = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC)
        eligibility = evaluate_layer_two_candidate_eligibility(
            as_of=as_of,
            decision_at=decision_at,
            data_snapshot_id=bundle.market_snap,
            candidates=[
                _candidate(symbol, as_of=as_of, decision_at=decision_at, planned=8_000.0) for symbol in bundle.symbols
            ],
            repo_root=REPO_ROOT,
        )
        eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
        cluster = diagnose_layer_two_statistical_risk_clusters(bundle.store, as_of, decision_at, eligible, REPO_ROOT)
        financials = [
            _financial(
                symbol,
                as_of=as_of,
                decision_at=decision_at,
                snapshot_id=f"{index:02d}" + ("a1" * 31),
            )
            for index, symbol in enumerate(eligible)
        ]
        constraint = assemble_layer_two_constraints(
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            phase_report=phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
        )
        state = _empty_state(constraint)
        ranking = _ranking(list(constraint.eligible_symbols))
        allocator = allocate_layer_two_stateful_single_opportunity(
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
        )
        observation = None
        if allocator.proposed_entry is not None:
            observation = _obs(
                symbol=allocator.proposed_entry.symbol,
                execution_date=_next_weekday(as_of),
                snapshot=allocator.market_data_snapshot_id,
                status="tradable",
                raw_open=FIXTURE_T1_OPEN,
                up_limit=FIXTURE_T1_UP_LIMIT,
            )
        structural, _ = _structural_from_diagnose(
            bundle=type("B", (), {"phase": phase})(),
            eligibility=eligibility,
            financials=financials,
            cluster=cluster,
            constraint=constraint,
            state=state,
            ranking=ranking,
            allocator=allocator,
            observation=observation,
        )
        rows.append(structural)
        if as_of == d2:
            file_meta = (eligibility, financials, cluster, bundle)
    return rows, phase, file_meta[0], file_meta[1], file_meta[2], file_meta[3]  # type: ignore[index]


def _chain_at_as_of(
    *,
    store,
    symbols: list[str],
    market_snap: str,
    phase,
    as_of,
    equity: float,
    planned: float,
) -> LayerTwoCashOccupancyStructuralRowInput:
    decision_at = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC)
    eligibility = evaluate_layer_two_candidate_eligibility(
        as_of=as_of,
        decision_at=decision_at,
        data_snapshot_id=market_snap,
        candidates=[_candidate(symbol, as_of=as_of, decision_at=decision_at, planned=planned) for symbol in symbols],
        repo_root=REPO_ROOT,
    )
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    cluster = diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, eligible, REPO_ROOT)
    financials = [
        _financial(
            symbol,
            as_of=as_of,
            decision_at=decision_at,
            snapshot_id=f"{index:02d}" + ("a1" * 31),
        )
        for index, symbol in enumerate(eligible)
    ]
    constraint = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=phase,
        store=store,
        repo_root=REPO_ROOT,
    )
    state = _empty_state(constraint)
    ranking = _ranking(list(constraint.eligible_symbols))
    allocator = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    observation = None
    if allocator.proposed_entry is not None:
        observation = _obs(
            symbol=allocator.proposed_entry.symbol,
            execution_date=_next_weekday(as_of),
            snapshot=allocator.market_data_snapshot_id,
            status="tradable",
            raw_open=10.0,
            up_limit=11.0,
        )
    structural, _ = _structural_from_diagnose(
        bundle=type("B", (), {"phase": phase})(),
        eligibility=eligibility,
        financials=financials,
        cluster=cluster,
        constraint=constraint,
        state=state,
        ranking=ranking,
        allocator=allocator,
        observation=observation,
    )
    return structural


def _alt_snapshot_row() -> LayerTwoCashOccupancyStructuralRowInput:
    """Valid E10e-0 chain on a distinct market snapshot and later as_of."""
    from app.research.layer_two_allocation_protocol import plan_base_slots
    from tests.test_layer_two_statistical_risk_clusters import _complete_fixture

    calendar, as_of, _decision_at, store, symbols = _complete_fixture(n_symbols=4, extra_days=5)
    market_snap = store.snapshot().snapshot_id
    t1 = _next_weekday(as_of)
    phase_cal = [*calendar]
    if t1 not in phase_cal:
        phase_cal.append(t1)
    equity, risk = 80_000.0, 0.3
    slot = plan_base_slots(current_account_equity=equity, risk_budget=risk)
    phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=phase_cal,
        start=phase_cal[0],
        end=phase_cal[-1],
        anchor=phase_cal[0],
        current_account_equity=equity,
        risk_budget=risk,
        market_data_snapshot_id=market_snap,
    )
    return _chain_at_as_of(
        store=store,
        symbols=symbols,
        market_snap=market_snap,
        phase=phase,
        as_of=as_of,
        equity=equity,
        planned=float(slot.base_slot_notional),
    )


def _alt_phase_same_snapshot_row(
    *,
    store=None,
    market_snap: str | None = None,
    symbols: list[str] | None = None,
    calendar: list | None = None,
) -> LayerTwoCashOccupancyStructuralRowInput:
    """Valid chain sharing a given (or default) snapshot but a distinct phase_report_id."""
    from app.research.layer_two_allocation_protocol import plan_base_slots

    if store is None or market_snap is None or symbols is None or calendar is None:
        bundle = _Bundle(anchor_index=0)
        t1 = _next_weekday(bundle.as_of)
        store = bundle.store
        market_snap = bundle.market_snap
        symbols = bundle.symbols
        calendar = [*bundle.calendar, t1]
    equity, risk = 80_000.0, 0.6
    slot = plan_base_slots(current_account_equity=equity, risk_budget=risk)
    phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=calendar,
        start=calendar[0],
        end=calendar[-1],
        anchor=calendar[0],
        current_account_equity=equity,
        risk_budget=risk,
        market_data_snapshot_id=market_snap,
    )
    as_of = phase.selected_schedule.opportunities[-1].decision_date
    return _chain_at_as_of(
        store=store,
        symbols=symbols,
        market_snap=market_snap,
        phase=phase,
        as_of=as_of,
        equity=equity,
        planned=float(slot.base_slot_notional),
    )


def test_confirmed_cause_order_frozen() -> None:
    assert BOUND_OCCUPANCY_CAUSES == tuple(CONFIRMED_CASH_OCCUPANCY_CAUSES)
    assert BOUND_OCCUPANCY_CAUSES == (
        "candidate_shortage",
        "gates",
        "unaffordable_board_lot_or_min_commission",
        "suspension",
        "limit_up_or_limit_down",
        "risk_budget",
    )


def test_unknown_amounts_remain_null() -> None:
    structural, entry = _happy_attempt_row(status="unknown", raw_open=None, up_limit=None)
    assert entry.outcome == "unknown_execution_observation"
    report = attribute_layer_two_cash_occupancy([structural])
    row = report.rows[0]
    assert row.cause_marker == "unknown"
    assert row.amount_quantified is False
    assert row.known_target_cash is None
    assert row.known_base_cash_used is None
    assert row.known_retained_cash is None
    assert report.total_unknown_count == 1
    assert report.global_sum_known_target_cash == 0.0


def test_suspension_and_limit_up_mappings() -> None:
    suspended, entry_s = _happy_attempt_row(status="known_full_day_suspension", raw_open=None, up_limit=None)
    assert entry_s.outcome == "blocked_suspension"
    report_s = attribute_layer_two_cash_occupancy([suspended])
    row_s = report_s.rows[0]
    assert row_s.cause_marker == "suspension"
    assert row_s.amount_quantified is True
    assert row_s.known_base_cash_used == 0.0
    assert row_s.known_retained_cash == row_s.known_target_cash == entry_s.proposed_target_notional
    assert row_s.limit_up_or_limit_down_represents_buy_side_limit_up_only is True

    limit, entry_l = _happy_attempt_row(status="tradable", raw_open=11.0, up_limit=11.0)
    assert entry_l.outcome == "blocked_limit_up"
    report_l = attribute_layer_two_cash_occupancy([limit])
    row_l = report_l.rows[0]
    assert row_l.cause_marker == "limit_up_or_limit_down"
    assert row_l.known_base_cash_used == 0.0
    assert row_l.known_retained_cash == row_l.known_target_cash
    assert "buy_side" in row_l.classification_evidence


def test_unaffordable_and_partial_board_lot_residual() -> None:
    unaff, entry_u = _happy_attempt_row(status="tradable", raw_open=200.0, up_limit=220.0)
    assert entry_u.outcome == "unaffordable_board_lot_or_minimum_commission"
    report_u = attribute_layer_two_cash_occupancy([unaff])
    row_u = report_u.rows[0]
    assert row_u.cause_marker == "unaffordable_board_lot_or_min_commission"
    assert row_u.known_base_cash_used == 0.0
    assert row_u.known_retained_cash == entry_u.proposed_target_notional

    residual, entry_r = _happy_attempt_row(status="tradable", raw_open=10.0, up_limit=11.0)
    assert entry_r.outcome == "hypothetically_fillable"
    assert entry_r.base_scenario is not None
    assert entry_r.base_scenario.unused_target_cash > 1e-6
    report_r = attribute_layer_two_cash_occupancy([residual])
    row_r = report_r.rows[0]
    assert row_r.cause_marker == "unaffordable_board_lot_or_min_commission"
    assert row_r.classification_evidence == "hypothetically_fillable_partial_lot_residual"
    assert row_r.known_base_cash_used == entry_r.base_scenario.total_cash_used
    assert row_r.known_retained_cash == entry_r.base_scenario.unused_target_cash
    # Stress must not replace base attribution amounts.
    assert entry_r.stress_scenario is not None
    assert row_r.known_base_cash_used != entry_r.stress_scenario.total_cash_used or (
        entry_r.stress_scenario.total_cash_used == entry_r.base_scenario.total_cash_used
    )
    assert row_r.stress_scenario_not_used_for_attribution is True


def test_no_retained_cash_full_target_used() -> None:
    fill_price = 79.95
    raw_open = fill_price / 1.0005
    structural, entry = _happy_attempt_row(status="tradable", raw_open=raw_open, up_limit=90.0)
    assert entry.outcome == "hypothetically_fillable"
    assert entry.base_scenario is not None
    assert entry.base_scenario.unused_target_cash <= 1e-6
    report = attribute_layer_two_cash_occupancy([structural])
    row = report.rows[0]
    assert row.cause_marker == "no_retained_cash"
    assert row.amount_quantified is True
    assert row.known_retained_cash == 0.0
    assert report.total_no_retained_count == 1
    # no_retained is not one of the six cause summary buckets
    assert all(summary.decision_count == 0 for summary in report.cause_summaries)


def test_not_attempted_risk_budget_zero() -> None:
    structural = _not_attempted_risk_budget_zero()
    assert structural.entry_execution_report.outcome == "not_attempted"
    assert structural.allocator_report.portfolio_cash_retention_reason == "zero_risk_budget"
    report = attribute_layer_two_cash_occupancy([structural])
    row = report.rows[0]
    assert row.cause_marker == "risk_budget"
    assert row.amount_quantified is False
    assert row.known_target_cash is None
    assert report.total_not_attempt_count == 1


def test_not_attempted_selected_tranche_occupied_gates() -> None:
    bundle, eligibility, financials, cluster, constraint, _, ranking, _, _t1 = _t1_bundle_inputs()
    selected = constraint.selected_phase_opportunity.tranche_id
    other = constraint.eligible_symbols[2]
    row = next(r for r in constraint.rows if r.symbol == other)
    state = _state_with_positions(
        constraint,
        [
            LayerTwoActiveTranchePosition(
                tranche_id=selected,
                symbol=other,
                current_market_notional=8_000.0,
                cluster_id=row.cluster_id or "cluster_002",
            )
        ],
    )
    allocator = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert allocator.portfolio_cash_retention_reason == "selected_tranche_occupied"
    structural, entry = _structural_from_diagnose(
        bundle=bundle,
        eligibility=eligibility,
        financials=financials,
        cluster=cluster,
        constraint=constraint,
        state=state,
        ranking=ranking,
        allocator=allocator,
        observation=None,
    )
    assert entry.outcome == "not_attempted"
    report = attribute_layer_two_cash_occupancy([structural])
    assert report.rows[0].cause_marker == "gates"


def test_candidate_shortage_all_already_held() -> None:
    # ST-exclude two names so only two remain eligible; hold both on free tranches.
    bundle = _Bundle(equity=80_000.0, risk_budget=0.3, anchor_index=0)
    t1 = _next_weekday(bundle.as_of)
    extended = [*bundle.calendar, t1]
    phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=extended,
        start=extended[0],
        end=extended[-1],
        anchor=extended[0],
        current_account_equity=bundle.equity,
        risk_budget=bundle.risk_budget,
        market_data_snapshot_id=bundle.market_snap,
    )
    bundle.phase = phase
    st_symbols = set(bundle.symbols[2:])
    eligibility = _eligibility(bundle, st_symbols=st_symbols)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    assert len(eligible) == 2
    financials = _financials_for(bundle, eligible)
    cluster = _cluster(bundle, eligible)
    constraint = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )
    selected = constraint.selected_phase_opportunity.tranche_id
    free = [tid for tid in range(constraint.active_tranche_count) if tid != selected]
    assert len(free) >= len(constraint.eligible_symbols)
    positions = []
    for index, symbol in enumerate(constraint.eligible_symbols):
        crow = next(r for r in constraint.rows if r.symbol == symbol)
        positions.append(
            LayerTwoActiveTranchePosition(
                tranche_id=free[index],
                symbol=symbol,
                current_market_notional=100.0,
                cluster_id=crow.cluster_id or f"cluster_{index}",
            )
        )
    positions = sorted(positions, key=lambda p: p.tranche_id)
    state = _state_with_positions(constraint, positions)
    ranking = _ranking(list(constraint.eligible_symbols))
    allocator = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert allocator.portfolio_cash_retention_reason == "no_admissible_candidate"
    assert allocator.candidate_rejection_diagnostics
    assert all(d.reason == "already_held" for d in allocator.candidate_rejection_diagnostics)
    structural, _ = _structural_from_diagnose(
        bundle=bundle,
        eligibility=eligibility,
        financials=financials,
        cluster=cluster,
        constraint=constraint,
        state=state,
        ranking=ranking,
        allocator=allocator,
        observation=None,
    )
    report = attribute_layer_two_cash_occupancy([structural])
    assert report.rows[0].cause_marker == "candidate_shortage"


def test_mixed_rejection_precedence_gates() -> None:
    # Outside notionals leave cash below base slot; one eligible is already_held → mixed → gates.
    bundle = _Bundle(equity=60_000.0, risk_budget=0.9)
    t1 = _next_weekday(bundle.as_of)
    extended = [*bundle.calendar, t1]
    phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=extended,
        start=extended[0],
        end=extended[-1],
        anchor=extended[0],
        current_account_equity=bundle.equity,
        risk_budget=bundle.risk_budget,
        market_data_snapshot_id=bundle.market_snap,
    )
    bundle.phase = phase
    eligibility = _eligibility(bundle)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    financials = _financials_for(bundle, eligible)
    cluster = _cluster(bundle, eligible)
    constraint = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )
    held = constraint.eligible_symbols[0]
    crow = next(r for r in constraint.rows if r.symbol == held)
    selected = constraint.selected_phase_opportunity.tranche_id
    free = [tid for tid in range(constraint.active_tranche_count) if tid != selected]
    assert len(free) >= 4
    state = _state_with_positions(
        constraint,
        [
            LayerTwoActiveTranchePosition(
                tranche_id=free[0],
                symbol="666661.SH",
                current_market_notional=18_000.0,
                cluster_id="cluster_other_a",
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=free[1],
                symbol="666662.SZ",
                current_market_notional=18_000.0,
                cluster_id="cluster_other_b",
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=free[2],
                symbol="666663.SH",
                current_market_notional=17_900.0,
                cluster_id="cluster_other_c",
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=free[3],
                symbol=held,
                current_market_notional=100.0,
                cluster_id=crow.cluster_id or "cluster_001",
            ),
        ],
    )
    ranking = _ranking(list(constraint.eligible_symbols))
    allocator = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert allocator.portfolio_cash_retention_reason == "no_admissible_candidate"
    reasons = {d.reason for d in allocator.candidate_rejection_diagnostics}
    assert "already_held" in reasons
    assert "insufficient_cash" in reasons
    assert reasons != {"already_held"}
    assert not (reasons <= {"insufficient_cash", "sleeve_notional_cap"})
    structural, _ = _structural_from_diagnose(
        bundle=bundle,
        eligibility=eligibility,
        financials=financials,
        cluster=cluster,
        constraint=constraint,
        state=state,
        ranking=ranking,
        allocator=allocator,
        observation=None,
    )
    report = attribute_layer_two_cash_occupancy([structural])
    assert report.rows[0].cause_marker == "gates"


def test_no_admissible_insufficient_cash_risk_budget() -> None:
    bundle = _Bundle(equity=60_000.0, risk_budget=0.9)
    t1 = _next_weekday(bundle.as_of)
    extended = [*bundle.calendar, t1]
    phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=extended,
        start=extended[0],
        end=extended[-1],
        anchor=extended[0],
        current_account_equity=bundle.equity,
        risk_budget=bundle.risk_budget,
        market_data_snapshot_id=bundle.market_snap,
    )
    bundle.phase = phase
    eligibility = _eligibility(bundle)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    financials = _financials_for(bundle, eligible)
    cluster = _cluster(bundle, eligible)
    constraint = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )
    state = _state_with_positions(
        constraint,
        [
            LayerTwoActiveTranchePosition(
                tranche_id=1,
                symbol="666661.SH",
                current_market_notional=18_000.0,
                cluster_id="cluster_other_a",
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=2,
                symbol="666662.SZ",
                current_market_notional=18_000.0,
                cluster_id="cluster_other_b",
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=3,
                symbol="666663.SH",
                current_market_notional=18_000.0,
                cluster_id="cluster_other_c",
            ),
        ],
    )
    ranking = _ranking(list(constraint.eligible_symbols))
    allocator = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert allocator.portfolio_cash_retention_reason == "no_admissible_candidate"
    assert all(d.reason == "insufficient_cash" for d in allocator.candidate_rejection_diagnostics)
    structural, _ = _structural_from_diagnose(
        bundle=bundle,
        eligibility=eligibility,
        financials=financials,
        cluster=cluster,
        constraint=constraint,
        state=state,
        ranking=ranking,
        allocator=allocator,
        observation=None,
    )
    report = attribute_layer_two_cash_occupancy([structural])
    assert report.rows[0].cause_marker == "risk_budget"


def test_longitudinal_aggregate_and_cause_rows_always_present() -> None:
    rows, *_rest = _shared_phase_longitudinal_rows()
    report = attribute_layer_two_cash_occupancy(rows)
    assert report.row_count == 2
    assert report.coverage_as_of_start < report.coverage_as_of_end
    assert [s.cause for s in report.cause_summaries] == list(BOUND_OCCUPANCY_CAUSES)
    assert report.total_report_count == 2
    assert report.total_attempt_count + report.total_not_attempt_count == 2
    assert (
        abs(
            report.global_sum_known_target_cash
            - (report.global_sum_known_base_cash_used + report.global_sum_known_retained_cash)
        )
        <= 1e-6
    )
    assert report.diagnostic_only is True
    assert report.ready_for_scoring is False
    assert report.ready_for_backtest is False
    assert report.ready_for_portfolio_construction is False
    assert report.ready_for_orders is False
    assert report.ready_for_trading is False
    assert report.auto_apply is False
    assert report.does_not_modify_allocator_or_execution is True
    assert report.does_not_claim_account_utilization is True
    assert report.protocol_cash_occupancy_blocker_not_resolved is True
    structural = verify_layer_two_cash_occupancy_attribution_report(report, rows=rows)
    assert structural.structural_ok is True
    assert structural.entry_execution_binding_ok is False
    assert structural.phase_binding_ok is False
    assert structural.tranche_evaluation_protocol_binding_ok is False


def test_duplicate_out_of_order_cross_snapshot_cross_phase() -> None:
    rows, phase, *_rest, bundle = _shared_phase_longitudinal_rows()
    with pytest.raises(ValueError, match="strictly increasing|unique as_of"):
        attribute_layer_two_cash_occupancy([rows[0], rows[0]])
    with pytest.raises(ValueError, match="strictly increasing|unique as_of"):
        attribute_layer_two_cash_occupancy([rows[1], rows[0]])

    # Distinct as_of + deliberately different market snapshot (extra_days store).
    early = rows[0]
    alt_snap = _alt_snapshot_row()
    assert early.entry_execution_report.as_of != alt_snap.entry_execution_report.as_of
    assert (
        early.entry_execution_report.market_data_snapshot_id != alt_snap.entry_execution_report.market_data_snapshot_id
    )
    pair_snap = sorted([early, alt_snap], key=lambda row: row.entry_execution_report.as_of)
    with pytest.raises(ValueError, match="market_data_snapshot_id"):
        attribute_layer_two_cash_occupancy(pair_snap)

    # Same snapshot, distinct phase_report_id (risk_budget 0.6 phase on shared hashed store).
    alt_phase = _alt_phase_same_snapshot_row(
        store=bundle.store,
        market_snap=bundle.market_snap,
        symbols=bundle.symbols,
        calendar=list(phase.market_calendar),
    )
    assert early.entry_execution_report.as_of != alt_phase.entry_execution_report.as_of
    assert (
        early.entry_execution_report.market_data_snapshot_id == alt_phase.entry_execution_report.market_data_snapshot_id
    )
    assert early.entry_execution_report.phase_report_id != alt_phase.entry_execution_report.phase_report_id
    pair_phase = sorted([early, alt_phase], key=lambda row: row.entry_execution_report.as_of)
    with pytest.raises(ValueError, match="phase_report_id"):
        attribute_layer_two_cash_occupancy(pair_phase)


def test_outer_reseal_cause_count_amount_tamper() -> None:
    structural, _ = _happy_attempt_row()
    report = attribute_layer_two_cash_occupancy([structural])

    # Self-hash / verify path rejects count drift even before aggregate recompute identities.
    tampered = report.model_copy(update={"total_unknown_count": report.total_unknown_count + 1})
    with pytest.raises(ValueError, match="report_id|unknown/no_retained|recompute"):
        verify_layer_two_cash_occupancy_attribution_report(tampered, rows=[structural])

    payload = report.model_dump(mode="json")
    payload["rows"][0]["cause_marker"] = "gates"
    payload["rows"][0]["amount_quantified"] = False
    payload["rows"][0]["known_target_cash"] = None
    payload["rows"][0]["known_base_cash_used"] = None
    payload["rows"][0]["known_retained_cash"] = None
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoCashOccupancyAttributionReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    payload["total_attempt_count"] = int(payload["total_attempt_count"]) + 1
    payload["total_not_attempt_count"] = max(0, int(payload["total_not_attempt_count"]) - 1)
    payload.pop("report_id", None)
    with pytest.raises(ValidationError, match="total_attempt_count|recompute"):
        LayerTwoCashOccupancyAttributionReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    for summary in payload["cause_summaries"]:
        if summary["cause"] == "gates":
            summary["decision_count"] = int(summary["decision_count"]) + 1
            summary["unquantified_row_count"] = int(summary["unquantified_row_count"]) + 1
            break
    payload.pop("report_id", None)
    with pytest.raises(ValidationError, match="cause decisions|decision_count drift|row_count"):
        LayerTwoCashOccupancyAttributionReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    if payload["rows"][0]["amount_quantified"]:
        payload["global_sum_known_retained_cash"] = float(payload["global_sum_known_retained_cash"]) + 1.0
        payload["global_sum_known_target_cash"] = float(payload["global_sum_known_target_cash"]) + 1.0
        for summary in payload["cause_summaries"]:
            if summary["cause"] == payload["rows"][0]["cause_marker"]:
                summary["sum_known_retained_cash"] = float(summary["sum_known_retained_cash"]) + 1.0
                summary["sum_known_target_cash"] = float(summary["sum_known_target_cash"]) + 1.0
                break
        payload.pop("report_id", None)
        with pytest.raises(ValidationError, match="global_sum_known|cause summary sum_known"):
            LayerTwoCashOccupancyAttributionReport.model_validate(payload)

    with pytest.raises(ValidationError):
        CashOccupancyRowAttribution.model_validate(
            {
                "as_of": "2023-06-20",
                "entry_execution_report_id": "ab" * 32,
                "allocator_report_id": "cd" * 32,
                "execution_outcome": "not_a_real_outcome",
                "cause_marker": "gates",
                "amount_quantified": False,
                "classification_evidence": "x",
            }
        )


def test_bool_nan_inf_rejected() -> None:
    with pytest.raises(ValidationError):
        CashOccupancyRowAttribution.model_validate(
            {
                "as_of": "2023-06-20",
                "entry_execution_report_id": "ab" * 32,
                "allocator_report_id": "cd" * 32,
                "execution_outcome": "not_attempted",
                "cause_marker": "gates",
                "amount_quantified": 1,
                "classification_evidence": "x",
            }
        )
    with pytest.raises(ValidationError):
        CashOccupancyCauseSummary.model_validate(
            {
                "cause": "gates",
                "decision_count": 1,
                "quantified_row_count": 0,
                "unquantified_row_count": 1,
                "sum_known_target_cash": float("nan"),
                "sum_known_base_cash_used": 0.0,
                "sum_known_retained_cash": 0.0,
            }
        )
    with pytest.raises(ValidationError):
        CashOccupancyCauseSummary.model_validate(
            {
                "cause": "gates",
                "decision_count": 1,
                "quantified_row_count": 0,
                "unquantified_row_count": 1,
                "sum_known_target_cash": float("inf"),
                "sum_known_base_cash_used": 0.0,
                "sum_known_retained_cash": 0.0,
            }
        )
    with pytest.raises(ValidationError):
        LayerTwoCashOccupancyAttributionVerificationResult.model_validate(
            {
                "report_id": "ab" * 32,
                "structural_ok": 1,
            }
        )
    with pytest.raises(ValidationError):
        LayerTwoCashOccupancyAttributionVerificationResult.model_validate(
            {
                "report_id": "ab" * 32,
                "structural_ok": True,
                "ready_for_orders": 0,
            }
        )


def test_structural_vs_file_binding(tmp_path: Path) -> None:
    rows, phase, _eligibility_meta, _financials_meta, _cluster_meta, bundle = _shared_phase_longitudinal_rows()
    report = attribute_layer_two_cash_occupancy(rows)
    structural = verify_layer_two_cash_occupancy_attribution_report(report, rows=rows)
    assert structural.entry_execution_binding_ok is False
    assert structural.phase_binding_ok is False
    assert structural.tranche_evaluation_protocol_binding_ok is False

    phase_path = tmp_path / "phase.json"
    write_layer_two_tranche_phase_schedule_report(phase_path, phase)

    rebuilt_file_rows: list[LayerTwoCashOccupancyFileRowInput] = []
    for structural_row in rows:
        as_of = structural_row.entry_execution_report.as_of
        decision_at = structural_row.constraint_report.decision_at
        elig = evaluate_layer_two_candidate_eligibility(
            as_of=as_of,
            decision_at=decision_at,
            data_snapshot_id=bundle.market_snap,
            candidates=[
                _candidate(symbol, as_of=as_of, decision_at=decision_at, planned=8_000.0) for symbol in bundle.symbols
            ],
            repo_root=REPO_ROOT,
        )
        elig_syms = [e.symbol for e in elig.evaluations if e.eligible_for_new_entry]
        cl = diagnose_layer_two_statistical_risk_clusters(bundle.store, as_of, decision_at, elig_syms, REPO_ROOT)
        fins = [
            _financial(
                symbol,
                as_of=as_of,
                decision_at=decision_at,
                snapshot_id=f"{index:02d}" + ("a1" * 31),
            )
            for index, symbol in enumerate(elig_syms)
        ]
        rebuilt_file_rows.append(
            LayerTwoCashOccupancyFileRowInput(
                structural=structural_row,
                file_bindings=LayerTwoCashOccupancyFileRowBindings(
                    eligibility_report=elig,
                    financial_reports=tuple(fins),
                    cluster_report=cl,
                    store=bundle.store,
                    repo_root=REPO_ROOT,
                    phase_report_path=phase_path,
                ),
            )
        )

    file_ok = verify_layer_two_cash_occupancy_attribution_report_file(
        report=report,
        rows=rebuilt_file_rows,
    )
    assert file_ok.entry_execution_binding_ok is True
    assert file_ok.phase_binding_ok is True
    assert file_ok.tranche_evaluation_protocol_binding_ok is True

    missing = tmp_path / "missing-phase.json"
    bad_rows = [
        LayerTwoCashOccupancyFileRowInput(
            structural=item.structural,
            file_bindings=LayerTwoCashOccupancyFileRowBindings(
                eligibility_report=item.file_bindings.eligibility_report,
                financial_reports=item.file_bindings.financial_reports,
                cluster_report=item.file_bindings.cluster_report,
                store=item.file_bindings.store,
                repo_root=item.file_bindings.repo_root,
                phase_report_path=missing,
            ),
        )
        for item in rebuilt_file_rows
    ]
    with pytest.raises(ValueError, match="phase report file missing|phase"):
        verify_layer_two_cash_occupancy_attribution_report_file(report=report, rows=bad_rows)

    tampered_phase = json.loads(phase_path.read_text(encoding="utf-8"))
    tampered_phase["current_account_equity"] = float(bundle.equity) + 1.0
    phase_path.write_text(json.dumps(tampered_phase, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        verify_layer_two_cash_occupancy_attribution_report_file(report=report, rows=rebuilt_file_rows)

    write_layer_two_tranche_phase_schedule_report(phase_path, phase)
    out = tmp_path / "occupancy.json"
    write_layer_two_cash_occupancy_attribution_report(out, report)
    assert out.exists()

    from unittest.mock import patch

    from app.research.layer_two_entry_execution_diagnostic import (
        LayerTwoEntryExecutionVerificationResult,
    )

    def _fake_e10e0_file_verifier(**kwargs):
        return LayerTwoEntryExecutionVerificationResult(
            report_id=kwargs["report"].report_id or ("ab" * 32),
            structural_ok=True,
            allocator_binding_ok=False,
            phase_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
            execution_observation_binding_ok=False,
        )

    with patch(
        "app.research.layer_two_cash_occupancy_attribution.verify_layer_two_entry_execution_diagnostic_report_file",
        side_effect=_fake_e10e0_file_verifier,
    ):
        with pytest.raises(ValueError, match="observation|allocator|structural_ok/allocator"):
            verify_layer_two_cash_occupancy_attribution_report_file(
                report=report,
                rows=rebuilt_file_rows,
            )

    def _fake_missing_structural(**kwargs):
        return LayerTwoEntryExecutionVerificationResult(
            report_id=kwargs["report"].report_id or ("ab" * 32),
            structural_ok=False,
            allocator_binding_ok=False,
            phase_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
            execution_observation_binding_ok=False,
        )

    with patch(
        "app.research.layer_two_cash_occupancy_attribution.verify_layer_two_entry_execution_diagnostic_report_file",
        side_effect=_fake_missing_structural,
    ):
        with pytest.raises(ValueError, match="structural_ok/allocator"):
            verify_layer_two_cash_occupancy_attribution_report_file(
                report=report,
                rows=rebuilt_file_rows,
            )


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
    assert "StrategyConfig" not in source
    assert "app.storage.protocol" in imported
