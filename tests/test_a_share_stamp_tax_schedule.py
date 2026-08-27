"""Attack-oriented tests for A-share stamp-tax factual schedule (E10f-0)."""

from __future__ import annotations

import ast
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.research.a_share_stamp_tax_schedule import (
    BOUND_A_SHARE_STAMP_TAX_SCHEDULE_PATH,
    BOUND_EVIDENCE_SOURCE_COUNT,
    DEFAULT_A_SHARE_STAMP_TAX_SCHEDULE_PATH,
    EVIDENCE_ACCESSED_AT,
    EXPECTED_CURRENT_CONTRACT_ID,
    VERIFIED_THROUGH,
    AShareStampTaxScheduleContract,
    AShareStampTaxScheduleVerificationResult,
    DateWindow,
    StampTaxEvidenceSource,
    StampTaxReaffirmationMilestone,
    StampTaxScheduleBand,
    assert_contract_self_hash,
    build_a_share_stamp_tax_schedule_v1,
    compute_contract_id,
    load_a_share_stamp_tax_schedule,
    seal_a_share_stamp_tax_schedule,
    stamp_tax_amount,
    stamp_tax_rate_for,
    verify_a_share_stamp_tax_schedule,
    verify_a_share_stamp_tax_schedule_file,
    write_a_share_stamp_tax_schedule,
)
from tests.helpers import PROJECT_ROOT

REPO_ROOT = PROJECT_ROOT
MODULE_PATH = REPO_ROOT / "src/app/research/a_share_stamp_tax_schedule.py"
COMMITTED_PATH = REPO_ROOT / BOUND_A_SHARE_STAMP_TAX_SCHEDULE_PATH


def _temp_repo_with_schedule(tmp_path: Path, *, mutate: dict | None = None, raw: str | None = None) -> Path:
    dest = tmp_path / BOUND_A_SHARE_STAMP_TAX_SCHEDULE_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        dest.write_text(raw, encoding="utf-8")
        return tmp_path
    payload = json.loads(COMMITTED_PATH.read_text(encoding="utf-8"))
    if mutate:
        payload.update(mutate)
    dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return tmp_path


def test_committed_file_verifies_and_rates_on_boundaries() -> None:
    contract, result = verify_a_share_stamp_tax_schedule_file(repo_root=REPO_ROOT)
    assert contract.contract_id == EXPECTED_CURRENT_CONTRACT_ID
    assert contract.verified_through == VERIFIED_THROUGH == date(2026, 8, 26)
    assert result.structural_ok is True
    assert result.disk_binding_ok is True
    assert result.ready_for_exit_diagnostic is True
    assert result.ready_for_scoring is False
    assert result.ready_for_backtest is False
    assert result.ready_for_trading is False
    assert result.ready_for_orders is False
    assert result.auto_apply is False
    assert contract.factual_cost_contract_only is True
    assert contract.existing_tranche_protocol_blocker_not_modified is True
    assert contract.legal_open_ended_band_does_not_authorize_extrapolation_past_verified_through is True
    assert contract.evidence_accessed_at_is_offline_review_timestamp is True
    assert "ready_for_exit_diagnostic" not in contract.model_dump()
    assert len(contract.evidence_sources) == BOUND_EVIDENCE_SOURCE_COUNT
    assert all(source.accessed_at == EVIDENCE_ACCESSED_AT for source in contract.evidence_sources)

    assert stamp_tax_rate_for(date(2022, 1, 1), "sell") == 0.001
    assert stamp_tax_rate_for(date(2023, 8, 27), "sell") == 0.001
    assert stamp_tax_rate_for(date(2023, 8, 28), "sell") == 0.0005
    assert stamp_tax_rate_for(date(2024, 12, 31), "sell") == 0.0005
    assert stamp_tax_rate_for(date(2026, 8, 26), "sell") == 0.0005
    assert stamp_tax_rate_for(date(2022, 1, 1), "buy") == 0.0
    assert stamp_tax_rate_for(date(2023, 8, 28), "buy") == 0.0
    assert stamp_tax_amount(transaction_amount=100_000.0, trade_date=date(2023, 8, 27), side="sell") == 100.0
    assert stamp_tax_amount(transaction_amount=100_000.0, trade_date=date(2023, 8, 28), side="sell") == 50.0
    assert stamp_tax_amount(transaction_amount=100_000.0, trade_date=date(2024, 12, 31), side="buy") == 0.0


