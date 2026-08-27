"""SQLAlchemy tables for layer-one risk-state persistence (E9b-1).

New create_all tables only. Timestamps are stored as canonical UTC ISO strings
because SQLite DateTime drops timezone. Never default-construct an unlocked state.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.models import Base

LAYER_ONE_STREAM_NAME: str = "layer-one-primary"


class LayerOneCurrentState(Base):
    """Singleton current state for the named layer-one stream."""

    __tablename__ = "layer_one_current_states"

    stream_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state_id: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_stock_budget_bp: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_lock_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    risk_lock_triggered_as_of: Mapped[str | None] = mapped_column(String(32), nullable=True)
    red_line_breached: Mapped[bool] = mapped_column(Boolean, nullable=False)
    manual_ceiling_bp: Mapped[int] = mapped_column(Integer, nullable=False)
    manual_ceiling_authorization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    manual_ceiling_stage_started_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)
    last_decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_decision_target_trading_day: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_audit_id: Mapped[str] = mapped_column(String(64), nullable=False)
    audit_sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    two_layer_decision_contract_id: Mapped[str] = mapped_column(String(64), nullable=False)
    layer_one_index_protocol_id: Mapped[str] = mapped_column(String(64), nullable=False)
    data_snapshot_id: Mapped[str] = mapped_column(Text, nullable=False)
    initialized_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)
    init_request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    init_audit_id: Mapped[str] = mapped_column(String(64), nullable=False)
    init_authorization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    init_state_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)


class LayerOneAuditRecord(Base):
    """Append-only audit chain; prior_audit_id binds order detectably."""

    __tablename__ = "layer_one_audit_records"
    __table_args__ = (UniqueConstraint("stream_name", "sequence_no", name="uq_layer_one_audit_seq"),)

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stream_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    prior_audit_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    authorization_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    unlock_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    evidence_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    revision_after: Mapped[int] = mapped_column(Integer, nullable=False)


class LayerOneManualCeilingAuthorizationRow(Base):
    """Append-only manual ceiling authorization receipts."""

    __tablename__ = "layer_one_manual_ceiling_authorizations"
    __table_args__ = (UniqueConstraint("stream_name", "request_id", name="uq_layer_one_ceiling_request_id"),)

    authorization_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stream_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    ceiling_bp: Mapped[int] = mapped_column(Integer, nullable=False)
    prior_ceiling_bp: Mapped[int] = mapped_column(Integer, nullable=False)
    authorized_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    user_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    contract_schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    two_layer_decision_contract_id: Mapped[str] = mapped_column(String(64), nullable=False)
    layer_one_index_protocol_id: Mapped[str] = mapped_column(String(64), nullable=False)
    data_snapshot_id: Mapped[str] = mapped_column(Text, nullable=False)
    historical_validation_evidence_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    no_severe_anomaly_evidence_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resulting_state_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resulting_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    audit_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)


class LayerOneUnlockRequestRow(Base):
    """Explicit unlock requests; unused until a decision consumes them."""

    __tablename__ = "layer_one_unlock_requests"
    __table_args__ = (
        UniqueConstraint("stream_name", "request_id", name="uq_layer_one_unlock_request_id"),
        UniqueConstraint("stream_name", "evidence_id", name="uq_layer_one_unlock_evidence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stream_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)
    user_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    two_layer_decision_contract_id: Mapped[str] = mapped_column(String(64), nullable=False)
    layer_one_index_protocol_id: Mapped[str] = mapped_column(String(64), nullable=False)
    data_snapshot_id: Mapped[str] = mapped_column(Text, nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consumed_by_decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resulting_state_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resulting_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    audit_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)


class LayerOneDeploymentEvidenceRow(Base):
    """Append-only sealed deployment evidence registry."""

    __tablename__ = "layer_one_deployment_evidence"
    __table_args__ = (UniqueConstraint("stream_name", "request_digest", name="uq_layer_one_evidence_request_digest"),)

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stream_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_from: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_through: Mapped[str] = mapped_column(String(32), nullable=False)
    recorded_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    user_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    contract_schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    two_layer_decision_contract_id: Mapped[str] = mapped_column(String(64), nullable=False)
    layer_one_index_protocol_id: Mapped[str] = mapped_column(String(64), nullable=False)
    data_snapshot_id: Mapped[str] = mapped_column(Text, nullable=False)
    historical_validation_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    no_severe_anomaly: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    resulting_state_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resulting_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    audit_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
