"""E11b-1a: Content-addressed input inventory for the E11b-0b run contract.

Strict read-only manifest that binds verified market and fundamental inputs to
the frozen diagnostic run contract. Each slot is either `bound` (with verified
cryptographic hashes) or `blocked_missing` (with a closed issue code explaining
why binding is impossible). Derived slots cannot be weakly bound — they must
pass a strict verifier or remain blocked.

This module never modifies source data, never runs scoring/backtest/portfolio,
and never evaluates 2025+ OOS data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_alpha_diagnostic_run_contract import (
    REQUIRED_INPUT_SLOT_KINDS,
    verify_contract_file,
)
from app.storage.fundamental_io import load_verified_fundamental_snapshot
from app.storage.snapshot_io import load_verified_snapshot

INPUT_INVENTORY_SCHEMA_VERSION: Literal["1"] = "1"
INPUT_INVENTORY_VERSION: Literal["layer-two-alpha-diagnostic-input-inventory-v1"] = (
    "layer-two-alpha-diagnostic-input-inventory-v1"
)

BOUND_CONTRACT_ID: Literal["f892b76c2140009e3b6dcad6599def52aaa1f0b62acc91bed747136d92e09df0"] = (
    "f892b76c2140009e3b6dcad6599def52aaa1f0b62acc91bed747136d92e09df0"
)
BOUND_CONTRACT_PATH: Literal["config/research/layer-two-alpha-diagnostic-run-contract-v1.json"] = (
    "config/research/layer-two-alpha-diagnostic-run-contract-v1.json"
)
BOUND_CONTRACT_FILE_SHA256: Literal["91385e6faccd7f7e05fb8792fb1e1c333c478cc5e2006f47153bded7993c7237"] = (
    "91385e6faccd7f7e05fb8792fb1e1c333c478cc5e2006f47153bded7993c7237"
)

REQUIRED_COVERAGE_START = date(2022, 1, 1)
REQUIRED_COVERAGE_END = date(2024, 12, 31)

DERIVED_SLOT_KINDS: frozenset[str] = frozenset(
    {
        "candidate_eligibility_reports",
        "financial_negative_list_reports",
        "statistical_cluster_companion_reports",
    }
)

BOUND_SLOT_KINDS: frozenset[str] = frozenset(
    {
        "sealed_market_snapshot",
        "pit_fundamental_overlay",
        "pit_daily_valuation",
    }
)

BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY = (
    "candidate_eligibility_reports: derived reports do not exist; strict verifier required before binding"
)
BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST = (
    "financial_negative_list_reports: derived reports do not exist; "
    "raw balance-sheet warning fields (cash and interest-bearing debt vs assets; "
    "receivables plus inventory growth vs revenue for two periods; "
    "other receivables vs assets; goodwill vs net assets) "
    "are unavailable in the current fundamental overlay and unknown cannot be treated as clean"
)
BLOCKED_ISSUE_STATISTICAL_CLUSTERS = (
    "statistical_cluster_companion_reports: derived reports do not exist; strict verifier required before binding"
)

_EXPECTED_BLOCKED_ISSUES: dict[str, str] = {
    "candidate_eligibility_reports": BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY,
    "financial_negative_list_reports": BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST,
    "statistical_cluster_companion_reports": BLOCKED_ISSUE_STATISTICAL_CLUSTERS,
}

_OOS_BOUNDARY_RE = re.compile(r"(?:^|[/\-_])oos(?:$|[/\-_])")


# ---------------------------------------------------------------------------
# Frozen base model
# ---------------------------------------------------------------------------


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Slot models
# ---------------------------------------------------------------------------


SlotState = Literal["bound", "blocked_missing"]


class BoundSlot(_StrictFrozen):
    kind: str = Field(min_length=1)
    state: Literal["bound"]
    repo_relative_path: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=64, max_length=64)
    file_sha256: str = Field(min_length=64, max_length=64)
    table_hashes: dict[str, str]
    base_market_snapshot_id: str | None = Field(default=None, min_length=64, max_length=64)
    coverage_start: date
    coverage_end: date
    note: str = Field(min_length=1)

    @field_validator("snapshot_id", "file_sha256", mode="before")
    @classmethod
    def _hex_required(cls, value: object) -> str:
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError("must be a 64-char hex SHA-256")
        if not all(c in "0123456789abcdef" for c in value):
            raise ValueError("must be lowercase hex")
        return value

    @field_validator("base_market_snapshot_id", mode="before")
    @classmethod
    def _hex_base(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError("base_market_snapshot_id must be a 64-char hex SHA-256")
        if not all(c in "0123456789abcdef" for c in value):
            raise ValueError("base_market_snapshot_id must be lowercase hex")
        return value

    @field_validator("table_hashes", mode="before")
    @classmethod
    def _validate_table_hashes(cls, value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ValueError("table_hashes must be a dict")
        for k, v in value.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError("table_hashes keys and values must be strings")
            if len(v) != 64 or not all(c in "0123456789abcdef" for c in v):
                raise ValueError(f"table_hashes[{k}] must be a 64-char lowercase hex SHA-256")
        return value

    @model_validator(mode="after")
    def _coverage_contains_required(self) -> BoundSlot:
        if self.coverage_start > REQUIRED_COVERAGE_START:
            raise ValueError(
                f"BoundSlot {self.kind}: coverage_start {self.coverage_start} "
                f"does not contain required {REQUIRED_COVERAGE_START}"
            )
        if self.coverage_end < REQUIRED_COVERAGE_END:
            raise ValueError(
                f"BoundSlot {self.kind}: coverage_end {self.coverage_end} "
                f"does not contain required {REQUIRED_COVERAGE_END}"
            )
        return self


class BlockedSlot(_StrictFrozen):
    kind: str = Field(min_length=1)
    state: Literal["blocked_missing"]
    issue: str = Field(min_length=1)
    note: str = Field(min_length=1)

    @field_validator("issue", "note", mode="before")
    @classmethod
    def _reject_blank(cls, value: object) -> str:
        if not isinstance(value, str) or value.strip() == "":
            raise ValueError("must be a non-empty string")
        return value


# ---------------------------------------------------------------------------
# Readiness flags
# ---------------------------------------------------------------------------


class InventoryReadinessFlags(_StrictFrozen):
    research_only: Literal[True]
    read_only: Literal[True]
    ready_for_data: Literal[False]
    ready_for_scoring: Literal[False]
    ready_for_backtest: Literal[False]
    ready_for_portfolio_construction: Literal[False]
    ready_for_orders: Literal[False]
    ready_for_trading: Literal[False]
    auto_apply: Literal[False]

    @field_validator(
        "research_only",
        "read_only",
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
            raise ValueError("readiness flag must be a strict bool (not int)")
        return value


# ---------------------------------------------------------------------------
# Top-level inventory
# ---------------------------------------------------------------------------


class LayerTwoAlphaDiagnosticInputInventoryV1(_StrictFrozen):
    schema_version: Literal["1"]
    inventory_version: Literal["layer-two-alpha-diagnostic-input-inventory-v1"]

    contract_id: str = Field(min_length=64, max_length=64)
    contract_path: Literal["config/research/layer-two-alpha-diagnostic-run-contract-v1.json"]
    contract_file_sha256: str = Field(min_length=64, max_length=64)

    slots: tuple[
        BoundSlot | BlockedSlot,
        BoundSlot | BlockedSlot,
        BoundSlot | BlockedSlot,
        BoundSlot | BlockedSlot,
        BoundSlot | BlockedSlot,
        BoundSlot | BlockedSlot,
    ]

    readiness: InventoryReadinessFlags

    inventory_id: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("contract_file_sha256", mode="before")
    @classmethod
    def _hex_lower(cls, value: object) -> str:
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError("must be a 64-char hex SHA-256")
        if not all(c in "0123456789abcdef" for c in value):
            raise ValueError("must be lowercase hex")
        return value

    @field_validator("contract_id", mode="before")
    @classmethod
    def _hex_contract_id(cls, value: object) -> str:
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError("contract_id must be a 64-char hex SHA-256")
        if not all(c in "0123456789abcdef" for c in value):
            raise ValueError("contract_id must be lowercase hex")
        return value

    @field_validator("inventory_id", mode="before")
    @classmethod
    def _hex_lower_optional(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError("inventory_id must be a 64-char hex SHA-256")
        if not all(c in "0123456789abcdef" for c in value):
            raise ValueError("inventory_id must be lowercase hex")
        return value

    @model_validator(mode="after")
    def _validate_slot_kinds(self) -> LayerTwoAlphaDiagnosticInputInventoryV1:
        slot_kinds = tuple(s.kind for s in self.slots)
        if slot_kinds != REQUIRED_INPUT_SLOT_KINDS:
            raise ValueError(f"slot kinds must be exactly {REQUIRED_INPUT_SLOT_KINDS}, got {slot_kinds}")
        seen: set[str] = set()
        for kind in slot_kinds:
            if kind in seen:
                raise ValueError(f"duplicate slot kind: {kind}")
            seen.add(kind)
        return self

    @model_validator(mode="after")
    def _validate_slot_types_and_issues(self) -> LayerTwoAlphaDiagnosticInputInventoryV1:
        for slot in self.slots:
            if slot.kind in DERIVED_SLOT_KINDS:
                if not isinstance(slot, BlockedSlot):
                    raise ValueError(
                        f"derived slot {slot.kind} must be BlockedSlot, "
                        f"got {type(slot).__name__}; binding derived slots is forbidden"
                    )
                expected_issue = _EXPECTED_BLOCKED_ISSUES[slot.kind]
                if slot.issue != expected_issue:
                    raise ValueError(
                        f"blocked slot {slot.kind} issue must be exactly the canonical constant, got {slot.issue!r}"
                    )
            elif slot.kind in BOUND_SLOT_KINDS:
                if not isinstance(slot, BoundSlot):
                    raise ValueError(f"slot {slot.kind} must be BoundSlot, got {type(slot).__name__}")
            else:
                raise ValueError(f"unknown slot kind: {slot.kind}")
        return self

    @model_validator(mode="after")
    def _validate_per_kind_semantics(self) -> LayerTwoAlphaDiagnosticInputInventoryV1:
        market_slot: BoundSlot | None = None
        fund_slot: BoundSlot | None = None
        val_slot: BoundSlot | None = None

        for slot in self.slots:
            if not isinstance(slot, BoundSlot):
                continue
            if slot.kind == "sealed_market_snapshot":
                if slot.base_market_snapshot_id is not None:
                    raise ValueError("sealed_market_snapshot must have base_market_snapshot_id=None")
                market_slot = slot
            elif slot.kind == "pit_fundamental_overlay":
                if slot.base_market_snapshot_id is None:
                    raise ValueError("pit_fundamental_overlay must have non-null base_market_snapshot_id")
                fund_slot = slot
            elif slot.kind == "pit_daily_valuation":
                if slot.base_market_snapshot_id is None:
                    raise ValueError("pit_daily_valuation must have non-null base_market_snapshot_id")
                if "daily_valuation" not in slot.table_hashes:
                    raise ValueError("pit_daily_valuation table_hashes must contain 'daily_valuation' key")
                if len(slot.table_hashes) != 1:
                    raise ValueError("pit_daily_valuation table_hashes must have exactly one entry")
                val_slot = slot

        if market_slot and fund_slot:
            if fund_slot.base_market_snapshot_id != market_slot.snapshot_id:
                raise ValueError(
                    "pit_fundamental_overlay base_market_snapshot_id must equal sealed_market_snapshot snapshot_id"
                )
        if market_slot and val_slot:
            if val_slot.base_market_snapshot_id != market_slot.snapshot_id:
                raise ValueError(
                    "pit_daily_valuation base_market_snapshot_id must equal sealed_market_snapshot snapshot_id"
                )
        if fund_slot and val_slot:
            if fund_slot.snapshot_id != val_slot.snapshot_id:
                raise ValueError("pit_fundamental_overlay and pit_daily_valuation must share the same snapshot_id")
            if fund_slot.base_market_snapshot_id != val_slot.base_market_snapshot_id:
                raise ValueError(
                    "pit_fundamental_overlay and pit_daily_valuation must share the same base_market_snapshot_id"
                )
            if fund_slot.coverage_start != val_slot.coverage_start:
                raise ValueError("pit_fundamental_overlay and pit_daily_valuation must share the same coverage_start")
            if fund_slot.coverage_end != val_slot.coverage_end:
                raise ValueError("pit_fundamental_overlay and pit_daily_valuation must share the same coverage_end")

        return self

    @model_validator(mode="after")
    def _validate_readiness_coherence(self) -> LayerTwoAlphaDiagnosticInputInventoryV1:
        has_blocked = any(s.state == "blocked_missing" for s in self.slots)
        if has_blocked and self.readiness.ready_for_data is not False:
            raise ValueError("ready_for_data must be false when any slot is blocked_missing")
        return self

    @model_validator(mode="after")
    def _validate_contract_binding(self) -> LayerTwoAlphaDiagnosticInputInventoryV1:
        if self.contract_id != BOUND_CONTRACT_ID:
            raise ValueError(f"contract_id must equal {BOUND_CONTRACT_ID}, got {self.contract_id}")
        if self.contract_file_sha256 != BOUND_CONTRACT_FILE_SHA256:
            raise ValueError(
                f"contract_file_sha256 must equal {BOUND_CONTRACT_FILE_SHA256}, got {self.contract_file_sha256}"
            )
        if self.contract_path != BOUND_CONTRACT_PATH:
            raise ValueError(f"contract_path must equal {BOUND_CONTRACT_PATH}, got {self.contract_path}")
        return self


# ---------------------------------------------------------------------------
# Canonical hashing
# ---------------------------------------------------------------------------


def canonical_inventory_payload(
    inventory: LayerTwoAlphaDiagnosticInputInventoryV1,
) -> dict[str, Any]:
    return inventory.model_dump(mode="json", exclude={"inventory_id"})


def canonical_inventory_bytes(
    inventory: LayerTwoAlphaDiagnosticInputInventoryV1,
) -> bytes:
    payload = canonical_inventory_payload(inventory)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_inventory_id(
    inventory: LayerTwoAlphaDiagnosticInputInventoryV1,
) -> str:
    return hashlib.sha256(canonical_inventory_bytes(inventory)).hexdigest()


def seal_inventory(
    inventory: LayerTwoAlphaDiagnosticInputInventoryV1,
) -> LayerTwoAlphaDiagnosticInputInventoryV1:
    """Seal by computing self-hash and revalidating through model_validate."""
    iid = compute_inventory_id(inventory)
    payload = inventory.model_dump(mode="json")
    payload["inventory_id"] = iid
    return LayerTwoAlphaDiagnosticInputInventoryV1.model_validate(payload)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _validate_repo_path(relative: str, *, repo_root: Path, field_name: str) -> Path:
    if ".." in relative.split("/"):
        raise ValueError(f"{field_name} contains '..' path escape")
    if relative.startswith("/"):
        raise ValueError(f"{field_name} must be repo-relative, not absolute")
    root_resolved = repo_root.resolve()
    current = root_resolved
    for component in Path(relative).parts:
        current = current / component
        if current.is_symlink():
            raise ValueError(f"{field_name} has a symlink component (forbidden): {relative}")
    full = (repo_root / relative).resolve()
    if not str(full).startswith(str(root_resolved) + os.sep) and full != root_resolved:
        raise ValueError(f"{field_name} escapes repo root")
    return full


def _validate_repo_dir(relative: str, *, repo_root: Path, field_name: str) -> Path:
    full = _validate_repo_path(relative, repo_root=repo_root, field_name=field_name)
    if not full.exists():
        raise ValueError(f"{field_name} does not exist: {relative}")
    if not full.is_dir():
        raise ValueError(f"{field_name} is not a directory: {relative}")
    return full


def _validate_repo_file(relative: str, *, repo_root: Path, field_name: str) -> Path:
    full = _validate_repo_path(relative, repo_root=repo_root, field_name=field_name)
    if not full.exists():
        raise ValueError(f"{field_name} does not exist: {relative}")
    if not full.is_file():
        raise ValueError(f"{field_name} is not a regular file: {relative}")
    return full


def _reject_oos_namespace(path_str: str) -> None:
    lower = path_str.lower()
    if "2025" in lower:
        raise ValueError(f"path references 2025/OOS namespace (forbidden): {path_str}")
    if _OOS_BOUNDARY_RE.search(lower):
        raise ValueError(f"path references 2025/OOS namespace (forbidden): {path_str}")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_inventory_self_hash(
    inventory: LayerTwoAlphaDiagnosticInputInventoryV1,
) -> None:
    if inventory.inventory_id is None:
        raise ValueError("inventory_id is missing (not sealed)")
    expected = compute_inventory_id(inventory)
    if inventory.inventory_id != expected:
        raise ValueError(
            f"inventory_id does not match canonical content hash: stored={inventory.inventory_id}, computed={expected}"
        )


def verify_inventory_semantic(
    inventory: LayerTwoAlphaDiagnosticInputInventoryV1,
) -> None:
    """Re-validate all model invariants by round-tripping through model_validate."""
    verify_inventory_self_hash(inventory)
    payload = inventory.model_dump(mode="json")
    LayerTwoAlphaDiagnosticInputInventoryV1.model_validate(payload)


def verify_inventory_sources(
    inventory: LayerTwoAlphaDiagnosticInputInventoryV1,
    *,
    repo_root: Path,
) -> None:
    """Full source verification: contract file, market, fundamental, valuation."""
    root = Path(repo_root).resolve()

    verify_inventory_self_hash(inventory)
    payload = inventory.model_dump(mode="json")
    LayerTwoAlphaDiagnosticInputInventoryV1.model_validate(payload)

    contract_full = _validate_repo_file(inventory.contract_path, repo_root=root, field_name="contract_path")
    contract_sha = _sha256_file(contract_full)
    if contract_sha != inventory.contract_file_sha256:
        raise ValueError(
            f"contract file SHA-256 mismatch: expected {inventory.contract_file_sha256}, got {contract_sha}"
        )
    _contract, _ = verify_contract_file(contract_path=contract_full, repo_root=root)
    if _contract.contract_id != inventory.contract_id:
        raise ValueError("contract_id in file does not match inventory contract_id")

    for slot in inventory.slots:
        if isinstance(slot, BlockedSlot):
            continue
        assert isinstance(slot, BoundSlot)

        _reject_oos_namespace(slot.repo_relative_path)
        _validate_repo_path(slot.repo_relative_path, repo_root=root, field_name=f"slot[{slot.kind}].repo_relative_path")

        if slot.kind == "sealed_market_snapshot":
            market_dir = root / slot.repo_relative_path
            if not market_dir.is_dir():
                raise ValueError(f"market dir does not exist: {slot.repo_relative_path}")
            market_snapshot = load_verified_snapshot(market_dir)
            if market_snapshot.snapshot_id != slot.snapshot_id:
                raise ValueError(
                    f"market snapshot_id mismatch: inventory={slot.snapshot_id}, actual={market_snapshot.snapshot_id}"
                )
            mkt_manifest_sha = _sha256_file(market_dir / "manifest.json")
            if mkt_manifest_sha != slot.file_sha256:
                raise ValueError("market manifest file_sha256 mismatch")
            if market_snapshot.table_hashes != slot.table_hashes:
                raise ValueError("market table_hashes mismatch")
            if market_snapshot.coverage_start != slot.coverage_start:
                raise ValueError("market coverage_start mismatch")
            if market_snapshot.coverage_end != slot.coverage_end:
                raise ValueError("market coverage_end mismatch")

        elif slot.kind == "pit_fundamental_overlay":
            fund_dir = root / slot.repo_relative_path
            if not fund_dir.is_dir():
                raise ValueError(f"fundamental dir does not exist: {slot.repo_relative_path}")
            fund_snapshot, _ = load_verified_fundamental_snapshot(fund_dir)
            if fund_snapshot.snapshot_id != slot.snapshot_id:
                raise ValueError("fundamental snapshot_id mismatch")
            fund_manifest_sha = _sha256_file(fund_dir / "manifest.json")
            if fund_manifest_sha != slot.file_sha256:
                raise ValueError("fundamental manifest file_sha256 mismatch")
            if fund_snapshot.table_hashes != slot.table_hashes:
                raise ValueError("fundamental table_hashes mismatch")
            if fund_snapshot.base_market_snapshot_id != slot.base_market_snapshot_id:
                raise ValueError("fundamental base_market_snapshot_id mismatch")
            if fund_snapshot.coverage_start != slot.coverage_start:
                raise ValueError("fundamental coverage_start mismatch")
            if fund_snapshot.coverage_end != slot.coverage_end:
                raise ValueError("fundamental coverage_end mismatch")

            market_slot = next(s for s in inventory.slots if s.kind == "sealed_market_snapshot")
            assert isinstance(market_slot, BoundSlot)
            if slot.base_market_snapshot_id != market_slot.snapshot_id:
                raise ValueError("fundamental base_market_snapshot_id does not match market slot snapshot_id")

        elif slot.kind == "pit_daily_valuation":
            valuation_file = root / slot.repo_relative_path
            if not valuation_file.is_file():
                raise ValueError(f"daily_valuation.parquet does not exist: {slot.repo_relative_path}")
            actual_sha = _sha256_file(valuation_file)
            if actual_sha != slot.file_sha256:
                raise ValueError(
                    f"daily_valuation.parquet SHA-256 mismatch: inventory={slot.file_sha256}, actual={actual_sha}"
                )
            fund_dir = valuation_file.parent
            fund_snapshot, _ = load_verified_fundamental_snapshot(fund_dir)
            if fund_snapshot.snapshot_id != slot.snapshot_id:
                raise ValueError(
                    f"valuation snapshot_id mismatch: inventory={slot.snapshot_id}, "
                    f"actual fundamental={fund_snapshot.snapshot_id}"
                )
            dv_hash = fund_snapshot.table_hashes.get("daily_valuation")
            if dv_hash is None:
                raise ValueError("fundamental snapshot missing daily_valuation table hash")
            expected_table_hashes = {"daily_valuation": dv_hash}
            if slot.table_hashes != expected_table_hashes:
                raise ValueError("pit_daily_valuation table_hashes mismatch")
            if fund_snapshot.base_market_snapshot_id != slot.base_market_snapshot_id:
                raise ValueError("valuation base_market_snapshot_id mismatch")
            if fund_snapshot.coverage_start != slot.coverage_start:
                raise ValueError("valuation coverage_start mismatch")
            if fund_snapshot.coverage_end != slot.coverage_end:
                raise ValueError("valuation coverage_end mismatch")


def verify_inventory(
    path: Path,
    *,
    repo_root: Path,
) -> LayerTwoAlphaDiagnosticInputInventoryV1:
    """Load, verify self-hash, semantic invariants, and all source bindings."""
    inventory = load_inventory(path)
    verify_inventory_sources(inventory, repo_root=repo_root)
    return inventory


# ---------------------------------------------------------------------------
# Inventory builder
# ---------------------------------------------------------------------------


def build_input_inventory(
    *,
    market_dir: Path,
    fundamental_dir: Path,
    repo_root: Path,
) -> LayerTwoAlphaDiagnosticInputInventoryV1:
    """Build the input inventory by verifying actual data sources."""
    root = Path(repo_root).resolve()

    contract_path = root / BOUND_CONTRACT_PATH
    _contract, _ = verify_contract_file(contract_path=contract_path, repo_root=root)
    contract_file_sha = _sha256_file(contract_path)
    if contract_file_sha != BOUND_CONTRACT_FILE_SHA256:
        raise ValueError(
            f"contract file SHA-256 mismatch: expected {BOUND_CONTRACT_FILE_SHA256}, got {contract_file_sha}"
        )
    if _contract.contract_id != BOUND_CONTRACT_ID:
        raise ValueError(f"contract_id mismatch: expected {BOUND_CONTRACT_ID}, got {_contract.contract_id}")

    resolved_market = Path(market_dir).resolve()
    market_dir_str = str(resolved_market.relative_to(root))
    _reject_oos_namespace(market_dir_str)
    _validate_repo_dir(market_dir_str, repo_root=root, field_name="market_dir")

    market_snapshot = load_verified_snapshot(resolved_market)
    if market_snapshot.coverage_start is None or market_snapshot.coverage_end is None:
        raise ValueError("market snapshot has no coverage dates")
    if market_snapshot.coverage_start > REQUIRED_COVERAGE_START:
        raise ValueError(
            f"market coverage_start {market_snapshot.coverage_start} does not contain "
            f"required {REQUIRED_COVERAGE_START}"
        )
    if market_snapshot.coverage_end < REQUIRED_COVERAGE_END:
        raise ValueError(
            f"market coverage_end {market_snapshot.coverage_end} does not contain required {REQUIRED_COVERAGE_END}"
        )

    resolved_fundamental = Path(fundamental_dir).resolve()
    fundamental_dir_str = str(resolved_fundamental.relative_to(root))
    _reject_oos_namespace(fundamental_dir_str)
    _validate_repo_dir(fundamental_dir_str, repo_root=root, field_name="fundamental_dir")

    fundamental_snapshot, _ = load_verified_fundamental_snapshot(resolved_fundamental)
    if fundamental_snapshot.base_market_snapshot_id != market_snapshot.snapshot_id:
        raise ValueError("fundamental overlay base_market_snapshot_id does not match the market snapshot_id")
    if fundamental_snapshot.coverage_start is None or fundamental_snapshot.coverage_end is None:
        raise ValueError("fundamental snapshot has no coverage dates")
    if fundamental_snapshot.coverage_start > REQUIRED_COVERAGE_START:
        raise ValueError(
            f"fundamental coverage_start {fundamental_snapshot.coverage_start} does not contain "
            f"required {REQUIRED_COVERAGE_START}"
        )
    if fundamental_snapshot.coverage_end < REQUIRED_COVERAGE_END:
        raise ValueError(
            f"fundamental coverage_end {fundamental_snapshot.coverage_end} does not contain "
            f"required {REQUIRED_COVERAGE_END}"
        )

    market_manifest_sha = _sha256_file(resolved_market / "manifest.json")
    fund_manifest_sha = _sha256_file(resolved_fundamental / "manifest.json")

    valuation_parquet_path = resolved_fundamental / "daily_valuation.parquet"
    if not valuation_parquet_path.is_file():
        raise ValueError("daily_valuation.parquet not found in fundamental dir")
    valuation_parquet_sha = _sha256_file(valuation_parquet_path)
    dv_table_hash = fundamental_snapshot.table_hashes.get("daily_valuation")
    if dv_table_hash is None:
        raise ValueError("fundamental snapshot missing daily_valuation table hash")

    valuation_relative = fundamental_dir_str + "/daily_valuation.parquet"

    sealed_market_slot = BoundSlot(
        kind="sealed_market_snapshot",
        state="bound",
        repo_relative_path=market_dir_str,
        snapshot_id=market_snapshot.snapshot_id,
        file_sha256=market_manifest_sha,
        table_hashes=market_snapshot.table_hashes,
        base_market_snapshot_id=None,
        coverage_start=market_snapshot.coverage_start,
        coverage_end=market_snapshot.coverage_end,
        note="PIT sealed adjusted-close market snapshot for all eligible names",
    )

    pit_fundamental_slot = BoundSlot(
        kind="pit_fundamental_overlay",
        state="bound",
        repo_relative_path=fundamental_dir_str,
        snapshot_id=fundamental_snapshot.snapshot_id,
        file_sha256=fund_manifest_sha,
        table_hashes=fundamental_snapshot.table_hashes,
        base_market_snapshot_id=fundamental_snapshot.base_market_snapshot_id,
        coverage_start=fundamental_snapshot.coverage_start,
        coverage_end=fundamental_snapshot.coverage_end,
        note="PIT fundamental overlay (quality family components)",
    )

    pit_valuation_slot = BoundSlot(
        kind="pit_daily_valuation",
        state="bound",
        repo_relative_path=valuation_relative,
        snapshot_id=fundamental_snapshot.snapshot_id,
        file_sha256=valuation_parquet_sha,
        table_hashes={"daily_valuation": dv_table_hash},
        base_market_snapshot_id=fundamental_snapshot.base_market_snapshot_id,
        coverage_start=fundamental_snapshot.coverage_start,
        coverage_end=fundamental_snapshot.coverage_end,
        note="PIT daily valuation multiples (value family components)",
    )

    candidate_eligibility_slot = BlockedSlot(
        kind="candidate_eligibility_reports",
        state="blocked_missing",
        issue=BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY,
        note="Layer-two candidate eligibility verdicts per decision date",
    )

    financial_negative_list_slot = BlockedSlot(
        kind="financial_negative_list_reports",
        state="blocked_missing",
        issue=BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST,
        note="Financial negative-list verdicts per decision date",
    )

    statistical_cluster_slot = BlockedSlot(
        kind="statistical_cluster_companion_reports",
        state="blocked_missing",
        issue=BLOCKED_ISSUE_STATISTICAL_CLUSTERS,
        note="Statistical risk cluster assignments per monthly anchor",
    )

    readiness = InventoryReadinessFlags(
        research_only=True,
        read_only=True,
        ready_for_data=False,
        ready_for_scoring=False,
        ready_for_backtest=False,
        ready_for_portfolio_construction=False,
        ready_for_orders=False,
        ready_for_trading=False,
        auto_apply=False,
    )

    inventory = LayerTwoAlphaDiagnosticInputInventoryV1(
        schema_version="1",
        inventory_version=INPUT_INVENTORY_VERSION,
        contract_id=BOUND_CONTRACT_ID,
        contract_path=BOUND_CONTRACT_PATH,
        contract_file_sha256=BOUND_CONTRACT_FILE_SHA256,
        slots=(
            sealed_market_slot,
            candidate_eligibility_slot,
            financial_negative_list_slot,
            pit_fundamental_slot,
            pit_valuation_slot,
            statistical_cluster_slot,
        ),
        readiness=readiness,
        inventory_id=None,
    )

    return seal_inventory(inventory)


# ---------------------------------------------------------------------------
# Load / write
# ---------------------------------------------------------------------------


def load_inventory(path: Path) -> LayerTwoAlphaDiagnosticInputInventoryV1:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("inventory file is missing or unreadable") from exc
    return LayerTwoAlphaDiagnosticInputInventoryV1.model_validate(payload)


def write_inventory(
    path: Path,
    inventory: LayerTwoAlphaDiagnosticInputInventoryV1,
    *,
    repo_root: Path,
    replace_existing: bool = False,
) -> LayerTwoAlphaDiagnosticInputInventoryV1:
    """Seal, verify semantics+sources, then write atomically."""
    destination = Path(path)
    if destination.exists() and not replace_existing:
        raise FileExistsError(f"inventory already exists at {destination}; pass replace_existing=True to overwrite")
    sealed = seal_inventory(inventory) if inventory.inventory_id is None else inventory
    verify_inventory_sources(sealed, repo_root=repo_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = sealed.model_dump(mode="json")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = destination.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(destination)
    return sealed


__all__ = [
    "BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY",
    "BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST",
    "BLOCKED_ISSUE_STATISTICAL_CLUSTERS",
    "BOUND_CONTRACT_FILE_SHA256",
    "BOUND_CONTRACT_ID",
    "BOUND_CONTRACT_PATH",
    "BOUND_SLOT_KINDS",
    "BlockedSlot",
    "BoundSlot",
    "DERIVED_SLOT_KINDS",
    "INPUT_INVENTORY_SCHEMA_VERSION",
    "INPUT_INVENTORY_VERSION",
    "InventoryReadinessFlags",
    "LayerTwoAlphaDiagnosticInputInventoryV1",
    "REQUIRED_COVERAGE_END",
    "REQUIRED_COVERAGE_START",
    "build_input_inventory",
    "canonical_inventory_bytes",
    "canonical_inventory_payload",
    "compute_inventory_id",
    "load_inventory",
    "seal_inventory",
    "verify_inventory",
    "verify_inventory_self_hash",
    "verify_inventory_semantic",
    "verify_inventory_sources",
    "write_inventory",
]
