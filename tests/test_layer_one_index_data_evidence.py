from __future__ import annotations

from pathlib import Path

import pytest

from app.research.layer_one_index_data_evidence import (
    DEFAULT_EVIDENCE_PATH,
    LayerOneIndexDataEvidence,
    build_layer_one_index_data_evidence,
    seal_layer_one_index_data_evidence,
    verify_layer_one_index_data_evidence,
    verify_layer_one_index_data_evidence_file,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_committed_evidence_matches_full_disk_recomputation() -> None:
    evidence = verify_layer_one_index_data_evidence_file(
        evidence_path=DEFAULT_EVIDENCE_PATH,
        repo_root=PROJECT_ROOT,
    )
    rebuilt = build_layer_one_index_data_evidence(repo_root=PROJECT_ROOT)
    assert evidence.model_dump(mode="json") == rebuilt.model_dump(mode="json")
    assert evidence.risk_state_index.symbol == "000985.CSI"
    assert evidence.performance_benchmark.symbol == "H00985.CSI"
    assert evidence.ready_for_layer_one_historical_evaluation is True
    assert evidence.ready_for_stock_scoring is False
    assert evidence.ready_for_trading is False


def test_tampered_binding_fails_even_when_resealed() -> None:
    evidence = build_layer_one_index_data_evidence(repo_root=PROJECT_ROOT)
    broken = evidence.model_copy(
        update={
            "identity_contract": evidence.identity_contract.model_copy(
                update={"sha256": "0" * 64}
            ),
            "evidence_id": None,
        }
    )
    resealed = seal_layer_one_index_data_evidence(broken)
    with pytest.raises(ValueError, match="does not match verified disk artifacts"):
        verify_layer_one_index_data_evidence(resealed, repo_root=PROJECT_ROOT)


def test_readiness_escalation_and_symbol_drift_rejected() -> None:
    evidence = build_layer_one_index_data_evidence(repo_root=PROJECT_ROOT)
    payload = evidence.model_dump(mode="json", exclude={"evidence_id"})
    payload["ready_for_trading"] = True
    with pytest.raises(ValueError, match="Input should be False"):
        LayerOneIndexDataEvidence.model_validate(payload)

    payload = evidence.model_dump(mode="json", exclude={"evidence_id"})
    payload["risk_state_index"]["symbol"] = "000300.SH"
    with pytest.raises(ValueError, match="verified CSI All-Share price index"):
        LayerOneIndexDataEvidence.model_validate(payload)


def test_path_escape_and_missing_evidence_fail_closed(tmp_path: Path) -> None:
    outside = tmp_path / "evidence.json"
    outside.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inside repo_root"):
        verify_layer_one_index_data_evidence_file(
            evidence_path=outside,
            repo_root=PROJECT_ROOT,
        )
    with pytest.raises(FileNotFoundError, match="file not found"):
        verify_layer_one_index_data_evidence_file(
            evidence_path=Path("config/research/not-present.json"),
            repo_root=PROJECT_ROOT,
        )
