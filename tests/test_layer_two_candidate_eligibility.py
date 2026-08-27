"""Strict unit tests for layer-two PIT candidate eligibility (E10a).

Synthetic explicit inputs only — never opens market data or mutates frozen JSON.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.research.layer_two_candidate_eligibility import (
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
    ELIGIBLE_REASON_CODE,
    LayerTwoCandidateEligibilityReport,
    LayerTwoCandidateInput,
    LayerTwoLiquidityObservation,
    assert_report_self_hash,
    bind_two_layer_eligibility_policy,
    compute_report_id,
    evaluate_layer_two_candidate_eligibility,
    seal_layer_two_candidate_eligibility_report,
    verify_layer_two_candidate_eligibility_report,
    verify_layer_two_candidate_eligibility_report_file,
    write_layer_two_candidate_eligibility_report,
)
from app.research.two_layer_contract import load_two_layer_decision_draft

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_CONTRACT = REPO_ROOT / BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
SNAP_ID = "snap-e10a-synthetic"
DECISION_AT = datetime(2024, 6, 28, 16, 0, tzinfo=UTC)
AS_OF = date(2024, 6, 28)
PLANNED_BUY = 50_000.0
SECURITY_STATUS_AT = AS_OF
SECURITY_STATUS_AVAILABLE_AT = datetime(2024, 6, 28, 15, 0, tzinfo=UTC)
CAP_AT = datetime(2024, 6, 28, 15, 30, tzinfo=UTC)
CAP_AS_OF = AS_OF


def _observation_days(end: date, count: int = 20) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def _liquidity_observations(
    *,
    end: date = AS_OF,
    tradable_count: int = 20,
    amount: float = 60_000_000.0,
    count: int = 20,
    mutate_index: int | None = None,
    mutate: dict[str, object] | None = None,
) -> list[LayerTwoLiquidityObservation]:
    days = _observation_days(end, count)
    slots: list[LayerTwoLiquidityObservation] = []
    for index, obs_date in enumerate(days):
        payload: dict[str, object] = {
            "observation_date": obs_date,
            "available_at": DECISION_AT - timedelta(minutes=30),
        }
        if index >= tradable_count:
            payload["tradability"] = "known_full_day_suspension"
            payload["amount_cny"] = 0.0
        else:
            payload["tradability"] = "tradable"
            payload["amount_cny"] = amount
        if mutate_index == index and mutate:
            payload.update(mutate)
        slots.append(LayerTwoLiquidityObservation.model_validate(payload))
    return slots


def _candidate(
    symbol: str = "000001.SZ",
    *,
    market: str | None = "SZSE",
    ordinary: bool | None = True,
    bse: bool | None = False,
    st: bool | None = False,
    suspended: bool | None = False,
    listed_days: int | None = 200,
    planned: float = PLANNED_BUY,
    cap_cny: float | None = 12_000_000_000.0,
    cap_as_of: date | None = CAP_AS_OF,
    cap_at: datetime | None = CAP_AT,
    security_as_of: date | None = SECURITY_STATUS_AT,
    security_at: datetime | None = SECURITY_STATUS_AVAILABLE_AT,
    liquidity: list[LayerTwoLiquidityObservation] | None = None,
) -> LayerTwoCandidateInput:
    return LayerTwoCandidateInput.model_validate(
        {
            "symbol": symbol,
            "market": market,
            "is_ordinary_a_share": ordinary,
            "is_bse": bse,
            "is_st_or_delist_risk": st,
            "is_suspended_on_decision_date": suspended,
            "listed_market_trading_days": listed_days,
            "security_status_as_of": security_as_of,
            "security_status_available_at": security_at,
            "planned_buy_notional_cny": planned,
            "liquidity_observations": liquidity or _liquidity_observations(),
            "pit_free_float_market_cap_cny": cap_cny,
            "pit_free_float_market_cap_as_of": cap_as_of,
            "pit_free_float_market_cap_available_at": cap_at,
        }
    )


def _evaluate(*candidates: LayerTwoCandidateInput):
    return evaluate_layer_two_candidate_eligibility(
        as_of=AS_OF,
        decision_at=DECISION_AT,
        data_snapshot_id=SNAP_ID,
        candidates=list(candidates),
        repo_root=REPO_ROOT,
    )


def test_bind_contract_matches_frozen_id() -> None:
    contract_id, path, policy = bind_two_layer_eligibility_policy(repo_root=REPO_ROOT)
    assert contract_id == BOUND_TWO_LAYER_DECISION_CONTRACT_ID
    assert path == "config/research/two-layer-strategy-decision-draft-v1.json"
    assert policy.universe.min_listed_market_trading_days == 180
    assert policy.liquidity.median_daily_amount_min_cny == 50_000_000


def test_happy_path_eligible() -> None:
    report = _evaluate(_candidate("000001.SZ"), _candidate("600000.SH", market="SSE"))
    assert report.requested_symbols == ["000001.SZ", "600000.SH"]
    for evaluation in report.evaluations:
        assert evaluation.eligible_for_new_entry is True
        assert evaluation.reason_codes == [ELIGIBLE_REASON_CODE]
        assert evaluation.ownership_role == "diagnostic_not_used"
        assert evaluation.size_multiplier == 1.0
        assert evaluation.adjusted_planned_notional_cny == PLANNED_BUY
    assert report.ready_for_scoring is False
    assert report.ready_for_portfolio_construction is False
    assert report.does_not_trade is True


def test_empty_input_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        evaluate_layer_two_candidate_eligibility(
            as_of=AS_OF,
            decision_at=DECISION_AT,
            data_snapshot_id=SNAP_ID,
            candidates=[],
            repo_root=REPO_ROOT,
        )


def test_duplicate_symbols_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate symbol"):
        _evaluate(_candidate("000001.SZ"), _candidate("000001.SZ"))


def test_decision_at_date_must_equal_as_of() -> None:
    with pytest.raises(ValueError, match="decision_at calendar date"):
        evaluate_layer_two_candidate_eligibility(
            as_of=AS_OF,
            decision_at=datetime(2024, 6, 27, 16, 0, tzinfo=UTC),
            data_snapshot_id=SNAP_ID,
            candidates=[_candidate()],
            repo_root=REPO_ROOT,
        )


def test_output_sorted_and_self_hash(tmp_path: Path) -> None:
    report = _evaluate(
        _candidate("600000.SH", market="SSE"),
        _candidate("000001.SZ", market="SZSE"),
    )
    assert report.candidate_inputs[0].symbol == "000001.SZ"
    assert report.requested_symbols == ["000001.SZ", "600000.SH"]
    assert report.report_id == compute_report_id(report)
    assert_report_self_hash(report)
    out = tmp_path / "report.json"
    write_layer_two_candidate_eligibility_report(report, out)
    loaded = verify_layer_two_candidate_eligibility_report_file(out, repo_root=REPO_ROOT)
    assert loaded.report_id == report.report_id


@pytest.mark.parametrize(
    ("listed_days", "eligible"),
    [
        (180, True),
        (179, False),
    ],
)
def test_listing_history_boundary(listed_days: int, eligible: bool) -> None:
    report = _evaluate(_candidate(listed_days=listed_days))
    evaluation = report.evaluations[0]
    assert evaluation.eligible_for_new_entry is eligible
    if not eligible:
        assert "listing_history_fail" in evaluation.reason_codes


@pytest.mark.parametrize(
    ("tradable_count", "eligible"),
    [
        (15, True),
        (14, False),
    ],
)
def test_tradable_days_boundary(tradable_count: int, eligible: bool) -> None:
    amount = 67_000_000.0 if tradable_count == 15 else 60_000_000.0
    report = _evaluate(_candidate(liquidity=_liquidity_observations(tradable_count=tradable_count, amount=amount)))
    evaluation = report.evaluations[0]
    assert evaluation.eligible_for_new_entry is eligible
    assert evaluation.tradable_days_in_lookback == tradable_count
    if not eligible:
        assert "liquidity_tradable_days_fail" in evaluation.reason_codes


@pytest.mark.parametrize(
    ("amount", "eligible"),
    [
        (50_000_000.0, True),
        (49_999_999.99, False),
    ],
)
def test_median_amount_boundary(amount: float, eligible: bool) -> None:
    report = _evaluate(_candidate(liquidity=_liquidity_observations(amount=amount)))
    evaluation = report.evaluations[0]
    assert evaluation.eligible_for_new_entry is eligible
    if not eligible:
        assert "liquidity_median_amount_fail" in evaluation.reason_codes


@pytest.mark.parametrize(
    ("cap_cny", "multiplier", "eligible"),
    [
        (3_000_000_000.0, 0.5, True),
        (2_999_999_999.99, None, False),
        (5_000_000_000.0, 0.75, True),
        (10_000_000_000.0, 1.0, True),
    ],
)
def test_size_cap_boundaries(cap_cny: float, multiplier: float | None, eligible: bool) -> None:
    report = _evaluate(_candidate(cap_cny=cap_cny))
    evaluation = report.evaluations[0]
    assert evaluation.eligible_for_new_entry is eligible
    assert evaluation.size_multiplier == multiplier
    if multiplier is not None:
        assert evaluation.adjusted_planned_notional_cny == pytest.approx(PLANNED_BUY * multiplier)
    else:
        assert "size_cap_hard_exclude_fail" in evaluation.reason_codes


def test_capacity_boundary_exact_and_over() -> None:
    avg = 60_000_000.0
    limit = avg * 0.001
    ok = _evaluate(_candidate(planned=limit, liquidity=_liquidity_observations(amount=avg)))
    assert ok.evaluations[0].eligible_for_new_entry is True

    over = _evaluate(_candidate(planned=limit + 0.01, liquidity=_liquidity_observations(amount=avg)))
    evaluation = over.evaluations[0]
    assert evaluation.eligible_for_new_entry is False
    assert "liquidity_capacity_fail" in evaluation.reason_codes


@pytest.mark.parametrize(
    ("market", "ordinary", "bse", "st", "suspended", "reason"),
    [
        ("SSE", True, False, False, False, None),
        (None, True, False, False, False, "unknown_critical_input"),
        ("SZSE", None, False, False, False, "unknown_critical_input"),
        ("SZSE", True, True, False, False, "bse_forbidden"),
        ("SZSE", True, False, True, False, "st_or_delist_risk_fail"),
        ("SZSE", True, False, False, True, "suspended_on_decision_date_fail"),
    ],
)
def test_security_scope_failures(
    market: str | None,
    ordinary: bool | None,
    bse: bool | None,
    st: bool | None,
    suspended: bool | None,
    reason: str | None,
) -> None:
    report = _evaluate(
        _candidate(
            symbol="600000.SH" if market == "SSE" else "000001.SZ",
            market=market,
            ordinary=ordinary,
            bse=bse,
            st=st,
            suspended=suspended,
        )
    )
    evaluation = report.evaluations[0]
    if reason is None:
        assert evaluation.eligible_for_new_entry is True
    else:
        assert evaluation.eligible_for_new_entry is False
        assert reason in evaluation.reason_codes


def test_all_security_fields_and_metadata_absent_is_unknown_not_eligible() -> None:
    report = _evaluate(
        _candidate(
            market=None,
            ordinary=None,
            bse=None,
            st=None,
            suspended=None,
            listed_days=None,
            security_as_of=None,
            security_at=None,
        )
    )
    evaluation = report.evaluations[0]
    assert evaluation.eligible_for_new_entry is False
    assert evaluation.unknown_critical_input is True
    assert evaluation.reason_codes == ["unknown_critical_input"]
    assert evaluation.market_scope_pass is None
    assert evaluation.st_delist_pass is None
    assert evaluation.tradability_pass is None
    assert evaluation.listing_history_pass is None


def test_missing_security_provenance_with_known_fields_is_unknown() -> None:
    report = _evaluate(
        _candidate(
            security_as_of=None,
            security_at=None,
            st=True,
            suspended=True,
            listed_days=100,
        )
    )
    evaluation = report.evaluations[0]
    assert evaluation.unknown_critical_input is True
    assert evaluation.eligible_for_new_entry is False
    assert evaluation.market_scope_pass is None
    assert evaluation.st_delist_pass is None
    assert evaluation.tradability_pass is None
    assert evaluation.listing_history_pass is None
    assert "st_or_delist_risk_fail" not in evaluation.reason_codes
    assert "suspended_on_decision_date_fail" not in evaluation.reason_codes
    assert "listing_history_fail" not in evaluation.reason_codes
    assert "market_scope_fail" not in evaluation.reason_codes


def test_partial_security_field_unknown_with_complete_metadata() -> None:
    report = _evaluate(_candidate(market=None, st=False, suspended=False, listed_days=200))
    evaluation = report.evaluations[0]
    assert evaluation.unknown_critical_input is True
    assert evaluation.market_scope_pass is None
    assert evaluation.st_delist_pass is True
    assert evaluation.tradability_pass is True
    assert evaluation.listing_history_pass is True


def test_security_provenance_wrong_as_of_raises() -> None:
    with pytest.raises(ValueError, match="security_status_as_of"):
        _evaluate(_candidate(security_as_of=AS_OF - timedelta(days=1)))


def test_cap_provenance_half_pair_rejected() -> None:
    with pytest.raises(ValueError, match="pit_free_float_market_cap fields"):
        _evaluate(_candidate(cap_cny=12_000_000_000.0, cap_as_of=CAP_AS_OF, cap_at=None))


def test_known_suspension_zero_vs_unknown_missing() -> None:
    suspended_slot = _liquidity_observations(
        mutate_index=0,
        mutate={"tradability": "known_full_day_suspension", "amount_cny": 0.0},
    )
    ok = _evaluate(_candidate(liquidity=suspended_slot))
    assert ok.evaluations[0].liquidity_structure_pass is True

    unknown_tradability = _liquidity_observations(
        mutate_index=0,
        mutate={"tradability": None, "amount_cny": None},
    )
    unknown = _evaluate(_candidate(liquidity=unknown_tradability))
    evaluation = unknown.evaluations[0]
    assert evaluation.eligible_for_new_entry is False
    assert evaluation.unknown_critical_input is True
    assert evaluation.liquidity_structure_pass is None


def test_nineteen_slots_unknown_and_structure_fail() -> None:
    report = _evaluate(_candidate(liquidity=_liquidity_observations(count=19)))
    evaluation = report.evaluations[0]
    assert evaluation.unknown_critical_input is True
    assert "liquidity_observation_structure_fail" in evaluation.reason_codes


def test_twenty_one_slots_raises() -> None:
    with pytest.raises(ValueError, match="exceeds lookback"):
        _evaluate(_candidate(liquidity=_liquidity_observations(count=21)))


def test_unsorted_observation_dates_raise() -> None:
    days = _observation_days(AS_OF, 20)
    shuffled = [days[1], days[0], *days[2:]]
    bad_order = [
        LayerTwoLiquidityObservation(
            observation_date=d,
            tradability="tradable",
            amount_cny=60_000_000.0,
            available_at=DECISION_AT - timedelta(minutes=1),
        )
        for d in shuffled
    ]
    with pytest.raises(ValueError, match="strictly increasing"):
        _evaluate(_candidate(liquidity=bad_order))


def test_duplicate_observation_dates_raise() -> None:
    days = _observation_days(AS_OF, 20)
    dup = [
        LayerTwoLiquidityObservation(
            observation_date=days[0],
            tradability="tradable",
            amount_cny=60_000_000.0,
            available_at=DECISION_AT - timedelta(minutes=1),
        ),
        LayerTwoLiquidityObservation(
            observation_date=days[0],
            tradability="tradable",
            amount_cny=60_000_000.0,
            available_at=DECISION_AT - timedelta(minutes=1),
        ),
        *[
            LayerTwoLiquidityObservation(
                observation_date=d,
                tradability="tradable",
                amount_cny=60_000_000.0,
                available_at=DECISION_AT - timedelta(minutes=1),
            )
            for d in days[2:]
        ],
    ]
    assert len(dup) == 20
    with pytest.raises(ValueError, match="strictly increasing"):
        _evaluate(_candidate(liquidity=dup))


def test_future_observation_date_raises() -> None:
    future_day = AS_OF + timedelta(days=3)
    while future_day.weekday() >= 5:
        future_day += timedelta(days=1)
    slots = _liquidity_observations()
    slots[-1] = LayerTwoLiquidityObservation(
        observation_date=future_day,
        tradability="tradable",
        amount_cny=60_000_000.0,
        available_at=DECISION_AT - timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="after as_of"):
        _evaluate(_candidate(liquidity=slots))


def test_stale_t_minus_one_window_end_rejected() -> None:
    stale_end = AS_OF - timedelta(days=1)
    while stale_end.weekday() >= 5:
        stale_end -= timedelta(days=1)
    with pytest.raises(ValueError, match="must equal as_of"):
        _evaluate(_candidate(liquidity=_liquidity_observations(end=stale_end)))


def test_late_available_at_raises() -> None:
    slots = _liquidity_observations(
        mutate_index=0,
        mutate={"available_at": DECISION_AT + timedelta(minutes=1)},
    )
    with pytest.raises(ValueError, match="available_at"):
        _evaluate(_candidate(liquidity=slots))


def test_late_slot_after_earlier_unknown_raises_not_conceals() -> None:
    days = _observation_days(AS_OF, 20)
    slots = [
        LayerTwoLiquidityObservation(
            observation_date=days[0],
            tradability=None,
            amount_cny=None,
            available_at=DECISION_AT - timedelta(minutes=2),
        ),
        LayerTwoLiquidityObservation(
            observation_date=days[1],
            tradability="tradable",
            amount_cny=60_000_000.0,
            available_at=DECISION_AT + timedelta(minutes=1),
        ),
        *[
            LayerTwoLiquidityObservation(
                observation_date=d,
                tradability="tradable",
                amount_cny=60_000_000.0,
                available_at=DECISION_AT - timedelta(minutes=3),
            )
            for d in days[2:]
        ],
    ]
    with pytest.raises(ValueError, match="available_at"):
        _evaluate(_candidate(liquidity=slots))


def test_naive_decision_at_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_layer_two_candidate_eligibility(
            as_of=AS_OF,
            decision_at=datetime(2024, 6, 28, 16, 0),
            data_snapshot_id=SNAP_ID,
            candidates=[_candidate()],
            repo_root=REPO_ROOT,
        )


def test_naive_observation_available_at_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        LayerTwoLiquidityObservation.model_validate(
            {
                "observation_date": AS_OF,
                "tradability": "tradable",
                "amount_cny": 60_000_000.0,
                "available_at": datetime(2024, 6, 28, 15, 0),
            }
        )


@pytest.mark.parametrize("bad_amount", [float("nan"), float("inf"), -1.0])
def test_non_finite_or_negative_amount_raises(bad_amount: float) -> None:
    for tradability in ("tradable", None):
        slots = _liquidity_observations(
            mutate_index=0,
            mutate={"amount_cny": bad_amount, "tradability": tradability},
        )
        with pytest.raises(ValueError):
            _evaluate(_candidate(liquidity=slots))


def test_corrupt_amount_after_unknown_slot_raises() -> None:
    days = _observation_days(AS_OF, 20)
    slots = [
        LayerTwoLiquidityObservation(
            observation_date=days[0],
            tradability=None,
            amount_cny=None,
            available_at=DECISION_AT - timedelta(minutes=2),
        ),
        *[
            LayerTwoLiquidityObservation(
                observation_date=d,
                tradability="tradable",
                amount_cny=60_000_000.0,
                available_at=DECISION_AT - timedelta(minutes=3),
            )
            for d in days[1:-1]
        ],
        LayerTwoLiquidityObservation(
            observation_date=days[-1],
            tradability=None,
            amount_cny=float("nan"),
            available_at=DECISION_AT - timedelta(minutes=1),
        ),
    ]
    with pytest.raises(ValueError, match="finite"):
        _evaluate(_candidate(liquidity=slots))


def test_corrupt_amount_before_unknown_slot_raises() -> None:
    days = _observation_days(AS_OF, 20)
    slots = [
        LayerTwoLiquidityObservation(
            observation_date=days[0],
            tradability=None,
            amount_cny=-1.0,
            available_at=DECISION_AT - timedelta(minutes=2),
        ),
        *[
            LayerTwoLiquidityObservation(
                observation_date=d,
                tradability="tradable",
                amount_cny=60_000_000.0,
                available_at=DECISION_AT - timedelta(minutes=3),
            )
            for d in days[1:]
        ],
    ]
    with pytest.raises(ValueError, match="non-negative"):
        _evaluate(_candidate(liquidity=slots))


def test_known_suspension_nonzero_amount_raises() -> None:
    slots = _liquidity_observations(
        mutate_index=0,
        mutate={"tradability": "known_full_day_suspension", "amount_cny": 1.0},
    )
    with pytest.raises(ValueError, match="amount_cny=0"):
        _evaluate(_candidate(liquidity=slots))


@pytest.mark.parametrize(
    ("symbol", "market", "error_match"),
    [
        ("600000.SH", "SZSE", "suffix"),
        ("000001.SZ", "SSE", "suffix"),
        ("000001.sz", "SZSE", "uppercase"),
        (" 000001.SZ", "SZSE", "whitespace"),
        ("00001.SZ", "SZSE", "six digits"),
        ("000001.SS", "SZSE", "six digits"),
        ("430047.BJ", "SZSE", "six digits"),
    ],
)
def test_symbol_canonical_form_rejected(symbol: str, market: str, error_match: str) -> None:
    with pytest.raises(ValueError, match=error_match):
        _evaluate(_candidate(symbol=symbol, market=market))


def test_lowercase_alias_cannot_duplicate_canonical_symbol() -> None:
    with pytest.raises(ValueError, match="uppercase"):
        _evaluate(
            _candidate("000001.SZ"),
            _candidate("000001.sz", market="SZSE"),
        )


def test_ownership_field_rejected() -> None:
    payload = json.loads(_candidate().model_dump_json())
    payload["ownership_proxy"] = "ignored"
    with pytest.raises(ValidationError):
        LayerTwoCandidateInput.model_validate(payload)


def test_stale_hash_rejected() -> None:
    report = seal_layer_two_candidate_eligibility_report(_evaluate(_candidate()))
    tampered = report.model_copy(update={"report_id": "0" * 64})
    with pytest.raises(ValueError, match="report_id"):
        assert_report_self_hash(tampered)


def test_tampered_derived_flag_reseal_rejected() -> None:
    report = _evaluate(_candidate())
    evaluation = report.evaluations[0].model_copy(
        update={
            "eligible_for_new_entry": False,
            "reason_codes": ["liquidity_capacity_fail"],
            "liquidity_capacity_pass": False,
        }
    )
    tampered = report.model_copy(
        update={
            "evaluations": [evaluation],
            "report_id": None,
        }
    )
    resealed = seal_layer_two_candidate_eligibility_report(tampered)
    with pytest.raises(ValueError, match="does not recompute"):
        verify_layer_two_candidate_eligibility_report(resealed, repo_root=REPO_ROOT)


def test_wrong_report_contract_binding_rejected() -> None:
    report = _evaluate(_candidate())
    bad = report.model_copy(update={"two_layer_decision_contract_id": "f" * 64, "report_id": None})
    resealed = seal_layer_two_candidate_eligibility_report(bad)
    with pytest.raises(ValueError, match="contract_id"):
        verify_layer_two_candidate_eligibility_report(resealed, repo_root=REPO_ROOT)


def test_tampered_disk_contract_rejected(tmp_path: Path) -> None:
    tampered_dir = tmp_path / "config" / "research"
    tampered_dir.mkdir(parents=True)
    tampered_path = tampered_dir / "two-layer-strategy-decision-draft-v1.json"
    payload = json.loads(COMMITTED_CONTRACT.read_text(encoding="utf-8"))
    payload["confirmed"]["note"] = "tampered contract content"
    tampered_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    ledger_src = REPO_ROOT / payload["research_trial_ledger_path"]
    ledger_dest = tmp_path / payload["research_trial_ledger_path"]
    ledger_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ledger_src, ledger_dest)

    with pytest.raises(ValueError, match="contract_id"):
        bind_two_layer_eligibility_policy(repo_root=tmp_path, contract_path=tampered_path)


def test_ready_flags_cannot_be_true() -> None:
    payload = json.loads(_evaluate(_candidate()).model_dump_json())
    for flag in (
        "ready_for_scoring",
        "ready_for_portfolio_construction",
        "ready_for_orders",
        "ready_for_trading",
    ):
        bad = dict(payload)
        bad[flag] = True
        bad.pop("report_id", None)
        with pytest.raises(ValidationError):
            LayerTwoCandidateEligibilityReport.model_validate(bad)


def test_missing_cap_fields_fail_closed() -> None:
    report = _evaluate(
        _candidate(
            cap_cny=None,
            cap_as_of=None,
            cap_at=None,
        )
    )
    evaluation = report.evaluations[0]
    assert evaluation.eligible_for_new_entry is False
    assert evaluation.unknown_critical_input is True
    assert evaluation.size_cap_pass is None


def test_invalid_cap_value_raises() -> None:
    with pytest.raises(ValueError, match="finite"):
        _evaluate(_candidate(cap_cny=float("nan")))


def test_late_cap_available_at_raises() -> None:
    with pytest.raises(ValueError, match="pit_free_float_market_cap_available_at"):
        _evaluate(_candidate(cap_at=DECISION_AT + timedelta(hours=1)))


def test_reason_codes_ordered_on_multiple_failures() -> None:
    report = _evaluate(
        _candidate(
            market=None,
            st=True,
            suspended=True,
            listed_days=100,
            cap_cny=1_000_000_000.0,
        )
    )
    codes = report.evaluations[0].reason_codes
    assert codes.index("unknown_critical_input") < codes.index("st_or_delist_risk_fail")
    assert codes.index("st_or_delist_risk_fail") < codes.index("listing_history_fail")


def test_deterministic_hash_for_identical_inputs() -> None:
    first = _evaluate(_candidate("000001.SZ"))
    second = _evaluate(_candidate("000001.SZ"))
    assert first.report_id == second.report_id


def test_disk_contract_loads_without_mutation() -> None:
    draft = load_two_layer_decision_draft(COMMITTED_CONTRACT)
    assert draft.contract_id == BOUND_TWO_LAYER_DECISION_CONTRACT_ID
