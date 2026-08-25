from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.research.portfolio_oos_freeze import (
    DEFAULT_PORTFOLIO_OOS_FREEZE_PATH,
    DESCRIPTIVE_ENDPOINTS,
    EVALUABILITY_GATES,
    FROZEN_CONFIG_HASH,
    FROZEN_DEVELOPMENT_DATA_SNAPSHOT_ID,
    FROZEN_DEVELOPMENT_FUNDAMENTAL_DIR,
    FROZEN_DEVELOPMENT_MARKET_CALENDAR_TABLE_HASH,
    FROZEN_DEVELOPMENT_MARKET_DIR,
    FROZEN_OOS_FUNDAMENTAL_DIR,
    FROZEN_OOS_MARKET_CALENDAR_TABLE_HASH,
    FROZEN_OOS_MARKET_DIR,
    FROZEN_OOS_MARKET_SNAPSHOT_ID,
    FROZEN_ROBUSTNESS_REPORT_PATH,
    FROZEN_ROBUSTNESS_REPORT_SHA256,
    FROZEN_SELECTION_REPORT_PATH,
    FROZEN_SELECTION_REPORT_SHA256,
    FROZEN_STRATEGY_CONFIG_ID,
    FROZEN_STRATEGY_PATH,
    HARD_RISK_GATES,
    PORTFOLIO_OOS_FREEZE_VERSION,
    PRIMARY_OOS_ENDPOINT,
    assert_committed_portfolio_oos_freeze_bindings,
    build_committed_portfolio_oos_freeze,
    build_copy_with_id,
    compute_calendar_equivalence_proof,
    load_verified_portfolio_oos_freeze,
    verify_portfolio_oos_freeze,
    write_portfolio_oos_freeze,
)
from app.storage.hashing import hash_table
from tests.helpers import PROJECT_ROOT

COMMITTED_FREEZE = PROJECT_ROOT / DEFAULT_PORTFOLIO_OOS_FREEZE_PATH
CALENDAR_FIXTURE = PROJECT_ROOT / "tests/fixtures/portfolio_oos_freeze_calendars.json"
REAL_STRATEGY = PROJECT_ROOT / FROZEN_STRATEGY_PATH
COMMITTED_FREEZE_ID = "d60f3a5e22044cb2a5793c382153fef9c5dd0caf7330f0023cc27df70354fee1"


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _calendar_from_fixture() -> tuple[list[date], list[date]]:
    payload = json.loads(CALENDAR_FIXTURE.read_text(encoding="utf-8"))
    base = date.fromisoformat(payload["base"])
    dev = [base + timedelta(days=int(offset)) for offset in payload["dev"]]
    oos = [base + timedelta(days=int(offset)) for offset in payload["oos"]]
    return dev, oos


