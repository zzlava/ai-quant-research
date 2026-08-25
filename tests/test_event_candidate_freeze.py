from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.research.event_candidate_diagnostics import (
    CANDIDATE_HYPOTHESES,
    DEVELOPMENT_WINDOW_END,
    DEVELOPMENT_WINDOW_START,
    FORWARD_HORIZONS,
    OBSERVATION_COLUMNS,
    SUMMARY_COLUMNS,
    CandidateHypothesisSpec,
    EventCandidateDiagnosticReport,
    _report_id,
    _sha256_file,
    write_event_candidate_diagnostics_atomically,
)
from app.research.event_candidate_freeze import (
    DEFAULT_EVENT_CANDIDATE_OOS_FREEZE_PATH,
    EVENT_CANDIDATE_OOS_FREEZE_VERSION,
    EventCandidateOosFreezeContract,
    build_copy_with_id,
    build_event_candidate_oos_freeze,
    evaluate_nomination_gate,
    load_verified_event_candidate_oos_freeze,
    verify_event_candidate_oos_freeze,
    write_event_candidate_oos_freeze,
)
from tests.helpers import PROJECT_ROOT

COMMITTED_FREEZE = PROJECT_ROOT / DEFAULT_EVENT_CANDIDATE_OOS_FREEZE_PATH
COMMITTED_DIAGNOSTIC = (
    PROJECT_ROOT
    / "data/all-a-share-historical-v1/event-candidate-diagnostics/development-2022-2023-v1"
)
PASSING_HYPOTHESIS_IDS = ("forecast_upward_revision", "audit_non_standard_opinion")


def _metric(spec: CandidateHypothesisSpec, *, supported: bool) -> float:
    if spec.candidate_direction == "positive":
        return 0.05 if supported else -0.05
    return -0.05 if supported else 0.05


def _summary_frame(
    *,
    passing_ids: tuple[str, ...] = PASSING_HYPOTHESIS_IDS,
    overrides: dict[str, dict[str, object]] | None = None,
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    extra = overrides or {}
    for spec in CANDIDATE_HYPOTHESES:
        supported = spec.hypothesis_id in passing_ids
        for year in ("2022", "2023"):
            key = f"{spec.hypothesis_id}:{year}"
            patch = extra.get(key, {})
            binary = spec.signal_kind == "binary_bucket"
            year_supported = bool(patch.get("supported", supported))
            year_metric = float(patch.get("metric", _metric(spec, supported=year_supported)))
            coverage = float(patch.get("known_coverage", 1.0))
            labeled = int(patch.get("labeled", 200))
            labeled_1 = patch.get("labeled_signal_1", 50 if binary else None)
            labeled_0 = patch.get("labeled_signal_0", 50 if binary else None)
            row = {
                "hypothesis_id": spec.hypothesis_id,
                "source": spec.source,
                "year": year,
                "horizon_days": 20,
                "signal_kind": spec.signal_kind,
                "annual_stability_metric": "mean_spread_1_minus_0" if binary else "spearman",
                "eligible": 220,
                "known": 220,
                "unknown": 0,
                "labeled": labeled,
                "known_coverage": coverage,
                "labeled_coverage": labeled / 220,
                "candidate_direction": spec.candidate_direction,
                "mean_raw_return": None,
                "median_raw_return": None,
                "win_rate_raw": None,
                "mean_rel_hs300_return": None,
                "median_rel_hs300_return": None,
                "win_rate_rel_hs300": None,
                "labeled_signal_1": labeled_1,
                "labeled_signal_0": labeled_0,
                "mean_raw_return_signal_1": None,
                "mean_raw_return_signal_0": None,
                "mean_rel_hs300_return_signal_1": None,
                "mean_rel_hs300_return_signal_0": None,
                "mean_raw_return_spread_1_minus_0": None,
                "mean_rel_hs300_return_spread_1_minus_0": year_metric if binary else None,
                "spearman_signal_vs_raw": None,
                "spearman_signal_vs_rel_hs300": None if binary else year_metric,
                "same_sign_2022_2023_raw": None,
                "same_sign_2022_2023_rel_hs300": year_supported,
                "candidate_direction_supported_2022_2023_raw": None,
                "candidate_direction_supported_2022_2023_rel_hs300": year_supported,
            }
            rows.append(row)
    return pl.DataFrame(rows).select(list(SUMMARY_COLUMNS))


def _observations() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "source": "forecast",
                "symbol": "000001.SZ",
                "ann_date": date(2022, 6, 10),
                "available_at": datetime(2022, 6, 10, 15, 59),
                "first_usable_trade_date": date(2022, 6, 13),
                "hypothesis_id": "forecast_bullish_type",
                "threshold_bucket": "bullish_type",
                "signal_value": 1.0,
                "signal_known": True,
                "year": 2022,
                "source_row_hash": "obs-1",
                "fwd_raw_ret_5d": 0.01,
                "fwd_raw_ret_10d": 0.01,
                "fwd_raw_ret_20d": 0.01,
                "fwd_rel_hs300_ret_5d": 0.01,
                "fwd_rel_hs300_ret_10d": 0.01,
                "fwd_rel_hs300_ret_20d": 0.01,
                "label_known_5d": True,
                "label_known_10d": True,
                "label_known_20d": True,
            }
        ]
    ).select(list(OBSERVATION_COLUMNS))


