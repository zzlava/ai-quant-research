from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.research.layer_two_alpha_input_feasibility import (
    EXPECTED_BLOCKERS,
    DailyFeasibility,
    FeasibilityReadiness,
    FeasibilitySourceBinding,
    FeasibilityThresholds,
    LayerTwoAlphaInputFeasibilityReport,
    YearFeasibility,
    compute_daily_feasibility,
    seal_report,
    verify_report_self_hash,
)


def _candidate(rows: list[tuple[str, str, bool, bool]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "symbol": pl.String,
            "as_of": pl.String,
            "eligible_for_new_entry": pl.Boolean,
            "unknown_critical_input": pl.Boolean,
        },
        orient="row",
    )


def _financial(rows: list[tuple[str, str, str, bool]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "symbol": pl.String,
            "as_of": pl.String,
            "decision_status": pl.String,
            "eligible_for_new_entry": pl.Boolean,
        },
        orient="row",
    )


def test_upper_bound_requires_both_candidate_and_complete_financial_verdict() -> None:
    candidate = _candidate(
        [
            ("000001.SZ", "2022-01-04", True, False),
            ("000002.SZ", "2022-01-04", True, False),
            ("000003.SZ", "2022-01-04", True, True),
            ("000001.SZ", "2022-01-05", True, False),
            ("000002.SZ", "2022-01-05", False, False),
            ("000003.SZ", "2022-01-05", True, False),
        ]
    )
    financial = _financial(
        [
            ("000001.SZ", "2022-01-04", "clean", True),
            ("000002.SZ", "2022-01-04", "insufficient_evidence", False),
            ("000003.SZ", "2022-01-04", "clean", True),
            ("000001.SZ", "2022-01-05", "halved", True),
            ("000002.SZ", "2022-01-05", "clean", True),
            ("000003.SZ", "2022-01-05", "hard_excluded", False),
        ]
    )
    rows = compute_daily_feasibility(candidate, financial, min_known_count=2, primary_horizon=1)
    assert rows[0].alpha_eligible_upper_bound == 1
    assert rows[0].financial_decisive_rows == 2
    assert rows[0].count_gate_upper_bound_pass is False
    assert rows[1].alpha_eligible_upper_bound == 1
    assert rows[1].financial_hard_excluded_rows == 1


def test_join_key_set_mismatch_fails_closed() -> None:
    candidate = _candidate([("000001.SZ", "2022-01-04", True, False)])
    financial = _financial([("000002.SZ", "2022-01-04", "clean", True)])
    with pytest.raises(ValueError, match="key sets differ"):
        compute_daily_feasibility(candidate, financial, min_known_count=1, primary_horizon=1)


