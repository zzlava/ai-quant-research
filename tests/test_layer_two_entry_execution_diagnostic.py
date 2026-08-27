"""Attack-oriented tests for layer-two T+1 entry execution diagnostic (E10e-0)."""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from app.research.layer_two_constraint_assembler import (
    LayerTwoConstraintAssemblerReport,
    assemble_layer_two_constraints,
)
from app.research.layer_two_entry_execution_diagnostic import (
    BOUND_BOARD_LOT_SIZE,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
    EntryCostScenarioRow,
    LayerTwoEntryExecutionDiagnosticReport,
    LayerTwoEntryExecutionObservation,
    LayerTwoEntryExecutionVerificationResult,
    diagnose_layer_two_entry_execution,
    seal_layer_two_entry_execution_diagnostic_report,
    verify_layer_two_entry_execution_diagnostic_report,
    verify_layer_two_entry_execution_diagnostic_report_file,
)
from app.research.layer_two_stateful_allocator import (
    LayerTwoStatefulPortfolioState,
    UnvalidatedDevelopmentRankingInput,
    allocate_layer_two_stateful_single_opportunity,
    seal_layer_two_stateful_portfolio_state,
)
from app.research.layer_two_tranche_phase_schedule import (
    plan_layer_two_tranche_phase_schedule,
    write_layer_two_tranche_phase_schedule_report,
)
from app.storage.memory import InMemoryStore
from tests.helpers import PROJECT_ROOT
from tests.test_layer_two_constraint_assembler import (
    _Bundle,
    _cluster,
    _eligibility,
    _financials_for,
)

REPO_ROOT = PROJECT_ROOT
MODULE_PATH = REPO_ROOT / "src/app/research/layer_two_entry_execution_diagnostic.py"

# Sealed fixture observation prices that must live inside the hashed MarketStore.
FIXTURE_T1_OPEN = 10.0
FIXTURE_T1_UP_LIMIT = 11.0
FIXTURE_T1_DOWN_LIMIT = 9.0


def _next_weekday(day):
    cursor = day + timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor += timedelta(days=1)
    return cursor


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


def _extend_store_with_daily_bars(
    store: InMemoryStore,
    *,
    extra_days: list,
    open_: float = FIXTURE_T1_OPEN,
    up_limit: float = FIXTURE_T1_UP_LIMIT,
    down_limit: float = FIXTURE_T1_DOWN_LIMIT,
    is_suspended: bool = False,
    symbols: list[str] | None = None,
    overwrite: bool = False,
) -> InMemoryStore:
    """Rebuild a content-hashed store that includes exact observation bars (no wrapper injection)."""
    from app.providers._frames import DAILY_SCHEMA

    symbols = symbols or sorted({str(s) for s in store.daily["symbol"].unique().to_list()})
    rows: list[dict[str, object]] = []
    for row in store.daily.to_dicts():
        item = dict(row)
        item.setdefault("up_limit", None)
        item.setdefault("down_limit", None)
        item.setdefault("pre_close", item.get("close"))
        item.setdefault("adj_factor", 1.0)
        rows.append(item)
    by_key = {(str(r["symbol"]), r["date"]): r for r in rows}
    for day in extra_days:
        for symbol in symbols:
            key = (symbol, day)
            if key in by_key and not overwrite:
                continue
            payload = {
                "symbol": symbol,
                "date": day,
                "open": float(open_),
                "high": float(open_) + 0.05,
                "low": float(open_) - 0.05,
                "close": float(open_),
                "volume": 12_000_000.0,
                "amount": 200_000_000.0,
                "turnover_rate": 0.03,
                "is_st": False,
                "is_suspended": bool(is_suspended),
                "price_limit_pct": 0.10,
                "adj_open": float(open_),
                "adj_high": float(open_) + 0.05,
                "adj_low": float(open_) - 0.05,
                "adj_close": float(open_),
                "adj_factor": 1.0,
                "pre_close": float(open_),
                "up_limit": None if is_suspended else float(up_limit),
                "down_limit": None if is_suspended else float(down_limit),
            }
            if key in by_key:
                by_key[key].update(payload)
            else:
                by_key[key] = payload
                rows.append(payload)
    calendar = list(dict.fromkeys([*list(store._calendar), *extra_days]))
    daily = pl.DataFrame(list(by_key.values()), schema=DAILY_SCHEMA)
    symbols_sorted = sorted({str(r["symbol"]) for r in by_key.values()})
    from app.models.market import Instrument
    from app.providers._frames import empty_global, instruments_to_frame
    from app.universe.membership import build_manual_static_membership

    instruments = [
        Instrument(symbol=s, name=s, sector="tech", listing_date=__import__("datetime").date(2018, 1, 1))
        for s in symbols_sorted
    ]
    membership = build_manual_static_membership(
        symbols_sorted, calendar, universe_id=getattr(store, "_universe_id", "demo")
    )
    return InMemoryStore(
        instruments=instruments_to_frame(instruments),
        daily=daily,
        index=daily.clear(),
        global_bars=empty_global(),
        calendar=calendar,
        universe_membership=membership,
        universe_id=getattr(store, "_universe_id", "demo"),
    )