def _write_diagnostic(path: Path, summary: pl.DataFrame) -> EventCandidateDiagnosticReport:
    observations = _observations()
    report = EventCandidateDiagnosticReport(
        strategy_config_hash="796b793856dcd02a",
        market_snapshot_id="ab" * 32,
        event_snapshot_id="cd" * 32,
        event_source_coverage_start=date(2022, 1, 1),
        event_source_coverage_end=date(2024, 12, 31),
        window_start=DEVELOPMENT_WINDOW_START,
        window_end=DEVELOPMENT_WINDOW_END,
        forward_horizons=list(FORWARD_HORIZONS),
        benchmark_symbol="000300.SH",
        hypotheses=list(CANDIDATE_HYPOTHESES),
        observation_rows=observations.height,
        summary_rows=summary.height,
    )
    return write_event_candidate_diagnostics_atomically(path, report, observations, summary)


def _write_freeze(
    freeze_path: Path,
    diagnostic_dir: Path,
    summary: pl.DataFrame | None = None,
) -> tuple[EventCandidateOosFreezeContract, EventCandidateDiagnosticReport]:
    if summary is None:
        summary = _summary_frame()
    report = _write_diagnostic(diagnostic_dir, summary)
    contract = build_event_candidate_oos_freeze(
        report=report,
        summary=summary,
        artifact_dir="tmp/event-candidate-diagnostics/development-2022-2023-v1",
        strategy_config_id="all_a_share_historical_value_portfolio_selected_v2",
    )
    write_event_candidate_oos_freeze(freeze_path, contract)
    return contract, report


def test_gate_nominates_only_transparent_passers() -> None:
    summary = _summary_frame()
    nominations = evaluate_nomination_gate(summary, CANDIDATE_HYPOTHESES)
    assert [item.hypothesis_id for item in nominations] == [
        item.hypothesis_id for item in CANDIDATE_HYPOTHESES
    ]
    passed = [item.hypothesis_id for item in nominations if item.passed]
    assert passed == list(PASSING_HYPOTHESIS_IDS)
    by_id = {item.hypothesis_id: item for item in nominations}
    assert by_id["forecast_bullish_type"].reason == "direction_not_supported"
    assert by_id["forecast_upward_revision"].reason == "passed_primary_20d_rel_hs300_gate"
    assert by_id["audit_non_standard_opinion"].passed is True


