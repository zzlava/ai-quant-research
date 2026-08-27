"""Layer-one risk-state persistence domain (E9b-1).

Research / implementation persistence only. Never scores, backtests, trades,
loads market bars, reads tokens, or connects a broker. Fail-closed on missing
state; never silently constructs an unlocked prior.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import CursorResult, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.persistence.layer_one_models import (
    LAYER_ONE_STREAM_NAME,
    LayerOneAuditRecord,
    LayerOneCurrentState,
    LayerOneDeploymentEvidenceRow,
    LayerOneManualCeilingAuthorizationRow,
    LayerOneUnlockRequestRow,
)
from app.research.layer_one_regime import (
    BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    LAYER_ONE_REGIME_ENGINE_VERSION,
    LAYER_ONE_REGIME_SCHEMA_VERSION,
    LayerOneRegimeDecisionReport,
    LayerOneRegimeNewState,
    LayerOneRegimePriorState,
    LayerOneUnlockRequest,
    assert_decision_logic_consistent,
    assert_decision_self_hash,
    bind_index_data_evidence,
    bind_upstream_contracts,
    compute_state_id,
    compute_unlock_request_evidence_id,
    seal_layer_one_regime_state,
)

_HEX64 = r"^[0-9a-f]{64}$"
_BUDGET_BP_LEVELS: tuple[int, ...] = (0, 3000, 6000, 9000)
_BP_SCALE = Decimal(10_000)
MIN_MONTHS_AT_PRIOR_STAGE = 3

EvidenceType = Literal["historical_validation_pass", "no_severe_anomaly_period"]
LayerOneAuditEventType = Literal[
    "initialize",
    "manual_ceiling_authorization",
    "unlock_request",
    "decision",
    "deployment_evidence",
]


class LayerOnePersistenceError(ValueError):
    """Base client-facing persistence error."""


class LayerOneConflictError(LayerOnePersistenceError):
    """CAS / divergent duplicate / idempotency conflict."""


class LayerOneNotFoundError(LayerOnePersistenceError):
    """Missing resource that must not be invented."""


class LayerOneNotInitializedError(LayerOnePersistenceError):
    """Named stream has no persistent state."""


class LayerOneIntegrityError(LayerOnePersistenceError):
    """Stored audit/state chain failed integrity verification."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _reject_blank(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise LayerOnePersistenceError(f"{field_name} must be a non-empty string")
    return value


def _require_hex64(value: object, *, field_name: str) -> str:
    text = _reject_blank(value, field_name=field_name)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise LayerOnePersistenceError(f"{field_name} must be a 64-char lowercase hex digest")
    return text


def _require_aware(value: datetime, *, field_name: str, now: datetime | None = None) -> datetime:
    if not isinstance(value, datetime):
        raise LayerOnePersistenceError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise LayerOnePersistenceError(f"{field_name} must be timezone-aware")
    if now is not None and value > now:
        raise LayerOnePersistenceError(f"{field_name} must not be in the future")
    return value


