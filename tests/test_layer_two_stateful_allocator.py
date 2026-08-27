"""Attack-oriented tests for layer-two stateful allocator (E10d-3)."""

from __future__ import annotations

import ast
import json
import math
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.research.layer_two_constraint_assembler import (
    LayerTwoConstraintAssemblerReport,
    assemble_layer_two_constraints,
    seal_layer_two_constraint_assembler_report,
)
from app.research.layer_two_constraint_assembler import (
    compute_report_id as compute_constraint_report_id,
)
from app.research.layer_two_stateful_allocator import (
    STATE_EQUITY_ABS_TOL,
    LayerTwoActiveTranchePosition,
    LayerTwoStatefulAllocatorReport,
    LayerTwoStatefulPortfolioState,
    UnvalidatedDevelopmentRankingInput,
    allocate_layer_two_stateful_single_opportunity,
    compute_report_id,
    seal_layer_two_stateful_allocator_report,
    seal_layer_two_stateful_portfolio_state,
    verify_layer_two_stateful_allocator_report,
    verify_layer_two_stateful_allocator_report_file,
)
from app.research.layer_two_tranche_phase_schedule import (
    plan_layer_two_tranche_phase_schedule,
    write_layer_two_tranche_phase_schedule_report,
)
from tests.helpers import PROJECT_ROOT
from tests.test_layer_two_constraint_assembler import (
    _assemble,
    _Bundle,
    _cluster,
    _eligibility,
    _financials_for,
)

REPO_ROOT = PROJECT_ROOT
MODULE_PATH = REPO_ROOT / "src/app/research/layer_two_stateful_allocator.py"


def _ranking(symbols: list[str]) -> UnvalidatedDevelopmentRankingInput:
    return UnvalidatedDevelopmentRankingInput(ranked_symbols=list(symbols))


def _empty_state(constraint: LayerTwoConstraintAssemblerReport) -> LayerTwoStatefulPortfolioState:
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


def _state_with_positions(
    constraint: LayerTwoConstraintAssemblerReport,
    positions: list[LayerTwoActiveTranchePosition],
    *,
    cash: float | None = None,
) -> LayerTwoStatefulPortfolioState:
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


def _happy_bundle_inputs():
    bundle = _Bundle(anchor_index=0)
    eligibility = _eligibility(bundle)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    financials = _financials_for(bundle, eligible)
    cluster = _cluster(bundle, eligible)
    constraint = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=bundle.phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )
    return bundle, eligibility, financials, cluster, constraint, eligible


def test_happy_path_selects_first_admissible() -> None:
    bundle, eligibility, financials, cluster, constraint, eligible = _happy_bundle_inputs()
    assert constraint.as_of_has_selected_phase_opportunity is True
    ranking = _ranking(eligible)
    state = _empty_state(constraint)
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert report.diagnostic_only is True
    assert report.ready_for_allocation_diagnostic is True
    assert report.ready_for_allocation_diagnostic_is_not_production_ready is True
    assert report.ready_for_orders is False
    assert report.ready_for_trading is False
    assert report.auto_apply is False
    assert report.proposed_entry is not None
    assert report.portfolio_cash_retention_reason is None
    assert report.proposed_entry.symbol == eligible[0]
    assert report.proposed_entry.ranking_position == 0
    assert report.proposed_entry.tranche_id == constraint.selected_phase_opportunity.tranche_id
    assert report.proposed_entry.target_notional == 8_000.0
    assert report.accounting.proposed_cash == constraint.current_account_equity - 8_000.0
    assert report.accounting.proposed_gross_notional == 8_000.0
    assert report.constraint_assembler_report_id == constraint.report_id
    assert report.current_state_id == state.state_id
    assert report.phase_report_id == constraint.phase_report_id
    dumped = report.model_dump()
    assert "quantity" not in dumped
    assert "price" not in dumped
    assert "order_side" not in dumped
    structural = verify_layer_two_stateful_allocator_report(
        report,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert structural.structural_ok is True
    assert structural.phase_binding_ok is False
    assert structural.constraint_assembler_binding_ok is False


def test_deterministic_ranking_order_changes_selection() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    state = _empty_state(constraint)
    first = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=_ranking(eligible),
    )
    reversed_rank = list(reversed(eligible))
    second = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=_ranking(reversed_rank),
    )
    assert first.proposed_entry is not None
    assert second.proposed_entry is not None
    assert first.proposed_entry.symbol == eligible[0]
    assert second.proposed_entry.symbol == eligible[-1]
    again = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=_ranking(eligible),
    )
    assert again.report_id == first.report_id