def _write_calendar(path: Path, days: list[date]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame({"date": days}).with_columns(pl.col("date").cast(pl.Date))
    frame.write_parquet(path)
    return hash_table(frame, "calendar")


def _write_json(path: Path, payload: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _sha256(path)


def _copy_committed_json(relative: str, root: Path) -> None:
    source = PROJECT_ROOT / relative
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _write_freeze_project(root: Path) -> Path:
    """Build a self-contained project tree under tmp_path for offline verify."""
    assert REAL_STRATEGY.is_file()
    strategy_dest = root / FROZEN_STRATEGY_PATH
    strategy_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REAL_STRATEGY, strategy_dest)

    _copy_committed_json(FROZEN_SELECTION_REPORT_PATH, root)
    _copy_committed_json(FROZEN_ROBUSTNESS_REPORT_PATH, root)
    assert _sha256(root / FROZEN_SELECTION_REPORT_PATH) == FROZEN_SELECTION_REPORT_SHA256
    assert _sha256(root / FROZEN_ROBUSTNESS_REPORT_PATH) == FROZEN_ROBUSTNESS_REPORT_SHA256

    for relative in (
        FROZEN_DEVELOPMENT_MARKET_DIR,
        FROZEN_DEVELOPMENT_FUNDAMENTAL_DIR,
        FROZEN_OOS_MARKET_DIR,
        FROZEN_OOS_FUNDAMENTAL_DIR,
    ):
        _copy_committed_json(f"{relative}/manifest.json", root)

    dev_days, oos_days = _calendar_from_fixture()
    dev_cal_hash = _write_calendar(root / FROZEN_DEVELOPMENT_MARKET_DIR / "calendar.parquet", dev_days)
    oos_cal_hash = _write_calendar(root / FROZEN_OOS_MARKET_DIR / "calendar.parquet", oos_days)
    assert dev_cal_hash == FROZEN_DEVELOPMENT_MARKET_CALENDAR_TABLE_HASH
    assert oos_cal_hash == FROZEN_OOS_MARKET_CALENDAR_TABLE_HASH

    freeze_path = root / DEFAULT_PORTFOLIO_OOS_FREEZE_PATH
    write_portfolio_oos_freeze(freeze_path, build_committed_portfolio_oos_freeze())
    return freeze_path


def test_calendar_equivalence_proof_matches_frozen_protocol() -> None:
    dev_days, oos_days = _calendar_from_fixture()
    proof = compute_calendar_equivalence_proof(dev_days, oos_days)
    assert proof.overlap_trading_days == 61
    assert proof.runtime_equivalent_anchor == date(2024, 10, 29)
    assert proof.first_2025_plus_signal == date(2025, 1, 22)
    assert proof.last_complete_signal == date(2026, 7, 22)
    assert proof.last_scheduled_exit == date(2026, 8, 19)


def test_verify_accepts_matching_freeze_and_rejects_tampered_id(tmp_path: Path) -> None:
    freeze_path = _write_freeze_project(tmp_path)
    loaded = verify_portfolio_oos_freeze(freeze_path=freeze_path, project_root=tmp_path)
    assert loaded.freeze_version == PORTFOLIO_OOS_FREEZE_VERSION
    assert loaded.bound_strategy.strategy_config_id == FROZEN_STRATEGY_CONFIG_ID
    assert loaded.ready_for_scoring is False
    assert loaded.ready_for_trading is False
    assert loaded.authorized is False
    assert loaded.auto_deploy is False
    assert loaded.one_shot_required is True

    payload = json.loads(freeze_path.read_text(encoding="utf-8"))
    payload["research_boundary"] = "tampered"
    freeze_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="freeze ID does not match"):
        load_verified_portfolio_oos_freeze(freeze_path)


def test_verify_rejects_strategy_hash_mismatch(tmp_path: Path) -> None:
    freeze_path = _write_freeze_project(tmp_path)
    strategy_path = tmp_path / FROZEN_STRATEGY_PATH
    strategy_path.write_text(strategy_path.read_text(encoding="utf-8") + "\n# tamper\n", encoding="utf-8")
    with pytest.raises(ValueError, match="strategy file SHA-256"):
        verify_portfolio_oos_freeze(freeze_path=freeze_path, project_root=tmp_path)


def test_verify_rejects_selection_hash_and_config_mismatch(tmp_path: Path) -> None:
    freeze_path = _write_freeze_project(tmp_path)
    selection_path = tmp_path / FROZEN_SELECTION_REPORT_PATH
    _write_json(
        selection_path,
        {
            "selected_candidate_id": "p10_h20",
            "selected_config_hash": "deadbeefdeadbeef",
            "data_snapshot_id": FROZEN_DEVELOPMENT_DATA_SNAPSHOT_ID,
        },
    )
    with pytest.raises(ValueError, match="selection report SHA-256"):
        verify_portfolio_oos_freeze(freeze_path=freeze_path, project_root=tmp_path)

    contract = load_verified_portfolio_oos_freeze(freeze_path)
    resealed = build_copy_with_id(
        contract.model_copy(
            update={
                "bound_selection": contract.bound_selection.model_copy(
                    update={"selected_config_hash": "deadbeefdeadbeef"}
                )
            }
        )
    )
    write_portfolio_oos_freeze(freeze_path, resealed)
    with pytest.raises(ValueError, match="selected_config_hash must match strategy"):
        load_verified_portfolio_oos_freeze(freeze_path)