def test_verified_through_fail_closed_no_extrapolation() -> None:
    with pytest.raises(ValueError, match="verified_through|extrapolation"):
        stamp_tax_rate_for(date(2026, 8, 27), "sell")
    with pytest.raises(ValueError, match="verified_through|extrapolation"):
        stamp_tax_rate_for(date(2035, 1, 1), "sell")
    with pytest.raises(ValueError, match="verified_through|extrapolation"):
        stamp_tax_amount(transaction_amount=1.0, trade_date=date(2026, 8, 27), side="buy")
    with pytest.raises(ValueError, match="verified_through|extrapolation"):
        stamp_tax_amount(transaction_amount=1.0, trade_date=date(2035, 1, 1), side="sell")


def test_structural_verifier_does_not_claim_disk_binding() -> None:
    contract = build_a_share_stamp_tax_schedule_v1()
    result = verify_a_share_stamp_tax_schedule(contract)
    assert result.structural_ok is True
    assert result.disk_binding_ok is False
    assert result.ready_for_exit_diagnostic is False


def test_precoverage_and_invalid_side_fail_closed() -> None:
    with pytest.raises(ValueError, match="precedes schedule coverage|2008-09-19"):
        stamp_tax_rate_for(date(2008, 9, 18), "sell")
    with pytest.raises(ValueError, match="side"):
        stamp_tax_rate_for(date(2022, 1, 1), "long")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="precedes schedule coverage|2008-09-19"):
        stamp_tax_amount(transaction_amount=1.0, trade_date=date(2008, 9, 18), side="buy")


def test_law_reaffirmation_is_not_a_rate_band() -> None:
    contract = build_a_share_stamp_tax_schedule_v1()
    assert len(contract.schedule_bands) == 2
    assert contract.schedule_bands[1].open_ended is True
    assert contract.reaffirmation_milestones[0].milestone_date == date(2022, 7, 1)
    assert stamp_tax_rate_for(date(2022, 6, 30), "sell") == 0.001
    assert stamp_tax_rate_for(date(2022, 7, 1), "sell") == 0.001


def test_gap_overlap_open_end_band_errors() -> None:
    base = build_a_share_stamp_tax_schedule_v1().model_dump(mode="json")
    base.pop("contract_id", None)
    gapped = json.loads(json.dumps(base))
    gapped["schedule_bands"][0]["effective_to"] = "2023-08-26"
    with pytest.raises(ValidationError, match="contiguous|gap|overlap"):
        AShareStampTaxScheduleContract.model_validate(gapped)

    overlapped = json.loads(json.dumps(base))
    overlapped["schedule_bands"][1]["effective_from"] = "2023-08-27"
    with pytest.raises(ValidationError, match="contiguous|gap|overlap"):
        AShareStampTaxScheduleContract.model_validate(overlapped)

    with pytest.raises(ValidationError, match="open-ended"):
        StampTaxScheduleBand.model_validate(
            {
                "effective_from": "2023-08-28",
                "effective_to": "2024-12-31",
                "seller_rate": 0.0005,
                "buyer_rate": 0.0,
                "open_ended": True,
            }
        )

    two_open = json.loads(json.dumps(base))
    two_open["schedule_bands"][0]["effective_to"] = None
    two_open["schedule_bands"][0]["open_ended"] = True
    with pytest.raises(ValidationError, match="open-ended"):
        AShareStampTaxScheduleContract.model_validate(two_open)


def test_tamper_verified_through_and_declared_window_overreach() -> None:
    payload = build_a_share_stamp_tax_schedule_v1().model_dump(mode="json")
    payload.pop("contract_id", None)

    bad_through = json.loads(json.dumps(payload))
    bad_through["verified_through"] = "2026-08-27"
    with pytest.raises(ValidationError, match="verified_through"):
        AShareStampTaxScheduleContract.model_validate(bad_through)

    overreach = json.loads(json.dumps(payload))
    overreach["declared_window"] = {"start": "2022-01-01", "end": "2027-01-01"}
    with pytest.raises(ValidationError, match="declared_window|verified_through"):
        AShareStampTaxScheduleContract.model_validate(overreach)