def test_held_name_skipped() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    # Hold an eligible name on another tranche with tiny notional so the next
    # same-cluster candidate remains under the sleeve cap.
    held = eligible[0]
    row = next(r for r in constraint.rows if r.symbol == held)
    state = _state_with_positions(
        constraint,
        [
            LayerTwoActiveTranchePosition(
                tranche_id=1,
                symbol=held,
                current_market_notional=100.0,
                cluster_id=row.cluster_id or "cluster_001",
            )
        ],
    )
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=_ranking(eligible),
    )
    assert report.proposed_entry is not None
    assert report.proposed_entry.symbol == eligible[1]
    assert report.candidate_rejection_diagnostics[0].reason == "already_held"
    assert report.candidate_rejection_diagnostics[0].symbol == held


def test_occupied_selected_tranche_retains_cash() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    selected = constraint.selected_phase_opportunity.tranche_id
    other = eligible[2]
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
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=_ranking(eligible),
    )
    assert report.proposed_entry is None
    assert report.portfolio_cash_retention_reason == "selected_tranche_occupied"
    assert report.candidate_rejection_diagnostics == []


def test_no_phase_opportunity_retains_cash() -> None:
    from datetime import UTC, date, datetime

    from tests.helpers import weekdays
    from tests.test_layer_two_statistical_risk_clusters import (
        LOOKBACK,
        PRICE_POINTS,
        _prices_from_returns,
        _store,
        _varying_returns,
    )

    price_calendar = weekdays(date(2023, 1, 3), PRICE_POINTS)
    as_of = price_calendar[-1]
    decision_at = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC)
    symbols = ["000001.SZ", "000002.SH", "000003.SZ", "000004.SH"]
    group_a = _varying_returns(LOOKBACK, seed=1)
    group_b = _varying_returns(LOOKBACK, seed=9)
    store = _store(
        price_calendar,
        {
            symbols[0]: _prices_from_returns(group_a),
            symbols[1]: _prices_from_returns(group_a),
            symbols[2]: _prices_from_returns(group_b),
            symbols[3]: _prices_from_returns(group_b),
        },
    )
    market_snap = store.snapshot().snapshot_id
    prefix = price_calendar[0] - timedelta(days=1)
    while prefix.weekday() >= 5:
        prefix -= timedelta(days=1)
    phase_calendar = [prefix, *price_calendar]
    equity = 80_000.0
    risk = 0.3
    from app.research.layer_two_allocation_protocol import plan_base_slots

    slot = plan_base_slots(current_account_equity=equity, risk_budget=risk)
    phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=phase_calendar,
        start=phase_calendar[0],
        end=phase_calendar[-1],
        anchor=phase_calendar[0],
        current_account_equity=equity,
        risk_budget=risk,
        market_data_snapshot_id=market_snap,
    )
    bundle = _Bundle.__new__(_Bundle)
    bundle.calendar = phase_calendar
    bundle.as_of = as_of
    bundle.decision_at = decision_at
    bundle.store = store
    bundle.symbols = symbols
    bundle.market_snap = market_snap
    bundle.equity = equity
    bundle.risk_budget = risk
    bundle.slot = slot
    bundle.phase = phase
    constraint = _assemble(bundle)
    assert constraint.as_of_has_selected_phase_opportunity is False
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=_empty_state(constraint),
        ranking=_ranking(constraint.eligible_symbols),
    )
    assert report.proposed_entry is None
    assert report.portfolio_cash_retention_reason == "no_selected_phase_opportunity"
    assert report.selected_tranche_id is None


def test_n0_zero_risk_budget_retains_cash() -> None:
    bundle = _Bundle(risk_budget=0.0)
    constraint = _assemble(bundle)
    assert constraint.active_tranche_count == 0
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=_empty_state(constraint),
        ranking=_ranking(constraint.eligible_symbols),
    )
    assert report.proposed_entry is None
    assert report.portfolio_cash_retention_reason == "zero_risk_budget"


def test_upstream_not_ready_retains_cash() -> None:
    from datetime import UTC, date, datetime

    from app.research.layer_two_allocation_protocol import plan_base_slots
    from tests.helpers import weekdays
    from tests.test_layer_two_statistical_risk_clusters import (
        _prices_from_returns,
        _store,
        _varying_returns,
    )

    short_calendar = weekdays(date(2024, 1, 2), 40)
    short_as_of = short_calendar[-1]
    short_decision = datetime(short_as_of.year, short_as_of.month, short_as_of.day, 16, 0, tzinfo=UTC)
    symbols = ["000001.SZ", "000002.SH"]
    returns = _varying_returns(len(short_calendar) - 1)
    short_store = _store(
        short_calendar,
        {
            symbols[0]: _prices_from_returns(returns),
            symbols[1]: _prices_from_returns(returns),
        },
    )
    market_snap = short_store.snapshot().snapshot_id
    equity = 80_000.0
    risk = 0.3
    slot = plan_base_slots(current_account_equity=equity, risk_budget=risk)
    phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=short_calendar,
        start=short_calendar[0],
        end=short_calendar[-1],
        anchor=short_calendar[0],
        current_account_equity=equity,
        risk_budget=risk,
        market_data_snapshot_id=market_snap,
    )
    bundle = _Bundle.__new__(_Bundle)
    bundle.calendar = short_calendar
    bundle.as_of = short_as_of
    bundle.decision_at = short_decision
    bundle.store = short_store
    bundle.symbols = symbols
    bundle.market_snap = market_snap
    bundle.equity = equity
    bundle.risk_budget = risk
    bundle.slot = slot
    bundle.phase = phase
    constraint = _assemble(bundle)
    assert constraint.ready_for_stateful_allocator_input is False
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=_empty_state(constraint),
        ranking=_ranking(constraint.eligible_symbols),
    )
    assert report.proposed_entry is None
    assert report.portfolio_cash_retention_reason == "upstream_not_ready_for_stateful_allocator_input"


