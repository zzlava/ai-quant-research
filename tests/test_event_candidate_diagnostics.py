from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.demo.generator import DEMO_SEED, generate_demo_market, write_demo_parquet
from app.models.events import EventSourceManifest
from app.providers.tushare_event_history import materialize_tushare_event_overlay
from app.providers.tushare_events import EXPRESS_NUMERIC
from app.research.event_candidate_diagnostics import (
    DEVELOPMENT_WINDOW_END,
    DEVELOPMENT_WINDOW_START,
    LABEL_HARD_END,
    build_event_candidate_diagnostics,
    load_verified_event_candidate_diagnostics,
    write_event_candidate_diagnostics_atomically,
)
from app.storage.event_io import load_verified_event_snapshot
from app.storage.snapshot_io import load_verified_snapshot
from tests.helpers import load_test_config


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _market(path: Path) -> Path:
    write_demo_parquet(generate_demo_market(seed=DEMO_SEED), path)
    return path


def _candidate_source_dir(path: Path) -> Path:
    """Offline event fixture inside the sealed 2022-2023 development window."""
    path.mkdir(parents=True)
    forecast = pl.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "ann_date": "20220610",
                "end_date": "20220630",
                "type": "预增",
                "p_change_min": 10.0,
                "p_change_max": 20.0,
                "net_profit_min": 100.0,
                "net_profit_max": 110.0,
                "last_parent_net": 80.0,
                "first_ann_date": "20220610",
                "summary": "initial",
                "change_reason": "operations",
            },
            {
                "ts_code": "000001.SZ",
                "ann_date": "20220617",
                "end_date": "20220630",
                "type": "预增",
                "p_change_min": 25.0,
                "p_change_max": 35.0,
                "net_profit_min": 120.0,
                "net_profit_max": 130.0,
                "last_parent_net": 80.0,
                "first_ann_date": "20220610",
                "summary": "upward revision",
                "change_reason": "operations revised",
            },
            {
                "ts_code": "000002.SZ",
                "ann_date": "20231201",
                "end_date": "20231231",
                "type": "预减",
                "p_change_min": -30.0,
                "p_change_max": -10.0,
                "net_profit_min": 40.0,
                "net_profit_max": 50.0,
                "last_parent_net": 60.0,
                "first_ann_date": "20231201",
                "summary": "year-end label truncation probe",
                "change_reason": "soft demand",
            },
            {
                "ts_code": "000003.SZ",
                "ann_date": "20220701",
                "end_date": "20220630",
                "type": "不确定",
                "p_change_min": None,
                "p_change_max": None,
                "net_profit_min": None,
                "net_profit_max": None,
                "last_parent_net": None,
                "first_ann_date": "20220701",
                "summary": "unknown midpoint",
                "change_reason": None,
            },
        ]
    )
    express_row: dict[str, object] = {
        "ts_code": "000001.SZ",
        "ann_date": "20220801",
        "end_date": "20220630",
        "summary": "express",
    }
    express_row.update({name: float(index + 1) for index, name in enumerate(EXPRESS_NUMERIC)})
    express_row["yoy_net_profit"] = 12.5
    express_missing: dict[str, object] = {
        "ts_code": "000002.SZ",
        "ann_date": "20220808",
        "end_date": "20220630",
        "summary": "express missing yoy",
    }
    express_missing.update({name: float(index + 1) for index, name in enumerate(EXPRESS_NUMERIC)})
    express_missing["yoy_net_profit"] = None
    sources = {
        "forecast": forecast,
        "express": pl.DataFrame([express_row, express_missing]),
        "share_float": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20220901",
                    "float_date": "20220920",
                    "float_share": 1_000_000.0,
                    "float_ratio": 3.0,
                    "holder_name": "holder-a",
                    "share_type": "定向增发机构配售股份",
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20220901",
                    "float_date": "20220925",
                    "float_share": 2_000_000.0,
                    "float_ratio": 4.0,
                    "holder_name": "holder-b",
                    "share_type": "定向增发机构配售股份",
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20220901",
                    "float_date": "20220922",
                    "float_share": 500_000.0,
                    "float_ratio": None,
                    "holder_name": "holder-c",
                    "share_type": "定向增发机构配售股份",
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20221003",
                    "float_date": "20221020",
                    "float_share": 1_000_000.0,
                    "float_ratio": 2.5,
                    "holder_name": "holder-d",
                    "share_type": "定向增发机构配售股份",
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20221003",
                    "float_date": "20221025",
                    "float_share": 1_500_000.0,
                    "float_ratio": 3.5,
                    "holder_name": "holder-e",
                    "share_type": "定向增发机构配售股份",
                },
            ]
        ),
        "stk_holdernumber": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20220603",
                    "end_date": "20220531",
                    "holder_num": 10000,
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20220715",
                    "end_date": "20220630",
                    "holder_num": 8000,
                },
                # Late revision of an earlier end_date announced after the July decision;
                # must not overwrite the PIT-visible prior used by the July observation.
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20220801",
                    "end_date": "20220531",
                    "holder_num": 5000,
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20220815",
                    "end_date": "20220731",
                    "holder_num": 9000,
                },
            ]
        ),
        "fina_audit": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20221010",
                    "end_date": "20211231",
                    "audit_result": "标准无保留意见",
                    "audit_fees": 100.0,
                    "audit_agency": "agency-a",
                    "audit_sign": "auditor-a",
                },
                {
                    "ts_code": "000002.SZ",
                    "ann_date": "20221011",
                    "end_date": "20211231",
                    "audit_result": "保留意见",
                    "audit_fees": 80.0,
                    "audit_agency": "agency-b",
                    "audit_sign": "auditor-b",
                },
            ]
        ),
    }
    filenames = {
        "forecast": "forecast.csv",
        "express": "express.csv",
        "stk_holdernumber": "stk_holdernumber.csv",
        "share_float": "share_float.csv",
        "fina_audit": "fina_audit.csv",
    }
    files: dict[str, dict[str, str]] = {}
    for source, frame in sources.items():
        target = path / filenames[source]
        frame.write_csv(target)
        files[source] = {"path": target.name, "sha256": _sha256(target)}
    evidence = {
        "forecast": "fixture ann_date",
        "express": "fixture ann_date",
        "stk_holdernumber": "fixture ann_date",
        "share_float": "fixture ann_date",
        "fina_audit": "fixture ann_date",
    }
    manifest = {
        "schema_version": "1",
        "source_name": "tushare_offline_fixture",
        "source_version": "candidate-fixture-v1",
        "fetched_at": "2025-01-02T00:00:00Z",
        "coverage_start": "2022-01-01",
        "coverage_end": "2023-12-31",
        "files": files,
        "availability_evidence": evidence,
        "notes": "candidate diagnostic fixture",
    }
    (path / "source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _event_bundle(tmp_path: Path) -> tuple[Path, Path]:
    market = _market(tmp_path / "market")
    event = tmp_path / "event-overlay"
    materialize_tushare_event_overlay(
        source_dir=_candidate_source_dir(tmp_path / "source"),
        market_dir=market,
        dest_dir=event,
    )
    return market, event


def _build(tmp_path: Path):
    market, event = _event_bundle(tmp_path)
    snapshot, tables = load_verified_event_snapshot(event)
    source = EventSourceManifest.model_validate_json(
        (event / "source_manifest.json").read_bytes()
    )
    report, observations, summary = build_event_candidate_diagnostics(
        market_dir=market,
        event_snapshot=snapshot,
        event_source_manifest=source,
        event_tables=tables,
        config=load_test_config(),
        window_start=DEVELOPMENT_WINDOW_START,
        window_end=DEVELOPMENT_WINDOW_END,
    )
    return market, event, report, observations, summary


def test_pit_first_usable_is_next_trading_day_not_announcement_close(tmp_path: Path) -> None:
    _, _, _, observations, _ = _build(tmp_path)
    row = observations.filter(
        (pl.col("hypothesis_id") == "forecast_bullish_type")
        & (pl.col("ann_date") == date(2022, 6, 10))
    ).row(0, named=True)
    assert row["ann_date"] == date(2022, 6, 10)
    assert row["first_usable_trade_date"] == date(2022, 6, 13)
    assert row["first_usable_trade_date"] > row["ann_date"]


def test_year_end_2023_label_truncation_keeps_unknown_not_zero(tmp_path: Path) -> None:
    _, _, _, observations, _ = _build(tmp_path)
    row = observations.filter(
        (pl.col("hypothesis_id") == "forecast_bearish_type")
        & (pl.col("ann_date") == date(2023, 12, 1))
    ).row(0, named=True)
    assert row["first_usable_trade_date"] == date(2023, 12, 4)
    assert row["label_known_20d"] is False
    assert row["fwd_raw_ret_20d"] is None
    assert row["fwd_rel_hs300_ret_20d"] is None


def test_unknown_signal_is_not_filled_with_zero(tmp_path: Path) -> None:
    _, _, _, observations, summary = _build(tmp_path)
    unknown = observations.filter(
        (pl.col("hypothesis_id") == "forecast_p_change_midpoint")
        & (pl.col("symbol") == "000003.SZ")
    )
    assert unknown.height == 1
    assert unknown["signal_known"].item() is False
    assert unknown["signal_value"].item() is None

    express_unknown = observations.filter(
        (pl.col("hypothesis_id") == "express_yoy_net_profit")
        & (pl.col("symbol") == "000002.SZ")
    )
    assert express_unknown.height == 1
    assert express_unknown["signal_known"].item() is False
    assert express_unknown["signal_value"].item() is None

    stats = summary.filter(
        (pl.col("hypothesis_id") == "forecast_p_change_midpoint")
        & (pl.col("year") == "all")
        & (pl.col("horizon_days") == 5)
    ).row(0, named=True)
    assert stats["unknown"] >= 1
    assert stats["known"] + stats["unknown"] == stats["eligible"]


def test_unlock_observations_are_deduped_and_require_all_tranche_ratios(tmp_path: Path) -> None:
    from app.research.event_candidate_diagnostics import _aggregate_source_row_hash

    _, event, _, observations, _ = _build(tmp_path)
    unlock = observations.filter(pl.col("hypothesis_id") == "unlock_announced_pressure_next_30d")
    assert unlock.height == 2

    missing = unlock.filter(pl.col("ann_date") == date(2022, 9, 1)).row(0, named=True)
    assert missing["signal_known"] is False
    assert missing["signal_value"] is None

    known = unlock.filter(pl.col("ann_date") == date(2022, 10, 3)).row(0, named=True)
    assert known["signal_known"] is True
    assert known["signal_value"] == pytest.approx(6.0)

    high = observations.filter(pl.col("hypothesis_id") == "unlock_announced_pressure_high")
    assert high.height == 2
    high_missing = high.filter(pl.col("ann_date") == date(2022, 9, 1)).row(0, named=True)
    assert high_missing["signal_known"] is False
    assert high_missing["signal_value"] is None
    high_known = high.filter(pl.col("ann_date") == date(2022, 10, 3)).row(0, named=True)
    assert high_known["signal_known"] is True
    assert high_known["signal_value"] == pytest.approx(1.0)

    day_rows = pl.read_parquet(event / "share_unlock_events.parquet").filter(
        (pl.col("symbol") == "000001.SZ") & (pl.col("ann_date") == date(2022, 10, 3))
    )
    expected = _aggregate_source_row_hash([str(v) for v in day_rows["source_row_hash"].to_list()])
    assert known["source_row_hash"] == expected
    assert known["source_row_hash"] != min(str(v) for v in day_rows["source_row_hash"].to_list())


def test_window_outside_development_period_is_rejected(tmp_path: Path) -> None:
    market, event = _event_bundle(tmp_path)
    snapshot, tables = load_verified_event_snapshot(event)
    source = EventSourceManifest.model_validate_json(
        (event / "source_manifest.json").read_bytes()
    )
    with pytest.raises(ValueError, match="sealed development window"):
        build_event_candidate_diagnostics(
            market_dir=market,
            event_snapshot=snapshot,
            event_source_manifest=source,
            event_tables=tables,
            config=load_test_config(),
            window_start=date(2022, 1, 1),
            window_end=date(2024, 12, 31),
        )


def test_market_event_snapshot_mismatch_fails_closed(tmp_path: Path) -> None:
    market_a = _market(tmp_path / "market-a")
    market_b = tmp_path / "market-b"
    write_demo_parquet(generate_demo_market(seed=DEMO_SEED + 7), market_b)
    event = tmp_path / "event-overlay"
    materialize_tushare_event_overlay(
        source_dir=_candidate_source_dir(tmp_path / "source"),
        market_dir=market_a,
        dest_dir=event,
    )
    snapshot, tables = load_verified_event_snapshot(event)
    source = EventSourceManifest.model_validate_json(
        (event / "source_manifest.json").read_bytes()
    )
    with pytest.raises(ValueError, match="different market snapshot"):
        build_event_candidate_diagnostics(
            market_dir=market_b,
            event_snapshot=snapshot,
            event_source_manifest=source,
            event_tables=tables,
            config=load_test_config(),
            window_start=DEVELOPMENT_WINDOW_START,
            window_end=DEVELOPMENT_WINDOW_END,
        )


def test_artifact_hashes_reject_tampering_and_output_is_deterministic(tmp_path: Path) -> None:
    _, _, report, observations, summary = _build(tmp_path / "first")
    output = tmp_path / "diagnostic"
    sealed = write_event_candidate_diagnostics_atomically(output, report, observations, summary)
    loaded, loaded_obs, loaded_summary = load_verified_event_candidate_diagnostics(output)
    assert loaded == sealed
    assert loaded_obs.equals(observations, null_equal=True)
    assert loaded_summary.equals(summary, null_equal=True)
    assert sealed.development_only is True
    assert sealed.ready_for_scoring is False
    assert sealed.ready_for_trading is False
    assert sealed.label_hard_end == LABEL_HARD_END

    report_path = output / "report.json"
    tampered = json.loads(report_path.read_text(encoding="utf-8"))
    tampered["observation_rows"] += 1
    report_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="report ID"):
        load_verified_event_candidate_diagnostics(output)
    report_path.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")

    path = output / "observations.parquet"
    pl.read_parquet(path).with_columns(pl.lit(0.0).alias("fwd_raw_ret_5d")).write_parquet(path)
    with pytest.raises(ValueError, match="hash does not match"):
        load_verified_event_candidate_diagnostics(output)

    _, _, report_b, observations_b, summary_b = _build(tmp_path / "second")
    assert observations.equals(observations_b, null_equal=True)
    assert summary.equals(summary_b, null_equal=True)
    assert report.model_dump(exclude={"report_id"}) == report_b.model_dump(exclude={"report_id"})