def test_rate_helpers_reject_self_hash_valid_resealed_rate_substitution() -> None:
    """P1: self-hash alone must not let forged seller_rate through rate helpers."""
    payload = build_a_share_stamp_tax_schedule_v1().model_dump(mode="json")
    rate_tamper = json.loads(json.dumps(payload))
    rate_tamper["schedule_bands"][1]["seller_rate"] = 0.0004
    rate_tamper.pop("contract_id", None)
    resealed = seal_a_share_stamp_tax_schedule(AShareStampTaxScheduleContract.model_validate(rate_tamper))
    # Self-hash is intentionally valid after reseal; rate helpers must still fail-closed.
    assert resealed.contract_id == compute_contract_id(resealed)
    assert_contract_self_hash(resealed)
    with pytest.raises(ValueError, match="canonical payload|factory"):
        verify_a_share_stamp_tax_schedule(resealed)
    with pytest.raises(ValueError, match="canonical payload|factory"):
        stamp_tax_rate_for(date(2024, 6, 1), "sell", contract=resealed)
    with pytest.raises(ValueError, match="canonical payload|factory"):
        stamp_tax_amount(
            transaction_amount=100_000.0,
            trade_date=date(2024, 6, 1),
            side="sell",
            contract=resealed,
        )


def test_rate_helpers_reject_self_hash_valid_notes_payload_tamper() -> None:
    payload = build_a_share_stamp_tax_schedule_v1().model_dump(mode="json")
    notes_tamper = json.loads(json.dumps(payload))
    notes_tamper["evidence_sources"][0]["notes"] = "tampered offline notes"
    notes_tamper.pop("contract_id", None)
    resealed = seal_a_share_stamp_tax_schedule(AShareStampTaxScheduleContract.model_validate(notes_tamper))
    assert_contract_self_hash(resealed)
    with pytest.raises(ValueError, match="canonical payload|factory"):
        stamp_tax_rate_for(date(2022, 1, 1), "buy", contract=resealed)
    with pytest.raises(ValueError, match="canonical payload|factory"):
        stamp_tax_amount(
            transaction_amount=1.0,
            trade_date=date(2022, 1, 1),
            side="buy",
            contract=resealed,
        )


def test_outer_reseal_rate_source_url_role_time_hash_rejected() -> None:
    contract = build_a_share_stamp_tax_schedule_v1()
    payload = contract.model_dump(mode="json")

    rate_tamper = json.loads(json.dumps(payload))
    rate_tamper["schedule_bands"][1]["seller_rate"] = 0.0004
    rate_tamper.pop("contract_id", None)
    resealed = seal_a_share_stamp_tax_schedule(AShareStampTaxScheduleContract.model_validate(rate_tamper))
    with pytest.raises(ValueError, match="canonical payload|factory|sealed evidence"):
        verify_a_share_stamp_tax_schedule(resealed)

    for field, value in (
        ("url", "https://example.invalid/stamp-tax"),
        ("document_identifier", "tampered-doc-id"),
        ("evidence_role", "validity_mirror"),
    ):
        bad = json.loads(json.dumps(payload))
        if field == "evidence_role":
            bad["evidence_sources"][0][field] = value
        else:
            bad["evidence_sources"][0][field] = value
        bad.pop("contract_id", None)
        with pytest.raises(ValidationError, match="sealed evidence|evidence_role|url|document"):
            AShareStampTaxScheduleContract.model_validate(bad)

    # Role swap that keeps required role set but breaks sealed ordering/identity.
    role_swap = json.loads(json.dumps(payload))
    role_swap["evidence_sources"][0]["evidence_role"] = "halves_seller_rate"
    role_swap["evidence_sources"][2]["evidence_role"] = "establishes_seller_only_levy"
    role_swap.pop("contract_id", None)
    with pytest.raises(ValidationError, match="sealed evidence|evidence_role"):
        AShareStampTaxScheduleContract.model_validate(role_swap)

    time_tamper = json.loads(json.dumps(payload))
    time_tamper["evidence_sources"][0]["accessed_at"] = "2026-08-26T12:00:00Z"
    time_tamper.pop("contract_id", None)
    with pytest.raises(ValidationError, match="accessed_at|offline review"):
        AShareStampTaxScheduleContract.model_validate(time_tamper)

    mixed_time = json.loads(json.dumps(payload))
    mixed_time["evidence_sources"][1]["accessed_at"] = "2026-08-26T06:54:53Z"
    mixed_time.pop("contract_id", None)
    with pytest.raises(ValidationError, match="accessed_at|identical|offline review"):
        AShareStampTaxScheduleContract.model_validate(mixed_time)

    date_tamper = json.loads(json.dumps(payload))
    date_tamper["evidence_sources"][2]["published_or_effective_date"] = "2023-08-27"
    date_tamper.pop("contract_id", None)
    with pytest.raises(ValidationError, match="sealed|published_or_effective_date"):
        AShareStampTaxScheduleContract.model_validate(date_tamper)

    hash_tamper = contract.model_copy(update={"contract_id": "ab" * 32})
    with pytest.raises(ValueError, match="contract_id"):
        verify_a_share_stamp_tax_schedule(hash_tamper)


