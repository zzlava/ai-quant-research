"""Strict unit tests for layer-one regime / risk-budget state machine (E9a).

Synthetic calendars and sealed feature reports only — never opens market data.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.research.index_risk_features import (
    IndexRiskFeatureReport,
    seal_index_risk_feature_report,
)
from app.research.layer_one_regime import (
    BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_ID,
    BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    LayerOneRegimePriorState,
    LayerOneUnlockRequest,
    assert_decision_self_hash,
    bind_upstream_contracts,
    compute_account_drawdown_decimal,
    compute_decision_id,
    compute_market_calendar_id,
    evaluate_layer_one_regime,
    map_account_drawdown_cap,
    map_index_drawdown_cap,
    map_trend_regime,
    map_volatility_cap,
    resolve_decision_timing,
    seal_layer_one_regime_decision,
    seal_prior_state,
    verify_layer_one_regime_decision_file,
    write_layer_one_regime_decision,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EQUITY_EVIDENCE_ID = "a" * 64
CEILING_AUTH_ID = "b" * 64


def _aware(year: int, month: int, day: int, hour: int = 8, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _trading_days(start: date, count: int) -> list[date]:
    """Weekday-only synthetic calendar (tests may splice holiday gaps separately)."""
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _sealed_feature_report(
    *,
    as_of: date,
    calendar_tail: list[date],
    close_to_sma_ratio: float = 1.05,
    realized_volatility_annualized: float = 0.15,
    drawdown: float = -0.05,
    report_id_tamper: str | None = None,
    trend_lookback: int = 200,
    vol_lookback: int = 60,
    dd_lookback: int = 242,
    mutate: dict[str, object] | None = None,
) -> IndexRiskFeatureReport:
    assert len(calendar_tail) >= dd_lookback
    assert calendar_tail[-1] <= as_of
    trend_window = calendar_tail[-trend_lookback:]
    vol_window = calendar_tail[-(vol_lookback + 1) :]
    dd_window = calendar_tail[-dd_lookback:]
    payload: dict[str, object] = {
        "data_snapshot_id": "9dbc0032539be62518bbc7f64e67cf9deb64e0564dcaca8aecc65bdc1d3890d0",
        "index_symbol": "000985.CSI",
        "as_of": as_of,
        "trend_lookback_bars": trend_lookback,
        "volatility_lookback_bars": vol_lookback,
        "drawdown_lookback_bars": dd_lookback,
        "trend_window_dates": trend_window,
        "volatility_price_window_dates": vol_window,
        "drawdown_window_dates": dd_window,
        "observation_count_trend": trend_lookback,
        "observation_count_volatility_returns": vol_lookback,
        "observation_count_drawdown": dd_lookback,
        "latest_close": 100.0,
        "simple_moving_average": 100.0 / close_to_sma_ratio,
        "close_to_sma_ratio": close_to_sma_ratio,
        "realized_volatility_annualized": realized_volatility_annualized,
        "rolling_peak": 100.0 / (1.0 + drawdown) if drawdown > -1.0 else 200.0,
        "drawdown": drawdown,
    }
    if mutate:
        payload.update(mutate)
    report = IndexRiskFeatureReport.model_validate(payload)
    sealed = seal_index_risk_feature_report(report)
    if report_id_tamper is not None:
        return sealed.model_copy(update={"report_id": report_id_tamper})
    return sealed


def _unlocked_prior(*, budget: float = 0.6) -> LayerOneRegimePriorState:
    return seal_prior_state(
        LayerOneRegimePriorState(
            applied_stock_budget=budget,
            risk_lock_active=False,
            risk_lock_triggered_as_of=None,
            red_line_breached=False,
        )
    )


def _locked_prior(*, triggered_as_of: date) -> LayerOneRegimePriorState:
    return seal_prior_state(
        LayerOneRegimePriorState(
            applied_stock_budget=0.0,
            risk_lock_active=True,
            risk_lock_triggered_as_of=triggered_as_of,
            red_line_breached=False,
        )
    )


def _base_calendar_for_as_of(as_of: date, *, extra_after: int = 5) -> list[date]:
    """Continuous weekday calendar ending at as_of, plus following sessions."""
    lead: list[date] = []
    cursor = as_of
    while len(lead) < 320:
        if cursor.weekday() < 5:
            lead.append(cursor)
        cursor -= timedelta(days=1)
    lead = sorted(lead)
    assert lead[-1] == as_of
    after = _trading_days(as_of + timedelta(days=1), extra_after)
    return lead + [day for day in after if day not in lead]


def _evaluate(
    *,
    target: date,
    as_of: date,
    calendar: list[date],
    prior_state: LayerOneRegimePriorState | None,
    peak: float = 100_000.0,
    current: float = 99_000.0,
    ceiling: float = 0.9,
    close_to_sma_ratio: float = 1.05,
    realized_volatility_annualized: float = 0.10,
    drawdown: float = -0.02,
    unlock_request: LayerOneUnlockRequest | None = None,
    evaluated_at: datetime | None = None,
    equity_evidence_id: str = EQUITY_EVIDENCE_ID,
    ceiling_auth_id: str = CEILING_AUTH_ID,
    index_risk_report: IndexRiskFeatureReport | None = None,
):
    report = index_risk_report or _sealed_feature_report(
        as_of=as_of,
        calendar_tail=[day for day in calendar if day <= as_of],
        close_to_sma_ratio=close_to_sma_ratio,
        realized_volatility_annualized=realized_volatility_annualized,
        drawdown=drawdown,
    )
    return evaluate_layer_one_regime(
        target_trading_day=target,
        market_calendar=calendar,
        index_risk_report=report,
        account_peak_equity=peak,
        account_current_equity=current,
        account_equity_evidence_id=equity_evidence_id,
        manual_open_ceiling=ceiling,
        manual_ceiling_authorization_id=ceiling_auth_id,
        prior_state=prior_state,
        evaluated_at=evaluated_at or _aware(target.year, target.month, target.day, 8, 0),
        unlock_request=unlock_request,
        repo_root=REPO_ROOT,
    )


@pytest.mark.parametrize(
    ("ratio", "regime", "base"),
    [
        (1.0300001, "positive", 0.9),
        (1.03, "neutral", 0.6),
        (1.0, "neutral", 0.6),
        (0.97, "neutral", 0.6),
        (0.9699999, "negative", 0.3),
    ],
)
def test_trend_threshold_boundaries(ratio: float, regime: str, base: float) -> None:
    got_regime, got_base = map_trend_regime(ratio)
    assert got_regime == regime
    assert got_base == base


@pytest.mark.parametrize(
    ("vol", "cap"),
    [
        (0.18, 0.9),
        (0.1800001, 0.6),
        (0.27, 0.6),
        (0.2700001, 0.3),
        (0.36, 0.3),
        (0.3600001, 0.0),
    ],
)
def test_volatility_cap_boundaries(vol: float, cap: float) -> None:
    assert map_volatility_cap(vol) == cap


@pytest.mark.parametrize(
    ("dd", "cap"),
    [
        (-0.0999999, 0.9),
        (-0.10, 0.6),
        (-0.1499999, 0.6),
        (-0.15, 0.3),
        (-0.1999999, 0.3),
        (-0.20, 0.0),
        (-0.25, 0.0),
    ],
)
def test_index_drawdown_cap_ordered_endpoints(dd: float, cap: float) -> None:
    assert map_index_drawdown_cap(dd) == cap


@pytest.mark.parametrize(
    ("dd", "cap", "lock", "red"),
    [
        (-0.0999999, 0.9, False, False),
        (-0.10, 0.6, False, False),
        (-0.1499999, 0.6, False, False),
        (-0.15, 0.3, False, False),
        (-0.1799999, 0.3, False, False),
        (-0.18, 0.0, True, False),
        (-0.1999999, 0.0, True, False),
        (-0.20, 0.0, True, True),
        (-0.25, 0.0, True, True),
    ],
)
def test_account_drawdown_lock_and_red_line(dd: float, cap: float, lock: bool, red: bool) -> None:
    got_cap, got_lock, got_red = map_account_drawdown_cap(dd)
    assert got_cap == cap
    assert got_lock is lock
    assert got_red is red


@pytest.mark.parametrize(
    ("current", "cap", "lock", "red"),
    [
        (90_000.01, 0.9, False, False),  # just above -10%
        (90_000.00, 0.6, False, False),  # exactly -10%
        (85_000.01, 0.6, False, False),  # just above -15%
        (85_000.00, 0.3, False, False),  # exactly -15%
        (82_000.01, 0.3, False, False),  # just above -18%
        (82_000.00, 0.0, True, False),  # exactly -18%
        (81_999.99, 0.0, True, False),  # just below -18%
        (80_000.01, 0.0, True, False),  # just above -20%
        (80_000.00, 0.0, True, True),  # exactly -20%
        (79_999.99, 0.0, True, True),  # just below -20%
    ],
)
def test_account_drawdown_decimal_endpoints_within_one_cent(current: float, cap: float, lock: bool, red: bool) -> None:
    dd = compute_account_drawdown_decimal(peak_equity=100_000.0, current_equity=current)
    got_cap, got_lock, got_red = map_account_drawdown_cap(dd)
    assert got_cap == cap
    assert got_lock is lock
    assert got_red is red


def test_holiday_week_first_trading_day_is_tuesday() -> None:
    # Monday holiday; Tuesday is first market day of that ISO week.
    tuesday = date(2024, 2, 13)
    wednesday = date(2024, 2, 14)
    prior_friday = date(2024, 2, 9)
    calendar = [prior_friday, tuesday, wednesday]
    p, is_first, _ = resolve_decision_timing(target_trading_day=tuesday, market_calendar=calendar)
    assert p == prior_friday
    assert is_first is True
    _p2, is_first2, _ = resolve_decision_timing(target_trading_day=wednesday, market_calendar=calendar)
    assert is_first2 is False
    assert date(2024, 2, 12) not in calendar


def test_midweek_increase_deferred_daily_decrease_ok() -> None:
    as_of = date(2024, 3, 5)  # Tuesday
    target = date(2024, 3, 6)  # Wednesday
    calendar = _base_calendar_for_as_of(as_of, extra_after=3)
    assert target in calendar
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.3),
        current=98_000.0,
    )
    assert decision.raw_target_budget == 0.9
    assert decision.increase_deferred is True
    assert decision.applied_stock_budget == 0.3
    assert decision.increase_deferred_reason == "increase_only_on_first_market_trading_day_of_week"
    assert decision.ready_for_trading is False
    assert decision.ready_for_orders is False
    assert decision.does_not_trade is True
    assert decision.exact_symbol_identity_verified is True
    assert decision.snapshot_full_raw_recomputation_verified is True
    assert decision.ready_for_historical_evaluation is True
    assert decision.layer_one_index_data_evidence_id == BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_ID
    assert decision.market_calendar_id == compute_market_calendar_id(calendar)
    assert decision.account_equity_evidence_id == EQUITY_EVIDENCE_ID
    assert decision.manual_ceiling_authorization_id == CEILING_AUTH_ID

    decision_down = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.9),
        current=98_000.0,
        close_to_sma_ratio=0.95,
    )
    assert decision_down.trend_base_budget == 0.3
    assert decision_down.raw_target_budget == 0.3
    assert decision_down.increase_deferred is False
    assert decision_down.applied_stock_budget == 0.3


def test_week_start_increase_applied() -> None:
    as_of = date(2024, 3, 1)  # Friday
    target = date(2024, 3, 4)  # Monday first of week
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.3),
    )
    assert decision.target_day_is_first_market_trading_day_of_week is True
    assert decision.applied_stock_budget == 0.9
    assert decision.increase_deferred is False


def test_manual_ceiling_caps_and_never_auto_raised() -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.3),
        ceiling=0.3,
    )
    assert decision.manual_open_ceiling == 0.3
    assert decision.raw_target_budget == 0.3
    assert decision.applied_stock_budget == 0.3
    with pytest.raises(ValueError, match="manual_open_ceiling"):
        _evaluate(
            target=target,
            as_of=as_of,
            calendar=calendar,
            prior_state=_unlocked_prior(budget=0.3),
            ceiling=0.45,
        )


def test_risk_lock_trigger_red_line_and_priority_over_weekly_increase() -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.9),
        current=80_000.0,
    )
    assert decision.risk_lock_triggered_this_decision is True
    assert decision.risk_lock_new_active is True
    assert decision.applied_stock_budget == 0.0
    assert decision.red_line_breached is True
    assert decision.new_state.risk_lock_triggered_as_of == as_of
    assert decision.increase_deferred is False
    assert decision.target_day_is_first_market_trading_day_of_week is True


def test_lock_persists_without_request_and_cooling_count() -> None:
    lock_as_of = date(2024, 1, 5)
    as_of = date(2024, 1, 19)
    target = date(2024, 1, 22)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    assert lock_as_of in calendar
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_locked_prior(triggered_as_of=lock_as_of),
    )
    assert decision.risk_lock_new_active is True
    assert decision.risk_lock_unlocked_this_decision is False
    assert "no_explicit_unlock_request" in decision.unlock_rejection_reasons
    assert decision.applied_stock_budget == 0.0
    assert decision.risk_lock_cooling_trading_days < 20


def test_unlock_rejected_when_conditions_fail() -> None:
    lock_as_of = date(2024, 1, 2)
    as_of = date(2024, 2, 20)
    target = date(2024, 2, 21)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    request = LayerOneUnlockRequest(
        request_id="unlock-1",
        operator="tester",
        reason="try unlock",
        requested_at=_aware(2024, 2, 20, 16, 0),
        user_confirmed=True,
    )
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_locked_prior(triggered_as_of=lock_as_of),
        unlock_request=request,
        close_to_sma_ratio=0.95,
        evaluated_at=_aware(2024, 2, 21, 8, 0),
    )
    assert decision.risk_lock_new_active is True
    assert "trend_regime_negative" in decision.unlock_rejection_reasons

    decision_vol = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_locked_prior(triggered_as_of=lock_as_of),
        unlock_request=request,
        close_to_sma_ratio=1.0,
        realized_volatility_annualized=0.27,
        evaluated_at=_aware(2024, 2, 21, 8, 0),
    )
    assert decision_vol.risk_lock_new_active is True
    assert any("realized_vol_not_strictly_below" in reason for reason in decision_vol.unlock_rejection_reasons)

    future_request = request.model_copy(update={"requested_at": _aware(2024, 2, 22, 9, 0)})
    decision_future = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_locked_prior(triggered_as_of=lock_as_of),
        unlock_request=future_request,
        close_to_sma_ratio=1.0,
        evaluated_at=_aware(2024, 2, 21, 8, 0),
    )
    assert "unlock_requested_at_in_future_vs_target_trading_day" in decision_future.unlock_rejection_reasons


def test_explicit_unlock_success_still_subject_to_weekly_and_ceiling() -> None:
    lock_as_of = date(2024, 1, 2)
    as_of = date(2024, 2, 20)
    target = date(2024, 2, 21)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    request = LayerOneUnlockRequest(
        request_id="unlock-ok",
        operator="operator-a",
        reason="cooling complete and confirmed",
        requested_at=_aware(2024, 2, 20, 16, 0),
        user_confirmed=True,
    )
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_locked_prior(triggered_as_of=lock_as_of),
        unlock_request=request,
        ceiling=0.6,
        evaluated_at=_aware(2024, 2, 21, 8, 0),
    )
    assert decision.risk_lock_unlocked_this_decision is True
    assert decision.risk_lock_new_active is False
    assert decision.unlock_rejection_reasons == []
    assert decision.raw_target_budget == 0.6
    assert decision.increase_deferred is True
    assert decision.applied_stock_budget == 0.0
    assert decision.manual_open_ceiling == 0.6
    assert decision.unlock_request_evidence_id is not None
    assert len(decision.unlock_request_evidence_id) == 64


def test_missing_prior_state_fail_closed() -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    with pytest.raises(ValueError, match="prior_state is required"):
        _evaluate(
            target=target,
            as_of=as_of,
            calendar=calendar,
            prior_state=None,
        )


def test_current_equity_above_peak_fail_closed() -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    with pytest.raises(ValueError, match="historical peak"):
        _evaluate(
            target=target,
            as_of=as_of,
            calendar=calendar,
            prior_state=_unlocked_prior(),
            current=100_000.01,
        )


def test_feature_report_hash_tamper_rejected() -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    tampered = _sealed_feature_report(
        as_of=as_of,
        calendar_tail=[day for day in calendar if day <= as_of],
        report_id_tamper="0" * 64,
    )
    with pytest.raises(ValueError, match="report_id"):
        _evaluate(
            target=target,
            as_of=as_of,
            calendar=calendar,
            prior_state=_unlocked_prior(),
            index_risk_report=tampered,
        )


def test_pd_not_adjacent_fail_closed() -> None:
    # Feature report as_of is Friday, but D=Tuesday ⇒ calendar P is Monday ≠ report as_of.
    as_of = date(2024, 3, 1)  # Friday
    target = date(2024, 3, 5)  # Tuesday
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    p, _, _ = resolve_decision_timing(target_trading_day=target, market_calendar=calendar)
    assert p == date(2024, 3, 4)
    assert p != as_of
    with pytest.raises(ValueError, match="as_of"):
        _evaluate(
            target=target,
            as_of=as_of,
            calendar=calendar,
            prior_state=_unlocked_prior(),
        )


def test_future_window_in_feature_report_rejected() -> None:
    as_of = date(2024, 3, 1)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    report = _sealed_feature_report(as_of=as_of, calendar_tail=[day for day in calendar if day <= as_of])
    leaked = list(report.trend_window_dates)
    leaked[-1] = date(2024, 3, 4)
    with pytest.raises(ValueError):
        IndexRiskFeatureReport.model_validate(
            {
                **report.model_dump(mode="python"),
                "report_id": None,
                "trend_window_dates": leaked,
            }
        )


def test_upstream_binding_ids() -> None:
    contract_id, _, protocol_id, _ = bind_upstream_contracts(repo_root=REPO_ROOT)
    assert contract_id == BOUND_TWO_LAYER_DECISION_CONTRACT_ID
    assert protocol_id == BOUND_LAYER_ONE_INDEX_PROTOCOL_ID


def test_verified_index_data_binding_rejects_snapshot_and_symbol_drift() -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    base = _sealed_feature_report(
        as_of=as_of,
        calendar_tail=[day for day in calendar if day <= as_of],
    )
    wrong_snapshot = seal_index_risk_feature_report(
        base.model_copy(update={"report_id": None, "data_snapshot_id": "wrong-snapshot"})
    )
    with pytest.raises(ValueError, match="data_snapshot_id does not match"):
        _evaluate(
            target=target,
            as_of=as_of,
            calendar=calendar,
            prior_state=_unlocked_prior(),
            index_risk_report=wrong_snapshot,
        )

    wrong_symbol = seal_index_risk_feature_report(
        base.model_copy(update={"report_id": None, "index_symbol": "000300.SH"})
    )
    with pytest.raises(ValueError, match="symbol does not match"):
        _evaluate(
            target=target,
            as_of=as_of,
            calendar=calendar,
            prior_state=_unlocked_prior(),
            index_risk_report=wrong_symbol,
        )


def test_upstream_drift_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing"):
        bind_upstream_contracts(repo_root=tmp_path)


def test_output_self_hash_and_file_verifier(tmp_path: Path) -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.9),
    )
    assert_decision_self_hash(decision)
    assert decision.two_layer_decision_contract_id == BOUND_TWO_LAYER_DECISION_CONTRACT_ID
    assert decision.layer_one_index_protocol_id == BOUND_LAYER_ONE_INDEX_PROTOCOL_ID
    out = tmp_path / "decision.json"
    write_layer_one_regime_decision(decision, out)
    verified = verify_layer_one_regime_decision_file(out, repo_root=REPO_ROOT)
    assert verified.decision_id == decision.decision_id

    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["decision_id"] = "1" * 64
    out.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="decision_id"):
        verify_layer_one_regime_decision_file(out, repo_root=REPO_ROOT)


def test_nan_inf_inputs_fail_closed() -> None:
    with pytest.raises(ValueError):
        map_trend_regime(float("nan"))
    with pytest.raises(ValueError):
        map_volatility_cap(float("inf"))
    with pytest.raises(ValueError):
        map_index_drawdown_cap(float("-inf"))


def test_wrong_lookbacks_rejected() -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    bad = _sealed_feature_report(
        as_of=as_of,
        calendar_tail=[day for day in calendar if day <= as_of],
        trend_lookback=199,
        vol_lookback=60,
        dd_lookback=242,
    )
    with pytest.raises(ValueError, match="trend_lookback"):
        _evaluate(
            target=target,
            as_of=as_of,
            calendar=calendar,
            prior_state=_unlocked_prior(),
            index_risk_report=bad,
        )


def test_attack_unsealed_prior_rejected() -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    unsealed = LayerOneRegimePriorState(
        applied_stock_budget=0.6,
        risk_lock_active=False,
        risk_lock_triggered_as_of=None,
        red_line_breached=False,
        state_id=None,
    )
    with pytest.raises(ValueError, match="prior_state.state_id is required"):
        _evaluate(target=target, as_of=as_of, calendar=calendar, prior_state=unsealed)


def test_attack_stale_prior_hash_rejected() -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    sealed = _unlocked_prior(budget=0.6)
    stale = sealed.model_copy(update={"applied_stock_budget": 0.9})
    with pytest.raises(ValueError, match="prior_state.state_id does not match"):
        _evaluate(target=target, as_of=as_of, calendar=calendar, prior_state=stale)


def test_attack_current_lock_trigger_blocks_unlock() -> None:
    lock_as_of = date(2024, 1, 2)
    as_of = date(2024, 2, 20)
    target = date(2024, 2, 21)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    request = LayerOneUnlockRequest(
        request_id="unlock-while-still-locked-obs",
        operator="operator-a",
        reason="attempt unlock while account still at risk-lock threshold",
        requested_at=_aware(2024, 2, 20, 16, 0),
        user_confirmed=True,
    )
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_locked_prior(triggered_as_of=lock_as_of),
        unlock_request=request,
        current=82_000.0,  # exactly -18% still triggers
        evaluated_at=_aware(2024, 2, 21, 8, 0),
    )
    assert decision.risk_lock_new_active is True
    assert decision.risk_lock_unlocked_this_decision is False
    assert "current_observation_triggers_risk_lock" in decision.unlock_rejection_reasons
    assert decision.risk_lock_triggered_as_of == lock_as_of


def test_attack_unlock_requested_at_early_late_and_naive() -> None:
    lock_as_of = date(2024, 1, 2)
    as_of = date(2024, 2, 20)
    target = date(2024, 2, 21)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    prior = _locked_prior(triggered_as_of=lock_as_of)
    evaluated_at = _aware(2024, 2, 21, 8, 0)

    early = LayerOneUnlockRequest(
        request_id="early",
        operator="op",
        reason="before lock date",
        requested_at=_aware(2024, 1, 1, 12, 0),
        user_confirmed=True,
    )
    early_decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=prior,
        unlock_request=early,
        evaluated_at=evaluated_at,
    )
    assert "unlock_requested_at_before_risk_lock_triggered_as_of" in early_decision.unlock_rejection_reasons

    late = LayerOneUnlockRequest(
        request_id="late",
        operator="op",
        reason="after evaluated_at",
        requested_at=_aware(2024, 2, 21, 9, 0),
        user_confirmed=True,
    )
    late_decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=prior,
        unlock_request=late,
        evaluated_at=evaluated_at,
    )
    assert "unlock_requested_at_after_evaluated_at" in late_decision.unlock_rejection_reasons

    with pytest.raises(ValueError, match="timezone-aware"):
        LayerOneUnlockRequest(
            request_id="naive",
            operator="op",
            reason="naive datetime",
            requested_at=datetime(2024, 2, 20, 16, 0, 0),
            user_confirmed=True,
        )

    with pytest.raises(ValueError, match="timezone-aware"):
        _evaluate(
            target=target,
            as_of=as_of,
            calendar=calendar,
            prior_state=_unlocked_prior(),
            evaluated_at=datetime(2024, 2, 21, 8, 0, 0),
        )


def test_attack_weekend_lock_date_rejected() -> None:
    as_of = date(2024, 2, 20)  # Tuesday
    target = date(2024, 2, 21)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    weekend = date(2024, 1, 6)  # Saturday
    assert weekend not in calendar
    with pytest.raises(ValueError, match="risk_lock_triggered_as_of must appear in market_calendar"):
        _evaluate(
            target=target,
            as_of=as_of,
            calendar=calendar,
            prior_state=_locked_prior(triggered_as_of=weekend),
            evaluated_at=_aware(2024, 2, 21, 8, 0),
        )


def test_attack_evidence_id_missing_or_bad_format() -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    prior = _unlocked_prior()
    with pytest.raises(ValueError, match="account_equity_evidence_id"):
        _evaluate(
            target=target,
            as_of=as_of,
            calendar=calendar,
            prior_state=prior,
            equity_evidence_id="not-hex",
        )
    with pytest.raises(ValueError, match="manual_ceiling_authorization_id"):
        _evaluate(
            target=target,
            as_of=as_of,
            calendar=calendar,
            prior_state=prior,
            ceiling_auth_id="A" * 64,  # uppercase rejected
        )
    with pytest.raises(TypeError):
        evaluate_layer_one_regime(  # type: ignore[call-arg]
            target_trading_day=target,
            market_calendar=calendar,
            index_risk_report=_sealed_feature_report(
                as_of=as_of,
                calendar_tail=[day for day in calendar if day <= as_of],
            ),
            account_peak_equity=100_000.0,
            account_current_equity=99_000.0,
            manual_open_ceiling=0.9,
            prior_state=prior,
            evaluated_at=_aware(2024, 3, 4, 8, 0),
            repo_root=REPO_ROOT,
        )


def test_attack_calendar_hash_and_date_tamper_rejected_by_verifier(tmp_path: Path) -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.9),
    )
    out = tmp_path / "decision.json"
    write_layer_one_regime_decision(decision, out)

    # Tamper calendar_id while keeping dates, then reseal decision_id.
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["market_calendar_id"] = "c" * 64
    tampered = seal_layer_one_regime_decision(type(decision).model_validate({**payload, "decision_id": None}))
    out.write_text(
        json.dumps(tampered.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="market_calendar_id"):
        verify_layer_one_regime_decision_file(out, repo_root=REPO_ROOT)

    # Tamper as_of date away from adjacent P, reseal.
    payload2 = json.loads(json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))
    payload2["as_of"] = date(2024, 2, 29).isoformat()  # Thursday; not adjacent prior of Monday
    payload2["decision_id"] = None
    resealed_date = seal_layer_one_regime_decision(type(decision).model_validate(payload2))
    out.write_text(
        json.dumps(resealed_date.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="as_of"):
        verify_layer_one_regime_decision_file(out, repo_root=REPO_ROOT)


def test_attack_logic_tamper_then_reseal_rejected_by_verifier(tmp_path: Path) -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.3),
    )
    assert decision.applied_stock_budget == 0.9
    payload = decision.model_dump(mode="json")
    payload["applied_stock_budget"] = 0.3
    payload["increase_deferred"] = True
    payload["increase_deferred_reason"] = "increase_only_on_first_market_trading_day_of_week"
    payload["decision_id"] = None
    # Keep new_state consistent with the forged applied budget so only weekly logic is wrong.
    payload["new_state"] = {
        "applied_stock_budget": 0.3,
        "risk_lock_active": False,
        "risk_lock_triggered_as_of": None,
        "red_line_breached": False,
        "state_id": None,
    }
    forged = type(decision).model_validate(payload)
    from app.research.layer_one_regime import seal_layer_one_regime_state

    forged = forged.model_copy(update={"new_state": seal_layer_one_regime_state(forged.new_state), "decision_id": None})
    resealed = seal_layer_one_regime_decision(forged)
    assert resealed.decision_id == compute_decision_id(resealed)
    out = tmp_path / "forged.json"
    write_layer_one_regime_decision(resealed, out)
    with pytest.raises(ValueError, match="applied_stock_budget|increase_deferred"):
        verify_layer_one_regime_decision_file(out, repo_root=REPO_ROOT)


def test_attack_top_level_feature_scalars_unbind_from_embedded_report(tmp_path: Path) -> None:
    """P1: forge top-level scalars + derived budgets while keeping embedded feature report."""
    from app.research.layer_one_regime import seal_layer_one_regime_state

    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.9),
        close_to_sma_ratio=1.05,
    )
    assert decision.close_to_sma_ratio == 1.05
    assert decision.trend_regime == "positive"
    assert decision.index_risk_feature_report.close_to_sma_ratio == 1.05
    original_feature_id = decision.index_risk_feature_report_id

    # Forge negative trend consistently at the decision surface; leave embedded report intact.
    payload = decision.model_dump(mode="json")
    payload["close_to_sma_ratio"] = 0.96
    payload["trend_regime"] = "negative"
    payload["trend_base_budget"] = 0.3
    payload["raw_target_budget"] = 0.3
    payload["applied_stock_budget"] = 0.3
    payload["increase_deferred"] = False
    payload["increase_deferred_reason"] = None
    payload["index_risk_feature_report_id"] = original_feature_id
    payload["decision_id"] = None
    payload["new_state"] = {
        "applied_stock_budget": 0.3,
        "risk_lock_active": False,
        "risk_lock_triggered_as_of": None,
        "red_line_breached": False,
        "state_id": None,
    }
    forged = type(decision).model_validate(payload)
    forged = forged.model_copy(update={"new_state": seal_layer_one_regime_state(forged.new_state), "decision_id": None})
    resealed = seal_layer_one_regime_decision(forged)
    assert resealed.index_risk_feature_report_id == original_feature_id
    assert resealed.index_risk_feature_report.close_to_sma_ratio == 1.05
    out = tmp_path / "unbind-scalars.json"
    write_layer_one_regime_decision(resealed, out)
    with pytest.raises(ValueError, match="close_to_sma_ratio does not match embedded"):
        verify_layer_one_regime_decision_file(out, repo_root=REPO_ROOT)


def test_attack_embedded_feature_content_without_resealing_report_id(tmp_path: Path) -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.9),
    )
    payload = decision.model_dump(mode="json")
    embedded = payload["index_risk_feature_report"]
    embedded["close_to_sma_ratio"] = 0.96
    embedded["simple_moving_average"] = 100.0 / 0.96
    # Intentionally keep stale report_id so self-hash must fail.
    payload["close_to_sma_ratio"] = 0.96
    payload["trend_regime"] = "negative"
    payload["trend_base_budget"] = 0.3
    payload["raw_target_budget"] = 0.3
    payload["applied_stock_budget"] = 0.3
    payload["decision_id"] = None
    forged = type(decision).model_validate(payload)
    resealed = seal_layer_one_regime_decision(forged)
    out = tmp_path / "stale-feature-hash.json"
    write_layer_one_regime_decision(resealed, out)
    with pytest.raises(ValueError, match="report_id"):
        verify_layer_one_regime_decision_file(out, repo_root=REPO_ROOT)


def test_attack_calendar_drops_feature_window_day_even_with_resealed_ids(tmp_path: Path) -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.9),
    )
    # Drop a mid-window day that is neither P nor D, so adjacency still holds.
    drop_day = decision.index_risk_feature_report.trend_window_dates[50]
    assert drop_day not in {as_of, target}
    trimmed = [day for day in decision.market_calendar if day != drop_day]
    payload = decision.model_dump(mode="json")
    payload["market_calendar"] = [day.isoformat() for day in trimmed]
    payload["market_calendar_id"] = compute_market_calendar_id(trimmed)
    payload["decision_id"] = None
    forged = type(decision).model_validate(payload)
    resealed = seal_layer_one_regime_decision(forged)
    out = tmp_path / "calendar-drop-window.json"
    write_layer_one_regime_decision(resealed, out)
    with pytest.raises(ValueError, match="absent from market_calendar"):
        verify_layer_one_regime_decision_file(out, repo_root=REPO_ROOT)


def test_attack_resealed_embedded_feature_report_id_mismatch(tmp_path: Path) -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.9),
        close_to_sma_ratio=1.05,
    )
    original_feature_id = decision.index_risk_feature_report_id
    mutated = seal_index_risk_feature_report(
        decision.index_risk_feature_report.model_copy(
            update={
                "report_id": None,
                "close_to_sma_ratio": 0.96,
                "simple_moving_average": 100.0 / 0.96,
            }
        )
    )
    assert mutated.report_id is not None
    assert mutated.report_id != original_feature_id

    from app.research.layer_one_regime import seal_layer_one_regime_state

    payload = decision.model_dump(mode="json")
    payload["index_risk_feature_report"] = mutated.model_dump(mode="json")
    # Keep stale outer id while embedding the resealed report and aligning top-level scalars.
    payload["index_risk_feature_report_id"] = original_feature_id
    payload["close_to_sma_ratio"] = 0.96
    payload["trend_regime"] = "negative"
    payload["trend_base_budget"] = 0.3
    payload["raw_target_budget"] = 0.3
    payload["applied_stock_budget"] = 0.3
    payload["increase_deferred"] = False
    payload["increase_deferred_reason"] = None
    payload["decision_id"] = None
    payload["new_state"] = {
        "applied_stock_budget": 0.3,
        "risk_lock_active": False,
        "risk_lock_triggered_as_of": None,
        "red_line_breached": False,
        "state_id": None,
    }
    forged = type(decision).model_validate(payload)
    forged = forged.model_copy(update={"new_state": seal_layer_one_regime_state(forged.new_state), "decision_id": None})
    resealed = seal_layer_one_regime_decision(forged)
    out = tmp_path / "feature-id-mismatch.json"
    write_layer_one_regime_decision(resealed, out)
    with pytest.raises(ValueError, match="index_risk_feature_report_id does not match embedded"):
        verify_layer_one_regime_decision_file(out, repo_root=REPO_ROOT)


def test_evaluate_embeds_sealed_feature_report() -> None:
    as_of = date(2024, 3, 1)
    target = date(2024, 3, 4)
    calendar = _base_calendar_for_as_of(as_of, extra_after=5)
    feature = _sealed_feature_report(
        as_of=as_of,
        calendar_tail=[day for day in calendar if day <= as_of],
        close_to_sma_ratio=1.05,
    )
    decision = _evaluate(
        target=target,
        as_of=as_of,
        calendar=calendar,
        prior_state=_unlocked_prior(budget=0.9),
        index_risk_report=feature,
    )
    assert decision.index_risk_feature_report.model_dump(mode="json") == feature.model_dump(mode="json")
    assert decision.index_risk_feature_report_id == feature.report_id
    assert decision.data_snapshot_id == feature.data_snapshot_id
    assert decision.index_symbol_input == feature.index_symbol
    assert decision.close_to_sma_ratio == feature.close_to_sma_ratio
    assert decision.realized_volatility_annualized == feature.realized_volatility_annualized
    assert decision.index_drawdown == feature.drawdown