def _sealed_report() -> LayerTwoAlphaInputFeasibilityReport:
    hexes = [f"{digit:x}" * 64 for digit in range(1, 10)]
    daily = (
        DailyFeasibility(
            as_of=date(2022, 1, 4),
            year=2022,
            candidate_rows=10,
            candidate_complete_rows=10,
            candidate_eligible_rows=8,
            financial_decisive_rows=5,
            financial_hard_excluded_rows=1,
            alpha_eligible_upper_bound=4,
            h40_endpoint=date(2022, 3, 1),
            endpoint_within_same_evidence_window=True,
            count_gate_upper_bound_pass=False,
            primary_date_upper_bound_pass=False,
        ),
        DailyFeasibility(
            as_of=date(2023, 1, 3),
            year=2023,
            candidate_rows=10,
            candidate_complete_rows=10,
            candidate_eligible_rows=8,
            financial_decisive_rows=5,
            financial_hard_excluded_rows=1,
            alpha_eligible_upper_bound=4,
            h40_endpoint=date(2023, 3, 1),
            endpoint_within_same_evidence_window=True,
            count_gate_upper_bound_pass=False,
            primary_date_upper_bound_pass=False,
        ),
        DailyFeasibility(
            as_of=date(2024, 1, 2),
            year=2024,
            candidate_rows=10,
            candidate_complete_rows=10,
            candidate_eligible_rows=8,
            financial_decisive_rows=5,
            financial_hard_excluded_rows=1,
            alpha_eligible_upper_bound=4,
            h40_endpoint=date(2024, 3, 1),
            endpoint_within_same_evidence_window=True,
            count_gate_upper_bound_pass=False,
            primary_date_upper_bound_pass=False,
        ),
    )
    yearly = tuple(
        YearFeasibility(
            year=year,
            trading_dates=1,
            min_candidate_eligible_rows=8,
            median_candidate_eligible_rows=8.0,
            max_candidate_eligible_rows=8,
            count_gate_upper_bound_dates=0,
            primary_valid_date_upper_bound=0,
            min_alpha_eligible_upper_bound=4,
            median_alpha_eligible_upper_bound=4.0,
            max_alpha_eligible_upper_bound=4,
        )
        for year in (2022, 2023, 2024)
    )
    report = LayerTwoAlphaInputFeasibilityReport(
        schema_version="1",
        report_version="layer-two-alpha-input-feasibility-v1",
        source_binding=FeasibilitySourceBinding(
            inventory_path="data/inventory.json",
            inventory_id=hexes[0],
            inventory_file_sha256=hexes[1],
            run_contract_path="config/run.json",
            run_contract_id=hexes[2],
            run_contract_file_sha256=hexes[3],
            alpha_protocol_path="config/protocol.json",
            alpha_protocol_id=hexes[4],
            alpha_protocol_file_sha256=hexes[5],
            market_snapshot_id=hexes[6],
            fundamental_snapshot_id=hexes[7],
            candidate_pack_path="data/candidate",
            candidate_pack_id=hexes[8],
            candidate_manifest_sha256=hexes[0],
            candidate_parquet_sha256=hexes[1],
            financial_overlay_path="data/financial",
            financial_overlay_id=hexes[2],
            financial_overlay_manifest_sha256=hexes[3],
            financial_overlay_dataset_hash=hexes[4],
        ),
        thresholds=FeasibilityThresholds(
            primary_horizon_market_days=40,
            min_factor_known_cs_per_decision=500,
            min_factor_known_cs_fraction_of_eligible=0.60,
            min_valid_primary_scoring_dates_pooled=120,
            min_valid_primary_scoring_dates_in_2022=40,
            min_valid_primary_scoring_dates_in_2023=40,
        ),
        coverage_start=daily[0].as_of,
        coverage_end=daily[-1].as_of,
        trading_date_count=3,
        daily=daily,
        yearly=yearly,
        development_primary_valid_date_upper_bound=0,
        blockers=EXPECTED_BLOCKERS,
        statistical_cluster_companion_materialized=False,
        stop_reason="frozen_coverage_gates_are_unreachable_even_if_every_eligible_factor_value_is_known",
        readiness=FeasibilityReadiness(
            research_only=True,
            optimistic_upper_bound_only=True,
            ready_for_alpha_diagnostic_execution=False,
            ready_for_scoring=False,
            ready_for_backtest=False,
            ready_for_portfolio_construction=False,
            ready_for_orders=False,
            ready_for_trading=False,
            auto_apply=False,
        ),
    )
    return seal_report(report)


def test_report_self_hash_detects_tamper() -> None:
    report = _sealed_report()
    verify_report_self_hash(report)
    tampered = report.model_copy(update={"development_primary_valid_date_upper_bound": 1})
    with pytest.raises(ValueError, match="self-hash mismatch"):
        verify_report_self_hash(tampered)


def test_report_rejects_readiness_true() -> None:
    report = _sealed_report()
    payload = report.model_dump(mode="json")
    payload["readiness"]["ready_for_alpha_diagnostic_execution"] = True
    with pytest.raises(ValueError):
        LayerTwoAlphaInputFeasibilityReport.model_validate(payload)