def test_verify_rejects_resealed_report_hash_drift(tmp_path: Path) -> None:
    freeze_path = _write_freeze_project(tmp_path)
    contract = load_verified_portfolio_oos_freeze(freeze_path)

    selection_path = tmp_path / FROZEN_SELECTION_REPORT_PATH
    drifted_selection_sha = _write_json(
        selection_path,
        {
            "selected_candidate_id": "p10_h20",
            "selected_config_hash": FROZEN_CONFIG_HASH,
            "data_snapshot_id": FROZEN_DEVELOPMENT_DATA_SNAPSHOT_ID,
            "note": "resealed-drift",
        },
    )
    assert drifted_selection_sha != FROZEN_SELECTION_REPORT_SHA256
    selection_resealed = build_copy_with_id(
        contract.model_copy(
            update={
                "bound_selection": contract.bound_selection.model_copy(update={"report_sha256": drifted_selection_sha})
            }
        )
    )
    write_portfolio_oos_freeze(freeze_path, selection_resealed)
    with pytest.raises(ValueError, match="selection report SHA-256 drifted"):
        load_verified_portfolio_oos_freeze(freeze_path)
    with pytest.raises(ValueError, match="selection report SHA-256 drifted|committed binding"):
        assert_committed_portfolio_oos_freeze_bindings(selection_resealed)

    write_portfolio_oos_freeze(freeze_path, contract)
    _copy_committed_json(FROZEN_SELECTION_REPORT_PATH, tmp_path)
    robustness_path = tmp_path / FROZEN_ROBUSTNESS_REPORT_PATH
    drifted_robustness_sha = _write_json(
        robustness_path,
        {
            "selected_config_hash": FROZEN_CONFIG_HASH,
            "status": "CONDITIONAL_GO",
            "data_snapshot_id": FROZEN_DEVELOPMENT_DATA_SNAPSHOT_ID,
            "note": "resealed-drift",
        },
    )
    assert drifted_robustness_sha != FROZEN_ROBUSTNESS_REPORT_SHA256
    robustness_resealed = build_copy_with_id(
        contract.model_copy(
            update={
                "bound_robustness": contract.bound_robustness.model_copy(
                    update={"report_sha256": drifted_robustness_sha}
                )
            }
        )
    )
    write_portfolio_oos_freeze(freeze_path, robustness_resealed)
    with pytest.raises(ValueError, match="robustness report SHA-256 drifted"):
        load_verified_portfolio_oos_freeze(freeze_path)


def test_cli_rejects_custom_resealed_freeze_with_report_hash_drift(tmp_path: Path) -> None:
    freeze_path = _write_freeze_project(tmp_path)
    contract = load_verified_portfolio_oos_freeze(freeze_path)
    drifted_sha = "ab" * 32
    resealed = build_copy_with_id(
        contract.model_copy(
            update={"bound_selection": contract.bound_selection.model_copy(update={"report_sha256": drifted_sha})}
        )
    )
    custom_path = tmp_path / "custom-portfolio-oos-freeze.json"
    write_portfolio_oos_freeze(custom_path, resealed)
    result = CliRunner().invoke(
        cli_app,
        [
            "verify-all-a-share-portfolio-oos-freeze",
            "--freeze-file",
            str(custom_path),
            "--project-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "selection report SHA-256 drifted" in result.output or "committed binding" in result.output


def test_verify_rejects_manifest_snapshot_base_and_coverage_drift(tmp_path: Path) -> None:
    freeze_path = _write_freeze_project(tmp_path)
    oos_manifest = tmp_path / FROZEN_OOS_MARKET_DIR / "manifest.json"
    payload = json.loads(oos_manifest.read_text(encoding="utf-8"))
    payload["snapshot_id"] = "ab" * 32
    payload["content_hash"] = "ab" * 32
    oos_manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="OOS market snapshot_id"):
        verify_portfolio_oos_freeze(freeze_path=freeze_path, project_root=tmp_path)

    freeze_path = _write_freeze_project(tmp_path)
    fund_manifest = tmp_path / FROZEN_OOS_FUNDAMENTAL_DIR / "manifest.json"
    payload = json.loads(fund_manifest.read_text(encoding="utf-8"))
    payload["base_market_snapshot_id"] = "cd" * 32
    fund_manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="base_market_snapshot_id|content_hash"):
        verify_portfolio_oos_freeze(freeze_path=freeze_path, project_root=tmp_path)

    freeze_path = _write_freeze_project(tmp_path)
    payload = json.loads(oos_manifest.read_text(encoding="utf-8"))
    payload["coverage_end"] = "2026-08-20"
    oos_manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="coverage_end"):
        verify_portfolio_oos_freeze(freeze_path=freeze_path, project_root=tmp_path)