def test_hard_unknown_unusable_rows_rejected_in_order() -> None:
    bundle = _Bundle()
    eligibility = _eligibility(bundle)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    financials = _financials_for(
        bundle,
        eligible,
        fin_modes={
            eligible[0]: "hard",
            eligible[1]: "unknown",
        },
    )
    cluster = _cluster(bundle, eligible)
    constraint = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=bundle.phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=_empty_state(constraint),
        ranking=_ranking(eligible),
    )
    assert report.proposed_entry is not None
    assert report.proposed_entry.symbol == eligible[2]
    assert report.candidate_rejection_diagnostics[0].reason == "hard_excluded"
    assert report.candidate_rejection_diagnostics[1].reason == "financial_unknown"


def test_cluster_position_cap() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    # cluster_001 holds symbols[0] and symbols[1]; fill both on other tranches.
    s0, s1 = eligible[0], eligible[1]
    r0 = next(r for r in constraint.rows if r.symbol == s0)
    r1 = next(r for r in constraint.rows if r.symbol == s1)
    assert r0.cluster_id == r1.cluster_id
    state = _state_with_positions(
        constraint,
        [
            LayerTwoActiveTranchePosition(
                tranche_id=1,
                symbol=s0,
                current_market_notional=1_000.0,
                cluster_id=r0.cluster_id or "cluster_001",
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=2,
                symbol=s1,
                current_market_notional=1_000.0,
                cluster_id=r1.cluster_id or "cluster_001",
            ),
        ],
    )
    # Rank cluster_001 names first, then cluster_002.
    ranking = _ranking([s0, s1, eligible[2], eligible[3]])
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert report.proposed_entry is not None
    assert report.proposed_entry.symbol == eligible[2]
    reasons = [d.reason for d in report.candidate_rejection_diagnostics]
    assert reasons[0] == "already_held"
    assert reasons[1] == "already_held"


def test_cluster_position_cap_blocks_third_name() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    s0, s1, s2, s3 = eligible
    r0 = next(r for r in constraint.rows if r.symbol == s0)
    r1 = next(r for r in constraint.rows if r.symbol == s1)
    # Hold two unrelated names that share cluster_001 via explicit state evidence
    # on symbols outside current eligible mapping is not needed; hold s0/s1 and
    # try to enter another symbol forced into same cluster by ranking only among
    # cluster_002 after skipping held — instead hold s2/s3 (cluster_002) and rank
    # a third eligible that would join cluster_002: only two eligibles in cluster.
    # Use held outside-eligible symbol with cluster_002 id evidence.
    outside = "999999.SH"
    state = _state_with_positions(
        constraint,
        [
            LayerTwoActiveTranchePosition(
                tranche_id=1,
                symbol=s2,
                current_market_notional=1_000.0,
                cluster_id=next(r.cluster_id for r in constraint.rows if r.symbol == s2) or "cluster_002",
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=2,
                symbol=outside,
                current_market_notional=1_000.0,
                cluster_id=next(r.cluster_id for r in constraint.rows if r.symbol == s3) or "cluster_002",
            ),
        ],
    )
    ranking = _ranking([s3, s0, s1, s2])
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert report.candidate_rejection_diagnostics[0].symbol == s3
    assert report.candidate_rejection_diagnostics[0].reason == "cluster_position_cap"
    assert report.proposed_entry is not None
    assert report.proposed_entry.symbol == s0
    _ = r0, r1


def test_cluster_notional_cap() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    s0, s2 = eligible[0], eligible[2]
    r0 = next(r for r in constraint.rows if r.symbol == s0)
    assert r0.cluster_sleeve_cap_notional == 8_400.0
    # Existing cluster notional 500 + target 8000 = 8500 > 8400.
    outside = "888888.SZ"
    state = _state_with_positions(
        constraint,
        [
            LayerTwoActiveTranchePosition(
                tranche_id=1,
                symbol=outside,
                current_market_notional=500.0,
                cluster_id=r0.cluster_id or "cluster_001",
            )
        ],
    )
    ranking = _ranking([s0, s2, eligible[1], eligible[3]])
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert report.candidate_rejection_diagnostics[0].reason == "cluster_notional_cap"
    assert report.proposed_entry is not None
    assert report.proposed_entry.symbol == s2