def _t1_bundle_inputs(*, inject_tradable_t1_bars: bool = True):
    """Happy allocator inputs with phase calendar extended one day past as_of.

    When inject_tradable_t1_bars is true, T+1 daily bars (open/up_limit) are written into the
    same InMemoryStore before snapshot/phase/constraint so file observation binding can pass.
    """
    bundle = _Bundle(anchor_index=0)
    t1 = _next_weekday(bundle.as_of)
    extended = [*bundle.calendar, t1]
    if inject_tradable_t1_bars:
        bundle.store = _extend_store_with_daily_bars(bundle.store, extra_days=[t1])
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
    bundle.calendar = extended
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
    assert constraint.as_of_has_selected_phase_opportunity is True
    state = _empty_state(constraint)
    ranking = _ranking(eligible)
    allocator = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert allocator.proposed_entry is not None
    return bundle, eligibility, financials, cluster, constraint, state, ranking, allocator, t1


def _obs(
    *,
    symbol: str,
    execution_date,
    snapshot: str,
    status: str,
    raw_open: float | None = None,
    up_limit: float | None = None,
) -> LayerTwoEntryExecutionObservation:
    return LayerTwoEntryExecutionObservation.model_validate(
        {
            "symbol": symbol,
            "execution_date": execution_date,
            "market_data_snapshot_id": snapshot,
            "observation_status": status,
            "raw_open": raw_open,
            "published_up_limit": up_limit,
        }
    )


def test_hypothetically_fillable_happy_path() -> None:
    bundle, eligibility, financials, cluster, constraint, state, ranking, allocator, t1 = _t1_bundle_inputs()
    entry = allocator.proposed_entry
    assert entry is not None
    observation = _obs(
        symbol=entry.symbol,
        execution_date=t1,
        snapshot=allocator.market_data_snapshot_id,
        status="tradable",
        raw_open=10.0,
        up_limit=11.0,
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
    assert report.diagnostic_only is True
    assert report.post_decision_execution_label_only is True
    assert report.must_not_feed_ranking_or_scoring is True
    assert report.ready_for_orders is False
    assert report.stamp_tax_irrelevant_for_buy_entry is True
    assert report.expected_t1_execution_date == t1
    assert report.base_scenario is not None
    assert report.stress_scenario is not None
    assert report.base_scenario.can_afford_one_lot is True
    assert report.base_scenario.affordable_shares % BOUND_BOARD_LOT_SIZE == 0
    assert report.base_scenario.total_cash_used <= entry.target_notional + 1e-9
    assert report.base_scenario.legal_limit_cap_applied is False
    assert report.stress_scenario.legal_limit_cap_applied is False
    assert report.tranche_evaluation_protocol_id == BOUND_TRANCHE_EVALUATION_PROTOCOL_ID
    structural = verify_layer_two_entry_execution_diagnostic_report(
        report,
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=observation,
    )
    assert structural.structural_ok is True
    assert structural.phase_binding_ok is False
    assert structural.tranche_evaluation_protocol_binding_ok is False
    assert structural.execution_observation_binding_ok is False
    assert structural.allocator_binding_ok is False
    _ = eligibility, financials, cluster


def test_not_attempted_without_observation() -> None:
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
    ranking = _ranking(eligible)
    allocator = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert allocator.proposed_entry is None
    report = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=phase,
        execution_observation=None,
    )
    assert report.outcome == "not_attempted"
    assert report.portfolio_cash_retention_reason == allocator.portfolio_cash_retention_reason
    assert report.observation is None
    assert report.base_scenario is None
    assert report.proposed_symbol is None