def test_bool_nan_inf_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        StampTaxScheduleBand.model_validate(
            {
                "effective_from": "2008-09-19",
                "effective_to": "2023-08-27",
                "seller_rate": True,
                "buyer_rate": 0.0,
                "open_ended": False,
            }
        )
    with pytest.raises(ValidationError):
        StampTaxScheduleBand.model_validate(
            {
                "effective_from": "2008-09-19",
                "effective_to": "2023-08-27",
                "seller_rate": float("nan"),
                "buyer_rate": 0.0,
                "open_ended": False,
            }
        )
    with pytest.raises(ValidationError):
        StampTaxScheduleBand.model_validate(
            {
                "effective_from": "2008-09-19",
                "effective_to": "2023-08-27",
                "seller_rate": float("inf"),
                "buyer_rate": 0.0,
                "open_ended": False,
            }
        )
    with pytest.raises(ValidationError):
        StampTaxScheduleBand.model_validate(
            {
                "effective_from": "2008-09-19",
                "effective_to": "2023-08-27",
                "seller_rate": -0.001,
                "buyer_rate": 0.0,
                "open_ended": False,
            }
        )
    with pytest.raises(ValidationError):
        StampTaxEvidenceSource.model_validate(
            {
                "source_id": "x",
                "title": "t",
                "url": "http://example.com",
                "document_identifier": "d",
                "evidence_role": "establishes_seller_only_levy",
                "published_or_effective_date": "2008-09-19",
                "accessed_at": "2026-08-26T06:54:52",
                "notes": "naive datetime rejected",
            }
        )
    with pytest.raises(ValidationError):
        AShareStampTaxScheduleVerificationResult.model_validate(
            {
                "contract_id": "ab" * 32,
                "structural_ok": 1,
            }
        )
    with pytest.raises(ValueError, match="real number|finite|>= 0"):
        stamp_tax_amount(transaction_amount=float("nan"), trade_date=date(2022, 1, 1), side="sell")
    with pytest.raises(ValueError, match="real number|finite|>= 0"):
        stamp_tax_amount(transaction_amount=-1.0, trade_date=date(2022, 1, 1), side="sell")
    with pytest.raises(ValueError, match="real number|bool"):
        stamp_tax_amount(transaction_amount=True, trade_date=date(2022, 1, 1), side="sell")  # type: ignore[arg-type]


