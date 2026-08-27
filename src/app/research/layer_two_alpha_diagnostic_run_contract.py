"""E11b-0b: Layer-two alpha diagnostic run contract.

Content-addressed registration and sealing of a diagnostic run's hypothesis
family, evidence windows, engine kernel, and future PIT input slots — before
any real data is assembled. This milestone is protocol-only and does not produce
factor evidence.

Upstream bindings (any drift fails file verification):
- E11a layer-two alpha development protocol
- E11b-0a alpha diagnostic engine module
- research trial ledger
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LAYER_TWO_ALPHA_DIAGNOSTIC_RUN_CONTRACT_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_ALPHA_DIAGNOSTIC_RUN_CONTRACT_VERSION: Literal["layer-two-alpha-diagnostic-run-contract-v1"] = (
    "layer-two-alpha-diagnostic-run-contract-v1"
)
DEFAULT_CONTRACT_PATH = Path("config/research/layer-two-alpha-diagnostic-run-contract-v1.json")

BOUND_E11A_PROTOCOL_PATH: Literal["config/research/layer-two-alpha-development-protocol-v1.json"] = (
    "config/research/layer-two-alpha-development-protocol-v1.json"
)
BOUND_E11A_PROTOCOL_ID: Literal["fa91f0e260beb59a7f639dd3650a3842c817e470e9c3614abf2583dd691d2f86"] = (
    "fa91f0e260beb59a7f639dd3650a3842c817e470e9c3614abf2583dd691d2f86"
)
BOUND_E11A_FILE_SHA256: Literal["88e586191d4217645eee0a975f8ea613506d3cd59468234f793a512c0f3f158d"] = (
    "88e586191d4217645eee0a975f8ea613506d3cd59468234f793a512c0f3f158d"
)

BOUND_ENGINE_PATH: Literal["src/app/research/layer_two_alpha_diagnostic_engine.py"] = (
    "src/app/research/layer_two_alpha_diagnostic_engine.py"
)
BOUND_ENGINE_VERSION: Literal["layer-two-alpha-diagnostic-engine-v0a"] = "layer-two-alpha-diagnostic-engine-v0a"
BOUND_ENGINE_FILE_SHA256: Literal["4680affb9521e68f027f1dda3e3c8b68e593653275df8b170ae0a8b2079a2bff"] = (
    "4680affb9521e68f027f1dda3e3c8b68e593653275df8b170ae0a8b2079a2bff"
)

BOUND_LEDGER_PATH: Literal["config/research/research-trial-ledger-v1.json"] = (
    "config/research/research-trial-ledger-v1.json"
)
BOUND_LEDGER_ID: Literal["1fc944251212da4972a087b4c54263912d621e43ad400b5936d6a492f1f9b9f4"] = (
    "1fc944251212da4972a087b4c54263912d621e43ad400b5936d6a492f1f9b9f4"
)
BOUND_LEDGER_FILE_SHA256: Literal["40e12754af39d846c33e5c535ab1c236957aaa1c087bf9a249ec19f6c0dfb7f8"] = (
    "40e12754af39d846c33e5c535ab1c236957aaa1c087bf9a249ec19f6c0dfb7f8"
)

FROZEN_HYPOTHESIS_FAMILY_IDS: tuple[str, ...] = (
    "quality",
    "value",
    "medium_momentum_12_1",
    "defensive_low_vol",
)
HOLM_FAMILY_WISE_ALPHA: float = 0.05
HOLM_HYPOTHESIS_COUNT: Literal[4] = 4
PRIMARY_HORIZON: Literal[40] = 40
PRIMARY_HAC_LAG: Literal[39] = 39
FROZEN_LABEL_HORIZONS: tuple[int, int, int] = (5, 20, 40)

DEVELOPMENT_START = date(2022, 1, 1)
DEVELOPMENT_END = date(2023, 12, 31)
SEEN_ROBUSTNESS_START = date(2024, 1, 1)
SEEN_ROBUSTNESS_END = date(2024, 12, 31)
CONSUMED_OOS_START = date(2025, 1, 1)
CONSUMED_OOS_END = date(2026, 8, 21)
NEW_FROZEN_OOS_BEGINS = date(2026, 8, 22)

REQUIRED_INPUT_SLOT_KINDS: tuple[str, ...] = (
    "sealed_market_snapshot",
    "candidate_eligibility_reports",
    "financial_negative_list_reports",
    "pit_fundamental_overlay",
    "pit_daily_valuation",
    "statistical_cluster_companion_reports",
)


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_literal_false(value: object, *, field_name: str) -> Literal[False]:
    if value is not False or type(value) is not bool:
        raise ValueError(f"{field_name} must be exactly false (bool)")
    return False


def _require_literal_true(value: object, *, field_name: str) -> Literal[True]:
    if value is not True or type(value) is not bool:
        raise ValueError(f"{field_name} must be exactly true (bool)")
    return True


def _require_non_bool_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an int (bool rejected)")
    return value


# ---------------------------------------------------------------------------
# Hypothesis registration
# ---------------------------------------------------------------------------


class RegisteredHypothesis(_StrictFrozen):
    hypothesis_id: str = Field(min_length=1)
    factor_family_id: str = Field(min_length=1)
    direction: Literal["positive"]
    h0: Literal["mean<=0"]
    h1: Literal["mean>0"]
    primary_horizon: Literal[40]
    hac_lag: Literal[39]
    hac_kernel: Literal["bartlett_newey_west"]
    is_holm_family_member: Literal[True]
    is_gate_only: Literal[False]

    @field_validator("hypothesis_id", "factor_family_id", mode="before")
    @classmethod
    def _reject_blank(cls, value: object) -> str:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("is_holm_family_member", mode="before")
    @classmethod
    def _strict_holm_member(cls, value: object) -> bool:
        return _require_literal_true(value, field_name="is_holm_family_member")

    @field_validator("is_gate_only", mode="before")
    @classmethod
    def _strict_gate_only(cls, value: object) -> bool:
        return _require_literal_false(value, field_name="is_gate_only")

    @model_validator(mode="after")
    def _hypothesis_id_format(self) -> RegisteredHypothesis:
        expected = f"h40-ic-{self.factor_family_id}"
        if self.hypothesis_id != expected:
            raise ValueError(f"hypothesis_id must be exactly '{expected}', got '{self.hypothesis_id}'")
        return self


# ---------------------------------------------------------------------------
# Evidence windows
# ---------------------------------------------------------------------------


class DateWindow(_StrictFrozen):
    start: date
    end: date

    @model_validator(mode="after")
    def _start_before_end(self) -> DateWindow:
        if self.start > self.end:
            raise ValueError("start must be <= end")
        return self


WindowRole = Literal[
    "development",
    "seen_robustness",
    "consumed_oos",
]

_ROLE_SEMANTICS: dict[str, dict[str, bool]] = {
    "development": {"selectable": True, "report_only": False, "forbidden": False},
    "seen_robustness": {"selectable": False, "report_only": True, "forbidden": False},
    "consumed_oos": {"selectable": False, "report_only": False, "forbidden": True},
}


class EvidenceWindow(_StrictFrozen):
    role: WindowRole
    window: DateWindow
    selectable: bool
    report_only: bool
    forbidden: bool
    label_horizons: tuple[int, ...]

    @field_validator("selectable", "report_only", "forbidden", mode="before")
    @classmethod
    def _strict_bool(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("must be a strict bool")
        return value

    @field_validator("label_horizons", mode="before")
    @classmethod
    def _validate_label_horizons(cls, value: object) -> tuple[int, ...]:
        if isinstance(value, (list, tuple)):
            items = tuple(value)
        else:
            raise ValueError("label_horizons must be a list or tuple")
        for item in items:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError(f"label_horizons items must be non-bool int, got {type(item).__name__}")
        if items != FROZEN_LABEL_HORIZONS:
            raise ValueError(f"label_horizons must be exactly {FROZEN_LABEL_HORIZONS}, got {items}")
        return items

    @model_validator(mode="after")
    def _role_semantics(self) -> EvidenceWindow:
        expected = _ROLE_SEMANTICS.get(self.role)
        if expected is None:
            raise ValueError(f"unknown role {self.role!r}")
        for field_name, expected_val in expected.items():
            actual = getattr(self, field_name)
            if actual is not expected_val:
                raise ValueError(f"{self.role} window: {field_name} must be {expected_val}, got {actual}")
        return self


class EvidenceWindows(_StrictFrozen):
    development: EvidenceWindow
    seen_robustness: EvidenceWindow
    consumed_oos: EvidenceWindow
    new_frozen_oos_begins: date
    new_frozen_oos_cannot_be_evaluated: Literal[True]
    label_endpoint_must_remain_within_same_window: Literal[True]
    horizon_never_shifts_or_shortens: Literal[True]
    missing_or_unverified_endpoint_is_unknown: Literal[True]

    @field_validator(
        "new_frozen_oos_cannot_be_evaluated",
        "label_endpoint_must_remain_within_same_window",
        "horizon_never_shifts_or_shortens",
        "missing_or_unverified_endpoint_is_unknown",
        mode="before",
    )
    @classmethod
    def _strict_literal_true(cls, value: object) -> bool:
        if value is not True or type(value) is not bool:
            raise ValueError("must be exactly true (strict bool)")
        return True

    @model_validator(mode="after")
    def _check_window_roles_and_boundaries(self) -> EvidenceWindows:
        if self.development.role != "development":
            raise ValueError("development window role must be 'development'")
        if self.seen_robustness.role != "seen_robustness":
            raise ValueError("seen_robustness window role must be 'seen_robustness'")
        if self.consumed_oos.role != "consumed_oos":
            raise ValueError("consumed_oos window role must be 'consumed_oos'")
        if self.development.window.end >= self.seen_robustness.window.start:
            raise ValueError("development must end before seen_robustness starts")
        if self.seen_robustness.window.end >= self.consumed_oos.window.start:
            raise ValueError("seen_robustness must end before consumed_oos starts")
        if self.consumed_oos.window.end >= self.new_frozen_oos_begins:
            raise ValueError("consumed_oos must end before new_frozen_oos_begins")
        return self


# ---------------------------------------------------------------------------
# Future PIT input slots
# ---------------------------------------------------------------------------


class FutureInputSlot(_StrictFrozen):
    kind: str = Field(min_length=1)
    required: Literal[True]
    state: Literal["unbound"]
    repo_relative_path: None
    sha256: None
    snapshot_id: None
    note: str = Field(min_length=1)

    @field_validator("required", mode="before")
    @classmethod
    def _strict_required(cls, value: object) -> bool:
        return _require_literal_true(value, field_name="required")

    @field_validator("kind", "note", mode="before")
    @classmethod
    def _reject_blank(cls, value: object) -> str:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("repo_relative_path", "sha256", "snapshot_id", mode="before")
    @classmethod
    def _must_be_null(cls, value: object) -> None:
        if value is not None:
            raise ValueError("unbound input slot must have null path/sha256/snapshot_id")
        return None


# ---------------------------------------------------------------------------
# Holm family specification
# ---------------------------------------------------------------------------


class HolmFamilySpec(_StrictFrozen):
    family_wise_alpha: float
    hypothesis_count: Literal[4]
    hypotheses: tuple[
        RegisteredHypothesis,
        RegisteredHypothesis,
        RegisteredHypothesis,
        RegisteredHypothesis,
    ]
    spread_positivity_is_gate_not_holm_member: Literal[True]
    yearly_direction_is_gate_not_holm_member: Literal[True]
    cluster_companion_is_gate_not_holm_member: Literal[True]

    @field_validator("family_wise_alpha", mode="before")
    @classmethod
    def _check_alpha(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("family_wise_alpha must be a number")
        if float(value) != HOLM_FAMILY_WISE_ALPHA:
            raise ValueError(f"family_wise_alpha must be exactly {HOLM_FAMILY_WISE_ALPHA}")
        return float(value)

    @field_validator("hypothesis_count", mode="before")
    @classmethod
    def _reject_bool_count(cls, value: object) -> int:
        return _require_non_bool_int(value, field_name="hypothesis_count")

    @field_validator(
        "spread_positivity_is_gate_not_holm_member",
        "yearly_direction_is_gate_not_holm_member",
        "cluster_companion_is_gate_not_holm_member",
        mode="before",
    )
    @classmethod
    def _strict_gate_bool(cls, value: object) -> bool:
        if value is not True or type(value) is not bool:
            raise ValueError("gate flag must be exactly true (strict bool)")
        return True

    @model_validator(mode="after")
    def _exactly_four_unique_hypotheses(self) -> HolmFamilySpec:
        ids = [h.hypothesis_id for h in self.hypotheses]
        if len(set(ids)) != 4:
            raise ValueError("hypotheses must have exactly 4 unique hypothesis_ids")
        families = [h.factor_family_id for h in self.hypotheses]
        if tuple(families) != FROZEN_HYPOTHESIS_FAMILY_IDS:
            raise ValueError(f"hypothesis factor_family_ids must be exactly {FROZEN_HYPOTHESIS_FAMILY_IDS} in order")
        return self


# ---------------------------------------------------------------------------
# Readiness flags
# ---------------------------------------------------------------------------


class ReadinessFlags(_StrictFrozen):
    research_only: Literal[True]
    does_not_run_data: Literal[True]
    ready_for_data: Literal[False]
    ready_for_scoring: Literal[False]
    ready_for_backtest: Literal[False]
    ready_for_portfolio_construction: Literal[False]
    ready_for_orders: Literal[False]
    ready_for_trading: Literal[False]
    auto_apply: Literal[False]

    @field_validator(
        "research_only",
        "does_not_run_data",
        "ready_for_data",
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_portfolio_construction",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
        mode="before",
    )
    @classmethod
    def _strict_bool(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("readiness flag must be a strict bool")
        return value


# ---------------------------------------------------------------------------
# Top-level contract
# ---------------------------------------------------------------------------


class LayerTwoAlphaDiagnosticRunContractV1(_StrictFrozen):
    schema_version: Literal["1"]
    contract_version: Literal["layer-two-alpha-diagnostic-run-contract-v1"]

    e11a_protocol_path: Literal["config/research/layer-two-alpha-development-protocol-v1.json"]
    e11a_protocol_id: str = Field(min_length=64, max_length=64)
    e11a_file_sha256: str = Field(min_length=64, max_length=64)

    engine_path: Literal["src/app/research/layer_two_alpha_diagnostic_engine.py"]
    engine_version: Literal["layer-two-alpha-diagnostic-engine-v0a"]
    engine_file_sha256: str = Field(min_length=64, max_length=64)

    base_ledger_path: Literal["config/research/research-trial-ledger-v1.json"]
    base_ledger_id: str = Field(min_length=64, max_length=64)
    base_ledger_file_sha256: str = Field(min_length=64, max_length=64)

    holm_family: HolmFamilySpec
    windows: EvidenceWindows
    future_input_slots: tuple[
        FutureInputSlot,
        FutureInputSlot,
        FutureInputSlot,
        FutureInputSlot,
        FutureInputSlot,
        FutureInputSlot,
    ]
    readiness: ReadinessFlags

    contract_id: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator(
        "e11a_protocol_id",
        "e11a_file_sha256",
        "engine_file_sha256",
        "base_ledger_id",
        "base_ledger_file_sha256",
        mode="before",
    )
    @classmethod
    def _hex_lowercase(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a hex string")
        if len(value) != 64 or not all(c in "0123456789abcdef" for c in value):
            raise ValueError("must be a 64-char lowercase hex SHA-256")
        return value

    @field_validator("contract_id", mode="before")
    @classmethod
    def _contract_id_hex(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("contract_id must be a string or null")
        if len(value) != 64 or not all(c in "0123456789abcdef" for c in value):
            raise ValueError("contract_id must be a 64-char lowercase hex SHA-256")
        return value

    @model_validator(mode="after")
    def _check_binding_constants(self) -> LayerTwoAlphaDiagnosticRunContractV1:
        _checks: list[tuple[str, str, str]] = [
            ("e11a_protocol_id", self.e11a_protocol_id, BOUND_E11A_PROTOCOL_ID),
            ("e11a_file_sha256", self.e11a_file_sha256, BOUND_E11A_FILE_SHA256),
            ("engine_file_sha256", self.engine_file_sha256, BOUND_ENGINE_FILE_SHA256),
            ("base_ledger_id", self.base_ledger_id, BOUND_LEDGER_ID),
            ("base_ledger_file_sha256", self.base_ledger_file_sha256, BOUND_LEDGER_FILE_SHA256),
        ]
        for field_name, actual, expected in _checks:
            if actual != expected:
                raise ValueError(f"{field_name} must equal BOUND constant {expected}, got {actual}")
        return self

    @model_validator(mode="after")
    def _validate_input_slots(self) -> LayerTwoAlphaDiagnosticRunContractV1:
        slot_kinds = tuple(s.kind for s in self.future_input_slots)
        if slot_kinds != REQUIRED_INPUT_SLOT_KINDS:
            raise ValueError(f"future_input_slots kinds must be exactly {REQUIRED_INPUT_SLOT_KINDS}")
        return self


# ---------------------------------------------------------------------------
# Canonical hashing
# ---------------------------------------------------------------------------


def canonical_contract_payload(
    contract: LayerTwoAlphaDiagnosticRunContractV1,
) -> dict[str, Any]:
    return contract.model_dump(mode="json", exclude={"contract_id"})


def canonical_contract_bytes(
    contract: LayerTwoAlphaDiagnosticRunContractV1,
) -> bytes:
    payload = canonical_contract_payload(contract)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_contract_id(
    contract: LayerTwoAlphaDiagnosticRunContractV1,
) -> str:
    return hashlib.sha256(canonical_contract_bytes(contract)).hexdigest()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _default_hypotheses() -> tuple[
    RegisteredHypothesis,
    RegisteredHypothesis,
    RegisteredHypothesis,
    RegisteredHypothesis,
]:
    specs = [
        ("h40-ic-quality", "quality"),
        ("h40-ic-value", "value"),
        ("h40-ic-medium_momentum_12_1", "medium_momentum_12_1"),
        ("h40-ic-defensive_low_vol", "defensive_low_vol"),
    ]
    hyps: list[RegisteredHypothesis] = []
    for hid, fid in specs:
        hyps.append(
            RegisteredHypothesis(
                hypothesis_id=hid,
                factor_family_id=fid,
                direction="positive",
                h0="mean<=0",
                h1="mean>0",
                primary_horizon=40,
                hac_lag=39,
                hac_kernel="bartlett_newey_west",
                is_holm_family_member=True,
                is_gate_only=False,
            )
        )
    return (hyps[0], hyps[1], hyps[2], hyps[3])


def _default_holm_family() -> HolmFamilySpec:
    return HolmFamilySpec(
        family_wise_alpha=HOLM_FAMILY_WISE_ALPHA,
        hypothesis_count=4,
        hypotheses=_default_hypotheses(),
        spread_positivity_is_gate_not_holm_member=True,
        yearly_direction_is_gate_not_holm_member=True,
        cluster_companion_is_gate_not_holm_member=True,
    )


def _default_windows() -> EvidenceWindows:
    return EvidenceWindows(
        development=EvidenceWindow(
            role="development",
            window=DateWindow(start=DEVELOPMENT_START, end=DEVELOPMENT_END),
            selectable=True,
            report_only=False,
            forbidden=False,
            label_horizons=(5, 20, 40),
        ),
        seen_robustness=EvidenceWindow(
            role="seen_robustness",
            window=DateWindow(start=SEEN_ROBUSTNESS_START, end=SEEN_ROBUSTNESS_END),
            selectable=False,
            report_only=True,
            forbidden=False,
            label_horizons=(5, 20, 40),
        ),
        consumed_oos=EvidenceWindow(
            role="consumed_oos",
            window=DateWindow(start=CONSUMED_OOS_START, end=CONSUMED_OOS_END),
            selectable=False,
            report_only=False,
            forbidden=True,
            label_horizons=(5, 20, 40),
        ),
        new_frozen_oos_begins=NEW_FROZEN_OOS_BEGINS,
        new_frozen_oos_cannot_be_evaluated=True,
        label_endpoint_must_remain_within_same_window=True,
        horizon_never_shifts_or_shortens=True,
        missing_or_unverified_endpoint_is_unknown=True,
    )


def _default_input_slots() -> tuple[
    FutureInputSlot,
    FutureInputSlot,
    FutureInputSlot,
    FutureInputSlot,
    FutureInputSlot,
    FutureInputSlot,
]:
    slot_notes = {
        "sealed_market_snapshot": "PIT sealed adjusted-close market snapshot for all eligible names",
        "candidate_eligibility_reports": "Layer-two candidate eligibility verdicts per decision date",
        "financial_negative_list_reports": "Financial negative-list verdicts per decision date",
        "pit_fundamental_overlay": "PIT fundamental overlay (quality family components)",
        "pit_daily_valuation": "PIT daily valuation multiples (value family components)",
        "statistical_cluster_companion_reports": "Statistical risk cluster assignments per monthly anchor",
    }
    slots: list[FutureInputSlot] = []
    for kind in REQUIRED_INPUT_SLOT_KINDS:
        slots.append(
            FutureInputSlot(
                kind=kind,
                required=True,
                state="unbound",
                repo_relative_path=None,
                sha256=None,
                snapshot_id=None,
                note=slot_notes[kind],
            )
        )
    return (slots[0], slots[1], slots[2], slots[3], slots[4], slots[5])


def _default_readiness() -> ReadinessFlags:
    return ReadinessFlags(
        research_only=True,
        does_not_run_data=True,
        ready_for_data=False,
        ready_for_scoring=False,
        ready_for_backtest=False,
        ready_for_portfolio_construction=False,
        ready_for_orders=False,
        ready_for_trading=False,
        auto_apply=False,
    )


def build_layer_two_alpha_diagnostic_run_contract() -> LayerTwoAlphaDiagnosticRunContractV1:
    """Construct the canonical contract with all bound constants."""
    return LayerTwoAlphaDiagnosticRunContractV1(
        schema_version="1",
        contract_version=LAYER_TWO_ALPHA_DIAGNOSTIC_RUN_CONTRACT_VERSION,
        e11a_protocol_path=BOUND_E11A_PROTOCOL_PATH,
        e11a_protocol_id=BOUND_E11A_PROTOCOL_ID,
        e11a_file_sha256=BOUND_E11A_FILE_SHA256,
        engine_path=BOUND_ENGINE_PATH,
        engine_version=BOUND_ENGINE_VERSION,
        engine_file_sha256=BOUND_ENGINE_FILE_SHA256,
        base_ledger_path=BOUND_LEDGER_PATH,
        base_ledger_id=BOUND_LEDGER_ID,
        base_ledger_file_sha256=BOUND_LEDGER_FILE_SHA256,
        holm_family=_default_holm_family(),
        windows=_default_windows(),
        future_input_slots=_default_input_slots(),
        readiness=_default_readiness(),
        contract_id=None,
    )


def seal_contract(
    contract: LayerTwoAlphaDiagnosticRunContractV1,
) -> LayerTwoAlphaDiagnosticRunContractV1:
    """Seal contract by computing and embedding the self-hash."""
    cid = compute_contract_id(contract)
    return contract.model_copy(update={"contract_id": cid})


# ---------------------------------------------------------------------------
# Structural verification (JSON-only, no disk)
# ---------------------------------------------------------------------------


class StructuralVerificationResult(_StrictFrozen):
    contract_id: str
    structural_ok: bool
    e11a_protocol_binding_ok: Literal[False]
    engine_binding_ok: Literal[False]
    ledger_binding_ok: Literal[False]
    all_input_slots_unbound: bool
    all_readiness_false: bool
    hypothesis_count_ok: bool


def verify_contract_structural(
    contract: LayerTwoAlphaDiagnosticRunContractV1,
) -> StructuralVerificationResult:
    """Structural verification without disk access. Binding flags are always False."""
    assert_contract_self_hash(contract)
    assert_binding_constants(contract)
    assert_readiness_all_false(contract)
    assert_input_slots_all_unbound(contract)
    assert_holm_family_exactly_four(contract)
    assert_windows_valid(contract)

    return StructuralVerificationResult(
        contract_id=contract.contract_id or compute_contract_id(contract),
        structural_ok=True,
        e11a_protocol_binding_ok=False,
        engine_binding_ok=False,
        ledger_binding_ok=False,
        all_input_slots_unbound=True,
        all_readiness_false=True,
        hypothesis_count_ok=True,
    )


def assert_contract_self_hash(
    contract: LayerTwoAlphaDiagnosticRunContractV1,
) -> None:
    if contract.contract_id is None:
        raise ValueError("contract_id is missing (not sealed)")
    expected = compute_contract_id(contract)
    if contract.contract_id != expected:
        raise ValueError("contract_id does not match canonical content hash")


def assert_binding_constants(
    contract: LayerTwoAlphaDiagnosticRunContractV1,
) -> None:
    """Verify all binding fields equal their BOUND module constants."""
    checks: list[tuple[str, str, str]] = [
        ("e11a_protocol_id", contract.e11a_protocol_id, BOUND_E11A_PROTOCOL_ID),
        ("e11a_file_sha256", contract.e11a_file_sha256, BOUND_E11A_FILE_SHA256),
        ("engine_file_sha256", contract.engine_file_sha256, BOUND_ENGINE_FILE_SHA256),
        ("base_ledger_id", contract.base_ledger_id, BOUND_LEDGER_ID),
        ("base_ledger_file_sha256", contract.base_ledger_file_sha256, BOUND_LEDGER_FILE_SHA256),
    ]
    for field_name, actual, expected in checks:
        if actual != expected:
            raise ValueError(f"structural: {field_name} must equal BOUND constant {expected}, got {actual}")


def assert_readiness_all_false(
    contract: LayerTwoAlphaDiagnosticRunContractV1,
) -> None:
    r = contract.readiness
    if r.ready_for_data is not False:
        raise ValueError("ready_for_data must be false")
    if r.ready_for_scoring is not False:
        raise ValueError("ready_for_scoring must be false")
    if r.ready_for_backtest is not False:
        raise ValueError("ready_for_backtest must be false")
    if r.ready_for_portfolio_construction is not False:
        raise ValueError("ready_for_portfolio_construction must be false")
    if r.ready_for_orders is not False:
        raise ValueError("ready_for_orders must be false")
    if r.ready_for_trading is not False:
        raise ValueError("ready_for_trading must be false")
    if r.auto_apply is not False:
        raise ValueError("auto_apply must be false")
    if r.research_only is not True:
        raise ValueError("research_only must be true")
    if r.does_not_run_data is not True:
        raise ValueError("does_not_run_data must be true")


def assert_input_slots_all_unbound(
    contract: LayerTwoAlphaDiagnosticRunContractV1,
) -> None:
    for slot in contract.future_input_slots:
        if slot.state != "unbound":
            raise ValueError(f"input slot {slot.kind} must be unbound")
        if slot.repo_relative_path is not None:
            raise ValueError(f"input slot {slot.kind} repo_relative_path must be null")
        if slot.sha256 is not None:
            raise ValueError(f"input slot {slot.kind} sha256 must be null")
        if slot.snapshot_id is not None:
            raise ValueError(f"input slot {slot.kind} snapshot_id must be null")


def assert_holm_family_exactly_four(
    contract: LayerTwoAlphaDiagnosticRunContractV1,
) -> None:
    hf = contract.holm_family
    if hf.hypothesis_count != 4:
        raise ValueError("hypothesis_count must be 4")
    if len(hf.hypotheses) != 4:
        raise ValueError("must have exactly 4 hypotheses")
    families = tuple(h.factor_family_id for h in hf.hypotheses)
    if families != FROZEN_HYPOTHESIS_FAMILY_IDS:
        raise ValueError("hypothesis factor_family_ids must match frozen order")
    for h in hf.hypotheses:
        expected_id = f"h40-ic-{h.factor_family_id}"
        if h.hypothesis_id != expected_id:
            raise ValueError(f"hypothesis_id must be '{expected_id}', got '{h.hypothesis_id}'")


def assert_windows_valid(
    contract: LayerTwoAlphaDiagnosticRunContractV1,
) -> None:
    w = contract.windows
    if w.development.window.start != DEVELOPMENT_START:
        raise ValueError("development start must be 2022-01-01")
    if w.development.window.end != DEVELOPMENT_END:
        raise ValueError("development end must be 2023-12-31")
    if w.seen_robustness.window.start != SEEN_ROBUSTNESS_START:
        raise ValueError("seen_robustness start must be 2024-01-01")
    if w.seen_robustness.window.end != SEEN_ROBUSTNESS_END:
        raise ValueError("seen_robustness end must be 2024-12-31")
    if w.consumed_oos.window.start != CONSUMED_OOS_START:
        raise ValueError("consumed_oos start must be 2025-01-01")
    if w.consumed_oos.window.end != CONSUMED_OOS_END:
        raise ValueError("consumed_oos end must be 2026-08-21")
    if w.new_frozen_oos_begins != NEW_FROZEN_OOS_BEGINS:
        raise ValueError("new_frozen_oos_begins must be 2026-08-22")

    for role_name, window in [
        ("development", w.development),
        ("seen_robustness", w.seen_robustness),
        ("consumed_oos", w.consumed_oos),
    ]:
        expected_sem = _ROLE_SEMANTICS[role_name]
        for sem_field, sem_val in expected_sem.items():
            actual_val = getattr(window, sem_field)
            if actual_val is not sem_val:
                raise ValueError(f"assert_windows_valid: {role_name}.{sem_field} must be {sem_val}")
        if window.label_horizons != FROZEN_LABEL_HORIZONS:
            raise ValueError(
                f"assert_windows_valid: {role_name}.label_horizons must be "
                f"{FROZEN_LABEL_HORIZONS}, got {window.label_horizons}"
            )

    if w.label_endpoint_must_remain_within_same_window is not True:
        raise ValueError("label_endpoint_must_remain_within_same_window must be true")
    if w.horizon_never_shifts_or_shortens is not True:
        raise ValueError("horizon_never_shifts_or_shortens must be true")
    if w.missing_or_unverified_endpoint_is_unknown is not True:
        raise ValueError("missing_or_unverified_endpoint_is_unknown must be true")


# ---------------------------------------------------------------------------
# File verification (disk-based binding checks)
# ---------------------------------------------------------------------------


class FileVerificationResult(_StrictFrozen):
    contract_id: str
    structural_ok: bool
    e11a_protocol_binding_ok: bool
    engine_binding_ok: bool
    ledger_binding_ok: bool
    all_input_slots_unbound: bool
    all_readiness_false: bool
    hypothesis_count_ok: bool


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _validate_repo_relative_path(relative: str, *, repo_root: Path, field_name: str) -> Path:
    if ".." in relative.split("/"):
        raise ValueError(f"{field_name} contains '..' path escape")
    if relative.startswith("/"):
        raise ValueError(f"{field_name} must be repo-relative, not absolute")
    unresolved = repo_root / relative
    if unresolved.is_symlink():
        raise ValueError(f"{field_name} is a symlink (forbidden): {relative}")
    full = unresolved.resolve()
    root_resolved = repo_root.resolve()
    if not str(full).startswith(str(root_resolved) + os.sep) and full != root_resolved:
        raise ValueError(f"{field_name} escapes repo root")
    if not full.exists():
        raise ValueError(f"{field_name} does not exist: {relative}")
    if not full.is_file():
        raise ValueError(f"{field_name} is not a regular file: {relative}")
    return full


def _verify_e11a_protocol_id_from_file(path: Path, expected_id: str) -> None:
    """Verify E11a protocol self-hash by reading the file and recomputing."""
    content = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        raise ValueError("E11a protocol file is not a JSON object")
    stored_id = content.get("protocol_id")
    if stored_id != expected_id:
        raise ValueError(
            f"E11a protocol_id in file ({stored_id}) does not match bound BOUND_E11A_PROTOCOL_ID ({expected_id})"
        )
    payload_for_hash = {k: v for k, v in content.items() if k != "protocol_id"}
    recomputed = hashlib.sha256(
        json.dumps(payload_for_hash, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if recomputed != expected_id:
        raise ValueError("E11a protocol self-hash verification failed (recomputed != stored)")


def _verify_engine_version_constant(path: Path, expected_version: str) -> None:
    """Verify engine module LAYER_TWO_ALPHA_DIAGNOSTIC_ENGINE_VERSION via AST."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = "LAYER_TWO_ALPHA_DIAGNOSTIC_ENGINE_VERSION"
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == target:
                if (
                    node.value is not None
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    if node.value.value == expected_version:
                        return
                    raise ValueError(
                        f"engine {target} AST value is {node.value.value!r}, expected {expected_version!r}"
                    )
                raise ValueError(f"engine {target} is not assigned a string constant")
        if isinstance(node, ast.Assign):
            for name_node in node.targets:
                if isinstance(name_node, ast.Name) and name_node.id == target:
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        if node.value.value == expected_version:
                            return
                        raise ValueError(
                            f"engine {target} AST value is {node.value.value!r}, expected {expected_version!r}"
                        )
                    raise ValueError(f"engine {target} is not assigned a string constant")
    raise ValueError(f"engine module missing top-level {target} assignment")


def _verify_ledger_id_from_file(path: Path, expected_id: str) -> None:
    """Verify ledger self-hash by reading the file and recomputing."""
    content = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(content, dict):
        raise ValueError("ledger file is not a JSON object")
    stored_id = content.get("ledger_id")
    if stored_id != expected_id:
        raise ValueError(f"ledger_id in file ({stored_id}) does not match bound BOUND_LEDGER_ID ({expected_id})")
    payload_for_hash = {k: v for k, v in content.items() if k != "ledger_id"}
    recomputed = hashlib.sha256(
        json.dumps(payload_for_hash, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if recomputed != expected_id:
        raise ValueError("ledger self-hash verification failed (recomputed != stored)")


def verify_contract_file(
    *,
    contract_path: Path,
    repo_root: Path,
) -> tuple[LayerTwoAlphaDiagnosticRunContractV1, FileVerificationResult]:
    """Full file-based verification: structural + disk binding checks."""
    root = Path(repo_root).resolve()
    contract = load_contract(contract_path)
    assert_contract_self_hash(contract)
    assert_binding_constants(contract)
    assert_readiness_all_false(contract)
    assert_input_slots_all_unbound(contract)
    assert_holm_family_exactly_four(contract)
    assert_windows_valid(contract)

    e11a_path = _validate_repo_relative_path(
        contract.e11a_protocol_path, repo_root=root, field_name="e11a_protocol_path"
    )
    e11a_file_hash = _sha256_file(e11a_path)
    if e11a_file_hash != contract.e11a_file_sha256:
        raise ValueError("E11a protocol file SHA-256 mismatch")
    _verify_e11a_protocol_id_from_file(e11a_path, contract.e11a_protocol_id)

    engine_path = _validate_repo_relative_path(contract.engine_path, repo_root=root, field_name="engine_path")
    engine_file_hash = _sha256_file(engine_path)
    if engine_file_hash != contract.engine_file_sha256:
        raise ValueError("engine file SHA-256 mismatch")
    _verify_engine_version_constant(engine_path, contract.engine_version)

    ledger_path = _validate_repo_relative_path(contract.base_ledger_path, repo_root=root, field_name="base_ledger_path")
    ledger_file_hash = _sha256_file(ledger_path)
    if ledger_file_hash != contract.base_ledger_file_sha256:
        raise ValueError("ledger file SHA-256 mismatch")
    _verify_ledger_id_from_file(ledger_path, contract.base_ledger_id)

    return contract, FileVerificationResult(
        contract_id=contract.contract_id or compute_contract_id(contract),
        structural_ok=True,
        e11a_protocol_binding_ok=True,
        engine_binding_ok=True,
        ledger_binding_ok=True,
        all_input_slots_unbound=True,
        all_readiness_false=True,
        hypothesis_count_ok=True,
    )


# ---------------------------------------------------------------------------
# Load / write
# ---------------------------------------------------------------------------


def load_contract(path: Path) -> LayerTwoAlphaDiagnosticRunContractV1:
    """Load and parse a contract JSON file."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("contract file is missing or unreadable") from exc
    return LayerTwoAlphaDiagnosticRunContractV1.model_validate(payload)


def write_contract(
    path: Path,
    contract: LayerTwoAlphaDiagnosticRunContractV1,
) -> LayerTwoAlphaDiagnosticRunContractV1:
    """Seal and write the contract to disk as canonical JSON."""
    sealed = seal_contract(contract)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = sealed.model_dump(mode="json")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    destination.write_text(text, encoding="utf-8")
    return sealed


__all__ = [
    "BOUND_E11A_FILE_SHA256",
    "BOUND_E11A_PROTOCOL_ID",
    "BOUND_E11A_PROTOCOL_PATH",
    "BOUND_ENGINE_FILE_SHA256",
    "BOUND_ENGINE_PATH",
    "BOUND_ENGINE_VERSION",
    "BOUND_LEDGER_FILE_SHA256",
    "BOUND_LEDGER_ID",
    "BOUND_LEDGER_PATH",
    "CONSUMED_OOS_END",
    "CONSUMED_OOS_START",
    "DEFAULT_CONTRACT_PATH",
    "DEVELOPMENT_END",
    "DEVELOPMENT_START",
    "DateWindow",
    "EvidenceWindow",
    "EvidenceWindows",
    "FROZEN_HYPOTHESIS_FAMILY_IDS",
    "FROZEN_LABEL_HORIZONS",
    "FileVerificationResult",
    "FutureInputSlot",
    "HOLM_FAMILY_WISE_ALPHA",
    "HOLM_HYPOTHESIS_COUNT",
    "HolmFamilySpec",
    "LAYER_TWO_ALPHA_DIAGNOSTIC_RUN_CONTRACT_SCHEMA_VERSION",
    "LAYER_TWO_ALPHA_DIAGNOSTIC_RUN_CONTRACT_VERSION",
    "LayerTwoAlphaDiagnosticRunContractV1",
    "NEW_FROZEN_OOS_BEGINS",
    "PRIMARY_HAC_LAG",
    "PRIMARY_HORIZON",
    "REQUIRED_INPUT_SLOT_KINDS",
    "ReadinessFlags",
    "RegisteredHypothesis",
    "SEEN_ROBUSTNESS_END",
    "SEEN_ROBUSTNESS_START",
    "StructuralVerificationResult",
    "assert_binding_constants",
    "assert_contract_self_hash",
    "assert_holm_family_exactly_four",
    "assert_input_slots_all_unbound",
    "assert_readiness_all_false",
    "assert_windows_valid",
    "build_layer_two_alpha_diagnostic_run_contract",
    "canonical_contract_bytes",
    "canonical_contract_payload",
    "compute_contract_id",
    "load_contract",
    "seal_contract",
    "verify_contract_file",
    "verify_contract_structural",
    "write_contract",
]