def test_preexisting_cluster_breach_blocks_all_new_entries_no_forced_exit() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    s0 = eligible[0]
    r0 = next(r for r in constraint.rows if r.symbol == s0)
    outside_a = "777777.SH"
    outside_b = "777778.SZ"
    state = _state_with_positions(
        constraint,
        [
            LayerTwoActiveTranchePosition(
                tranche_id=1,
                symbol=outside_a,
                current_market_notional=5_000.0,
                cluster_id=r0.cluster_id or "cluster_001",
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=2,
                symbol=outside_b,
                current_market_notional=5_000.0,
                cluster_id=r0.cluster_id or "cluster_001",
            ),
        ],
    )
    # 10k > 8400 global cluster cap — portfolio-wide gate; still no forced exits.
    ranking = _ranking([s0, eligible[2], eligible[1], eligible[3]])
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert report.proposed_entry is None
    assert report.portfolio_cash_retention_reason == "preexisting_cluster_breach"
    assert report.candidate_rejection_diagnostics == []
    assert len(state.positions) == 2


def test_preexisting_cluster_count_breach_includes_outside_eligible() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    assert constraint.active_tranche_count >= 3
    cluster_id = "cluster_breach_outside"
    state = _state_with_positions(
        constraint,
        [
            LayerTwoActiveTranchePosition(
                tranche_id=0,
                symbol="111111.SH",
                current_market_notional=100.0,
                cluster_id=cluster_id,
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=1,
                symbol="111112.SZ",
                current_market_notional=100.0,
                cluster_id=cluster_id,
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=2,
                symbol="111113.SH",
                current_market_notional=100.0,
                cluster_id=cluster_id,
            ),
        ],
    )
    # Selected tranche 0 is occupied AND cluster count>2; cluster breach is checked first.
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=_ranking(eligible),
    )
    assert report.proposed_entry is None
    assert report.portfolio_cash_retention_reason == "preexisting_cluster_breach"


def test_preexisting_sleeve_breach_blocks_all_entries() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    assert constraint.sleeve_budget == 24_000.0
    # Per-cluster under global 8400 cap, but aggregate gross > sleeve_budget.
    state = _state_with_positions(
        constraint,
        [
            LayerTwoActiveTranchePosition(
                tranche_id=0,
                symbol="555551.SH",
                current_market_notional=8_400.0,
                cluster_id="cluster_a",
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=1,
                symbol="555552.SZ",
                current_market_notional=8_400.0,
                cluster_id="cluster_b",
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=2,
                symbol="555553.SH",
                current_market_notional=8_400.0,
                cluster_id="cluster_c",
            ),
        ],
    )
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=_ranking(eligible),
    )
    assert report.proposed_entry is None
    assert report.portfolio_cash_retention_reason == "preexisting_sleeve_breach"
    assert report.candidate_rejection_diagnostics == []
    assert report.accounting.current_gross_notional == 25_200.0


def test_sleeve_notional_cap_rejects_and_continues_ranking() -> None:
    bundle = _Bundle()
    # First eligible gets full 8000 target; second gets size 0.5 → 4000 via float-cap band.
    eligibility = _eligibility(
        bundle,
        caps={
            bundle.symbols[0]: 12_000_000_000.0,
            bundle.symbols[1]: 4_000_000_000.0,
        },
    )
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    financials = _financials_for(bundle, eligible)
    cluster = _cluster(bundle, eligible)
    constraint = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=bundle.phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )
    by_symbol = {row.symbol: row for row in constraint.rows}
    assert by_symbol[eligible[0]].target_for_later_allocator == 8_000.0
    assert by_symbol[eligible[1]].target_for_later_allocator == 4_000.0
    # Two clusters at the global cluster cap: gross 16800; +8000 exceeds sleeve, +4000 fits.
    # Leave selected tranche 0 free.
    state = _state_with_positions(
        constraint,
        [
            LayerTwoActiveTranchePosition(
                tranche_id=1,
                symbol="444441.SH",
                current_market_notional=8_400.0,
                cluster_id="cluster_other_a",
            ),
            LayerTwoActiveTranchePosition(
                tranche_id=2,
                symbol="444442.SZ",
                current_market_notional=8_400.0,
                cluster_id="cluster_other_b",
            ),
        ],
    )
    ranking = _ranking(eligible)
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert report.candidate_rejection_diagnostics[0].reason == "sleeve_notional_cap"
    assert report.candidate_rejection_diagnostics[0].symbol == eligible[0]
    assert report.proposed_entry is not None
    assert report.proposed_entry.symbol == eligible[1]
    assert report.proposed_entry.target_notional == 4_000.0
    assert report.accounting.proposed_gross_notional == 20_800.0


