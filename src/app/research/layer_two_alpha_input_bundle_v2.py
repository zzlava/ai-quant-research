"""Complete, content-addressed input binding for layer-two alpha v2.

This receipt upgrades the freeze-only v2 run contract by binding the verified
monthly statistical-cluster pack.  It authorizes only the specifically frozen
offline alpha diagnostic over 2022-2024.  It does not authorize scoring,
backtests, portfolio construction, orders, trading, or any 2025+ evaluation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_alpha_v2_freeze_bundle import (
    DEFAULT_RUN_CONTRACT_PATH,
    InputSlotV2,
    verify_bundle,
)
from app.research.layer_two_statistical_cluster_pack_v2 import (
    ASSIGNMENTS_FILE,
    verify_cluster_pack,
)
from app.research.layer_two_statistical_cluster_pack_v2 import (
    DEFAULT_OUTPUT_DIR as DEFAULT_CLUSTER_PACK_DIR,
)
from app.research.layer_two_statistical_cluster_pack_v2 import (
    MANIFEST_FILE as CLUSTER_MANIFEST_FILE,
)

SCHEMA_VERSION: Literal["1"] = "1"
BUNDLE_VERSION: Literal["layer-two-alpha-input-bundle-v2"] = "layer-two-alpha-input-bundle-v2"
DEFAULT_OUTPUT_PATH = Path(
    "data/all-a-share-historical-v1/research/layer-two-alpha-input-bundle-v2.json"
)
SLOT_ORDER: tuple[str, ...] = (
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


class BundleSource(_StrictFrozen):
    path: str
    artifact_id: str
    file_sha256: str

    @field_validator("artifact_id", "file_sha256", mode="before")
    @classmethod
    def _hashes(cls, value: object, info: Any) -> str:
        return _hex64(value, field_name=str(info.field_name))


class FullyBoundSlot(_StrictFrozen):
    kind: str
    path: str
    artifact_id: str
    file_sha256: str
    role: str

    @field_validator("artifact_id", "file_sha256", mode="before")
    @classmethod
    def _hashes(cls, value: object, info: Any) -> str:
        return _hex64(value, field_name=str(info.field_name))


class InputBundleReadiness(_StrictFrozen):
    research_only: Literal[True]
    all_six_slots_bound: Literal[True]
    ready_for_frozen_alpha_diagnostic_execution: Literal[True]
    ready_for_scoring: Literal[False]
    ready_for_backtest: Literal[False]
    ready_for_portfolio_construction: Literal[False]
    ready_for_orders: Literal[False]
    ready_for_trading: Literal[False]
    auto_apply: Literal[False]


class LayerTwoAlphaInputBundleV2(_StrictFrozen):
    schema_version: Literal["1"]
    bundle_version: Literal["layer-two-alpha-input-bundle-v2"]
    status: Literal["fully_bound_for_frozen_offline_alpha_diagnostic_only"]
    run_contract: BundleSource
    statistical_cluster_pack: BundleSource
    slots: tuple[FullyBoundSlot, ...]
    alpha_evidence_denominator: Literal[
        "candidate_complete_and_eligible_for_new_entry_and_factor_known"
    ]
    financial_overlay_role: Literal[
        "independent_fail_closed_new_entry_safety_overlay_not_ic_denominator"
    ]
    development_window: Literal["2022-01-01..2023-12-31"]
    seen_robustness_report_only_window: Literal["2024-01-01..2024-12-31"]
    consumed_oos_forbidden: Literal["2025-01-01..2026-08-21"]
    new_frozen_oos_unauthorized_from: Literal["2026-08-22"]
    readiness: InputBundleReadiness
    bundle_id: str | None = Field(default=None)

    @field_validator("bundle_id", mode="before")
    @classmethod
    def _bundle_id(cls, value: object) -> str | None:
        return None if value is None else _hex64(value, field_name="bundle_id")

    @model_validator(mode="after")
    def _slot_order(self) -> LayerTwoAlphaInputBundleV2:
        if tuple(slot.kind for slot in self.slots) != SLOT_ORDER:
            raise ValueError("all six input slots must be present in frozen order")
        if len({slot.kind for slot in self.slots}) != len(self.slots):
            raise ValueError("input slot kinds must be unique")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_id(bundle: LayerTwoAlphaInputBundleV2) -> str:
    payload = bundle.model_dump(mode="json", exclude={"bundle_id"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _seal(bundle: LayerTwoAlphaInputBundleV2) -> LayerTwoAlphaInputBundleV2:
    return bundle.model_copy(update={"bundle_id": _canonical_id(bundle)})


def _assert_self_hash(bundle: LayerTwoAlphaInputBundleV2) -> None:
    if bundle.bundle_id is None or bundle.bundle_id != _canonical_id(bundle):
        raise ValueError("layer-two alpha input bundle self-hash mismatch")


def _bound_contract_slot(slot: InputSlotV2) -> FullyBoundSlot:
    if slot.state != "bound" or slot.path is None or slot.artifact_id is None or slot.file_sha256 is None:
        raise ValueError(f"run contract slot is not fully bound: {slot.kind}")
    return FullyBoundSlot(
        kind=slot.kind,
        path=slot.path,
        artifact_id=slot.artifact_id,
        file_sha256=slot.file_sha256,
        role=slot.role,
    )


def build_input_bundle(*, repo_root: Path) -> LayerTwoAlphaInputBundleV2:
    root = repo_root.resolve()
    _, _, contract = verify_bundle(repo_root=root)
    cluster = verify_cluster_pack(
        repo_root=root,
        pack_dir=root / DEFAULT_CLUSTER_PACK_DIR,
        full_recomputation=False,
    )
    if contract.contract_id is None or cluster.pack_id is None:
        raise ValueError("sealed run contract or cluster pack ID missing")
    contract_slots = {slot.kind: slot for slot in contract.input_slots}
    first_five = tuple(_bound_contract_slot(contract_slots[kind]) for kind in SLOT_ORDER[:-1])
    cluster_manifest_path = root / DEFAULT_CLUSTER_PACK_DIR / CLUSTER_MANIFEST_FILE
    cluster_assignments_path = root / DEFAULT_CLUSTER_PACK_DIR / ASSIGNMENTS_FILE
    cluster_slot = FullyBoundSlot(
        kind="statistical_cluster_companion_reports",
        path=DEFAULT_CLUSTER_PACK_DIR.as_posix(),
        artifact_id=cluster.pack_id,
        file_sha256=_sha256_file(cluster_manifest_path),
        role="pit_statistical_risk_companion_gate_only_not_fifth_hypothesis",
    )
    if _sha256_file(cluster_assignments_path) != cluster.integrity.assignments_file_sha256:
        raise ValueError("cluster assignment hash drift")
    return _seal(
        LayerTwoAlphaInputBundleV2(
            schema_version=SCHEMA_VERSION,
            bundle_version=BUNDLE_VERSION,
            status="fully_bound_for_frozen_offline_alpha_diagnostic_only",
            run_contract=BundleSource(
                path=DEFAULT_RUN_CONTRACT_PATH.as_posix(),
                artifact_id=contract.contract_id,
                file_sha256=_sha256_file(root / DEFAULT_RUN_CONTRACT_PATH),
            ),
            statistical_cluster_pack=BundleSource(
                path=DEFAULT_CLUSTER_PACK_DIR.as_posix(),
                artifact_id=cluster.pack_id,
                file_sha256=_sha256_file(cluster_manifest_path),
            ),
            slots=(*first_five, cluster_slot),
            alpha_evidence_denominator="candidate_complete_and_eligible_for_new_entry_and_factor_known",
            financial_overlay_role="independent_fail_closed_new_entry_safety_overlay_not_ic_denominator",
            development_window="2022-01-01..2023-12-31",
            seen_robustness_report_only_window="2024-01-01..2024-12-31",
            consumed_oos_forbidden="2025-01-01..2026-08-21",
            new_frozen_oos_unauthorized_from="2026-08-22",
            readiness=InputBundleReadiness(
                research_only=True,
                all_six_slots_bound=True,
                ready_for_frozen_alpha_diagnostic_execution=True,
                ready_for_scoring=False,
                ready_for_backtest=False,
                ready_for_portfolio_construction=False,
                ready_for_orders=False,
                ready_for_trading=False,
                auto_apply=False,
            ),
        )
    )


def write_input_bundle(
    path: Path,
    bundle: LayerTwoAlphaInputBundleV2,
    *,
    replace_existing: bool = False,
) -> None:
    _assert_self_hash(bundle)
    if path.exists() and not replace_existing:
        raise FileExistsError(f"input bundle already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def verify_input_bundle(
    *,
    repo_root: Path,
    path: Path = DEFAULT_OUTPUT_PATH,
) -> LayerTwoAlphaInputBundleV2:
    root = repo_root.resolve()
    source = path if path.is_absolute() else root / path
    try:
        bundle = LayerTwoAlphaInputBundleV2.model_validate_json(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("layer-two alpha input bundle missing or invalid") from exc
    _assert_self_hash(bundle)
    rebuilt = build_input_bundle(repo_root=root)
    if bundle != rebuilt:
        raise ValueError("layer-two alpha input bundle does not recompute from bound sources")
    return bundle


__all__ = [
    "DEFAULT_OUTPUT_PATH",
    "LayerTwoAlphaInputBundleV2",
    "build_input_bundle",
    "verify_input_bundle",
    "write_input_bundle",
]
