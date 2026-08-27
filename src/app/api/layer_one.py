"""Research-only layer-one risk-state API (E9b-1).

Does not connect ranking, scoring, backtest, portfolio, order, or broker code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from app.errors import sanitize_error_message
from app.persistence.db import get_engine, init_db
from app.research.layer_one_persistence import (
    LayerOneAuditPage,
    LayerOneConflictError,
    LayerOneDecisionCommitRequest,
    LayerOneDeploymentEvidenceRequest,
    LayerOneInitializeRequest,
    LayerOneIntegrityError,
    LayerOneManualCeilingAuthorizationRequest,
    LayerOneMutationReceipt,
    LayerOneNotFoundError,
    LayerOneNotInitializedError,
    LayerOnePersistenceError,
    LayerOneRiskStateStore,
    LayerOneRiskStateView,
    LayerOneUnlockRequestSubmission,
)
from app.settings import get_settings

router = APIRouter(prefix="/layer-one", tags=["layer-one-research"])


def _repo_root() -> Path:
    return Path(get_settings().config_dir).resolve().parent


def _store() -> LayerOneRiskStateStore:
    engine = init_db(get_engine())
    return LayerOneRiskStateStore(engine, repo_root=_repo_root())


def _http_from_persistence(exc: BaseException) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, LayerOneConflictError):
        return HTTPException(status_code=409, detail=sanitize_error_message(exc))
    if isinstance(exc, LayerOneNotFoundError):
        return HTTPException(status_code=404, detail=sanitize_error_message(exc))
    if isinstance(
        exc,
        LayerOneNotInitializedError | LayerOneIntegrityError | LayerOnePersistenceError | ValidationError | ValueError,
    ):
        return HTTPException(status_code=400, detail=sanitize_error_message(exc))
    return HTTPException(status_code=500, detail="internal server error")


@router.get("/risk-state", response_model=LayerOneRiskStateView)
def get_risk_state() -> LayerOneRiskStateView:
    try:
        return _store().get_risk_state()
    except Exception as exc:  # noqa: BLE001
        raise _http_from_persistence(exc) from exc


@router.post("/risk-state/initialize", response_model=LayerOneMutationReceipt)
def initialize_risk_state(payload: LayerOneInitializeRequest) -> LayerOneMutationReceipt:
    try:
        return _store().initialize(payload)
    except Exception as exc:  # noqa: BLE001
        raise _http_from_persistence(exc) from exc


@router.post("/manual-ceiling-authorizations", response_model=LayerOneMutationReceipt)
def authorize_manual_ceiling(payload: LayerOneManualCeilingAuthorizationRequest) -> LayerOneMutationReceipt:
    try:
        return _store().authorize_manual_ceiling(payload)
    except Exception as exc:  # noqa: BLE001
        raise _http_from_persistence(exc) from exc


@router.post("/deployment-evidence", response_model=LayerOneMutationReceipt)
def register_deployment_evidence(payload: LayerOneDeploymentEvidenceRequest) -> LayerOneMutationReceipt:
    try:
        return _store().register_deployment_evidence(payload)
    except Exception as exc:  # noqa: BLE001
        raise _http_from_persistence(exc) from exc


@router.post("/unlock-requests", response_model=LayerOneMutationReceipt)
def submit_unlock_request(payload: LayerOneUnlockRequestSubmission) -> LayerOneMutationReceipt:
    try:
        return _store().submit_unlock_request(payload)
    except Exception as exc:  # noqa: BLE001
        raise _http_from_persistence(exc) from exc


@router.post("/risk-state/decisions", response_model=LayerOneMutationReceipt)
def commit_decision(payload: LayerOneDecisionCommitRequest) -> LayerOneMutationReceipt:
    try:
        return _store().commit_decision(payload)
    except Exception as exc:  # noqa: BLE001
        raise _http_from_persistence(exc) from exc


@router.get("/audit", response_model=LayerOneAuditPage)
def list_audit(
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> LayerOneAuditPage:
    try:
        return _store().list_audit(after_sequence=after_sequence, page_size=page_size)
    except Exception as exc:  # noqa: BLE001
        raise _http_from_persistence(exc) from exc
