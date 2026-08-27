"""Fail-closed manual input gate between shadow observation and any live action."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.index_shadow_execution import verify_index_shadow_execution_protocol
from app.research.index_shadow_observation import verify_index_shadow_observation_plan
from app.research.repo_file_safety import resolve_repo_regular_file

DEFAULT_LIVE_GATE_PATH = Path("config/research/index-controlled-live-input-gate-v1.json")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class IndexControlledLiveInputGate(_StrictModel):
    schema_version: Literal["1"]
    gate_version: Literal["index-controlled-live-input-gate-v1"]
    gate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_on: date
    role: Literal["manual_inputs_and_authorization_gate_only"]
    shadow_protocol_binding: ArtifactBinding
    observation_plan_binding: ArtifactBinding
    product_review: dict[str, Any]
    minimum_shadow_evidence_before_review: dict[str, Any]
    missing_manual_inputs: dict[str, Any]
    manual_confirmation_template: str
    authorization_boundary: dict[str, bool]
    readiness: dict[str, bool]

    @model_validator(mode="after")
    def _fail_closed(self) -> IndexControlledLiveInputGate:
        if self.sealed_on != date(2026, 8, 28):
            raise ValueError("controlled-live input gate seal date drifted")
        expected_review = {
            "shadow_pair": ["510300.SH", "511010.SH"],
            "official_identity_and_fee_evidence_verified": True,
            "recent_reported_fund_scale_cny": {
                "510300.SH@2025-12-31": 422257732361.57,
                "511010.SH@2026-03-31": 3813258240.94,
            },
            "published_management_fee_rate": {
                "510300.SH": 0.0015,
                "511010.SH": 0.0015,
            },
            "published_custody_fee_rate": {
                "510300.SH": 0.0005,
                "511010.SH": 0.0005,
            },
            "official_market_maker_notice_verified": {
                "510300.SH": True,
                "511010.SH": True,
            },
            "exact_research_proxy_match": False,
            "shadow_product_selection_is_investment_recommendation": False,
            "final_live_product_mapping_status": ("pending_manual_broker_eligibility_and_user_decision"),
        }
        if self.product_review != expected_review:
            raise ValueError("controlled-live product review drifted")
        expected_floor = {
            "minimum_observations": 12,
            "minimum_elapsed_calendar_days": 84,
            "year_end_final_market_day_observation_required": True,
            "next_year_first_market_day_observation_required": True,
            "performance_or_alpha_proof": False,
        }
        if self.minimum_shadow_evidence_before_review != expected_floor:
            raise ValueError("controlled-live evidence floor drifted")
        expected_missing_keys = {
            "broker_legal_name",
            "broker_tariff_evidence_path",
            "broker_tariff_evidence_sha256",
            "actual_etf_commission_rate_per_side",
            "actual_minimum_commission_cny_per_order",
            "exchange_and_regulatory_fees_included_in_commission",
            "broker_confirms_510300_buy_sell_eligibility",
            "broker_confirms_511010_buy_sell_eligibility",
            "exact_controlled_capital_cny",
            "exact_intended_execution_date",
            "exact_authorized_products",
            "user_live_promotion_confirmation_text",
        }
        if set(self.missing_manual_inputs) != expected_missing_keys or any(
            value is not None for value in self.missing_manual_inputs.values()
        ):
            raise ValueError("controlled-live manual inputs must remain explicitly missing")
        expected_boundary = {
            "broker_credential_access_authorized": False,
            "broker_connection_authorized": False,
            "capital_deployment_authorized": False,
            "portfolio_construction_authorized": False,
            "order_submission_authorized": False,
            "trading_authorized": False,
            "automatic_promotion_authorized": False,
        }
        if self.authorization_boundary != expected_boundary:
            raise ValueError("controlled-live authorization boundary drifted")
        expected_readiness = {
            "manual_inputs_complete": False,
            "minimum_shadow_evidence_complete": False,
            "ready_for_live_product_mapping": False,
            "ready_for_portfolio_construction": False,
            "ready_for_orders": False,
            "ready_for_trading": False,
        }
        if self.readiness != expected_readiness:
            raise ValueError("controlled-live readiness must remain false")
        if self.manual_confirmation_template != (
            "⚠️ 我确认将影子执行升级为受控真实资金试运行，并理解这不是收益保证；"
            "本次仅授权指定日期、产品和金额，不授权自动交易。"
        ):
            raise ValueError("controlled-live manual confirmation must remain prominent")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def compute_index_controlled_live_gate_id(gate: IndexControlledLiveInputGate) -> str:
    return _json_hash(gate.model_dump(mode="json", exclude={"gate_id"}))


def verify_index_controlled_live_input_gate(
    *, repo_root: Path, path: Path = DEFAULT_LIVE_GATE_PATH
) -> IndexControlledLiveInputGate:
    root = Path(repo_root).resolve(strict=True)
    source = resolve_repo_regular_file(path, repo_root=root, field_name="controlled_live_gate")
    try:
        gate = IndexControlledLiveInputGate.model_validate_json(source.read_text())
    except Exception as exc:
        raise ValueError("controlled-live input gate is missing or invalid") from exc
    if gate.gate_id != compute_index_controlled_live_gate_id(gate):
        raise ValueError("controlled-live input gate self-hash mismatch")
    bindings = {
        "shadow_protocol": gate.shadow_protocol_binding,
        "observation_plan": gate.observation_plan_binding,
    }
    for name, binding in bindings.items():
        bound_path = resolve_repo_regular_file(Path(binding.path), repo_root=root, field_name=f"{name}_binding.path")
        if _sha256_file(bound_path) != binding.sha256:
            raise ValueError(f"controlled-live {name} hash mismatch")
    protocol = verify_index_shadow_execution_protocol(
        repo_root=root, path=Path(gate.shadow_protocol_binding.path), require_evidence=False
    )
    if protocol.protocol_id != gate.shadow_protocol_binding.artifact_id:
        raise ValueError("controlled-live shadow protocol ID mismatch")
    plan = verify_index_shadow_observation_plan(repo_root=root, path=Path(gate.observation_plan_binding.path))
    if plan.plan_id != gate.observation_plan_binding.artifact_id:
        raise ValueError("controlled-live observation plan ID mismatch")
    return gate


__all__ = [
    "DEFAULT_LIVE_GATE_PATH",
    "IndexControlledLiveInputGate",
    "compute_index_controlled_live_gate_id",
    "verify_index_controlled_live_input_gate",
]