def test_unknown_suspension_limit_up_unaffordable_branches() -> None:
    bundle, _, _, _, constraint, state, ranking, allocator, t1 = _t1_bundle_inputs()
    entry = allocator.proposed_entry
    assert entry is not None
    snap = allocator.market_data_snapshot_id

    unknown = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=_obs(symbol=entry.symbol, execution_date=t1, snapshot=snap, status="unknown"),
    )
    assert unknown.outcome == "unknown_execution_observation"
    assert unknown.base_scenario is None

    suspended = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=_obs(
            symbol=entry.symbol,
            execution_date=t1,
            snapshot=snap,
            status="known_full_day_suspension",
        ),
    )
    assert suspended.outcome == "blocked_suspension"
    assert suspended.base_scenario is None

    limit_up = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=_obs(
            symbol=entry.symbol,
            execution_date=t1,
            snapshot=snap,
            status="tradable",
            raw_open=11.0,
            up_limit=11.0,
        ),
    )
    assert limit_up.outcome == "blocked_limit_up"
    assert limit_up.base_scenario is None

    unaffordable = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=_obs(
            symbol=entry.symbol,
            execution_date=t1,
            snapshot=snap,
            status="tradable",
            raw_open=200.0,
            up_limit=220.0,
        ),
    )
    assert unaffordable.outcome == "unaffordable_board_lot_or_minimum_commission"
    assert unaffordable.base_scenario is not None
    assert unaffordable.base_scenario.affordable_shares == 0
    assert unaffordable.base_scenario.can_afford_one_lot is False
    assert unaffordable.base_scenario.hypothetical_fill_price > 0


def test_observation_validation_and_bindings() -> None:
    bundle, _, _, _, constraint, state, ranking, allocator, t1 = _t1_bundle_inputs()
    entry = allocator.proposed_entry
    assert entry is not None
    with pytest.raises(ValidationError):
        _obs(
            symbol=entry.symbol,
            execution_date=t1,
            snapshot=allocator.market_data_snapshot_id,
            status="tradable",
            raw_open=None,
            up_limit=11.0,
        )
    with pytest.raises(ValidationError):
        _obs(
            symbol=entry.symbol,
            execution_date=t1,
            snapshot=allocator.market_data_snapshot_id,
            status="known_full_day_suspension",
            raw_open=10.0,
            up_limit=None,
        )
    with pytest.raises(ValidationError):
        _obs(
            symbol=entry.symbol,
            execution_date=t1,
            snapshot=allocator.market_data_snapshot_id,
            status="unknown",
            raw_open=0.0,
            up_limit=None,
        )
    with pytest.raises(ValueError, match="exact T\\+1|execution_date"):
        diagnose_layer_two_entry_execution(
            allocator_report=allocator,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            phase_report=bundle.phase,
            execution_observation=_obs(
                symbol=entry.symbol,
                execution_date=t1 + timedelta(days=1),
                snapshot=allocator.market_data_snapshot_id,
                status="unknown",
            ),
        )
    with pytest.raises(ValueError, match="symbol"):
        diagnose_layer_two_entry_execution(
            allocator_report=allocator,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            phase_report=bundle.phase,
            execution_observation=_obs(
                symbol="ZZZZZZ.SH",
                execution_date=t1,
                snapshot=allocator.market_data_snapshot_id,
                status="unknown",
            ),
        )