def test_cli_rejects_non_development_window_and_writes_read_only_artifact(tmp_path: Path) -> None:
    market, event = _event_bundle(tmp_path)
    runner = CliRunner()
    bad = runner.invoke(
        cli_app,
        [
            "diagnose-a-share-event-candidates",
            "--strategy",
            "baseline_v1",
            "--start",
            "2022-01-01",
            "--end",
            "2024-12-31",
            "--market-dir",
            str(market),
            "--event-dir",
            str(event),
            "--output-dir",
            str(tmp_path / "bad"),
        ],
    )
    assert bad.exit_code != 0
    assert "2022-01-01..2023-12-31" in bad.output

    output = tmp_path / "diagnostic"
    good = runner.invoke(
        cli_app,
        [
            "diagnose-a-share-event-candidates",
            "--strategy",
            "baseline_v1",
            "--start",
            "2022-01-01",
            "--end",
            "2023-12-31",
            "--market-dir",
            str(market),
            "--event-dir",
            str(event),
            "--output-dir",
            str(output),
        ],
    )
    assert good.exit_code == 0, good.output
    assert "ready_for_scoring=false" in good.output
    assert "ready_for_trading=false" in good.output
    assert "development_only=true" in good.output
    assert (output / "report.json").is_file()
    assert (output / "observations.parquet").is_file()
    assert (output / "hypothesis_annual_summary.parquet").is_file()
    market_snap = load_verified_snapshot(market)
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["market_snapshot_id"] == market_snap.snapshot_id
    assert report["window_start"] == "2022-01-01"
    assert report["window_end"] == "2023-12-31"
    obs = pl.read_parquet(output / "observations.parquet")
    assert obs.filter(pl.col("first_usable_trade_date") > LABEL_HARD_END).height == 0
    late = obs.filter(pl.col("ann_date") == date(2023, 12, 1))
    assert late.height >= 1
    assert late.filter(pl.col("label_known_20d")).height == 0