def test_insufficient_cash_no_scale() -> None:
    # cash_at_full_sleeve = equity*(1-risk) must be < base_slot while selected tranche stays free.
    bundle = _Bundle(equity=60_000.0, risk_budget=0.9)
    constraint = _assemble(bundle)
    assert constraint.sleeve_budget == 54_000.0
    assert constraint.base_slot_notional == 9_000.0
    assert constraint.active_tranche_count == 6
    # Fill sleeve via non-selected tranches without breaching per-cluster 35% cap (18900).
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
    assert state.cash == 6_000.0
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=_ranking(constraint.eligible_symbols),
    )
    assert report.proposed_entry is None
    assert report.portfolio_cash_retention_reason == "no_admissible_candidate"
    assert all(d.reason == "insufficient_cash" for d in report.candidate_rejection_diagnostics)
    assert report.accounting.proposed_cash == 6_000.0


def test_unsealed_state_rejected() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    unsealed = LayerTwoStatefulPortfolioState(
        as_of=constraint.as_of,
        decision_at=constraint.decision_at,
        market_data_snapshot_id=constraint.market_data_snapshot_id,
        current_account_equity=constraint.current_account_equity,
        cash=constraint.current_account_equity,
        positions=[],
    )
    assert unsealed.state_id is None
    with pytest.raises(ValueError, match="state_id is missing"):
        allocate_layer_two_stateful_single_opportunity(
            constraint_report=constraint,
            current_state=unsealed,
            ranking=_ranking(eligible),
        )


def test_positions_require_strictly_increasing_tranche_id() -> None:
    _, _, _, _, constraint, _ = _happy_bundle_inputs()
    with pytest.raises(ValidationError, match="strictly increasing tranche_id"):
        LayerTwoStatefulPortfolioState(
            as_of=constraint.as_of,
            decision_at=constraint.decision_at,
            market_data_snapshot_id=constraint.market_data_snapshot_id,
            current_account_equity=constraint.current_account_equity,
            cash=constraint.current_account_equity - 2_000.0,
            positions=[
                LayerTwoActiveTranchePosition(
                    tranche_id=2,
                    symbol="B.SZ",
                    current_market_notional=1_000.0,
                    cluster_id="c1",
                ),
                LayerTwoActiveTranchePosition(
                    tranche_id=1,
                    symbol="A.SZ",
                    current_market_notional=1_000.0,
                    cluster_id="c1",
                ),
            ],
        )


def test_reordered_positions_cannot_create_ambiguous_state_id() -> None:
    _, _, _, _, constraint, _ = _happy_bundle_inputs()
    ordered = seal_layer_two_stateful_portfolio_state(
        LayerTwoStatefulPortfolioState(
            as_of=constraint.as_of,
            decision_at=constraint.decision_at,
            market_data_snapshot_id=constraint.market_data_snapshot_id,
            current_account_equity=constraint.current_account_equity,
            cash=constraint.current_account_equity - 2_000.0,
            positions=[
                LayerTwoActiveTranchePosition(
                    tranche_id=1,
                    symbol="A.SZ",
                    current_market_notional=1_000.0,
                    cluster_id="c1",
                ),
                LayerTwoActiveTranchePosition(
                    tranche_id=2,
                    symbol="B.SZ",
                    current_market_notional=1_000.0,
                    cluster_id="c1",
                ),
            ],
        )
    )
    again = seal_layer_two_stateful_portfolio_state(
        LayerTwoStatefulPortfolioState(
            as_of=constraint.as_of,
            decision_at=constraint.decision_at,
            market_data_snapshot_id=constraint.market_data_snapshot_id,
            current_account_equity=constraint.current_account_equity,
            cash=constraint.current_account_equity - 2_000.0,
            positions=[
                LayerTwoActiveTranchePosition(
                    tranche_id=1,
                    symbol="A.SZ",
                    current_market_notional=1_000.0,
                    cluster_id="c1",
                ),
                LayerTwoActiveTranchePosition(
                    tranche_id=2,
                    symbol="B.SZ",
                    current_market_notional=1_000.0,
                    cluster_id="c1",
                ),
            ],
        )
    )
    assert ordered.state_id == again.state_id
    # Equivalent multiset with swapped order is rejected — no alternate state_id.
    with pytest.raises(ValidationError, match="strictly increasing tranche_id"):
        LayerTwoStatefulPortfolioState(
            as_of=constraint.as_of,
            decision_at=constraint.decision_at,
            market_data_snapshot_id=constraint.market_data_snapshot_id,
            current_account_equity=constraint.current_account_equity,
            cash=constraint.current_account_equity - 2_000.0,
            positions=[
                LayerTwoActiveTranchePosition(
                    tranche_id=2,
                    symbol="B.SZ",
                    current_market_notional=1_000.0,
                    cluster_id="c1",
                ),
                LayerTwoActiveTranchePosition(
                    tranche_id=1,
                    symbol="A.SZ",
                    current_market_notional=1_000.0,
                    cluster_id="c1",
                ),
            ],
        )