def test_gate_records_coverage_labeled_and_arm_failures() -> None:
    summary = _summary_frame(
        passing_ids=PASSING_HYPOTHESIS_IDS,
        overrides={
            "holder_count_decrease:2022": {"known_coverage": 0.84, "supported": True},
            "holder_count_decrease:2023": {"supported": True},
            "forecast_upward_revision:2022": {"labeled": 80, "supported": True},
            "forecast_upward_revision:2023": {"supported": True},
            "audit_non_standard_opinion:2023": {
                "labeled_signal_1": 10,
                "supported": True,
            },
        },
    )
    nominations = {
        item.hypothesis_id: item for item in evaluate_nomination_gate(summary, CANDIDATE_HYPOTHESES)
    }
    holder = nominations["holder_count_decrease"]
    assert holder.passed is False
    assert [item.code for item in holder.failures] == ["known_coverage_below_minimum"]
    assert holder.failures[0].years == ["2022"]
    revision = nominations["forecast_upward_revision"]
    assert revision.passed is False
    assert [item.code for item in revision.failures] == ["labeled_sample_below_minimum"]
    audit = nominations["audit_non_standard_opinion"]
    assert audit.passed is False
    assert [item.code for item in audit.failures] == ["binary_arm_labeled_below_minimum"]
    assert audit.failures[0].years == ["2023"]


def test_gate_fail_closes_when_summary_row_missing() -> None:
    summary = _summary_frame().filter(pl.col("hypothesis_id") != "forecast_bullish_type")
    with pytest.raises(ValueError, match="do not cover the registered hypotheses"):
        evaluate_nomination_gate(summary, CANDIDATE_HYPOTHESES)


def test_verify_accepts_matching_freeze_and_rejects_tampered_id(tmp_path: Path) -> None:
    freeze_path = tmp_path / "freeze.json"
    diagnostic_dir = tmp_path / "diagnostic"
    contract, _ = _write_freeze(freeze_path, diagnostic_dir)
    loaded = verify_event_candidate_oos_freeze(
        freeze_path=freeze_path,
        diagnostic_dir=diagnostic_dir,
    )
    assert loaded.freeze_id == contract.freeze_id
    assert loaded.nominated_hypothesis_ids == list(PASSING_HYPOTHESIS_IDS)
    assert loaded.ready_for_scoring is False
    assert loaded.ready_for_trading is False

    payload = json.loads(freeze_path.read_text(encoding="utf-8"))
    payload["research_boundary"] = "tampered"
    freeze_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="freeze ID does not match"):
        load_verified_event_candidate_oos_freeze(freeze_path)


def test_verify_rejects_internally_inconsistent_nominated_ids(tmp_path: Path) -> None:
    freeze_path = tmp_path / "freeze.json"
    diagnostic_dir = tmp_path / "diagnostic"
    contract, _ = _write_freeze(freeze_path, diagnostic_dir)
    tampered = contract.model_copy(update={"nominated_hypothesis_ids": ["forecast_bullish_type"]})
    write_event_candidate_oos_freeze(freeze_path, build_copy_with_id(tampered))
    with pytest.raises(ValueError, match="nominated hypothesis IDs"):
        load_verified_event_candidate_oos_freeze(freeze_path)