def test_no_next_trading_day_and_duplicate_calendar() -> None:
    bundle, _, _, _, constraint, state, ranking, allocator, _t1 = _t1_bundle_inputs()
    # Truncate phase calendar so as_of is last day.
    short_phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=bundle.calendar[:-1],
        start=bundle.calendar[0],
        end=bundle.calendar[-2],
        anchor=bundle.calendar[0],
        current_account_equity=bundle.equity,
        risk_budget=bundle.risk_budget,
        market_data_snapshot_id=bundle.market_snap,
    )
    # Rebuild allocator bindings against short phase would change ids; instead call T+1 helper path
    # via diagnose with mismatched phase that still shares snapshot but ends at as_of.
    # Force by using original allocator against truncated calendar phase with matching ids is hard;
    # unit-check via recompute failure when phase lacks next day after resealing allocator is heavy.
    # Direct calendar helper coverage through diagnose with rebuilt chain:
    eligibility = _eligibility(bundle)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    financials = _financials_for(bundle, eligible)
    cluster = _cluster(bundle, eligible)
    short_constraint = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=short_phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )
    short_state = _empty_state(short_constraint)
    short_ranking = _ranking(eligible)
    short_allocator = allocate_layer_two_stateful_single_opportunity(
        constraint_report=short_constraint,
        current_state=short_state,
        ranking=short_ranking,
    )
    with pytest.raises(ValueError, match="no next market trading day"):
        diagnose_layer_two_entry_execution(
            allocator_report=short_allocator,
            constraint_report=short_constraint,
            current_state=short_state,
            ranking=short_ranking,
            phase_report=short_phase,
            execution_observation=None
            if short_allocator.proposed_entry is None
            else _obs(
                symbol=short_allocator.proposed_entry.symbol,
                execution_date=short_constraint.as_of,
                snapshot=short_allocator.market_data_snapshot_id,
                status="unknown",
            ),
        )

    dup_cal = [*bundle.calendar, bundle.calendar[-1]]
    with pytest.raises(ValueError, match="duplicate|strictly increasing"):
        plan_layer_two_tranche_phase_schedule(
            market_calendar=dup_cal,
            start=dup_cal[0],
            end=dup_cal[-1],
            anchor=dup_cal[0],
            current_account_equity=bundle.equity,
            risk_budget=bundle.risk_budget,
            market_data_snapshot_id=bundle.market_snap,
        )


def test_bool_nan_inf_and_literal_flags() -> None:
    _, _, _, _, _, _, _, allocator, t1 = _t1_bundle_inputs()
    entry = allocator.proposed_entry
    assert entry is not None
    with pytest.raises(ValidationError):
        LayerTwoEntryExecutionObservation.model_validate(
            {
                "symbol": entry.symbol,
                "execution_date": t1,
                "market_data_snapshot_id": allocator.market_data_snapshot_id,
                "observation_status": "tradable",
                "raw_open": True,
                "published_up_limit": 11.0,
            }
        )
    with pytest.raises(ValidationError):
        LayerTwoEntryExecutionObservation.model_validate(
            {
                "symbol": entry.symbol,
                "execution_date": t1,
                "market_data_snapshot_id": allocator.market_data_snapshot_id,
                "observation_status": "tradable",
                "raw_open": float("nan"),
                "published_up_limit": 11.0,
            }
        )
    with pytest.raises(ValidationError):
        EntryCostScenarioRow.model_validate(
            {
                "scenario_label": "base_5bps",
                "slippage_bps": 5,
                "hypothetical_fill_price": 10.0,
                "affordable_shares": 100,
                "affordable_lots": 1,
                "stock_notional": 1000.0,
                "commission": 5.0,
                "total_cash_used": 1005.0,
                "unused_target_cash": 0.0,
                "can_afford_one_lot": 1,
                "legal_limit_cap_applied": False,
            }
        )