def test_exact_permutation_violations() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    state = _empty_state(constraint)
    with pytest.raises(ValueError, match="exact duplicate-free permutation"):
        allocate_layer_two_stateful_single_opportunity(
            constraint_report=constraint,
            current_state=state,
            ranking=_ranking(eligible[:-1]),
        )
    with pytest.raises(ValueError, match="exact duplicate-free permutation"):
        allocate_layer_two_stateful_single_opportunity(
            constraint_report=constraint,
            current_state=state,
            ranking=_ranking([*eligible, "EXTRA.SH"]),
        )
    with pytest.raises(ValidationError):
        UnvalidatedDevelopmentRankingInput(ranked_symbols=[eligible[0], eligible[0], *eligible[1:]])


def test_bool_nan_inf_rejected() -> None:
    _, _, _, _, constraint, _ = _happy_bundle_inputs()
    with pytest.raises(ValidationError):
        LayerTwoStatefulPortfolioState(
            as_of=constraint.as_of,
            decision_at=constraint.decision_at,
            market_data_snapshot_id=constraint.market_data_snapshot_id,
            current_account_equity=True,  # type: ignore[arg-type]
            cash=80_000.0,
            positions=[],
        )
    with pytest.raises(ValidationError):
        LayerTwoStatefulPortfolioState(
            as_of=constraint.as_of,
            decision_at=constraint.decision_at,
            market_data_snapshot_id=constraint.market_data_snapshot_id,
            current_account_equity=80_000.0,
            cash=float("nan"),
            positions=[],
        )
    with pytest.raises(ValidationError):
        LayerTwoActiveTranchePosition(
            tranche_id=0,
            symbol="000001.SZ",
            current_market_notional=float("inf"),
            cluster_id="cluster_001",
        )
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=_empty_state(constraint),
        ranking=_ranking(constraint.eligible_symbols),
    )
    payload = report.model_dump(mode="json")
    payload["accounting"]["current_cash"] = True
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoStatefulAllocatorReport.model_validate(payload)
    payload = report.model_dump(mode="json")
    payload["ready_for_allocation_diagnostic"] = 1
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoStatefulAllocatorReport.model_validate(payload)
    payload = report.model_dump(mode="json")
    payload["ready_for_orders"] = 0
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoStatefulAllocatorReport.model_validate(payload)
    assert report.ready_for_allocation_diagnostic is True
    assert math.isfinite(STATE_EQUITY_ABS_TOL)

    state_payload = _empty_state(constraint).model_dump(mode="json")
    state_payload["diagnostic_only"] = 1
    state_payload.pop("state_id", None)
    with pytest.raises(ValidationError):
        LayerTwoStatefulPortfolioState.model_validate(state_payload)
    state_payload = _empty_state(constraint).model_dump(mode="json")
    state_payload["account_identity_not_required"] = 1
    state_payload.pop("state_id", None)
    with pytest.raises(ValidationError):
        LayerTwoStatefulPortfolioState.model_validate(state_payload)
    ranking_payload = _ranking(constraint.eligible_symbols).model_dump(mode="json")
    ranking_payload["does_not_derive_scores_or_weights"] = 1
    with pytest.raises(ValidationError):
        UnvalidatedDevelopmentRankingInput.model_validate(ranking_payload)


def test_selected_phase_tranche_id_out_of_range_rejected() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    assert constraint.selected_phase_opportunity is not None
    payload = constraint.model_dump(mode="json")
    payload["selected_phase_opportunity"]["tranche_id"] = constraint.active_tranche_count
    payload.pop("report_id", None)
    drifted = seal_layer_two_constraint_assembler_report(LayerTwoConstraintAssemblerReport.model_validate(payload))
    with pytest.raises(ValueError, match="selected_phase_opportunity.tranche_id"):
        allocate_layer_two_stateful_single_opportunity(
            constraint_report=drifted,
            current_state=_empty_state(constraint),
            ranking=_ranking(eligible),
        )


def test_duplicate_constraint_row_symbols_fail_closed() -> None:
    from app.research.layer_two_stateful_allocator import _row_by_symbol

    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    payload = constraint.model_dump(mode="json")
    assert payload["rows"]
    payload["rows"] = [payload["rows"][0], payload["rows"][0], *payload["rows"][1:]]
    # Keep symbol order non-decreasing so assembler model accepts the reseal.
    payload["rows"].sort(key=lambda row: row["symbol"])
    payload.pop("report_id", None)
    drifted = seal_layer_two_constraint_assembler_report(LayerTwoConstraintAssemblerReport.model_validate(payload))
    with pytest.raises(ValueError, match="duplicate constraint row symbol"):
        _row_by_symbol(drifted)
    with pytest.raises(ValueError, match="duplicate constraint row symbol"):
        allocate_layer_two_stateful_single_opportunity(
            constraint_report=drifted,
            current_state=_empty_state(constraint),
            ranking=_ranking(eligible),
        )


