from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.research.statistical_power_gate import (
    DEFAULT_STATISTICAL_POWER_GATE_PATH,
    StatisticalPowerGateProtocol,
    build_retrospective_power_review,
    seal_protocol,
    seal_review,
    verify_power_review,
    verify_protocol,
    write_power_review,
)
from tests.helpers import PROJECT_ROOT

COMMITTED_PROTOCOL = PROJECT_ROOT / DEFAULT_STATISTICAL_POWER_GATE_PATH
SOURCE_RELATIVE = "data/all-a-share-historical-v1/research/layer-two-alpha-diagnostic-v2/report.json"
SOURCE_REPORT = PROJECT_ROOT / SOURCE_RELATIVE


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _protocol_for_source(root: Path, source_relative: str, *, source_sha: str) -> StatisticalPowerGateProtocol:
    protocol = StatisticalPowerGateProtocol.model_validate(
        {
            "method": {
                "familywise_alpha": 0.05,
                "target_power": 0.8,
                "family_size": 4,
            },
            "endpoints": [
                {"endpoint_id": "quality", "horizon_market_days": 40, "minimum_effect_of_interest": 0.05},
                {"endpoint_id": "value", "horizon_market_days": 40, "minimum_effect_of_interest": 0.05},
                {
                    "endpoint_id": "medium_momentum_12_1",
                    "horizon_market_days": 40,
                    "minimum_effect_of_interest": 0.05,
                },
                {
                    "endpoint_id": "defensive_low_vol",
                    "horizon_market_days": 40,
                    "minimum_effect_of_interest": 0.05,
                },
            ],
            "source_binding": {
                "diagnostic_report_path": source_relative,
                "diagnostic_report_sha256": source_sha,
            },
        }
    )
    return seal_protocol(protocol)


def _write_protocol(path: Path, protocol: StatisticalPowerGateProtocol) -> None:
    _write_json(path, protocol.model_dump(mode="json"))


def _temporary_bound_protocol(tmp_path: Path, source_payload: dict[str, Any]) -> tuple[Path, Path]:
    source_relative = "data/diagnostic/report.json"
    source_path = tmp_path / source_relative
    _write_json(source_path, source_payload)
    protocol = _protocol_for_source(tmp_path, source_relative, source_sha=_sha256(source_path))
    protocol_path = tmp_path / "config/power.json"
    _write_protocol(protocol_path, protocol)
    return protocol_path, source_path


def test_committed_protocol_verifies_and_current_family_is_not_evaluable() -> None:
    protocol = verify_protocol(COMMITTED_PROTOCOL, repo_root=PROJECT_ROOT)
    assert protocol.protocol_id == "25b10aaf70e51198f83808004102eaa4779dff9940c72584922fd6ce6e205bf2"
    review = build_retrospective_power_review(protocol_path=COMMITTED_PROTOCOL, repo_root=PROJECT_ROOT)
    assert review.retrospective_calibration_only is True
    assert review.consumes_oos is False
    assert review.family_outcome == "not_evaluable"
    assert review.endpoints_evaluable == 1
    assert review.endpoints_not_evaluable == 3
    by_id = {row.endpoint_id: row for row in review.rows}
    assert by_id["quality"].normal_approximation_mde == pytest.approx(0.04308, abs=0.0001)
    assert by_id["quality"].outcome == "evaluable_for_minimum_effect"
    assert by_id["value"].normal_approximation_mde == pytest.approx(0.12505, abs=0.0001)
    assert by_id["value"].outcome == "not_evaluable"
    assert by_id["medium_momentum_12_1"].outcome == "not_evaluable"
    assert by_id["defensive_low_vol"].outcome == "not_evaluable"
    assert review.ready_for_scoring is False
    assert review.ready_for_backtest is False
    assert review.ready_for_trading is False


def test_protocol_rejects_duplicate_endpoints_and_family_mismatch() -> None:
    base = {
        "method": {"familywise_alpha": 0.05, "target_power": 0.8, "family_size": 2},
        "endpoints": [
            {"endpoint_id": "same", "horizon_market_days": 40, "minimum_effect_of_interest": 0.05},
            {"endpoint_id": "same", "horizon_market_days": 40, "minimum_effect_of_interest": 0.05},
        ],
        "source_binding": {
            "diagnostic_report_path": SOURCE_RELATIVE,
            "diagnostic_report_sha256": "0" * 64,
        },
    }
    with pytest.raises(ValidationError, match="endpoint_id values must be unique"):
        StatisticalPowerGateProtocol.model_validate(base)
    base["endpoints"][1]["endpoint_id"] = "other"
    base["method"]["family_size"] = 3
    with pytest.raises(ValidationError, match="family_size"):
        StatisticalPowerGateProtocol.model_validate(base)


