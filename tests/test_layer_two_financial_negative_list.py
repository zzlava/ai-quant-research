"""Strict unit tests for layer-two PIT financial negative-list adjudicator (E10b).

Synthetic explicit evidences only — never opens market data or mutates frozen JSON.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.research.layer_two_financial_negative_list import (
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
    NON_STANDARD_AUDIT_RULE,
    REQUIRED_RULE_CODES,
    WARNING_RULE_CODES,
    LayerTwoFinancialNegativeEvidence,
    LayerTwoFinancialNegativeListReport,
    assert_report_self_hash,
    bind_two_layer_financial_negative_list_policy,
    compute_report_id,
    evaluate_layer_two_financial_negative_list,
    seal_layer_two_financial_negative_list_report,
    verify_layer_two_financial_negative_list_report,
    verify_layer_two_financial_negative_list_report_file,
    write_layer_two_financial_negative_list_report,
)
from app.research.two_layer_contract import load_two_layer_decision_draft

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_CONTRACT = REPO_ROOT / BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
SNAP_ID = "snap-e10b-synthetic"
DECISION_AT = datetime(2024, 6, 28, 16, 0, tzinfo=UTC)
AS_OF = date(2024, 6, 28)
SYMBOL = "000001.SZ"
REPORT_PERIOD = date(2023, 12, 31)
OBS_AS_OF = AS_OF
AVAILABLE_AT = datetime(2024, 6, 28, 15, 0, tzinfo=UTC)


def _evidence(
    rule_code: str,
    hit_state: str = "false",
    *,
    symbol: str = SYMBOL,
    observation_as_of: date = OBS_AS_OF,
    report_period: date = REPORT_PERIOD,
    available_at: datetime = AVAILABLE_AT,
    source: str = "synthetic-pit",
    evidence_id: str | None = None,
) -> LayerTwoFinancialNegativeEvidence:
    return LayerTwoFinancialNegativeEvidence.model_validate(
        {
            "symbol": symbol,
            "rule_code": rule_code,
            "hit_state": hit_state,
            "observation_as_of": observation_as_of,
            "report_period": report_period,
            "available_at": available_at,
            "source": source,
            "evidence_id": evidence_id or f"ev-{rule_code}",
        }
    )


def _full_known(
    *,
    non_standard: str = "false",
    warning_trues: set[str] | None = None,
) -> list[LayerTwoFinancialNegativeEvidence]:
    hits = warning_trues or set()
    rows = [_evidence(NON_STANDARD_AUDIT_RULE, non_standard)]
    for code in WARNING_RULE_CODES:
        rows.append(_evidence(code, "true" if code in hits else "false"))
    return rows


def _evaluate(
    evidences: list[LayerTwoFinancialNegativeEvidence],
    *,
    symbol: str = SYMBOL,
    decision_at: datetime = DECISION_AT,
    snapshot_id: str = SNAP_ID,
    repo_root: Path = REPO_ROOT,
):
    return evaluate_layer_two_financial_negative_list(
        symbol=symbol,
        decision_at=decision_at,
        data_snapshot_id=snapshot_id,
        evidences=evidences,
        repo_root=repo_root,
    )


def test_bind_contract_matches_frozen_id() -> None:
    contract_id, path, policy = bind_two_layer_financial_negative_list_policy(repo_root=REPO_ROOT)
    assert contract_id == BOUND_TWO_LAYER_DECISION_CONTRACT_ID
    assert path == BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
    assert policy.financial_negative_list.non_standard_audit_single_hit_excludes is True
    assert policy.financial_negative_list.exclusion_cannot_be_offset_by_alpha is True


def test_non_standard_audit_true_hard_excludes() -> None:
    report = _evaluate(_full_known(non_standard="true"))
    assert report.decision_status == "hard_excluded"
    assert report.target_multiplier == 0.0
    assert report.eligible_for_new_entry is False
    assert report.known_hit_codes == [NON_STANDARD_AUDIT_RULE]
    assert "non_standard_audit_hard_exclude" in report.reason_codes
    assert report.ready_for_scoring is False
    assert report.ready_for_portfolio_construction is False
    assert report.ready_for_trading is False


@pytest.mark.parametrize(
    ("warning_count", "status", "multiplier", "eligible", "reason"),
    [
        (0, "clean", 1.0, True, "clean_no_hits"),
        (1, "halved", 0.5, True, "warning_hits_eq_1_halve"),
        (2, "hard_excluded", 0.0, False, "warning_hits_ge_2_exclude"),
        (3, "hard_excluded", 0.0, False, "warning_hits_ge_2_exclude"),
        (4, "hard_excluded", 0.0, False, "warning_hits_ge_2_exclude"),
    ],
)
def test_warning_hit_counts(
    warning_count: int,
    status: str,
    multiplier: float,
    eligible: bool,
    reason: str,
) -> None:
    trues = set(WARNING_RULE_CODES[:warning_count])
    report = _evaluate(_full_known(non_standard="false", warning_trues=trues))
    assert report.decision_status == status
    assert report.target_multiplier == multiplier
    assert report.eligible_for_new_entry is eligible
    assert report.known_warning_hit_count == warning_count
    assert reason in report.reason_codes
    assert report.known_hit_codes == sorted(trues)
    assert report.unknown_codes == []


def test_missing_required_rule_is_insufficient_not_clean() -> None:
    evidences = _full_known()
    evidences = [row for row in evidences if row.rule_code != WARNING_RULE_CODES[0]]
    report = _evaluate(evidences)
    assert report.decision_status == "insufficient_evidence"
    assert report.target_multiplier is None
    assert report.eligible_for_new_entry is False
    assert WARNING_RULE_CODES[0] in report.unknown_codes
    assert "insufficient_evidence" in report.reason_codes
    assert report.known_warning_hit_count is None


def test_explicit_unknown_is_insufficient_and_not_a_miss() -> None:
    # Zero known warning hits + one explicit unknown → insufficient (not clean/1.0).
    evidences = _full_known(warning_trues=set())
    evidences = [
        row if row.rule_code != WARNING_RULE_CODES[0] else _evidence(WARNING_RULE_CODES[0], "unknown")
        for row in evidences
    ]
    report = _evaluate(evidences)
    assert report.decision_status == "insufficient_evidence"
    assert report.target_multiplier is None
    assert report.eligible_for_new_entry is False
    assert WARNING_RULE_CODES[0] in report.unknown_codes
    # Unknown must not contribute to known hits / miss counting.
    assert WARNING_RULE_CODES[0] not in report.known_hit_codes
    assert report.known_warning_hit_count is None


def test_non_standard_unknown_fails_closed() -> None:
    evidences = _full_known()
    evidences = [
        row if row.rule_code != NON_STANDARD_AUDIT_RULE else _evidence(NON_STANDARD_AUDIT_RULE, "unknown")
        for row in evidences
    ]
    report = _evaluate(evidences)
    assert report.decision_status == "insufficient_evidence"
    assert report.target_multiplier is None
    assert NON_STANDARD_AUDIT_RULE in report.unknown_codes


def test_audit_true_with_missing_rule_still_hard_excludes() -> None:
    evidences = [
        row
        for row in _full_known(non_standard="true", warning_trues={WARNING_RULE_CODES[0]})
        if row.rule_code != WARNING_RULE_CODES[1]
    ]
    report = _evaluate(evidences)
    assert report.decision_status == "hard_excluded"
    assert report.target_multiplier == 0.0
    assert report.eligible_for_new_entry is False
    assert "non_standard_audit_hard_exclude" in report.reason_codes
    assert WARNING_RULE_CODES[1] in report.unknown_codes
    assert report.known_warning_hit_count == 1
    assert NON_STANDARD_AUDIT_RULE in report.known_hit_codes
    assert WARNING_RULE_CODES[0] in report.known_hit_codes


def test_audit_true_with_unknown_rule_still_hard_excludes() -> None:
    evidences = [
        row if row.rule_code != WARNING_RULE_CODES[0] else _evidence(WARNING_RULE_CODES[0], "unknown")
        for row in _full_known(non_standard="true")
    ]
    report = _evaluate(evidences)
    assert report.decision_status == "hard_excluded"
    assert report.target_multiplier == 0.0
    assert report.eligible_for_new_entry is False
    assert WARNING_RULE_CODES[0] in report.unknown_codes
    assert report.known_warning_hit_count == 0
    assert report.known_hit_codes == [NON_STANDARD_AUDIT_RULE]


def test_two_known_warning_hits_with_missing_still_hard_excludes() -> None:
    evidences = [
        row
        for row in _full_known(
            non_standard="false",
            warning_trues={WARNING_RULE_CODES[0], WARNING_RULE_CODES[1]},
        )
        if row.rule_code != WARNING_RULE_CODES[2]
    ]
    report = _evaluate(evidences)
    assert report.decision_status == "hard_excluded"
    assert report.target_multiplier == 0.0
    assert report.eligible_for_new_entry is False
    assert "warning_hits_ge_2_exclude" in report.reason_codes
    assert WARNING_RULE_CODES[2] in report.unknown_codes
    assert report.known_warning_hit_count == 2
    assert report.known_hit_codes == sorted([WARNING_RULE_CODES[0], WARNING_RULE_CODES[1]])


def test_one_known_hit_plus_unknown_still_insufficient() -> None:
    evidences = [
        row if row.rule_code != WARNING_RULE_CODES[1] else _evidence(WARNING_RULE_CODES[1], "unknown")
        for row in _full_known(warning_trues={WARNING_RULE_CODES[0]})
    ]
    report = _evaluate(evidences)
    assert report.decision_status == "insufficient_evidence"
    assert report.target_multiplier is None
    assert report.eligible_for_new_entry is False
    assert WARNING_RULE_CODES[1] in report.unknown_codes
    assert WARNING_RULE_CODES[0] in report.known_hit_codes
    assert report.known_warning_hit_count is None
    assert "warning_hits_eq_1_halve" not in report.reason_codes


def test_zero_known_hits_plus_unknown_still_insufficient() -> None:
    evidences = [
        row if row.rule_code != WARNING_RULE_CODES[3] else _evidence(WARNING_RULE_CODES[3], "unknown")
        for row in _full_known(warning_trues=set())
    ]
    report = _evaluate(evidences)
    assert report.decision_status == "insufficient_evidence"
    assert report.target_multiplier is None
    assert report.eligible_for_new_entry is False
    assert WARNING_RULE_CODES[3] in report.unknown_codes
    assert report.known_hit_codes == []
    assert report.known_warning_hit_count is None
    assert "clean_no_hits" not in report.reason_codes


def test_late_available_at_raises() -> None:
    evidences = _full_known()
    evidences[0] = _evidence(
        evidences[0].rule_code,
        evidences[0].hit_state,
        available_at=DECISION_AT + timedelta(microseconds=1),
        evidence_id=evidences[0].evidence_id,
    )
    with pytest.raises(ValueError, match="available_at"):
        _evaluate(evidences)


def test_future_observation_as_of_raises() -> None:
    evidences = _full_known()
    evidences[1] = _evidence(
        evidences[1].rule_code,
        evidences[1].hit_state,
        observation_as_of=AS_OF + timedelta(days=1),
        evidence_id=evidences[1].evidence_id,
    )
    with pytest.raises(ValueError, match="observation_as_of"):
        _evaluate(evidences)


def test_future_report_period_raises() -> None:
    evidences = _full_known()
    evidences[2] = _evidence(
        evidences[2].rule_code,
        evidences[2].hit_state,
        report_period=AS_OF + timedelta(days=1),
        evidence_id=evidences[2].evidence_id,
    )
    with pytest.raises(ValueError, match="report_period"):
        _evaluate(evidences)


def test_duplicate_rule_conflict_raises() -> None:
    evidences = _full_known()
    evidences.append(_evidence(WARNING_RULE_CODES[0], "true", evidence_id="dup"))
    with pytest.raises(ValueError, match="duplicate conflicting evidence"):
        _evaluate(evidences)


def test_illegal_rule_code_rejected() -> None:
    with pytest.raises(ValidationError):
        LayerTwoFinancialNegativeEvidence.model_validate(
            {
                "symbol": SYMBOL,
                "rule_code": "pledge_ratio_over_threshold",
                "hit_state": "true",
                "observation_as_of": OBS_AS_OF,
                "report_period": REPORT_PERIOD,
                "available_at": AVAILABLE_AT,
                "source": "synthetic",
                "evidence_id": "bad-rule",
            }
        )


@pytest.mark.parametrize(
    "symbol",
    ["000001.sz", " 000001.SZ", "00001.SZ", "000001.SS", "430047.BJ"],
)
def test_symbol_alias_rejected(symbol: str) -> None:
    with pytest.raises(ValueError):
        _evaluate(_full_known(), symbol=symbol)


def test_evidence_symbol_mismatch_raises() -> None:
    evidences = _full_known()
    evidences[0] = _evidence(
        evidences[0].rule_code,
        evidences[0].hit_state,
        symbol="600000.SH",
        evidence_id=evidences[0].evidence_id,
    )
    with pytest.raises(ValueError, match="evidence symbol"):
        _evaluate(evidences, symbol=SYMBOL)


def test_illegal_hit_state_enum_rejected() -> None:
    with pytest.raises(ValidationError):
        LayerTwoFinancialNegativeEvidence.model_validate(
            {
                "symbol": SYMBOL,
                "rule_code": NON_STANDARD_AUDIT_RULE,
                "hit_state": "maybe",
                "observation_as_of": OBS_AS_OF,
                "report_period": REPORT_PERIOD,
                "available_at": AVAILABLE_AT,
                "source": "synthetic",
                "evidence_id": "bad-state",
            }
        )


def test_nan_inf_cannot_masquerade_as_hit_state() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            LayerTwoFinancialNegativeEvidence.model_validate(
                {
                    "symbol": SYMBOL,
                    "rule_code": NON_STANDARD_AUDIT_RULE,
                    "hit_state": bad,
                    "observation_as_of": OBS_AS_OF,
                    "report_period": REPORT_PERIOD,
                    "available_at": AVAILABLE_AT,
                    "source": "synthetic",
                    "evidence_id": "nan-state",
                }
            )


def test_tampered_disk_contract_rejected(tmp_path: Path) -> None:
    tampered_dir = tmp_path / "config" / "research"
    tampered_dir.mkdir(parents=True)
    tampered_path = tampered_dir / "two-layer-strategy-decision-draft-v1.json"
    payload = json.loads(COMMITTED_CONTRACT.read_text(encoding="utf-8"))
    payload["confirmed"]["note"] = "tampered contract content"
    tampered_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    ledger_src = REPO_ROOT / payload["research_trial_ledger_path"]
    ledger_dest = tmp_path / payload["research_trial_ledger_path"]
    ledger_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ledger_src, ledger_dest)

    with pytest.raises(ValueError, match="contract_id"):
        bind_two_layer_financial_negative_list_policy(repo_root=tmp_path, contract_path=tampered_path)


def test_tampered_report_field_resealed_still_detected() -> None:
    report = _evaluate(_full_known())
    tampered = report.model_copy(
        update={
            "decision_status": "clean",
            "target_multiplier": 1.0,
            "eligible_for_new_entry": True,
            "reason_codes": ["clean_no_hits"],
            "known_hit_codes": [],
            "known_warning_hit_count": 0,
            "report_id": None,
        }
    )
    # Force a semantic drift that outer reseal cannot hide: drop a required evidence.
    bad_evidences = [row for row in report.evidences if row.rule_code != WARNING_RULE_CODES[0]]
    drifted = tampered.model_copy(
        update={
            "evidences": bad_evidences,
            "input_evidence_hashes": report.input_evidence_hashes[: len(bad_evidences)],
            "decision_status": "clean",
            "target_multiplier": 1.0,
            "eligible_for_new_entry": True,
            "unknown_codes": [],
            "reason_codes": ["clean_no_hits"],
            "known_warning_hit_count": 0,
            "report_id": None,
        }
    )
    resealed = seal_layer_two_financial_negative_list_report(drifted)
    with pytest.raises(ValueError, match="does not recompute"):
        verify_layer_two_financial_negative_list_report(resealed, repo_root=REPO_ROOT)


def test_derived_status_tamper_reseal_rejected() -> None:
    report = _evaluate(_full_known(warning_trues={WARNING_RULE_CODES[0], WARNING_RULE_CODES[1]}))
    assert report.target_multiplier == 0.0
    tampered = report.model_copy(
        update={
            "decision_status": "clean",
            "target_multiplier": 1.0,
            "eligible_for_new_entry": True,
            "reason_codes": ["clean_no_hits"],
            "known_hit_codes": [],
            "known_warning_hit_count": 0,
            "report_id": None,
        }
    )
    resealed = seal_layer_two_financial_negative_list_report(tampered)
    with pytest.raises(ValueError, match="does not recompute"):
        verify_layer_two_financial_negative_list_report(resealed, repo_root=REPO_ROOT)


def test_alpha_and_ownership_fields_cannot_inject() -> None:
    payload = json.loads(_evidence(NON_STANDARD_AUDIT_RULE).model_dump_json())
    for forbidden in (
        "alpha_score",
        "ownership_proxy",
        "holder_count",
        "pledge_ratio",
        "unlock_ratio",
        "event_candidate_signal",
    ):
        bad = dict(payload)
        bad[forbidden] = 1.0
        with pytest.raises(ValidationError):
            LayerTwoFinancialNegativeEvidence.model_validate(bad)

    report_payload = json.loads(_evaluate(_full_known()).model_dump_json())
    for forbidden in ("alpha_score", "ownership_proxy", "pledge_ratio"):
        bad = dict(report_payload)
        bad[forbidden] = 0.9
        bad.pop("report_id", None)
        with pytest.raises(ValidationError):
            LayerTwoFinancialNegativeListReport.model_validate(bad)


def test_decision_date_timezone_boundary() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    decision_shanghai = datetime(2024, 6, 28, 23, 59, tzinfo=shanghai)
    report = _evaluate(_full_known(), decision_at=decision_shanghai)
    assert report.as_of == date(2024, 6, 28)

    # 2024-06-28 23:59 Asia/Shanghai == 2024-06-28 15:59 UTC
    assert decision_shanghai.astimezone(UTC).date() == date(2024, 6, 28)

    late_utc = datetime(2024, 6, 28, 16, 1, tzinfo=UTC)
    ok = _evaluate(_full_known(), decision_at=late_utc)
    assert ok.as_of == date(2024, 6, 28)

    evidences = _full_known()
    evidences[0] = _evidence(
        evidences[0].rule_code,
        evidences[0].hit_state,
        available_at=datetime(2024, 6, 29, 0, 0, 1, tzinfo=shanghai),
        evidence_id=evidences[0].evidence_id,
    )
    with pytest.raises(ValueError, match="available_at"):
        _evaluate(evidences, decision_at=decision_shanghai)


def test_naive_decision_at_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _evaluate(_full_known(), decision_at=datetime(2024, 6, 28, 16, 0))


def test_input_order_does_not_affect_report_id() -> None:
    base = _full_known(warning_trues={WARNING_RULE_CODES[0]})
    reversed_rows = list(reversed(base))
    first = _evaluate(base)
    second = _evaluate(reversed_rows)
    assert first.report_id == second.report_id
    assert first.evidences == second.evidences
    assert first.input_evidence_hashes == second.input_evidence_hashes


def test_output_self_hash_and_roundtrip(tmp_path: Path) -> None:
    report = _evaluate(_full_known())
    assert report.report_id == compute_report_id(report)
    assert_report_self_hash(report)
    out = tmp_path / "report.json"
    write_layer_two_financial_negative_list_report(report, out)
    loaded = verify_layer_two_financial_negative_list_report_file(out, repo_root=REPO_ROOT)
    assert loaded.report_id == report.report_id


def test_stale_hash_rejected() -> None:
    report = seal_layer_two_financial_negative_list_report(_evaluate(_full_known()))
    tampered = report.model_copy(update={"report_id": "0" * 64})
    with pytest.raises(ValueError, match="report_id"):
        assert_report_self_hash(tampered)


def test_wrong_report_contract_binding_rejected() -> None:
    report = _evaluate(_full_known())
    bad = report.model_copy(update={"two_layer_decision_contract_id": "f" * 64, "report_id": None})
    resealed = seal_layer_two_financial_negative_list_report(bad)
    with pytest.raises(ValueError, match="contract_id"):
        verify_layer_two_financial_negative_list_report(resealed, repo_root=REPO_ROOT)


def test_ready_flags_cannot_be_true() -> None:
    payload = json.loads(_evaluate(_full_known()).model_dump_json())
    for flag in ("ready_for_scoring", "ready_for_portfolio_construction", "ready_for_trading"):
        bad = dict(payload)
        bad[flag] = True
        bad.pop("report_id", None)
        with pytest.raises(ValidationError):
            LayerTwoFinancialNegativeListReport.model_validate(bad)


def test_registry_is_closed_to_four_warnings_plus_audit() -> None:
    assert REQUIRED_RULE_CODES == (NON_STANDARD_AUDIT_RULE, *WARNING_RULE_CODES)
    assert len(WARNING_RULE_CODES) == 4
    assert len(REQUIRED_RULE_CODES) == 5


def test_disk_contract_loads_without_mutation() -> None:
    draft = load_two_layer_decision_draft(COMMITTED_CONTRACT)
    assert draft.contract_id == BOUND_TWO_LAYER_DECISION_CONTRACT_ID


def test_available_at_equal_decision_at_allowed() -> None:
    evidences = [
        _evidence(code, "false", available_at=DECISION_AT, evidence_id=f"eq-{code}") for code in REQUIRED_RULE_CODES
    ]
    report = _evaluate(evidences)
    assert report.decision_status == "clean"
    assert report.target_multiplier == 1.0


def test_observation_as_of_equal_decision_date_allowed() -> None:
    evidences = [
        _evidence(code, "false", observation_as_of=AS_OF, evidence_id=f"asof-{code}") for code in REQUIRED_RULE_CODES
    ]
    report = _evaluate(evidences)
    assert report.as_of == AS_OF
    assert report.decision_status == "clean"