def test_as_of_decision_snapshot_equity_drift() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    base = _empty_state(constraint)
    drifted_as_of = seal_layer_two_stateful_portfolio_state(
        base.model_copy(update={"state_id": None, "as_of": constraint.as_of - timedelta(days=1)})
    )
    with pytest.raises(ValueError, match="as_of"):
        allocate_layer_two_stateful_single_opportunity(
            constraint_report=constraint,
            current_state=drifted_as_of,
            ranking=_ranking(eligible),
        )
    drifted_decision = seal_layer_two_stateful_portfolio_state(
        base.model_copy(update={"state_id": None, "decision_at": constraint.decision_at + timedelta(minutes=1)})
    )
    with pytest.raises(ValueError, match="decision_at"):
        allocate_layer_two_stateful_single_opportunity(
            constraint_report=constraint,
            current_state=drifted_decision,
            ranking=_ranking(eligible),
        )
    drifted_snap = seal_layer_two_stateful_portfolio_state(
        base.model_copy(update={"state_id": None, "market_data_snapshot_id": "00" * 32})
    )
    with pytest.raises(ValueError, match="snapshot"):
        allocate_layer_two_stateful_single_opportunity(
            constraint_report=constraint,
            current_state=drifted_snap,
            ranking=_ranking(eligible),
        )
    drifted_equity = seal_layer_two_stateful_portfolio_state(
        LayerTwoStatefulPortfolioState(
            as_of=constraint.as_of,
            decision_at=constraint.decision_at,
            market_data_snapshot_id=constraint.market_data_snapshot_id,
            current_account_equity=constraint.current_account_equity + 1.0,
            cash=constraint.current_account_equity + 1.0,
            positions=[],
        )
    )
    with pytest.raises(ValueError, match="equity"):
        allocate_layer_two_stateful_single_opportunity(
            constraint_report=constraint,
            current_state=drifted_equity,
            ranking=_ranking(eligible),
        )


def test_duplicate_and_out_of_range_tranche() -> None:
    _, _, _, _, constraint, _ = _happy_bundle_inputs()
    with pytest.raises(ValidationError):
        LayerTwoStatefulPortfolioState(
            as_of=constraint.as_of,
            decision_at=constraint.decision_at,
            market_data_snapshot_id=constraint.market_data_snapshot_id,
            current_account_equity=constraint.current_account_equity,
            cash=constraint.current_account_equity - 2_000.0,
            positions=[
                LayerTwoActiveTranchePosition(
                    tranche_id=1,
                    symbol="A.SZ",
                    current_market_notional=1_000.0,
                    cluster_id="c1",
                ),
                LayerTwoActiveTranchePosition(
                    tranche_id=1,
                    symbol="B.SZ",
                    current_market_notional=1_000.0,
                    cluster_id="c1",
                ),
            ],
        )
    bad = seal_layer_two_stateful_portfolio_state(
        LayerTwoStatefulPortfolioState(
            as_of=constraint.as_of,
            decision_at=constraint.decision_at,
            market_data_snapshot_id=constraint.market_data_snapshot_id,
            current_account_equity=constraint.current_account_equity,
            cash=constraint.current_account_equity - 1_000.0,
            positions=[
                LayerTwoActiveTranchePosition(
                    tranche_id=constraint.active_tranche_count,
                    symbol="A.SZ",
                    current_market_notional=1_000.0,
                    cluster_id="c1",
                )
            ],
        )
    )
    with pytest.raises(ValueError, match="tranche_id"):
        allocate_layer_two_stateful_single_opportunity(
            constraint_report=constraint,
            current_state=bad,
            ranking=_ranking(constraint.eligible_symbols),
        )


def test_state_accounting_mismatch() -> None:
    _, _, _, _, constraint, _ = _happy_bundle_inputs()
    with pytest.raises(ValidationError, match="current_account_equity"):
        LayerTwoStatefulPortfolioState(
            as_of=constraint.as_of,
            decision_at=constraint.decision_at,
            market_data_snapshot_id=constraint.market_data_snapshot_id,
            current_account_equity=constraint.current_account_equity,
            cash=constraint.current_account_equity - 100.0,
            positions=[],
        )


def test_cluster_id_inconsistency_fail_closed() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    held = eligible[0]
    row = next(r for r in constraint.rows if r.symbol == held)
    assert row.cluster_id is not None
    state = _state_with_positions(
        constraint,
        [
            LayerTwoActiveTranchePosition(
                tranche_id=1,
                symbol=held,
                current_market_notional=1_000.0,
                cluster_id="wrong_cluster",
            )
        ],
    )
    with pytest.raises(ValueError, match="cluster state inconsistency"):
        allocate_layer_two_stateful_single_opportunity(
            constraint_report=constraint,
            current_state=state,
            ranking=_ranking(eligible),
        )