def test_verify_rejects_manifest_table_hash_and_schema_drift(tmp_path: Path) -> None:
    freeze_path = _write_freeze_project(tmp_path)
    oos_manifest = tmp_path / FROZEN_OOS_MARKET_DIR / "manifest.json"
    payload = json.loads(oos_manifest.read_text(encoding="utf-8"))
    table_hashes = dict(payload["table_hashes"])
    table_hashes["daily_bars"] = "ef" * 32
    payload["table_hashes"] = table_hashes
    oos_manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="content_hash does not match table_hashes"):
        verify_portfolio_oos_freeze(freeze_path=freeze_path, project_root=tmp_path)

    freeze_path = _write_freeze_project(tmp_path)
    fund_manifest = tmp_path / FROZEN_OOS_FUNDAMENTAL_DIR / "manifest.json"
    payload = json.loads(fund_manifest.read_text(encoding="utf-8"))
    table_hashes = dict(payload["table_hashes"])
    table_hashes["daily_valuation"] = "11" * 32
    payload["table_hashes"] = table_hashes
    fund_manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="content_hash does not match table_hashes"):
        verify_portfolio_oos_freeze(freeze_path=freeze_path, project_root=tmp_path)

    freeze_path = _write_freeze_project(tmp_path)
    invalid = {
        "snapshot_id": FROZEN_OOS_MARKET_SNAPSHOT_ID,
        "content_hash": FROZEN_OOS_MARKET_SNAPSHOT_ID,
        "schema_version": "4",
        "table_hashes": {"calendar": FROZEN_OOS_MARKET_CALENDAR_TABLE_HASH},
    }
    oos_manifest.write_text(json.dumps(invalid, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest is invalid"):
        verify_portfolio_oos_freeze(freeze_path=freeze_path, project_root=tmp_path)

    freeze_path = _write_freeze_project(tmp_path)
    payload = json.loads(oos_manifest.read_text(encoding="utf-8"))
    payload["content_hash"] = "22" * 32
    oos_manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot_id does not equal content_hash|content_hash"):
        verify_portfolio_oos_freeze(freeze_path=freeze_path, project_root=tmp_path)


def test_verify_rejects_calendar_overlap_and_anchor_drift(tmp_path: Path) -> None:
    freeze_path = _write_freeze_project(tmp_path)
    oos_cal = tmp_path / FROZEN_OOS_MARKET_DIR / "calendar.parquet"
    _days, oos_days = _calendar_from_fixture()
    tampered = [day for day in oos_days if day != date(2024, 10, 8)]
    _write_calendar(oos_cal, tampered)
    with pytest.raises(ValueError, match="OOS calendar content hash"):
        verify_portfolio_oos_freeze(freeze_path=freeze_path, project_root=tmp_path)

    broken_oos = [day for day in oos_days if day != date(2024, 11, 1)]
    with pytest.raises(ValueError, match="calendars differ on the overlap|overlap trading-day"):
        compute_calendar_equivalence_proof(_days, broken_oos)


def test_verify_rejects_signal_cutoff_drift_in_contract(tmp_path: Path) -> None:
    freeze_path = _write_freeze_project(tmp_path)
    contract = load_verified_portfolio_oos_freeze(freeze_path)
    drifted = contract.model_copy(
        update={"evaluation_window": contract.evaluation_window.model_copy(update={"signal_cutoff": date(2026, 7, 21)})}
    )
    write_portfolio_oos_freeze(freeze_path, build_copy_with_id(drifted))
    with pytest.raises(ValueError, match="signal_cutoff"):
        load_verified_portfolio_oos_freeze(freeze_path)


def test_verify_rejects_absolute_or_parent_paths(tmp_path: Path) -> None:
    freeze_path = _write_freeze_project(tmp_path)
    contract = load_verified_portfolio_oos_freeze(freeze_path)
    absolute = contract.model_copy(
        update={"bound_strategy": contract.bound_strategy.model_copy(update={"strategy_path": "/tmp/strategy.yaml"})}
    )
    write_portfolio_oos_freeze(freeze_path, build_copy_with_id(absolute))
    with pytest.raises(ValueError, match="relative path"):
        load_verified_portfolio_oos_freeze(freeze_path)

    parent = contract.model_copy(
        update={"bound_oos_data": contract.bound_oos_data.model_copy(update={"market_dir": "../secrets/parquet"})}
    )
    write_portfolio_oos_freeze(freeze_path, build_copy_with_id(parent))
    with pytest.raises(ValueError, match="relative path"):
        load_verified_portfolio_oos_freeze(freeze_path)


def test_verify_rejects_boundary_flag_and_gate_drift(tmp_path: Path) -> None:
    freeze_path = _write_freeze_project(tmp_path)
    contract = load_verified_portfolio_oos_freeze(freeze_path)
    for field in ("ready_for_scoring", "ready_for_trading", "auto_deploy", "authorized"):
        payload = json.loads(freeze_path.read_text(encoding="utf-8"))
        payload[field] = True
        freeze_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing or invalid"):
            load_verified_portfolio_oos_freeze(freeze_path)
        write_portfolio_oos_freeze(freeze_path, contract)

    one_shot = json.loads(freeze_path.read_text(encoding="utf-8"))
    one_shot["one_shot_required"] = False
    freeze_path.write_text(json.dumps(one_shot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing or invalid"):
        load_verified_portfolio_oos_freeze(freeze_path)
    write_portfolio_oos_freeze(freeze_path, contract)

    gate_drift = contract.model_copy(
        update={"evaluability_gates": EVALUABILITY_GATES.model_copy(update={"pnl_reconciliation_abs_tol": 1e-5})}
    )
    write_portfolio_oos_freeze(freeze_path, build_copy_with_id(gate_drift))
    with pytest.raises(ValueError, match="evaluability gates drifted"):
        load_verified_portfolio_oos_freeze(freeze_path)

    risk_drift = contract.model_copy(
        update={"hard_risk_gates": HARD_RISK_GATES.model_copy(update={"max_drawdown_floor": -0.20})}
    )
    write_portfolio_oos_freeze(freeze_path, build_copy_with_id(risk_drift))
    with pytest.raises(ValueError, match="hard risk gates drifted"):
        load_verified_portfolio_oos_freeze(freeze_path)

    primary_drift = contract.model_copy(
        update={"primary_oos_endpoint": PRIMARY_OOS_ENDPOINT.model_copy(update={"may_promote_to_scoring": True})}
    )
    write_portfolio_oos_freeze(freeze_path, build_copy_with_id(primary_drift))
    with pytest.raises(ValueError, match="missing or invalid"):
        load_verified_portfolio_oos_freeze(freeze_path)

    descriptive = [
        DESCRIPTIVE_ENDPOINTS[0].model_copy(update={"decides_oos_result": True}),
        *DESCRIPTIVE_ENDPOINTS[1:],
    ]
    desc_drift = contract.model_copy(update={"descriptive_endpoints": descriptive})
    write_portfolio_oos_freeze(freeze_path, build_copy_with_id(desc_drift))
    with pytest.raises(ValueError, match="missing or invalid"):
        load_verified_portfolio_oos_freeze(freeze_path)


def test_cli_verifies_freeze_without_scoring(tmp_path: Path) -> None:
    freeze_path = _write_freeze_project(tmp_path)
    result = CliRunner().invoke(
        cli_app,
        [
            "verify-all-a-share-portfolio-oos-freeze",
            "--freeze-file",
            str(freeze_path),
            "--project-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "ready_for_scoring=false" in result.output
    assert "ready_for_trading=false" in result.output
    assert "auto_deploy=false" in result.output
    assert "authorized=false" in result.output
    assert "one_shot_required=true" in result.output
    assert "runtime_equivalent_anchor=2024-10-29" in result.output
    assert "p10_h20" in result.output
    assert COMMITTED_FREEZE_ID in result.output


def test_committed_freeze_is_self_consistent() -> None:
    contract = load_verified_portfolio_oos_freeze(COMMITTED_FREEZE)
    assert_committed_portfolio_oos_freeze_bindings(contract)
    assert contract.freeze_version == PORTFOLIO_OOS_FREEZE_VERSION
    assert contract.bound_strategy.strategy_config_id == FROZEN_STRATEGY_CONFIG_ID
    assert contract.bound_strategy.config_hash == FROZEN_CONFIG_HASH
    assert contract.bound_strategy.candidate_id == "p10_h20"
    assert contract.bound_selection.report_sha256 == FROZEN_SELECTION_REPORT_SHA256
    assert contract.bound_robustness.report_sha256 == FROZEN_ROBUSTNESS_REPORT_SHA256
    assert contract.bound_robustness.status == "CONDITIONAL_GO"
    assert contract.calendar_equivalence.runtime_equivalent_anchor == date(2024, 10, 29)
    assert contract.evaluation_window.signal_cutoff == date(2026, 7, 22)
    assert contract.ready_for_scoring is False
    assert contract.ready_for_trading is False
    assert contract.authorized is False
    assert contract.auto_deploy is False
    assert contract.one_shot_required is True
    assert contract.freeze_id == COMMITTED_FREEZE_ID
    assert contract.freeze_id == build_committed_portfolio_oos_freeze().freeze_id