def test_all_declared_hypotheses_appear_in_summary(tmp_path: Path) -> None:
    _, _, report, _, summary = _build(tmp_path)
    declared = {item.hypothesis_id for item in report.hypotheses}
    observed = set(summary["hypothesis_id"].unique().to_list())
    assert declared == observed
    assert len(declared) == 11


def test_binary_hypotheses_emit_full_eligible_zero_one_and_unknown(tmp_path: Path) -> None:
    _, _, _, observations, summary = _build(tmp_path)

    bullish = observations.filter(pl.col("hypothesis_id") == "forecast_bullish_type")
    assert set(bullish["signal_value"].drop_nulls().to_list()) == {0.0, 1.0}
    assert bullish.filter(pl.col("signal_value") == 1.0).height >= 1
    assert bullish.filter(pl.col("signal_value") == 0.0).height >= 1
    # 不确定 is known and coded as not-bullish (0), not dropped.
    uncertain = bullish.filter(pl.col("symbol") == "000003.SZ").row(0, named=True)
    assert uncertain["signal_known"] is True
    assert uncertain["signal_value"] == pytest.approx(0.0)

    revision = observations.filter(pl.col("hypothesis_id") == "forecast_upward_revision")
    assert revision.height >= 1
    assert set(revision["signal_value"].drop_nulls().to_list()) <= {0.0, 1.0}

    express_pos = observations.filter(pl.col("hypothesis_id") == "express_yoy_net_profit_positive")
    assert express_pos.filter(pl.col("signal_known").not_()).height >= 1
    assert express_pos.filter(pl.col("signal_value") == 0.0).height + express_pos.filter(
        pl.col("signal_value") == 1.0
    ).height == express_pos.filter(pl.col("signal_known")).height

    decrease = observations.filter(pl.col("hypothesis_id") == "holder_count_decrease")
    assert decrease.filter(pl.col("signal_value") == 0.0).height >= 1
    assert decrease.filter(pl.col("signal_value") == 1.0).height >= 1

    audit = observations.filter(pl.col("hypothesis_id") == "audit_non_standard_opinion")
    assert audit.height == 2
    assert set(audit["signal_value"].to_list()) == {0.0, 1.0}

    binary_summary = summary.filter(
        (pl.col("signal_kind") == "binary_bucket")
        & (pl.col("year") == "all")
        & (pl.col("horizon_days") == 5)
        & (pl.col("hypothesis_id") == "forecast_bullish_type")
    ).row(0, named=True)
    assert binary_summary["mean_raw_return"] is None
    assert binary_summary["labeled_signal_1"] is not None
    assert binary_summary["labeled_signal_0"] is not None
    assert binary_summary["mean_raw_return_spread_1_minus_0"] is not None