def test_outer_reseal_tampering_rejected() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    state = _empty_state(constraint)
    ranking = _ranking(eligible)
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    tampered = report.model_copy(update={"market_data_snapshot_id": "11" * 32})
    with pytest.raises(ValueError, match="report_id"):
        verify_layer_two_stateful_allocator_report(
            tampered,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
        )
    payload = report.model_dump(mode="json")
    assert payload["proposed_entry"] is not None
    payload["proposed_entry"]["target_notional"] = 1.0
    payload.pop("report_id", None)
    drifted = seal_layer_two_stateful_allocator_report(LayerTwoStatefulAllocatorReport.model_validate(payload))
    assert drifted.report_id == compute_report_id(drifted)
    with pytest.raises(ValueError, match="does not match full recompute"):
        verify_layer_two_stateful_allocator_report(
            drifted,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
        )


def test_file_verifier_phase_path_missing_wrong_tampered(tmp_path: Path) -> None:
    bundle, eligibility, financials, cluster, constraint, eligible = _happy_bundle_inputs()
    state = _empty_state(constraint)
    ranking = _ranking(eligible)
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    missing = tmp_path / "missing-phase.json"
    with pytest.raises(ValueError, match="phase report file missing"):
        verify_layer_two_stateful_allocator_report_file(
            report=report,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            phase_report=bundle.phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
            phase_report_path=missing,
        )

    other_phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=bundle.calendar,
        start=bundle.calendar[0],
        end=bundle.calendar[-1],
        anchor=bundle.calendar[0],
        current_account_equity=bundle.equity,
        risk_budget=0.6,
        market_data_snapshot_id=bundle.market_snap,
    )
    other_path = tmp_path / "other-phase.json"
    write_layer_two_tranche_phase_schedule_report(other_path, other_phase)
    with pytest.raises(ValueError, match="phase report file id does not match"):
        verify_layer_two_stateful_allocator_report_file(
            report=report,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            phase_report=bundle.phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
            phase_report_path=other_path,
        )

    phase_path = tmp_path / "phase.json"
    write_layer_two_tranche_phase_schedule_report(phase_path, bundle.phase)
    tampered_payload = json.loads(phase_path.read_text(encoding="utf-8"))
    tampered_payload["current_account_equity"] = float(bundle.equity) + 1.0
    phase_path.write_text(json.dumps(tampered_payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        verify_layer_two_stateful_allocator_report_file(
            report=report,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            phase_report=bundle.phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
            phase_report_path=phase_path,
        )

    write_layer_two_tranche_phase_schedule_report(phase_path, bundle.phase)
    result = verify_layer_two_stateful_allocator_report_file(
        report=report,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=bundle.phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
        phase_report_path=phase_path,
    )
    assert result.structural_ok is True
    assert result.constraint_assembler_binding_ok is True
    assert result.phase_binding_ok is True
    assert result.allocation_protocol_binding_ok is True


def test_no_order_trade_fields_on_models() -> None:
    _, _, _, _, constraint, eligible = _happy_bundle_inputs()
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=_empty_state(constraint),
        ranking=_ranking(eligible),
    )
    dumped = report.model_dump()
    forbidden = {
        "quantity",
        "price",
        "order_side",
        "side",
        "fill",
        "pnl",
        "shares",
        "broker",
    }
    assert forbidden.isdisjoint(dumped.keys())
    entry = dumped["proposed_entry"]
    assert entry is not None
    assert forbidden.isdisjoint(entry.keys())


def test_no_import_wiring_to_production_engines() -> None:
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
        "app.backtest",
        "app.api",
        "app.cli",
        "app.persistence",
        "app.models.config",
        "app.strategies",
    )
    for module in imported:
        assert not any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_prefixes)
    assert "ScoringEngine" not in source
    assert "BacktestEngine" not in source
    assert "StrategyConfig" not in source


def test_constraint_reseal_tamper_still_fails_allocator_verify() -> None:
    bundle, eligibility, financials, cluster, constraint, eligible = _happy_bundle_inputs()
    state = _empty_state(constraint)
    ranking = _ranking(eligible)
    report = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    payload = constraint.model_dump(mode="json")
    payload["eligible_symbols"] = list(reversed(payload["eligible_symbols"]))
    payload.pop("report_id", None)
    # May fail validation if sort invariant breaks; reseal a drifted usable field instead.
    payload = constraint.model_dump(mode="json")
    payload["market_data_snapshot_id"] = "ab" * 32
    payload.pop("report_id", None)
    drifted = seal_layer_two_constraint_assembler_report(LayerTwoConstraintAssemblerReport.model_validate(payload))
    assert drifted.report_id == compute_constraint_report_id(drifted)
    with pytest.raises(ValueError):
        allocate_layer_two_stateful_single_opportunity(
            constraint_report=drifted,
            current_state=state,
            ranking=ranking,
        )
    _ = bundle, eligibility, financials, cluster, report