def test_missing_tampered_path_and_contract_id_via_temp_repo(tmp_path: Path) -> None:
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    (empty / "config/research").mkdir(parents=True)
    with pytest.raises(ValueError, match="missing"):
        verify_a_share_stamp_tax_schedule_file(repo_root=empty)

    good = _temp_repo_with_schedule(tmp_path / "good")
    shutil.copy2(COMMITTED_PATH, good / BOUND_A_SHARE_STAMP_TAX_SCHEDULE_PATH)
    contract, result = verify_a_share_stamp_tax_schedule_file(repo_root=good)
    assert result.disk_binding_ok is True
    assert result.ready_for_exit_diagnostic is True
    assert contract.contract_id == EXPECTED_CURRENT_CONTRACT_ID

    tampered = _temp_repo_with_schedule(
        tmp_path / "tampered",
        mutate={"confirmation_as_of": "2026-08-25"},
    )
    with pytest.raises(ValueError):
        verify_a_share_stamp_tax_schedule_file(repo_root=tampered)

    wrong_path = tmp_path / "wrong-path-repo"
    wrong_path.mkdir()
    alt = wrong_path / "config/research/other-stamp.json"
    alt.parent.mkdir(parents=True)
    shutil.copy2(COMMITTED_PATH, alt)
    with pytest.raises(ValueError, match="fixed default repo path|missing"):
        verify_a_share_stamp_tax_schedule_file(repo_root=wrong_path, contract_path=alt)

    id_mismatch = tmp_path / "id-mismatch"
    id_mismatch.mkdir()
    dest = id_mismatch / BOUND_A_SHARE_STAMP_TAX_SCHEDULE_PATH
    dest.parent.mkdir(parents=True)
    payload = json.loads(COMMITTED_PATH.read_text(encoding="utf-8"))
    payload["contract_id"] = "ab" * 32
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="contract_id"):
        verify_a_share_stamp_tax_schedule_file(repo_root=id_mismatch)


def test_ready_for_exit_diagnostic_only_on_file_result() -> None:
    with pytest.raises(ValidationError):
        AShareStampTaxScheduleVerificationResult.model_validate(
            {
                "contract_id": EXPECTED_CURRENT_CONTRACT_ID,
                "structural_ok": True,
                "disk_binding_ok": False,
                "ready_for_exit_diagnostic": True,
            }
        )


def test_load_write_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "stamp.json"
    sealed = write_a_share_stamp_tax_schedule(path, build_a_share_stamp_tax_schedule_v1())
    loaded = load_a_share_stamp_tax_schedule(path)
    assert loaded.contract_id == sealed.contract_id == EXPECTED_CURRENT_CONTRACT_ID
    assert loaded.verified_through == VERIFIED_THROUGH


def test_no_production_imports() -> None:
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
        "app.api",
        "app.cli",
        "app.persistence",
        "app.strategies",
        "app.backtest",
        "app.pipeline",
    )
    for module in imported:
        assert not any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_prefixes)
    assert "ScoringEngine" not in source
    assert "BacktestEngine" not in source
    assert "StrategyConfig" not in source
    assert "urllib" not in imported
    assert "requests" not in imported
    assert str(DEFAULT_A_SHARE_STAMP_TAX_SCHEDULE_PATH) == BOUND_A_SHARE_STAMP_TAX_SCHEDULE_PATH


def test_declared_window_helpers_present() -> None:
    window = DateWindow(start=date(2022, 1, 1), end=date(2024, 12, 31))
    assert window.start <= window.end
    with pytest.raises(ValidationError):
        DateWindow(start=date(2024, 1, 1), end=date(2022, 1, 1))
    milestone = StampTaxReaffirmationMilestone(
        milestone_id="x",
        milestone_date=date(2022, 7, 1),
        evidence_source_id="sta_stamp_tax_law_2022",
        notes="reaffirmation only",
    )
    assert milestone.does_not_create_new_rate_band is True
    source = StampTaxEvidenceSource(
        source_id="mof_2008_unilateral_levy",
        title="t",
        url="http://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/200809/t20080919_76432.htm",
        document_identifier="d",
        evidence_role="establishes_seller_only_levy",
        published_or_effective_date=date(2008, 9, 19),
        accessed_at=EVIDENCE_ACCESSED_AT,
        notes="n",
    )
    assert source.accessed_at == datetime(2026, 8, 26, 6, 54, 52, tzinfo=UTC)
