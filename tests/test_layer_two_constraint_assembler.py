"""Attack-oriented tests for layer-two constraint assembler (E10d-2)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.research.layer_two_allocation_protocol import plan_base_slots
from app.research.layer_two_candidate_eligibility import (
    LayerTwoCandidateInput,
    LayerTwoLiquidityObservation,
    evaluate_layer_two_candidate_eligibility,
)
from app.research.layer_two_constraint_assembler import (
    LayerTwoConstraintAssemblerReport,
    assemble_layer_two_constraints,
    compute_report_id,
    seal_layer_two_constraint_assembler_report,
    verify_layer_two_constraint_assembler_report,
    verify_layer_two_constraint_assembler_report_file,
    write_layer_two_constraint_assembler_report,
)
from app.research.layer_two_financial_negative_list import (
    NON_STANDARD_AUDIT_RULE,
    WARNING_RULE_CODES,
    LayerTwoFinancialNegativeEvidence,
    evaluate_layer_two_financial_negative_list,
)
from app.research.layer_two_statistical_risk_clusters import (
    diagnose_layer_two_statistical_risk_clusters,
)
from app.research.layer_two_tranche_phase_schedule import (
    plan_layer_two_tranche_phase_schedule,
    write_layer_two_tranche_phase_schedule_report,
)
from tests.helpers import PROJECT_ROOT
from tests.test_layer_two_statistical_risk_clusters import _complete_fixture

REPO_ROOT = PROJECT_ROOT


def _obs_days(end: date, count: int = 20) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def _liquidity(end: date, decision_at: datetime) -> list[LayerTwoLiquidityObservation]:
    return [
        LayerTwoLiquidityObservation.model_validate(
            {
                "observation_date": day,
                "available_at": decision_at - timedelta(minutes=30),
                "tradability": "tradable",
                "amount_cny": 60_000_000.0,
            }
        )
        for day in _obs_days(end)
    ]


def _market_for(symbol: str) -> str:
    return "SSE" if symbol.endswith(".SH") else "SZSE"


def _candidate(
    symbol: str,
    *,
    as_of: date,
    decision_at: datetime,
    planned: float,
    cap_cny: float = 12_000_000_000.0,
    eligible: bool = True,
) -> LayerTwoCandidateInput:
    return LayerTwoCandidateInput.model_validate(
        {
            "symbol": symbol,
            "market": _market_for(symbol),
            "is_ordinary_a_share": True,
            "is_bse": False,
            "is_st_or_delist_risk": False if eligible else True,
            "is_suspended_on_decision_date": False,
            "listed_market_trading_days": 200,
            "security_status_as_of": as_of,
            "security_status_available_at": decision_at - timedelta(minutes=45),
            "planned_buy_notional_cny": planned,
            "liquidity_observations": _liquidity(as_of, decision_at),
            "pit_free_float_market_cap_cny": cap_cny,
            "pit_free_float_market_cap_as_of": as_of,
            "pit_free_float_market_cap_available_at": decision_at - timedelta(minutes=20),
        }
    )


def _financial_evidence(
    symbol: str,
    *,
    as_of: date,
    decision_at: datetime,
    non_standard: str = "false",
    warning_trues: set[str] | None = None,
    unknown_codes: set[str] | None = None,
) -> list[LayerTwoFinancialNegativeEvidence]:
    hits = warning_trues or set()
    unknowns = unknown_codes or set()
    rows: list[LayerTwoFinancialNegativeEvidence] = []
    for code in (NON_STANDARD_AUDIT_RULE, *WARNING_RULE_CODES):
        if code == NON_STANDARD_AUDIT_RULE:
            state = non_standard
        elif code in unknowns:
            state = "unknown"
        else:
            state = "true" if code in hits else "false"
        rows.append(
            LayerTwoFinancialNegativeEvidence.model_validate(
                {
                    "symbol": symbol,
                    "rule_code": code,
                    "hit_state": state,
                    "observation_as_of": as_of,
                    "report_period": date(2022, 12, 31),
                    "available_at": decision_at - timedelta(hours=1),
                    "source": "synthetic-pit",
                    "evidence_id": f"ev-{symbol}-{code}",
                }
            )
        )
    return rows


def _financial(
    symbol: str,
    *,
    as_of: date,
    decision_at: datetime,
    snapshot_id: str,
    non_standard: str = "false",
    warning_trues: set[str] | None = None,
    unknown_codes: set[str] | None = None,
):
    return evaluate_layer_two_financial_negative_list(
        symbol=symbol,
        decision_at=decision_at,
        data_snapshot_id=snapshot_id,
        evidences=_financial_evidence(
            symbol,
            as_of=as_of,
            decision_at=decision_at,
            non_standard=non_standard,
            warning_trues=warning_trues,
            unknown_codes=unknown_codes,
        ),
        repo_root=REPO_ROOT,
    )


class _Bundle:
    def __init__(
        self,
        *,
        equity: float = 80_000.0,
        risk_budget: float = 0.3,
        n_symbols: int = 4,
        anchor_index: int = 0,
    ) -> None:
        calendar, as_of, decision_at, store, symbols = _complete_fixture(n_symbols=n_symbols)
        self.calendar = calendar
        self.as_of = as_of
        self.decision_at = decision_at
        self.store = store
        self.symbols = symbols
        self.market_snap = store.snapshot().snapshot_id
        self.equity = equity
        self.risk_budget = risk_budget
        self.slot = plan_base_slots(current_account_equity=equity, risk_budget=risk_budget)
        self.phase = plan_layer_two_tranche_phase_schedule(
            market_calendar=calendar,
            start=calendar[0],
            end=calendar[-1],
            anchor=calendar[anchor_index],
            current_account_equity=equity,
            risk_budget=risk_budget,
            market_data_snapshot_id=self.market_snap,
        )


def _eligibility(
    bundle: _Bundle,
    *,
    symbols: list[str] | None = None,
    caps: dict[str, float] | None = None,
    st_symbols: set[str] | None = None,
    planned: float | None = None,
):
    use_symbols = symbols or bundle.symbols
    st_symbols = st_symbols or set()
    caps = caps or {}
    buy = planned if planned is not None else float(bundle.slot.base_slot_notional or 8_000.0)
    candidates = [
        _candidate(
            symbol,
            as_of=bundle.as_of,
            decision_at=bundle.decision_at,
            planned=buy,
            cap_cny=caps.get(symbol, 12_000_000_000.0),
            eligible=symbol not in st_symbols,
        )
        for symbol in use_symbols
    ]
    return evaluate_layer_two_candidate_eligibility(
        as_of=bundle.as_of,
        decision_at=bundle.decision_at,
        data_snapshot_id=bundle.market_snap,
        candidates=candidates,
        repo_root=REPO_ROOT,
    )


def _cluster(bundle: _Bundle, symbols: list[str]):
    return diagnose_layer_two_statistical_risk_clusters(
        bundle.store,
        bundle.as_of,
        bundle.decision_at,
        symbols,
        REPO_ROOT,
    )


def _financials_for(
    bundle: _Bundle,
    eligible: list[str],
    *,
    fin_modes: dict[str, str] | None = None,
):
    fin_modes = fin_modes or {}
    financials = []
    for index, symbol in enumerate(eligible):
        mode = fin_modes.get(symbol, "clean")
        snap = f"{index:02d}" + ("a1" * 31)
        if mode == "hard":
            financials.append(
                _financial(
                    symbol,
                    as_of=bundle.as_of,
                    decision_at=bundle.decision_at,
                    snapshot_id=snap,
                    non_standard="true",
                )
            )
        elif mode == "half":
            financials.append(
                _financial(
                    symbol,
                    as_of=bundle.as_of,
                    decision_at=bundle.decision_at,
                    snapshot_id=snap,
                    warning_trues={WARNING_RULE_CODES[0]},
                )
            )
        elif mode == "unknown":
            financials.append(
                _financial(
                    symbol,
                    as_of=bundle.as_of,
                    decision_at=bundle.decision_at,
                    snapshot_id=snap,
                    unknown_codes={WARNING_RULE_CODES[0]},
                )
            )
        else:
            financials.append(
                _financial(
                    symbol,
                    as_of=bundle.as_of,
                    decision_at=bundle.decision_at,
                    snapshot_id=snap,
                )
            )
    return financials


def _assemble(
    bundle: _Bundle,
    *,
    eligibility=None,
    financials=None,
    cluster=None,
    phase=None,
    caps: dict[str, float] | None = None,
    fin_modes: dict[str, str] | None = None,
):
    eligibility = eligibility or _eligibility(bundle, caps=caps)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    cluster = cluster or _cluster(bundle, eligible)
    financials = financials if financials is not None else _financials_for(bundle, eligible, fin_modes=fin_modes)
    return assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=phase or bundle.phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )


def test_happy_path_multiple_symbols_with_phase_opportunity() -> None:
    bundle = _Bundle(anchor_index=0)
    eligibility = _eligibility(bundle, caps={bundle.symbols[0]: 4_000_000_000.0})
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    financials = _financials_for(bundle, eligible, fin_modes={bundle.symbols[1]: "half"})
    cluster = _cluster(bundle, eligible)
    report = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=bundle.phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )
    assert report.ready_for_stateful_allocator_input is True
    assert report.ready_for_portfolio_construction is False
    assert report.does_not_rank_or_select_stocks is True
    assert report.cluster_is_not_industry_classification is True
    assert len(report.rows) == 4
    by_symbol = {row.symbol: row for row in report.rows}
    assert by_symbol[bundle.symbols[0]].size_multiplier == 0.5
    assert by_symbol[bundle.symbols[0]].final_target_notional == 4_000.0
    assert by_symbol[bundle.symbols[1]].financial_decision_status == "halved"
    assert by_symbol[bundle.symbols[1]].final_target_notional == 4_000.0
    assert by_symbol[bundle.symbols[2]].final_target_notional == 8_000.0
    assert report.as_of_has_selected_phase_opportunity is True
    assert report.selected_phase_opportunity is not None
    assert report.selected_phase_opportunity.decision_date == bundle.as_of
    structural = verify_layer_two_constraint_assembler_report(
        report,
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=bundle.phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )
    assert structural.structural_ok is True


def test_as_of_without_selected_phase_opportunity() -> None:
    # Prepend one weekday so as_of lands on phase 1 (not in N=3 offsets {0,13,26}).
    from datetime import UTC

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
    assert phase_calendar.index(as_of) % 40 == 1
    equity = 80_000.0
    risk = 0.3
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
    report = _assemble(bundle)
    assert report.as_of_has_selected_phase_opportunity is False
    assert report.selected_phase_opportunity is None
    assert report.ready_for_stateful_allocator_input is True


def test_ineligible_and_unknown_excluded_from_rows() -> None:
    bundle = _Bundle()
    st_symbol = bundle.symbols[0]
    eligibility = _eligibility(bundle, st_symbols={st_symbol})
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    assert st_symbol not in eligible
    report = _assemble(bundle, eligibility=eligibility)
    assert st_symbol not in report.eligible_symbols
    assert all(row.symbol != st_symbol for row in report.rows)


def test_financial_clean_half_hard_unknown() -> None:
    bundle = _Bundle()
    symbols = bundle.symbols
    report = _assemble(
        bundle,
        fin_modes={
            symbols[0]: "clean",
            symbols[1]: "half",
            symbols[2]: "hard",
            symbols[3]: "unknown",
        },
    )
    by_symbol = {row.symbol: row for row in report.rows}
    assert by_symbol[symbols[0]].final_target_notional == 8_000.0
    assert by_symbol[symbols[1]].final_target_notional == 4_000.0
    assert by_symbol[symbols[2]].hard_excluded is True
    assert by_symbol[symbols[2]].target_for_later_allocator is None
    assert by_symbol[symbols[3]].financial_multiplier == "unknown"
    assert by_symbol[symbols[3]].retain_cash is True


def test_missing_extra_duplicate_financial_rejected() -> None:
    bundle = _Bundle()
    eligibility = _eligibility(bundle)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    cluster = _cluster(bundle, eligible)
    financials = _financials_for(bundle, eligible[:-1])
    with pytest.raises(ValueError, match="1:1 map"):
        assemble_layer_two_constraints(
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            phase_report=bundle.phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
        )
    financials_extra = _financials_for(bundle, eligible) + [
        _financial(
            "600000.SH",
            as_of=bundle.as_of,
            decision_at=bundle.decision_at,
            snapshot_id="zz" * 32,
        )
    ]
    with pytest.raises(ValueError, match="1:1 map"):
        assemble_layer_two_constraints(
            eligibility_report=eligibility,
            financial_reports=financials_extra,
            cluster_report=cluster,
            phase_report=bundle.phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
        )
    dup = [
        _financial(eligible[0], as_of=bundle.as_of, decision_at=bundle.decision_at, snapshot_id="11" * 32),
        _financial(eligible[0], as_of=bundle.as_of, decision_at=bundle.decision_at, snapshot_id="22" * 32),
    ] + _financials_for(bundle, eligible[1:])
    with pytest.raises(ValueError, match="duplicate financial"):
        assemble_layer_two_constraints(
            eligibility_report=eligibility,
            financial_reports=dup,
            cluster_report=cluster,
            phase_report=bundle.phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
        )


def test_snapshot_and_decision_drift_rejected() -> None:
    bundle = _Bundle()
    eligibility = _eligibility(bundle)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    cluster = _cluster(bundle, eligible)
    financials = _financials_for(bundle, eligible)
    bad_phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=bundle.calendar,
        start=bundle.calendar[0],
        end=bundle.calendar[-1],
        anchor=bundle.calendar[0],
        current_account_equity=bundle.equity,
        risk_budget=bundle.risk_budget,
        market_data_snapshot_id="ee" * 32,
    )
    with pytest.raises(ValueError, match="market_data_snapshot_id"):
        assemble_layer_two_constraints(
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            phase_report=bad_phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
        )
    next_day = bundle.as_of + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    bad_decision = datetime(next_day.year, next_day.month, next_day.day, 16, 0, tzinfo=bundle.decision_at.tzinfo)
    bad_fin = _financial(
        eligible[0],
        as_of=next_day,
        decision_at=bad_decision,
        snapshot_id="ff" * 32,
    )
    with pytest.raises(ValueError, match="as_of/decision_at"):
        assemble_layer_two_constraints(
            eligibility_report=eligibility,
            financial_reports=[bad_fin] + _financials_for(bundle, eligible[1:]),
            cluster_report=cluster,
            phase_report=bundle.phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
        )


def test_planned_amount_must_match_base_slot() -> None:
    bundle = _Bundle()
    with pytest.raises(ValueError, match="planned_buy_notional_cny"):
        _assemble(bundle, eligibility=_eligibility(bundle, planned=1.0))


def test_cluster_candidate_set_mismatch_rejected() -> None:
    bundle = _Bundle()
    eligibility = _eligibility(bundle)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    cluster = _cluster(bundle, eligible[:-1])
    financials = _financials_for(bundle, eligible)
    with pytest.raises(ValueError, match="cluster.candidates"):
        assemble_layer_two_constraints(
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            phase_report=bundle.phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
        )


def test_incomplete_cluster_fail_closed_not_usable() -> None:
    from datetime import UTC

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
    candidates = [
        _candidate(
            symbol,
            as_of=short_as_of,
            decision_at=short_decision,
            planned=float(slot.base_slot_notional or 8_000.0),
        )
        for symbol in symbols
    ]
    eligibility = evaluate_layer_two_candidate_eligibility(
        as_of=short_as_of,
        decision_at=short_decision,
        data_snapshot_id=market_snap,
        candidates=candidates,
        repo_root=REPO_ROOT,
    )
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    cluster = diagnose_layer_two_statistical_risk_clusters(
        short_store,
        short_as_of,
        short_decision,
        eligible,
        REPO_ROOT,
    )
    assert cluster.ready_for_cluster_constraints is False
    financials = [
        _financial(
            symbol,
            as_of=short_as_of,
            decision_at=short_decision,
            snapshot_id=f"{index:02d}" + ("b2" * 31),
        )
        for index, symbol in enumerate(eligible)
    ]
    report = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=phase,
        store=short_store,
        repo_root=REPO_ROOT,
    )
    assert report.cluster_constraints_complete is False
    assert report.ready_for_stateful_allocator_input is False
    assert report.rows
    assert all(row.usable_for_later_allocator is False for row in report.rows)
    assert all(row.target_for_later_allocator is None for row in report.rows)
    assert report.portfolio_cash_retention_reason == "cluster_report_unknown_or_incomplete"


def test_single_name_over_cluster_cap_not_clipped() -> None:
    # N=1 → base_slot_notional == sleeve > 0.35*sleeve, so single-name exceeds cluster cap.
    from datetime import UTC

    from tests.test_layer_two_statistical_risk_clusters import (
        LOOKBACK,
        PRICE_POINTS,
        _prices_from_returns,
        _store,
        _varying_returns,
        weekdays,
    )

    calendar = weekdays(date(2023, 1, 3), PRICE_POINTS)
    as_of = calendar[-1]
    decision_at = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC)
    symbols = ["000001.SZ", "000002.SH"]
    group_a = _varying_returns(LOOKBACK, seed=1)
    group_b = _varying_returns(LOOKBACK, seed=9)
    store = _store(
        calendar,
        {
            symbols[0]: _prices_from_returns(group_a),
            symbols[1]: _prices_from_returns(group_b),
        },
    )
    market_snap = store.snapshot().snapshot_id
    equity = 16_000.0
    risk = 0.9  # sleeve=14400 → N=1, base=14400 > 0.35*14400
    slot = plan_base_slots(current_account_equity=equity, risk_budget=risk)
    assert slot.base_slot_count == 1
    assert slot.base_slot_notional == 14_400.0
    phase = plan_layer_two_tranche_phase_schedule(
        market_calendar=calendar,
        start=calendar[0],
        end=calendar[-1],
        anchor=calendar[0],
        current_account_equity=equity,
        risk_budget=risk,
        market_data_snapshot_id=market_snap,
    )
    bundle = _Bundle.__new__(_Bundle)
    bundle.calendar = calendar
    bundle.as_of = as_of
    bundle.decision_at = decision_at
    bundle.store = store
    bundle.symbols = symbols
    bundle.market_snap = market_snap
    bundle.equity = equity
    bundle.risk_budget = risk
    bundle.slot = slot
    bundle.phase = phase
    report = _assemble(bundle)
    assert report.rows
    for row in report.rows:
        assert row.final_target_notional == 14_400.0
        assert row.cluster_single_name_admissible is False
        assert row.target_for_later_allocator is None
        assert row.cash_retention_reason == "cluster_single_name_exceeds_cap"
        assert row.usable_for_later_allocator is False


def test_zero_tranche_empty_rows() -> None:
    bundle = _Bundle(risk_budget=0.0)
    report = _assemble(bundle)
    assert report.active_tranche_count == 0
    assert report.rows == []
    assert report.portfolio_cash_retention_reason == "zero_risk_budget"
    assert report.ready_for_stateful_allocator_input is True


def test_tamper_without_and_with_reseal_rejected() -> None:
    bundle = _Bundle()
    eligibility = _eligibility(bundle)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    cluster = _cluster(bundle, eligible)
    financials = _financials_for(bundle, eligible)
    report = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=bundle.phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )
    tampered = report.model_copy(update={"market_data_snapshot_id": "00" * 32})
    with pytest.raises(ValueError, match="report_id"):
        verify_layer_two_constraint_assembler_report(
            tampered,
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            phase_report=bundle.phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
        )
    payload = report.model_dump(mode="json")
    payload["rows"][0]["final_target_notional"] = 1.0
    payload.pop("report_id", None)
    drifted = seal_layer_two_constraint_assembler_report(LayerTwoConstraintAssemblerReport.model_validate(payload))
    assert drifted.report_id == compute_report_id(drifted)
    with pytest.raises(ValueError, match="does not match full recompute"):
        verify_layer_two_constraint_assembler_report(
            drifted,
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            phase_report=bundle.phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
        )


def test_ready_injection_rejected() -> None:
    bundle = _Bundle()
    report = _assemble(bundle)
    payload = report.model_dump(mode="json")
    for flag in (
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_portfolio_construction",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
    ):
        bad = json.loads(json.dumps(payload))
        bad[flag] = True
        bad.pop("report_id", None)
        with pytest.raises(ValidationError):
            LayerTwoConstraintAssemblerReport.model_validate(bad)


def test_file_verifier_requires_matching_phase_on_disk(tmp_path: Path) -> None:
    bundle = _Bundle()
    eligibility = _eligibility(bundle)
    eligible = [e.symbol for e in eligibility.evaluations if e.eligible_for_new_entry]
    cluster = _cluster(bundle, eligible)
    financials = _financials_for(bundle, eligible)
    report = assemble_layer_two_constraints(
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=bundle.phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
    )
    missing = tmp_path / "missing-phase.json"
    with pytest.raises(ValueError, match="phase report file missing"):
        verify_layer_two_constraint_assembler_report_file(
            report=report,
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
    assert other_phase.report_id != bundle.phase.report_id
    other_path = tmp_path / "other-phase.json"
    write_layer_two_tranche_phase_schedule_report(other_path, other_phase)
    with pytest.raises(ValueError, match="phase report file id does not match"):
        verify_layer_two_constraint_assembler_report_file(
            report=report,
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
        verify_layer_two_constraint_assembler_report_file(
            report=report,
            eligibility_report=eligibility,
            financial_reports=financials,
            cluster_report=cluster,
            phase_report=bundle.phase,
            store=bundle.store,
            repo_root=REPO_ROOT,
            phase_report_path=phase_path,
        )

    write_layer_two_tranche_phase_schedule_report(phase_path, bundle.phase)
    result = verify_layer_two_constraint_assembler_report_file(
        report=report,
        eligibility_report=eligibility,
        financial_reports=financials,
        cluster_report=cluster,
        phase_report=bundle.phase,
        store=bundle.store,
        repo_root=REPO_ROOT,
        phase_report_path=phase_path,
    )
    assert result.allocation_protocol_binding_ok is True
    assert result.phase_binding_ok is True
    assert result.eligibility_binding_ok is True
    assert result.financial_binding_ok is True
    assert result.cluster_binding_ok is True
    out = tmp_path / "assembler.json"
    write_layer_two_constraint_assembler_report(out, report)
    assert out.is_file()


def test_bool_nan_inf_rejected_on_report_fields() -> None:
    bundle = _Bundle()
    report = _assemble(bundle)
    payload = report.model_dump(mode="json")
    payload["current_account_equity"] = True
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoConstraintAssemblerReport.model_validate(payload)
    payload = report.model_dump(mode="json")
    payload["risk_budget"] = float("nan")
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoConstraintAssemblerReport.model_validate(payload)
    payload = report.model_dump(mode="json")
    payload["sleeve_budget"] = float("inf")
    payload.pop("report_id", None)
    with pytest.raises(ValidationError):
        LayerTwoConstraintAssemblerReport.model_validate(payload)
