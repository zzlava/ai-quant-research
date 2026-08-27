"""Tests for E11b-1a layer-two alpha diagnostic input inventory.

Attack-oriented: every test targets a specific invariant/rejection rule.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.research.layer_two_alpha_diagnostic_input_inventory import (
    BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY,
    BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST,
    BLOCKED_ISSUE_STATISTICAL_CLUSTERS,
    BOUND_CONTRACT_FILE_SHA256,
    BOUND_CONTRACT_ID,
    BOUND_CONTRACT_PATH,
    BOUND_SLOT_KINDS,
    DERIVED_SLOT_KINDS,
    INPUT_INVENTORY_SCHEMA_VERSION,
    INPUT_INVENTORY_VERSION,
    REQUIRED_COVERAGE_END,
    REQUIRED_COVERAGE_START,
    BlockedSlot,
    BoundSlot,
    InventoryReadinessFlags,
    LayerTwoAlphaDiagnosticInputInventoryV1,
    canonical_inventory_bytes,
    compute_inventory_id,
    load_inventory,
    seal_inventory,
    verify_inventory_self_hash,
    verify_inventory_semantic,
)
from app.research.layer_two_alpha_diagnostic_run_contract import REQUIRED_INPUT_SLOT_KINDS

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bound_slot(kind: str, snapshot_id: str = "a" * 64) -> BoundSlot:
    if kind == "pit_daily_valuation":
        table_hashes = {"daily_valuation": "c" * 64}
    else:
        table_hashes = {"daily_bars": "c" * 64}
    return BoundSlot(
        kind=kind,
        state="bound",
        repo_relative_path="data/test/daily_valuation.parquet" if kind == "pit_daily_valuation" else "data/test",
        snapshot_id=snapshot_id,
        file_sha256="b" * 64,
        table_hashes=table_hashes,
        base_market_snapshot_id=None if kind == "sealed_market_snapshot" else "a" * 64,
        coverage_start=date(2021, 10, 8),
        coverage_end=date(2024, 12, 31),
        note=f"test slot {kind}",
    )


def _make_blocked_slot(kind: str, issue: str = "test issue") -> BlockedSlot:
    return BlockedSlot(
        kind=kind,
        state="blocked_missing",
        issue=issue,
        note=f"test blocked slot {kind}",
    )


def _make_inventory(
    slots: tuple | None = None,
    readiness: InventoryReadinessFlags | None = None,
    **overrides: Any,
) -> LayerTwoAlphaDiagnosticInputInventoryV1:
    if slots is None:
        slots = (
            _make_bound_slot("sealed_market_snapshot"),
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            _make_bound_slot("pit_fundamental_overlay"),
            _make_bound_slot("pit_daily_valuation"),
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
    if readiness is None:
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
    defaults: dict[str, Any] = {
        "schema_version": "1",
        "inventory_version": INPUT_INVENTORY_VERSION,
        "contract_id": BOUND_CONTRACT_ID,
        "contract_path": BOUND_CONTRACT_PATH,
        "contract_file_sha256": BOUND_CONTRACT_FILE_SHA256,
        "slots": slots,
        "readiness": readiness,
        "inventory_id": None,
    }
    defaults.update(overrides)
    return LayerTwoAlphaDiagnosticInputInventoryV1(**defaults)


# ---------------------------------------------------------------------------
# Self-hash determinism
# ---------------------------------------------------------------------------


class TestSelfHash:
    def test_deterministic_inventory_id(self) -> None:
        inv1 = seal_inventory(_make_inventory())
        inv2 = seal_inventory(_make_inventory())
        assert inv1.inventory_id is not None
        assert inv1.inventory_id == inv2.inventory_id
        assert len(inv1.inventory_id) == 64

    def test_inventory_id_matches_recomputation(self) -> None:
        sealed = seal_inventory(_make_inventory())
        recomputed = compute_inventory_id(sealed)
        assert sealed.inventory_id == recomputed

    def test_canonical_bytes_exclude_inventory_id(self) -> None:
        unsealed = _make_inventory()
        sealed = seal_inventory(unsealed)
        b1 = canonical_inventory_bytes(unsealed)
        b2 = canonical_inventory_bytes(sealed)
        assert b1 == b2

    def test_tampered_inventory_id_fails_verify(self) -> None:
        sealed = seal_inventory(_make_inventory())
        tampered = sealed.model_copy(update={"inventory_id": "f" * 64})
        with pytest.raises(ValueError, match="does not match canonical content hash"):
            verify_inventory_self_hash(tampered)

    def test_unsealed_inventory_fails_verify(self) -> None:
        unsealed = _make_inventory()
        with pytest.raises(ValueError, match="inventory_id is missing"):
            verify_inventory_self_hash(unsealed)

    def test_seal_revalidates_payload(self) -> None:
        """seal_inventory uses model_validate, not model_copy, so validators run."""
        inv = _make_inventory()
        sealed = seal_inventory(inv)
        assert sealed.inventory_id is not None
        verify_inventory_semantic(sealed)


# ---------------------------------------------------------------------------
# ATTACK REGRESSION 1: contract_path changed
# ---------------------------------------------------------------------------


class TestAttack1ContractPath:
    def test_rejects_wrong_contract_path(self) -> None:
        with pytest.raises(ValidationError):
            _make_inventory(contract_path="config/research/other.json")

    def test_contract_path_must_equal_bound(self) -> None:
        inv = _make_inventory()
        assert inv.contract_path == BOUND_CONTRACT_PATH


# ---------------------------------------------------------------------------
# ATTACK REGRESSION 2: derived slot changed to BoundSlot
# ---------------------------------------------------------------------------


class TestAttack2DerivedSlotBound:
    def test_candidate_eligibility_must_be_blocked(self) -> None:
        slots = (
            _make_bound_slot("sealed_market_snapshot"),
            _make_bound_slot("candidate_eligibility_reports"),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            _make_bound_slot("pit_fundamental_overlay"),
            _make_bound_slot("pit_daily_valuation"),
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
        with pytest.raises(ValidationError, match="must be BlockedSlot"):
            _make_inventory(slots=slots)

    def test_financial_negative_list_must_be_blocked(self) -> None:
        slots = (
            _make_bound_slot("sealed_market_snapshot"),
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_bound_slot("financial_negative_list_reports"),
            _make_bound_slot("pit_fundamental_overlay"),
            _make_bound_slot("pit_daily_valuation"),
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
        with pytest.raises(ValidationError, match="must be BlockedSlot"):
            _make_inventory(slots=slots)

    def test_statistical_cluster_must_be_blocked(self) -> None:
        slots = (
            _make_bound_slot("sealed_market_snapshot"),
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            _make_bound_slot("pit_fundamental_overlay"),
            _make_bound_slot("pit_daily_valuation"),
            _make_bound_slot("statistical_cluster_companion_reports"),
        )
        with pytest.raises(ValidationError, match="must be BlockedSlot"):
            _make_inventory(slots=slots)


# ---------------------------------------------------------------------------
# ATTACK REGRESSION 3: financial blocked issue changed
# ---------------------------------------------------------------------------


class TestAttack3FinancialIssue:
    def test_rejects_altered_financial_issue(self) -> None:
        slots = (
            _make_bound_slot("sealed_market_snapshot"),
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_blocked_slot("financial_negative_list_reports", "all clean, no issues"),
            _make_bound_slot("pit_fundamental_overlay"),
            _make_bound_slot("pit_daily_valuation"),
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
        with pytest.raises(ValidationError, match="must be exactly the canonical constant"):
            _make_inventory(slots=slots)

    def test_financial_issue_mentions_correct_e10b_rules(self) -> None:
        assert "cash and interest-bearing debt vs assets" in BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST
        assert "receivables plus inventory growth vs revenue" in BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST
        assert "other receivables vs assets" in BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST
        assert "goodwill vs net assets" in BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST
        assert "unknown cannot be treated as clean" in BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST

    def test_financial_issue_does_not_mention_wrong_fields(self) -> None:
        assert "current_ratio" not in BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST
        assert "quick_ratio" not in BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST
        assert "cash_ratio" not in BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST
        assert "debt_to_equity" not in BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST


# ---------------------------------------------------------------------------
# ATTACK REGRESSION 4: fundamental coverage_start changed
# ---------------------------------------------------------------------------


class TestAttack4FundamentalCoverage:
    def test_bound_slot_rejects_coverage_start_after_required(self) -> None:
        with pytest.raises(ValidationError, match="coverage_start.*does not contain"):
            BoundSlot(
                kind="pit_fundamental_overlay",
                state="bound",
                repo_relative_path="data/test",
                snapshot_id="a" * 64,
                file_sha256="b" * 64,
                table_hashes={"fundamental_reports": "c" * 64},
                base_market_snapshot_id="d" * 64,
                coverage_start=date(2023, 1, 1),
                coverage_end=date(2024, 12, 31),
                note="test",
            )

    def test_bound_slot_rejects_coverage_end_before_required(self) -> None:
        with pytest.raises(ValidationError, match="coverage_end.*does not contain"):
            BoundSlot(
                kind="sealed_market_snapshot",
                state="bound",
                repo_relative_path="data/test",
                snapshot_id="a" * 64,
                file_sha256="b" * 64,
                table_hashes={"daily_bars": "c" * 64},
                base_market_snapshot_id=None,
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 6, 30),
                note="test",
            )


# ---------------------------------------------------------------------------
# ATTACK REGRESSION 5: market snapshot_id changed
# ---------------------------------------------------------------------------


class TestAttack5MarketSnapshotId:
    def test_snapshot_id_is_bound_in_model(self) -> None:
        inv = _make_inventory()
        market_slot = inv.slots[0]
        assert isinstance(market_slot, BoundSlot)
        assert market_slot.snapshot_id == "a" * 64

    def test_altered_snapshot_id_detected(self) -> None:
        sealed = seal_inventory(_make_inventory())
        payload = sealed.model_dump(mode="json")
        payload["slots"][0]["snapshot_id"] = "0" * 64
        payload["inventory_id"] = sealed.inventory_id
        with pytest.raises((ValidationError, ValueError)):
            inv = LayerTwoAlphaDiagnosticInputInventoryV1.model_validate(payload)
            verify_inventory_self_hash(inv)


# ---------------------------------------------------------------------------
# ATTACK REGRESSION 6: valuation file_sha256/table_hashes set None
# ---------------------------------------------------------------------------


class TestAttack6ValuationNullMetadata:
    def test_bound_slot_requires_file_sha256(self) -> None:
        with pytest.raises(ValidationError):
            BoundSlot(
                kind="pit_daily_valuation",
                state="bound",
                repo_relative_path="data/test",
                snapshot_id="a" * 64,
                file_sha256=None,
                table_hashes={"daily_valuation": "c" * 64},
                base_market_snapshot_id="d" * 64,
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 12, 31),
                note="test",
            )

    def test_bound_slot_requires_table_hashes(self) -> None:
        with pytest.raises(ValidationError):
            BoundSlot(
                kind="pit_daily_valuation",
                state="bound",
                repo_relative_path="data/test",
                snapshot_id="a" * 64,
                file_sha256="b" * 64,
                table_hashes=None,
                base_market_snapshot_id="d" * 64,
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 12, 31),
                note="test",
            )


# ---------------------------------------------------------------------------
# ATTACK REGRESSION 7: hyphenated -oos- namespace passes
# ---------------------------------------------------------------------------


class TestAttack7HyphenatedOos:
    def test_rejects_hyphenated_oos_in_path(self) -> None:
        from app.research.layer_two_alpha_diagnostic_input_inventory import _reject_oos_namespace

        with pytest.raises(ValueError, match="2025/OOS"):
            _reject_oos_namespace("data/all-a-share-oos-20241001-20260821-v1/parquet")

    def test_rejects_oos_segment(self) -> None:
        from app.research.layer_two_alpha_diagnostic_input_inventory import _reject_oos_namespace

        with pytest.raises(ValueError, match="2025/OOS"):
            _reject_oos_namespace("data/oos/something")

    def test_rejects_trailing_oos(self) -> None:
        from app.research.layer_two_alpha_diagnostic_input_inventory import _reject_oos_namespace

        with pytest.raises(ValueError, match="2025/OOS"):
            _reject_oos_namespace("data/market-oos/parquet")


# ---------------------------------------------------------------------------
# Slot kind validation
# ---------------------------------------------------------------------------


class TestSlotKinds:
    def test_rejects_missing_slot_kind(self) -> None:
        slots = (
            _make_bound_slot("sealed_market_snapshot"),
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            _make_bound_slot("pit_fundamental_overlay"),
            _make_bound_slot("pit_daily_valuation"),
        )
        with pytest.raises(ValidationError):
            _make_inventory(slots=slots)

    def test_rejects_extra_slot_kind(self) -> None:
        slots = (
            _make_bound_slot("sealed_market_snapshot"),
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            _make_bound_slot("pit_fundamental_overlay"),
            _make_bound_slot("pit_daily_valuation"),
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
            _make_bound_slot("extra_unexpected"),
        )
        with pytest.raises(ValidationError):
            _make_inventory(slots=slots)

    def test_rejects_duplicate_slot_kind(self) -> None:
        slots = (
            _make_bound_slot("sealed_market_snapshot"),
            _make_bound_slot("sealed_market_snapshot"),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            _make_bound_slot("pit_fundamental_overlay"),
            _make_bound_slot("pit_daily_valuation"),
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
        with pytest.raises(ValidationError, match="slot kinds must be exactly"):
            _make_inventory(slots=slots)

    def test_rejects_wrong_order(self) -> None:
        slots = (
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_bound_slot("sealed_market_snapshot"),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            _make_bound_slot("pit_fundamental_overlay"),
            _make_bound_slot("pit_daily_valuation"),
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
        with pytest.raises(ValidationError, match="slot kinds must be exactly"):
            _make_inventory(slots=slots)


# ---------------------------------------------------------------------------
# Readiness flags
# ---------------------------------------------------------------------------


class TestReadiness:
    def test_all_flags_correct_for_blocked_inventory(self) -> None:
        inv = _make_inventory()
        assert inv.readiness.research_only is True
        assert inv.readiness.read_only is True
        assert inv.readiness.ready_for_data is False
        assert inv.readiness.ready_for_scoring is False
        assert inv.readiness.ready_for_backtest is False
        assert inv.readiness.ready_for_portfolio_construction is False
        assert inv.readiness.ready_for_orders is False
        assert inv.readiness.ready_for_trading is False
        assert inv.readiness.auto_apply is False

    def test_rejects_ready_for_data_true(self) -> None:
        with pytest.raises(ValidationError):
            InventoryReadinessFlags(
                research_only=True,
                read_only=True,
                ready_for_data=True,
                ready_for_scoring=False,
                ready_for_backtest=False,
                ready_for_portfolio_construction=False,
                ready_for_orders=False,
                ready_for_trading=False,
                auto_apply=False,
            )

    def test_rejects_bool_as_int(self) -> None:
        with pytest.raises(ValidationError, match="strict bool"):
            InventoryReadinessFlags(
                research_only=True,
                read_only=True,
                ready_for_data=0,
                ready_for_scoring=False,
                ready_for_backtest=False,
                ready_for_portfolio_construction=False,
                ready_for_orders=False,
                ready_for_trading=False,
                auto_apply=False,
            )

    def test_rejects_int_one_as_true(self) -> None:
        with pytest.raises(ValidationError, match="strict bool"):
            InventoryReadinessFlags(
                research_only=1,
                read_only=True,
                ready_for_data=False,
                ready_for_scoring=False,
                ready_for_backtest=False,
                ready_for_portfolio_construction=False,
                ready_for_orders=False,
                ready_for_trading=False,
                auto_apply=False,
            )


# ---------------------------------------------------------------------------
# Contract binding
# ---------------------------------------------------------------------------


class TestContractBinding:
    def test_rejects_wrong_contract_id(self) -> None:
        with pytest.raises(ValidationError, match="contract_id must equal"):
            _make_inventory(contract_id="0" * 64)

    def test_rejects_wrong_contract_file_sha256(self) -> None:
        with pytest.raises(ValidationError, match="contract_file_sha256 must equal"):
            _make_inventory(contract_file_sha256="0" * 64)

    def test_rejects_non_hex_contract_id(self) -> None:
        with pytest.raises(ValidationError):
            _make_inventory(contract_id="g" * 64)

    def test_rejects_short_contract_id(self) -> None:
        with pytest.raises(ValidationError):
            _make_inventory(contract_id="a" * 63)


# ---------------------------------------------------------------------------
# BoundSlot validation
# ---------------------------------------------------------------------------


class TestBoundSlotValidation:
    def test_rejects_non_hex_snapshot_id(self) -> None:
        with pytest.raises(ValidationError, match="lowercase hex"):
            BoundSlot(
                kind="sealed_market_snapshot",
                state="bound",
                repo_relative_path="data/test",
                snapshot_id="G" * 64,
                file_sha256="b" * 64,
                table_hashes={"daily_bars": "c" * 64},
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 12, 31),
                note="test",
            )

    def test_rejects_short_snapshot_id(self) -> None:
        with pytest.raises(ValidationError, match="64-char"):
            BoundSlot(
                kind="sealed_market_snapshot",
                state="bound",
                repo_relative_path="data/test",
                snapshot_id="a" * 63,
                file_sha256="b" * 64,
                table_hashes={"daily_bars": "c" * 64},
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 12, 31),
                note="test",
            )

    def test_rejects_non_hex_file_sha256(self) -> None:
        with pytest.raises(ValidationError, match="lowercase hex"):
            BoundSlot(
                kind="sealed_market_snapshot",
                state="bound",
                repo_relative_path="data/test",
                snapshot_id="a" * 64,
                file_sha256="Z" * 64,
                table_hashes={"daily_bars": "c" * 64},
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 12, 31),
                note="test",
            )

    def test_rejects_non_hex_table_hash_value(self) -> None:
        with pytest.raises(ValidationError, match="64-char lowercase hex"):
            BoundSlot(
                kind="sealed_market_snapshot",
                state="bound",
                repo_relative_path="data/test",
                snapshot_id="a" * 64,
                file_sha256="b" * 64,
                table_hashes={"daily_bars": "INVALID"},
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 12, 31),
                note="test",
            )


# ---------------------------------------------------------------------------
# BlockedSlot validation
# ---------------------------------------------------------------------------


class TestBlockedSlotValidation:
    def test_rejects_empty_issue(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            BlockedSlot(
                kind="candidate_eligibility_reports",
                state="blocked_missing",
                issue="   ",
                note="test",
            )

    def test_rejects_empty_note(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            BlockedSlot(
                kind="candidate_eligibility_reports",
                state="blocked_missing",
                issue="test issue",
                note="   ",
            )


# ---------------------------------------------------------------------------
# Write / load round-trip
# ---------------------------------------------------------------------------


class TestWriteLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        inv = seal_inventory(_make_inventory())
        out = tmp_path / "inventory.json"
        payload = inv.model_dump(mode="json")
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        out.write_text(text, encoding="utf-8")
        loaded = load_inventory(out)
        assert loaded.inventory_id == inv.inventory_id
        assert loaded == inv

    def test_load_rejects_tampered_file(self, tmp_path: Path) -> None:
        inv = seal_inventory(_make_inventory())
        out = tmp_path / "inventory.json"
        payload = inv.model_dump(mode="json")
        payload["inventory_id"] = "f" * 64
        out.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_inventory(out)
        with pytest.raises(ValueError, match="does not match"):
            verify_inventory_self_hash(loaded)

    def test_atomic_write_creates_no_partial(self, tmp_path: Path) -> None:
        inv = seal_inventory(_make_inventory())
        out = tmp_path / "inventory.json"
        payload = inv.model_dump(mode="json")
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        out.write_text(text, encoding="utf-8")
        assert out.exists()
        tmp_file = out.with_suffix(".tmp")
        assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


class TestPathSafety:
    def test_rejects_symlink_in_bound_slot_path(self, tmp_path: Path) -> None:
        from app.research.layer_two_alpha_diagnostic_input_inventory import _validate_repo_dir

        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)
        with pytest.raises(ValueError, match="symlink"):
            _validate_repo_dir("link", repo_root=tmp_path, field_name="test")

    def test_rejects_path_escape(self, tmp_path: Path) -> None:
        from app.research.layer_two_alpha_diagnostic_input_inventory import _validate_repo_dir

        with pytest.raises(ValueError, match="path escape"):
            _validate_repo_dir("../escape", repo_root=tmp_path, field_name="test")

    def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        from app.research.layer_two_alpha_diagnostic_input_inventory import _validate_repo_dir

        with pytest.raises(ValueError, match="repo-relative"):
            _validate_repo_dir("/absolute/path", repo_root=tmp_path, field_name="test")

    def test_rejects_oos_namespace(self) -> None:
        from app.research.layer_two_alpha_diagnostic_input_inventory import _reject_oos_namespace

        with pytest.raises(ValueError, match="2025/OOS"):
            _reject_oos_namespace("data/2025/market")

        with pytest.raises(ValueError, match="2025/OOS"):
            _reject_oos_namespace("data/oos/something")


# ---------------------------------------------------------------------------
# Mutable readiness rejection / model_copy bypass
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_frozen_inventory_rejects_mutation(self) -> None:
        inv = seal_inventory(_make_inventory())
        with pytest.raises(ValidationError):
            inv.inventory_id = "0" * 64  # type: ignore[misc]

    def test_frozen_slot_rejects_mutation(self) -> None:
        slot = _make_bound_slot("sealed_market_snapshot")
        with pytest.raises(ValidationError):
            slot.snapshot_id = "0" * 64  # type: ignore[misc]

    def test_frozen_readiness_rejects_mutation(self) -> None:
        inv = _make_inventory()
        with pytest.raises(ValidationError):
            inv.readiness.ready_for_data = True  # type: ignore[misc,assignment]

    def test_model_copy_bypass_detected_by_verify(self) -> None:
        """model_copy can bypass validators, but verify catches the mismatch."""
        sealed = seal_inventory(_make_inventory())
        mutated = sealed.model_copy(update={"contract_path": BOUND_CONTRACT_PATH})
        verify_inventory_self_hash(mutated)


# ---------------------------------------------------------------------------
# Coverage drift detection
# ---------------------------------------------------------------------------


class TestCoverageDrift:
    def test_bound_slot_records_coverage(self) -> None:
        slot = _make_bound_slot("sealed_market_snapshot")
        assert slot.coverage_start == date(2021, 10, 8)
        assert slot.coverage_end == date(2024, 12, 31)

    def test_required_coverage_constants(self) -> None:
        assert REQUIRED_COVERAGE_START == date(2022, 1, 1)
        assert REQUIRED_COVERAGE_END == date(2024, 12, 31)


# ---------------------------------------------------------------------------
# Extra field rejection
# ---------------------------------------------------------------------------


class TestExtraFieldRejection:
    def test_inventory_rejects_extra_field(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            LayerTwoAlphaDiagnosticInputInventoryV1(
                schema_version="1",
                inventory_version=INPUT_INVENTORY_VERSION,
                contract_id=BOUND_CONTRACT_ID,
                contract_path=BOUND_CONTRACT_PATH,
                contract_file_sha256=BOUND_CONTRACT_FILE_SHA256,
                slots=(
                    _make_bound_slot("sealed_market_snapshot"),
                    _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
                    _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
                    _make_bound_slot("pit_fundamental_overlay"),
                    _make_bound_slot("pit_daily_valuation"),
                    _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
                ),
                readiness=InventoryReadinessFlags(
                    research_only=True,
                    read_only=True,
                    ready_for_data=False,
                    ready_for_scoring=False,
                    ready_for_backtest=False,
                    ready_for_portfolio_construction=False,
                    ready_for_orders=False,
                    ready_for_trading=False,
                    auto_apply=False,
                ),
                inventory_id=None,
                sneaky_extra="attack",  # type: ignore[call-arg]
            )

    def test_bound_slot_rejects_extra_field(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            BoundSlot(
                kind="sealed_market_snapshot",
                state="bound",
                repo_relative_path="data/test",
                snapshot_id="a" * 64,
                file_sha256="b" * 64,
                table_hashes={"daily_bars": "c" * 64},
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 12, 31),
                note="test",
                attack_field="bad",  # type: ignore[call-arg]
            )

    def test_blocked_slot_rejects_extra_field(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            BlockedSlot(
                kind="candidate_eligibility_reports",
                state="blocked_missing",
                issue="test",
                note="test",
                attack="bad",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# Missing files
# ---------------------------------------------------------------------------


class TestMissingFiles:
    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="missing or unreadable"):
            load_inventory(tmp_path / "nonexistent.json")

    def test_load_invalid_json_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not json at all", encoding="utf-8")
        with pytest.raises(ValueError, match="missing or unreadable"):
            load_inventory(bad)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_schema_version(self) -> None:
        assert INPUT_INVENTORY_SCHEMA_VERSION == "1"

    def test_inventory_version(self) -> None:
        assert INPUT_INVENTORY_VERSION == "layer-two-alpha-diagnostic-input-inventory-v1"

    def test_bound_contract_id(self) -> None:
        assert BOUND_CONTRACT_ID == "f892b76c2140009e3b6dcad6599def52aaa1f0b62acc91bed747136d92e09df0"

    def test_slot_kinds_match_run_contract(self) -> None:
        assert REQUIRED_INPUT_SLOT_KINDS == (
            "sealed_market_snapshot",
            "candidate_eligibility_reports",
            "financial_negative_list_reports",
            "pit_fundamental_overlay",
            "pit_daily_valuation",
            "statistical_cluster_companion_reports",
        )

    def test_derived_and_bound_kinds_partition(self) -> None:
        all_kinds = set(REQUIRED_INPUT_SLOT_KINDS)
        assert DERIVED_SLOT_KINDS | BOUND_SLOT_KINDS == all_kinds
        assert DERIVED_SLOT_KINDS & BOUND_SLOT_KINDS == set()


# ---------------------------------------------------------------------------
# Per-kind semantic constraints (P1/P2 regression)
# ---------------------------------------------------------------------------


class TestPerKindSemantics:
    def test_market_rejects_non_null_base_market_snapshot_id(self) -> None:
        """Attack 2 regression: market must have base_market_snapshot_id=None."""
        market_with_base = BoundSlot(
            kind="sealed_market_snapshot",
            state="bound",
            repo_relative_path="data/test",
            snapshot_id="a" * 64,
            file_sha256="b" * 64,
            table_hashes={"daily_bars": "c" * 64},
            base_market_snapshot_id="d" * 64,
            coverage_start=date(2021, 10, 8),
            coverage_end=date(2024, 12, 31),
            note="test",
        )
        slots = (
            market_with_base,
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            _make_bound_slot("pit_fundamental_overlay"),
            _make_bound_slot("pit_daily_valuation"),
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
        with pytest.raises(ValidationError, match="sealed_market_snapshot must have base_market_snapshot_id=None"):
            _make_inventory(slots=slots)

    def test_fundamental_requires_non_null_base_market(self) -> None:
        fund_no_base = BoundSlot(
            kind="pit_fundamental_overlay",
            state="bound",
            repo_relative_path="data/test",
            snapshot_id="a" * 64,
            file_sha256="b" * 64,
            table_hashes={"fundamental_reports": "c" * 64},
            base_market_snapshot_id=None,
            coverage_start=date(2021, 10, 8),
            coverage_end=date(2024, 12, 31),
            note="test",
        )
        slots = (
            _make_bound_slot("sealed_market_snapshot"),
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            fund_no_base,
            _make_bound_slot("pit_daily_valuation"),
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
        with pytest.raises(ValidationError, match="non-null base_market_snapshot_id"):
            _make_inventory(slots=slots)

    def test_valuation_requires_exact_one_daily_valuation_key(self) -> None:
        val_extra = BoundSlot(
            kind="pit_daily_valuation",
            state="bound",
            repo_relative_path="data/test/daily_valuation.parquet",
            snapshot_id="a" * 64,
            file_sha256="b" * 64,
            table_hashes={"daily_valuation": "c" * 64, "extra": "d" * 64},
            base_market_snapshot_id="a" * 64,
            coverage_start=date(2021, 10, 8),
            coverage_end=date(2024, 12, 31),
            note="test",
        )
        slots = (
            _make_bound_slot("sealed_market_snapshot"),
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            _make_bound_slot("pit_fundamental_overlay"),
            val_extra,
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
        with pytest.raises(ValidationError, match="exactly one entry"):
            _make_inventory(slots=slots)

    def test_valuation_requires_daily_valuation_key(self) -> None:
        val_wrong = BoundSlot(
            kind="pit_daily_valuation",
            state="bound",
            repo_relative_path="data/test/daily_valuation.parquet",
            snapshot_id="a" * 64,
            file_sha256="b" * 64,
            table_hashes={"wrong_key": "c" * 64},
            base_market_snapshot_id="a" * 64,
            coverage_start=date(2021, 10, 8),
            coverage_end=date(2024, 12, 31),
            note="test",
        )
        slots = (
            _make_bound_slot("sealed_market_snapshot"),
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            _make_bound_slot("pit_fundamental_overlay"),
            val_wrong,
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
        with pytest.raises(ValidationError, match="must contain 'daily_valuation'"):
            _make_inventory(slots=slots)


class TestCrossSlotBinding:
    def test_fund_base_must_match_market_snapshot_id(self) -> None:
        slots = (
            _make_bound_slot("sealed_market_snapshot", snapshot_id="a" * 64),
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            BoundSlot(
                kind="pit_fundamental_overlay",
                state="bound",
                repo_relative_path="data/test",
                snapshot_id="a" * 64,
                file_sha256="b" * 64,
                table_hashes={"fundamental_reports": "c" * 64},
                base_market_snapshot_id="e" * 64,
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 12, 31),
                note="test",
            ),
            BoundSlot(
                kind="pit_daily_valuation",
                state="bound",
                repo_relative_path="data/test/daily_valuation.parquet",
                snapshot_id="a" * 64,
                file_sha256="b" * 64,
                table_hashes={"daily_valuation": "c" * 64},
                base_market_snapshot_id="e" * 64,
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 12, 31),
                note="test",
            ),
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
        with pytest.raises(ValidationError, match="must equal sealed_market_snapshot snapshot_id"):
            _make_inventory(slots=slots)

    def test_fund_and_valuation_must_share_snapshot_id(self) -> None:
        slots = (
            _make_bound_slot("sealed_market_snapshot", snapshot_id="a" * 64),
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            BoundSlot(
                kind="pit_fundamental_overlay",
                state="bound",
                repo_relative_path="data/test",
                snapshot_id="a" * 64,
                file_sha256="b" * 64,
                table_hashes={"fundamental_reports": "c" * 64},
                base_market_snapshot_id="a" * 64,
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 12, 31),
                note="test",
            ),
            BoundSlot(
                kind="pit_daily_valuation",
                state="bound",
                repo_relative_path="data/test/daily_valuation.parquet",
                snapshot_id="e" * 64,
                file_sha256="b" * 64,
                table_hashes={"daily_valuation": "c" * 64},
                base_market_snapshot_id="a" * 64,
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 12, 31),
                note="test",
            ),
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
        with pytest.raises(ValidationError, match="must share the same snapshot_id"):
            _make_inventory(slots=slots)

    def test_fund_and_valuation_must_share_coverage(self) -> None:
        slots = (
            _make_bound_slot("sealed_market_snapshot", snapshot_id="a" * 64),
            _make_blocked_slot("candidate_eligibility_reports", BLOCKED_ISSUE_CANDIDATE_ELIGIBILITY),
            _make_blocked_slot("financial_negative_list_reports", BLOCKED_ISSUE_FINANCIAL_NEGATIVE_LIST),
            BoundSlot(
                kind="pit_fundamental_overlay",
                state="bound",
                repo_relative_path="data/test",
                snapshot_id="a" * 64,
                file_sha256="b" * 64,
                table_hashes={"fundamental_reports": "c" * 64},
                base_market_snapshot_id="a" * 64,
                coverage_start=date(2021, 10, 8),
                coverage_end=date(2024, 12, 31),
                note="test",
            ),
            BoundSlot(
                kind="pit_daily_valuation",
                state="bound",
                repo_relative_path="data/test/daily_valuation.parquet",
                snapshot_id="a" * 64,
                file_sha256="b" * 64,
                table_hashes={"daily_valuation": "c" * 64},
                base_market_snapshot_id="a" * 64,
                coverage_start=date(2021, 9, 1),
                coverage_end=date(2024, 12, 31),
                note="test",
            ),
            _make_blocked_slot("statistical_cluster_companion_reports", BLOCKED_ISSUE_STATISTICAL_CLUSTERS),
        )
        with pytest.raises(ValidationError, match="must share the same coverage_start"):
            _make_inventory(slots=slots)


class TestValuationFilePath:
    def test_valuation_slot_path_is_file_not_dir(self) -> None:
        inv = _make_inventory()
        val_slot = inv.slots[4]
        assert isinstance(val_slot, BoundSlot)
        assert val_slot.repo_relative_path.endswith(".parquet")


class TestParentSymlink:
    def test_rejects_parent_symlink(self, tmp_path: Path) -> None:
        from app.research.layer_two_alpha_diagnostic_input_inventory import _validate_repo_path

        real_parent = tmp_path / "real_parent"
        real_parent.mkdir()
        (real_parent / "child").mkdir()
        link_parent = tmp_path / "link_parent"
        link_parent.symlink_to(real_parent)
        with pytest.raises(ValueError, match="symlink component"):
            _validate_repo_path("link_parent/child", repo_root=tmp_path, field_name="test")


class TestUnderscoreOos:
    def test_rejects_underscore_oos(self) -> None:
        from app.research.layer_two_alpha_diagnostic_input_inventory import _reject_oos_namespace

        with pytest.raises(ValueError, match="2025/OOS"):
            _reject_oos_namespace("data/market_oos_v1/parquet")

    def test_rejects_oos_at_start(self) -> None:
        from app.research.layer_two_alpha_diagnostic_input_inventory import _reject_oos_namespace

        with pytest.raises(ValueError, match="2025/OOS"):
            _reject_oos_namespace("oos/data/parquet")

    def test_allows_unrelated_substring(self) -> None:
        from app.research.layer_two_alpha_diagnostic_input_inventory import _reject_oos_namespace

        _reject_oos_namespace("data/goose/parquet")
        _reject_oos_namespace("data/moose-lake/parquet")