def test_near_limit_slippage_cap_base_and_stress() -> None:
    bundle, _, _, _, constraint, state, ranking, allocator, t1 = _t1_bundle_inputs()
    entry = allocator.proposed_entry
    assert entry is not None
    # raw_open just below limit so 5bps and especially 15bps would exceed without cap.
    raw_open = 10.0
    up_limit = 10.01  # 5bps slipped = 10.005 < 10.01; 15bps slipped = 10.015 > 10.01
    observation = _obs(
        symbol=entry.symbol,
        execution_date=t1,
        snapshot=allocator.market_data_snapshot_id,
        status="tradable",
        raw_open=raw_open,
        up_limit=up_limit,
    )
    report = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=observation,
    )
    assert report.outcome in (
        "hypothetically_fillable",
        "unaffordable_board_lot_or_minimum_commission",
    )
    assert report.base_scenario is not None
    assert report.stress_scenario is not None
    assert report.base_scenario.hypothetical_fill_price <= up_limit + 1e-12
    assert report.stress_scenario.hypothetical_fill_price <= up_limit + 1e-12
    assert report.base_scenario.legal_limit_cap_applied is False
    assert report.stress_scenario.legal_limit_cap_applied is True
    assert report.stress_scenario.hypothetical_fill_price == up_limit

    # Force both base and stress to need the cap.
    tighter = _obs(
        symbol=entry.symbol,
        execution_date=t1,
        snapshot=allocator.market_data_snapshot_id,
        status="tradable",
        raw_open=10.0,
        up_limit=10.001,
    )
    both = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=tighter,
    )
    assert both.base_scenario is not None and both.stress_scenario is not None
    assert both.base_scenario.legal_limit_cap_applied is True
    assert both.stress_scenario.legal_limit_cap_applied is True
    assert both.base_scenario.hypothetical_fill_price == 10.001
    assert both.stress_scenario.hypothetical_fill_price == 10.001


def test_scenario_arithmetic_and_verification_bool_attacks() -> None:
    from app.research.layer_two_entry_execution_diagnostic import (
        LayerTwoEntryExecutionVerificationResult,
    )

    with pytest.raises(ValidationError):
        EntryCostScenarioRow.model_validate(
            {
                "scenario_label": "base_5bps",
                "slippage_bps": 15,
                "hypothetical_fill_price": 10.0,
                "affordable_shares": 100,
                "affordable_lots": 1,
                "stock_notional": 1000.0,
                "commission": 5.0,
                "total_cash_used": 1005.0,
                "unused_target_cash": 0.0,
                "can_afford_one_lot": True,
                "legal_limit_cap_applied": False,
            }
        )
    with pytest.raises(ValidationError):
        EntryCostScenarioRow.model_validate(
            {
                "scenario_label": "base_5bps",
                "slippage_bps": 5,
                "hypothetical_fill_price": 10.0,
                "affordable_shares": 100,
                "affordable_lots": 1,
                "stock_notional": 999.0,
                "commission": 5.0,
                "total_cash_used": 1004.0,
                "unused_target_cash": 0.0,
                "can_afford_one_lot": True,
                "legal_limit_cap_applied": False,
            }
        )
    with pytest.raises(ValidationError):
        EntryCostScenarioRow.model_validate(
            {
                "scenario_label": "base_5bps",
                "slippage_bps": 5,
                "hypothetical_fill_price": 10.0,
                "affordable_shares": 0,
                "affordable_lots": 0,
                "stock_notional": 0.0,
                "commission": 5.0,
                "total_cash_used": 5.0,
                "unused_target_cash": 0.0,
                "can_afford_one_lot": False,
                "legal_limit_cap_applied": False,
            }
        )
    with pytest.raises(ValidationError):
        EntryCostScenarioRow.model_validate(
            {
                "scenario_label": "base_5bps",
                "slippage_bps": 5,
                "hypothetical_fill_price": 10.0,
                "affordable_shares": 100,
                "affordable_lots": 1,
                "stock_notional": 1000.0,
                "commission": 5.0,
                "total_cash_used": 1005.0,
                "unused_target_cash": 0.0,
                "can_afford_one_lot": True,
                "legal_limit_cap_applied": 1,
            }
        )
    with pytest.raises(ValidationError):
        LayerTwoEntryExecutionVerificationResult.model_validate(
            {
                "report_id": "ab" * 32,
                "structural_ok": 1,
            }
        )
    with pytest.raises(ValidationError):
        LayerTwoEntryExecutionVerificationResult.model_validate(
            {
                "report_id": "ab" * 32,
                "structural_ok": True,
                "allocator_binding_ok": "true",
            }
        )
    with pytest.raises(ValidationError):
        LayerTwoEntryExecutionVerificationResult.model_validate(
            {
                "report_id": "ab" * 32,
                "structural_ok": True,
                "ready_for_orders": 0,
            }
        )


