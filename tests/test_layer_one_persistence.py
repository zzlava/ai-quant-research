"""Focused persistence/API tests for layer-one risk-state store (E9b-1).

Uses temporary SQLite only. Never opens market data, tokens, or brokers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.main import app
from app.persistence.db import init_db
from app.persistence.layer_one_models import (
    LayerOneAuditRecord,
    LayerOneCurrentState,
    LayerOneManualCeilingAuthorizationRow,
    LayerOneUnlockRequestRow,
)
from app.research.index_risk_features import (
    IndexRiskFeatureReport,
    seal_index_risk_feature_report,
)
from app.research.layer_one_persistence import (
    LAYER_ONE_STREAM_NAME,
    LayerOneConflictError,
    LayerOneDecisionCommitRequest,
    LayerOneDeploymentEvidenceRequest,
    LayerOneInitializeRequest,
    LayerOneIntegrityError,
    LayerOneManualCeilingAuthorizationRequest,
    LayerOneNotInitializedError,
    LayerOnePersistenceError,
    LayerOneRiskStateStore,
    LayerOneUnlockRequestSubmission,
    add_calendar_months,
    budget_to_bp,
)
from app.research.layer_one_regime import (
    BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    LayerOneRegimePriorState,
    LayerOneUnlockRequest,
    evaluate_layer_one_regime,
    seal_layer_one_regime_decision,
    seal_prior_state,
)
from tests.helpers import PROJECT_ROOT

EQUITY_EVIDENCE_ID = "c" * 64
CSI_INDEX_SNAPSHOT_ID = "9dbc0032539be62518bbc7f64e67cf9deb64e0564dcaca8aecc65bdc1d3890d0"


def _aware(year: int, month: int, day: int, hour: int = 8, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _trading_days(start: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _base_calendar_for_as_of(as_of: date, *, extra_after: int = 5) -> list[date]:
    lead: list[date] = []
    cursor = as_of
    while len(lead) < 320:
        if cursor.weekday() < 5:
            lead.append(cursor)
        cursor -= timedelta(days=1)
    lead = sorted(lead)
    after = _trading_days(as_of + timedelta(days=1), extra_after)
    return lead + [day for day in after if day not in lead]


def _sealed_feature_report(
    *,
    as_of: date,
    calendar: list[date],
    close_to_sma_ratio: float = 1.05,
    realized_volatility_annualized: float = 0.15,
    drawdown: float = -0.05,
) -> IndexRiskFeatureReport:
    calendar_tail = [day for day in calendar if day <= as_of]
    payload = {
        "data_snapshot_id": CSI_INDEX_SNAPSHOT_ID,
        "index_symbol": "000985.CSI",
        "as_of": as_of,
        "trend_lookback_bars": 200,
        "volatility_lookback_bars": 60,
        "drawdown_lookback_bars": 242,
        "trend_window_dates": calendar_tail[-200:],
        "volatility_price_window_dates": calendar_tail[-(60 + 1) :],
        "drawdown_window_dates": calendar_tail[-242:],
        "observation_count_trend": 200,
        "observation_count_volatility_returns": 60,
        "observation_count_drawdown": 242,
        "latest_close": 100.0,
        "simple_moving_average": 100.0 / close_to_sma_ratio,
        "close_to_sma_ratio": close_to_sma_ratio,
        "realized_volatility_annualized": realized_volatility_annualized,
        "rolling_peak": 100.0 / (1.0 + drawdown),
        "drawdown": drawdown,
    }
    return seal_index_risk_feature_report(IndexRiskFeatureReport.model_validate(payload))


@pytest.fixture
def store(tmp_path: Path) -> LayerOneRiskStateStore:
    eng = create_engine(f"sqlite:///{tmp_path / 'layer_one.db'}", future=True)
    init_db(eng)
    return LayerOneRiskStateStore(eng, repo_root=PROJECT_ROOT)


def _init(store: LayerOneRiskStateStore, *, at: datetime | None = None):
    return store.initialize(
        LayerOneInitializeRequest(
            operator="ops",
            reason="bootstrap fail-closed stream",
            initialized_at=at or _aware(2024, 1, 2),
            user_confirmed=True,
            two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
        )
    )


def _register_hist(store: LayerOneRiskStateStore, *, at: datetime) -> str:
    receipt = store.register_deployment_evidence(
        LayerOneDeploymentEvidenceRequest(
            evidence_type="historical_validation_pass",
            observed_from=date(2023, 1, 1),
            observed_through=date(2023, 12, 31),
            recorded_at=at,
            operator="ops",
            summary="historical validation passed",
            user_confirmed=True,
            two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
            historical_validation_pass=True,
        )
    )
    assert receipt.evidence_id is not None
    return receipt.evidence_id


def _register_anomaly(
    store: LayerOneRiskStateStore,
    *,
    at: datetime,
    observed_from: date,
    observed_through: date,
) -> str:
    receipt = store.register_deployment_evidence(
        LayerOneDeploymentEvidenceRequest(
            evidence_type="no_severe_anomaly_period",
            observed_from=observed_from,
            observed_through=observed_through,
            recorded_at=at,
            operator="ops",
            summary="no severe anomaly",
            user_confirmed=True,
            two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
            no_severe_anomaly=True,
        )
    )
    assert receipt.evidence_id is not None
    return receipt.evidence_id


def _authorize(
    store: LayerOneRiskStateStore,
    *,
    request_id: str,
    ceiling: float,
    at: datetime,
    hist: str | None = None,
    anomaly: str | None = None,
):
    return store.authorize_manual_ceiling(
        LayerOneManualCeilingAuthorizationRequest(
            request_id=request_id,
            ceiling=ceiling,
            authorized_at=at,
            operator="ops",
            reason=f"set ceiling {ceiling}",
            user_confirmed=True,
            two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
            historical_validation_evidence_id=hist,
            no_severe_anomaly_evidence_id=anomaly,
        )
    )


def _decision_report(
    store: LayerOneRiskStateStore,
    *,
    target: date,
    peak: float = 100_000.0,
    current: float = 99_000.0,
    unlock: LayerOneUnlockRequest | None = None,
    ceiling: float | None = None,
    auth_id: str | None = None,
    close_to_sma_ratio: float = 1.05,
    realized_volatility_annualized: float = 0.15,
    evaluated_at: datetime | None = None,
):
    state = store.get_risk_state()
    assert state.initialized
    assert state.state_id is not None
    as_of = target - timedelta(days=1)
    while as_of.weekday() >= 5:
        as_of -= timedelta(days=1)
    calendar = _base_calendar_for_as_of(as_of, extra_after=8)
    if target not in calendar:
        calendar = sorted(set(calendar) | {target})
    prior = seal_prior_state(
        LayerOneRegimePriorState(
            applied_stock_budget=state.applied_stock_budget,
            risk_lock_active=bool(state.risk_lock_active),
            risk_lock_triggered_as_of=state.risk_lock_triggered_as_of,
            red_line_breached=bool(state.red_line_breached),
            state_id=state.state_id,
        )
    )
    return evaluate_layer_one_regime(
        target_trading_day=target,
        market_calendar=calendar,
        index_risk_report=_sealed_feature_report(
            as_of=as_of,
            calendar=calendar,
            close_to_sma_ratio=close_to_sma_ratio,
            realized_volatility_annualized=realized_volatility_annualized,
        ),
        account_peak_equity=peak,
        account_current_equity=current,
        account_equity_evidence_id=EQUITY_EVIDENCE_ID,
        manual_open_ceiling=ceiling if ceiling is not None else float(state.manual_ceiling),
        manual_ceiling_authorization_id=auth_id or str(state.manual_ceiling_authorization_id),
        prior_state=prior,
        evaluated_at=evaluated_at or _aware(target.year, target.month, target.day, 8, 0),
        unlock_request=unlock,
        repo_root=PROJECT_ROOT,
    )


def _commit(store: LayerOneRiskStateStore, report):
    state = store.get_risk_state()
    assert state.last_audit_id is not None
    assert state.revision is not None
    return store.commit_decision(
        LayerOneDecisionCommitRequest(
            expected_last_audit_id=state.last_audit_id,
            expected_revision=state.revision,
            report=report,
        )
    )


def test_missing_state_is_explicit_fail_closed(store: LayerOneRiskStateStore) -> None:
    view = store.get_risk_state()
    assert view.initialized is False
    assert view.effective_stock_budget == 0.0
    assert view.risk_lock_active is None
    assert view.ready_for_trading is False
    with pytest.raises(LayerOneNotInitializedError):
        _authorize(store, request_id="a1", ceiling=0.0, at=_aware(2024, 1, 2))


def test_initialize_idempotent_after_progression(store: LayerOneRiskStateStore) -> None:
    req = LayerOneInitializeRequest(
        operator="ops",
        reason="bootstrap",
        initialized_at=_aware(2024, 1, 2),
        user_confirmed=True,
        two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
        layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
        data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
    )
    first = store.initialize(req)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    auth = _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    assert auth.idempotent_replay is False
    replay = store.initialize(req)
    assert replay.idempotent_replay is True
    assert replay.audit_id == first.audit_id
    assert replay.authorization_id == first.authorization_id
    assert replay.state_id == first.state_id
    assert replay.revision == 1
    assert replay.authorization_id != auth.authorization_id
    with pytest.raises(LayerOneConflictError):
        store.initialize(req.model_copy(update={"reason": "different"}))


def test_manual_authorization_exact_replay_independent_of_prior_ceiling(
    store: LayerOneRiskStateStore,
) -> None:
    _init(store)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    req = LayerOneManualCeilingAuthorizationRequest(
        request_id="ceil-30",
        ceiling=0.3,
        authorized_at=_aware(2024, 1, 3),
        operator="ops",
        reason="trial 30",
        user_confirmed=True,
        two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
        layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
        data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
        historical_validation_evidence_id=hist,
    )
    first = store.authorize_manual_ceiling(req)
    # Lower then try exact replay — must not create a new auth using recomputed prior_ceiling.
    _authorize(store, request_id="ceil-0", ceiling=0.0, at=_aware(2024, 1, 4))
    replay = store.authorize_manual_ceiling(req)
    assert replay.idempotent_replay is True
    assert replay.audit_id == first.audit_id
    assert replay.authorization_id == first.authorization_id
    assert replay.state_id == first.state_id
    assert store.get_risk_state().manual_ceiling == 0.0
    with pytest.raises(LayerOneConflictError):
        store.authorize_manual_ceiling(req.model_copy(update={"reason": "changed"}))


def test_unknown_and_wrong_type_evidence_fail_closed(store: LayerOneRiskStateStore) -> None:
    _init(store)
    with pytest.raises(LayerOnePersistenceError):
        _authorize(
            store,
            request_id="ceil-30",
            ceiling=0.3,
            at=_aware(2024, 1, 3),
            hist="a" * 64,
        )
    anomaly = _register_anomaly(
        store,
        at=_aware(2024, 1, 2, 9, 0),
        observed_from=date(2023, 1, 1),
        observed_through=date(2023, 12, 31),
    )
    with pytest.raises(LayerOnePersistenceError):
        _authorize(
            store,
            request_id="ceil-30-wrong",
            ceiling=0.3,
            at=_aware(2024, 1, 3),
            hist=anomaly,
        )


def test_restart_keeps_active_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "restart.db"
    eng1 = create_engine(f"sqlite:///{db_path}", future=True)
    init_db(eng1)
    store1 = LayerOneRiskStateStore(eng1, repo_root=PROJECT_ROOT)
    _init(store1)
    hist = _register_hist(store1, at=_aware(2024, 1, 2, 9, 0))
    _authorize(store1, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    decision = _decision_report(
        store1,
        target=date(2024, 1, 5),
        peak=100_000.0,
        current=81_000.0,
        ceiling=0.3,
        evaluated_at=_aware(2024, 1, 5, 10, 0),
    )
    assert decision.risk_lock_triggered_this_decision is True
    _commit(store1, decision)
    store2 = LayerOneRiskStateStore(create_engine(f"sqlite:///{db_path}", future=True), repo_root=PROJECT_ROOT)
    restarted = store2.get_risk_state()
    assert restarted.risk_lock_active is True
    assert restarted.effective_stock_budget == 0.0


def test_same_state_out_of_order_decision_rejected(store: LayerOneRiskStateStore) -> None:
    _init(store)
    # Ceiling stays 0; applied stays 0 → identical prior/new state_id across targets.
    early = _decision_report(store, target=date(2024, 1, 8), evaluated_at=_aware(2024, 1, 8, 8, 0))
    later = _decision_report(store, target=date(2024, 1, 9), evaluated_at=_aware(2024, 1, 9, 8, 0))
    assert early.prior_state_id == later.prior_state_id
    assert early.new_state.state_id == early.prior_state_id
    assert later.new_state.state_id == later.prior_state_id
    _commit(store, later)
    # Monotonic evaluated_at but earlier target — must still reject.
    early_late_eval = early.model_copy(update={"evaluated_at": _aware(2024, 1, 10, 8, 0), "decision_id": None})
    early_late_eval = seal_layer_one_regime_decision(early_late_eval)
    with pytest.raises(LayerOneConflictError):
        _commit(store, early_late_eval)


def test_decision_cas_envelope_and_exact_idempotency_after_progression(
    store: LayerOneRiskStateStore,
) -> None:
    _init(store)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    d_a = _decision_report(store, target=date(2024, 1, 8), ceiling=0.3, evaluated_at=_aware(2024, 1, 8, 8, 0))
    state = store.get_risk_state()
    stale_env = LayerOneDecisionCommitRequest(
        expected_last_audit_id=state.last_audit_id or "",
        expected_revision=(state.revision or 1),
        report=d_a,
    )
    store.register_deployment_evidence(
        LayerOneDeploymentEvidenceRequest(
            evidence_type="historical_validation_pass",
            observed_from=date(2022, 1, 1),
            observed_through=date(2022, 6, 30),
            recorded_at=_aware(2024, 1, 7, 12, 0),
            operator="ops",
            summary="other hist",
            user_confirmed=True,
            two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
            historical_validation_pass=True,
        )
    )
    with pytest.raises(LayerOneConflictError):
        store.commit_decision(stale_env)

    d_a2 = _decision_report(store, target=date(2024, 1, 8), ceiling=0.3, evaluated_at=_aware(2024, 1, 8, 13, 0))
    r1 = _commit(store, d_a2)
    assert r1.idempotent_replay is False
    r2 = _commit(store, d_a2)
    assert r2.idempotent_replay is True
    assert r2.state_id == d_a2.new_state.state_id
    assert r2.audit_id == r1.audit_id
    # Progress with a lock-triggering decision so current state_id diverges.
    d_lock = _decision_report(
        store,
        target=date(2024, 1, 15),
        peak=100_000.0,
        current=81_000.0,
        ceiling=0.3,
        evaluated_at=_aware(2024, 1, 15, 8, 0),
    )
    assert d_lock.new_state.state_id != d_a2.new_state.state_id
    _commit(store, d_lock)
    r3 = _commit(store, d_a2)
    assert r3.idempotent_replay is True
    assert r3.state_id == d_a2.new_state.state_id
    assert r3.state_id != store.get_risk_state().state_id


def test_ceiling_column_tamper_vs_payload_fail_closed(store: LayerOneRiskStateStore) -> None:
    _init(store)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    with Session(store.engine) as session:
        current = session.get(LayerOneCurrentState, LAYER_ONE_STREAM_NAME)
        assert current is not None
        auth = session.get(LayerOneManualCeilingAuthorizationRow, current.manual_ceiling_authorization_id)
        assert auth is not None
        current.manual_ceiling_bp = 9000
        auth.ceiling_bp = 9000
        session.add(current)
        session.add(auth)
        session.commit()
    with pytest.raises(LayerOneIntegrityError):
        store.get_risk_state()


def test_evidence_type_column_tamper_fail_closed(store: LayerOneRiskStateStore) -> None:
    _init(store)
    anomaly_id = _register_anomaly(
        store,
        at=_aware(2024, 1, 2, 9, 0),
        observed_from=date(2023, 1, 1),
        observed_through=date(2023, 12, 31),
    )
    from app.persistence.layer_one_models import LayerOneDeploymentEvidenceRow

    with Session(store.engine) as session:
        row = session.get(LayerOneDeploymentEvidenceRow, anomaly_id)
        assert row is not None
        row.evidence_type = "historical_validation_pass"
        row.historical_validation_pass = True
        row.no_severe_anomaly = None
        session.add(row)
        session.commit()
    with pytest.raises(LayerOneIntegrityError):
        store.get_risk_state()
    with pytest.raises(LayerOneIntegrityError):
        _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=anomaly_id)


def test_deleted_referenced_evidence_fail_closed(store: LayerOneRiskStateStore) -> None:
    _init(store)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    from app.persistence.layer_one_models import LayerOneDeploymentEvidenceRow

    with Session(store.engine) as session:
        row = session.get(LayerOneDeploymentEvidenceRow, hist)
        assert row is not None
        session.delete(row)
        session.commit()
    with pytest.raises(LayerOneIntegrityError):
        store.get_risk_state()


def test_orphan_evidence_row_fail_closed(store: LayerOneRiskStateStore) -> None:
    _init(store)
    from app.persistence.layer_one_models import LayerOneDeploymentEvidenceRow

    with Session(store.engine) as session:
        session.add(
            LayerOneDeploymentEvidenceRow(
                evidence_id="a" * 64,
                stream_name=LAYER_ONE_STREAM_NAME,
                request_digest="b" * 64,
                evidence_type="historical_validation_pass",
                observed_from="2023-01-01",
                observed_through="2023-12-31",
                recorded_at_utc="2024-01-02T09:00:00Z",
                operator="ops",
                summary="orphan",
                user_confirmed=True,
                contract_schema_version="1",
                two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
                layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
                data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
                historical_validation_pass=True,
                no_severe_anomaly=None,
                resulting_state_id="c" * 64,
                resulting_revision=99,
                audit_id="d" * 64,
                payload_json="{}",
            )
        )
        session.commit()
    with pytest.raises(LayerOneIntegrityError):
        store.get_risk_state()


def test_evidence_replay_after_progression_returns_original_state(
    store: LayerOneRiskStateStore,
) -> None:
    _init(store)
    req = LayerOneDeploymentEvidenceRequest(
        evidence_type="historical_validation_pass",
        observed_from=date(2023, 1, 1),
        observed_through=date(2023, 12, 31),
        recorded_at=_aware(2024, 1, 2, 9, 0),
        operator="ops",
        summary="historical validation passed",
        user_confirmed=True,
        two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
        layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
        data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
        historical_validation_pass=True,
    )
    first = store.register_deployment_evidence(req)
    _authorize(
        store,
        request_id="ceil-30",
        ceiling=0.3,
        at=_aware(2024, 1, 3),
        hist=first.evidence_id,
    )
    replay = store.register_deployment_evidence(req)
    assert replay.idempotent_replay is True
    assert replay.state_id == first.state_id
    assert replay.audit_id == first.audit_id
    assert replay.revision == first.revision
    assert store.get_risk_state().manual_ceiling == 0.3


def test_stage_started_and_updated_at_tamper_fail_closed(store: LayerOneRiskStateStore) -> None:
    _init(store)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    with Session(store.engine) as session:
        current = session.get(LayerOneCurrentState, LAYER_ONE_STREAM_NAME)
        assert current is not None
        current.manual_ceiling_stage_started_at_utc = "2020-01-01T00:00:00Z"
        session.add(current)
        session.commit()
    with pytest.raises(LayerOneIntegrityError):
        store.get_risk_state()


def test_consumed_unlock_metadata_tamper_fail_closed(store: LayerOneRiskStateStore) -> None:
    _init(store)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    _commit(
        store,
        _decision_report(
            store,
            target=date(2024, 1, 5),
            peak=100_000.0,
            current=81_000.0,
            ceiling=0.3,
            evaluated_at=_aware(2024, 1, 5, 10, 0),
        ),
    )
    unlock = LayerOneUnlockRequest(
        request_id="u-meta",
        operator="ops",
        reason="cooldown",
        requested_at=_aware(2024, 2, 5),
        user_confirmed=True,
    )
    store.submit_unlock_request(
        LayerOneUnlockRequestSubmission(
            request=unlock,
            two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
        )
    )
    with Session(store.engine) as session:
        row = session.scalars(
            select(LayerOneUnlockRequestRow).where(LayerOneUnlockRequestRow.request_id == "u-meta")
        ).one()
        row.consumed = True
        row.consumed_by_decision_id = "f" * 64
        session.add(row)
        session.commit()
    with pytest.raises(LayerOneIntegrityError):
        store.get_risk_state()


def test_audit_payload_tamper_and_delete_fail_closed(store: LayerOneRiskStateStore) -> None:
    _init(store)
    with Session(store.engine) as session:
        row = session.scalars(select(LayerOneAuditRecord).where(LayerOneAuditRecord.sequence_no == 1)).one()
        row.payload_json = "{}"
        session.add(row)
        session.commit()
    with pytest.raises(LayerOneIntegrityError):
        store.get_risk_state()
    with pytest.raises(LayerOneIntegrityError):
        store.list_audit()


def test_current_state_column_tamper_fail_closed(store: LayerOneRiskStateStore) -> None:
    _init(store)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    with Session(store.engine) as session:
        current = session.get(LayerOneCurrentState, LAYER_ONE_STREAM_NAME)
        assert current is not None
        current.applied_stock_budget_bp = 9000
        session.add(current)
        session.commit()
    with pytest.raises(LayerOneIntegrityError):
        store.get_risk_state()


def test_audit_row_delete_fail_closed(store: LayerOneRiskStateStore) -> None:
    _init(store)
    _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    with Session(store.engine) as session:
        row = session.scalars(select(LayerOneAuditRecord).where(LayerOneAuditRecord.sequence_no == 2)).one()
        session.delete(row)
        session.commit()
    with pytest.raises(LayerOneIntegrityError):
        store.list_audit()


def test_backdated_timestamps_rejected(store: LayerOneRiskStateStore) -> None:
    _init(store, at=_aware(2024, 1, 10))
    with pytest.raises(LayerOnePersistenceError):
        _register_hist(store, at=_aware(2024, 1, 9))
    with pytest.raises(LayerOnePersistenceError):
        store.authorize_manual_ceiling(
            LayerOneManualCeilingAuthorizationRequest(
                request_id="late",
                ceiling=0.0,
                authorized_at=_aware(2024, 1, 9, 12, 0),
                operator="ops",
                reason="backdated",
                user_confirmed=True,
                two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
                layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
                data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
            )
        )


def test_unlock_replay_after_consume_and_no_lock(store: LayerOneRiskStateStore) -> None:
    _init(store)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    lock = _decision_report(
        store,
        target=date(2024, 1, 5),
        peak=100_000.0,
        current=81_000.0,
        ceiling=0.3,
        evaluated_at=_aware(2024, 1, 5, 10, 0),
    )
    _commit(store, lock)
    unlock = LayerOneUnlockRequest(
        request_id="u1",
        operator="ops",
        reason="cooldown",
        requested_at=_aware(2024, 2, 5),
        user_confirmed=True,
    )
    sub = LayerOneUnlockRequestSubmission(
        request=unlock,
        two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
        layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
        data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
    )
    first = store.submit_unlock_request(sub)
    decision = _decision_report(
        store,
        target=date(2024, 3, 4),
        peak=100_000.0,
        current=95_000.0,
        ceiling=0.3,
        unlock=unlock,
        evaluated_at=_aware(2024, 3, 4, 8, 0),
        close_to_sma_ratio=1.05,
        realized_volatility_annualized=0.10,
    )
    _commit(store, decision)
    # After unlock consume / possible clear, exact replay still returns original receipt.
    replay = store.submit_unlock_request(sub)
    assert replay.idempotent_replay is True
    assert replay.audit_id == first.audit_id
    assert replay.revision == first.revision
    with pytest.raises(LayerOneConflictError):
        store.submit_unlock_request(
            LayerOneUnlockRequestSubmission(
                request=unlock.model_copy(update={"reason": "changed"}),
                two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
                layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
                data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
            )
        )


def test_manual_ceiling_timing_evidence_coverage_and_90_lock(store: LayerOneRiskStateStore) -> None:
    _init(store, at=_aware(2024, 1, 31))
    hist = _register_hist(store, at=_aware(2024, 1, 31, 9, 0))
    with pytest.raises(LayerOnePersistenceError):
        _authorize(store, request_id="skip", ceiling=0.6, at=_aware(2024, 2, 1))
    _authorize(store, request_id="c30", ceiling=0.3, at=_aware(2024, 1, 31, 10, 0), hist=hist)
    earliest = add_calendar_months(_aware(2024, 1, 31, 10, 0), 3)
    assert earliest.date() == date(2024, 4, 30)
    with pytest.raises(LayerOnePersistenceError):
        _authorize(
            store,
            request_id="c60-early",
            ceiling=0.6,
            at=_aware(2024, 4, 29, 12, 0),
            anomaly=_register_anomaly(
                store,
                at=_aware(2024, 4, 29, 11, 0),
                observed_from=date(2024, 1, 31),
                observed_through=date(2024, 4, 29),
            ),
        )
    bad = _register_anomaly(
        store,
        at=_aware(2024, 4, 30, 11, 0),
        observed_from=date(2024, 2, 1),
        observed_through=date(2024, 4, 30),
    )
    with pytest.raises(LayerOnePersistenceError):
        _authorize(store, request_id="c60-bad", ceiling=0.6, at=_aware(2024, 4, 30, 12, 0), anomaly=bad)
    good = _register_anomaly(
        store,
        at=_aware(2024, 4, 30, 13, 0),
        observed_from=date(2024, 1, 31),
        observed_through=date(2024, 4, 30),
    )
    _authorize(store, request_id="c60", ceiling=0.6, at=_aware(2024, 4, 30, 14, 0), anomaly=good)

    _authorize(store, request_id="c0", ceiling=0.0, at=_aware(2024, 5, 1))
    hist2 = store.register_deployment_evidence(
        LayerOneDeploymentEvidenceRequest(
            evidence_type="historical_validation_pass",
            observed_from=date(2021, 1, 1),
            observed_through=date(2021, 12, 31),
            recorded_at=_aware(2024, 5, 1, 9, 0),
            operator="ops",
            summary="hist2",
            user_confirmed=True,
            two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
            historical_validation_pass=True,
        )
    ).evidence_id
    _authorize(store, request_id="c30b", ceiling=0.3, at=_aware(2024, 5, 2), hist=hist2)
    stage60 = add_calendar_months(_aware(2024, 5, 2), 3)
    anom2 = _register_anomaly(
        store,
        at=stage60 - timedelta(hours=1),
        observed_from=date(2024, 5, 2),
        observed_through=stage60.date(),
    )
    _authorize(store, request_id="c60b", ceiling=0.6, at=stage60, anomaly=anom2)
    target = date(2024, 8, 16)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    lock = _decision_report(
        store,
        target=target,
        peak=100_000.0,
        current=81_000.0,
        ceiling=0.6,
        evaluated_at=_aware(target.year, target.month, target.day, 10, 0),
    )
    _commit(store, lock)
    with pytest.raises(LayerOnePersistenceError):
        _authorize(store, request_id="c90", ceiling=0.9, at=add_calendar_months(stage60, 3))
    assert budget_to_bp(0.9) == 9000


def test_two_store_writers_cas(store: LayerOneRiskStateStore) -> None:
    _init(store)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    store_b = LayerOneRiskStateStore(store.engine, repo_root=PROJECT_ROOT)
    d1 = _decision_report(store, target=date(2024, 1, 8), ceiling=0.3, evaluated_at=_aware(2024, 1, 8, 8, 0))
    d2 = _decision_report(store_b, target=date(2024, 1, 15), ceiling=0.3, evaluated_at=_aware(2024, 1, 15, 8, 0))
    env1 = LayerOneDecisionCommitRequest(
        expected_last_audit_id=store.get_risk_state().last_audit_id or "",
        expected_revision=store.get_risk_state().revision or 1,
        report=d1,
    )
    env2 = LayerOneDecisionCommitRequest(
        expected_last_audit_id=store_b.get_risk_state().last_audit_id or "",
        expected_revision=store_b.get_risk_state().revision or 1,
        report=d2,
    )
    store.commit_decision(env1)
    with pytest.raises(LayerOneConflictError):
        store_b.commit_decision(env2)


def test_unlock_consume_rollback(store: LayerOneRiskStateStore) -> None:
    _init(store)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    _commit(
        store,
        _decision_report(
            store,
            target=date(2024, 1, 5),
            peak=100_000.0,
            current=81_000.0,
            ceiling=0.3,
            evaluated_at=_aware(2024, 1, 5, 10, 0),
        ),
    )
    unlock = LayerOneUnlockRequest(
        request_id="u-roll",
        operator="ops",
        reason="cooldown",
        requested_at=_aware(2024, 3, 1, 7, 0),
        user_confirmed=True,
    )
    store.submit_unlock_request(
        LayerOneUnlockRequestSubmission(
            request=unlock,
            two_layer_decision_contract_id=BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            layer_one_index_protocol_id=BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            data_snapshot_id=CSI_INDEX_SNAPSHOT_ID,
        )
    )
    decision = _decision_report(
        store,
        target=date(2024, 3, 4),
        peak=100_000.0,
        current=95_000.0,
        ceiling=0.3,
        unlock=unlock,
        evaluated_at=_aware(2024, 3, 4, 8, 0),
    )

    def flaky(self: LayerOneRiskStateStore, request: LayerOneDecisionCommitRequest):
        from app.research.layer_one_persistence import verify_persisted_layer_one_decision

        verify_persisted_layer_one_decision(request.report, repo_root=self.repo_root)
        with Session(self.engine) as session:
            row = session.scalars(
                select(LayerOneUnlockRequestRow).where(
                    LayerOneUnlockRequestRow.request_id == request.report.unlock_request_id
                )
            ).one()
            row.consumed = True
            session.add(row)
            session.flush()
            raise RuntimeError("inject failure")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(LayerOneRiskStateStore, "commit_decision", flaky)
    with pytest.raises(RuntimeError):
        store.commit_decision(
            LayerOneDecisionCommitRequest(
                expected_last_audit_id=store.get_risk_state().last_audit_id or "",
                expected_revision=store.get_risk_state().revision or 1,
                report=decision,
            )
        )
    monkey.undo()
    with Session(store.engine) as session:
        assert (
            session.scalars(select(LayerOneUnlockRequestRow).where(LayerOneUnlockRequestRow.request_id == "u-roll"))
            .one()
            .consumed
            is False
        )


def test_auth_mismatch_and_tampered_decision(store: LayerOneRiskStateStore) -> None:
    _init(store)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    decision = _decision_report(store, target=date(2024, 1, 8), ceiling=0.3, evaluated_at=_aware(2024, 1, 8, 8, 0))
    wrong = seal_layer_one_regime_decision(
        decision.model_copy(update={"manual_ceiling_authorization_id": "f" * 64, "decision_id": None})
    )
    with pytest.raises(LayerOnePersistenceError):
        _commit(store, wrong)
    tampered = seal_layer_one_regime_decision(
        decision.model_copy(update={"applied_stock_budget": 0.9, "decision_id": None})
    )
    with pytest.raises((LayerOnePersistenceError, ValueError)):
        _commit(store, tampered)


def test_audit_pagination(store: LayerOneRiskStateStore) -> None:
    _init(store)
    hist = _register_hist(store, at=_aware(2024, 1, 2, 9, 0))
    _authorize(store, request_id="ceil-30", ceiling=0.3, at=_aware(2024, 1, 3), hist=hist)
    page1 = store.list_audit(page_size=1)
    assert len(page1.items) == 1
    page2 = store.list_audit(after_sequence=1, page_size=10)
    assert page2.items[0]["prior_audit_id"] == page1.items[0]["audit_id"]


def test_api_flags_envelope_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIQ_DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("AIQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    client = TestClient(app)
    missing = client.get("/layer-one/risk-state")
    assert missing.status_code == 200
    assert missing.json()["initialized"] is False
    assert missing.json()["does_not_trade"] is True

    ok = client.post(
        "/layer-one/risk-state/initialize",
        json={
            "operator": "ops",
            "reason": "bootstrap",
            "initialized_at": "2024-01-02T08:00:00Z",
            "user_confirmed": True,
            "two_layer_decision_contract_id": BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            "layer_one_index_protocol_id": BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            "data_snapshot_id": CSI_INDEX_SNAPSHOT_ID,
        },
    )
    assert ok.status_code == 200
    assert ok.json()["revision"] == 1

    ev = client.post(
        "/layer-one/deployment-evidence",
        json={
            "evidence_type": "historical_validation_pass",
            "observed_from": "2023-01-01",
            "observed_through": "2023-12-31",
            "recorded_at": "2024-01-02T09:00:00Z",
            "operator": "ops",
            "summary": "ok",
            "user_confirmed": True,
            "two_layer_decision_contract_id": BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            "layer_one_index_protocol_id": BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            "data_snapshot_id": CSI_INDEX_SNAPSHOT_ID,
            "historical_validation_pass": True,
        },
    )
    assert ev.status_code == 200
    evidence_id = ev.json()["evidence_id"]

    auth = client.post(
        "/layer-one/manual-ceiling-authorizations",
        json={
            "request_id": "api-30",
            "ceiling": 0.3,
            "authorized_at": "2024-01-03T08:00:00Z",
            "operator": "ops",
            "reason": "trial",
            "user_confirmed": True,
            "two_layer_decision_contract_id": BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            "layer_one_index_protocol_id": BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            "data_snapshot_id": CSI_INDEX_SNAPSHOT_ID,
            "historical_validation_evidence_id": evidence_id,
        },
    )
    assert auth.status_code == 200

    state = client.get("/layer-one/risk-state").json()
    assert state["revision"] >= 2
    assert state["last_audit_id"]

    bad_decision = client.post(
        "/layer-one/risk-state/decisions",
        json={
            "expected_last_audit_id": "a" * 64,
            "expected_revision": 1,
            "report": {"schema_version": "1"},
        },
    )
    assert bad_decision.status_code in (400, 422)

    conflict = client.post(
        "/layer-one/risk-state/initialize",
        json={
            "operator": "ops",
            "reason": "different",
            "initialized_at": "2024-01-02T08:00:00Z",
            "user_confirmed": True,
            "two_layer_decision_contract_id": BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
            "layer_one_index_protocol_id": BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
            "data_snapshot_id": CSI_INDEX_SNAPSHOT_ID,
        },
    )
    assert conflict.status_code == 409
    assert LAYER_ONE_STREAM_NAME == "layer-one-primary"


def test_no_token_or_broker_imports() -> None:
    import app.api.layer_one as api_mod
    import app.research.layer_one_persistence as mod

    banned = ("tushare", "run_score", "run_backtest", "connect a broker")
    src = Path(mod.__file__).read_text(encoding="utf-8") + Path(api_mod.__file__).read_text(encoding="utf-8")
    for token in banned:
        assert token not in src