def test_holder_prior_ignores_future_revision_of_earlier_end_date(tmp_path: Path) -> None:
    _, _, _, observations, _ = _build(tmp_path)
    july = observations.filter(
        (pl.col("hypothesis_id") == "holder_count_change_pct")
        & (pl.col("ann_date") == date(2022, 7, 15))
    ).row(0, named=True)
    # Visible prior is 10000 (announced 2022-06-03), not the later 5000 revision.
    assert july["signal_known"] is True
    assert july["signal_value"] == pytest.approx((8000 - 10000) / 10000)

    august = observations.filter(
        (pl.col("hypothesis_id") == "holder_count_change_pct")
        & (pl.col("ann_date") == date(2022, 8, 15))
    ).row(0, named=True)
    assert august["signal_known"] is True
    assert august["signal_value"] == pytest.approx((9000 - 8000) / 8000)


def test_same_sign_is_computed_per_horizon_not_copied_from_20d() -> None:
    from datetime import datetime

    from app.research.event_candidate_diagnostics import OBSERVATION_COLUMNS, _build_summary

    def _row(
        *,
        year: int,
        signal: float,
        raw5: float,
        raw20: float,
        rel5: float,
        rel20: float,
        idx: int,
    ) -> dict[str, object]:
        return {
            "source": "forecast",
            "symbol": "000001.SZ",
            "ann_date": date(year, 6, 10),
            "available_at": datetime(year, 6, 10, 15, 59),
            "first_usable_trade_date": date(year, 6, 13),
            "hypothesis_id": "forecast_p_change_midpoint",
            "threshold_bucket": "p_change_midpoint",
            "signal_value": signal,
            "signal_known": True,
            "year": year,
            "source_row_hash": f"hash-{year}-{idx}",
            "fwd_raw_ret_5d": raw5,
            "fwd_raw_ret_10d": 0.01,
            "fwd_raw_ret_20d": raw20,
            "fwd_rel_hs300_ret_5d": rel5,
            "fwd_rel_hs300_ret_10d": 0.01,
            "fwd_rel_hs300_ret_20d": rel20,
            "label_known_5d": True,
            "label_known_10d": True,
            "label_known_20d": True,
        }

    # Continuous stability uses Spearman, not mean return sign.
    # 5d: both years have positive Spearman (signal ranks with returns).
    # 20d: 2022 negative Spearman, 2023 positive Spearman, while both years' mean
    # returns stay positive — so a mean-based same_sign would wrongly be True.
    frame = pl.DataFrame(
        [
            _row(year=2022, signal=1.0, raw5=0.01, raw20=0.06, rel5=0.01, rel20=0.06, idx=1),
            _row(year=2022, signal=2.0, raw5=0.02, raw20=0.04, rel5=0.02, rel20=0.04, idx=2),
            _row(year=2022, signal=3.0, raw5=0.03, raw20=0.02, rel5=0.03, rel20=0.02, idx=3),
            _row(year=2023, signal=1.0, raw5=0.02, raw20=0.01, rel5=0.02, rel20=0.01, idx=4),
            _row(year=2023, signal=2.0, raw5=0.03, raw20=0.03, rel5=0.03, rel20=0.03, idx=5),
            _row(year=2023, signal=3.0, raw5=0.04, raw20=0.05, rel5=0.04, rel20=0.05, idx=6),
        ]
    ).select(list(OBSERVATION_COLUMNS))
    summary = _build_summary(frame)
    mid = summary.filter(
        (pl.col("hypothesis_id") == "forecast_p_change_midpoint") & (pl.col("year") == "all")
    )
    row5 = mid.filter(pl.col("horizon_days") == 5).row(0, named=True)
    row20 = mid.filter(pl.col("horizon_days") == 20).row(0, named=True)
    assert row5["annual_stability_metric"] == "spearman"
    assert row20["annual_stability_metric"] == "spearman"
    assert row5["same_sign_2022_2023_raw"] is True
    assert row20["same_sign_2022_2023_raw"] is False
    assert row5["same_sign_2022_2023_rel_hs300"] is True
    assert row20["same_sign_2022_2023_rel_hs300"] is False
    # Declared direction is positive; 5d Spearman support true, 20d false.
    assert row5["candidate_direction_supported_2022_2023_raw"] is True
    assert row20["candidate_direction_supported_2022_2023_raw"] is False

    # Guard: annual means stay same-sign for 20d, proving same_sign is not mean-based.
    y22 = summary.filter(
        (pl.col("hypothesis_id") == "forecast_p_change_midpoint")
        & (pl.col("year") == "2022")
        & (pl.col("horizon_days") == 20)
    ).row(0, named=True)
    y23 = summary.filter(
        (pl.col("hypothesis_id") == "forecast_p_change_midpoint")
        & (pl.col("year") == "2023")
        & (pl.col("horizon_days") == 20)
    ).row(0, named=True)
    assert y22["mean_raw_return"] is not None and y23["mean_raw_return"] is not None
    assert y22["mean_raw_return"] > 0 and y23["mean_raw_return"] > 0
    assert y22["spearman_signal_vs_raw"] is not None and y23["spearman_signal_vs_raw"] is not None
    assert (y22["spearman_signal_vs_raw"] > 0) != (y23["spearman_signal_vs_raw"] > 0)


def test_binary_same_sign_uses_spread_and_reports_stability_metric(tmp_path: Path) -> None:
    _, _, _, _, summary = _build(tmp_path)
    row = summary.filter(
        (pl.col("hypothesis_id") == "forecast_bullish_type")
        & (pl.col("year") == "all")
        & (pl.col("horizon_days") == 5)
    ).row(0, named=True)
    assert row["annual_stability_metric"] == "mean_spread_1_minus_0"
    assert "candidate_direction_supported_2022_2023_raw" in row