def test_outer_reseal_outcome_snapshot_scenario_rejected() -> None:
    bundle, _, _, _, constraint, state, ranking, allocator, t1 = _t1_bundle_inputs()
    entry = allocator.proposed_entry
    assert entry is not None
    observation = _obs(
        symbol=entry.symbol,
        execution_date=t1,
        snapshot=allocator.market_data_snapshot_id,
        status="tradable",
        raw_open=10.0,
        up_limit=11.0,
    )
    report = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=observation,
    )
    payload = report.model_dump(mode="json")
    payload["observation"]["market_data_snapshot_id"] = "cd" * 32
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoEntryExecutionDiagnosticReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    payload["outcome"] = "blocked_suspension"
    payload["base_scenario"] = None
    payload["stress_scenario"] = None
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoEntryExecutionDiagnosticReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    assert payload["base_scenario"] is not None
    payload["base_scenario"]["scenario_label"] = "stress_15bps"
    payload["base_scenario"]["slippage_bps"] = 15
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoEntryExecutionDiagnosticReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    assert payload["base_scenario"] is not None
    payload["base_scenario"]["unused_target_cash"] = float(payload["base_scenario"]["unused_target_cash"]) + 1.0
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoEntryExecutionDiagnosticReport.model_validate(payload)

    payload = report.model_dump(mode="json")
    assert payload["base_scenario"] is not None
    payload["base_scenario"]["hypothetical_fill_price"] = 11.5
    payload["base_scenario"]["stock_notional"] = 11.5 * payload["base_scenario"]["affordable_shares"]
    payload["base_scenario"]["commission"] = max(payload["base_scenario"]["stock_notional"] * 0.00025, 5.0)
    payload["base_scenario"]["total_cash_used"] = (
        payload["base_scenario"]["stock_notional"] + payload["base_scenario"]["commission"]
    )
    payload["base_scenario"]["unused_target_cash"] = (
        float(payload["proposed_target_notional"]) - payload["base_scenario"]["total_cash_used"]
    )
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoEntryExecutionDiagnosticReport.model_validate(payload)


