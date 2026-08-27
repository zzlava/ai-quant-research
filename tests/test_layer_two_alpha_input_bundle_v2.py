from __future__ import annotations

from pathlib import Path

import pytest

from app.research.layer_two_alpha_input_bundle_v2 import (
    DEFAULT_OUTPUT_PATH,
    SLOT_ORDER,
    LayerTwoAlphaInputBundleV2,
    verify_input_bundle,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bundle() -> LayerTwoAlphaInputBundleV2:
    return verify_input_bundle(repo_root=REPO_ROOT, path=DEFAULT_OUTPUT_PATH)


def test_committed_input_bundle_fully_recomputes(bundle: LayerTwoAlphaInputBundleV2) -> None:
    assert bundle.bundle_id == "c8363dabde718b0d93f2ba4f33d1c75ab2861834ce11611c653e2f0290157bfb"
    assert tuple(slot.kind for slot in bundle.slots) == SLOT_ORDER
    assert bundle.readiness.all_six_slots_bound is True


def test_bundle_authorizes_only_frozen_offline_diagnostic(
    bundle: LayerTwoAlphaInputBundleV2,
) -> None:
    readiness = bundle.readiness
    assert readiness.research_only is True
    assert readiness.ready_for_frozen_alpha_diagnostic_execution is True
    assert readiness.ready_for_scoring is False
    assert readiness.ready_for_backtest is False
    assert readiness.ready_for_portfolio_construction is False
    assert readiness.ready_for_orders is False
    assert readiness.ready_for_trading is False
    assert readiness.auto_apply is False


def test_bundle_preserves_seen_and_oos_boundaries(bundle: LayerTwoAlphaInputBundleV2) -> None:
    assert bundle.development_window == "2022-01-01..2023-12-31"
    assert bundle.seen_robustness_report_only_window == "2024-01-01..2024-12-31"
    assert bundle.consumed_oos_forbidden == "2025-01-01..2026-08-21"
    assert bundle.new_frozen_oos_unauthorized_from == "2026-08-22"
    assert (
        bundle.alpha_evidence_denominator
        == "candidate_complete_and_eligible_for_new_entry_and_factor_known"
    )
    assert (
        bundle.financial_overlay_role
        == "independent_fail_closed_new_entry_safety_overlay_not_ic_denominator"
    )