def test_verify_rejects_gate_recalculation_mismatch(tmp_path: Path) -> None:
    freeze_path = tmp_path / "freeze.json"
    diagnostic_dir = tmp_path / "diagnostic"
    contract, _ = _write_freeze(freeze_path, diagnostic_dir)
    summary = _summary_frame(passing_ids=())
    summary.write_parquet(diagnostic_dir / "hypothesis_annual_summary.parquet")
    report_path = diagnostic_dir / "report.json"
    report = EventCandidateDiagnosticReport.model_validate_json(
        report_path.read_text(encoding="utf-8")
    )
    resealed = report.model_copy(
        update={"summary_file_sha256": _sha256_file(diagnostic_dir / report.summary_file)}
    )
    resealed = resealed.model_copy(update={"report_id": _report_id(resealed)})
    report_path.write_text(resealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    rebound = contract.bound_diagnostic.model_copy(
        update={
            "report_id": resealed.report_id,
            "summary_file_sha256": resealed.summary_file_sha256,
        }
    )
    write_event_candidate_oos_freeze(
        freeze_path,
        build_copy_with_id(contract.model_copy(update={"bound_diagnostic": rebound})),
    )
    with pytest.raises(ValueError, match="do not match the sealed 2022/2023 summary gate"):
        verify_event_candidate_oos_freeze(freeze_path=freeze_path, diagnostic_dir=diagnostic_dir)


def test_verify_rejects_bound_report_id_mismatch(tmp_path: Path) -> None:
    freeze_path = tmp_path / "freeze.json"
    diagnostic_dir = tmp_path / "diagnostic"
    contract, _ = _write_freeze(freeze_path, diagnostic_dir)
    bound = contract.bound_diagnostic.model_copy(update={"report_id": "ef" * 32})
    tampered = build_copy_with_id(contract.model_copy(update={"bound_diagnostic": bound}))
    write_event_candidate_oos_freeze(freeze_path, tampered)
    with pytest.raises(ValueError, match="bound report_id"):
        verify_event_candidate_oos_freeze(freeze_path=freeze_path, diagnostic_dir=diagnostic_dir)


def test_cli_verifies_freeze_without_scoring(tmp_path: Path) -> None:
    freeze_path = tmp_path / "freeze.json"
    diagnostic_dir = tmp_path / "diagnostic"
    _write_freeze(freeze_path, diagnostic_dir)
    result = CliRunner().invoke(
        cli_app,
        [
            "verify-a-share-event-candidate-freeze",
            "--freeze-file",
            str(freeze_path),
            "--diagnostic-dir",
            str(diagnostic_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "ready_for_scoring=false" in result.output
    assert "ready_for_trading=false" in result.output
    assert "forecast_upward_revision" in result.output
    assert "audit_non_standard_opinion" in result.output
    assert "one_shot" in result.output


def test_committed_freeze_is_self_consistent() -> None:
    contract = load_verified_event_candidate_oos_freeze(COMMITTED_FREEZE)
    assert contract.freeze_version == EVENT_CANDIDATE_OOS_FREEZE_VERSION
    assert contract.nominated_hypothesis_ids == list(PASSING_HYPOTHESIS_IDS)
    assert contract.nominated_count == 2
    assert contract.registered_hypothesis_count == 11
    assert contract.bound_diagnostic.report_id == (
        "782a042d666600a4383cce72ecff27c2599acbde0acd2b0f2100b164d928bd01"
    )
    assert contract.bound_diagnostic.strategy_config_hash == "796b793856dcd02a"
    assert (
        contract.bound_diagnostic.market_snapshot_id
        == "de546fbbf5a6308a76fbfbd077a918cbbedfb3ad0ca361a24212c1bfe3e06857"
    )
    assert (
        contract.bound_diagnostic.event_snapshot_id
        == "c17c1629c6ed52929846d9e970fb038e0842e2c88d1e950dc4149369d7524d4a"
    )
    assert (
        contract.bound_diagnostic.observation_file_sha256
        == "60447202f4651075a22d9105afb0fbeee7559690a153c12dffb5062b5e81efa3"
    )
    assert (
        contract.bound_diagnostic.summary_file_sha256
        == "4060b7b6e4a0e55c6345235704d4b9f376e6aba0ec2f88d6bc392874206dd4a3"
    )
    assert contract.primary_oos_endpoint.observation_field == "fwd_rel_hs300_ret_20d"
    assert contract.oos_policy.authorized_oos_window == "future_2025_plus_not_yet_authorized"
    assert contract.ready_for_scoring is False
    assert contract.ready_for_trading is False
    passed = [item.hypothesis_id for item in contract.hypothesis_nominations if item.passed]
    assert passed == list(PASSING_HYPOTHESIS_IDS)


@pytest.mark.skipif(
    not (COMMITTED_DIAGNOSTIC / "report.json").is_file(),
    reason="development diagnostic artifacts not present",
)
def test_committed_freeze_matches_sealed_development_diagnostic() -> None:
    contract = verify_event_candidate_oos_freeze(
        freeze_path=COMMITTED_FREEZE,
        diagnostic_dir=COMMITTED_DIAGNOSTIC,
    )
    assert contract.nominated_hypothesis_ids == list(PASSING_HYPOTHESIS_IDS)