def test_outer_reseal_and_file_verifier(tmp_path: Path) -> None:
    bundle, eligibility, financials, cluster, constraint, state, ranking, allocator, t1 = _t1_bundle_inputs()
    entry = allocator.proposed_entry
    assert entry is not None
    observation = _obs(
        symbol=entry.symbol,
        execution_date=t1,
        snapshot=allocator.market_data_snapshot_id,
        status="tradable",
        raw_open=10.0,
        up_limit=11.0,
    )
    report = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=observation,
    )
    tampered = report.model_copy(update={"market_data_snapshot_id": "11" * 32})
    with pytest.raises(ValueError, match="report_id"):
        verify_layer_two_entry_execution_diagnostic_report(
            tampered,
            allocator_report=allocator,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            phase_report=bundle.phase,
            execution_observation=observation,
        )
    payload = report.model_dump(mode="json")
    payload["outcome"] = "blocked_suspension"
    payload["base_scenario"] = None
    payload["stress_scenario"] = None
    payload["observation"]["observation_status"] = "known_full_day_suspension"
    payload["observation"]["raw_open"] = None
    payload["observation"]["published_up_limit"] = None
    payload.pop("report_id", None)
    drifted = seal_layer_two_entry_execution_diagnostic_report(
        LayerTwoEntryExecutionDiagnosticReport.model_validate(payload)
    )
    with pytest.raises(ValueError, match="does not match full recompute"):
        verify_layer_two_entry_execution_diagnostic_report(
            drifted,
            allocator_report=allocator,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            phase_report=bundle.phase,
            execution_observation=observation,
        )

    missing = tmp_path / "missing-phase.json"
    with pytest.raises(ValueError, match="phase report file missing"):
        verify_layer_two_entry_execution_diagnostic_report_file(
            report=report,
            allocator_report=allocator,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            phase_report=bundle.phase,
            execution_observation=observation,
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            store=bundle.store,
            repo_root=REPO_ROOT,
            phase_report_path=missing,
        )

    phase_path = tmp_path / "phase.json"
    write_layer_two_tranche_phase_schedule_report(phase_path, bundle.phase)
    result = verify_layer_two_entry_execution_diagnostic_report_file(
        report=report,
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=observation,
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        store=bundle.store,
        repo_root=REPO_ROOT,
        phase_report_path=phase_path,
    )
    assert result.allocator_binding_ok is True
    assert result.phase_binding_ok is True
    assert result.tranche_evaluation_protocol_binding_ok is True
    assert result.execution_observation_binding_ok is True

    tampered_phase = json.loads(phase_path.read_text(encoding="utf-8"))
    tampered_phase["current_account_equity"] = float(bundle.equity) + 1.0
    phase_path.write_text(json.dumps(tampered_phase, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        verify_layer_two_entry_execution_diagnostic_report_file(
            report=report,
            allocator_report=allocator,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            phase_report=bundle.phase,
            execution_observation=observation,
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            store=bundle.store,
            repo_root=REPO_ROOT,
            phase_report_path=phase_path,
        )


def test_ready_flag_injection_rejected() -> None:
    bundle, _, _, _, constraint, state, ranking, allocator, t1 = _t1_bundle_inputs()
    entry = allocator.proposed_entry
    assert entry is not None
    report = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=_obs(
            symbol=entry.symbol,
            execution_date=t1,
            snapshot=allocator.market_data_snapshot_id,
            status="unknown",
        ),
    )
    payload = report.model_dump(mode="json")
    for flag in ("ready_for_scoring", "ready_for_orders", "ready_for_trading", "auto_apply"):
        bad = json.loads(json.dumps(payload))
        bad[flag] = True
        bad.pop("report_id", None)
        with pytest.raises(ValidationError):
            LayerTwoEntryExecutionDiagnosticReport.model_validate(bad)
    bad = json.loads(json.dumps(payload))
    bad["must_not_feed_ranking_or_scoring"] = 1
    bad.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoEntryExecutionDiagnosticReport.model_validate(bad)


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
    )
    for module in imported:
        assert not any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_prefixes)
    assert "app.backtest.engine" not in imported
    assert "ScoringEngine" not in source
    assert "BacktestEngine" not in source
    assert "StrategyConfig" not in source
    # Pure cost helpers are allowed; engines are not.
    assert "app.backtest.costs" in imported


def test_legal_price_boundary_strict_no_cash_tol() -> None:
    from app.research.layer_two_entry_execution_diagnostic import _build_scenario_row

    bundle, _, _, _, constraint, state, ranking, allocator, t1 = _t1_bundle_inputs()
    entry = allocator.proposed_entry
    assert entry is not None
    up = 10.0
    just_below = up - 5e-10
    just_above = up + 5e-10
    ok = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=_obs(
            symbol=entry.symbol,
            execution_date=t1,
            snapshot=allocator.market_data_snapshot_id,
            status="tradable",
            raw_open=just_below,
            up_limit=up,
        ),
    )
    assert ok.outcome in ("hypothetically_fillable", "unaffordable_board_lot_or_minimum_commission")
    eq = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=_obs(
            symbol=entry.symbol,
            execution_date=t1,
            snapshot=allocator.market_data_snapshot_id,
            status="tradable",
            raw_open=up,
            up_limit=up,
        ),
    )
    assert eq.outcome == "blocked_limit_up"
    with pytest.raises(ValueError, match="strictly below"):
        _build_scenario_row(
            label="base_5bps",
            slippage_bps=5,
            raw_open=up,
            published_up_limit=up,
            target_notional=float(entry.target_notional),
        )
    above = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=_obs(
            symbol=entry.symbol,
            execution_date=t1,
            snapshot=allocator.market_data_snapshot_id,
            status="tradable",
            raw_open=just_above,
            up_limit=up,
        ),
    )
    assert above.outcome == "blocked_limit_up"
    exact_up = 10.001
    exact_raw = exact_up / (1.0 + 5.0 / 10_000.0)
    row = _build_scenario_row(
        label="base_5bps",
        slippage_bps=5,
        raw_open=exact_raw,
        published_up_limit=exact_up,
        target_notional=float(entry.target_notional),
    )
    assert row.legal_limit_cap_applied is False
    assert row.hypothetical_fill_price == exact_up


