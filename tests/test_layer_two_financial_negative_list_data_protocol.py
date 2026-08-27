"""Tests for E11b-2a frozen PIT financial-negative-list data protocol.

Validates canonical seal/id, exact golden protocol file verification,
threshold/date/source field/readiness/contract/candidate pack drift detection,
same-day cutoff semantics, strict threshold boundaries, scope behavior,
no silent total_revenue fallback, partial debt components unknown,
restatement ambiguity unknown, missing remains unknown, 2025/OOS rejected.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.research.layer_two_financial_negative_list_data_protocol import (
    ANNOUNCEMENT_COLLECTION_END,
    ANNOUNCEMENT_COLLECTION_START,
    BALANCESHEET_FIELDS,
    BOUND_CANDIDATE_PACK_ID,
    BOUND_CANDIDATE_PACK_PARQUET_SHA256,
    BOUND_CANDIDATE_PACK_ROW_COUNT,
    BOUND_E10B_ENGINE_VERSION,
    BOUND_E10B_MODULE_SHA256,
    BOUND_RAW_COLLECTION_MANIFEST_SHA256,
    BOUND_RAW_COLLECTION_REQUEST_ID,
    BOUND_RAW_QUALITY_REPORT_SHA256,
    BOUND_TWO_LAYER_CONTRACT_FILE_SHA256,
    BOUND_TWO_LAYER_CONTRACT_ID,
    DECISION_WINDOW_END,
    DECISION_WINDOW_START,
    FINA_AUDIT_FIELDS,
    FINA_INDICATOR_FIELDS,
    INCOME_FIELDS,
    MAX_REPORT_PERIOD_AGE_AUDIT_DAYS,
    MAX_REPORT_PERIOD_AGE_STATEMENT_DAYS,
    PROTOCOL_FILE_PATH,
    PROTOCOL_ID,
    SOURCE_ENDPOINTS,
    THRESHOLD_CASH_DEBT_RATIO,
    THRESHOLD_GOODWILL_RATIO,
    THRESHOLD_OTHER_RECEIVABLES_RATIO,
    THRESHOLD_RECEIVABLES_REVENUE_GAP,
    FinancialNegativeListDataProtocol,
    ProtocolReadiness,
    _validate_safe_path,
    compute_exposure,
    compute_protocol_id,
    effective_disclosure_date,
    evaluate_cash_debt_ratio,
    evaluate_debt_component_crosscheck,
    evaluate_non_standard_audit,
    evaluate_ratio_rule,
    evaluate_receivables_revenue_gap,
    evaluate_two_period_gaps,
    is_general_industrial,
    is_in_decision_window,
    is_included_report_type,
    is_usable_at_decision,
    is_within_freshness,
    load_protocol,
    make_available_at,
    make_decision_at,
    reject_oos_date,
    report_period_age_days,
    verify_protocol,
    verify_protocol_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Canonical seal / ID verification
# ---------------------------------------------------------------------------


class TestCanonicalSeal:
    def test_golden_file_seal_verifies(self) -> None:
        """The on-disk protocol file passes full verification."""
        protocol = verify_protocol_file(REPO_ROOT)
        assert protocol.protocol_id == PROTOCOL_ID

    def test_golden_production_disk_verification(self) -> None:
        protocol = verify_protocol_file(REPO_ROOT)
        assert protocol.protocol_id == PROTOCOL_ID
        assert protocol.bindings.e10b_module_sha256 == BOUND_E10B_MODULE_SHA256

    def test_golden_verification_is_cwd_independent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unrelated = Path(tmp).resolve()
            monkeypatch.chdir(unrelated)
            protocol = verify_protocol_file(REPO_ROOT)
        assert protocol.protocol_id == PROTOCOL_ID

    def test_protocol_id_is_64_hex(self) -> None:
        import re

        assert re.fullmatch(r"[0-9a-f]{64}", PROTOCOL_ID)

    def test_compute_protocol_id_matches_stored(self) -> None:
        data = load_protocol(REPO_ROOT / PROTOCOL_FILE_PATH)
        assert compute_protocol_id(data) == data["protocol_id"]

    def test_tampered_field_breaks_seal(self) -> None:
        data = load_protocol(REPO_ROOT / PROTOCOL_FILE_PATH)
        data["status"] = "tampered"
        computed = compute_protocol_id(data)
        assert computed != PROTOCOL_ID

    def test_tampered_file_rejected(self) -> None:
        data = load_protocol(REPO_ROOT / PROTOCOL_FILE_PATH)
        data["status"] = "tampered"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / PROTOCOL_FILE_PATH
            out.parent.mkdir(parents=True)
            with open(out, "w") as f:
                json.dump(data, f)
            with pytest.raises(ValueError, match="seal mismatch"):
                verify_protocol(out, repo_root=root)

    def test_verify_protocol_file_fail_closed_when_bound_artifacts_missing(self) -> None:
        data = load_protocol(REPO_ROOT / PROTOCOL_FILE_PATH)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / PROTOCOL_FILE_PATH
            out.parent.mkdir(parents=True)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

            sealed_protocol = verify_protocol(out, repo_root=root)
            assert sealed_protocol.protocol_id == PROTOCOL_ID

            with pytest.raises(ValueError, match="escapes repo root|not found"):
                verify_protocol_file(root)

    def test_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(FileNotFoundError):
                verify_protocol_file(Path(tmp))


# ---------------------------------------------------------------------------
# Binding drift detection
# ---------------------------------------------------------------------------


class TestBindingDrift:
    def _load_and_reseal(self, mutation: dict[str, Any]) -> dict[str, Any]:
        data = load_protocol(REPO_ROOT / PROTOCOL_FILE_PATH)
        bindings = data["bindings"]
        bindings.update(mutation)
        data["protocol_id"] = compute_protocol_id(data)
        return data

    def test_contract_id_drift(self) -> None:
        data = self._load_and_reseal({"two_layer_decision_contract_id": "0" * 64})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / PROTOCOL_FILE_PATH
            out.parent.mkdir(parents=True)
            with open(out, "w") as f:
                json.dump(data, f)
            with pytest.raises(ValueError, match="does not match frozen constant"):
                verify_protocol(out, repo_root=root)


class TestTypedSemanticHardening:
    def _load_and_reseal(self, mutation: dict[str, Any]) -> dict[str, Any]:
        data = load_protocol(REPO_ROOT / PROTOCOL_FILE_PATH)
        bindings = data["bindings"]
        bindings.update(mutation)
        data["protocol_id"] = compute_protocol_id(data)
        return data

    def _reseal(self, data: dict[str, Any]) -> dict[str, Any]:
        mutated = json.loads(json.dumps(data))
        mutated["protocol_id"] = compute_protocol_id(mutated)
        return mutated

    def _load_base(self) -> dict[str, Any]:
        return load_protocol(REPO_ROOT / PROTOCOL_FILE_PATH)

    def test_endpoint_missing_field_rejected_by_typed_semantics(self) -> None:
        data = self._load_base()
        data["source_endpoints"]["income"]["fields"].remove("total_revenue")
        resealed = self._reseal(data)
        with pytest.raises(Exception, match="source_endpoints.income.fields must exactly match"):
            FinancialNegativeListDataProtocol.model_validate(resealed)

    def test_pit_flag_false_rejected_by_typed_semantics(self) -> None:
        data = self._load_base()
        data["pit_availability"]["same_day_decision_unusable"] = False
        resealed = self._reseal(data)
        with pytest.raises(Exception, match="pit_availability flags must exactly match"):
            FinancialNegativeListDataProtocol.model_validate(resealed)

    def test_rule_threshold_changed_rejected_by_typed_semantics(self) -> None:
        data = self._load_base()
        data["rules"]["D_other_receivables_to_assets"]["threshold"] = 0.051
        resealed = self._reseal(data)
        with pytest.raises(Exception, match="rules.D_other_receivables_to_assets semantics must exactly match"):
            FinancialNegativeListDataProtocol.model_validate(resealed)

    def test_issue_code_mapping_changed_rejected_by_typed_semantics(self) -> None:
        data = self._load_base()
        data["issue_codes"]["FNLD-010"] = "changed_negative_numerator_invalid"
        resealed = self._reseal(data)
        with pytest.raises(Exception, match="issue_codes must exactly match"):
            FinancialNegativeListDataProtocol.model_validate(resealed)

    def test_contract_file_sha_drift(self) -> None:
        data = self._load_and_reseal({"two_layer_decision_contract_file_sha256": "1" * 64})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / PROTOCOL_FILE_PATH
            out.parent.mkdir(parents=True)
            with open(out, "w") as f:
                json.dump(data, f)
            with pytest.raises(ValueError, match="does not match frozen constant"):
                verify_protocol(out, repo_root=root)

    def test_e10b_module_sha_drift(self) -> None:
        data = self._load_and_reseal({"e10b_module_sha256": "2" * 64})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / PROTOCOL_FILE_PATH
            out.parent.mkdir(parents=True)
            with open(out, "w") as f:
                json.dump(data, f)
            with pytest.raises(ValueError, match="does not match frozen constant"):
                verify_protocol(out, repo_root=root)

    def test_candidate_pack_id_drift(self) -> None:
        data = self._load_and_reseal({"candidate_pack_id": "3" * 64})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / PROTOCOL_FILE_PATH
            out.parent.mkdir(parents=True)
            with open(out, "w") as f:
                json.dump(data, f)
            with pytest.raises(ValueError, match="does not match frozen constant"):
                verify_protocol(out, repo_root=root)

    def test_candidate_pack_row_count_drift(self) -> None:
        data = self._load_and_reseal({"candidate_pack_row_count": 9999})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / PROTOCOL_FILE_PATH
            out.parent.mkdir(parents=True)
            with open(out, "w") as f:
                json.dump(data, f)
            with pytest.raises(ValueError, match="does not match frozen constant"):
                verify_protocol(out, repo_root=root)

    def test_raw_request_id_drift(self) -> None:
        data = self._load_and_reseal({"raw_collection_request_id": "4" * 64})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / PROTOCOL_FILE_PATH
            out.parent.mkdir(parents=True)
            with open(out, "w") as f:
                json.dump(data, f)
            with pytest.raises(ValueError, match="does not match frozen constant"):
                verify_protocol(out, repo_root=root)


# ---------------------------------------------------------------------------
# Readiness mutation
# ---------------------------------------------------------------------------


class TestReadiness:
    def test_all_readiness_false(self) -> None:
        protocol = verify_protocol_file(REPO_ROOT)
        r = protocol.readiness
        assert r.research_only is True
        assert r.ready_for_scoring is False
        assert r.ready_for_backtest is False
        assert r.ready_for_portfolio_construction is False
        assert r.ready_for_trading is False
        assert r.ready_for_data_collection is False
        assert r.auto_apply is False

    def test_readiness_mutation_rejected(self) -> None:
        with pytest.raises(ValueError):
            ProtocolReadiness(
                research_only=True,
                ready_for_scoring=True,
                ready_for_backtest=False,
                ready_for_portfolio_construction=False,
                ready_for_trading=False,
                ready_for_data_collection=False,
                auto_apply=False,
            )

    def test_research_only_false_rejected(self) -> None:
        with pytest.raises(ValueError):
            ProtocolReadiness(
                research_only=False,
                ready_for_scoring=False,
                ready_for_backtest=False,
                ready_for_portfolio_construction=False,
                ready_for_trading=False,
                ready_for_data_collection=False,
                auto_apply=False,
            )


# ---------------------------------------------------------------------------
# Same-day cutoff semantics
# ---------------------------------------------------------------------------


class TestSameDayCutoff:
    def test_same_day_disclosure_unusable(self) -> None:
        """Disclosed at 23:59:59 same day > decision at 17:30 => unusable."""
        assert is_usable_at_decision(date(2023, 3, 15), date(2023, 3, 15)) is False

    def test_prior_day_disclosure_usable(self) -> None:
        """Disclosed day before => usable at next decision."""
        assert is_usable_at_decision(date(2023, 3, 14), date(2023, 3, 15)) is True

    def test_future_disclosure_unusable(self) -> None:
        """Future disclosure date => unusable."""
        assert is_usable_at_decision(date(2023, 3, 16), date(2023, 3, 15)) is False

    def test_decision_at_exact_time(self) -> None:
        dt = make_decision_at(date(2023, 6, 1))
        assert dt.hour == 17
        assert dt.minute == 30
        assert dt.second == 0

    def test_available_at_exact_time(self) -> None:
        dt = make_available_at(date(2023, 6, 1))
        assert dt.hour == 23
        assert dt.minute == 59
        assert dt.second == 59


# ---------------------------------------------------------------------------
# Effective disclosure date
# ---------------------------------------------------------------------------


class TestEffectiveDisclosureDate:
    def test_ann_date_only(self) -> None:
        assert effective_disclosure_date(date(2023, 4, 1), None) == date(2023, 4, 1)

    def test_both_takes_max(self) -> None:
        assert effective_disclosure_date(date(2023, 4, 1), date(2023, 4, 15)) == date(2023, 4, 15)

    def test_f_ann_date_earlier(self) -> None:
        assert effective_disclosure_date(date(2023, 4, 15), date(2023, 4, 1)) == date(2023, 4, 15)

    def test_missing_ann_date_returns_none(self) -> None:
        assert effective_disclosure_date(None, date(2023, 4, 1)) is None

    def test_both_none(self) -> None:
        assert effective_disclosure_date(None, None) is None


# ---------------------------------------------------------------------------
# 2025 / OOS rejection
# ---------------------------------------------------------------------------


class TestOOSRejection:
    def test_2025_rejected(self) -> None:
        with pytest.raises(ValueError, match="2025"):
            reject_oos_date(date(2025, 1, 1))

    def test_2026_rejected(self) -> None:
        with pytest.raises(ValueError, match="2025"):
            reject_oos_date(date(2026, 6, 15))

    def test_2021_rejected(self) -> None:
        with pytest.raises(ValueError, match="outside decision window"):
            reject_oos_date(date(2021, 12, 31))

    def test_2022_start_accepted(self) -> None:
        reject_oos_date(date(2022, 1, 1))

    def test_2024_end_accepted(self) -> None:
        reject_oos_date(date(2024, 12, 31))

    def test_decision_window_boundary(self) -> None:
        assert is_in_decision_window(date(2022, 1, 1)) is True
        assert is_in_decision_window(date(2024, 12, 31)) is True
        assert is_in_decision_window(date(2021, 12, 31)) is False
        assert is_in_decision_window(date(2025, 1, 1)) is False


# ---------------------------------------------------------------------------
# Report type / comp_type scope
# ---------------------------------------------------------------------------


class TestScope:
    def test_included_report_types(self) -> None:
        assert is_included_report_type(1) is True
        assert is_included_report_type(4) is True
        assert is_included_report_type(5) is True

    def test_excluded_report_types(self) -> None:
        for rt in [2, 3, 6, 7, 8, 9, 10, 11, 12]:
            assert is_included_report_type(rt) is False

    def test_none_report_type(self) -> None:
        assert is_included_report_type(None) is False

    def test_general_industrial(self) -> None:
        assert is_general_industrial(1) is True

    def test_bank_not_general(self) -> None:
        assert is_general_industrial(2) is False

    def test_insurer_not_general(self) -> None:
        assert is_general_industrial(3) is False

    def test_securities_not_general(self) -> None:
        assert is_general_industrial(4) is False

    def test_missing_comp_type(self) -> None:
        assert is_general_industrial(None) is None

    @pytest.mark.parametrize("invalid_code", [0, -1, 5, 999])
    def test_invalid_comp_type_codes_return_none(self, invalid_code: int) -> None:
        assert is_general_industrial(invalid_code) is None

    def test_bool_comp_type_returns_none(self) -> None:
        assert is_general_industrial(True) is None


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


class TestFreshness:
    def test_audit_within_limit(self) -> None:
        assert is_within_freshness(date(2023, 6, 1), date(2022, 12, 31), MAX_REPORT_PERIOD_AGE_AUDIT_DAYS)

    def test_audit_beyond_limit(self) -> None:
        decision = date(2023, 12, 31)
        end = date(2022, 1, 1)
        age = report_period_age_days(decision, end)
        assert age > MAX_REPORT_PERIOD_AGE_AUDIT_DAYS
        assert is_within_freshness(decision, end, MAX_REPORT_PERIOD_AGE_AUDIT_DAYS) is False

    def test_statement_within_limit(self) -> None:
        assert is_within_freshness(date(2023, 6, 1), date(2023, 3, 31), MAX_REPORT_PERIOD_AGE_STATEMENT_DAYS)

    def test_statement_beyond_limit(self) -> None:
        decision = date(2023, 12, 31)
        end = date(2023, 3, 31)
        age = report_period_age_days(decision, end)
        assert age > MAX_REPORT_PERIOD_AGE_STATEMENT_DAYS
        assert is_within_freshness(decision, end, MAX_REPORT_PERIOD_AGE_STATEMENT_DAYS) is False

    def test_exact_boundary_audit(self) -> None:
        end = date(2023, 1, 1)
        decision = end + timedelta(days=MAX_REPORT_PERIOD_AGE_AUDIT_DAYS)
        assert is_within_freshness(decision, end, MAX_REPORT_PERIOD_AGE_AUDIT_DAYS) is True
        decision_over = end + timedelta(days=MAX_REPORT_PERIOD_AGE_AUDIT_DAYS + 1)
        assert is_within_freshness(decision_over, end, MAX_REPORT_PERIOD_AGE_AUDIT_DAYS) is False


# ---------------------------------------------------------------------------
# Rule A: non-standard audit
# ---------------------------------------------------------------------------


class TestRuleANonStandardAudit:
    def test_clean_standard(self) -> None:
        assert evaluate_non_standard_audit("标准无保留意见") == "false"

    def test_qualified(self) -> None:
        assert evaluate_non_standard_audit("保留意见") == "true"

    def test_other_nonblank(self) -> None:
        assert evaluate_non_standard_audit("无法表示意见") == "true"

    def test_missing_none(self) -> None:
        assert evaluate_non_standard_audit(None) == "unknown"

    def test_blank_string(self) -> None:
        assert evaluate_non_standard_audit("") == "unknown"

    def test_whitespace_only(self) -> None:
        assert evaluate_non_standard_audit("   ") == "unknown"


# ---------------------------------------------------------------------------
# Rule B: large cash and interest-bearing debt
# ---------------------------------------------------------------------------


class TestRuleBCashDebt:
    def test_both_above_threshold_true(self) -> None:
        assert evaluate_cash_debt_ratio(30.0, 100.0, 30.0) == "true"

    def test_cash_below_threshold_false(self) -> None:
        assert evaluate_cash_debt_ratio(20.0, 100.0, 30.0) == "false"

    def test_debt_below_threshold_false(self) -> None:
        assert evaluate_cash_debt_ratio(30.0, 100.0, 20.0) == "false"

    def test_exact_boundary_not_triggered(self) -> None:
        """Strict >: exactly 0.25 does NOT trigger."""
        assert evaluate_cash_debt_ratio(25.0, 100.0, 25.0) == "false"

    def test_just_above_boundary_triggers(self) -> None:
        assert evaluate_cash_debt_ratio(25.01, 100.0, 25.01) == "true"

    def test_missing_total_assets(self) -> None:
        assert evaluate_cash_debt_ratio(30.0, None, 30.0) == "unknown"

    def test_zero_total_assets(self) -> None:
        assert evaluate_cash_debt_ratio(30.0, 0.0, 30.0) == "unknown"

    def test_negative_total_assets(self) -> None:
        assert evaluate_cash_debt_ratio(30.0, -100.0, 30.0) == "unknown"

    def test_negative_money_cap(self) -> None:
        assert evaluate_cash_debt_ratio(-1.0, 100.0, 30.0) == "unknown"

    def test_negative_interestdebt(self) -> None:
        assert evaluate_cash_debt_ratio(30.0, 100.0, -1.0) == "unknown"


# ---------------------------------------------------------------------------
# Debt component crosscheck
# ---------------------------------------------------------------------------


class TestDebtComponentCrosscheck:
    def test_all_five_numeric(self) -> None:
        result = evaluate_debt_component_crosscheck(10.0, 20.0, 5.0, 3.0, 7.0)
        assert result == 45.0

    def test_partial_missing_returns_none(self) -> None:
        """Partial components never zero-filled."""
        assert evaluate_debt_component_crosscheck(10.0, 20.0, None, 3.0, 7.0) is None

    def test_all_none(self) -> None:
        assert evaluate_debt_component_crosscheck(None, None, None, None, None) is None

    def test_non_numeric_string(self) -> None:
        assert evaluate_debt_component_crosscheck(10.0, "abc", 5.0, 3.0, 7.0) is None

    def test_inf_value(self) -> None:
        assert evaluate_debt_component_crosscheck(float("inf"), 20.0, 5.0, 3.0, 7.0) is None

    def test_negative_component_returns_none(self) -> None:
        assert evaluate_debt_component_crosscheck(-1.0, 20.0, 5.0, 3.0, 7.0) is None

    def test_bool_component_returns_none(self) -> None:
        assert evaluate_debt_component_crosscheck(True, 20.0, 5.0, 3.0, 7.0) is None


# ---------------------------------------------------------------------------
# Rule C: receivables/inventory growth vs revenue
# ---------------------------------------------------------------------------


class TestRuleCReceivablesRevenue:
    def test_both_gaps_above_threshold_true(self) -> None:
        gap1 = evaluate_receivables_revenue_gap(130.0, 100.0, 100.0, 100.0)
        gap2 = evaluate_receivables_revenue_gap(160.0, 120.0, 120.0, 120.0)
        assert gap1 is not None and gap1 > 0.20
        assert gap2 is not None and gap2 > 0.20
        assert evaluate_two_period_gaps(gap1, gap2) == "true"

    def test_one_gap_below_false(self) -> None:
        gap1 = evaluate_receivables_revenue_gap(130.0, 100.0, 100.0, 100.0)
        gap2 = evaluate_receivables_revenue_gap(110.0, 100.0, 100.0, 100.0)
        assert gap1 is not None and gap1 > 0.20
        assert gap2 is not None and gap2 <= 0.20
        assert evaluate_two_period_gaps(gap1, gap2) == "false"

    def test_exact_threshold_not_triggered(self) -> None:
        """Strict >0.20: exactly 0.20 does NOT trigger."""
        gap = evaluate_receivables_revenue_gap(120.0, 100.0, 100.0, 100.0)
        assert gap is not None
        assert gap == pytest.approx(0.20)
        assert evaluate_two_period_gaps(gap, gap) == "false"

    def test_missing_gap_unknown(self) -> None:
        assert evaluate_two_period_gaps(None, 0.3) == "unknown"
        assert evaluate_two_period_gaps(0.3, None) == "unknown"

    def test_zero_prior_exposure_unknown(self) -> None:
        assert evaluate_receivables_revenue_gap(130.0, 0.0, 100.0, 100.0) is None

    def test_zero_prior_revenue_unknown(self) -> None:
        assert evaluate_receivables_revenue_gap(130.0, 100.0, 100.0, 0.0) is None

    def test_no_silent_total_revenue_fallback(self) -> None:
        """Revenue field is 'revenue' only; total_revenue not used."""
        protocol = verify_protocol_file(REPO_ROOT)
        rule_c = protocol.rules.C_receivables_inventory_growth_vs_revenue_two_periods
        assert rule_c.revenue_field == "revenue"
        assert rule_c.no_silent_total_revenue_fallback is True

    @pytest.mark.parametrize("value", [-1.0, True, float("inf"), float("-inf"), float("nan")])
    def test_invalid_gap_inputs_return_none(self, value: float | bool) -> None:
        assert evaluate_receivables_revenue_gap(value, 100.0, 100.0, 100.0) is None
        assert evaluate_receivables_revenue_gap(100.0, value, 100.0, 100.0) is None
        assert evaluate_receivables_revenue_gap(100.0, 100.0, value, 100.0) is None
        assert evaluate_receivables_revenue_gap(100.0, 100.0, 100.0, value) is None


# ---------------------------------------------------------------------------
# Rule D: other receivables to assets
# ---------------------------------------------------------------------------


class TestRuleDOtherReceivables:
    def test_above_threshold_true(self) -> None:
        assert evaluate_ratio_rule(6.0, 100.0, THRESHOLD_OTHER_RECEIVABLES_RATIO) == "true"

    def test_below_threshold_false(self) -> None:
        assert evaluate_ratio_rule(4.0, 100.0, THRESHOLD_OTHER_RECEIVABLES_RATIO) == "false"

    def test_exact_boundary_not_triggered(self) -> None:
        """Strict >: exactly 0.05 does NOT trigger."""
        assert evaluate_ratio_rule(5.0, 100.0, THRESHOLD_OTHER_RECEIVABLES_RATIO) == "false"

    def test_negative_numerator_unknown(self) -> None:
        assert evaluate_ratio_rule(-1.0, 100.0, THRESHOLD_OTHER_RECEIVABLES_RATIO) == "unknown"

    def test_zero_denominator_unknown(self) -> None:
        assert evaluate_ratio_rule(6.0, 0.0, THRESHOLD_OTHER_RECEIVABLES_RATIO) == "unknown"

    def test_missing_unknown(self) -> None:
        assert evaluate_ratio_rule(None, 100.0, THRESHOLD_OTHER_RECEIVABLES_RATIO) == "unknown"


# ---------------------------------------------------------------------------
# Rule E: goodwill to net assets
# ---------------------------------------------------------------------------


class TestRuleEGoodwill:
    def test_above_threshold_true(self) -> None:
        assert evaluate_ratio_rule(35.0, 100.0, THRESHOLD_GOODWILL_RATIO) == "true"

    def test_below_threshold_false(self) -> None:
        assert evaluate_ratio_rule(25.0, 100.0, THRESHOLD_GOODWILL_RATIO) == "false"

    def test_exact_boundary_not_triggered(self) -> None:
        assert evaluate_ratio_rule(30.0, 100.0, THRESHOLD_GOODWILL_RATIO) == "false"

    def test_nonpositive_equity_unknown(self) -> None:
        assert evaluate_ratio_rule(35.0, 0.0, THRESHOLD_GOODWILL_RATIO) == "unknown"
        assert evaluate_ratio_rule(35.0, -50.0, THRESHOLD_GOODWILL_RATIO) == "unknown"

    def test_negative_goodwill_unknown(self) -> None:
        assert evaluate_ratio_rule(-5.0, 100.0, THRESHOLD_GOODWILL_RATIO) == "unknown"


# ---------------------------------------------------------------------------
# Exposure computation
# ---------------------------------------------------------------------------


class TestExposure:
    def test_all_numeric(self) -> None:
        assert compute_exposure(10.0, 20.0, 30.0) == 60.0

    def test_missing_one_returns_none(self) -> None:
        assert compute_exposure(10.0, None, 30.0) is None

    def test_non_numeric_returns_none(self) -> None:
        assert compute_exposure(10.0, "abc", 30.0) is None

    def test_negative_component_returns_none(self) -> None:
        assert compute_exposure(-1.0, 20.0, 30.0) is None

    def test_bool_component_returns_none(self) -> None:
        assert compute_exposure(True, 20.0, 30.0) is None


# ---------------------------------------------------------------------------
# Source field constants
# ---------------------------------------------------------------------------


class TestSourceFields:
    def test_endpoints_complete(self) -> None:
        assert "balancesheet" in SOURCE_ENDPOINTS
        assert "income" in SOURCE_ENDPOINTS
        assert "fina_indicator" in SOURCE_ENDPOINTS
        assert "fina_audit" in SOURCE_ENDPOINTS

    def test_balancesheet_fields(self) -> None:
        assert "money_cap" in BALANCESHEET_FIELDS
        assert "notes_receiv" in BALANCESHEET_FIELDS
        assert "accounts_receiv" in BALANCESHEET_FIELDS
        assert "oth_receiv" in BALANCESHEET_FIELDS
        assert "inventories" in BALANCESHEET_FIELDS
        assert "goodwill" in BALANCESHEET_FIELDS
        assert "total_assets" in BALANCESHEET_FIELDS
        assert "total_hldr_eqy_exc_min_int" in BALANCESHEET_FIELDS
        for f in ("st_borr", "lt_borr", "st_bonds_payable", "non_cur_liab_due_1y", "bond_payable"):
            assert f in BALANCESHEET_FIELDS

    def test_income_fields(self) -> None:
        assert "revenue" in INCOME_FIELDS
        assert "total_revenue" in INCOME_FIELDS

    def test_fina_indicator_fields(self) -> None:
        assert "interestdebt" in FINA_INDICATOR_FIELDS

    def test_fina_audit_fields(self) -> None:
        assert "audit_result" in FINA_AUDIT_FIELDS
        assert "audit_agency" in FINA_AUDIT_FIELDS


# ---------------------------------------------------------------------------
# Protocol model semantic checks
# ---------------------------------------------------------------------------


class TestProtocolSemantics:
    def test_not_alpha_evidence(self) -> None:
        protocol = verify_protocol_file(REPO_ROOT)
        assert protocol.not_alpha_evidence is True

    def test_not_authorization(self) -> None:
        protocol = verify_protocol_file(REPO_ROOT)
        assert protocol.not_authorization is True

    def test_does_not_modify_e10b(self) -> None:
        protocol = verify_protocol_file(REPO_ROOT)
        assert protocol.does_not_modify_e10b_evaluator is True

    def test_does_not_deploy_rules(self) -> None:
        protocol = verify_protocol_file(REPO_ROOT)
        assert protocol.does_not_deploy_rules is True

    def test_outcome_driven_changes_forbidden(self) -> None:
        protocol = verify_protocol_file(REPO_ROOT)
        assert protocol.outcome_driven_changes_forbidden is True

    def test_bindings_match_constants(self) -> None:
        protocol = verify_protocol_file(REPO_ROOT)
        b = protocol.bindings
        assert b.two_layer_decision_contract_id == BOUND_TWO_LAYER_CONTRACT_ID
        assert b.two_layer_decision_contract_file_sha256 == BOUND_TWO_LAYER_CONTRACT_FILE_SHA256
        assert b.e10b_engine_version == BOUND_E10B_ENGINE_VERSION
        assert b.e10b_module_sha256 == BOUND_E10B_MODULE_SHA256
        assert b.candidate_pack_id == BOUND_CANDIDATE_PACK_ID
        assert b.candidate_pack_parquet_sha256 == BOUND_CANDIDATE_PACK_PARQUET_SHA256
        assert b.candidate_pack_row_count == BOUND_CANDIDATE_PACK_ROW_COUNT
        assert b.raw_collection_request_id == BOUND_RAW_COLLECTION_REQUEST_ID
        assert b.raw_collection_manifest_sha256 == BOUND_RAW_COLLECTION_MANIFEST_SHA256
        assert b.raw_quality_report_sha256 == BOUND_RAW_QUALITY_REPORT_SHA256

    def test_decision_window_dates(self) -> None:
        assert DECISION_WINDOW_START == date(2022, 1, 1)
        assert DECISION_WINDOW_END == date(2024, 12, 31)

    def test_announcement_window_dates(self) -> None:
        assert ANNOUNCEMENT_COLLECTION_START == date(2020, 1, 1)
        assert ANNOUNCEMENT_COLLECTION_END == date(2024, 12, 31)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


class TestPathSafety:
    def test_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "real.json"
            target.write_text("{}")
            link = root / "link.json"
            link.symlink_to(target)
            with pytest.raises(ValueError, match="symlink"):
                verify_protocol(link, repo_root=root)

    def test_outside_repo_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp1:
            with tempfile.TemporaryDirectory() as tmp2:
                root = Path(tmp1).resolve()
                outside = Path(tmp2).resolve() / "protocol.json"
                outside.write_text("{}")
                with pytest.raises(ValueError, match="inside repo_root"):
                    verify_protocol(outside, repo_root=root)

    def test_symlink_component_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            real = root / "real"
            real.mkdir()
            link_dir = root / "linkdir"
            link_dir.symlink_to(real, target_is_directory=True)
            with pytest.raises(ValueError, match="symlink component"):
                _validate_safe_path(link_dir / "file.json", repo_root=root, field_name="candidate_pack_path")


# ---------------------------------------------------------------------------
# Threshold constants frozen
# ---------------------------------------------------------------------------


class TestThresholdConstants:
    def test_cash_debt_threshold(self) -> None:
        assert THRESHOLD_CASH_DEBT_RATIO == 0.25

    def test_receivables_gap_threshold(self) -> None:
        assert THRESHOLD_RECEIVABLES_REVENUE_GAP == 0.20

    def test_other_receivables_threshold(self) -> None:
        assert THRESHOLD_OTHER_RECEIVABLES_RATIO == 0.05

    def test_goodwill_threshold(self) -> None:
        assert THRESHOLD_GOODWILL_RATIO == 0.30

    def test_audit_freshness(self) -> None:
        assert MAX_REPORT_PERIOD_AGE_AUDIT_DAYS == 550

    def test_statement_freshness(self) -> None:
        assert MAX_REPORT_PERIOD_AGE_STATEMENT_DAYS == 240


# ---------------------------------------------------------------------------
# Missing remains unknown
# ---------------------------------------------------------------------------


class TestMissingRemainsUnknown:
    def test_all_rules_missing_inputs(self) -> None:
        assert evaluate_non_standard_audit(None) == "unknown"
        assert evaluate_cash_debt_ratio(None, None, None) == "unknown"
        assert evaluate_two_period_gaps(None, None) == "unknown"
        assert evaluate_ratio_rule(None, None, 0.05) == "unknown"
        assert evaluate_ratio_rule(None, None, 0.30) == "unknown"


# ---------------------------------------------------------------------------
# Restatement ambiguity
# ---------------------------------------------------------------------------


class TestRestatementAmbiguity:
    def test_protocol_encodes_ambiguous_restatement_unknown(self) -> None:
        protocol = verify_protocol_file(REPO_ROOT)
        pit = protocol.pit_availability
        assert pit.ambiguous_restatement_chronology_unknown is True

    def test_protocol_encodes_conflicting_values_unknown(self) -> None:
        protocol = verify_protocol_file(REPO_ROOT)
        pit = protocol.pit_availability
        assert pit.same_key_conflicting_values_unknown is True


# ---------------------------------------------------------------------------
# E10b binding verification
# ---------------------------------------------------------------------------


class TestE10bBinding:
    def test_e10b_engine_version_matches(self) -> None:
        from app.research.layer_two_financial_negative_list import (
            LAYER_TWO_FINANCIAL_NEGATIVE_LIST_ENGINE_VERSION,
        )

        assert BOUND_E10B_ENGINE_VERSION == LAYER_TWO_FINANCIAL_NEGATIVE_LIST_ENGINE_VERSION

    def test_e10b_module_sha_matches_disk(self) -> None:
        import hashlib

        e10b_path = REPO_ROOT / "src" / "app" / "research" / "layer_two_financial_negative_list.py"
        if not e10b_path.is_file():
            pytest.skip("E10b module not available")
        h = hashlib.sha256()
        with open(e10b_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        assert h.hexdigest() == BOUND_E10B_MODULE_SHA256