def test_verify_protocol_rejects_source_hash_change(tmp_path: Path) -> None:
    source_payload = json.loads(SOURCE_REPORT.read_text(encoding="utf-8"))
    protocol_path, source_path = _temporary_bound_protocol(tmp_path, source_payload)
    verify_protocol(protocol_path, repo_root=tmp_path)
    source_payload["status"] = "tampered"
    _write_json(source_path, source_payload)
    with pytest.raises(ValueError, match="SHA-256"):
        verify_protocol(protocol_path, repo_root=tmp_path)


def test_build_rejects_missing_factor_even_when_source_hash_is_resealed(tmp_path: Path) -> None:
    source_payload = json.loads(SOURCE_REPORT.read_text(encoding="utf-8"))
    source_payload["factor_decisions"] = source_payload["factor_decisions"][:-1]
    protocol_path, _source_path = _temporary_bound_protocol(tmp_path, source_payload)
    with pytest.raises(ValueError, match="factor set does not match protocol"):
        build_retrospective_power_review(protocol_path=protocol_path, repo_root=tmp_path)


def test_build_rejects_unrecoverable_hac_standard_error(tmp_path: Path) -> None:
    source_payload = json.loads(SOURCE_REPORT.read_text(encoding="utf-8"))
    source_payload["factor_decisions"][0]["primary"]["hac_statistic"] = 0.0
    protocol_path, _source_path = _temporary_bound_protocol(tmp_path, source_payload)
    with pytest.raises(ValueError, match="positive HAC standard error"):
        build_retrospective_power_review(protocol_path=protocol_path, repo_root=tmp_path)


def test_review_full_recomputation_and_tamper_detection(tmp_path: Path) -> None:
    review = build_retrospective_power_review(protocol_path=COMMITTED_PROTOCOL, repo_root=PROJECT_ROOT)
    review_path = tmp_path / "review.json"
    write_power_review(review_path, review)
    verified = verify_power_review(
        review_path=review_path,
        protocol_path=COMMITTED_PROTOCOL,
        repo_root=PROJECT_ROOT,
    )
    assert verified.review_id == review.review_id

    payload = json.loads(review_path.read_text(encoding="utf-8"))
    payload["rows"][0]["normal_approximation_mde"] += 0.01
    tampered = seal_review(type(review).model_validate({**payload, "review_id": None}))
    _write_json(review_path, tampered.model_dump(mode="json"))
    with pytest.raises(ValueError, match="full recomputation"):
        verify_power_review(
            review_path=review_path,
            protocol_path=COMMITTED_PROTOCOL,
            repo_root=PROJECT_ROOT,
        )


def test_review_writer_is_idempotent_and_refuses_different_bytes(tmp_path: Path) -> None:
    review = build_retrospective_power_review(protocol_path=COMMITTED_PROTOCOL, repo_root=PROJECT_ROOT)
    review_path = tmp_path / "review.json"
    first = write_power_review(review_path, review)
    second = write_power_review(review_path, review)
    assert second.review_id == first.review_id

    payload = review.model_dump(mode="json")
    payload["rows"][0]["normal_approximation_mde"] += 0.01
    different = seal_review(type(review).model_validate({**payload, "review_id": None}))
    with pytest.raises(FileExistsError, match="different bytes"):
        write_power_review(review_path, different)


def test_cli_builds_and_verifies_review(tmp_path: Path) -> None:
    output = tmp_path / "power-review.json"
    runner = CliRunner()
    built = runner.invoke(
        cli_app,
        [
            "review-statistical-power",
            "--output",
            str(output),
            "--protocol-file",
            str(COMMITTED_PROTOCOL),
            "--repo-root",
            str(PROJECT_ROOT),
        ],
    )
    assert built.exit_code == 0, built.output
    assert "family_outcome=not_evaluable" in built.output
    assert "consumes_oos=false" in built.output
    assert "ready_for_backtest=false" in built.output
    assert output.is_file()

    verified = runner.invoke(
        cli_app,
        [
            "verify-statistical-power-review",
            "--review-file",
            str(output),
            "--protocol-file",
            str(COMMITTED_PROTOCOL),
            "--repo-root",
            str(PROJECT_ROOT),
        ],
    )
    assert verified.exit_code == 0, verified.output
    assert "endpoints_evaluable=1" in verified.output
    assert "endpoints_not_evaluable=3" in verified.output
    assert "ready_for_scoring=false" in verified.output
