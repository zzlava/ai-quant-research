"""Attack-oriented tests for fixed 40-market-bar exit diagnostic (E10f-2)."""

from __future__ import annotations

import ast
import math
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.research.a_share_stamp_tax_schedule import (
    EXPECTED_CURRENT_CONTRACT_ID,
    AShareStampTaxScheduleVerificationResult,
    build_a_share_stamp_tax_schedule_v1,
    stamp_tax_rate_for,
    verify_a_share_stamp_tax_schedule,
)
from app.research.layer_two_constraint_assembler import assemble_layer_two_constraints
from app.research.layer_two_entry_execution_diagnostic import diagnose_layer_two_entry_execution
from app.research.layer_two_fixed_horizon_exit_diagnostic import (
    BOUND_EXIT_ATTEMPT_OFFSET_FROM_ENTRY_INDEX,
    BOUND_HOLDING_PERIOD_MARKET_BARS,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_ID,
    BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH,
    LayerTwoFixedHorizonExitDiagnosticReport,
    LayerTwoFixedHorizonExitFileInput,
    LayerTwoFixedHorizonExitObservation,
    LayerTwoFixedHorizonExitStructuralInput,
    LayerTwoFixedHorizonExitVerificationResult,
    assert_report_self_hash,
    build_exit_sell_cost_scenario,
    diagnose_layer_two_fixed_horizon_exit,
    seal_layer_two_fixed_horizon_exit_diagnostic_report,
    verify_layer_two_fixed_horizon_exit_diagnostic_report,
    verify_layer_two_fixed_horizon_exit_diagnostic_report_file,
)
from app.research.layer_two_hypothetical_position_lifecycle import (
    LayerTwoHypotheticalLifecycleFileBindings,
    LayerTwoHypotheticalLifecycleFileInput,
    LayerTwoHypotheticalLifecycleStructuralInput,
    LayerTwoHypotheticalPositionLifecycleVerificationResult,
    open_layer_two_hypothetical_position_lifecycle,
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
from app.research.tranche_evaluation_protocol import (
    TrancheEvaluationProtocolV2,
    verify_tranche_evaluation_protocol_draft_file,
)
from tests.helpers import PROJECT_ROOT
from tests.test_layer_two_constraint_assembler import _Bundle, _cluster, _eligibility, _financials_for
from tests.test_layer_two_entry_execution_diagnostic import (
    FIXTURE_T1_DOWN_LIMIT,
    FIXTURE_T1_OPEN,
    FIXTURE_T1_UP_LIMIT,
    _extend_store_with_daily_bars,
    _obs,
)

REPO_ROOT = PROJECT_ROOT
MODULE_PATH = REPO_ROOT / "src/app/research/layer_two_fixed_horizon_exit_diagnostic.py"

# Exit-day store prices used by happy-path tradable observations (hashed into MarketStore).
FIXTURE_EXIT_OPEN = 12.0
FIXTURE_EXIT_DOWN_LIMIT = 10.0
FIXTURE_EXIT_UP_LIMIT = 13.0


def _next_weekdays_after(day: date, count: int) -> list[date]:
    out: list[date] = []
    cursor = day
    while len(out) < count:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            out.append(cursor)
    return out


def _lifecycle_chain(*, extra_after_entry: int = 45):
    """Build sealed E10f-1 open record with phase calendar covering entry+extra days.

    Entry + future exit daily bars are written into one InMemoryStore before snapshot/phase
    so E10e-0/E10f-2 file observation bindings can pass. Default covers fixed 40-bar exit.
    """
    bundle = _Bundle(anchor_index=0)
    after = _next_weekdays_after(bundle.as_of, 1 + extra_after_entry)
    entry_day = after[0]
    exit_days = after[1:]
    extended = [*bundle.calendar, *after]
    # Entry T+1 bars first (open/up for E10e-0), then future exit bars (open/down for E10f-2).
    store = _extend_store_with_daily_bars(
        bundle.store,
        extra_days=[entry_day],
        open_=FIXTURE_T1_OPEN,
        up_limit=FIXTURE_T1_UP_LIMIT,
        down_limit=FIXTURE_T1_DOWN_LIMIT,
    )
    store = _extend_store_with_daily_bars(
        store,
        extra_days=exit_days,
        open_=FIXTURE_EXIT_OPEN,
        up_limit=FIXTURE_EXIT_UP_LIMIT,
        down_limit=FIXTURE_EXIT_DOWN_LIMIT,
    )
    bundle.store = store
    bundle.market_snap = store.snapshot().snapshot_id
    bundle.calendar = extended
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
    ranking = UnvalidatedDevelopmentRankingInput(ranked_symbols=list(eligible))
    allocator = allocate_layer_two_stateful_single_opportunity(
        constraint_report=constraint,
        current_state=state,
        ranking=ranking,
    )
    assert allocator.proposed_entry is not None
    observation = _obs(
        symbol=allocator.proposed_entry.symbol,
        execution_date=entry_day,
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
    lifecycle_structural = LayerTwoHypotheticalLifecycleStructuralInput(
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
    stamp = build_a_share_stamp_tax_schedule_v1()
    verify_a_share_stamp_tax_schedule(stamp)
    return (
        bundle,
        eligibility,
        financials,
        cluster,
        lifecycle_structural,
        lifecycle,
        stamp,
        list(phase.market_calendar),
    )


def _exit_obs(
    *,
    symbol: str,
    day: date,
    snapshot: str,
    status: str,
    raw_open: float | None = None,
    down_limit: float | None = None,
) -> LayerTwoFixedHorizonExitObservation:
    return LayerTwoFixedHorizonExitObservation.model_validate(
        {
            "symbol": symbol,
            "observation_date": day,
            "market_data_snapshot_id": snapshot,
            "observation_status": status,
            "raw_open": raw_open,
            "published_down_limit": down_limit,
        }
    )


def _scheduled(calendar: list[date], entry: date) -> date:
    return calendar[calendar.index(entry) + BOUND_EXIT_ATTEMPT_OFFSET_FROM_ENTRY_INDEX]


def _structural(lifecycle, lifecycle_structural, stamp, calendar, observations):
    return LayerTwoFixedHorizonExitStructuralInput(
        lifecycle_record=lifecycle,
        lifecycle_structural=lifecycle_structural,
        stamp_tax_contract=stamp,
        market_calendar=tuple(calendar),
        exit_observations=tuple(observations),
    )


def test_hypothetically_exitable_happy_path_and_structural_bindings_false() -> None:
    bundle, eligibility, financials, cluster, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    scheduled = _scheduled(calendar, lifecycle.entry_trade_date)
    observations = [
        _exit_obs(
            symbol=lifecycle.symbol,
            day=scheduled,
            snapshot=lifecycle.market_data_snapshot_id,
            status="tradable",
            raw_open=12.0,
            down_limit=10.0,
        )
    ]
    report = diagnose_layer_two_fixed_horizon_exit(
        lifecycle_record=lifecycle,
        lifecycle_structural=life_struct,
        stamp_tax_contract=stamp,
        market_calendar=calendar,
        exit_observations=observations,
    )
    assert report.holding_period_market_bars == BOUND_HOLDING_PERIOD_MARKET_BARS == 40
    assert report.scheduled_exit_attempt_date == scheduled
    assert report.final_outcome == "hypothetically_exitable"
    assert report.attempt_rows[0].holding_market_bars_elapsed_before_open == 40
    assert report.tranche_evaluation_protocol_id == BOUND_TRANCHE_EVALUATION_PROTOCOL_ID
    assert report.tranche_evaluation_protocol_path == BOUND_TRANCHE_EVALUATION_PROTOCOL_PATH
    assert report.tranche_evaluation_protocol_id == lifecycle.tranche_evaluation_protocol_id
    assert report.base_scenario is not None and report.stress_scenario is not None
    assert report.base_scenario.shares == lifecycle.shares
    assert report.stamp_tax_contract_id == EXPECTED_CURRENT_CONTRACT_ID
    assert report.ready_for_exit_diagnostic is False
    assert report.ready_for_scoring is False
    assert report.does_not_invent_return_pnl_or_alpha is True
    assert report.holds_frozen_40_bar_two_layer_protocol_not_consumed_p10_h20 is True
    structural = _structural(lifecycle, life_struct, stamp, calendar, observations)
    result = verify_layer_two_fixed_horizon_exit_diagnostic_report(report, structural=structural)
    assert result.structural_ok is True
    assert result.lifecycle_binding_ok is False
    assert result.stamp_tax_binding_ok is False
    assert result.tranche_evaluation_protocol_binding_ok is False
    assert result.exit_observation_binding_ok is False
    assert result.ready_for_exit_diagnostic is False
    _ = bundle, eligibility, financials, cluster


def test_off_by_one_calendar_index_and_short_calendar() -> None:
    from app.research.layer_two_fixed_horizon_exit_diagnostic import (
        _assert_strict_calendar,
        _scheduled_exit_date,
    )

    _b, _e, _f, _c, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    entry_index = calendar.index(lifecycle.entry_trade_date)
    wrong_early = calendar[entry_index + 39]
    assert wrong_early != calendar[entry_index + BOUND_EXIT_ATTEMPT_OFFSET_FROM_ENTRY_INDEX]
    wrong_obs = [
        _exit_obs(
            symbol=lifecycle.symbol,
            day=wrong_early,
            snapshot=lifecycle.market_data_snapshot_id,
            status="tradable",
            raw_open=12.0,
            down_limit=10.0,
        )
    ]
    with pytest.raises(ValueError, match="start at scheduled|scheduled_exit"):
        diagnose_layer_two_fixed_horizon_exit(
            lifecycle_record=lifecycle,
            lifecycle_structural=life_struct,
            stamp_tax_contract=stamp,
            market_calendar=calendar,
            exit_observations=wrong_obs,
        )

    short = calendar[: entry_index + 40]  # missing index+40
    with pytest.raises(ValueError, match="too short|entry\\+"):
        _scheduled_exit_date(entry_trade_date=lifecycle.entry_trade_date, calendar=short)

    unordered = list(calendar)
    unordered[10], unordered[11] = unordered[11], unordered[10]
    with pytest.raises(ValueError, match="strictly increasing|unique"):
        _assert_strict_calendar(unordered)

    with pytest.raises(ValueError, match="entry_trade_date missing"):
        _scheduled_exit_date(
            entry_trade_date=lifecycle.entry_trade_date,
            calendar=[d for d in calendar if d != lifecycle.entry_trade_date],
        )

    with pytest.raises(ValueError, match="phase_report.market_calendar"):
        diagnose_layer_two_fixed_horizon_exit(
            lifecycle_record=lifecycle,
            lifecycle_structural=life_struct,
            stamp_tax_contract=stamp,
            market_calendar=short,
            exit_observations=[
                _exit_obs(
                    symbol=lifecycle.symbol,
                    day=lifecycle.entry_trade_date,
                    snapshot=lifecycle.market_data_snapshot_id,
                    status="tradable",
                    raw_open=12.0,
                    down_limit=10.0,
                )
            ],
        )


def test_observation_scan_rules_unknown_suspension_limit_down_still_open() -> None:
    _b, _e, _f, _c, life_struct, lifecycle, stamp, calendar = _lifecycle_chain(extra_after_entry=50)
    scheduled = _scheduled(calendar, lifecycle.entry_trade_date)
    idx = calendar.index(scheduled)
    snap = lifecycle.market_data_snapshot_id
    sym = lifecycle.symbol

    unknown = diagnose_layer_two_fixed_horizon_exit(
        lifecycle_record=lifecycle,
        lifecycle_structural=life_struct,
        stamp_tax_contract=stamp,
        market_calendar=calendar,
        exit_observations=[_exit_obs(symbol=sym, day=scheduled, snapshot=snap, status="unknown")],
    )
    assert unknown.final_outcome == "unknown_exit_observation"
    assert unknown.base_scenario is None

    with pytest.raises(ValueError, match="after unknown"):
        diagnose_layer_two_fixed_horizon_exit(
            lifecycle_record=lifecycle,
            lifecycle_structural=life_struct,
            stamp_tax_contract=stamp,
            market_calendar=calendar,
            exit_observations=[
                _exit_obs(symbol=sym, day=scheduled, snapshot=snap, status="unknown"),
                _exit_obs(
                    symbol=sym,
                    day=calendar[idx + 1],
                    snapshot=snap,
                    status="tradable",
                    raw_open=12.0,
                    down_limit=10.0,
                ),
            ],
        )

    blocked_then_exit = diagnose_layer_two_fixed_horizon_exit(
        lifecycle_record=lifecycle,
        lifecycle_structural=life_struct,
        stamp_tax_contract=stamp,
        market_calendar=calendar,
        exit_observations=[
            _exit_obs(symbol=sym, day=scheduled, snapshot=snap, status="known_full_day_suspension"),
            _exit_obs(
                symbol=sym,
                day=calendar[idx + 1],
                snapshot=snap,
                status="tradable",
                raw_open=9.0,
                down_limit=9.0,
            ),
            _exit_obs(
                symbol=sym,
                day=calendar[idx + 2],
                snapshot=snap,
                status="tradable",
                raw_open=12.0,
                down_limit=10.0,
            ),
        ],
    )
    assert blocked_then_exit.final_outcome == "hypothetically_exitable"
    assert blocked_then_exit.blocked_suspension_days == 1
    assert blocked_then_exit.blocked_limit_down_days == 1
    assert blocked_then_exit.attempt_rows[0].holding_market_bars_elapsed_before_open == 40
    assert blocked_then_exit.attempt_rows[1].holding_market_bars_elapsed_before_open == 41
    assert blocked_then_exit.attempt_rows[-1].holding_market_bars_elapsed_before_open == 42

    with pytest.raises(ValueError, match="after first hypothetically_exitable"):
        diagnose_layer_two_fixed_horizon_exit(
            lifecycle_record=lifecycle,
            lifecycle_structural=life_struct,
            stamp_tax_contract=stamp,
            market_calendar=calendar,
            exit_observations=[
                _exit_obs(
                    symbol=sym,
                    day=scheduled,
                    snapshot=snap,
                    status="tradable",
                    raw_open=12.0,
                    down_limit=10.0,
                ),
                _exit_obs(
                    symbol=sym,
                    day=calendar[idx + 1],
                    snapshot=snap,
                    status="tradable",
                    raw_open=12.0,
                    down_limit=10.0,
                ),
            ],
        )

    still_open = diagnose_layer_two_fixed_horizon_exit(
        lifecycle_record=lifecycle,
        lifecycle_structural=life_struct,
        stamp_tax_contract=stamp,
        market_calendar=calendar,
        exit_observations=[
            _exit_obs(symbol=sym, day=scheduled, snapshot=snap, status="known_full_day_suspension"),
            _exit_obs(
                symbol=sym,
                day=calendar[idx + 1],
                snapshot=snap,
                status="tradable",
                raw_open=8.0,
                down_limit=8.5,
            ),
        ],
    )
    assert still_open.final_outcome == "still_open_after_observed_blocks"
    assert still_open.exit_observation_date is None

    with pytest.raises(ValueError, match="contiguous|gaps|start at scheduled"):
        diagnose_layer_two_fixed_horizon_exit(
            lifecycle_record=lifecycle,
            lifecycle_structural=life_struct,
            stamp_tax_contract=stamp,
            market_calendar=calendar,
            exit_observations=[
                _exit_obs(symbol=sym, day=scheduled, snapshot=snap, status="known_full_day_suspension"),
                _exit_obs(
                    symbol=sym,
                    day=calendar[idx + 2],
                    snapshot=snap,
                    status="known_full_day_suspension",
                ),
            ],
        )


def test_observation_field_validation() -> None:
    with pytest.raises(ValidationError):
        _exit_obs(
            symbol="000001.SZ",
            day=date(2023, 7, 1),
            snapshot="snap",
            status="unknown",
            raw_open=10.0,
            down_limit=None,
        )
    with pytest.raises(ValidationError):
        _exit_obs(
            symbol="000001.SZ",
            day=date(2023, 7, 1),
            snapshot="snap",
            status="tradable",
            raw_open=None,
            down_limit=10.0,
        )
    for bad in (True, math.nan, math.inf, 0.0, -1.0):
        with pytest.raises(ValidationError):
            _exit_obs(
                symbol="000001.SZ",
                day=date(2023, 7, 1),
                snapshot="snap",
                status="tradable",
                raw_open=bad if bad is not True else True,
                down_limit=9.0,
            )


def test_legal_price_boundary_strict_no_cash_tol() -> None:
    """Cash amount tol must not decide legal raw/down/slipped/fill ordering."""
    from app.research.layer_two_fixed_horizon_exit_diagnostic import _scan_exit_attempts

    stamp = build_a_share_stamp_tax_schedule_v1()
    verify_a_share_stamp_tax_schedule(stamp)
    down = 10.0
    just_below = down - 5e-10
    just_above = down + 5e-10
    trade_day = date(2023, 8, 28)
    calendar = [trade_day]
    snap = "snap-legal-boundary"
    sym = "000001.SZ"

    # raw slightly below down → blocked; helper rejects.
    final_below, rows_below, _, _ = _scan_exit_attempts(
        observations=[
            _exit_obs(symbol=sym, day=trade_day, snapshot=snap, status="tradable", raw_open=just_below, down_limit=down)
        ],
        calendar=calendar,
        scheduled=trade_day,
        symbol=sym,
        snapshot_id=snap,
    )
    assert final_below == "still_open_after_observed_blocks"
    assert rows_below[0].attempt_outcome == "blocked_limit_down"
    with pytest.raises(ValueError, match="strictly above published_down_limit"):
        build_exit_sell_cost_scenario(
            label="base_5bps",
            slippage_bps=5,
            raw_open=just_below,
            published_down_limit=down,
            shares=100,
            trade_date=trade_day,
            stamp_tax_contract=stamp,
        )

    # raw exactly equal down → blocked / helper rejects.
    final_eq, rows_eq, _, _ = _scan_exit_attempts(
        observations=[
            _exit_obs(symbol=sym, day=trade_day, snapshot=snap, status="tradable", raw_open=down, down_limit=down)
        ],
        calendar=calendar,
        scheduled=trade_day,
        symbol=sym,
        snapshot_id=snap,
    )
    assert rows_eq[0].attempt_outcome == "blocked_limit_down"
    assert final_eq == "still_open_after_observed_blocks"
    with pytest.raises(ValueError, match="strictly above published_down_limit"):
        build_exit_sell_cost_scenario(
            label="base_5bps",
            slippage_bps=5,
            raw_open=down,
            published_down_limit=down,
            shares=100,
            trade_date=trade_day,
            stamp_tax_contract=stamp,
        )

    # raw slightly above down → exitable; stress floor may apply; fill <= raw exact.
    final_above, rows_above, _, _ = _scan_exit_attempts(
        observations=[
            _exit_obs(symbol=sym, day=trade_day, snapshot=snap, status="tradable", raw_open=just_above, down_limit=down)
        ],
        calendar=calendar,
        scheduled=trade_day,
        symbol=sym,
        snapshot_id=snap,
    )
    assert final_above == "hypothetically_exitable"
    assert rows_above[0].attempt_outcome == "hypothetically_exitable"
    floored = build_exit_sell_cost_scenario(
        label="stress_15bps",
        slippage_bps=15,
        raw_open=just_above,
        published_down_limit=down,
        shares=100,
        trade_date=trade_day,
        stamp_tax_contract=stamp,
    )
    assert floored.legal_limit_floor_applied is True
    assert floored.hypothetical_fill_price == down
    assert floored.hypothetical_fill_price <= just_above
    assert floored.hypothetical_fill_price >= down

    # slipped exactly equal to down → floor flag false, fill=down.
    # slipped = raw * (1 - 5/10000) == down  =>  raw = down / (1 - 0.0005)
    exact_raw = down / (1.0 - 5.0 / 10_000.0)
    assert exact_raw * (1.0 - 5.0 / 10_000.0) == down
    exact = build_exit_sell_cost_scenario(
        label="base_5bps",
        slippage_bps=5,
        raw_open=exact_raw,
        published_down_limit=down,
        shares=100,
        trade_date=trade_day,
        stamp_tax_contract=stamp,
    )
    assert exact.legal_limit_floor_applied is False
    assert exact.hypothetical_fill_price == down
    assert exact.hypothetical_fill_price <= exact_raw


def test_sell_scenarios_stamp_tax_switch_and_floor_and_2025_rejected() -> None:
    stamp = build_a_share_stamp_tax_schedule_v1()
    verify_a_share_stamp_tax_schedule(stamp)
    before = build_exit_sell_cost_scenario(
        label="base_5bps",
        slippage_bps=5,
        raw_open=10.0,
        published_down_limit=9.0,
        shares=100,
        trade_date=date(2023, 8, 27),
        stamp_tax_contract=stamp,
    )
    after = build_exit_sell_cost_scenario(
        label="base_5bps",
        slippage_bps=5,
        raw_open=10.0,
        published_down_limit=9.0,
        shares=100,
        trade_date=date(2023, 8, 28),
        stamp_tax_contract=stamp,
    )
    assert before.stamp_tax_rate == 0.001 == stamp_tax_rate_for(date(2023, 8, 27), "sell", contract=stamp)
    assert after.stamp_tax_rate == 0.0005 == stamp_tax_rate_for(date(2023, 8, 28), "sell", contract=stamp)
    floored = build_exit_sell_cost_scenario(
        label="stress_15bps",
        slippage_bps=15,
        raw_open=10.0,
        published_down_limit=9.995,
        shares=100,
        trade_date=date(2023, 8, 28),
        stamp_tax_contract=stamp,
    )
    assert floored.legal_limit_floor_applied is True
    assert floored.hypothetical_fill_price == 9.995
    with pytest.raises(ValueError, match="declared_window|2025"):
        build_exit_sell_cost_scenario(
            label="base_5bps",
            slippage_bps=5,
            raw_open=10.0,
            published_down_limit=9.0,
            shares=100,
            trade_date=date(2025, 1, 2),
            stamp_tax_contract=stamp,
        )


def test_outer_reseal_tamper_rejected() -> None:
    _b, _e, _f, _c, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    scheduled = _scheduled(calendar, lifecycle.entry_trade_date)
    observations = [
        _exit_obs(
            symbol=lifecycle.symbol,
            day=scheduled,
            snapshot=lifecycle.market_data_snapshot_id,
            status="tradable",
            raw_open=12.0,
            down_limit=10.0,
        )
    ]
    report = diagnose_layer_two_fixed_horizon_exit(
        lifecycle_record=lifecycle,
        lifecycle_structural=life_struct,
        stamp_tax_contract=stamp,
        market_calendar=calendar,
        exit_observations=observations,
    )
    structural = _structural(lifecycle, life_struct, stamp, calendar, observations)
    for field, value in (
        ("shares", report.shares + 100),
        ("scheduled_exit_attempt_date", (scheduled + timedelta(days=1)).isoformat()),
        ("market_data_snapshot_id", "tampered-snap"),
        ("symbol", "999999.SH"),
        ("lifecycle_record_id", "ab" * 32),
        ("stamp_tax_contract_id", "cd" * 32),
        ("blocked_suspension_days", report.blocked_suspension_days + 1),
    ):
        payload = report.model_dump(mode="json")
        payload[field] = value
        if field == "shares":
            payload["base_scenario"]["shares"] = value
            payload["stress_scenario"]["shares"] = value
            for key in ("base_scenario", "stress_scenario"):
                scen = payload[key]
                scen["gross_notional"] = scen["hypothetical_fill_price"] * value
                scen["commission"] = max(scen["gross_notional"] * 0.00025, 5.0)
                scen["stamp_tax"] = scen["gross_notional"] * scen["stamp_tax_rate"]
                scen["net_sale_cash"] = scen["gross_notional"] - scen["commission"] - scen["stamp_tax"]
        if field == "blocked_suspension_days":
            payload.pop("report_id", None)
            with pytest.raises(ValidationError, match="blocked_"):
                LayerTwoFixedHorizonExitDiagnosticReport.model_validate(payload)
            continue
        payload.pop("report_id", None)
        try:
            resealed = seal_layer_two_fixed_horizon_exit_diagnostic_report(
                LayerTwoFixedHorizonExitDiagnosticReport.model_validate(payload)
            )
        except ValidationError:
            continue
        assert_report_self_hash(resealed)
        with pytest.raises(ValueError, match="recompute|canonical payload|report_id"):
            verify_layer_two_fixed_horizon_exit_diagnostic_report(resealed, structural=structural)

    # Scenario amount tamper: model identity rejects drifted commission before reseal verify.
    payload = report.model_dump(mode="json")
    payload["base_scenario"]["commission"] = float(payload["base_scenario"]["commission"]) + 1.0
    payload["base_scenario"]["net_sale_cash"] = (
        payload["base_scenario"]["gross_notional"]
        - payload["base_scenario"]["commission"]
        - payload["base_scenario"]["stamp_tax"]
    )
    payload.pop("report_id", None)
    with pytest.raises(ValidationError, match="commission"):
        LayerTwoFixedHorizonExitDiagnosticReport.model_validate(payload)

    # Floor flag / rate tamper that keeps commission identity then fails recompute.
    payload = report.model_dump(mode="json")
    payload["base_scenario"]["legal_limit_floor_applied"] = not payload["base_scenario"]["legal_limit_floor_applied"]
    payload.pop("report_id", None)
    resealed = seal_layer_two_fixed_horizon_exit_diagnostic_report(
        LayerTwoFixedHorizonExitDiagnosticReport.model_validate(payload)
    )
    with pytest.raises(ValueError, match="recompute|canonical payload"):
        verify_layer_two_fixed_horizon_exit_diagnostic_report(resealed, structural=structural)

    missing = report.model_copy(update={"report_id": None})
    with pytest.raises(ValueError, match="report_id is missing"):
        verify_layer_two_fixed_horizon_exit_diagnostic_report(missing, structural=structural)
    bad_hash = report.model_copy(update={"report_id": "ab" * 32})
    with pytest.raises(ValueError, match="report_id"):
        verify_layer_two_fixed_horizon_exit_diagnostic_report(bad_hash, structural=structural)


def test_verification_result_state_machine() -> None:
    rid = "ab" * 32
    LayerTwoFixedHorizonExitVerificationResult(
        report_id=rid,
        structural_ok=True,
        lifecycle_binding_ok=False,
        stamp_tax_binding_ok=False,
        tranche_evaluation_protocol_binding_ok=False,
        exit_observation_binding_ok=False,
        ready_for_exit_diagnostic=False,
    )
    LayerTwoFixedHorizonExitVerificationResult(
        report_id=rid,
        structural_ok=True,
        lifecycle_binding_ok=True,
        stamp_tax_binding_ok=True,
        tranche_evaluation_protocol_binding_ok=True,
        exit_observation_binding_ok=True,
        ready_for_exit_diagnostic=True,
    )
    with pytest.raises(ValidationError, match="partial bindings"):
        LayerTwoFixedHorizonExitVerificationResult(
            report_id=rid,
            structural_ok=True,
            lifecycle_binding_ok=True,
            stamp_tax_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
            exit_observation_binding_ok=False,
            ready_for_exit_diagnostic=False,
        )
    with pytest.raises(ValidationError, match="ready_for_exit_diagnostic=true requires"):
        LayerTwoFixedHorizonExitVerificationResult(
            report_id=rid,
            structural_ok=True,
            lifecycle_binding_ok=False,
            stamp_tax_binding_ok=False,
            tranche_evaluation_protocol_binding_ok=False,
            exit_observation_binding_ok=False,
            ready_for_exit_diagnostic=True,
        )
    with pytest.raises(ValidationError, match="all bindings true requires ready"):
        LayerTwoFixedHorizonExitVerificationResult(
            report_id=rid,
            structural_ok=True,
            lifecycle_binding_ok=True,
            stamp_tax_binding_ok=True,
            tranche_evaluation_protocol_binding_ok=True,
            exit_observation_binding_ok=True,
            ready_for_exit_diagnostic=False,
        )
    with pytest.raises(ValidationError, match="structural_ok=false forbids"):
        LayerTwoFixedHorizonExitVerificationResult(
            report_id=rid,
            structural_ok=False,
            lifecycle_binding_ok=True,
            stamp_tax_binding_ok=True,
            tranche_evaluation_protocol_binding_ok=True,
            exit_observation_binding_ok=True,
            ready_for_exit_diagnostic=True,
        )


def test_file_verifier_rebuilds_ready(tmp_path: Path) -> None:
    bundle, eligibility, financials, cluster, life_struct, lifecycle, stamp, calendar = _lifecycle_chain()
    scheduled = _scheduled(calendar, lifecycle.entry_trade_date)
    observations = [
        _exit_obs(
            symbol=lifecycle.symbol,
            day=scheduled,
            snapshot=lifecycle.market_data_snapshot_id,
            status="tradable",
            raw_open=12.0,
            down_limit=10.0,
        )
    ]
    report = diagnose_layer_two_fixed_horizon_exit(
        lifecycle_record=lifecycle,
        lifecycle_structural=life_struct,
        stamp_tax_contract=stamp,
        market_calendar=calendar,
        exit_observations=observations,
    )
    structural = _structural(lifecycle, life_struct, stamp, calendar, observations)
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
    file_input = LayerTwoFixedHorizonExitFileInput(
        structural=structural,
        lifecycle_file=lifecycle_file,
        stamp_tax_repo_root=REPO_ROOT,
    )
    result = verify_layer_two_fixed_horizon_exit_diagnostic_report_file(report=report, file_input=file_input)
    assert result.lifecycle_binding_ok is True
    assert result.stamp_tax_binding_ok is True
    assert result.tranche_evaluation_protocol_binding_ok is True
    assert result.exit_observation_binding_ok is True
    assert result.ready_for_exit_diagnostic is True

    structural_only = verify_layer_two_fixed_horizon_exit_diagnostic_report(report, structural=structural)
    assert structural_only.ready_for_exit_diagnostic is False
    assert structural_only.exit_observation_binding_ok is False
    assert structural_only.tranche_evaluation_protocol_binding_ok is False

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
        "app.research.layer_two_fixed_horizon_exit_diagnostic.verify_layer_two_hypothetical_position_lifecycle_record_file",
        side_effect=_fake_life,
    ):
        with pytest.raises(ValueError, match="E10f-1 file verifier"):
            verify_layer_two_fixed_horizon_exit_diagnostic_report_file(report=report, file_input=file_input)

    def _fake_stamp(**kwargs):
        return stamp, AShareStampTaxScheduleVerificationResult(
            contract_id=EXPECTED_CURRENT_CONTRACT_ID,
            structural_ok=True,
            disk_binding_ok=False,
            ready_for_exit_diagnostic=False,
        )

    with patch(
        "app.research.layer_two_fixed_horizon_exit_diagnostic.verify_a_share_stamp_tax_schedule_file",
        side_effect=_fake_stamp,
    ):
        with pytest.raises(ValueError, match="E10f-0 file verifier|disk_binding"):
            verify_layer_two_fixed_horizon_exit_diagnostic_report_file(report=report, file_input=file_input)

    def _fake_protocol_id(**kwargs):
        doc, result = verify_tranche_evaluation_protocol_draft_file(**kwargs)
        return doc, result.model_copy(update={"protocol_id": "ff" * 32})

    with patch(
        "app.research.layer_two_fixed_horizon_exit_diagnostic.verify_tranche_evaluation_protocol_draft_file",
        side_effect=_fake_protocol_id,
    ):
        with pytest.raises(ValueError, match="protocol_id"):
            verify_layer_two_fixed_horizon_exit_diagnostic_report_file(report=report, file_input=file_input)

    def _fake_holding_20(**kwargs):
        doc, result = verify_tranche_evaluation_protocol_draft_file(**kwargs)
        fake = MagicMock(spec=TrancheEvaluationProtocolV2)
        fake.protocol_id = doc.protocol_id
        hold = MagicMock()
        hold.holding_period_market_trading_days = 20
        hold.holding_cycle_market_trading_days = 20
        fake.tranche_hold = hold
        timing = MagicMock()
        timing.fill_day_is_holding_day_1 = True
        timing.exit_after_holding_period_at_next_tradable_open = True
        fake.decision_timing = timing
        return fake, result

    with patch(
        "app.research.layer_two_fixed_horizon_exit_diagnostic.verify_tranche_evaluation_protocol_draft_file",
        side_effect=_fake_holding_20,
    ):
        with pytest.raises(ValueError, match="holding_period_market_trading_days|must equal 40"):
            verify_layer_two_fixed_horizon_exit_diagnostic_report_file(report=report, file_input=file_input)

    def _fake_decision_timing(**kwargs):
        doc, result = verify_tranche_evaluation_protocol_draft_file(**kwargs)
        fake = MagicMock(spec=TrancheEvaluationProtocolV2)
        fake.protocol_id = doc.protocol_id
        fake.tranche_hold = doc.tranche_hold
        timing = MagicMock()
        timing.fill_day_is_holding_day_1 = False
        timing.exit_after_holding_period_at_next_tradable_open = True
        fake.decision_timing = timing
        return fake, result

    with patch(
        "app.research.layer_two_fixed_horizon_exit_diagnostic.verify_tranche_evaluation_protocol_draft_file",
        side_effect=_fake_decision_timing,
    ):
        with pytest.raises(ValueError, match="disk decision_timing"):
            verify_layer_two_fixed_horizon_exit_diagnostic_report_file(report=report, file_input=file_input)

    # Fabricated tradable open that does not match hashed MarketStore row.
    forged = _exit_obs(
        symbol=lifecycle.symbol,
        day=scheduled,
        snapshot=lifecycle.market_data_snapshot_id,
        status="tradable",
        raw_open=FIXTURE_EXIT_OPEN + 1.0,
        down_limit=FIXTURE_EXIT_DOWN_LIMIT,
    )
    forged_report = diagnose_layer_two_fixed_horizon_exit(
        lifecycle_record=lifecycle,
        lifecycle_structural=life_struct,
        stamp_tax_contract=stamp,
        market_calendar=calendar,
        exit_observations=[forged],
    )
    forged_structural = _structural(lifecycle, life_struct, stamp, calendar, [forged])
    forged_file = LayerTwoFixedHorizonExitFileInput(
        structural=forged_structural,
        lifecycle_file=lifecycle_file,
        stamp_tax_repo_root=REPO_ROOT,
    )
    with pytest.raises(ValueError, match="raw_open|open"):
        verify_layer_two_fixed_horizon_exit_diagnostic_report_file(report=forged_report, file_input=forged_file)

    from app.research.layer_two_fixed_horizon_exit_diagnostic import _bind_exit_observations_to_store

    # unknown over a complete tradable store row must fail.
    unknown_over = _exit_obs(
        symbol=lifecycle.symbol,
        day=scheduled,
        snapshot=lifecycle.market_data_snapshot_id,
        status="unknown",
    )
    with pytest.raises(ValueError, match="unknown exit observation forbidden|complete determinate"):
        _bind_exit_observations_to_store(
            report=report,
            observations=[unknown_over],
            store=bundle.store,
            market_calendar=calendar,
        )

    # adj_open impersonation: observation claims adj_open while store raw open differs.
    adj_obs = _exit_obs(
        symbol=lifecycle.symbol,
        day=scheduled,
        snapshot=lifecycle.market_data_snapshot_id,
        status="tradable",
        raw_open=FIXTURE_EXIT_OPEN + 0.5,
        down_limit=FIXTURE_EXIT_DOWN_LIMIT,
    )
    with pytest.raises(ValueError, match="raw_open|open"):
        _bind_exit_observations_to_store(
            report=report,
            observations=[adj_obs],
            store=bundle.store,
            market_calendar=calendar,
        )

    # Snapshot mismatch.
    with pytest.raises(ValueError, match="snapshot_id"):
        _bind_exit_observations_to_store(
            report=report.model_copy(update={"market_data_snapshot_id": "ab" * 32}),
            observations=observations,
            store=bundle.store,
            market_calendar=calendar,
        )

    # Missing exact symbol/date row fails tradable binding.
    missing_day = calendar[calendar.index(scheduled) + 1]
    missing_obs = _exit_obs(
        symbol=lifecycle.symbol,
        day=missing_day,
        snapshot=lifecycle.market_data_snapshot_id,
        status="tradable",
        raw_open=FIXTURE_EXIT_OPEN,
        down_limit=FIXTURE_EXIT_DOWN_LIMIT,
    )
    with pytest.raises(ValueError, match="exactly one MarketStore daily row|tradable"):
        _bind_exit_observations_to_store(
            report=report,
            observations=[
                _exit_obs(
                    symbol="ZZ.NOTEXIST",
                    day=scheduled,
                    snapshot=lifecycle.market_data_snapshot_id,
                    status="tradable",
                    raw_open=FIXTURE_EXIT_OPEN,
                    down_limit=FIXTURE_EXIT_DOWN_LIMIT,
                )
            ],
            store=bundle.store,
            market_calendar=calendar,
        )
    _ = missing_obs


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
    assert "from app.models.config import CostConfig" not in source
    assert "app.backtest.costs" not in source
    assert "stamp_tax_schedule=[]" not in source
