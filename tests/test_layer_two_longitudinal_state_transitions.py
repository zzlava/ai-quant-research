"""Attack-oriented tests for longitudinal cash/tranche state transitions (E10f-3b v2)."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.research.layer_two_allocation_protocol import (
    verify_layer_two_allocation_protocol_file,
)
from app.research.layer_two_cash_occupancy_attribution import (
    CashOccupancyRowAttribution,
    LayerTwoCashOccupancyAttributionVerificationResult,
    LayerTwoCashOccupancyFileRowBindings,
    LayerTwoCashOccupancyFileRowInput,
    LayerTwoCashOccupancyStructuralRowInput,
    attribute_layer_two_cash_occupancy,
    seal_layer_two_cash_occupancy_attribution_report,
    verify_layer_two_cash_occupancy_attribution_report_file,
)
from app.research.layer_two_constraint_assembler import (
    assemble_layer_two_constraints,
    seal_layer_two_constraint_assembler_report,
)
from app.research.layer_two_entry_execution_diagnostic import diagnose_layer_two_entry_execution
from app.research.layer_two_fixed_horizon_exit_diagnostic import (
    LayerTwoFixedHorizonExitFileInput,
    LayerTwoFixedHorizonExitStructuralInput,
    LayerTwoFixedHorizonExitVerificationResult,
    diagnose_layer_two_fixed_horizon_exit,
)
from app.research.layer_two_hypothetical_position_lifecycle import (
    LayerTwoHypotheticalLifecycleFileBindings,
    LayerTwoHypotheticalLifecycleFileInput,
    LayerTwoHypotheticalLifecycleStructuralInput,
    LayerTwoHypotheticalPositionLifecycleVerificationResult,
    open_layer_two_hypothetical_position_lifecycle,
)
from app.research.layer_two_longitudinal_state_transitions import (
    BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID,
    BOUND_INITIAL_CASH,
    LAYER_TWO_LONGITUDINAL_ENGINE_VERSION,
    LAYER_TWO_LONGITUDINAL_SCHEMA_VERSION,
    LayerTwoLongitudinalDayFileInput,
    LayerTwoLongitudinalDayStructuralInput,
    LayerTwoLongitudinalEntryFileInput,
    LayerTwoLongitudinalEntryStructuralInput,
    LayerTwoLongitudinalExitFileInput,
    LayerTwoLongitudinalExitStructuralInput,
    LayerTwoLongitudinalFileInput,
    LayerTwoLongitudinalStateTransitionReport,
    LayerTwoLongitudinalStructuralInput,
    LayerTwoLongitudinalVerificationResult,
    LongitudinalActivePosition,
    assert_lifecycle_current_state_matches_longitudinal_start_of_day,
    diagnose_layer_two_longitudinal_state_transitions,
    seal_layer_two_longitudinal_state_transition_report,
    verify_layer_two_longitudinal_state_transition_report,
    verify_layer_two_longitudinal_state_transition_report_file,
)
from app.research.layer_two_stateful_allocator import (
    LayerTwoActiveTranchePosition,
    LayerTwoStatefulPortfolioState,
    UnvalidatedDevelopmentRankingInput,
    allocate_layer_two_stateful_single_opportunity,
    seal_layer_two_stateful_portfolio_state,
)
from app.research.layer_two_tranche_phase_schedule import write_layer_two_tranche_phase_schedule_report
from tests.helpers import PROJECT_ROOT
from tests.test_layer_two_constraint_assembler import _cluster, _eligibility, _financials_for
from tests.test_layer_two_entry_execution_diagnostic import FIXTURE_T1_OPEN, FIXTURE_T1_UP_LIMIT, _obs
from tests.test_layer_two_fixed_horizon_exit_diagnostic import (
    FIXTURE_EXIT_DOWN_LIMIT,
    FIXTURE_EXIT_OPEN,
    _exit_obs,
    _lifecycle_chain,
    _scheduled,
)

REPO_ROOT = PROJECT_ROOT
MODULE_PATH = REPO_ROOT / "src/app/research/layer_two_longitudinal_state_transitions.py"


def _entry_struct(lifecycle, life_struct) -> LayerTwoLongitudinalEntryStructuralInput:
    return LayerTwoLongitudinalEntryStructuralInput(
        lifecycle_record=lifecycle,
        lifecycle_structural=life_struct,
    )


def _exit_struct(lifecycle, life_struct, stamp, calendar, observations, report):
    return LayerTwoLongitudinalExitStructuralInput(
        exit_report=report,
        exit_structural=LayerTwoFixedHorizonExitStructuralInput(
            lifecycle_record=lifecycle,
            lifecycle_structural=life_struct,
            stamp_tax_contract=stamp,
            market_calendar=tuple(calendar),
            exit_observations=tuple(observations),
        ),
    )


def _build_exit(lifecycle, life_struct, stamp, calendar, *, status: str, raw_open=None, down_limit=None, extra_obs=()):
    scheduled = _scheduled(calendar, lifecycle.entry_trade_date)
    snap = lifecycle.market_data_snapshot_id
    observations = [
        _exit_obs(
            symbol=lifecycle.symbol,
            day=scheduled,
            snapshot=snap,
            status=status,
            raw_open=raw_open,
            down_limit=down_limit,
        ),
        *extra_obs,
    ]
    exit_report = diagnose_layer_two_fixed_horizon_exit(
        lifecycle_record=lifecycle,
        lifecycle_structural=life_struct,
        stamp_tax_contract=stamp,
        market_calendar=calendar,
        exit_observations=observations,
    )
    return scheduled, observations, exit_report


def _fillable_row(life_struct) -> LayerTwoCashOccupancyStructuralRowInput:
    return LayerTwoCashOccupancyStructuralRowInput(
        entry_execution_report=life_struct.entry_execution_report,
        allocator_report=life_struct.allocator_report,
        constraint_report=life_struct.constraint_report,
        current_state=life_struct.current_state,
        ranking=life_struct.ranking,
        phase_report=life_struct.phase_report,
        execution_observation=life_struct.execution_observation,
    )


def _occupancy_from_rows(*rows: LayerTwoCashOccupancyStructuralRowInput):
    ordered = sorted(rows, key=lambda row: row.entry_execution_report.as_of)
    report = attribute_layer_two_cash_occupancy(list(ordered))
    return report, tuple(ordered)


def _occupancy_from_life_struct(life_struct):
    row = _fillable_row(life_struct)
    return _occupancy_from_rows(row)


def _make_structural(days, occ_report, occ_rows) -> LayerTwoLongitudinalStructuralInput:
    return LayerTwoLongitudinalStructuralInput(
        days=tuple(days),
        cash_occupancy_report=occ_report,
        cash_occupancy_rows=tuple(occ_rows),
    )


def _build_not_attempted_at_prior(bundle, life_struct):
    """Earlier phase opportunity: not_attempted via phantom tranche occupancy or empty state."""
    phase = bundle.phase
    fillable_as_of = life_struct.constraint_report.as_of
    prior_opps = [row for row in phase.selected_schedule.opportunities if row.decision_date < fillable_as_of]
    assert prior_opps, "fixture phase must expose an earlier opportunity than fillable entry"
    prior_as_of = prior_opps[-1].decision_date
    decision_at = datetime(prior_as_of.year, prior_as_of.month, prior_as_of.day, 16, 0, tzinfo=UTC)
    saved_as_of, saved_decision = bundle.as_of, bundle.decision_at
    bundle.as_of = prior_as_of
    bundle.decision_at = decision_at
    try:
        eligibility = _eligibility(bundle)
        eligible = [row.symbol for row in eligibility.evaluations if row.eligible_for_new_entry]
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
        ranking = UnvalidatedDevelopmentRankingInput(ranked_symbols=list(eligible))
        has_clusters = any(row.cluster_id for row in constraint.rows)
        if has_clusters:
            selected = constraint.selected_phase_opportunity.tranche_id
            other = constraint.eligible_symbols[min(2, len(constraint.eligible_symbols) - 1)]
            crow = next(row for row in constraint.rows if row.symbol == other)
            state = seal_layer_two_stateful_portfolio_state(
                LayerTwoStatefulPortfolioState(
                    as_of=constraint.as_of,
                    decision_at=constraint.decision_at,
                    market_data_snapshot_id=constraint.market_data_snapshot_id,
                    current_account_equity=constraint.current_account_equity,
                    cash=constraint.current_account_equity - 8_000.0,
                    positions=[
                        LayerTwoActiveTranchePosition(
                            tranche_id=selected,
                            symbol=other,
                            current_market_notional=8_000.0,
                            cluster_id=crow.cluster_id or "cluster_002",
                        )
                    ],
                )
            )
        else:
            state = seal_layer_two_stateful_portfolio_state(
                LayerTwoStatefulPortfolioState(
                    as_of=constraint.as_of,
                    decision_at=constraint.decision_at,
                    market_data_snapshot_id=constraint.market_data_snapshot_id,
                    current_account_equity=constraint.current_account_equity,
                    cash=constraint.current_account_equity,
                    positions=[],
                )
            )
        allocator = allocate_layer_two_stateful_single_opportunity(
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
        )
        entry = diagnose_layer_two_entry_execution(
            allocator_report=allocator,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            phase_report=phase,
            execution_observation=None,
        )
        assert entry.outcome == "not_attempted"
        structural = LayerTwoCashOccupancyStructuralRowInput(
            entry_execution_report=entry,
            allocator_report=allocator,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            phase_report=phase,
            execution_observation=None,
        )
        meta = (eligibility, financials, cluster, bundle.store)
        return structural, meta
    finally:
        bundle.as_of = saved_as_of
        bundle.decision_at = saved_decision


def _occupancy_with_not_attempted_and_fillable(bundle, life_struct, lifecycle):
    not_attempted_struct, not_meta = _build_not_attempted_at_prior(bundle, life_struct)
    fillable_struct = _fillable_row(life_struct)
    rows = sorted([not_attempted_struct, fillable_struct], key=lambda row: row.entry_execution_report.as_of)
    report = attribute_layer_two_cash_occupancy(rows)
    _ = lifecycle
    return report, tuple(rows), not_meta


def _occupancy_file_row(
    structural_row: LayerTwoCashOccupancyStructuralRowInput,
    *,
    eligibility,
    financials,
    cluster,
    store,
    phase_path: Path,
) -> LayerTwoCashOccupancyFileRowInput:
    return LayerTwoCashOccupancyFileRowInput(
        structural=structural_row,
        file_bindings=LayerTwoCashOccupancyFileRowBindings(
            eligibility_report=eligibility,
            financial_reports=tuple(financials),
            cluster_report=cluster,
            store=store,
            repo_root=REPO_ROOT,
            phase_report_path=phase_path,
        ),
    )


def _build_occupancy_file_rows(
    occ_rows: tuple[LayerTwoCashOccupancyStructuralRowInput, ...],
    *,
    phase_path: Path,
    fillable_meta: tuple,
    not_attempted_meta=None,
) -> tuple[LayerTwoCashOccupancyFileRowInput, ...]:
    fill_elig, fill_fins, fill_cl, fill_store = fillable_meta
    out: list[LayerTwoCashOccupancyFileRowInput] = []
    for row in occ_rows:
        if row.entry_execution_report.outcome == "not_attempted" and not_attempted_meta is not None:
            na_elig, na_fins, na_cl, na_store = not_attempted_meta
            out.append(
                _occupancy_file_row(
                    row,
                    eligibility=na_elig,
                    financials=na_fins,
                    cluster=na_cl,
                    store=na_store,
                    phase_path=phase_path,
                )
            )
        else:
            out.append(
                _occupancy_file_row(
                    row,
                    eligibility=fill_elig,
                    financials=fill_fins,
                    cluster=fill_cl,
                    store=fill_store,
                    phase_path=phase_path,
                )
            )
    return tuple(out)


def _happy_close_bundle():
    bundle, eligibility, financials, cluster, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    occ_report, occ_rows = _occupancy_from_life_struct(life_struct)
    scheduled, observations, exit_report = _build_exit(
        lifecycle,
        life_struct,
        stamp,
        calendar,
        status="tradable",
        raw_open=FIXTURE_EXIT_OPEN,
        down_limit=FIXTURE_EXIT_DOWN_LIMIT,
    )
    assert exit_report.final_outcome == "hypothetically_exitable"
    days = (
        LayerTwoLongitudinalDayStructuralInput(
            event_date=lifecycle.entry_trade_date,
            entry=_entry_struct(lifecycle, life_struct),
            exits=(),
        ),
        LayerTwoLongitudinalDayStructuralInput(
            event_date=scheduled,
            entry=None,
            exits=(_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),),
        ),
    )
    structural = _make_structural(days, occ_report, occ_rows)
    return (
        bundle,
        eligibility,
        financials,
        cluster,
        life_struct,
        lifecycle,
        stamp,
        calendar,
        observations,
        exit_report,
        occ_report,
        occ_rows,
        structural,
    )


def _rebuild_lifecycle_on_later_opportunity(
    *,
    bundle,
    phase,
    prior_as_of,
    prior_entry_trade_date,
    state: LayerTwoStatefulPortfolioState,
    constraint_equity: float,
):
    """Open another E10f-1 record on a later phase opportunity with an explicit state."""
    opps = [row for row in phase.selected_schedule.opportunities if row.decision_date > prior_as_of]
    calendar = list(phase.market_calendar)
    chosen = None
    for row in opps:
        t1 = calendar[calendar.index(row.decision_date) + 1]
        if t1 > prior_entry_trade_date:
            chosen = row
            break
    assert chosen is not None
    as_of = chosen.decision_date
    decision_at = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC)
    bundle.as_of = as_of
    bundle.decision_at = decision_at
    if state.as_of != as_of or state.decision_at != decision_at:
        raise ValueError("test fixture state timing must match later opportunity")
    eligibility = _eligibility(bundle)
    eligible = [row.symbol for row in eligibility.evaluations if row.eligible_for_new_entry]
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
    if abs(float(constraint_equity) - float(constraint.current_account_equity)) > 1e-9:
        constraint = seal_layer_two_constraint_assembler_report(
            constraint.model_copy(update={"current_account_equity": float(constraint_equity), "report_id": None})
        )
    ranking = UnvalidatedDevelopmentRankingInput(ranked_symbols=list(eligible))
    allocator = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert allocator.proposed_entry is not None
    t1 = calendar[calendar.index(as_of) + 1]
    observation = _obs(
        symbol=allocator.proposed_entry.symbol,
        execution_date=t1,
        snapshot=allocator.market_data_snapshot_id,
        status="tradable",
        raw_open=FIXTURE_T1_OPEN,
        up_limit=FIXTURE_T1_UP_LIMIT,
    )
    entry_report = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=phase,
        execution_observation=observation,
    )
    assert entry_report.outcome == "hypothetically_fillable"
    life_struct = LayerTwoHypotheticalLifecycleStructuralInput(
        entry_execution_report=entry_report,
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=phase,
        execution_observation=observation,
    )
    lifecycle = open_layer_two_hypothetical_position_lifecycle(
        entry_execution_report=entry_report,
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=phase,
        execution_observation=observation,
    )
    return lifecycle, life_struct


def _later_opportunity_timing(phase, *, prior_as_of, after_trade_date):
    calendar = list(phase.market_calendar)
    for row in phase.selected_schedule.opportunities:
        if row.decision_date <= prior_as_of:
            continue
        t1 = calendar[calendar.index(row.decision_date) + 1]
        if t1 > after_trade_date:
            as_of = row.decision_date
            decision_at = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC)
            return as_of, decision_at
    raise AssertionError("no later phase opportunity available in fixture calendar")


def test_happy_entry_then_40_bar_exit_cash_flow_identity() -> None:
    *_, lifecycle, _stamp, _calendar, _obs_rows, exit_report, occ_report, occ_rows, structural = _happy_close_bundle()
    report = diagnose_layer_two_longitudinal_state_transitions(structural=structural)
    assert report.schema_version == LAYER_TWO_LONGITUDINAL_SCHEMA_VERSION == "2"
    assert report.engine_version == LAYER_TWO_LONGITUDINAL_ENGINE_VERSION
    assert report.initial_cash == BOUND_INITIAL_CASH == 80000
    assert report.allocation_implementation_protocol_id == BOUND_ALLOCATION_IMPLEMENTATION_PROTOCOL_ID
    assert report.cash_occupancy_attribution_report_id == occ_report.report_id
    assert report.cash_occupancy_input_entry_execution_report_ids == list(occ_report.input_entry_execution_report_ids)
    assert report.closed_position_count == 1
    assert report.open_position_count == 0
    assert report.transition_rows[0].transition_kind == "entry_opened"
    assert report.transition_rows[0].entry_execution_report_id == lifecycle.entry_execution_report_id
    assert report.transition_rows[1].transition_kind == "exit_closed"
    assert report.transition_rows[1].entry_execution_report_id is None
    assert report.cumulative_entry_total_cash_used == lifecycle.entry_total_cash_used
    assert exit_report.base_scenario is not None
    assert report.cumulative_base_exit_net_cash_received == exit_report.base_scenario.net_sale_cash
    expected = (
        float(BOUND_INITIAL_CASH)
        - float(lifecycle.entry_total_cash_used)
        + float(exit_report.base_scenario.net_sale_cash)
    )
    assert abs(report.ending_cash - expected) <= 1e-9
    assert report.ready_for_longitudinal_diagnostic is False
    assert report.does_not_compute_return_pnl_equity_or_mark is True
    assert report.does_not_reinterpret_consumed_p10_h20 is True
    result = verify_layer_two_longitudinal_state_transition_report(report, structural=structural)
    assert result.structural_ok is True
    assert result.lifecycle_bindings_ok is False
    assert result.exit_bindings_ok is False
    assert result.allocation_protocol_binding_ok is False
    assert result.cash_occupancy_attribution_binding_ok is False
    assert result.ready_for_longitudinal_diagnostic is False


def test_happy_with_not_attempted_and_fillable() -> None:
    bundle, eligibility, financials, cluster, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    occ_report, occ_rows, not_meta = _occupancy_with_not_attempted_and_fillable(bundle, life_struct, lifecycle)
    assert len(occ_rows) == 2
    assert occ_report.total_not_attempt_count == 1
    assert occ_report.total_attempt_count >= 1
    not_row = next(row for row in occ_report.rows if row.execution_outcome == "not_attempted")
    fill_row = next(row for row in occ_report.rows if row.execution_outcome == "hypothetically_fillable")
    assert not_row.entry_execution_report_id in occ_report.input_entry_execution_report_ids
    assert fill_row.entry_execution_report_id in occ_report.input_entry_execution_report_ids
    scheduled, observations, exit_report = _build_exit(
        lifecycle,
        life_struct,
        stamp,
        calendar,
        status="tradable",
        raw_open=FIXTURE_EXIT_OPEN,
        down_limit=FIXTURE_EXIT_DOWN_LIMIT,
    )
    structural = _make_structural(
        (
            LayerTwoLongitudinalDayStructuralInput(
                event_date=lifecycle.entry_trade_date,
                entry=_entry_struct(lifecycle, life_struct),
                exits=(),
            ),
            LayerTwoLongitudinalDayStructuralInput(
                event_date=scheduled,
                entry=None,
                exits=(_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),),
            ),
        ),
        occ_report,
        occ_rows,
    )
    report = diagnose_layer_two_longitudinal_state_transitions(structural=structural)
    assert set(report.cash_occupancy_input_entry_execution_report_ids) == {
        not_row.entry_execution_report_id,
        fill_row.entry_execution_report_id,
    }
    entry_ids = [
        row.entry_execution_report_id for row in report.transition_rows if row.transition_kind == "entry_opened"
    ]
    assert entry_ids == [fill_row.entry_execution_report_id]
    assert not_row.entry_execution_report_id not in entry_ids
    assert report.transition_rows[0].entry_execution_report_id == lifecycle.entry_execution_report_id
    _ = eligibility, financials, cluster, not_meta


def test_deferred_still_open_keeps_cash_and_tranche() -> None:
    bundle, eligibility, financials, cluster, life_struct, lifecycle, stamp, calendar = _lifecycle_chain(
        extra_after_entry=45
    )
    occ_report, occ_rows = _occupancy_from_life_struct(life_struct)
    scheduled, observations, exit_report = _build_exit(
        lifecycle,
        life_struct,
        stamp,
        calendar,
        status="known_full_day_suspension",
    )
    assert exit_report.final_outcome == "still_open_after_observed_blocks"
    structural = _make_structural(
        (
            LayerTwoLongitudinalDayStructuralInput(
                event_date=lifecycle.entry_trade_date,
                entry=_entry_struct(lifecycle, life_struct),
                exits=(),
            ),
            LayerTwoLongitudinalDayStructuralInput(
                event_date=scheduled,
                entry=None,
                exits=(_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),),
            ),
        ),
        occ_report,
        occ_rows,
    )
    report = diagnose_layer_two_longitudinal_state_transitions(structural=structural)
    assert report.deferred_position_count == 1
    assert report.closed_position_count == 0
    assert report.cumulative_base_exit_net_cash_received == 0.0
    assert abs(report.ending_cash - (BOUND_INITIAL_CASH - lifecycle.entry_total_cash_used)) <= 1e-9
    assert report.ending_positions[0].status == "deferred_still_open"
    assert report.ending_positions[0].tranche_id == lifecycle.tranche_id
    _ = bundle, eligibility, financials, cluster


def test_unknown_halt_blocks_further_events() -> None:
    _b, _e, _f, _c, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    occ_report, occ_rows = _occupancy_from_life_struct(life_struct)
    scheduled, observations, exit_report = _build_exit(lifecycle, life_struct, stamp, calendar, status="unknown")
    assert exit_report.final_outcome == "unknown_exit_observation"
    days = [
        LayerTwoLongitudinalDayStructuralInput(
            event_date=lifecycle.entry_trade_date,
            entry=_entry_struct(lifecycle, life_struct),
            exits=(),
        ),
        LayerTwoLongitudinalDayStructuralInput(
            event_date=scheduled,
            entry=None,
            exits=(_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),),
        ),
    ]
    report = diagnose_layer_two_longitudinal_state_transitions(structural=_make_structural(days, occ_report, occ_rows))
    assert report.terminal_unknown_halt is True
    assert report.unknown_halt_position_count == 1

    later = calendar[calendar.index(scheduled) + 1]
    bad_days = [
        *days,
        LayerTwoLongitudinalDayStructuralInput(event_date=later, entry=None, exits=()),
    ]
    with pytest.raises(ValueError, match="terminal_unknown_halt"):
        diagnose_layer_two_longitudinal_state_transitions(structural=_make_structural(bad_days, occ_report, occ_rows))


def test_unknown_exit_then_same_day_closed_exit_rejected() -> None:
    _b, _e, _f, _c, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    occ_report, occ_rows = _occupancy_from_life_struct(life_struct)
    scheduled, unknown_obs, unknown_report = _build_exit(lifecycle, life_struct, stamp, calendar, status="unknown")
    _sched2, closed_obs, closed_report = _build_exit(
        lifecycle,
        life_struct,
        stamp,
        calendar,
        status="tradable",
        raw_open=FIXTURE_EXIT_OPEN,
        down_limit=FIXTURE_EXIT_DOWN_LIMIT,
    )
    assert unknown_report.final_outcome == "unknown_exit_observation"
    assert closed_report.final_outcome == "hypothetically_exitable"
    with pytest.raises(ValueError, match="terminal_unknown_halt|final transition"):
        diagnose_layer_two_longitudinal_state_transitions(
            structural=_make_structural(
                (
                    LayerTwoLongitudinalDayStructuralInput(
                        event_date=lifecycle.entry_trade_date,
                        entry=_entry_struct(lifecycle, life_struct),
                        exits=(),
                    ),
                    LayerTwoLongitudinalDayStructuralInput(
                        event_date=scheduled,
                        entry=None,
                        exits=(
                            _exit_struct(lifecycle, life_struct, stamp, calendar, unknown_obs, unknown_report),
                            _exit_struct(lifecycle, life_struct, stamp, calendar, closed_obs, closed_report),
                        ),
                    ),
                ),
                occ_report,
                occ_rows,
            )
        )


def test_current_state_binding_empty_rejected_carried_accepted_and_mismatches() -> None:
    bundle, _e, _f, _c, life_struct, lifecycle, stamp, calendar = _lifecycle_chain(extra_after_entry=80)
    phase = bundle.phase
    prior_as_of = life_struct.constraint_report.as_of
    cal = list(phase.market_calendar)

    as_of, decision_at = _later_opportunity_timing(
        phase, prior_as_of=prior_as_of, after_trade_date=lifecycle.entry_trade_date
    )
    empty_later = seal_layer_two_stateful_portfolio_state(
        LayerTwoStatefulPortfolioState(
            as_of=as_of,
            decision_at=decision_at,
            market_data_snapshot_id=lifecycle.market_data_snapshot_id,
            current_account_equity=float(BOUND_INITIAL_CASH),
            cash=float(BOUND_INITIAL_CASH),
            positions=[],
        )
    )
    life2, life2_struct = _rebuild_lifecycle_on_later_opportunity(
        bundle=bundle,
        phase=phase,
        prior_as_of=prior_as_of,
        prior_entry_trade_date=lifecycle.entry_trade_date,
        state=empty_later,
        constraint_equity=float(BOUND_INITIAL_CASH),
    )
    occ_report, occ_rows = _occupancy_from_rows(_fillable_row(life_struct), _fillable_row(life2_struct))
    with pytest.raises(ValueError, match="current_state.cash|positions must exactly match"):
        diagnose_layer_two_longitudinal_state_transitions(
            structural=_make_structural(
                (
                    LayerTwoLongitudinalDayStructuralInput(
                        event_date=lifecycle.entry_trade_date,
                        entry=_entry_struct(lifecycle, life_struct),
                        exits=(),
                    ),
                    LayerTwoLongitudinalDayStructuralInput(
                        event_date=life2.entry_trade_date,
                        entry=_entry_struct(life2, life2_struct),
                        exits=(),
                    ),
                ),
                occ_report,
                occ_rows,
            )
        )

    sod_cash = float(BOUND_INITIAL_CASH) - float(lifecycle.entry_total_cash_used)
    sod_positions = {
        str(lifecycle.record_id): LongitudinalActivePosition(
            lifecycle_record_id=str(lifecycle.record_id),
            symbol=lifecycle.symbol,
            tranche_id=int(lifecycle.tranche_id),
            cluster_id=lifecycle.cluster_id,
            shares=int(lifecycle.shares),
            entry_trade_date=lifecycle.entry_trade_date,
            stock_notional=float(lifecycle.stock_notional),
            buy_commission=float(lifecycle.buy_commission),
            entry_total_cash_used=float(lifecycle.entry_total_cash_used),
            status="open",
        )
    }
    carried = seal_layer_two_stateful_portfolio_state(
        LayerTwoStatefulPortfolioState(
            as_of=as_of,
            decision_at=decision_at,
            market_data_snapshot_id=lifecycle.market_data_snapshot_id,
            current_account_equity=sod_cash + float(lifecycle.stock_notional),
            cash=sod_cash,
            positions=[
                LayerTwoActiveTranchePosition(
                    tranche_id=int(lifecycle.tranche_id),
                    symbol=lifecycle.symbol,
                    current_market_notional=float(lifecycle.stock_notional),
                    cluster_id=lifecycle.cluster_id,
                )
            ],
        )
    )
    assert_lifecycle_current_state_matches_longitudinal_start_of_day(
        current_state=carried,
        sod_cash=sod_cash,
        sod_positions=sod_positions,
        longitudinal_market_data_snapshot_id=lifecycle.market_data_snapshot_id,
    )

    mismatch_cases = {
        "cash": {
            "cash": sod_cash + 1.0,
            "current_account_equity": sod_cash + 1.0 + float(lifecycle.stock_notional),
        },
        "notional": {
            "positions": [
                LayerTwoActiveTranchePosition(
                    tranche_id=int(lifecycle.tranche_id),
                    symbol=lifecycle.symbol,
                    current_market_notional=float(lifecycle.stock_notional) + 1.0,
                    cluster_id=lifecycle.cluster_id,
                )
            ],
            "current_account_equity": sod_cash + float(lifecycle.stock_notional) + 1.0,
        },
        "symbol": {
            "positions": [
                LayerTwoActiveTranchePosition(
                    tranche_id=int(lifecycle.tranche_id),
                    symbol="999999.SH",
                    current_market_notional=float(lifecycle.stock_notional),
                    cluster_id=lifecycle.cluster_id,
                )
            ],
        },
        "tranche": {
            "positions": [
                LayerTwoActiveTranchePosition(
                    tranche_id=int(lifecycle.tranche_id) + 1,
                    symbol=lifecycle.symbol,
                    current_market_notional=float(lifecycle.stock_notional),
                    cluster_id=lifecycle.cluster_id,
                )
            ],
        },
        "cluster": {
            "positions": [
                LayerTwoActiveTranchePosition(
                    tranche_id=int(lifecycle.tranche_id),
                    symbol=lifecycle.symbol,
                    current_market_notional=float(lifecycle.stock_notional),
                    cluster_id="cluster_other",
                )
            ],
        },
    }
    for _label, updates in mismatch_cases.items():
        payload = {
            "as_of": as_of,
            "decision_at": decision_at,
            "market_data_snapshot_id": lifecycle.market_data_snapshot_id,
            "current_account_equity": sod_cash + float(lifecycle.stock_notional),
            "cash": sod_cash,
            "positions": [
                LayerTwoActiveTranchePosition(
                    tranche_id=int(lifecycle.tranche_id),
                    symbol=lifecycle.symbol,
                    current_market_notional=float(lifecycle.stock_notional),
                    cluster_id=lifecycle.cluster_id,
                )
            ],
        }
        payload.update(updates)
        bad = seal_layer_two_stateful_portfolio_state(LayerTwoStatefulPortfolioState(**payload))
        with pytest.raises(ValueError, match="cash|notional|positions|stock_notional|cluster|tranche|symbol"):
            assert_lifecycle_current_state_matches_longitudinal_start_of_day(
                current_state=bad,
                sod_cash=sod_cash,
                sod_positions=sod_positions,
                longitudinal_market_data_snapshot_id=lifecycle.market_data_snapshot_id,
            )

    scheduled, observations, exit_report = _build_exit(
        lifecycle,
        life_struct,
        stamp,
        calendar,
        status="tradable",
        raw_open=FIXTURE_EXIT_OPEN,
        down_limit=FIXTURE_EXIT_DOWN_LIMIT,
    )
    closed_days = (
        LayerTwoLongitudinalDayStructuralInput(
            event_date=lifecycle.entry_trade_date,
            entry=_entry_struct(lifecycle, life_struct),
            exits=(),
        ),
        LayerTwoLongitudinalDayStructuralInput(
            event_date=scheduled,
            entry=None,
            exits=(_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),),
        ),
    )
    closed_occ_report, closed_occ_rows = _occupancy_from_life_struct(life_struct)
    closed_report = diagnose_layer_two_longitudinal_state_transitions(
        structural=_make_structural(closed_days, closed_occ_report, closed_occ_rows)
    )
    ending_cash = float(closed_report.ending_cash)
    as3, decision3 = _later_opportunity_timing(phase, prior_as_of=prior_as_of, after_trade_date=scheduled)
    carried_flat = seal_layer_two_stateful_portfolio_state(
        LayerTwoStatefulPortfolioState(
            as_of=as3,
            decision_at=decision3,
            market_data_snapshot_id=lifecycle.market_data_snapshot_id,
            current_account_equity=ending_cash,
            cash=ending_cash,
            positions=[],
        )
    )
    life3, life3_struct = _rebuild_lifecycle_on_later_opportunity(
        bundle=bundle,
        phase=phase,
        prior_as_of=prior_as_of,
        prior_entry_trade_date=scheduled,
        state=carried_flat,
        constraint_equity=ending_cash,
    )
    multi_occ_report, multi_occ_rows = _occupancy_from_rows(
        _fillable_row(life_struct),
        _fillable_row(life3_struct),
    )
    accepted = diagnose_layer_two_longitudinal_state_transitions(
        structural=_make_structural(
            (
                *closed_days,
                LayerTwoLongitudinalDayStructuralInput(
                    event_date=life3.entry_trade_date,
                    entry=_entry_struct(life3, life3_struct),
                    exits=(),
                ),
            ),
            multi_occ_report,
            multi_occ_rows,
        )
    )
    assert accepted.open_position_count == 1
    assert accepted.closed_position_count == 1
    assert life3_struct.current_state.cash == ending_cash
    _ = stamp, calendar, cal, occ_report, occ_rows


def test_insufficient_cash_duplicate_exit_and_mismatch() -> None:
    _b, _e, _f, _c, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    occ_report, occ_rows = _occupancy_from_life_struct(life_struct)
    scheduled, observations, exit_report = _build_exit(
        lifecycle,
        life_struct,
        stamp,
        calendar,
        status="tradable",
        raw_open=FIXTURE_EXIT_OPEN,
        down_limit=FIXTURE_EXIT_DOWN_LIMIT,
    )
    entry = _entry_struct(lifecycle, life_struct)

    with patch("app.research.layer_two_longitudinal_state_transitions.BOUND_INITIAL_CASH", 1000):
        with pytest.raises(ValueError, match="insufficient cash|current_state.cash"):
            diagnose_layer_two_longitudinal_state_transitions(
                structural=_make_structural(
                    (
                        LayerTwoLongitudinalDayStructuralInput(
                            event_date=lifecycle.entry_trade_date,
                            entry=entry,
                            exits=(),
                        ),
                    ),
                    occ_report,
                    occ_rows,
                )
            )

    with pytest.raises(ValueError, match="currently tracked|open"):
        diagnose_layer_two_longitudinal_state_transitions(
            structural=_make_structural(
                (
                    LayerTwoLongitudinalDayStructuralInput(
                        event_date=scheduled,
                        entry=None,
                        exits=(_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),),
                    ),
                ),
                occ_report,
                occ_rows,
            )
        )

    sched2, obs2, deferred = _build_exit(lifecycle, life_struct, stamp, calendar, status="known_full_day_suspension")
    with pytest.raises(ValueError, match="duplicate lifecycle_record_id exits"):
        diagnose_layer_two_longitudinal_state_transitions(
            structural=_make_structural(
                (
                    LayerTwoLongitudinalDayStructuralInput(
                        event_date=lifecycle.entry_trade_date,
                        entry=entry,
                        exits=(),
                    ),
                    LayerTwoLongitudinalDayStructuralInput(
                        event_date=sched2,
                        entry=None,
                        exits=(
                            _exit_struct(lifecycle, life_struct, stamp, calendar, obs2, deferred),
                            _exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),
                        ),
                    ),
                ),
                occ_report,
                occ_rows,
            )
        )

    with pytest.raises(ValidationError, match="shares|sell scenarios"):
        type(exit_report).model_validate(
            {**exit_report.model_dump(mode="json"), "shares": int(lifecycle.shares) + 100, "report_id": None}
        )


def test_same_day_exit_cash_cannot_fund_entry() -> None:
    """Entry is judged on start-of-day cash; same-day exit proceeds are not reusable."""
    _b, _e, _f, _c, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    occ_report, occ_rows = _occupancy_from_life_struct(life_struct)
    scheduled, observations, exit_report = _build_exit(
        lifecycle,
        life_struct,
        stamp,
        calendar,
        status="tradable",
        raw_open=FIXTURE_EXIT_OPEN,
        down_limit=FIXTURE_EXIT_DOWN_LIMIT,
    )
    with pytest.raises(ValueError, match="entry_trade_date|opened only once|insufficient cash|occupied|current_state"):
        diagnose_layer_two_longitudinal_state_transitions(
            structural=_make_structural(
                (
                    LayerTwoLongitudinalDayStructuralInput(
                        event_date=lifecycle.entry_trade_date,
                        entry=_entry_struct(lifecycle, life_struct),
                        exits=(),
                    ),
                    LayerTwoLongitudinalDayStructuralInput(
                        event_date=scheduled,
                        entry=_entry_struct(lifecycle, life_struct),
                        exits=(_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),),
                    ),
                ),
                occ_report,
                occ_rows,
            )
        )


def test_verification_result_state_machine() -> None:
    rid = "ab" * 32
    LayerTwoLongitudinalVerificationResult(
        report_id=rid,
        structural_ok=True,
        lifecycle_bindings_ok=False,
        exit_bindings_ok=False,
        allocation_protocol_binding_ok=False,
        cash_occupancy_attribution_binding_ok=False,
        ready_for_longitudinal_diagnostic=False,
    )
    LayerTwoLongitudinalVerificationResult(
        report_id=rid,
        structural_ok=True,
        lifecycle_bindings_ok=True,
        exit_bindings_ok=True,
        allocation_protocol_binding_ok=True,
        cash_occupancy_attribution_binding_ok=True,
        ready_for_longitudinal_diagnostic=True,
    )
    with pytest.raises(ValidationError, match="partial bindings"):
        LayerTwoLongitudinalVerificationResult(
            report_id=rid,
            structural_ok=True,
            lifecycle_bindings_ok=True,
            exit_bindings_ok=False,
            allocation_protocol_binding_ok=False,
            cash_occupancy_attribution_binding_ok=False,
            ready_for_longitudinal_diagnostic=False,
        )
    with pytest.raises(ValidationError, match="partial bindings"):
        LayerTwoLongitudinalVerificationResult(
            report_id=rid,
            structural_ok=True,
            lifecycle_bindings_ok=True,
            exit_bindings_ok=True,
            allocation_protocol_binding_ok=False,
            cash_occupancy_attribution_binding_ok=True,
            ready_for_longitudinal_diagnostic=False,
        )
    with pytest.raises(ValidationError, match="ready_for_longitudinal_diagnostic=true requires"):
        LayerTwoLongitudinalVerificationResult(
            report_id=rid,
            structural_ok=True,
            lifecycle_bindings_ok=False,
            exit_bindings_ok=False,
            allocation_protocol_binding_ok=False,
            cash_occupancy_attribution_binding_ok=False,
            ready_for_longitudinal_diagnostic=True,
        )
    with pytest.raises(ValidationError, match="structural_ok=false forbids"):
        LayerTwoLongitudinalVerificationResult(
            report_id=rid,
            structural_ok=False,
            lifecycle_bindings_ok=True,
            exit_bindings_ok=False,
            allocation_protocol_binding_ok=False,
            cash_occupancy_attribution_binding_ok=False,
            ready_for_longitudinal_diagnostic=False,
        )


def test_report_tamper_and_ready_injection() -> None:
    structural = _happy_close_bundle()[-1]
    report = diagnose_layer_two_longitudinal_state_transitions(structural=structural)
    payload = report.model_dump(mode="json")
    payload["ending_cash"] = float(payload["ending_cash"]) + 1.0
    payload.pop("report_id", None)
    with pytest.raises(ValidationError, match="cash-flow identity|ending_cash"):
        LayerTwoLongitudinalStateTransitionReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    payload["closed_position_count"] = int(payload["closed_position_count"]) + 1
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoLongitudinalStateTransitionReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    payload["ready_for_scoring"] = True
    payload.pop("report_id", None)
    with pytest.raises(ValidationError, match="ready_for_scoring"):
        LayerTwoLongitudinalStateTransitionReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    payload["extra_field"] = 1
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoLongitudinalStateTransitionReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    payload["ending_cash"] = float("nan")
    payload.pop("report_id", None)
    with pytest.raises(ValidationError, match="finite|NaN"):
        LayerTwoLongitudinalStateTransitionReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    payload["ready_for_longitudinal_diagnostic"] = True
    payload.pop("report_id", None)
    with pytest.raises(ValidationError, match="ready_for_longitudinal_diagnostic"):
        LayerTwoLongitudinalStateTransitionReport.model_validate(payload)

    resealed = seal_layer_two_longitudinal_state_transition_report(report.model_copy(update={"report_id": None}))
    assert resealed.report_id == report.report_id
    bad = report.model_copy(update={"report_id": "ab" * 32})
    with pytest.raises(ValueError, match="report_id"):
        verify_layer_two_longitudinal_state_transition_report(bad, structural=structural)


def test_e10e1_occupancy_attacks() -> None:
    bundle, _e, _f, _c, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    occ_report, occ_rows, not_meta = _occupancy_with_not_attempted_and_fillable(bundle, life_struct, lifecycle)
    scheduled, observations, exit_report = _build_exit(
        lifecycle,
        life_struct,
        stamp,
        calendar,
        status="tradable",
        raw_open=FIXTURE_EXIT_OPEN,
        down_limit=FIXTURE_EXIT_DOWN_LIMIT,
    )
    base_days = (
        LayerTwoLongitudinalDayStructuralInput(
            event_date=lifecycle.entry_trade_date,
            entry=_entry_struct(lifecycle, life_struct),
            exits=(),
        ),
        LayerTwoLongitudinalDayStructuralInput(
            event_date=scheduled,
            entry=None,
            exits=(_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),),
        ),
    )
    base_structural = _make_structural(base_days, occ_report, occ_rows)

    tampered_occ = occ_report.model_copy(update={"report_id": "ff" * 32})
    with pytest.raises(ValueError, match="report_id|self-hash|recompute"):
        diagnose_layer_two_longitudinal_state_transitions(
            structural=_make_structural(base_days, tampered_occ, occ_rows)
        )

    not_only_rows = (next(row for row in occ_rows if row.entry_execution_report.outcome == "not_attempted"),)
    not_only_report = attribute_layer_two_cash_occupancy(list(not_only_rows))
    with pytest.raises(ValueError, match="missing from E10e-1|entry_execution_report_id"):
        diagnose_layer_two_longitudinal_state_transitions(
            structural=_make_structural(base_days, not_only_report, not_only_rows)
        )

    not_row = next(row for row in occ_rows if row.entry_execution_report.outcome == "not_attempted")
    only_not_for_lifecycle_id = attribute_layer_two_cash_occupancy([not_row])
    with pytest.raises(ValueError, match="missing from E10e-1|entry_execution_report_id"):
        diagnose_layer_two_longitudinal_state_transitions(
            structural=_make_structural(base_days, only_not_for_lifecycle_id, (not_row,))
        )

    fillable_occ_row = next(
        row for row in occ_report.rows if row.entry_execution_report_id == lifecycle.entry_execution_report_id
    )
    wrong_used = float(lifecycle.entry_total_cash_used) + 1.0
    target = float(fillable_occ_row.known_target_cash or 0.0)
    forged_cash_rows: list[CashOccupancyRowAttribution] = []
    for occ_row in occ_report.rows:
        if occ_row.entry_execution_report_id == lifecycle.entry_execution_report_id:
            forged_cash_rows.append(
                occ_row.model_copy(
                    update={
                        "known_base_cash_used": wrong_used,
                        "known_retained_cash": target - wrong_used,
                    }
                )
            )
        else:
            forged_cash_rows.append(occ_row)
    forged_cash_report = seal_layer_two_cash_occupancy_attribution_report(
        occ_report.model_copy(update={"rows": forged_cash_rows, "report_id": None})
    )
    ok_verify = LayerTwoCashOccupancyAttributionVerificationResult(
        report_id=forged_cash_report.report_id or ("ab" * 32),
        structural_ok=True,
        entry_execution_binding_ok=False,
        phase_binding_ok=False,
        tranche_evaluation_protocol_binding_ok=False,
    )

    def _patch_ok(report, rows=()):
        return ok_verify.model_copy(update={"report_id": report.report_id or ok_verify.report_id})

    with patch(
        "app.research.layer_two_longitudinal_state_transitions.verify_layer_two_cash_occupancy_attribution_report",
        side_effect=_patch_ok,
    ):
        with pytest.raises(ValueError, match="known_base_cash_used|entry_total_cash_used"):
            diagnose_layer_two_longitudinal_state_transitions(
                structural=_make_structural(base_days, forged_cash_report, occ_rows)
            )

    forged_non_fill_rows: list[CashOccupancyRowAttribution] = []
    for occ_row in occ_report.rows:
        if occ_row.entry_execution_report_id == lifecycle.entry_execution_report_id:
            forged_non_fill_rows.append(
                CashOccupancyRowAttribution(
                    as_of=occ_row.as_of,
                    entry_execution_report_id=lifecycle.entry_execution_report_id,
                    allocator_report_id=occ_row.allocator_report_id,
                    execution_outcome="not_attempted",
                    cause_marker="gates",
                    amount_quantified=False,
                    classification_evidence="synthetic non-fill mapping attack row",
                )
            )
        else:
            forged_non_fill_rows.append(occ_row)
    forged_non_fill_report = seal_layer_two_cash_occupancy_attribution_report(
        occ_report.model_copy(
            update={
                "rows": forged_non_fill_rows,
                "report_id": None,
                "total_attempt_count": occ_report.total_attempt_count - 1,
                "total_not_attempt_count": occ_report.total_not_attempt_count + 1,
            }
        )
    )
    with patch(
        "app.research.layer_two_longitudinal_state_transitions.verify_layer_two_cash_occupancy_attribution_report",
        side_effect=_patch_ok,
    ):
        with pytest.raises(ValueError, match="hypothetically_fillable|not_attempted"):
            diagnose_layer_two_longitudinal_state_transitions(
                structural=_make_structural(base_days, forged_non_fill_report, occ_rows)
            )

    from tests.test_layer_two_cash_occupancy_attribution import _alt_snapshot_row

    drift_row = _alt_snapshot_row()
    drift_report = attribute_layer_two_cash_occupancy([drift_row])
    with pytest.raises(ValueError, match="market_data_snapshot_id|phase_report_id|E10e-1"):
        diagnose_layer_two_longitudinal_state_transitions(
            structural=_make_structural(base_days, drift_report, (drift_row,))
        )

    _ = not_meta, base_structural


def test_file_verifier_ready_and_forged_upstream(tmp_path: Path) -> None:
    (
        bundle,
        eligibility,
        financials,
        cluster,
        life_struct,
        lifecycle,
        stamp,
        calendar,
        observations,
        exit_report,
        occ_report,
        occ_rows,
        structural,
    ) = _happy_close_bundle()
    report = diagnose_layer_two_longitudinal_state_transitions(structural=structural)
    phase_path = tmp_path / "phase.json"
    write_layer_two_tranche_phase_schedule_report(phase_path, bundle.phase)
    lifecycle_file = LayerTwoHypotheticalLifecycleFileInput(
        structural=life_struct,
        file_bindings=LayerTwoHypotheticalLifecycleFileBindings(
            eligibility_report=eligibility,
            financial_reports=tuple(financials),
            cluster_report=cluster,
            store=bundle.store,
            repo_root=REPO_ROOT,
            phase_report_path=phase_path,
        ),
    )
    exit_file = LayerTwoFixedHorizonExitFileInput(
        structural=_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report).exit_structural,
        lifecycle_file=lifecycle_file,
        stamp_tax_repo_root=REPO_ROOT,
    )
    occ_file_rows = _build_occupancy_file_rows(
        occ_rows,
        phase_path=phase_path,
        fillable_meta=(eligibility, financials, cluster, bundle.store),
    )
    file_input = LayerTwoLongitudinalFileInput(
        days=(
            LayerTwoLongitudinalDayFileInput(
                event_date=lifecycle.entry_trade_date,
                entry=LayerTwoLongitudinalEntryFileInput(
                    structural=_entry_struct(lifecycle, life_struct),
                    lifecycle_file=lifecycle_file,
                ),
                exits=(),
            ),
            LayerTwoLongitudinalDayFileInput(
                event_date=_scheduled(calendar, lifecycle.entry_trade_date),
                entry=None,
                exits=(
                    LayerTwoLongitudinalExitFileInput(
                        structural=_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),
                        exit_file=exit_file,
                    ),
                ),
            ),
        ),
        repo_root=REPO_ROOT,
        cash_occupancy_report=occ_report,
        cash_occupancy_file_rows=occ_file_rows,
    )
    file_ok = verify_layer_two_longitudinal_state_transition_report_file(report=report, file_input=file_input)
    assert file_ok.lifecycle_bindings_ok is True
    assert file_ok.exit_bindings_ok is True
    assert file_ok.allocation_protocol_binding_ok is True
    assert file_ok.cash_occupancy_attribution_binding_ok is True
    assert file_ok.ready_for_longitudinal_diagnostic is True

    def _fake_life(**kwargs):
        return LayerTwoHypotheticalPositionLifecycleVerificationResult(
            record_id=kwargs["record"].record_id or ("ab" * 32),
            structural_ok=True,
            entry_execution_binding_ok=False,
            allocator_binding_ok=False,
            phase_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
            ready_for_lifecycle_diagnostic=False,
        )

    with patch(
        "app.research.layer_two_longitudinal_state_transitions.verify_layer_two_hypothetical_position_lifecycle_record_file",
        side_effect=_fake_life,
    ):
        with pytest.raises(ValueError, match="E10f-1 file verifier"):
            verify_layer_two_longitudinal_state_transition_report_file(report=report, file_input=file_input)

    def _fake_exit(**kwargs):
        return LayerTwoFixedHorizonExitVerificationResult(
            report_id=kwargs["report"].report_id or ("ab" * 32),
            structural_ok=True,
            lifecycle_binding_ok=False,
            stamp_tax_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
            exit_observation_binding_ok=False,
            ready_for_exit_diagnostic=False,
        )

    with patch(
        "app.research.layer_two_longitudinal_state_transitions.verify_layer_two_fixed_horizon_exit_diagnostic_report_file",
        side_effect=_fake_exit,
    ):
        with pytest.raises(ValueError, match="E10f-2 file verifier"):
            verify_layer_two_longitudinal_state_transition_report_file(report=report, file_input=file_input)

    def _fake_alloc(**kwargs):
        doc, result = verify_layer_two_allocation_protocol_file(**kwargs)
        return doc.model_copy(
            update={"capital_budget": doc.capital_budget.model_copy(update={"initial_cash": 80000})}
        ), result.model_copy(update={"protocol_id": "ff" * 32})

    with patch(
        "app.research.layer_two_longitudinal_state_transitions.verify_layer_two_allocation_protocol_file",
        side_effect=_fake_alloc,
    ):
        with pytest.raises(ValueError, match="allocation protocol_id|protocol_id"):
            verify_layer_two_longitudinal_state_transition_report_file(report=report, file_input=file_input)

    def _fake_e10e1_partial(**kwargs):
        return LayerTwoCashOccupancyAttributionVerificationResult(
            report_id=kwargs["report"].report_id or ("ab" * 32),
            structural_ok=True,
            entry_execution_binding_ok=True,
            phase_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
        )

    with patch(
        "app.research.layer_two_longitudinal_state_transitions.verify_layer_two_cash_occupancy_attribution_report_file",
        side_effect=_fake_e10e1_partial,
    ):
        with pytest.raises(ValueError, match="E10e-1 file verifier"):
            verify_layer_two_longitudinal_state_transition_report_file(report=report, file_input=file_input)

    mixed_entry = LayerTwoHypotheticalLifecycleFileInput(
        structural=life_struct,
        file_bindings=LayerTwoHypotheticalLifecycleFileBindings(
            eligibility_report=eligibility,
            financial_reports=tuple(financials),
            cluster_report=cluster,
            store=bundle.store,
            repo_root=tmp_path,
            phase_report_path=phase_path,
        ),
    )
    mixed_file = LayerTwoLongitudinalFileInput(
        days=(
            LayerTwoLongitudinalDayFileInput(
                event_date=lifecycle.entry_trade_date,
                entry=LayerTwoLongitudinalEntryFileInput(
                    structural=_entry_struct(lifecycle, life_struct),
                    lifecycle_file=mixed_entry,
                ),
                exits=(),
            ),
            LayerTwoLongitudinalDayFileInput(
                event_date=_scheduled(calendar, lifecycle.entry_trade_date),
                entry=None,
                exits=(
                    LayerTwoLongitudinalExitFileInput(
                        structural=_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),
                        exit_file=exit_file,
                    ),
                ),
            ),
        ),
        repo_root=REPO_ROOT,
        cash_occupancy_report=occ_report,
        cash_occupancy_file_rows=occ_file_rows,
    )
    with pytest.raises(ValueError, match="repo_root"):
        verify_layer_two_longitudinal_state_transition_report_file(report=report, file_input=mixed_file)

    mixed_exit = LayerTwoFixedHorizonExitFileInput(
        structural=_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report).exit_structural,
        lifecycle_file=lifecycle_file,
        stamp_tax_repo_root=tmp_path,
    )
    mixed_exit_file = LayerTwoLongitudinalFileInput(
        days=file_input.days[:1]
        + (
            LayerTwoLongitudinalDayFileInput(
                event_date=_scheduled(calendar, lifecycle.entry_trade_date),
                entry=None,
                exits=(
                    LayerTwoLongitudinalExitFileInput(
                        structural=_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),
                        exit_file=mixed_exit,
                    ),
                ),
            ),
        ),
        repo_root=REPO_ROOT,
        cash_occupancy_report=occ_report,
        cash_occupancy_file_rows=occ_file_rows,
    )
    with pytest.raises(ValueError, match="repo_root"):
        verify_layer_two_longitudinal_state_transition_report_file(report=report, file_input=mixed_exit_file)

    mixed_occ_rows = (
        LayerTwoCashOccupancyFileRowInput(
            structural=occ_file_rows[0].structural,
            file_bindings=LayerTwoCashOccupancyFileRowBindings(
                eligibility_report=eligibility,
                financial_reports=tuple(financials),
                cluster_report=cluster,
                store=bundle.store,
                repo_root=tmp_path,
                phase_report_path=phase_path,
            ),
        ),
    )
    mixed_occ_file = LayerTwoLongitudinalFileInput(
        days=file_input.days,
        repo_root=REPO_ROOT,
        cash_occupancy_report=occ_report,
        cash_occupancy_file_rows=mixed_occ_rows,
    )
    with pytest.raises(ValueError, match="E10e-1 cash occupancy|repo_root"):
        verify_layer_two_longitudinal_state_transition_report_file(report=report, file_input=mixed_occ_file)


def test_file_verifier_with_not_attempted_occupancy_rows(tmp_path: Path) -> None:
    bundle, eligibility, financials, cluster, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    occ_report, occ_rows, not_meta = _occupancy_with_not_attempted_and_fillable(bundle, life_struct, lifecycle)
    scheduled, observations, exit_report = _build_exit(
        lifecycle,
        life_struct,
        stamp,
        calendar,
        status="tradable",
        raw_open=FIXTURE_EXIT_OPEN,
        down_limit=FIXTURE_EXIT_DOWN_LIMIT,
    )
    structural = _make_structural(
        (
            LayerTwoLongitudinalDayStructuralInput(
                event_date=lifecycle.entry_trade_date,
                entry=_entry_struct(lifecycle, life_struct),
                exits=(),
            ),
            LayerTwoLongitudinalDayStructuralInput(
                event_date=scheduled,
                entry=None,
                exits=(_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),),
            ),
        ),
        occ_report,
        occ_rows,
    )
    report = diagnose_layer_two_longitudinal_state_transitions(structural=structural)
    phase_path = tmp_path / "phase.json"
    write_layer_two_tranche_phase_schedule_report(phase_path, bundle.phase)
    lifecycle_file = LayerTwoHypotheticalLifecycleFileInput(
        structural=life_struct,
        file_bindings=LayerTwoHypotheticalLifecycleFileBindings(
            eligibility_report=eligibility,
            financial_reports=tuple(financials),
            cluster_report=cluster,
            store=bundle.store,
            repo_root=REPO_ROOT,
            phase_report_path=phase_path,
        ),
    )
    exit_file = LayerTwoFixedHorizonExitFileInput(
        structural=_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report).exit_structural,
        lifecycle_file=lifecycle_file,
        stamp_tax_repo_root=REPO_ROOT,
    )
    occ_file_rows = _build_occupancy_file_rows(
        occ_rows,
        phase_path=phase_path,
        fillable_meta=(eligibility, financials, cluster, bundle.store),
        not_attempted_meta=not_meta,
    )
    assert len(occ_file_rows) == 2
    file_input = LayerTwoLongitudinalFileInput(
        days=(
            LayerTwoLongitudinalDayFileInput(
                event_date=lifecycle.entry_trade_date,
                entry=LayerTwoLongitudinalEntryFileInput(
                    structural=_entry_struct(lifecycle, life_struct),
                    lifecycle_file=lifecycle_file,
                ),
                exits=(),
            ),
            LayerTwoLongitudinalDayFileInput(
                event_date=scheduled,
                entry=None,
                exits=(
                    LayerTwoLongitudinalExitFileInput(
                        structural=_exit_struct(lifecycle, life_struct, stamp, calendar, observations, exit_report),
                        exit_file=exit_file,
                    ),
                ),
            ),
        ),
        repo_root=REPO_ROOT,
        cash_occupancy_report=occ_report,
        cash_occupancy_file_rows=occ_file_rows,
    )
    file_ok = verify_layer_two_longitudinal_state_transition_report_file(report=report, file_input=file_input)
    assert file_ok.cash_occupancy_attribution_binding_ok is True
    assert (
        verify_layer_two_cash_occupancy_attribution_report_file(
            report=occ_report,
            rows=occ_file_rows,
        ).entry_execution_binding_ok
        is True
    )


def test_e10e1_forged_occupancy_recompute_on_diagnose() -> None:
    structural = _happy_close_bundle()[-1]
    occ_report = structural.cash_occupancy_report
    occ_rows = structural.cash_occupancy_rows
    days = structural.days
    tampered = seal_layer_two_cash_occupancy_attribution_report(
        occ_report.model_copy(
            update={
                "global_sum_known_base_cash_used": occ_report.global_sum_known_base_cash_used + 1.0,
                "report_id": None,
            }
        )
    )
    with pytest.raises(ValueError, match="structural_ok|recompute|report_id"):
        diagnose_layer_two_longitudinal_state_transitions(structural=_make_structural(days, tampered, occ_rows))


def test_e10e1_future_not_attempted_row_outside_declared_window_rejected() -> None:
    """Outer-boundary isolation: patch only E10e-1 structural verifier; sealed outer dates spill to 2025+.

    Real 2025+ fixture recompute is impractical under the frozen 2022..2024 store calendar, so the
    upstream occupancy verifier is patched while the sealed occupancy report carries a future
    not_attempted as_of / coverage end. Longitudinal must still fail closed on declared_window.
    """
    bundle, _e, _f, _c, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    occ_report, occ_rows, _meta = _occupancy_with_not_attempted_and_fillable(bundle, life_struct, lifecycle)
    future = date(2025, 1, 8)
    rewritten_rows = []
    for row in occ_report.rows:
        if row.execution_outcome == "not_attempted":
            rewritten_rows.append(row.model_copy(update={"as_of": future}))
        else:
            rewritten_rows.append(row)
    rewritten_rows = sorted(rewritten_rows, key=lambda row: row.as_of)
    forged = seal_layer_two_cash_occupancy_attribution_report(
        occ_report.model_copy(
            update={
                "rows": rewritten_rows,
                "coverage_as_of_start": rewritten_rows[0].as_of,
                "coverage_as_of_end": rewritten_rows[-1].as_of,
                "input_entry_execution_report_ids": [row.entry_execution_report_id for row in rewritten_rows],
                "report_id": None,
            }
        )
    )
    assert forged.coverage_as_of_end == future
    days = (
        LayerTwoLongitudinalDayStructuralInput(
            event_date=lifecycle.entry_trade_date,
            entry=_entry_struct(lifecycle, life_struct),
            exits=(),
        ),
    )

    def _fake_occ_verify(report, *, rows):
        return LayerTwoCashOccupancyAttributionVerificationResult(
            report_id=report.report_id or ("ab" * 32),
            structural_ok=True,
            entry_execution_binding_ok=False,
            phase_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
        )

    with (
        patch(
            "app.research.layer_two_longitudinal_state_transitions.assert_cash_occupancy_self_hash",
            return_value=None,
        ),
        patch(
            "app.research.layer_two_longitudinal_state_transitions.verify_layer_two_cash_occupancy_attribution_report",
            side_effect=_fake_occ_verify,
        ),
    ):
        with pytest.raises(ValueError, match="declared_window|2025|coverage_as_of|as_of"):
            diagnose_layer_two_longitudinal_state_transitions(structural=_make_structural(days, forged, occ_rows))


def test_e10e1_future_expected_t1_or_observation_outside_declared_window_rejected() -> None:
    """Outer-boundary isolation: fillable structural entry T1/observation spills into 2025+.

    Upstream E10e-1 verifier is patched so only longitudinal declared_window enforcement is tested.
    """
    *_, life_struct, lifecycle, _stamp, _calendar, _obs, _exit, _occ_report, _occ_rows, structural = (
        _happy_close_bundle()
    )
    occ_report = structural.cash_occupancy_report
    occ_rows = list(structural.cash_occupancy_rows)
    fill_idx = next(
        i for i, row in enumerate(occ_rows) if row.entry_execution_report.outcome == "hypothetically_fillable"
    )
    fill_row = occ_rows[fill_idx]
    future = date(2025, 1, 9)
    entry = fill_row.entry_execution_report
    observation = fill_row.execution_observation
    assert observation is not None
    bad_entry = entry.model_copy(update={"expected_t1_execution_date": future})
    bad_obs = observation.model_copy(update={"execution_date": future})
    occ_rows[fill_idx] = replace(
        fill_row,
        entry_execution_report=bad_entry,
        execution_observation=bad_obs,
    )

    def _fake_occ_verify(report, *, rows):
        return LayerTwoCashOccupancyAttributionVerificationResult(
            report_id=report.report_id or ("ab" * 32),
            structural_ok=True,
            entry_execution_binding_ok=False,
            phase_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
        )

    with (
        patch(
            "app.research.layer_two_longitudinal_state_transitions.assert_cash_occupancy_self_hash",
            return_value=None,
        ),
        patch(
            "app.research.layer_two_longitudinal_state_transitions.verify_layer_two_cash_occupancy_attribution_report",
            side_effect=_fake_occ_verify,
        ),
    ):
        with pytest.raises(ValueError, match="declared_window|2025|expected_t1|execution_date"):
            diagnose_layer_two_longitudinal_state_transitions(
                structural=_make_structural(structural.days, occ_report, tuple(occ_rows))
            )
    _ = lifecycle, life_struct


def test_no_production_imports_or_pnl_helpers() -> None:
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
    for needle in ("Sharpe", "mark_to_market", "annualized", "ScoringEngine", "BacktestEngine"):
        assert needle not in source
    assert "def compute_return" not in source
    assert "def compute_pnl" not in source
    assert "ready_for_scoring: Literal[False]" in source
    assert "cost-notional" in source
    assert 'LAYER_TWO_LONGITUDINAL_SCHEMA_VERSION: Literal["2"]' in source
    assert "cash_occupancy_attribution_binding_ok" in source