def test_execution_observation_store_binding_attacks(tmp_path: Path) -> None:
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
    phase_path = tmp_path / "phase.json"
    write_layer_two_tranche_phase_schedule_report(phase_path, bundle.phase)

    def _file(*, store=bundle.store, rep=report, obs=observation):
        return verify_layer_two_entry_execution_diagnostic_report_file(
            report=rep,
            allocator_report=allocator,
            constraint_report=constraint,
            current_state=state,
            ranking=ranking,
            phase_report=bundle.phase,
            execution_observation=obs,
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            store=store,
            repo_root=REPO_ROOT,
            phase_report_path=phase_path,
        )

    assert _file().execution_observation_binding_ok is True

    bad_obs = _obs(
        symbol=entry.symbol,
        execution_date=t1,
        snapshot=allocator.market_data_snapshot_id,
        status="tradable",
        raw_open=FIXTURE_T1_OPEN + 0.5,
        up_limit=FIXTURE_T1_UP_LIMIT,
    )
    bad_report = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=bad_obs,
    )
    with pytest.raises(ValueError, match="raw_open must exactly equal store open"):
        _file(rep=bad_report, obs=bad_obs)

    unknown_obs = _obs(
        symbol=entry.symbol,
        execution_date=t1,
        snapshot=allocator.market_data_snapshot_id,
        status="unknown",
    )
    unknown_report = diagnose_layer_two_entry_execution(
        allocator_report=allocator,
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
        phase_report=bundle.phase,
        execution_observation=unknown_obs,
    )
    with pytest.raises(ValueError, match="unknown execution observation forbidden"):
        _file(rep=unknown_report, obs=unknown_obs)

    bare = _t1_bundle_inputs(inject_tradable_t1_bars=False)
    b2, e2, f2, c2, cons2, st2, rank2, alloc2, t1b = bare
    entry2 = alloc2.proposed_entry
    assert entry2 is not None
    unk2 = _obs(
        symbol=entry2.symbol,
        execution_date=t1b,
        snapshot=alloc2.market_data_snapshot_id,
        status="unknown",
    )
    unk_rep = diagnose_layer_two_entry_execution(
        allocator_report=alloc2,
        constraint_report=cons2,
        current_state=st2,
        ranking=rank2,
        phase_report=b2.phase,
        execution_observation=unk2,
    )
    phase2 = tmp_path / "phase-bare.json"
    write_layer_two_tranche_phase_schedule_report(phase2, b2.phase)
    ok_unknown = verify_layer_two_entry_execution_diagnostic_report_file(
        report=unk_rep,
        allocator_report=alloc2,
        constraint_report=cons2,
        current_state=st2,
        ranking=rank2,
        phase_report=b2.phase,
        execution_observation=unk2,
        eligibility_report=e2,
        financial_reports=f2,
        cluster_report=c2,
        store=b2.store,
        repo_root=REPO_ROOT,
        phase_report_path=phase2,
    )
    assert ok_unknown.execution_observation_binding_ok is True

    with pytest.raises(ValidationError, match="partial disk bindings"):
        LayerTwoEntryExecutionVerificationResult(
            report_id="ab" * 32,
            structural_ok=True,
            allocator_binding_ok=True,
            phase_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
            execution_observation_binding_ok=False,
        )
