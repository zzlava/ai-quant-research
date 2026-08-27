"""Freeze-only registration chain for the corrected layer-two alpha diagnostic.

The bundle creates three independently content-addressed artifacts in order:

1. an additive trial-registration receipt bound to the immutable base ledger;
2. a v2 development protocol that inherits E11a and applies only the sealed
   coverage-separation correction; and
3. a v2 run contract that binds the verified inputs already available while
   leaving the statistical-cluster companion explicitly unbound.

No function in this module reads forward returns or runs IC, scoring,
portfolio construction, backtests, orders, or trading.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.experiment_ledger import (
    DEFAULT_RESEARCH_TRIAL_LEDGER_PATH,
    verify_research_trial_ledger,
)
from app.research.layer_two_alpha_coverage_separation_policy import (
    DEFAULT_POLICY_PATH,
    verify_policy_file,
)
from app.research.layer_two_alpha_development_protocol import (
    DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH,
    verify_layer_two_alpha_development_protocol_file,
)
from app.research.layer_two_alpha_diagnostic_input_inventory import (
    verify_inventory,
)

SCHEMA_VERSION: Literal["1"] = "1"
REGISTRATION_VERSION: Literal["layer-two-alpha-trial-registration-v2"] = (
    "layer-two-alpha-trial-registration-v2"
)
PROTOCOL_VERSION: Literal["layer-two-alpha-development-protocol-v2"] = (
    "layer-two-alpha-development-protocol-v2"
)
RUN_CONTRACT_VERSION: Literal["layer-two-alpha-diagnostic-run-contract-v2"] = (
    "layer-two-alpha-diagnostic-run-contract-v2"
)

DEFAULT_REGISTRATION_PATH = Path("config/research/layer-two-alpha-trial-registration-v2.json")
DEFAULT_PROTOCOL_PATH = Path("config/research/layer-two-alpha-development-protocol-v2.json")
DEFAULT_RUN_CONTRACT_PATH = Path("config/research/layer-two-alpha-diagnostic-run-contract-v2.json")

BASE_INVENTORY_PATH = Path(
    "data/all-a-share-historical-v1/research/layer-two-alpha-diagnostic-input-inventory-v1.json"
)
CANDIDATE_PACK_PATH = Path(
    "data/all-a-share-historical-v1/research/candidate-eligibility-pack-v1"
)
FINANCIAL_OVERLAY_PATH = Path(
    "data/all-a-share-historical-v1/research/financial-negative-list-verdict-overlay-v1"
)
ENGINE_PATH = Path("src/app/research/layer_two_alpha_diagnostic_engine.py")

HYPOTHESES: tuple[str, ...] = (
    "h40-ic-quality",
    "h40-ic-value",
    "h40-ic-medium_momentum_12_1",
    "h40-ic-defensive_low_vol",
)
INPUT_SLOT_ORDER: tuple[str, ...] = (
    "sealed_market_snapshot",
    "candidate_eligibility_reports",
    "financial_negative_list_reports",
    "pit_fundamental_overlay",
    "pit_daily_valuation",
    "statistical_cluster_companion_reports",
)


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _hex64(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{field_name} must be a 64-char lowercase hex SHA-256")
    return value


class SourceRef(_StrictFrozen):
    path: str
    artifact_id: str
    file_sha256: str

    @field_validator("artifact_id", "file_sha256", mode="before")
    @classmethod
    def _hashes(cls, value: object, info: Any) -> str:
        return _hex64(value, field_name=str(info.field_name))


class FrozenWindowSet(_StrictFrozen):
    development: Literal["2022-01-01..2023-12-31"]
    seen_robustness_report_only: Literal["2024-01-01..2024-12-31"]
    consumed_oos_forbidden: Literal["2025-01-01..2026-08-21"]
    new_frozen_oos_begins: Literal["2026-08-22"]
    exact_label_horizons_market_days: tuple[int, int, int]

    @model_validator(mode="after")
    def _horizons(self) -> FrozenWindowSet:
        if self.exact_label_horizons_market_days != (5, 20, 40):
            raise ValueError("label horizons must remain exactly 5,20,40")
        return self


class FrozenReadiness(_StrictFrozen):
    research_only: Literal[True]
    no_outcome_observed_by_this_artifact: Literal[True]
    ready_for_alpha_diagnostic_execution: Literal[False]
    ready_for_scoring: Literal[False]
    ready_for_backtest: Literal[False]
    ready_for_portfolio_construction: Literal[False]
    ready_for_orders: Literal[False]
    ready_for_trading: Literal[False]
    auto_apply: Literal[False]


class LayerTwoAlphaTrialRegistrationV2(_StrictFrozen):
    schema_version: Literal["1"]
    registration_version: Literal["layer-two-alpha-trial-registration-v2"]
    status: Literal["registered_pre_outcome_not_executed"]
    base_ledger: SourceRef
    separation_policy: SourceRef
    trial_id: Literal["layer-two-alpha-four-family-v2-development"]
    family_id: Literal["all-a-share-layer-two-alpha-v2"]
    hypotheses: tuple[str, ...]
    holm_hypothesis_count: Literal[4]
    holm_family_wise_alpha: float
    primary_horizon_market_days: Literal[40]
    primary_hac_lag: Literal[39]
    windows: FrozenWindowSet
    declared_before_factor_or_forward_return_observation: Literal[True]
    base_ledger_remains_immutable: Literal[True]
    additive_registration_only: Literal[True]
    readiness: FrozenReadiness
    registration_id: str | None = Field(default=None)

    @field_validator("registration_id", mode="before")
    @classmethod
    def _registration_id(cls, value: object) -> str | None:
        return None if value is None else _hex64(value, field_name="registration_id")

    @field_validator("holm_family_wise_alpha", mode="before")
    @classmethod
    def _alpha(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) != 0.05:
            raise ValueError("holm_family_wise_alpha must remain exactly 0.05")
        return 0.05

    @model_validator(mode="after")
    def _hypotheses(self) -> LayerTwoAlphaTrialRegistrationV2:
        if self.hypotheses != HYPOTHESES:
            raise ValueError("hypotheses must remain the exact frozen ordered family")
        return self


class ProtocolSources(_StrictFrozen):
    base_e11a: SourceRef
    separation_policy: SourceRef
    trial_registration: SourceRef


class LayerTwoAlphaDevelopmentProtocolV2(_StrictFrozen):
    schema_version: Literal["1"]
    protocol_version: Literal["layer-two-alpha-development-protocol-v2"]
    status: Literal["frozen_not_executable_until_exact_input_contract_complete"]
    sources: ProtocolSources
    change_scope: Literal["coverage_denominator_separation_only"]
    inherited_factor_families: tuple[str, ...]
    hypotheses: tuple[str, ...]
    alpha_evidence_denominator: Literal[
        "candidate_complete_and_eligible_for_new_entry_and_factor_known"
    ]
    financial_overlay_role: Literal[
        "independent_fail_closed_new_entry_safety_overlay_not_ic_denominator"
    ]
    factor_known_count_gate: Literal[500]
    factor_known_fraction_gate: float
    pooled_primary_valid_date_gate: Literal[120]
    per_year_primary_valid_date_gate: Literal[40]
    primary_horizon_market_days: Literal[40]
    primary_hac_lag: Literal[39]
    holm_hypothesis_count: Literal[4]
    holm_family_wise_alpha: float
    windows: FrozenWindowSet
    forbidden_changes: tuple[str, ...]
    readiness: FrozenReadiness
    protocol_id: str | None = Field(default=None)

    @field_validator("protocol_id", mode="before")
    @classmethod
    def _protocol_id(cls, value: object) -> str | None:
        return None if value is None else _hex64(value, field_name="protocol_id")

    @field_validator("factor_known_fraction_gate", mode="before")
    @classmethod
    def _known_fraction(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) != 0.6:
            raise ValueError("factor_known_fraction_gate must remain exactly 0.6")
        return 0.6

    @field_validator("holm_family_wise_alpha", mode="before")
    @classmethod
    def _holm_alpha(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) != 0.05:
            raise ValueError("holm_family_wise_alpha must remain exactly 0.05")
        return 0.05

    @model_validator(mode="after")
    def _ordered_sets(self) -> LayerTwoAlphaDevelopmentProtocolV2:
        if self.inherited_factor_families != (
            "quality",
            "value",
            "medium_momentum_12_1",
            "defensive_low_vol",
        ):
            raise ValueError("factor family order drift")
        if self.hypotheses != HYPOTHESES:
            raise ValueError("hypothesis order drift")
        expected = (
            "no_factor_definition_change",
            "no_threshold_or_weight_change",
            "no_2024_selection_or_weight_change",
            "no_consumed_or_new_frozen_oos_evaluation",
            "no_financial_unknown_to_clean_coercion",
        )
        if self.forbidden_changes != expected:
            raise ValueError("forbidden_changes drift")
        return self


class InputSlotV2(_StrictFrozen):
    kind: str
    state: Literal["bound", "unbound_required"]
    path: str | None
    artifact_id: str | None
    file_sha256: str | None
    role: str

    @field_validator("artifact_id", "file_sha256", mode="before")
    @classmethod
    def _optional_hashes(cls, value: object, info: Any) -> str | None:
        return None if value is None else _hex64(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _state_fields(self) -> InputSlotV2:
        values = (self.path, self.artifact_id, self.file_sha256)
        if self.state == "bound" and any(value is None for value in values):
            raise ValueError("bound input slots require path, artifact_id, and file_sha256")
        if self.state == "unbound_required" and any(value is not None for value in values):
            raise ValueError("unbound input slots must not carry weak bindings")
        return self


class RunContractSources(_StrictFrozen):
    protocol_v2: SourceRef
    trial_registration: SourceRef
    separation_policy: SourceRef
    base_inventory: SourceRef
    engine_path: str
    engine_version: Literal["layer-two-alpha-diagnostic-engine-v0a"]
    engine_file_sha256: str

    @field_validator("engine_file_sha256", mode="before")
    @classmethod
    def _engine_hash(cls, value: object) -> str:
        return _hex64(value, field_name="engine_file_sha256")


class LayerTwoAlphaDiagnosticRunContractV2(_StrictFrozen):
    schema_version: Literal["1"]
    contract_version: Literal["layer-two-alpha-diagnostic-run-contract-v2"]
    status: Literal["prepared_blocked_on_statistical_cluster_companion"]
    sources: RunContractSources
    hypotheses: tuple[str, ...]
    windows: FrozenWindowSet
    input_slots: tuple[InputSlotV2, ...]
    exact_unbound_required_slots: tuple[str, ...]
    data_assembly_permitted: Literal[True]
    alpha_execution_permitted: Literal[False]
    readiness: FrozenReadiness
    contract_id: str | None = Field(default=None)

    @field_validator("contract_id", mode="before")
    @classmethod
    def _contract_id(cls, value: object) -> str | None:
        return None if value is None else _hex64(value, field_name="contract_id")

    @model_validator(mode="after")
    def _slots(self) -> LayerTwoAlphaDiagnosticRunContractV2:
        kinds = tuple(slot.kind for slot in self.input_slots)
        if kinds != INPUT_SLOT_ORDER:
            raise ValueError("input slot order drift")
        unbound = tuple(slot.kind for slot in self.input_slots if slot.state == "unbound_required")
        if unbound != ("statistical_cluster_companion_reports",):
            raise ValueError("only the statistical cluster companion may remain unbound")
        if self.exact_unbound_required_slots != unbound:
            raise ValueError("exact_unbound_required_slots mismatch")
        if self.hypotheses != HYPOTHESES:
            raise ValueError("hypothesis order drift")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha(model: BaseModel, *, id_field: str) -> str:
    payload = model.model_dump(mode="json", exclude={id_field})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _seal_registration(value: LayerTwoAlphaTrialRegistrationV2) -> LayerTwoAlphaTrialRegistrationV2:
    return value.model_copy(update={"registration_id": _canonical_sha(value, id_field="registration_id")})


def _seal_protocol(value: LayerTwoAlphaDevelopmentProtocolV2) -> LayerTwoAlphaDevelopmentProtocolV2:
    return value.model_copy(update={"protocol_id": _canonical_sha(value, id_field="protocol_id")})


def _seal_contract(value: LayerTwoAlphaDiagnosticRunContractV2) -> LayerTwoAlphaDiagnosticRunContractV2:
    return value.model_copy(update={"contract_id": _canonical_sha(value, id_field="contract_id")})


def _assert_self_hash(model: BaseModel, *, id_field: str, label: str) -> None:
    stored = getattr(model, id_field)
    if stored is None or stored != _canonical_sha(model, id_field=id_field):
        raise ValueError(f"{label} self-hash mismatch")


def _windows() -> FrozenWindowSet:
    return FrozenWindowSet(
        development="2022-01-01..2023-12-31",
        seen_robustness_report_only="2024-01-01..2024-12-31",
        consumed_oos_forbidden="2025-01-01..2026-08-21",
        new_frozen_oos_begins="2026-08-22",
        exact_label_horizons_market_days=(5, 20, 40),
    )


def _readiness() -> FrozenReadiness:
    return FrozenReadiness(
        research_only=True,
        no_outcome_observed_by_this_artifact=True,
        ready_for_alpha_diagnostic_execution=False,
        ready_for_scoring=False,
        ready_for_backtest=False,
        ready_for_portfolio_construction=False,
        ready_for_orders=False,
        ready_for_trading=False,
        auto_apply=False,
    )


def _safe_repo_file(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("artifact path must be repo-relative and non-traversing")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"artifact path contains symlink: {relative}")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact path escapes repo root") from exc
    if not resolved.is_file():
        raise ValueError(f"artifact file missing: {relative}")
    return resolved


def _source_ref(root: Path, relative: Path, artifact_id: str) -> SourceRef:
    path = _safe_repo_file(root, relative)
    return SourceRef(path=relative.as_posix(), artifact_id=artifact_id, file_sha256=_sha256_file(path))


def _load_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid JSON source: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON source must be an object: {path}")
    return payload


def build_trial_registration(*, repo_root: Path) -> LayerTwoAlphaTrialRegistrationV2:
    root = repo_root.resolve()
    ledger_path = _safe_repo_file(root, DEFAULT_RESEARCH_TRIAL_LEDGER_PATH)
    ledger, _ = verify_research_trial_ledger(ledger_path=ledger_path, repo_root=root)
    policy_path = _safe_repo_file(root, DEFAULT_POLICY_PATH)
    policy = verify_policy_file(policy_path, repo_root=root, full_source_recomputation=False)
    if ledger.ledger_id is None or policy.policy_id is None:
        raise ValueError("sealed upstream artifact ID missing")
    return _seal_registration(
        LayerTwoAlphaTrialRegistrationV2(
            schema_version=SCHEMA_VERSION,
            registration_version=REGISTRATION_VERSION,
            status="registered_pre_outcome_not_executed",
            base_ledger=_source_ref(root, DEFAULT_RESEARCH_TRIAL_LEDGER_PATH, ledger.ledger_id),
            separation_policy=_source_ref(root, DEFAULT_POLICY_PATH, policy.policy_id),
            trial_id="layer-two-alpha-four-family-v2-development",
            family_id="all-a-share-layer-two-alpha-v2",
            hypotheses=HYPOTHESES,
            holm_hypothesis_count=4,
            holm_family_wise_alpha=0.05,
            primary_horizon_market_days=40,
            primary_hac_lag=39,
            windows=_windows(),
            declared_before_factor_or_forward_return_observation=True,
            base_ledger_remains_immutable=True,
            additive_registration_only=True,
            readiness=_readiness(),
        )
    )


def build_protocol_v2(
    *,
    repo_root: Path,
    registration: LayerTwoAlphaTrialRegistrationV2,
) -> LayerTwoAlphaDevelopmentProtocolV2:
    root = repo_root.resolve()
    _assert_self_hash(registration, id_field="registration_id", label="trial registration")
    registration_path = _safe_repo_file(root, DEFAULT_REGISTRATION_PATH)
    if registration.registration_id is None:
        raise ValueError("trial registration ID missing")
    on_disk_registration = LayerTwoAlphaTrialRegistrationV2.model_validate_json(
        registration_path.read_text(encoding="utf-8")
    )
    _assert_self_hash(on_disk_registration, id_field="registration_id", label="trial registration")
    if on_disk_registration != registration:
        raise ValueError("trial registration argument does not match disk")
    base_path = _safe_repo_file(root, DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH)
    base, _ = verify_layer_two_alpha_development_protocol_file(protocol_path=base_path, repo_root=root)
    policy_path = _safe_repo_file(root, DEFAULT_POLICY_PATH)
    policy = verify_policy_file(policy_path, repo_root=root, full_source_recomputation=False)
    if base.protocol_id is None or policy.policy_id is None:
        raise ValueError("sealed upstream artifact ID missing")
    return _seal_protocol(
        LayerTwoAlphaDevelopmentProtocolV2(
            schema_version=SCHEMA_VERSION,
            protocol_version=PROTOCOL_VERSION,
            status="frozen_not_executable_until_exact_input_contract_complete",
            sources=ProtocolSources(
                base_e11a=_source_ref(root, DEFAULT_LAYER_TWO_ALPHA_DEVELOPMENT_PROTOCOL_PATH, base.protocol_id),
                separation_policy=_source_ref(root, DEFAULT_POLICY_PATH, policy.policy_id),
                trial_registration=_source_ref(root, DEFAULT_REGISTRATION_PATH, registration.registration_id),
            ),
            change_scope="coverage_denominator_separation_only",
            inherited_factor_families=(
                "quality",
                "value",
                "medium_momentum_12_1",
                "defensive_low_vol",
            ),
            hypotheses=HYPOTHESES,
            alpha_evidence_denominator="candidate_complete_and_eligible_for_new_entry_and_factor_known",
            financial_overlay_role="independent_fail_closed_new_entry_safety_overlay_not_ic_denominator",
            factor_known_count_gate=500,
            factor_known_fraction_gate=0.6,
            pooled_primary_valid_date_gate=120,
            per_year_primary_valid_date_gate=40,
            primary_horizon_market_days=40,
            primary_hac_lag=39,
            holm_hypothesis_count=4,
            holm_family_wise_alpha=0.05,
            windows=_windows(),
            forbidden_changes=(
                "no_factor_definition_change",
                "no_threshold_or_weight_change",
                "no_2024_selection_or_weight_change",
                "no_consumed_or_new_frozen_oos_evaluation",
                "no_financial_unknown_to_clean_coercion",
            ),
            readiness=_readiness(),
        )
    )


def _bound_slot(*, kind: str, path: str, artifact_id: str, file_sha256: str, role: str) -> InputSlotV2:
    return InputSlotV2(
        kind=kind,
        state="bound",
        path=path,
        artifact_id=artifact_id,
        file_sha256=file_sha256,
        role=role,
    )


def build_run_contract_v2(
    *,
    repo_root: Path,
    registration: LayerTwoAlphaTrialRegistrationV2,
    protocol: LayerTwoAlphaDevelopmentProtocolV2,
) -> LayerTwoAlphaDiagnosticRunContractV2:
    root = repo_root.resolve()
    _assert_self_hash(registration, id_field="registration_id", label="trial registration")
    _assert_self_hash(protocol, id_field="protocol_id", label="v2 protocol")
    if registration.registration_id is None or protocol.protocol_id is None:
        raise ValueError("sealed v2 artifact ID missing")
    policy_path = _safe_repo_file(root, DEFAULT_POLICY_PATH)
    policy = verify_policy_file(policy_path, repo_root=root, full_source_recomputation=False)
    inventory_path = _safe_repo_file(root, BASE_INVENTORY_PATH)
    inventory = verify_inventory(inventory_path, repo_root=root)
    if policy.policy_id is None or inventory.inventory_id is None:
        raise ValueError("sealed source ID missing")
    bound = {slot.kind: slot for slot in inventory.slots if slot.state == "bound"}
    market = bound["sealed_market_snapshot"]
    fundamental = bound["pit_fundamental_overlay"]
    valuation = bound["pit_daily_valuation"]

    candidate_manifest_path = _safe_repo_file(root, CANDIDATE_PACK_PATH / "manifest.json")
    candidate = _load_json_dict(candidate_manifest_path)
    financial_manifest_path = _safe_repo_file(root, FINANCIAL_OVERLAY_PATH / "manifest.json")
    financial = _load_json_dict(financial_manifest_path)
    candidate_id = _hex64(candidate.get("pack_id"), field_name="candidate pack_id")
    financial_id = _hex64(financial.get("overlay_id"), field_name="financial overlay_id")
    if candidate_id != policy.source_binding.candidate_pack_id:
        raise ValueError("candidate pack ID does not match separation policy")
    if financial_id != policy.source_binding.financial_overlay_id:
        raise ValueError("financial overlay ID does not match separation policy")
    if financial.get("candidate_pack_id") != candidate_id:
        raise ValueError("financial overlay candidate binding drift")
    if market.snapshot_id != policy.source_binding.market_snapshot_id:
        raise ValueError("market snapshot ID does not match separation policy")
    if fundamental.snapshot_id != policy.source_binding.fundamental_snapshot_id:
        raise ValueError("fundamental snapshot ID does not match separation policy")
    if valuation.snapshot_id != fundamental.snapshot_id:
        raise ValueError("valuation/fundamental snapshot binding drift")
    engine_path = _safe_repo_file(root, ENGINE_PATH)

    slots = (
        _bound_slot(
            kind="sealed_market_snapshot",
            path=market.repo_relative_path,
            artifact_id=market.snapshot_id,
            file_sha256=market.file_sha256,
            role="factor_inputs_and_forward_diagnostic_labels_only",
        ),
        _bound_slot(
            kind="candidate_eligibility_reports",
            path=CANDIDATE_PACK_PATH.as_posix(),
            artifact_id=candidate_id,
            file_sha256=_sha256_file(candidate_manifest_path),
            role="alpha_evidence_denominator",
        ),
        _bound_slot(
            kind="financial_negative_list_reports",
            path=FINANCIAL_OVERLAY_PATH.as_posix(),
            artifact_id=financial_id,
            file_sha256=_sha256_file(financial_manifest_path),
            role="separate_fail_closed_safety_overlay_not_alpha_denominator",
        ),
        _bound_slot(
            kind="pit_fundamental_overlay",
            path=fundamental.repo_relative_path,
            artifact_id=fundamental.snapshot_id,
            file_sha256=fundamental.file_sha256,
            role="quality_factor_inputs",
        ),
        _bound_slot(
            kind="pit_daily_valuation",
            path=valuation.repo_relative_path,
            artifact_id=valuation.snapshot_id,
            file_sha256=valuation.file_sha256,
            role="value_factor_inputs",
        ),
        InputSlotV2(
            kind="statistical_cluster_companion_reports",
            state="unbound_required",
            path=None,
            artifact_id=None,
            file_sha256=None,
            role="pit_statistical_risk_companion_gate_only_not_fifth_hypothesis",
        ),
    )
    return _seal_contract(
        LayerTwoAlphaDiagnosticRunContractV2(
            schema_version=SCHEMA_VERSION,
            contract_version=RUN_CONTRACT_VERSION,
            status="prepared_blocked_on_statistical_cluster_companion",
            sources=RunContractSources(
                protocol_v2=_source_ref(root, DEFAULT_PROTOCOL_PATH, protocol.protocol_id),
                trial_registration=_source_ref(root, DEFAULT_REGISTRATION_PATH, registration.registration_id),
                separation_policy=_source_ref(root, DEFAULT_POLICY_PATH, policy.policy_id),
                base_inventory=_source_ref(root, BASE_INVENTORY_PATH, inventory.inventory_id),
                engine_path=ENGINE_PATH.as_posix(),
                engine_version="layer-two-alpha-diagnostic-engine-v0a",
                engine_file_sha256=_sha256_file(engine_path),
            ),
            hypotheses=HYPOTHESES,
            windows=_windows(),
            input_slots=slots,
            exact_unbound_required_slots=("statistical_cluster_companion_reports",),
            data_assembly_permitted=True,
            alpha_execution_permitted=False,
            readiness=_readiness(),
        )
    )


def _write_model(path: Path, model: BaseModel, *, replace_existing: bool) -> None:
    if path.exists() and not replace_existing:
        raise FileExistsError(f"frozen artifact already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def freeze_bundle(*, repo_root: Path, replace_existing: bool = False) -> tuple[
    LayerTwoAlphaTrialRegistrationV2,
    LayerTwoAlphaDevelopmentProtocolV2,
    LayerTwoAlphaDiagnosticRunContractV2,
]:
    root = repo_root.resolve()
    registration = build_trial_registration(repo_root=root)
    _write_model(root / DEFAULT_REGISTRATION_PATH, registration, replace_existing=replace_existing)
    try:
        protocol = build_protocol_v2(repo_root=root, registration=registration)
        _write_model(root / DEFAULT_PROTOCOL_PATH, protocol, replace_existing=replace_existing)
        contract = build_run_contract_v2(repo_root=root, registration=registration, protocol=protocol)
        _write_model(root / DEFAULT_RUN_CONTRACT_PATH, contract, replace_existing=replace_existing)
    except Exception:
        if not replace_existing:
            for path in (DEFAULT_PROTOCOL_PATH, DEFAULT_REGISTRATION_PATH):
                candidate = root / path
                if candidate.exists():
                    candidate.unlink()
        raise
    return registration, protocol, contract


def verify_bundle(*, repo_root: Path) -> tuple[
    LayerTwoAlphaTrialRegistrationV2,
    LayerTwoAlphaDevelopmentProtocolV2,
    LayerTwoAlphaDiagnosticRunContractV2,
]:
    root = repo_root.resolve()
    registration_path = _safe_repo_file(root, DEFAULT_REGISTRATION_PATH)
    protocol_path = _safe_repo_file(root, DEFAULT_PROTOCOL_PATH)
    contract_path = _safe_repo_file(root, DEFAULT_RUN_CONTRACT_PATH)
    registration = LayerTwoAlphaTrialRegistrationV2.model_validate_json(
        registration_path.read_text(encoding="utf-8")
    )
    protocol = LayerTwoAlphaDevelopmentProtocolV2.model_validate_json(protocol_path.read_text(encoding="utf-8"))
    contract = LayerTwoAlphaDiagnosticRunContractV2.model_validate_json(contract_path.read_text(encoding="utf-8"))
    _assert_self_hash(registration, id_field="registration_id", label="trial registration")
    _assert_self_hash(protocol, id_field="protocol_id", label="v2 protocol")
    _assert_self_hash(contract, id_field="contract_id", label="v2 run contract")
    rebuilt_registration = build_trial_registration(repo_root=root)
    if registration != rebuilt_registration:
        raise ValueError("trial registration does not recompute from bound sources")
    rebuilt_protocol = build_protocol_v2(repo_root=root, registration=registration)
    if protocol != rebuilt_protocol:
        raise ValueError("v2 protocol does not recompute from bound sources")
    rebuilt_contract = build_run_contract_v2(
        repo_root=root,
        registration=registration,
        protocol=protocol,
    )
    if contract != rebuilt_contract:
        raise ValueError("v2 run contract does not recompute from bound sources")
    return registration, protocol, contract


__all__ = [
    "DEFAULT_PROTOCOL_PATH",
    "DEFAULT_REGISTRATION_PATH",
    "DEFAULT_RUN_CONTRACT_PATH",
    "LayerTwoAlphaDevelopmentProtocolV2",
    "LayerTwoAlphaDiagnosticRunContractV2",
    "LayerTwoAlphaTrialRegistrationV2",
    "build_protocol_v2",
    "build_run_contract_v2",
    "build_trial_registration",
    "freeze_bundle",
    "verify_bundle",
]