def _to_utc_iso(value: datetime) -> str:
    aware = _require_aware(value, field_name="timestamp")
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc_iso(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LayerOnePersistenceError("stored timestamp must be timezone-aware UTC")
    return parsed.astimezone(UTC)


def budget_to_bp(value: float | Decimal | int | str) -> int:
    number = Decimal(str(value))
    scaled = number * _BP_SCALE
    if scaled != scaled.to_integral_value():
        raise LayerOnePersistenceError("budget level must map exactly to integer basis points")
    bp = int(scaled)
    if bp not in _BUDGET_BP_LEVELS:
        raise LayerOnePersistenceError(f"budget level must be one of {[bp / 10000 for bp in _BUDGET_BP_LEVELS]}")
    return bp


def bp_to_budget(bp: int) -> float:
    if bp not in _BUDGET_BP_LEVELS:
        raise LayerOnePersistenceError("persisted budget basis points are invalid")
    return float(Decimal(bp) / _BP_SCALE)


def add_calendar_months(instant: datetime, months: int) -> datetime:
    aware = _require_aware(instant, field_name="instant")
    if months < 0:
        raise LayerOnePersistenceError("months must be non-negative")
    return aware + relativedelta(months=months)


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(payload: dict[str, Any] | bytes) -> str:
    raw = payload if isinstance(payload, bytes) else canonical_json_bytes(payload)
    return hashlib.sha256(raw).hexdigest()


def verify_persisted_layer_one_decision(
    report: LayerOneRegimeDecisionReport,
    *,
    repo_root: Path,
) -> LayerOneRegimeDecisionReport:
    assert_decision_self_hash(report)
    if report.ready_for_orders or report.ready_for_trading or report.does_not_trade is not True:
        raise LayerOnePersistenceError("layer-one regime decision cannot authorize orders or trading")
    if report.exact_symbol_identity_verified is not True:
        raise LayerOnePersistenceError("exact_symbol_identity_verified must remain true")
    contract_id, contract_path, protocol_id, protocol_path = bind_upstream_contracts(repo_root=repo_root)
    evidence_id, evidence_path, snapshot_id, risk_symbol = bind_index_data_evidence(repo_root=repo_root)
    if report.two_layer_decision_contract_id != contract_id:
        raise LayerOnePersistenceError("decision two_layer_decision_contract_id does not match disk binding")
    if report.layer_one_index_protocol_id != protocol_id:
        raise LayerOnePersistenceError("decision layer_one_index_protocol_id does not match disk binding")
    if report.two_layer_decision_contract_path != contract_path:
        raise LayerOnePersistenceError("decision two_layer_decision_contract_path does not match disk binding")
    if report.layer_one_index_protocol_path != protocol_path:
        raise LayerOnePersistenceError("decision layer_one_index_protocol_path does not match disk binding")
    if report.layer_one_index_data_evidence_id != evidence_id:
        raise LayerOnePersistenceError("decision layer_one_index_data_evidence_id does not match disk binding")
    if report.layer_one_index_data_evidence_path != evidence_path:
        raise LayerOnePersistenceError("decision layer_one_index_data_evidence_path does not match disk binding")
    if report.data_snapshot_id != snapshot_id:
        raise LayerOnePersistenceError("decision data_snapshot_id does not match verified CSI snapshot")
    if report.index_symbol_input != risk_symbol:
        raise LayerOnePersistenceError("decision index_symbol_input does not match verified CSI identity")
    assert_decision_logic_consistent(report)
    return report


class LayerOneInitializeRequest(_StrictModel):
    operator: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    initialized_at: datetime
    user_confirmed: bool
    two_layer_decision_contract_id: str = Field(pattern=_HEX64)
    layer_one_index_protocol_id: str = Field(pattern=_HEX64)
    data_snapshot_id: str = Field(min_length=1)
    contract_schema_version: Literal["1"] = LAYER_ONE_REGIME_SCHEMA_VERSION
    engine_version: Literal["layer-one-regime-engine-v1"] = LAYER_ONE_REGIME_ENGINE_VERSION

    @field_validator("operator", "reason", "data_snapshot_id", mode="before")
    @classmethod
    def _blank(cls, value: object, info: Any) -> object:
        return _reject_blank(value, field_name=info.field_name)

    @field_validator("initialized_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="initialized_at")


class LayerOneManualCeilingAuthorizationRequest(_StrictModel):
    request_id: str = Field(min_length=1)
    ceiling: float
    authorized_at: datetime
    operator: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    user_confirmed: bool
    contract_schema_version: Literal["1"] = LAYER_ONE_REGIME_SCHEMA_VERSION
    two_layer_decision_contract_id: str = Field(pattern=_HEX64)
    layer_one_index_protocol_id: str = Field(pattern=_HEX64)
    data_snapshot_id: str = Field(min_length=1)
    historical_validation_evidence_id: str | None = Field(default=None, pattern=_HEX64)
    no_severe_anomaly_evidence_id: str | None = Field(default=None, pattern=_HEX64)
    auto_upgrade: Literal[False] = False

    @field_validator("request_id", "operator", "reason", "data_snapshot_id", mode="before")
    @classmethod
    def _blank(cls, value: object, info: Any) -> object:
        return _reject_blank(value, field_name=info.field_name)

    @field_validator("authorized_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="authorized_at")

    @field_validator("ceiling")
    @classmethod
    def _ceiling(cls, value: float) -> float:
        return bp_to_budget(budget_to_bp(value))

    @model_validator(mode="after")
    def _no_auto(self) -> LayerOneManualCeilingAuthorizationRequest:
        if self.auto_upgrade is not False:
            raise LayerOnePersistenceError("automatic ceiling upgrade is forbidden")
        return self


class LayerOneUnlockRequestSubmission(_StrictModel):
    request: LayerOneUnlockRequest
    two_layer_decision_contract_id: str = Field(pattern=_HEX64)
    layer_one_index_protocol_id: str = Field(pattern=_HEX64)
    data_snapshot_id: str = Field(min_length=1)

    @field_validator("data_snapshot_id", mode="before")
    @classmethod
    def _blank(cls, value: object) -> object:
        return _reject_blank(value, field_name="data_snapshot_id")


class LayerOneDeploymentEvidenceRequest(_StrictModel):
    evidence_type: EvidenceType
    observed_from: date
    observed_through: date
    recorded_at: datetime
    operator: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    user_confirmed: bool
    contract_schema_version: Literal["1"] = LAYER_ONE_REGIME_SCHEMA_VERSION
    two_layer_decision_contract_id: str = Field(pattern=_HEX64)
    layer_one_index_protocol_id: str = Field(pattern=_HEX64)
    data_snapshot_id: str = Field(min_length=1)
    historical_validation_pass: Literal[True] | None = None
    no_severe_anomaly: Literal[True] | None = None

    @field_validator("operator", "summary", "data_snapshot_id", mode="before")
    @classmethod
    def _blank(cls, value: object, info: Any) -> object:
        return _reject_blank(value, field_name=info.field_name)

    @field_validator("recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="recorded_at")

    @model_validator(mode="after")
    def _type_flags(self) -> LayerOneDeploymentEvidenceRequest:
        if self.observed_from > self.observed_through:
            raise LayerOnePersistenceError("observed_from must be on or before observed_through")
        if self.evidence_type == "historical_validation_pass":
            if self.historical_validation_pass is not True:
                raise LayerOnePersistenceError("historical_validation_pass must be true")
            if self.no_severe_anomaly is not None:
                raise LayerOnePersistenceError("no_severe_anomaly must be null for historical_validation_pass")
        else:
            if self.no_severe_anomaly is not True:
                raise LayerOnePersistenceError("no_severe_anomaly must be true")
            if self.historical_validation_pass is not None:
                raise LayerOnePersistenceError("historical_validation_pass must be null for no_severe_anomaly_period")
        if not self.user_confirmed:
            raise LayerOnePersistenceError("deployment evidence requires user_confirmed=true")
        return self


class LayerOneDecisionCommitRequest(_StrictModel):
    expected_last_audit_id: str = Field(pattern=_HEX64)
    expected_revision: int = Field(ge=1)
    report: LayerOneRegimeDecisionReport


class LayerOneRiskStateView(_StrictModel):
    stream_name: str
    initialized: bool
    revision: int | None = None
    state_id: str | None = None
    applied_stock_budget: float = 0.0
    effective_stock_budget: float = 0.0
    manual_ceiling: float = 0.0
    manual_ceiling_authorization_id: str | None = None
    risk_lock_active: bool | None = None
    risk_lock_triggered_as_of: date | None = None
    red_line_breached: bool | None = None
    last_decision_id: str | None = None
    last_decision_target_trading_day: date | None = None
    last_audit_id: str | None = None
    data_snapshot_id: str | None = None
    two_layer_decision_contract_id: str | None = None
    layer_one_index_protocol_id: str | None = None
    initialized_at: datetime | None = None
    updated_at: datetime | None = None
    research_only: Literal[True] = True
    implementation_only: Literal[True] = True
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    does_not_trade: Literal[True] = True


class LayerOneMutationReceipt(_StrictModel):
    stream_name: str
    event_type: LayerOneAuditEventType
    audit_id: str
    revision: int
    authorization_id: str | None = None
    unlock_request_id: str | None = None
    unlock_evidence_id: str | None = None
    evidence_id: str | None = None
    decision_id: str | None = None
    state_id: str | None = None
    idempotent_replay: bool = False
    research_only: Literal[True] = True
    implementation_only: Literal[True] = True
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    does_not_trade: Literal[True] = True


class LayerOneAuditPage(_StrictModel):
    stream_name: str
    items: list[dict[str, Any]]
    next_after_sequence: int | None = None
    page_size: int
    research_only: Literal[True] = True
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    does_not_trade: Literal[True] = True


class _AuditEventKind(StrEnum):
    INITIALIZE = "initialize"
    MANUAL_CEILING = "manual_ceiling_authorization"
    UNLOCK_REQUEST = "unlock_request"
    DECISION = "decision"
    DEPLOYMENT_EVIDENCE = "deployment_evidence"


def _uninitialized_view(*, stream_name: str = LAYER_ONE_STREAM_NAME) -> LayerOneRiskStateView:
    return LayerOneRiskStateView(
        stream_name=stream_name,
        initialized=False,
        applied_stock_budget=0.0,
        effective_stock_budget=0.0,
        manual_ceiling=0.0,
        risk_lock_active=None,
        red_line_breached=None,
    )


def _state_view(row: LayerOneCurrentState) -> LayerOneRiskStateView:
    budget = bp_to_budget(row.applied_stock_budget_bp)
    lock_as_of = date.fromisoformat(row.risk_lock_triggered_as_of) if row.risk_lock_triggered_as_of else None
    last_target = (
        date.fromisoformat(row.last_decision_target_trading_day) if row.last_decision_target_trading_day else None
    )
    return LayerOneRiskStateView(
        stream_name=row.stream_name,
        initialized=True,
        revision=row.revision,
        state_id=row.state_id,
        applied_stock_budget=budget,
        effective_stock_budget=budget,
        manual_ceiling=bp_to_budget(row.manual_ceiling_bp),
        manual_ceiling_authorization_id=row.manual_ceiling_authorization_id,
        risk_lock_active=row.risk_lock_active,
        risk_lock_triggered_as_of=lock_as_of,
        red_line_breached=row.red_line_breached,
        last_decision_id=row.last_decision_id,
        last_decision_target_trading_day=last_target,
        last_audit_id=row.last_audit_id,
        data_snapshot_id=row.data_snapshot_id,
        two_layer_decision_contract_id=row.two_layer_decision_contract_id,
        layer_one_index_protocol_id=row.layer_one_index_protocol_id,
        initialized_at=_parse_utc_iso(row.initialized_at_utc),
        updated_at=_parse_utc_iso(row.updated_at_utc),
    )


def _assert_bound_contract_ids(
    *,
    two_layer_decision_contract_id: str,
    layer_one_index_protocol_id: str,
) -> None:
    if two_layer_decision_contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
        raise LayerOnePersistenceError("two_layer_decision_contract_id does not match bound contract")
    if layer_one_index_protocol_id != BOUND_LAYER_ONE_INDEX_PROTOCOL_ID:
        raise LayerOnePersistenceError("layer_one_index_protocol_id does not match bound protocol")


def _require_monotonic(*, stamp: datetime, current_updated_at: datetime, field_name: str) -> None:
    if stamp < current_updated_at:
        raise LayerOnePersistenceError(f"{field_name} must be >= current stream updated_at")


def _manual_request_digest(request: LayerOneManualCeilingAuthorizationRequest) -> str:
    """Canonical digest independent of current prior ceiling / stage state."""
    return sha256_hex(
        {
            "request_id": request.request_id,
            "ceiling_bp": budget_to_bp(request.ceiling),
            "authorized_at_utc": _to_utc_iso(request.authorized_at),
            "operator": request.operator,
            "reason": request.reason,
            "user_confirmed": request.user_confirmed,
            "contract_schema_version": request.contract_schema_version,
            "two_layer_decision_contract_id": request.two_layer_decision_contract_id,
            "layer_one_index_protocol_id": request.layer_one_index_protocol_id,
            "data_snapshot_id": request.data_snapshot_id,
            "historical_validation_evidence_id": request.historical_validation_evidence_id,
            "no_severe_anomaly_evidence_id": request.no_severe_anomaly_evidence_id,
            "auto_upgrade": False,
        }
    )


def _unlock_submission_digest(submission: LayerOneUnlockRequestSubmission) -> str:
    return sha256_hex(
        {
            **submission.request.model_dump(mode="json"),
            "two_layer_decision_contract_id": submission.two_layer_decision_contract_id,
            "layer_one_index_protocol_id": submission.layer_one_index_protocol_id,
            "data_snapshot_id": submission.data_snapshot_id,
        }
    )


def _evidence_canonical_body(request: LayerOneDeploymentEvidenceRequest) -> dict[str, Any]:
    return {
        "evidence_type": request.evidence_type,
        "observed_from": request.observed_from.isoformat(),
        "observed_through": request.observed_through.isoformat(),
        "recorded_at_utc": _to_utc_iso(request.recorded_at),
        "operator": request.operator,
        "summary": request.summary,
        "user_confirmed": True,
        "contract_schema_version": request.contract_schema_version,
        "two_layer_decision_contract_id": request.two_layer_decision_contract_id,
        "layer_one_index_protocol_id": request.layer_one_index_protocol_id,
        "data_snapshot_id": request.data_snapshot_id,
        "historical_validation_pass": request.historical_validation_pass,
        "no_severe_anomaly": request.no_severe_anomaly,
    }


def compute_deployment_evidence_id(request: LayerOneDeploymentEvidenceRequest) -> str:
    return sha256_hex(_evidence_canonical_body(request))


def _build_audit_payload(
    *,
    stream_name: str,
    sequence_no: int,
    prior_audit_id: str | None,
    event_type: str,
    recorded_at: datetime,
    body: dict[str, Any],
    revision_after: int,
    decision_id: str | None = None,
    authorization_id: str | None = None,
    unlock_request_id: str | None = None,
    evidence_id: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    payload = {
        "stream_name": stream_name,
        "sequence_no": sequence_no,
        "prior_audit_id": prior_audit_id,
        "event_type": event_type,
        "recorded_at_utc": _to_utc_iso(recorded_at),
        "body": body,
        "decision_id": decision_id,
        "authorization_id": authorization_id,
        "unlock_request_id": unlock_request_id,
        "evidence_id": evidence_id,
        "revision_after": revision_after,
    }
    digest = sha256_hex(payload)
    audit_id = sha256_hex({**payload, "payload_digest": digest})
    return payload, digest, audit_id


def _append_audit(
    session: Session,
    *,
    stream_name: str,
    event_type: str,
    recorded_at: datetime,
    body: dict[str, Any],
    prior_audit_id: str | None,
    sequence_no: int,
    revision_after: int,
    decision_id: str | None = None,
    authorization_id: str | None = None,
    unlock_request_id: str | None = None,
    evidence_id: str | None = None,
) -> LayerOneAuditRecord:
    payload, digest, audit_id = _build_audit_payload(
        stream_name=stream_name,
        sequence_no=sequence_no,
        prior_audit_id=prior_audit_id,
        event_type=event_type,
        recorded_at=recorded_at,
        body=body,
        revision_after=revision_after,
        decision_id=decision_id,
        authorization_id=authorization_id,
        unlock_request_id=unlock_request_id,
        evidence_id=evidence_id,
    )
    row = LayerOneAuditRecord(
        audit_id=audit_id,
        stream_name=stream_name,
        sequence_no=sequence_no,
        prior_audit_id=prior_audit_id,
        event_type=event_type,
        recorded_at_utc=_to_utc_iso(recorded_at),
        payload_digest=digest,
        payload_json=canonical_json_bytes(payload).decode("utf-8"),
        decision_id=decision_id,
        authorization_id=authorization_id,
        unlock_request_id=unlock_request_id,
        evidence_id=evidence_id,
        revision_after=revision_after,
    )
    session.add(row)
    return row


def _cas_update_current(
    session: Session,
    *,
    stream_name: str,
    expected_revision: int,
    expected_last_audit_id: str,
    expected_state_id: str | None,
    values: dict[str, Any],
) -> None:
    clauses = [
        LayerOneCurrentState.stream_name == stream_name,
        LayerOneCurrentState.revision == expected_revision,
        LayerOneCurrentState.last_audit_id == expected_last_audit_id,
    ]
    if expected_state_id is not None:
        clauses.append(LayerOneCurrentState.state_id == expected_state_id)
    result = session.execute(update(LayerOneCurrentState).where(*clauses).values(**values))
    if not isinstance(result, CursorResult) or result.rowcount != 1:
        raise LayerOneConflictError("compare-and-swap rejected; stale revision, audit, or state")


class LayerOneRiskStateStore:
    """SQLite-compatible transactional store for one named layer-one stream."""

    def __init__(
        self,
        engine: Engine,
        *,
        stream_name: str = LAYER_ONE_STREAM_NAME,
        repo_root: Path | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.stream_name = stream_name
        self.repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
        self._now_provider = now_provider or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        return _require_aware(self._now_provider(), field_name="now")

    def verify_storage_integrity(self, session: Session) -> LayerOneCurrentState | None:
        """Strict full-chain verifier. Fail closed; never repair."""
        current = session.get(LayerOneCurrentState, self.stream_name)
        audits = list(
            session.scalars(
                select(LayerOneAuditRecord)
                .where(LayerOneAuditRecord.stream_name == self.stream_name)
                .order_by(LayerOneAuditRecord.sequence_no.asc())
            ).all()
        )
        if current is None:
            if audits:
                raise LayerOneIntegrityError("audit records exist without current state")
            auth_orphan = session.scalars(
                select(LayerOneManualCeilingAuthorizationRow).where(
                    LayerOneManualCeilingAuthorizationRow.stream_name == self.stream_name
                )
            ).first()
            evidence_orphan = session.scalars(
                select(LayerOneDeploymentEvidenceRow).where(
                    LayerOneDeploymentEvidenceRow.stream_name == self.stream_name
                )
            ).first()
            unlock_orphan = session.scalars(
                select(LayerOneUnlockRequestRow).where(LayerOneUnlockRequestRow.stream_name == self.stream_name)
            ).first()
            if auth_orphan is not None or evidence_orphan is not None or unlock_orphan is not None:
                raise LayerOneIntegrityError("specialized rows exist without current state")
            return None
        if not audits:
            raise LayerOneIntegrityError("current state exists without audit records")

        prior_id: str | None = None
        for idx, audit_row in enumerate(audits, start=1):
            if audit_row.sequence_no != idx:
                raise LayerOneIntegrityError("audit sequence is not contiguous from 1")
            try:
                payload = json.loads(audit_row.payload_json)
            except json.JSONDecodeError as exc:
                raise LayerOneIntegrityError("audit payload_json is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise LayerOneIntegrityError("audit payload must be an object")
            column_map = {
                "stream_name": audit_row.stream_name,
                "sequence_no": audit_row.sequence_no,
                "prior_audit_id": audit_row.prior_audit_id,
                "event_type": audit_row.event_type,
                "recorded_at_utc": audit_row.recorded_at_utc,
                "decision_id": audit_row.decision_id,
                "authorization_id": audit_row.authorization_id,
                "unlock_request_id": audit_row.unlock_request_id,
                "evidence_id": audit_row.evidence_id,
                "revision_after": audit_row.revision_after,
            }
            for key, value in column_map.items():
                if payload.get(key) != value:
                    raise LayerOneIntegrityError(f"audit column/payload mismatch for {key}")
            if "body" not in payload:
                raise LayerOneIntegrityError("audit payload missing body")
            recomputed_digest = sha256_hex(payload)
            if audit_row.payload_digest != recomputed_digest:
                raise LayerOneIntegrityError("audit payload_digest does not match payload")
            recomputed_id = sha256_hex({**payload, "payload_digest": recomputed_digest})
            if audit_row.audit_id != recomputed_id:
                raise LayerOneIntegrityError("audit_id does not match canonical content")
            if audit_row.prior_audit_id != prior_id:
                raise LayerOneIntegrityError("audit prior_audit_id chain is broken")
            prior_id = audit_row.audit_id

        tail = audits[-1]
        if current.last_audit_id != tail.audit_id:
            raise LayerOneIntegrityError("current last_audit_id does not match audit tail")
        if current.audit_sequence_no != tail.sequence_no:
            raise LayerOneIntegrityError("current audit_sequence_no does not match audit tail")
        if current.revision != tail.revision_after:
            raise LayerOneIntegrityError("current revision does not match audit tail revision_after")
        if current.updated_at_utc != tail.recorded_at_utc:
            raise LayerOneIntegrityError("current updated_at_utc does not match audit tail recorded_at_utc")

        auth_rows = list(
            session.scalars(
                select(LayerOneManualCeilingAuthorizationRow).where(
                    LayerOneManualCeilingAuthorizationRow.stream_name == self.stream_name
                )
            ).all()
        )
        evidence_rows = list(
            session.scalars(
                select(LayerOneDeploymentEvidenceRow).where(
                    LayerOneDeploymentEvidenceRow.stream_name == self.stream_name
                )
            ).all()
        )
        unlock_rows = list(
            session.scalars(
                select(LayerOneUnlockRequestRow).where(LayerOneUnlockRequestRow.stream_name == self.stream_name)
            ).all()
        )
        auth_by_id = {row.authorization_id: row for row in auth_rows}
        evidence_by_id = {row.evidence_id: row for row in evidence_rows}
        unlock_by_request = {row.request_id: row for row in unlock_rows}

        audit_auth_ids: set[str] = set()
        audit_evidence_ids: set[str] = set()
        audit_unlock_ids: set[str] = set()
        for audit in audits:
            if audit.event_type in (
                _AuditEventKind.INITIALIZE.value,
                _AuditEventKind.MANUAL_CEILING.value,
            ):
                if audit.authorization_id is None:
                    raise LayerOneIntegrityError("authorization audit missing authorization_id")
                audit_auth_ids.add(audit.authorization_id)
                auth_match = auth_by_id.get(audit.authorization_id)
                if auth_match is None:
                    raise LayerOneIntegrityError("authorization audit missing specialized row")
                if auth_match.audit_id != audit.audit_id:
                    raise LayerOneIntegrityError("authorization row audit_id does not match audit")
            if audit.event_type == _AuditEventKind.DEPLOYMENT_EVIDENCE.value:
                if audit.evidence_id is None:
                    raise LayerOneIntegrityError("evidence audit missing evidence_id")
                audit_evidence_ids.add(audit.evidence_id)
                evidence_match = evidence_by_id.get(audit.evidence_id)
                if evidence_match is None:
                    raise LayerOneIntegrityError("evidence audit missing specialized row")
                if evidence_match.audit_id != audit.audit_id:
                    raise LayerOneIntegrityError("evidence row audit_id does not match audit")
            if audit.event_type == _AuditEventKind.UNLOCK_REQUEST.value:
                if audit.unlock_request_id is None:
                    raise LayerOneIntegrityError("unlock audit missing unlock_request_id")
                audit_unlock_ids.add(audit.unlock_request_id)
                unlock_match = unlock_by_request.get(audit.unlock_request_id)
                if unlock_match is None:
                    raise LayerOneIntegrityError("unlock audit missing specialized row")
                if unlock_match.audit_id != audit.audit_id:
                    raise LayerOneIntegrityError("unlock row audit_id does not match audit")

        if set(auth_by_id) != audit_auth_ids:
            raise LayerOneIntegrityError("authorization rows are not 1:1 with authorization audits")
        if set(evidence_by_id) != audit_evidence_ids:
            raise LayerOneIntegrityError("evidence rows are not 1:1 with evidence audits")
        if set(unlock_by_request) != audit_unlock_ids:
            raise LayerOneIntegrityError("unlock rows are not 1:1 with unlock audits")

        for auth_row in auth_rows:
            self._verify_authorization_row(
                auth_row, audits=audits, auth_by_id=auth_by_id, evidence_by_id=evidence_by_id
            )
        for evidence_row in evidence_rows:
            self._verify_evidence_row(evidence_row)
        for unlock_row in unlock_rows:
            self._verify_unlock_row(unlock_row, audits=audits)

        try:
            state = LayerOneRegimeNewState.model_validate_json(current.state_json)
        except Exception as exc:
            raise LayerOneIntegrityError("current state_json is not a valid sealed new state") from exc
        if state.state_id is None or compute_state_id(state) != state.state_id:
            raise LayerOneIntegrityError("current state_json self-hash mismatch")
        if state.state_id != current.state_id:
            raise LayerOneIntegrityError("current.state_id does not match state_json")
        if budget_to_bp(state.applied_stock_budget) != current.applied_stock_budget_bp:
            raise LayerOneIntegrityError("applied budget column does not match state_json")
        if state.risk_lock_active != current.risk_lock_active:
            raise LayerOneIntegrityError("risk_lock_active column does not match state_json")
        expected_lock_as_of = state.risk_lock_triggered_as_of.isoformat() if state.risk_lock_triggered_as_of else None
        if expected_lock_as_of != current.risk_lock_triggered_as_of:
            raise LayerOneIntegrityError("risk_lock_triggered_as_of column does not match state_json")
        if state.red_line_breached != current.red_line_breached:
            raise LayerOneIntegrityError("red_line_breached column does not match state_json")
        if state.risk_lock_active and current.applied_stock_budget_bp != 0:
            raise LayerOneIntegrityError("risk lock requires applied budget zero")

        decision_audits = [a for a in audits if a.event_type == _AuditEventKind.DECISION.value]
        if decision_audits:
            latest_decision = decision_audits[-1]
            body = json.loads(latest_decision.payload_json).get("body") or {}
            if current.last_decision_id != latest_decision.decision_id:
                raise LayerOneIntegrityError("last_decision_id does not match latest decision audit")
            if current.last_decision_target_trading_day != body.get("target_trading_day"):
                raise LayerOneIntegrityError("last_decision_target_trading_day mismatch")
            if body.get("new_state_id") != current.state_id:
                raise LayerOneIntegrityError("current state_id does not match latest decision new_state")
            decision_obj = body.get("decision") or {}
            try:
                decision_new_state = LayerOneRegimeNewState.model_validate(decision_obj.get("new_state"))
            except Exception as exc:
                raise LayerOneIntegrityError("latest decision new_state is invalid") from exc
            if decision_new_state.model_dump(mode="json") != json.loads(current.state_json):
                raise LayerOneIntegrityError("current state_json does not match latest decision new_state")
        else:
            if current.last_decision_id is not None or current.last_decision_target_trading_day is not None:
                raise LayerOneIntegrityError("last decision fields must be null when no decision audit exists")
            if current.state_id != current.init_state_id:
                raise LayerOneIntegrityError("current state_id must equal init_state_id before any decision")

        auth_audits = [
            a
            for a in audits
            if a.event_type in (_AuditEventKind.INITIALIZE.value, _AuditEventKind.MANUAL_CEILING.value)
        ]
        latest_auth_audit = auth_audits[-1]
        if current.manual_ceiling_authorization_id != latest_auth_audit.authorization_id:
            raise LayerOneIntegrityError("current authorization_id is not the latest authorization event")
        latest_auth = auth_by_id[latest_auth_audit.authorization_id or ""]
        if current.manual_ceiling_bp != latest_auth.ceiling_bp:
            raise LayerOneIntegrityError("current ceiling does not match latest authorization payload/row")
        if current.data_snapshot_id != latest_auth.data_snapshot_id:
            raise LayerOneIntegrityError("current data_snapshot_id does not match latest authorization")
        if current.manual_ceiling_stage_started_at_utc != latest_auth.authorized_at_utc:
            raise LayerOneIntegrityError("current stage_started_at does not match latest authorization")

        init_audit = session.get(LayerOneAuditRecord, current.init_audit_id)
        if init_audit is None or init_audit.sequence_no != 1:
            raise LayerOneIntegrityError("init_audit_id must reference sequence-1 audit")
        init_payload = json.loads(init_audit.payload_json)
        init_body = init_payload.get("body") or {}
        if current.init_authorization_id != init_payload.get("authorization_id"):
            raise LayerOneIntegrityError("init_authorization_id does not match initialize audit")
        if current.init_state_id != init_body.get("state_id"):
            raise LayerOneIntegrityError("init_state_id does not match initialize audit")
        init_auth = auth_by_id.get(current.init_authorization_id)
        if init_auth is None:
            raise LayerOneIntegrityError("init authorization row missing")
        stored_init = {
            "operator": init_body.get("operator"),
            "reason": init_body.get("reason"),
            "initialized_at_utc": init_body.get("initialized_at_utc"),
            "user_confirmed": init_body.get("user_confirmed"),
            "two_layer_decision_contract_id": init_body.get("two_layer_decision_contract_id"),
            "layer_one_index_protocol_id": init_body.get("layer_one_index_protocol_id"),
            "data_snapshot_id": init_body.get("data_snapshot_id"),
            "contract_schema_version": init_body.get("contract_schema_version"),
            "engine_version": init_body.get("engine_version"),
            "applied_stock_budget_bp": init_body.get("applied_stock_budget_bp"),
            "manual_ceiling_bp": init_body.get("manual_ceiling_bp"),
        }
        if sha256_hex(stored_init) != current.init_request_digest:
            raise LayerOneIntegrityError("init_request_digest does not match initialize audit body")
        if current.initialized_at_utc != init_body.get("initialized_at_utc"):
            raise LayerOneIntegrityError("initialized_at_utc does not match initialize audit")
        if current.two_layer_decision_contract_id != init_body.get("two_layer_decision_contract_id"):
            raise LayerOneIntegrityError("current contract id does not match initialize audit")
        if current.layer_one_index_protocol_id != init_body.get("layer_one_index_protocol_id"):
            raise LayerOneIntegrityError("current protocol id does not match initialize audit")
        if init_auth.authorization_id != current.init_authorization_id:
            raise LayerOneIntegrityError("init auth row id mismatch")

        return current

    def _verify_authorization_row(
        self,
        row: LayerOneManualCeilingAuthorizationRow,
        *,
        audits: list[LayerOneAuditRecord],
        auth_by_id: dict[str, LayerOneManualCeilingAuthorizationRow],
        evidence_by_id: dict[str, LayerOneDeploymentEvidenceRow],
    ) -> None:
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError as exc:
            raise LayerOneIntegrityError("authorization payload_json invalid") from exc
        if not isinstance(payload, dict):
            raise LayerOneIntegrityError("authorization payload must be an object")
        expected_columns = {
            "request_id": row.request_id,
            "request_digest": row.request_digest,
            "ceiling_bp": row.ceiling_bp,
            "prior_ceiling_bp": row.prior_ceiling_bp,
            "authorized_at_utc": row.authorized_at_utc,
            "operator": row.operator,
            "reason": row.reason,
            "user_confirmed": row.user_confirmed,
            "contract_schema_version": row.contract_schema_version,
            "two_layer_decision_contract_id": row.two_layer_decision_contract_id,
            "layer_one_index_protocol_id": row.layer_one_index_protocol_id,
            "data_snapshot_id": row.data_snapshot_id,
            "historical_validation_evidence_id": row.historical_validation_evidence_id,
            "no_severe_anomaly_evidence_id": row.no_severe_anomaly_evidence_id,
            "resulting_state_id": row.resulting_state_id,
            "resulting_revision": row.resulting_revision,
            "source": payload.get("source"),
        }
        for key, value in expected_columns.items():
            if key == "source":
                continue
            if payload.get(key) != value:
                raise LayerOneIntegrityError(f"authorization column/payload mismatch for {key}")
        if payload.get("source") not in {"initialize", "manual_authorization"}:
            raise LayerOneIntegrityError("authorization payload source invalid")
        if row.stream_name != self.stream_name:
            raise LayerOneIntegrityError("authorization stream mismatch")
        if sha256_hex(payload) != row.authorization_id:
            raise LayerOneIntegrityError("authorization_id does not match sealed payload")
        session = Session.object_session(row)
        assert session is not None
        audit_row = session.get(LayerOneAuditRecord, row.audit_id)
        if audit_row is None or audit_row.authorization_id != row.authorization_id:
            raise LayerOneIntegrityError("authorization audit binding broken")
        if audit_row.revision_after != row.resulting_revision:
            raise LayerOneIntegrityError("authorization resulting_revision does not match audit")

        if row.ceiling_bp == 3000 and row.prior_ceiling_bp == 0:
            if row.historical_validation_evidence_id is None:
                raise LayerOneIntegrityError("0.3 authorization missing historical_validation evidence")
            evidence = evidence_by_id.get(row.historical_validation_evidence_id)
            if evidence is None:
                raise LayerOneIntegrityError("referenced historical_validation evidence missing")
            self._assert_auth_evidence_sealed(
                evidence,
                expected_type="historical_validation_pass",
                data_snapshot_id=row.data_snapshot_id,
                authorized_at_utc=row.authorized_at_utc,
                stage_started_at_utc=None,
            )
        if row.ceiling_bp == 6000 and row.prior_ceiling_bp == 3000:
            if row.no_severe_anomaly_evidence_id is None:
                raise LayerOneIntegrityError("0.6 authorization missing no_severe_anomaly evidence")
            evidence = evidence_by_id.get(row.no_severe_anomaly_evidence_id)
            if evidence is None:
                raise LayerOneIntegrityError("referenced no_severe_anomaly evidence missing")
            stage_started = self._prior_stage_started_at_utc(
                row=row,
                audits=audits,
                auth_by_id=auth_by_id,
            )
            self._assert_auth_evidence_sealed(
                evidence,
                expected_type="no_severe_anomaly_period",
                data_snapshot_id=row.data_snapshot_id,
                authorized_at_utc=row.authorized_at_utc,
                stage_started_at_utc=stage_started,
            )

    def _prior_stage_started_at_utc(
        self,
        *,
        row: LayerOneManualCeilingAuthorizationRow,
        audits: list[LayerOneAuditRecord],
        auth_by_id: dict[str, LayerOneManualCeilingAuthorizationRow],
    ) -> str:
        auth_audit = next(a for a in audits if a.audit_id == row.audit_id)
        candidates: list[tuple[int, LayerOneManualCeilingAuthorizationRow]] = []
        for audit in audits:
            if audit.sequence_no >= auth_audit.sequence_no:
                break
            if audit.authorization_id is None:
                continue
            other = auth_by_id.get(audit.authorization_id)
            if other is not None and other.ceiling_bp == row.prior_ceiling_bp:
                candidates.append((audit.sequence_no, other))
        if not candidates:
            raise LayerOneIntegrityError("unable to resolve prior stage start for authorization")
        return max(candidates, key=lambda item: item[0])[1].authorized_at_utc

    def _assert_auth_evidence_sealed(
        self,
        evidence: LayerOneDeploymentEvidenceRow,
        *,
        expected_type: EvidenceType,
        data_snapshot_id: str,
        authorized_at_utc: str,
        stage_started_at_utc: str | None,
    ) -> None:
        self._verify_evidence_row(evidence)
        payload = json.loads(evidence.payload_json)
        if payload.get("evidence_type") != expected_type:
            raise LayerOneIntegrityError("authorization evidence type mismatch")
        if payload.get("data_snapshot_id") != data_snapshot_id:
            raise LayerOneIntegrityError("authorization evidence data_snapshot_id mismatch")
        if _parse_utc_iso(str(payload.get("recorded_at_utc"))) > _parse_utc_iso(authorized_at_utc):
            raise LayerOneIntegrityError("authorization evidence recorded after authorization")
        if expected_type == "historical_validation_pass":
            if payload.get("historical_validation_pass") is not True:
                raise LayerOneIntegrityError("historical_validation evidence flag invalid")
            return
        if payload.get("no_severe_anomaly") is not True:
            raise LayerOneIntegrityError("no_severe_anomaly evidence flag invalid")
        if stage_started_at_utc is None:
            raise LayerOneIntegrityError("stage start required for anomaly evidence coverage")
        observed_from = date.fromisoformat(str(payload.get("observed_from")))
        observed_through = date.fromisoformat(str(payload.get("observed_through")))
        stage_day = _parse_utc_iso(stage_started_at_utc).date()
        auth_day = _parse_utc_iso(authorized_at_utc).date()
        if observed_from > stage_day or observed_through < auth_day:
            raise LayerOneIntegrityError("no_severe_anomaly evidence coverage does not match auth stage")

    def _verify_unlock_row(
        self,
        row: LayerOneUnlockRequestRow,
        *,
        audits: list[LayerOneAuditRecord],
    ) -> None:
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError as exc:
            raise LayerOneIntegrityError("unlock payload_json invalid") from exc
        if not isinstance(payload, dict):
            raise LayerOneIntegrityError("unlock payload must be an object")
        expected = {
            "request_id": row.request_id,
            "operator": row.operator,
            "reason": row.reason,
            "user_confirmed": row.user_confirmed,
            "two_layer_decision_contract_id": row.two_layer_decision_contract_id,
            "layer_one_index_protocol_id": row.layer_one_index_protocol_id,
            "data_snapshot_id": row.data_snapshot_id,
            "evidence_id": row.evidence_id,
            "request_digest": row.request_digest,
            "resulting_state_id": row.resulting_state_id,
            "resulting_revision": row.resulting_revision,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise LayerOneIntegrityError(f"unlock column/payload mismatch for {key}")
        expected_request = LayerOneUnlockRequest(
            request_id=row.request_id,
            operator=row.operator,
            reason=row.reason,
            requested_at=_parse_utc_iso(row.requested_at_utc),
            user_confirmed=row.user_confirmed,
        )
        if expected_request.model_dump(mode="json")["requested_at"] != payload.get("requested_at"):
            raise LayerOneIntegrityError("unlock requested_at column/payload mismatch")
        if row.stream_name != self.stream_name:
            raise LayerOneIntegrityError("unlock stream mismatch")
        if compute_unlock_request_evidence_id(expected_request) != row.evidence_id:
            raise LayerOneIntegrityError("unlock evidence reseal failed")
        session = Session.object_session(row)
        assert session is not None
        audit_row = session.get(LayerOneAuditRecord, row.audit_id)
        if audit_row is None or audit_row.unlock_request_id != row.request_id:
            raise LayerOneIntegrityError("unlock audit binding broken")
        if audit_row.revision_after != row.resulting_revision:
            raise LayerOneIntegrityError("unlock resulting_revision does not match audit")

        referencing = [
            a
            for a in audits
            if a.event_type == _AuditEventKind.DECISION.value and a.unlock_request_id == row.request_id
        ]
        if not row.consumed:
            if referencing:
                raise LayerOneIntegrityError("unused unlock request is referenced by a decision audit")
            if row.consumed_by_decision_id is not None:
                raise LayerOneIntegrityError("unused unlock request has consumed_by_decision_id set")
        else:
            if len(referencing) != 1:
                raise LayerOneIntegrityError("consumed unlock must be referenced by exactly one decision")
            if row.consumed_by_decision_id != referencing[0].decision_id:
                raise LayerOneIntegrityError("consumed_by_decision_id does not match decision audit")

    def _verify_evidence_row(self, row: LayerOneDeploymentEvidenceRow) -> None:
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError as exc:
            raise LayerOneIntegrityError("evidence payload_json invalid") from exc
        if not isinstance(payload, dict):
            raise LayerOneIntegrityError("evidence payload must be an object")
        expected = {
            "stream_name": row.stream_name,
            "request_digest": row.request_digest,
            "evidence_type": row.evidence_type,
            "observed_from": row.observed_from,
            "observed_through": row.observed_through,
            "recorded_at_utc": row.recorded_at_utc,
            "operator": row.operator,
            "summary": row.summary,
            "user_confirmed": row.user_confirmed,
            "contract_schema_version": row.contract_schema_version,
            "two_layer_decision_contract_id": row.two_layer_decision_contract_id,
            "layer_one_index_protocol_id": row.layer_one_index_protocol_id,
            "data_snapshot_id": row.data_snapshot_id,
            "historical_validation_pass": row.historical_validation_pass,
            "no_severe_anomaly": row.no_severe_anomaly,
            "resulting_state_id": row.resulting_state_id,
            "resulting_revision": row.resulting_revision,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise LayerOneIntegrityError(f"evidence column/payload mismatch for {key}")
        if row.stream_name != self.stream_name:
            raise LayerOneIntegrityError("evidence stream mismatch")
        if sha256_hex(payload) != row.evidence_id:
            raise LayerOneIntegrityError("evidence_id does not match sealed payload")
        request_only = {
            "evidence_type": payload.get("evidence_type"),
            "observed_from": payload.get("observed_from"),
            "observed_through": payload.get("observed_through"),
            "recorded_at_utc": payload.get("recorded_at_utc"),
            "operator": payload.get("operator"),
            "summary": payload.get("summary"),
            "user_confirmed": payload.get("user_confirmed"),
            "contract_schema_version": payload.get("contract_schema_version"),
            "two_layer_decision_contract_id": payload.get("two_layer_decision_contract_id"),
            "layer_one_index_protocol_id": payload.get("layer_one_index_protocol_id"),
            "data_snapshot_id": payload.get("data_snapshot_id"),
            "historical_validation_pass": payload.get("historical_validation_pass"),
            "no_severe_anomaly": payload.get("no_severe_anomaly"),
        }
        if sha256_hex(request_only) != row.request_digest:
            raise LayerOneIntegrityError("evidence request_digest does not reseal")
        session = Session.object_session(row)
        assert session is not None
        audit_row = session.get(LayerOneAuditRecord, row.audit_id)
        if audit_row is None or audit_row.evidence_id != row.evidence_id:
            raise LayerOneIntegrityError("evidence audit binding broken")
        if audit_row.revision_after != row.resulting_revision:
            raise LayerOneIntegrityError("evidence resulting_revision does not match audit")

    def get_risk_state(self) -> LayerOneRiskStateView:
        with Session(self.engine) as session:
            current = self.verify_storage_integrity(session)
            if current is None:
                return _uninitialized_view(stream_name=self.stream_name)
            return _state_view(current)

    def initialize(self, request: LayerOneInitializeRequest) -> LayerOneMutationReceipt:
        now = self._now()
        initialized_at = _require_aware(request.initialized_at, field_name="initialized_at", now=now)
        if not request.user_confirmed:
            raise LayerOnePersistenceError("initialization requires user_confirmed=true")
        _assert_bound_contract_ids(
            two_layer_decision_contract_id=request.two_layer_decision_contract_id,
            layer_one_index_protocol_id=request.layer_one_index_protocol_id,
        )
        init_body = {
            "operator": request.operator,
            "reason": request.reason,
            "initialized_at_utc": _to_utc_iso(initialized_at),
            "user_confirmed": True,
            "two_layer_decision_contract_id": request.two_layer_decision_contract_id,
            "layer_one_index_protocol_id": request.layer_one_index_protocol_id,
            "data_snapshot_id": request.data_snapshot_id,
            "contract_schema_version": request.contract_schema_version,
            "engine_version": request.engine_version,
            "applied_stock_budget_bp": 0,
            "manual_ceiling_bp": 0,
        }
        init_digest = sha256_hex(init_body)

        with Session(self.engine) as session:
            existing = self.verify_storage_integrity(session)
            if existing is not None:
                if existing.init_request_digest != init_digest:
                    raise LayerOneConflictError("layer-one stream already initialized with a divergent request")
                init_audit = session.get(LayerOneAuditRecord, existing.init_audit_id)
                if init_audit is None:
                    raise LayerOneIntegrityError("missing initialize audit for idempotent replay")
                stored_body = (json.loads(init_audit.payload_json).get("body") or {}).copy()
                stored_body.pop("authorization_id", None)
                stored_body.pop("state_id", None)
                if sha256_hex(stored_body) != init_digest:
                    raise LayerOneIntegrityError("stored initialize payload digest mismatch")
                return LayerOneMutationReceipt(
                    stream_name=self.stream_name,
                    event_type="initialize",
                    audit_id=existing.init_audit_id,
                    revision=1,
                    authorization_id=existing.init_authorization_id,
                    state_id=existing.init_state_id,
                    idempotent_replay=True,
                )

            sealed_state = seal_layer_one_regime_state(
                LayerOneRegimeNewState(
                    applied_stock_budget=0.0,
                    risk_lock_active=False,
                    risk_lock_triggered_as_of=None,
                    red_line_breached=False,
                )
            )
            assert sealed_state.state_id is not None
            auth_body = {
                "request_id": "__initialize__",
                "request_digest": init_digest,
                "ceiling_bp": 0,
                "prior_ceiling_bp": 0,
                "authorized_at_utc": _to_utc_iso(initialized_at),
                "operator": request.operator,
                "reason": request.reason,
                "user_confirmed": True,
                "contract_schema_version": request.contract_schema_version,
                "two_layer_decision_contract_id": request.two_layer_decision_contract_id,
                "layer_one_index_protocol_id": request.layer_one_index_protocol_id,
                "data_snapshot_id": request.data_snapshot_id,
                "historical_validation_evidence_id": None,
                "no_severe_anomaly_evidence_id": None,
                "resulting_state_id": sealed_state.state_id,
                "resulting_revision": 1,
                "source": "initialize",
            }
            authorization_id = sha256_hex(auth_body)
            try:
                audit = _append_audit(
                    session,
                    stream_name=self.stream_name,
                    event_type=_AuditEventKind.INITIALIZE.value,
                    recorded_at=initialized_at,
                    body={**init_body, "authorization_id": authorization_id, "state_id": sealed_state.state_id},
                    prior_audit_id=None,
                    sequence_no=1,
                    revision_after=1,
                    authorization_id=authorization_id,
                )
                session.add(
                    LayerOneManualCeilingAuthorizationRow(
                        authorization_id=authorization_id,
                        stream_name=self.stream_name,
                        request_id="__initialize__",
                        request_digest=init_digest,
                        ceiling_bp=0,
                        prior_ceiling_bp=0,
                        authorized_at_utc=_to_utc_iso(initialized_at),
                        operator=request.operator,
                        reason=request.reason,
                        user_confirmed=True,
                        contract_schema_version=request.contract_schema_version,
                        two_layer_decision_contract_id=request.two_layer_decision_contract_id,
                        layer_one_index_protocol_id=request.layer_one_index_protocol_id,
                        data_snapshot_id=request.data_snapshot_id,
                        historical_validation_evidence_id=None,
                        no_severe_anomaly_evidence_id=None,
                        resulting_state_id=sealed_state.state_id,
                        resulting_revision=1,
                        audit_id=audit.audit_id,
                        payload_json=canonical_json_bytes(auth_body).decode("utf-8"),
                    )
                )
                session.add(
                    LayerOneCurrentState(
                        stream_name=self.stream_name,
                        revision=1,
                        state_id=sealed_state.state_id,
                        applied_stock_budget_bp=0,
                        risk_lock_active=False,
                        risk_lock_triggered_as_of=None,
                        red_line_breached=False,
                        manual_ceiling_bp=0,
                        manual_ceiling_authorization_id=authorization_id,
                        manual_ceiling_stage_started_at_utc=_to_utc_iso(initialized_at),
                        last_decision_id=None,
                        last_decision_target_trading_day=None,
                        last_audit_id=audit.audit_id,
                        audit_sequence_no=1,
                        two_layer_decision_contract_id=request.two_layer_decision_contract_id,
                        layer_one_index_protocol_id=request.layer_one_index_protocol_id,
                        data_snapshot_id=request.data_snapshot_id,
                        initialized_at_utc=_to_utc_iso(initialized_at),
                        init_request_digest=init_digest,
                        init_audit_id=audit.audit_id,
                        init_authorization_id=authorization_id,
                        init_state_id=sealed_state.state_id,
                        state_json=canonical_json_bytes(sealed_state.model_dump(mode="json")).decode("utf-8"),
                        updated_at_utc=_to_utc_iso(initialized_at),
                    )
                )
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise LayerOneConflictError("initialization conflict") from exc
            return LayerOneMutationReceipt(
                stream_name=self.stream_name,
                event_type="initialize",
                audit_id=audit.audit_id,
                revision=1,
                authorization_id=authorization_id,
                state_id=sealed_state.state_id,
                idempotent_replay=False,
            )

    def register_deployment_evidence(
        self,
        request: LayerOneDeploymentEvidenceRequest,
    ) -> LayerOneMutationReceipt:
        now = self._now()
        recorded_at = _require_aware(request.recorded_at, field_name="recorded_at", now=now)
        if request.observed_through > recorded_at.date():
            raise LayerOnePersistenceError("observed_through must be <= recorded_at date")
        _assert_bound_contract_ids(
            two_layer_decision_contract_id=request.two_layer_decision_contract_id,
            layer_one_index_protocol_id=request.layer_one_index_protocol_id,
        )
        request_digest = sha256_hex(_evidence_canonical_body(request))

        with Session(self.engine) as session:
            current = self.verify_storage_integrity(session)
            if current is None:
                raise LayerOneNotInitializedError("layer-one stream is uninitialized")
            existing = session.scalars(
                select(LayerOneDeploymentEvidenceRow).where(
                    LayerOneDeploymentEvidenceRow.stream_name == self.stream_name,
                    LayerOneDeploymentEvidenceRow.request_digest == request_digest,
                )
            ).one_or_none()
            if existing is not None:
                return LayerOneMutationReceipt(
                    stream_name=self.stream_name,
                    event_type="deployment_evidence",
                    audit_id=existing.audit_id,
                    revision=existing.resulting_revision,
                    evidence_id=existing.evidence_id,
                    state_id=existing.resulting_state_id,
                    idempotent_replay=True,
                )

            _require_monotonic(
                stamp=recorded_at,
                current_updated_at=_parse_utc_iso(current.updated_at_utc),
                field_name="recorded_at",
            )
            new_revision = current.revision + 1
            seq = current.audit_sequence_no + 1
            body = {
                **_evidence_canonical_body(request),
                "stream_name": self.stream_name,
                "request_digest": request_digest,
                "resulting_state_id": current.state_id,
                "resulting_revision": new_revision,
            }
            evidence_id = sha256_hex(body)
            try:
                audit = _append_audit(
                    session,
                    stream_name=self.stream_name,
                    event_type=_AuditEventKind.DEPLOYMENT_EVIDENCE.value,
                    recorded_at=recorded_at,
                    body=body,
                    prior_audit_id=current.last_audit_id,
                    sequence_no=seq,
                    revision_after=new_revision,
                    evidence_id=evidence_id,
                )
                session.add(
                    LayerOneDeploymentEvidenceRow(
                        evidence_id=evidence_id,
                        stream_name=self.stream_name,
                        request_digest=request_digest,
                        evidence_type=request.evidence_type,
                        observed_from=request.observed_from.isoformat(),
                        observed_through=request.observed_through.isoformat(),
                        recorded_at_utc=_to_utc_iso(recorded_at),
                        operator=request.operator,
                        summary=request.summary,
                        user_confirmed=True,
                        contract_schema_version=request.contract_schema_version,
                        two_layer_decision_contract_id=request.two_layer_decision_contract_id,
                        layer_one_index_protocol_id=request.layer_one_index_protocol_id,
                        data_snapshot_id=request.data_snapshot_id,
                        historical_validation_pass=request.historical_validation_pass,
                        no_severe_anomaly=request.no_severe_anomaly,
                        resulting_state_id=current.state_id,
                        resulting_revision=new_revision,
                        audit_id=audit.audit_id,
                        payload_json=canonical_json_bytes(body).decode("utf-8"),
                    )
                )
                _cas_update_current(
                    session,
                    stream_name=self.stream_name,
                    expected_revision=current.revision,
                    expected_last_audit_id=current.last_audit_id,
                    expected_state_id=current.state_id,
                    values={
                        "revision": new_revision,
                        "last_audit_id": audit.audit_id,
                        "audit_sequence_no": seq,
                        "updated_at_utc": _to_utc_iso(recorded_at),
                    },
                )
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise LayerOneConflictError("deployment evidence conflict") from exc
            except LayerOneConflictError:
                session.rollback()
                raise
            return LayerOneMutationReceipt(
                stream_name=self.stream_name,
                event_type="deployment_evidence",
                audit_id=audit.audit_id,
                revision=new_revision,
                evidence_id=evidence_id,
                state_id=current.state_id,
                idempotent_replay=False,
            )

    def authorize_manual_ceiling(
        self,
        request: LayerOneManualCeilingAuthorizationRequest,
    ) -> LayerOneMutationReceipt:
        now = self._now()
        authorized_at = _require_aware(request.authorized_at, field_name="authorized_at", now=now)
        if not request.user_confirmed:
            raise LayerOnePersistenceError("manual ceiling authorization requires user_confirmed=true")
        if request.auto_upgrade is not False:
            raise LayerOnePersistenceError("automatic ceiling upgrade is forbidden")
        _assert_bound_contract_ids(
            two_layer_decision_contract_id=request.two_layer_decision_contract_id,
            layer_one_index_protocol_id=request.layer_one_index_protocol_id,
        )
        new_bp = budget_to_bp(request.ceiling)
        request_digest = _manual_request_digest(request)

        with Session(self.engine) as session:
            current = self.verify_storage_integrity(session)
            if current is None:
                raise LayerOneNotInitializedError("layer-one stream is uninitialized")

            existing = session.scalars(
                select(LayerOneManualCeilingAuthorizationRow).where(
                    LayerOneManualCeilingAuthorizationRow.stream_name == self.stream_name,
                    LayerOneManualCeilingAuthorizationRow.request_id == request.request_id,
                )
            ).one_or_none()
            if existing is not None:
                if existing.request_digest != request_digest:
                    raise LayerOneConflictError("manual ceiling request_id exists with divergent content")
                return LayerOneMutationReceipt(
                    stream_name=self.stream_name,
                    event_type="manual_ceiling_authorization",
                    audit_id=existing.audit_id,
                    revision=existing.resulting_revision,
                    authorization_id=existing.authorization_id,
                    state_id=existing.resulting_state_id,
                    idempotent_replay=True,
                )

            _require_monotonic(
                stamp=authorized_at,
                current_updated_at=_parse_utc_iso(current.updated_at_utc),
                field_name="authorized_at",
            )
            prior_bp = current.manual_ceiling_bp
            if new_bp > prior_bp:
                self._assert_upgrade_allowed(
                    session,
                    current=current,
                    prior_bp=prior_bp,
                    new_bp=new_bp,
                    request=request,
                    authorized_at=authorized_at,
                )

            new_revision = current.revision + 1
            seq = current.audit_sequence_no + 1
            auth_body = {
                "request_id": request.request_id,
                "request_digest": request_digest,
                "ceiling_bp": new_bp,
                "prior_ceiling_bp": prior_bp,
                "authorized_at_utc": _to_utc_iso(authorized_at),
                "operator": request.operator,
                "reason": request.reason,
                "user_confirmed": True,
                "contract_schema_version": request.contract_schema_version,
                "two_layer_decision_contract_id": request.two_layer_decision_contract_id,
                "layer_one_index_protocol_id": request.layer_one_index_protocol_id,
                "data_snapshot_id": request.data_snapshot_id,
                "historical_validation_evidence_id": request.historical_validation_evidence_id,
                "no_severe_anomaly_evidence_id": request.no_severe_anomaly_evidence_id,
                "resulting_state_id": current.state_id,
                "resulting_revision": new_revision,
                "source": "manual_authorization",
            }
            authorization_id = sha256_hex(auth_body)
            try:
                audit = _append_audit(
                    session,
                    stream_name=self.stream_name,
                    event_type=_AuditEventKind.MANUAL_CEILING.value,
                    recorded_at=authorized_at,
                    body=auth_body,
                    prior_audit_id=current.last_audit_id,
                    sequence_no=seq,
                    revision_after=new_revision,
                    authorization_id=authorization_id,
                )
                session.add(
                    LayerOneManualCeilingAuthorizationRow(
                        authorization_id=authorization_id,
                        stream_name=self.stream_name,
                        request_id=request.request_id,
                        request_digest=request_digest,
                        ceiling_bp=new_bp,
                        prior_ceiling_bp=prior_bp,
                        authorized_at_utc=_to_utc_iso(authorized_at),
                        operator=request.operator,
                        reason=request.reason,
                        user_confirmed=True,
                        contract_schema_version=request.contract_schema_version,
                        two_layer_decision_contract_id=request.two_layer_decision_contract_id,
                        layer_one_index_protocol_id=request.layer_one_index_protocol_id,
                        data_snapshot_id=request.data_snapshot_id,
                        historical_validation_evidence_id=request.historical_validation_evidence_id,
                        no_severe_anomaly_evidence_id=request.no_severe_anomaly_evidence_id,
                        resulting_state_id=current.state_id,
                        resulting_revision=new_revision,
                        audit_id=audit.audit_id,
                        payload_json=canonical_json_bytes(auth_body).decode("utf-8"),
                    )
                )
                _cas_update_current(
                    session,
                    stream_name=self.stream_name,
                    expected_revision=current.revision,
                    expected_last_audit_id=current.last_audit_id,
                    expected_state_id=current.state_id,
                    values={
                        "revision": new_revision,
                        "manual_ceiling_bp": new_bp,
                        "manual_ceiling_authorization_id": authorization_id,
                        "manual_ceiling_stage_started_at_utc": _to_utc_iso(authorized_at),
                        "last_audit_id": audit.audit_id,
                        "audit_sequence_no": seq,
                        "data_snapshot_id": request.data_snapshot_id,
                        "updated_at_utc": _to_utc_iso(authorized_at),
                    },
                )
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise LayerOneConflictError("manual ceiling authorization conflict") from exc
            except LayerOneConflictError:
                session.rollback()
                raise
            return LayerOneMutationReceipt(
                stream_name=self.stream_name,
                event_type="manual_ceiling_authorization",
                audit_id=audit.audit_id,
                revision=new_revision,
                authorization_id=authorization_id,
                state_id=current.state_id,
                idempotent_replay=False,
            )

    def _load_verified_evidence(
        self,
        session: Session,
        *,
        evidence_id: str,
        expected_type: EvidenceType,
        data_snapshot_id: str,
        authorized_at: datetime,
        stage_started_at: datetime | None,
    ) -> LayerOneDeploymentEvidenceRow:
        row = session.get(LayerOneDeploymentEvidenceRow, evidence_id)
        if row is None or row.stream_name != self.stream_name:
            raise LayerOnePersistenceError("unknown deployment evidence_id")
        self._verify_evidence_row(row)
        payload = json.loads(row.payload_json)
        if payload.get("evidence_type") != expected_type:
            raise LayerOnePersistenceError("deployment evidence type mismatch")
        if payload.get("data_snapshot_id") != data_snapshot_id:
            raise LayerOnePersistenceError("deployment evidence data_snapshot_id mismatch")
        if payload.get("two_layer_decision_contract_id") != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
            raise LayerOnePersistenceError("deployment evidence contract id mismatch")
        if payload.get("layer_one_index_protocol_id") != BOUND_LAYER_ONE_INDEX_PROTOCOL_ID:
            raise LayerOnePersistenceError("deployment evidence protocol id mismatch")
        recorded_at = _parse_utc_iso(str(payload.get("recorded_at_utc")))
        if recorded_at > authorized_at:
            raise LayerOnePersistenceError("evidence recorded_at must be <= authorization authorized_at")
        if expected_type == "historical_validation_pass":
            if payload.get("historical_validation_pass") is not True:
                raise LayerOnePersistenceError("historical validation evidence flag invalid")
        else:
            if payload.get("no_severe_anomaly") is not True:
                raise LayerOnePersistenceError("no_severe_anomaly evidence flag invalid")
            if stage_started_at is None:
                raise LayerOnePersistenceError("stage start required for anomaly evidence coverage")
            observed_from = date.fromisoformat(str(payload.get("observed_from")))
            observed_through = date.fromisoformat(str(payload.get("observed_through")))
            stage_day = stage_started_at.astimezone(UTC).date()
            auth_day = authorized_at.astimezone(UTC).date()
            if observed_from > stage_day or observed_through < auth_day:
                raise LayerOnePersistenceError(
                    "no_severe_anomaly evidence must cover the entire current stage through authorized_at"
                )
        return row

    def _assert_upgrade_allowed(
        self,
        session: Session,
        *,
        current: LayerOneCurrentState,
        prior_bp: int,
        new_bp: int,
        request: LayerOneManualCeilingAuthorizationRequest,
        authorized_at: datetime,
    ) -> None:
        if current.risk_lock_active:
            raise LayerOnePersistenceError("manual ceiling upgrade forbidden while risk lock is active")
        allowed_steps = {(0, 3000), (3000, 6000), (6000, 9000)}
        if (prior_bp, new_bp) not in allowed_steps:
            raise LayerOnePersistenceError("manual ceiling upgrade may not skip levels")
        stage_started = _parse_utc_iso(current.manual_ceiling_stage_started_at_utc)
        if new_bp == 3000:
            if request.historical_validation_evidence_id is None:
                raise LayerOnePersistenceError("0->0.3 upgrade requires historical_validation_evidence_id")
            self._load_verified_evidence(
                session,
                evidence_id=request.historical_validation_evidence_id,
                expected_type="historical_validation_pass",
                data_snapshot_id=request.data_snapshot_id,
                authorized_at=authorized_at,
                stage_started_at=None,
            )
            return
        if new_bp == 6000:
            if request.no_severe_anomaly_evidence_id is None:
                raise LayerOnePersistenceError("0.3->0.6 upgrade requires no_severe_anomaly_evidence_id")
            earliest = add_calendar_months(stage_started, MIN_MONTHS_AT_PRIOR_STAGE)
            if authorized_at < earliest:
                raise LayerOnePersistenceError("0.3->0.6 upgrade requires at least 3 calendar months at prior stage")
            self._load_verified_evidence(
                session,
                evidence_id=request.no_severe_anomaly_evidence_id,
                expected_type="no_severe_anomaly_period",
                data_snapshot_id=request.data_snapshot_id,
                authorized_at=authorized_at,
                stage_started_at=stage_started,
            )
            return
        if new_bp == 9000:
            earliest = add_calendar_months(stage_started, MIN_MONTHS_AT_PRIOR_STAGE)
            if authorized_at < earliest:
                raise LayerOnePersistenceError("0.6->0.9 upgrade requires at least 3 calendar months at prior stage")
            if self._risk_lock_triggered_during_stage(session, stage_started_at=stage_started):
                raise LayerOnePersistenceError(
                    "0.6->0.9 upgrade forbidden because a risk lock was triggered during the 0.6 stage"
                )
            return
        raise LayerOnePersistenceError("unsupported manual ceiling upgrade")

    def _risk_lock_triggered_during_stage(
        self,
        session: Session,
        *,
        stage_started_at: datetime,
    ) -> bool:
        stage_iso = _to_utc_iso(stage_started_at)
        rows = session.scalars(
            select(LayerOneAuditRecord).where(
                LayerOneAuditRecord.stream_name == self.stream_name,
                LayerOneAuditRecord.event_type == _AuditEventKind.DECISION.value,
                LayerOneAuditRecord.recorded_at_utc >= stage_iso,
            )
        ).all()
        for row in rows:
            payload = json.loads(row.payload_json)
            body = payload.get("body") or {}
            if body.get("risk_lock_triggered_this_decision") is True:
                return True
        return False

    def submit_unlock_request(self, submission: LayerOneUnlockRequestSubmission) -> LayerOneMutationReceipt:
        now = self._now()
        request = submission.request
        requested_at = _require_aware(request.requested_at, field_name="requested_at", now=now)
        if not request.user_confirmed:
            raise LayerOnePersistenceError("unlock request requires user_confirmed=true")
        _assert_bound_contract_ids(
            two_layer_decision_contract_id=submission.two_layer_decision_contract_id,
            layer_one_index_protocol_id=submission.layer_one_index_protocol_id,
        )
        evidence_id = compute_unlock_request_evidence_id(request)
        request_digest = _unlock_submission_digest(submission)

        with Session(self.engine) as session:
            current = self.verify_storage_integrity(session)
            if current is None:
                raise LayerOneNotInitializedError("layer-one stream is uninitialized")

            existing = session.scalars(
                select(LayerOneUnlockRequestRow).where(
                    LayerOneUnlockRequestRow.stream_name == self.stream_name,
                    LayerOneUnlockRequestRow.request_id == request.request_id,
                )
            ).one_or_none()
            if existing is not None:
                if existing.request_digest != request_digest or existing.evidence_id != evidence_id:
                    raise LayerOneConflictError("unlock request_id already exists with divergent content")
                return LayerOneMutationReceipt(
                    stream_name=self.stream_name,
                    event_type="unlock_request",
                    audit_id=existing.audit_id,
                    revision=existing.resulting_revision,
                    unlock_request_id=request.request_id,
                    unlock_evidence_id=evidence_id,
                    state_id=existing.resulting_state_id,
                    idempotent_replay=True,
                )

            if not current.risk_lock_active:
                raise LayerOnePersistenceError("unlock request rejected because no risk lock is active")
            if current.risk_lock_triggered_as_of is None:
                raise LayerOnePersistenceError("active risk lock missing triggered_as_of")
            lock_day = date.fromisoformat(current.risk_lock_triggered_as_of)
            if requested_at.date() < lock_day:
                raise LayerOnePersistenceError("unlock requested_at precedes risk_lock_triggered_as_of")
            _require_monotonic(
                stamp=requested_at,
                current_updated_at=_parse_utc_iso(current.updated_at_utc),
                field_name="requested_at",
            )

            new_revision = current.revision + 1
            seq = current.audit_sequence_no + 1
            body = {
                **request.model_dump(mode="json"),
                "two_layer_decision_contract_id": submission.two_layer_decision_contract_id,
                "layer_one_index_protocol_id": submission.layer_one_index_protocol_id,
                "data_snapshot_id": submission.data_snapshot_id,
                "evidence_id": evidence_id,
                "request_digest": request_digest,
                "resulting_state_id": current.state_id,
                "resulting_revision": new_revision,
            }
            try:
                audit = _append_audit(
                    session,
                    stream_name=self.stream_name,
                    event_type=_AuditEventKind.UNLOCK_REQUEST.value,
                    recorded_at=requested_at,
                    body=body,
                    prior_audit_id=current.last_audit_id,
                    sequence_no=seq,
                    revision_after=new_revision,
                    unlock_request_id=request.request_id,
                )
                session.add(
                    LayerOneUnlockRequestRow(
                        stream_name=self.stream_name,
                        request_id=request.request_id,
                        request_digest=request_digest,
                        evidence_id=evidence_id,
                        operator=request.operator,
                        reason=request.reason,
                        requested_at_utc=_to_utc_iso(requested_at),
                        user_confirmed=True,
                        two_layer_decision_contract_id=submission.two_layer_decision_contract_id,
                        layer_one_index_protocol_id=submission.layer_one_index_protocol_id,
                        data_snapshot_id=submission.data_snapshot_id,
                        consumed=False,
                        consumed_by_decision_id=None,
                        resulting_state_id=current.state_id,
                        resulting_revision=new_revision,
                        audit_id=audit.audit_id,
                        payload_json=canonical_json_bytes(body).decode("utf-8"),
                    )
                )
                _cas_update_current(
                    session,
                    stream_name=self.stream_name,
                    expected_revision=current.revision,
                    expected_last_audit_id=current.last_audit_id,
                    expected_state_id=current.state_id,
                    values={
                        "revision": new_revision,
                        "last_audit_id": audit.audit_id,
                        "audit_sequence_no": seq,
                        "updated_at_utc": _to_utc_iso(requested_at),
                    },
                )
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise LayerOneConflictError("unlock request conflict") from exc
            except LayerOneConflictError:
                session.rollback()
                raise
            return LayerOneMutationReceipt(
                stream_name=self.stream_name,
                event_type="unlock_request",
                audit_id=audit.audit_id,
                revision=new_revision,
                unlock_request_id=request.request_id,
                unlock_evidence_id=evidence_id,
                state_id=current.state_id,
                idempotent_replay=False,
            )

    def commit_decision(self, request: LayerOneDecisionCommitRequest) -> LayerOneMutationReceipt:
        report = verify_persisted_layer_one_decision(request.report, repo_root=self.repo_root)
        if report.decision_id is None:
            raise LayerOnePersistenceError("decision_id is required")
        decision_id = report.decision_id
        evaluated_at = _require_aware(report.evaluated_at, field_name="evaluated_at", now=self._now())

        with Session(self.engine) as session:
            current = self.verify_storage_integrity(session)
            if current is None:
                raise LayerOneNotInitializedError("layer-one stream is uninitialized")

            prior_audit = session.scalars(
                select(LayerOneAuditRecord).where(
                    LayerOneAuditRecord.stream_name == self.stream_name,
                    LayerOneAuditRecord.decision_id == decision_id,
                )
            ).one_or_none()
            if prior_audit is not None:
                prior_payload = json.loads(prior_audit.payload_json)
                prior_decision = (prior_payload.get("body") or {}).get("decision")
                if prior_decision == report.model_dump(mode="json"):
                    new_state_id = (prior_payload.get("body") or {}).get("new_state_id")
                    return LayerOneMutationReceipt(
                        stream_name=self.stream_name,
                        event_type="decision",
                        audit_id=prior_audit.audit_id,
                        revision=prior_audit.revision_after,
                        decision_id=decision_id,
                        unlock_request_id=report.unlock_request_id,
                        state_id=new_state_id,
                        idempotent_replay=True,
                    )
                raise LayerOneConflictError("divergent decision submitted for existing decision_id")

            if request.expected_revision != current.revision or request.expected_last_audit_id != current.last_audit_id:
                raise LayerOneConflictError("stale decision CAS envelope")
            if report.prior_state_id != current.state_id:
                raise LayerOneConflictError("stale prior_state_id; compare-and-swap rejected")
            if report.manual_ceiling_authorization_id != current.manual_ceiling_authorization_id:
                raise LayerOnePersistenceError("manual_ceiling_authorization_id does not match current authorization")
            if budget_to_bp(report.manual_open_ceiling) != current.manual_ceiling_bp:
                raise LayerOnePersistenceError("manual_open_ceiling does not match current authorized ceiling")
            _require_monotonic(
                stamp=evaluated_at,
                current_updated_at=_parse_utc_iso(current.updated_at_utc),
                field_name="evaluated_at",
            )
            if current.last_decision_target_trading_day is not None:
                last_target = date.fromisoformat(current.last_decision_target_trading_day)
                if report.target_trading_day <= last_target:
                    raise LayerOneConflictError(
                        "target_trading_day must be strictly later than last accepted decision target"
                    )

            persisted_prior = LayerOneRegimePriorState(
                applied_stock_budget=bp_to_budget(current.applied_stock_budget_bp),
                risk_lock_active=current.risk_lock_active,
                risk_lock_triggered_as_of=(
                    date.fromisoformat(current.risk_lock_triggered_as_of) if current.risk_lock_triggered_as_of else None
                ),
                red_line_breached=current.red_line_breached,
                state_id=current.state_id,
            )
            if compute_state_id(persisted_prior) != current.state_id:
                raise LayerOnePersistenceError("persisted state_id does not match canonical content")
            if report.previous_applied_stock_budget != persisted_prior.applied_stock_budget:
                raise LayerOnePersistenceError("decision previous_applied_stock_budget does not match persisted state")
            if report.risk_lock_prior_active != persisted_prior.risk_lock_active:
                raise LayerOnePersistenceError("decision risk_lock_prior_active does not match persisted state")
            if report.prior_risk_lock_triggered_as_of != persisted_prior.risk_lock_triggered_as_of:
                raise LayerOnePersistenceError(
                    "decision prior_risk_lock_triggered_as_of does not match persisted state"
                )
            if report.prior_red_line_breached != persisted_prior.red_line_breached:
                raise LayerOnePersistenceError("decision prior_red_line_breached does not match persisted state")

            if report.unlock_request_id is not None:
                unlock_row = session.scalars(
                    select(LayerOneUnlockRequestRow).where(
                        LayerOneUnlockRequestRow.stream_name == self.stream_name,
                        LayerOneUnlockRequestRow.request_id == report.unlock_request_id,
                    )
                ).one_or_none()
                if unlock_row is None:
                    raise LayerOnePersistenceError("referenced unlock request is missing")
                if unlock_row.consumed:
                    raise LayerOneConflictError("referenced unlock request was already consumed")
                report_requested = report.unlock_requested_at
                if report_requested is None:
                    raise LayerOnePersistenceError("unlock_requested_at missing on decision")
                stored_requested = _parse_utc_iso(unlock_row.requested_at_utc)
                if (
                    report.unlock_request_evidence_id != unlock_row.evidence_id
                    or report.unlock_operator != unlock_row.operator
                    or report.unlock_reason != unlock_row.reason
                    or report_requested.astimezone(UTC) != stored_requested.astimezone(UTC)
                    or report.unlock_user_confirmed != unlock_row.user_confirmed
                ):
                    raise LayerOneConflictError("referenced unlock request does not match persisted request")
                unlock_row.consumed = True
                unlock_row.consumed_by_decision_id = decision_id
                session.add(unlock_row)

            new_state = report.new_state
            if new_state.state_id is None:
                raise LayerOnePersistenceError("new_state.state_id is required")
            new_revision = current.revision + 1
            seq = current.audit_sequence_no + 1
            body = {
                "decision": report.model_dump(mode="json"),
                "decision_digest": sha256_hex(report.model_dump(mode="json")),
                "prior_state_id": current.state_id,
                "new_state_id": new_state.state_id,
                "risk_lock_triggered_this_decision": report.risk_lock_triggered_this_decision,
                "risk_lock_unlocked_this_decision": report.risk_lock_unlocked_this_decision,
                "manual_ceiling_authorization_id": report.manual_ceiling_authorization_id,
                "target_trading_day": report.target_trading_day.isoformat(),
            }
            try:
                audit = _append_audit(
                    session,
                    stream_name=self.stream_name,
                    event_type=_AuditEventKind.DECISION.value,
                    recorded_at=evaluated_at,
                    body=body,
                    prior_audit_id=current.last_audit_id,
                    sequence_no=seq,
                    revision_after=new_revision,
                    decision_id=decision_id,
                    unlock_request_id=report.unlock_request_id,
                )
                _cas_update_current(
                    session,
                    stream_name=self.stream_name,
                    expected_revision=current.revision,
                    expected_last_audit_id=current.last_audit_id,
                    expected_state_id=current.state_id,
                    values={
                        "revision": new_revision,
                        "state_id": new_state.state_id,
                        "applied_stock_budget_bp": budget_to_bp(new_state.applied_stock_budget),
                        "risk_lock_active": new_state.risk_lock_active,
                        "risk_lock_triggered_as_of": (
                            new_state.risk_lock_triggered_as_of.isoformat()
                            if new_state.risk_lock_triggered_as_of is not None
                            else None
                        ),
                        "red_line_breached": new_state.red_line_breached,
                        "last_decision_id": decision_id,
                        "last_decision_target_trading_day": report.target_trading_day.isoformat(),
                        "last_audit_id": audit.audit_id,
                        "audit_sequence_no": seq,
                        "state_json": canonical_json_bytes(new_state.model_dump(mode="json")).decode("utf-8"),
                        "updated_at_utc": _to_utc_iso(evaluated_at),
                    },
                )
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise LayerOneConflictError("decision commit conflict") from exc
            except LayerOneConflictError:
                session.rollback()
                raise
            return LayerOneMutationReceipt(
                stream_name=self.stream_name,
                event_type="decision",
                audit_id=audit.audit_id,
                revision=new_revision,
                decision_id=decision_id,
                unlock_request_id=report.unlock_request_id,
                state_id=new_state.state_id,
                idempotent_replay=False,
            )

    def list_audit(
        self,
        *,
        after_sequence: int = 0,
        page_size: int = 50,
    ) -> LayerOneAuditPage:
        if page_size < 1 or page_size > 100:
            raise LayerOnePersistenceError("page_size must be between 1 and 100")
        if after_sequence < 0:
            raise LayerOnePersistenceError("after_sequence must be non-negative")
        with Session(self.engine) as session:
            self.verify_storage_integrity(session)
            rows = session.scalars(
                select(LayerOneAuditRecord)
                .where(
                    LayerOneAuditRecord.stream_name == self.stream_name,
                    LayerOneAuditRecord.sequence_no > after_sequence,
                )
                .order_by(LayerOneAuditRecord.sequence_no.asc())
                .limit(page_size + 1)
            ).all()
            page = rows[:page_size]
            items = [
                {
                    "audit_id": row.audit_id,
                    "sequence_no": row.sequence_no,
                    "prior_audit_id": row.prior_audit_id,
                    "event_type": row.event_type,
                    "recorded_at_utc": row.recorded_at_utc,
                    "payload_digest": row.payload_digest,
                    "payload": json.loads(row.payload_json),
                    "decision_id": row.decision_id,
                    "authorization_id": row.authorization_id,
                    "unlock_request_id": row.unlock_request_id,
                    "evidence_id": row.evidence_id,
                    "revision_after": row.revision_after,
                }
                for row in page
            ]
            next_after = page[-1].sequence_no if len(rows) > page_size else None
            return LayerOneAuditPage(
                stream_name=self.stream_name,
                items=items,
                next_after_sequence=next_after,
                page_size=page_size,
            )


__all__ = [
    "LAYER_ONE_STREAM_NAME",
    "LayerOneAuditPage",
    "LayerOneConflictError",
    "LayerOneDecisionCommitRequest",
    "LayerOneDeploymentEvidenceRequest",
    "LayerOneInitializeRequest",
    "LayerOneIntegrityError",
    "LayerOneManualCeilingAuthorizationRequest",
    "LayerOneMutationReceipt",
    "LayerOneNotFoundError",
    "LayerOneNotInitializedError",
    "LayerOnePersistenceError",
    "LayerOneRiskStateStore",
    "LayerOneRiskStateView",
    "LayerOneUnlockRequestSubmission",
    "add_calendar_months",
    "bp_to_budget",
    "budget_to_bp",
    "compute_deployment_evidence_id",
    "verify_persisted_layer_one_decision",
]
