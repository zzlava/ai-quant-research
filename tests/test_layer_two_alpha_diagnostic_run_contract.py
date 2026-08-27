"""Tests for E11b-0b layer-two alpha diagnostic run contract."""

from __future__ import annotations

import ast
import hashlib
import json
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.research.layer_two_alpha_diagnostic_run_contract import (
    BOUND_E11A_PROTOCOL_PATH,
    BOUND_ENGINE_PATH,
    BOUND_ENGINE_VERSION,
    BOUND_LEDGER_PATH,
    CONSUMED_OOS_END,
    CONSUMED_OOS_START,
    DEVELOPMENT_END,
    DEVELOPMENT_START,
    FROZEN_HYPOTHESIS_FAMILY_IDS,
    FROZEN_LABEL_HORIZONS,
    HOLM_FAMILY_WISE_ALPHA,
    HOLM_HYPOTHESIS_COUNT,
    NEW_FROZEN_OOS_BEGINS,
    PRIMARY_HAC_LAG,
    PRIMARY_HORIZON,
    REQUIRED_INPUT_SLOT_KINDS,
    SEEN_ROBUSTNESS_END,
    SEEN_ROBUSTNESS_START,
    LayerTwoAlphaDiagnosticRunContractV1,
    assert_binding_constants,
    assert_contract_self_hash,
    assert_windows_valid,
    build_layer_two_alpha_diagnostic_run_contract,
    canonical_contract_payload,
    load_contract,
    seal_contract,
    verify_contract_file,
    verify_contract_structural,
    write_contract,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "src" / "app" / "research" / "layer_two_alpha_diagnostic_run_contract.py"
CONTRACT_JSON_PATH = REPO_ROOT / "config" / "research" / "layer-two-alpha-diagnostic-run-contract-v1.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_sealed() -> LayerTwoAlphaDiagnosticRunContractV1:
    return seal_contract(build_layer_two_alpha_diagnostic_run_contract())


def _contract_json() -> dict[str, Any]:
    return json.loads(CONTRACT_JSON_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Deterministic self-hash
# ---------------------------------------------------------------------------


class TestSelfHash:
    def test_deterministic_contract_id(self) -> None:
        c1 = _build_sealed()
        c2 = _build_sealed()
        assert c1.contract_id is not None
        assert c1.contract_id == c2.contract_id
        assert len(c1.contract_id) == 64

    def test_contract_id_matches_canonical_recomputation(self) -> None:
        sealed = _build_sealed()
        payload = canonical_contract_payload(sealed)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hashlib.sha256(raw).hexdigest()
        assert sealed.contract_id == expected

    def test_contract_id_excludes_itself(self) -> None:
        sealed = _build_sealed()
        payload = canonical_contract_payload(sealed)
        assert "contract_id" not in payload

    def test_tampered_id_fails_self_hash(self) -> None:
        sealed = _build_sealed()
        tampered = sealed.model_copy(update={"contract_id": "a" * 64})
        with pytest.raises(ValueError, match="contract_id"):
            assert_contract_self_hash(tampered)

    def test_missing_contract_id_fails(self) -> None:
        contract = build_layer_two_alpha_diagnostic_run_contract()
        assert contract.contract_id is None
        with pytest.raises(ValueError, match="missing"):
            assert_contract_self_hash(contract)


# ---------------------------------------------------------------------------
# Real four-file binding success
# ---------------------------------------------------------------------------


class TestRealFileBinding:
    def test_real_file_verification_success(self) -> None:
        contract, result = verify_contract_file(
            contract_path=CONTRACT_JSON_PATH,
            repo_root=REPO_ROOT,
        )
        assert result.structural_ok is True
        assert result.e11a_protocol_binding_ok is True
        assert result.engine_binding_ok is True
        assert result.ledger_binding_ok is True
        assert result.all_input_slots_unbound is True
        assert result.all_readiness_false is True
        assert result.hypothesis_count_ok is True
        assert contract.contract_id is not None


# ---------------------------------------------------------------------------
# Structural verifier binding flags always False
# ---------------------------------------------------------------------------


class TestStructuralVerifier:
    def test_structural_binding_flags_false(self) -> None:
        sealed = _build_sealed()
        result = verify_contract_structural(sealed)
        assert result.structural_ok is True
        assert result.e11a_protocol_binding_ok is False
        assert result.engine_binding_ok is False
        assert result.ledger_binding_ok is False

    def test_structural_verifier_calls_binding_constants(self) -> None:
        sealed = _build_sealed()
        assert_binding_constants(sealed)


# ---------------------------------------------------------------------------
# Adversarial drift: every ACCEPTED vulnerability now rejected
# ---------------------------------------------------------------------------


class TestAdversarialDrift:
    """Covers every drift that Codex adversarial testing confirmed as ACCEPTED."""

    def test_required_int_1_rejected(self) -> None:
        data = _contract_json()
        data["future_input_slots"][0]["required"] = 1
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="bool"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_is_holm_family_member_int_1_rejected(self) -> None:
        data = _contract_json()
        data["holm_family"]["hypotheses"][0]["is_holm_family_member"] = 1
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="bool"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_is_gate_only_int_0_rejected(self) -> None:
        data = _contract_json()
        data["holm_family"]["hypotheses"][0]["is_gate_only"] = 0
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="bool"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_development_selectable_false_rejected(self) -> None:
        data = _contract_json()
        data["windows"]["development"]["selectable"] = False
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="selectable"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_development_forbidden_true_rejected(self) -> None:
        data = _contract_json()
        data["windows"]["development"]["forbidden"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="forbidden"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_seen_robustness_report_only_false_rejected(self) -> None:
        data = _contract_json()
        data["windows"]["seen_robustness"]["report_only"] = False
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="report_only"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_consumed_oos_selectable_true_rejected(self) -> None:
        data = _contract_json()
        data["windows"]["consumed_oos"]["selectable"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="selectable"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_label_horizons_single_value_rejected(self) -> None:
        data = _contract_json()
        data["windows"]["development"]["label_horizons"] = [1]
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="label_horizons"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_label_horizons_wrong_triple_rejected(self) -> None:
        data = _contract_json()
        data["windows"]["development"]["label_horizons"] = [5, 20, 60]
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="label_horizons"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_label_horizons_bool_items_rejected(self) -> None:
        data = _contract_json()
        data["windows"]["development"]["label_horizons"] = [True, 20, 40]
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="non-bool int"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_e11a_protocol_id_zeroes_rejected_at_model(self) -> None:
        data = _contract_json()
        data["e11a_protocol_id"] = "0" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_engine_file_sha256_zeroes_rejected_at_model(self) -> None:
        data = _contract_json()
        data["engine_file_sha256"] = "0" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_e11a_file_sha256_zeroes_rejected_at_model(self) -> None:
        data = _contract_json()
        data["e11a_file_sha256"] = "0" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_ledger_id_zeroes_rejected_at_model(self) -> None:
        data = _contract_json()
        data["base_ledger_id"] = "0" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_ledger_sha_zeroes_rejected_at_model(self) -> None:
        data = _contract_json()
        data["base_ledger_file_sha256"] = "0" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_reseal_after_e11a_id_drift_still_fails(self) -> None:
        """Even recomputing contract_id cannot rescue a drifted binding."""
        data = _contract_json()
        data["e11a_protocol_id"] = "0" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_reseal_after_engine_sha_drift_still_fails(self) -> None:
        data = _contract_json()
        data["engine_file_sha256"] = "0" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_reseal_after_ledger_id_drift_still_fails(self) -> None:
        data = _contract_json()
        data["base_ledger_id"] = "0" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_reseal_after_ledger_sha_drift_still_fails(self) -> None:
        data = _contract_json()
        data["base_ledger_file_sha256"] = "0" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_reseal_after_e11a_file_sha_drift_still_fails(self) -> None:
        data = _contract_json()
        data["e11a_file_sha256"] = "0" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)


# ---------------------------------------------------------------------------
# Hypothesis ID exact format enforcement
# ---------------------------------------------------------------------------


class TestHypothesisIdFormat:
    def test_correct_hypothesis_id_accepted(self) -> None:
        sealed = _build_sealed()
        for h in sealed.holm_family.hypotheses:
            assert h.hypothesis_id == f"h40-ic-{h.factor_family_id}"

    def test_wrong_hypothesis_id_format_rejected(self) -> None:
        data = _contract_json()
        data["holm_family"]["hypotheses"][0]["hypothesis_id"] = "wrong-format"
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="hypothesis_id must be exactly"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_hypothesis_id_without_prefix_rejected(self) -> None:
        data = _contract_json()
        data["holm_family"]["hypotheses"][0]["hypothesis_id"] = "quality"
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="hypothesis_id must be exactly"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_spread_hypothesis_id_rejected(self) -> None:
        """spread/year/cluster factor cannot enter hypotheses."""
        data = _contract_json()
        data["holm_family"]["hypotheses"][0]["hypothesis_id"] = "h40-ic-spread"
        data["holm_family"]["hypotheses"][0]["factor_family_id"] = "spread"
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_yearly_direction_in_hypotheses_rejected(self) -> None:
        data = _contract_json()
        data["holm_family"]["hypotheses"][1]["hypothesis_id"] = "h40-ic-yearly_direction"
        data["holm_family"]["hypotheses"][1]["factor_family_id"] = "yearly_direction"
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_cluster_in_hypotheses_rejected(self) -> None:
        data = _contract_json()
        data["holm_family"]["hypotheses"][3]["hypothesis_id"] = "h40-ic-cluster_companion"
        data["holm_family"]["hypotheses"][3]["factor_family_id"] = "cluster_companion"
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="factor_family_ids"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)


# ---------------------------------------------------------------------------
# Endpoint rules (E11a mechanized)
# ---------------------------------------------------------------------------


class TestEndpointRules:
    def test_endpoint_rules_present_in_sealed(self) -> None:
        sealed = _build_sealed()
        assert sealed.windows.label_endpoint_must_remain_within_same_window is True
        assert sealed.windows.horizon_never_shifts_or_shortens is True
        assert sealed.windows.missing_or_unverified_endpoint_is_unknown is True

    def test_endpoint_rule_false_rejected(self) -> None:
        data = _contract_json()
        data["windows"]["label_endpoint_must_remain_within_same_window"] = False
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="true"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_endpoint_rule_int_1_rejected(self) -> None:
        data = _contract_json()
        data["windows"]["horizon_never_shifts_or_shortens"] = 1
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="true"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_endpoint_rule_missing_rejected(self) -> None:
        data = _contract_json()
        del data["windows"]["missing_or_unverified_endpoint_is_unknown"]
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_all_three_endpoint_rules_in_assert_windows_valid(self) -> None:
        sealed = _build_sealed()
        assert_windows_valid(sealed)

    def test_endpoint_rules_present_in_config(self) -> None:
        data = _contract_json()
        assert data["windows"]["label_endpoint_must_remain_within_same_window"] is True
        assert data["windows"]["horizon_never_shifts_or_shortens"] is True
        assert data["windows"]["missing_or_unverified_endpoint_is_unknown"] is True


# ---------------------------------------------------------------------------
# Holm gate bool strict enforcement
# ---------------------------------------------------------------------------


class TestHolmGateBoolStrict:
    def test_gate_bool_int_1_rejected(self) -> None:
        data = _contract_json()
        data["holm_family"]["spread_positivity_is_gate_not_holm_member"] = 1
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="true"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_hypothesis_count_bool_rejected(self) -> None:
        data = _contract_json()
        data["holm_family"]["hypothesis_count"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="bool rejected"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_new_frozen_oos_int_1_rejected(self) -> None:
        data = _contract_json()
        data["windows"]["new_frozen_oos_cannot_be_evaluated"] = 1
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="true"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)


# ---------------------------------------------------------------------------
# Contract ID hex validation
# ---------------------------------------------------------------------------


class TestContractIdHex:
    def test_contract_id_uppercase_hex_rejected(self) -> None:
        data = _contract_json()
        data["contract_id"] = "A" * 64
        with pytest.raises(ValidationError, match="lowercase"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_contract_id_non_hex_rejected(self) -> None:
        data = _contract_json()
        data["contract_id"] = "g" * 64
        with pytest.raises(ValidationError, match="lowercase"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)


# ---------------------------------------------------------------------------
# Tampered bindings (model-level rejection, not just file SHA mismatch)
# ---------------------------------------------------------------------------


class TestTamperedBindings:
    def test_wrong_e11a_protocol_id_fails_at_model(self) -> None:
        data = _contract_json()
        data["e11a_protocol_id"] = "b" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_wrong_e11a_file_sha256_fails_at_model(self) -> None:
        data = _contract_json()
        data["e11a_file_sha256"] = "c" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_wrong_engine_file_sha256_fails_at_model(self) -> None:
        data = _contract_json()
        data["engine_file_sha256"] = "d" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_wrong_ledger_id_fails_at_model(self) -> None:
        data = _contract_json()
        data["base_ledger_id"] = "f" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_wrong_ledger_file_sha256_fails_at_model(self) -> None:
        data = _contract_json()
        data["base_ledger_file_sha256"] = "a" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="BOUND"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_wrong_e11a_path(self) -> None:
        data = _contract_json()
        data["e11a_protocol_path"] = "wrong/path.json"
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)


# ---------------------------------------------------------------------------
# Hypothesis mutations
# ---------------------------------------------------------------------------


class TestHypothesisMutations:
    def test_modify_hypothesis_removes_one(self) -> None:
        data = _contract_json()
        data["holm_family"]["hypotheses"] = data["holm_family"]["hypotheses"][:3]
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_add_fifth_hypothesis(self) -> None:
        data = _contract_json()
        extra = dict(data["holm_family"]["hypotheses"][0])
        extra["hypothesis_id"] = "h40-ic-extra"
        extra["factor_family_id"] = "extra_factor"
        data["holm_family"]["hypotheses"].append(extra)
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_duplicate_hypothesis_id(self) -> None:
        data = _contract_json()
        data["holm_family"]["hypotheses"][1]["hypothesis_id"] = data["holm_family"]["hypotheses"][0]["hypothesis_id"]
        data["holm_family"]["hypotheses"][1]["factor_family_id"] = data["holm_family"]["hypotheses"][0][
            "factor_family_id"
        ]
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_wrong_factor_family_order(self) -> None:
        data = _contract_json()
        h = data["holm_family"]["hypotheses"]
        h[0], h[1] = h[1], h[0]
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="factor_family_ids"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_spread_disguised_as_hypothesis(self) -> None:
        data = _contract_json()
        extra = dict(data["holm_family"]["hypotheses"][0])
        extra["hypothesis_id"] = "h40-spread-positivity"
        extra["factor_family_id"] = "spread_companion"
        data["holm_family"]["hypotheses"].append(extra)
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_cluster_disguised_as_hypothesis(self) -> None:
        data = _contract_json()
        h = data["holm_family"]["hypotheses"]
        h[3]["factor_family_id"] = "cluster_companion"
        h[3]["hypothesis_id"] = "h40-ic-cluster_companion"
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="factor_family_ids"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)


# ---------------------------------------------------------------------------
# Window boundary / role drift
# ---------------------------------------------------------------------------


class TestWindowDrift:
    def test_development_start_drift(self) -> None:
        sealed = _build_sealed()
        assert sealed.windows.development.window.start == date(2022, 1, 1)
        assert sealed.windows.development.window.end == date(2023, 12, 31)
        assert sealed.windows.seen_robustness.window.start == date(2024, 1, 1)
        assert sealed.windows.seen_robustness.window.end == date(2024, 12, 31)
        assert sealed.windows.consumed_oos.window.start == date(2025, 1, 1)
        assert sealed.windows.consumed_oos.window.end == date(2026, 8, 21)
        assert sealed.windows.new_frozen_oos_begins == date(2026, 8, 22)

    def test_development_role_changed(self) -> None:
        data = _contract_json()
        data["windows"]["development"]["role"] = "seen_robustness"
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="selectable|role"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_consumed_oos_not_forbidden(self) -> None:
        data = _contract_json()
        data["windows"]["consumed_oos"]["forbidden"] = False
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="forbidden"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_seen_robustness_selectable(self) -> None:
        data = _contract_json()
        data["windows"]["seen_robustness"]["selectable"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="selectable"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_windows_overlap(self) -> None:
        data = _contract_json()
        data["windows"]["development"]["window"]["end"] = "2024-06-01"
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="before"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_endpoint_crosses_window(self) -> None:
        sealed = _build_sealed()
        assert sealed.windows.consumed_oos.forbidden is True
        assert sealed.windows.consumed_oos.window.start == date(2025, 1, 1)
        assert sealed.windows.development.window.end == date(2023, 12, 31)
        assert sealed.windows.seen_robustness.window.end == date(2024, 12, 31)

    def test_development_report_only_true_rejected(self) -> None:
        data = _contract_json()
        data["windows"]["development"]["report_only"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="report_only"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_consumed_oos_report_only_true_rejected(self) -> None:
        data = _contract_json()
        data["windows"]["consumed_oos"]["report_only"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="report_only"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)


# ---------------------------------------------------------------------------
# Unbound input slots: reject path/hash/id injection
# ---------------------------------------------------------------------------


class TestUnboundSlots:
    def test_slot_with_path_fails(self) -> None:
        data = _contract_json()
        data["future_input_slots"][0]["repo_relative_path"] = "some/path.parquet"
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="null"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_slot_with_sha256_fails(self) -> None:
        data = _contract_json()
        data["future_input_slots"][1]["sha256"] = "a" * 64
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="null"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_slot_with_snapshot_id_fails(self) -> None:
        data = _contract_json()
        data["future_input_slots"][2]["snapshot_id"] = "snap-001"
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="null"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_slot_state_not_unbound_fails(self) -> None:
        data = _contract_json()
        data["future_input_slots"][0]["state"] = "bound"
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_all_six_required_slots_present(self) -> None:
        sealed = _build_sealed()
        kinds = tuple(s.kind for s in sealed.future_input_slots)
        assert kinds == REQUIRED_INPUT_SLOT_KINDS
        assert len(kinds) == 6

    def test_wrong_slot_order(self) -> None:
        data = _contract_json()
        slots = data["future_input_slots"]
        slots[0], slots[1] = slots[1], slots[0]
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="kinds"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)


# ---------------------------------------------------------------------------
# Readiness flag tests
# ---------------------------------------------------------------------------


class TestReadinessFlags:
    def test_ready_for_data_true_fails(self) -> None:
        data = _contract_json()
        data["readiness"]["ready_for_data"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_ready_for_scoring_true_fails(self) -> None:
        data = _contract_json()
        data["readiness"]["ready_for_scoring"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_ready_for_backtest_true_fails(self) -> None:
        data = _contract_json()
        data["readiness"]["ready_for_backtest"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_ready_for_trading_true_fails(self) -> None:
        data = _contract_json()
        data["readiness"]["ready_for_trading"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_auto_apply_true_fails(self) -> None:
        data = _contract_json()
        data["readiness"]["auto_apply"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_ready_for_portfolio_construction_true_fails(self) -> None:
        data = _contract_json()
        data["readiness"]["ready_for_portfolio_construction"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_ready_for_orders_true_fails(self) -> None:
        data = _contract_json()
        data["readiness"]["ready_for_orders"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)


# ---------------------------------------------------------------------------
# Bool / NaN / empty-string / 0 disguise
# ---------------------------------------------------------------------------


class TestTypeDisguise:
    def test_bool_as_number_rejected(self) -> None:
        data = _contract_json()
        data["holm_family"]["family_wise_alpha"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="number"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_int_0_as_bool_rejected_in_readiness(self) -> None:
        data = _contract_json()
        data["readiness"]["ready_for_data"] = 0
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="bool"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_int_1_as_bool_rejected_in_readiness(self) -> None:
        data = _contract_json()
        data["readiness"]["research_only"] = 1
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="bool"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_empty_string_hypothesis_id_rejected(self) -> None:
        data = _contract_json()
        data["holm_family"]["hypotheses"][0]["hypothesis_id"] = ""
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_blank_string_hypothesis_id_rejected(self) -> None:
        data = _contract_json()
        data["holm_family"]["hypotheses"][0]["hypothesis_id"] = "   "
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_empty_string_slot_kind_rejected(self) -> None:
        data = _contract_json()
        data["future_input_slots"][0]["kind"] = ""
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_int_0_as_bool_in_evidence_window(self) -> None:
        data = _contract_json()
        data["windows"]["development"]["selectable"] = 1
        data["contract_id"] = None
        with pytest.raises(ValidationError, match="strict bool"):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)


# ---------------------------------------------------------------------------
# Path escape / directory / symlink
# ---------------------------------------------------------------------------


class TestPathEscape:
    def test_dotdot_path_escape(self) -> None:
        from app.research.layer_two_alpha_diagnostic_run_contract import (
            _validate_repo_relative_path,
        )

        with pytest.raises(ValueError, match="\\.\\."):
            _validate_repo_relative_path("../etc/passwd", repo_root=REPO_ROOT, field_name="test")

    def test_absolute_path_rejected(self) -> None:
        from app.research.layer_two_alpha_diagnostic_run_contract import (
            _validate_repo_relative_path,
        )

        with pytest.raises(ValueError, match="repo-relative"):
            _validate_repo_relative_path("/etc/passwd", repo_root=REPO_ROOT, field_name="test")

    def test_directory_rejected(self) -> None:
        from app.research.layer_two_alpha_diagnostic_run_contract import (
            _validate_repo_relative_path,
        )

        with pytest.raises(ValueError, match="not a regular file"):
            _validate_repo_relative_path("src", repo_root=REPO_ROOT, field_name="test")

    def test_symlink_rejected(self) -> None:
        from app.research.layer_two_alpha_diagnostic_run_contract import (
            _validate_repo_relative_path,
        )

        link_path = REPO_ROOT / "_test_symlink_tmp"
        try:
            link_path.symlink_to(REPO_ROOT / "config" / "research" / "research-trial-ledger-v1.json")
            with pytest.raises(ValueError, match="symlink"):
                _validate_repo_relative_path("_test_symlink_tmp", repo_root=REPO_ROOT, field_name="test")
        finally:
            if link_path.exists() or link_path.is_symlink():
                link_path.unlink()

    def test_nonexistent_path_rejected(self) -> None:
        from app.research.layer_two_alpha_diagnostic_run_contract import (
            _validate_repo_relative_path,
        )

        with pytest.raises(ValueError, match="does not exist"):
            _validate_repo_relative_path("nonexistent_file_xyz.json", repo_root=REPO_ROOT, field_name="test")


# ---------------------------------------------------------------------------
# Immutability / frozen post-construction
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_contract_frozen(self) -> None:
        sealed = _build_sealed()
        with pytest.raises(ValidationError):
            sealed.contract_id = "x" * 64  # type: ignore[misc]

    def test_hypothesis_frozen(self) -> None:
        sealed = _build_sealed()
        h = sealed.holm_family.hypotheses[0]
        with pytest.raises(ValidationError):
            h.direction = "negative"  # type: ignore[misc,assignment]

    def test_readiness_frozen(self) -> None:
        sealed = _build_sealed()
        with pytest.raises(ValidationError):
            sealed.readiness.ready_for_scoring = True  # type: ignore[misc,assignment]

    def test_input_slot_frozen(self) -> None:
        sealed = _build_sealed()
        slot = sealed.future_input_slots[0]
        with pytest.raises(ValidationError):
            slot.state = "bound"  # type: ignore[misc,assignment]

    def test_window_frozen(self) -> None:
        sealed = _build_sealed()
        with pytest.raises(ValidationError):
            sealed.windows.development.window.start = date(2021, 1, 1)  # type: ignore[misc]

    def test_hypotheses_tuple_immutable(self) -> None:
        sealed = _build_sealed()
        assert isinstance(sealed.holm_family.hypotheses, tuple)
        with pytest.raises((TypeError, AttributeError)):
            sealed.holm_family.hypotheses.append(sealed.holm_family.hypotheses[0])  # type: ignore[attr-defined]

    def test_input_slots_tuple_immutable(self) -> None:
        sealed = _build_sealed()
        assert isinstance(sealed.future_input_slots, tuple)
        with pytest.raises((TypeError, AttributeError)):
            sealed.future_input_slots.append(sealed.future_input_slots[0])  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Extra fields (extra=forbid)
# ---------------------------------------------------------------------------


class TestExtraForbid:
    def test_extra_field_at_top_level(self) -> None:
        data = _contract_json()
        data["extra_field"] = "surprise"
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_extra_field_in_readiness(self) -> None:
        data = _contract_json()
        data["readiness"]["extra"] = True
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)

    def test_extra_field_in_hypothesis(self) -> None:
        data = _contract_json()
        data["holm_family"]["hypotheses"][0]["extra"] = "bad"
        data["contract_id"] = None
        with pytest.raises(ValidationError):
            LayerTwoAlphaDiagnosticRunContractV1.model_validate(data)


# ---------------------------------------------------------------------------
# AST: no scoring/backtest/strategies/pipeline/broker imports
# ---------------------------------------------------------------------------


class TestASTForbiddenImports:
    def test_module_no_forbidden_imports(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
        forbidden_prefixes = (
            "app.scoring",
            "app.backtest",
            "app.strategies",
            "app.pipeline",
            "app.broker",
        )
        for module in imported:
            for prefix in forbidden_prefixes:
                assert not (module == prefix or module.startswith(prefix + ".")), f"forbidden import: {module}"

    def test_no_real_data_commands(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        for token in ("run_pipeline", "run_scoring", "run_backtest", "tushare"):
            assert token not in source, f"module must not contain {token!r}"

    def test_test_file_no_forbidden_imports(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
        forbidden_prefixes = (
            "app.scoring",
            "app.backtest",
            "app.strategies",
            "app.pipeline",
            "app.broker",
        )
        for module in imported:
            for prefix in forbidden_prefixes:
                assert not (module == prefix or module.startswith(prefix + ".")), f"test forbidden import: {module}"


# ---------------------------------------------------------------------------
# Engine version AST verification
# ---------------------------------------------------------------------------


class TestEngineVersionAST:
    def test_ast_verification_succeeds_on_real_engine(self) -> None:
        from app.research.layer_two_alpha_diagnostic_run_contract import (
            _verify_engine_version_constant,
        )

        engine_path = REPO_ROOT / BOUND_ENGINE_PATH
        _verify_engine_version_constant(engine_path, BOUND_ENGINE_VERSION)

    def test_ast_verification_rejects_wrong_version(self) -> None:
        import tempfile

        from app.research.layer_two_alpha_diagnostic_run_contract import (
            _verify_engine_version_constant,
        )

        source = 'LAYER_TWO_ALPHA_DIAGNOSTIC_ENGINE_VERSION: str = "wrong-version"\n'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(source)
            tmp = Path(f.name)
        try:
            with pytest.raises(ValueError, match="AST value"):
                _verify_engine_version_constant(tmp, BOUND_ENGINE_VERSION)
        finally:
            tmp.unlink()

    def test_ast_verification_rejects_string_contains_trick(self) -> None:
        """Ensures source-string-contains cannot fool AST-based check."""
        import tempfile

        from app.research.layer_two_alpha_diagnostic_run_contract import (
            _verify_engine_version_constant,
        )

        source = f'_DECOY = "{BOUND_ENGINE_VERSION}"\nLAYER_TWO_ALPHA_DIAGNOSTIC_ENGINE_VERSION: str = "tampered"\n'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(source)
            tmp = Path(f.name)
        try:
            with pytest.raises(ValueError, match="AST value"):
                _verify_engine_version_constant(tmp, BOUND_ENGINE_VERSION)
        finally:
            tmp.unlink()

    def test_ast_verification_rejects_missing_constant(self) -> None:
        import tempfile

        from app.research.layer_two_alpha_diagnostic_run_contract import (
            _verify_engine_version_constant,
        )

        source = "# empty module\nx = 1\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(source)
            tmp = Path(f.name)
        try:
            with pytest.raises(ValueError, match="missing"):
                _verify_engine_version_constant(tmp, BOUND_ENGINE_VERSION)
        finally:
            tmp.unlink()


# ---------------------------------------------------------------------------
# Config JSON generated by builder (not hand-written)
# ---------------------------------------------------------------------------


class TestConfigFromBuilder:
    def test_config_matches_builder_output(self) -> None:
        sealed = _build_sealed()
        on_disk = _contract_json()
        assert on_disk["contract_id"] == sealed.contract_id

    def test_config_round_trips(self) -> None:
        loaded = load_contract(CONTRACT_JSON_PATH)
        assert_contract_self_hash(loaded)

    def test_config_has_endpoint_rules(self) -> None:
        on_disk = _contract_json()
        assert on_disk["windows"]["label_endpoint_must_remain_within_same_window"] is True
        assert on_disk["windows"]["horizon_never_shifts_or_shortens"] is True
        assert on_disk["windows"]["missing_or_unverified_endpoint_is_unknown"] is True

    def test_engine_sha_is_4680(self) -> None:
        on_disk = _contract_json()
        assert on_disk["engine_file_sha256"].startswith("4680")


# ---------------------------------------------------------------------------
# Write and reload
# ---------------------------------------------------------------------------


class TestWriteAndReload:
    def test_write_and_reload(self) -> None:
        contract = build_layer_two_alpha_diagnostic_run_contract()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            sealed = write_contract(tmp, contract)
            reloaded = load_contract(tmp)
            assert reloaded.contract_id == sealed.contract_id
            assert_contract_self_hash(reloaded)
        finally:
            tmp.unlink()


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


class TestConstants:
    def test_frozen_family_ids(self) -> None:
        assert FROZEN_HYPOTHESIS_FAMILY_IDS == (
            "quality",
            "value",
            "medium_momentum_12_1",
            "defensive_low_vol",
        )

    def test_holm_params(self) -> None:
        assert HOLM_FAMILY_WISE_ALPHA == 0.05
        assert HOLM_HYPOTHESIS_COUNT == 4
        assert PRIMARY_HORIZON == 40
        assert PRIMARY_HAC_LAG == 39

    def test_frozen_label_horizons(self) -> None:
        assert FROZEN_LABEL_HORIZONS == (5, 20, 40)

    def test_window_dates(self) -> None:
        assert DEVELOPMENT_START == date(2022, 1, 1)
        assert DEVELOPMENT_END == date(2023, 12, 31)
        assert SEEN_ROBUSTNESS_START == date(2024, 1, 1)
        assert SEEN_ROBUSTNESS_END == date(2024, 12, 31)
        assert CONSUMED_OOS_START == date(2025, 1, 1)
        assert CONSUMED_OOS_END == date(2026, 8, 21)
        assert NEW_FROZEN_OOS_BEGINS == date(2026, 8, 22)

    def test_bound_paths(self) -> None:
        assert BOUND_E11A_PROTOCOL_PATH == "config/research/layer-two-alpha-development-protocol-v1.json"
        assert BOUND_ENGINE_PATH == "src/app/research/layer_two_alpha_diagnostic_engine.py"
        assert BOUND_LEDGER_PATH == "config/research/research-trial-ledger-v1.json"
        assert BOUND_ENGINE_VERSION == "layer-two-alpha-diagnostic-engine-v0a"
