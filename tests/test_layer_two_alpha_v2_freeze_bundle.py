from __future__ import annotations

from pathlib import Path

import pytest

from app.research.layer_two_alpha_v2_freeze_bundle import (
    HYPOTHESES,
    LayerTwoAlphaDevelopmentProtocolV2,
    LayerTwoAlphaDiagnosticRunContractV2,
    LayerTwoAlphaTrialRegistrationV2,
    verify_bundle,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bundle() -> tuple[
    LayerTwoAlphaTrialRegistrationV2,
    LayerTwoAlphaDevelopmentProtocolV2,
    LayerTwoAlphaDiagnosticRunContractV2,
]:
    return verify_bundle(repo_root=REPO_ROOT)


def test_committed_v2_freeze_chain_recomputes_from_bound_sources(
    bundle: tuple[
        LayerTwoAlphaTrialRegistrationV2,
        LayerTwoAlphaDevelopmentProtocolV2,
        LayerTwoAlphaDiagnosticRunContractV2,
    ],
) -> None:
    registration, protocol, contract = bundle

    assert registration.registration_id == "8a5a2e03595f7df48a6da0d01317e27c9cb189aadd4ca5d4df96dfe0df36012a"
    assert protocol.protocol_id == "7cd295ab6dcf596aef4d117b1b7db9abab7057d34063d6659f886e324e57fe74"
    assert contract.contract_id == "57644d0e85c6feff56c7b2a5a11615216e5769d49ffe02a0d0c4693b502d444a"


def test_v2_protocol_changes_only_the_evidence_denominator(
    bundle: tuple[
        LayerTwoAlphaTrialRegistrationV2,
        LayerTwoAlphaDevelopmentProtocolV2,
        LayerTwoAlphaDiagnosticRunContractV2,
    ],
) -> None:
    registration, protocol, _ = bundle

    assert registration.hypotheses == HYPOTHESES
    assert protocol.hypotheses == HYPOTHESES
    assert protocol.change_scope == "coverage_denominator_separation_only"
    assert protocol.factor_known_count_gate == 500
    assert protocol.factor_known_fraction_gate == 0.6
    assert protocol.primary_horizon_market_days == 40
    assert protocol.primary_hac_lag == 39
    assert protocol.windows.development == "2022-01-01..2023-12-31"
    assert protocol.windows.seen_robustness_report_only == "2024-01-01..2024-12-31"


def test_run_contract_remains_fail_closed_on_cluster_slot(
    bundle: tuple[
        LayerTwoAlphaTrialRegistrationV2,
        LayerTwoAlphaDevelopmentProtocolV2,
        LayerTwoAlphaDiagnosticRunContractV2,
    ],
) -> None:
    _, _, contract = bundle

    assert contract.exact_unbound_required_slots == ("statistical_cluster_companion_reports",)
    assert contract.alpha_execution_permitted is False
    assert contract.readiness.ready_for_alpha_diagnostic_execution is False
    assert contract.readiness.ready_for_scoring is False
    assert contract.readiness.ready_for_backtest is False
    assert contract.readiness.ready_for_trading is False
    financial = next(slot for slot in contract.input_slots if slot.kind == "financial_negative_list_reports")
    assert financial.state == "bound"
    assert financial.role == "separate_fail_closed_safety_overlay_not_alpha_denominator"
