from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.demo.generator import DEMO_SEED, INDEX_CSI300, generate_demo_market, write_demo_parquet
from app.models.events import EventSourceManifest
from app.providers.tushare_event_history import materialize_tushare_event_overlay
from app.providers.tushare_events import EXPRESS_NUMERIC
from app.research.event_candidate_diagnostics import (
    DEVELOPMENT_WINDOW_END,
    DEVELOPMENT_WINDOW_START,
    OBSERVATION_COLUMNS,
    PREDECLARED_HYPOTHESIS_IDS,
    _build_observations,
    build_event_candidate_diagnostics,
)
from app.research.event_candidate_freeze import (
    DEFAULT_EVENT_CANDIDATE_OOS_FREEZE_PATH,
    PRIMARY_OOS_ENDPOINT,
    load_verified_event_candidate_oos_freeze,
)
from app.research.event_candidate_oos_authorization import (
    AUTHORIZED_CANDIDATES,
    AUTHORIZED_FREEZE_ID,
    DEFAULT_EVENT_CANDIDATE_OOS_AUTH_PATH,
    AuthorizedEvaluationGates,
    AuthorizedOosWindows,
    EventCandidateOosOneShotAuthorization,
    assert_authorization_paths_unused,
    assert_committed_authorization_bindings,
    build_committed_event_candidate_oos_authorization,
    load_verified_committed_event_candidate_oos_authorization,
    load_verified_event_candidate_oos_authorization,
    seal_authorization,
    verify_authorization_against_freeze,
    write_event_candidate_oos_authorization,
)
from app.research.event_candidate_oos_evaluation import (
    CANDIDATE_SUMMARY_COLUMNS,
    _build_candidate_summary,
    _count_incomplete_horizon_20d_rows,
    decide_oos_outcome,
    evaluate_and_write_event_candidate_oos_one_shot,
    load_verified_consumption_receipt,
    load_verified_event_candidate_oos_evaluation,
    write_event_candidate_oos_evaluation_atomically,
)
from app.storage.event_io import load_verified_event_snapshot
from app.storage.hashing import build_snapshot
from app.storage.import_market import write_snapshot_atomically
from app.storage.snapshot_io import load_verified_snapshot, read_tables
from app.strategies.loader import load_strategy_config
from tests.helpers import PROJECT_ROOT, load_test_config

COMMITTED_AUTH = PROJECT_ROOT / DEFAULT_EVENT_CANDIDATE_OOS_AUTH_PATH
COMMITTED_FREEZE = PROJECT_ROOT / DEFAULT_EVENT_CANDIDATE_OOS_FREEZE_PATH
STRATEGY_ID = "all_a_share_historical_value_portfolio_selected_v2"
AUTHORIZED_LABEL_HARD_END = date(2026, 8, 21)


def _weekday_trading_calendar(
    start: date = date(2025, 1, 1),
    end: date = date(2026, 12, 31),
) -> tuple[list[date], dict[date, int]]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days, {day: idx for idx, day in enumerate(days)}


def _summary_calendar() -> tuple[list[date], dict[date, int], date]:
    trading_days, day_index = _weekday_trading_calendar()
    return trading_days, day_index, AUTHORIZED_LABEL_HARD_END


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _oos_market(path: Path, *, end: date = date(2026, 8, 31), seed: int = DEMO_SEED) -> Path:
    bundle = generate_demo_market(
        seed=seed,
        n_stocks=8,
        start=date(2024, 10, 1),
        end=end,
    )
    remapped_index = [
        bar.model_copy(update={"symbol": "000300.SH"}) if bar.symbol == INDEX_CSI300 else bar
        for bar in bundle.index_bars
    ]
    remapped_instruments = [
        item.model_copy(update={"symbol": "000300.SH"})
        if item.symbol == INDEX_CSI300
        else item
        for item in bundle.instruments
    ]
    bundle = bundle.model_copy(
        update={"index_bars": remapped_index, "instruments": remapped_instruments}
    )
    from app.providers._frames import bars_to_frame, global_to_frame, instruments_to_frame
    from app.storage.quality import (
        validate_calendar,
        validate_global,
        validate_instruments,
        validate_ohlcv,
        validate_universe_membership,
    )
    from app.universe.membership import build_manual_static_membership

    daily = bars_to_frame(bundle.daily_bars)
    index = bars_to_frame(bundle.index_bars)
    glob = global_to_frame(bundle.global_bars)
    instruments = instruments_to_frame(bundle.instruments)
    calendar = pl.DataFrame({"date": bundle.calendar}).with_columns(pl.col("date").cast(pl.Date))
    stocks = [item.symbol for item in bundle.instruments if not item.is_index and not item.is_global]
    membership = build_manual_static_membership(stocks, bundle.calendar, universe_id="demo")
    validate_ohlcv(daily, "daily_bars")
    validate_ohlcv(index, "index_bars")
    validate_global(glob)
    validate_instruments(instruments)
    validate_calendar(calendar)
    validate_universe_membership(membership, bundle.calendar, instruments, universe_id="demo")
    tables = {
        "daily_bars": daily,
        "index_bars": index,
        "global_bars": glob,
        "instruments": instruments,
        "calendar": calendar,
        "universe_membership": membership,
    }
    snapshot = build_snapshot(
        tables,
        adjustment=bundle.adjustment,
        source_name="demo",
        source_version="oos-fixture",
        market_index="000300.SH",
        global_symbol="GLB_SPX",
    )
    write_snapshot_atomically(Path(path), tables, snapshot)
    return path


def _oos_source_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    forecast = pl.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "ann_date": "20241220",
                "end_date": "20241231",
                "type": "预增",
                "p_change_min": 10.0,
                "p_change_max": 20.0,
                "net_profit_min": 100.0,
                "net_profit_max": 110.0,
                "last_parent_net": 80.0,
                "first_ann_date": "20241220",
                "summary": "pre-window prior",
                "change_reason": "operations",
            },
            {
                "ts_code": "000001.SZ",
                "ann_date": "20250103",
                "end_date": "20241231",
                "type": "预增",
                "p_change_min": 30.0,
                "p_change_max": 40.0,
                "net_profit_min": 120.0,
                "net_profit_max": 130.0,
                "last_parent_net": 80.0,
                "first_ann_date": "20241220",
                "summary": "upward revision in OOS window",
                "change_reason": "operations revised",
            },
            {
                "ts_code": "000002.SZ",
                "ann_date": "20250103",
                "end_date": "20241231",
                "type": "预增",
                "p_change_min": 10.0,
                "p_change_max": 20.0,
                "net_profit_min": 100.0,
                "net_profit_max": 110.0,
                "last_parent_net": 80.0,
                "first_ann_date": "20250103",
                "summary": "prior mid known",
                "change_reason": "operations",
            },
            {
                "ts_code": "000002.SZ",
                "ann_date": "20250110",
                "end_date": "20241231",
                "type": "不确定",
                "p_change_min": None,
                "p_change_max": None,
                "net_profit_min": None,
                "net_profit_max": None,
                "last_parent_net": None,
                "first_ann_date": "20250103",
                "summary": "unknown midpoint revision",
                "change_reason": None,
            },
            {
                "ts_code": "000001.SZ",
                "ann_date": "20260722",
                "end_date": "20260630",
                "type": "预增",
                "p_change_min": 1.0,
                "p_change_max": 2.0,
                "net_profit_min": 80.0,
                "net_profit_max": 85.0,
                "last_parent_net": 70.0,
                "first_ann_date": "20260722",
                "summary": "prior for last-day revision",
                "change_reason": "operations",
            },
            {
                "ts_code": "000001.SZ",
                "ann_date": "20260723",
                "end_date": "20260630",
                "type": "预增",
                "p_change_min": 8.0,
                "p_change_max": 12.0,
                "net_profit_min": 95.0,
                "net_profit_max": 105.0,
                "last_parent_net": 70.0,
                "first_ann_date": "20260722",
                "summary": "revision on last announcement day",
                "change_reason": "operations revised",
            },
        ]
    )
    express_row: dict[str, object] = {
        "ts_code": "000001.SZ",
        "ann_date": "20250203",
        "end_date": "20241231",
        "summary": "express",
    }
    express_row.update({name: float(index + 1) for index, name in enumerate(EXPRESS_NUMERIC)})
    express_row["yoy_net_profit"] = 12.5
    sources = {
        "forecast": forecast,
        "express": pl.DataFrame([express_row]),
        "share_float": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20250303",
                    "float_date": "20250320",
                    "float_share": 1_000_000.0,
                    "float_ratio": 2.0,
                    "holder_name": "holder-a",
                    "share_type": "定向增发机构配售股份",
                }
            ]
        ),
        "stk_holdernumber": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20250106",
                    "end_date": "20241231",
                    "holder_num": 10000,
                },
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20250210",
                    "end_date": "20250131",
                    "holder_num": 8000,
                },
            ]
        ),
        "fina_audit": pl.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20250410",
                    "end_date": "20241231",
                    "audit_result": "标准无保留意见",
                    "audit_fees": 100.0,
                    "audit_agency": "agency-a",
                    "audit_sign": "auditor-a",
                },
                {
                    "ts_code": "000002.SZ",
                    "ann_date": "20250411",
                    "end_date": "20241231",
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
    evidence = {name: "fixture ann_date" for name in filenames}
    manifest = {
        "schema_version": "1",
        "source_name": "tushare_offline_fixture",
        "source_version": "oos-candidate-fixture-v1",
        "fetched_at": "2026-08-22T00:00:00Z",
        "coverage_start": "2024-10-08",
        "coverage_end": "2026-08-21",
        "files": files,
        "availability_evidence": evidence,
        "notes": "synthetic OOS candidate fixture",
    }
    (path / "source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _strategy_config():
    return load_strategy_config(STRATEGY_ID, PROJECT_ROOT / "config" / "strategies")


def _auth_for_bundle(
    *,
    market_snapshot_id: str,
    event_snapshot_id: str,
    first_2025_trading_day: date,
) -> EventCandidateOosOneShotAuthorization:
    freeze = load_verified_event_candidate_oos_freeze(COMMITTED_FREEZE)
    contract = EventCandidateOosOneShotAuthorization(
        authorization_date=date(2026, 8, 25),
        freeze_file=str(DEFAULT_EVENT_CANDIDATE_OOS_FREEZE_PATH),
        freeze_id=freeze.freeze_id,
        strategy_config_id=STRATEGY_ID,
        strategy_config_hash=freeze.bound_diagnostic.strategy_config_hash,
        market_dir="market",
        market_snapshot_id=market_snapshot_id,
        event_dir="events",
        event_snapshot_id=event_snapshot_id,
        base_market_snapshot_id=market_snapshot_id,
        windows=AuthorizedOosWindows(
            announcement_start=date(2025, 1, 1),
            announcement_end=date(2026, 7, 23),
            first_2025_trading_day=first_2025_trading_day,
            last_complete_label_entry_date=date(2026, 7, 24),
            label_hard_end=date(2026, 8, 21),
            event_source_coverage_start=date(2024, 10, 8),
            event_source_coverage_end=date(2026, 8, 21),
        ),
        primary_endpoint=PRIMARY_OOS_ENDPOINT,
        nominated_candidates=list(AUTHORIZED_CANDIDATES),
        nominated_hypothesis_ids=[item.hypothesis_id for item in AUTHORIZED_CANDIDATES],
        evaluation_gates=AuthorizedEvaluationGates(),
        output_dir="out/one-shot-v1",
        consumption_receipt_path="out/one-shot-v1.consumption-receipt.json",
    )
    return seal_authorization(contract)


def _prepare_bundle(tmp_path: Path):
    market = _oos_market(tmp_path / "market")
    event = tmp_path / "events"
    materialize_tushare_event_overlay(
        source_dir=_oos_source_dir(tmp_path / "source"),
        market_dir=market,
        dest_dir=event,
    )
    market_snap = load_verified_snapshot(market)
    event_snap, _ = load_verified_event_snapshot(event)
    calendar = read_tables(market)["calendar"]["date"].to_list()
    first_2025 = next(day for day in sorted(calendar) if day >= date(2025, 1, 1))
    auth = _auth_for_bundle(
        market_snapshot_id=market_snap.snapshot_id,
        event_snapshot_id=event_snap.snapshot_id,
        first_2025_trading_day=first_2025,
    )
    write_event_candidate_oos_authorization(tmp_path / "authorization.json", auth)
    return market, event, auth


def _obs_row(
    *,
    hypothesis_id: str,
    signal_value: float | None,
    signal_known: bool,
    rel20: float | None,
    label_known: bool,
    ann_date: date = date(2025, 3, 3),
    symbol: str = "000001.SZ",
) -> dict[str, object]:
    return {
        "source": "forecast" if "forecast" in hypothesis_id else "fina_audit",
        "symbol": symbol,
        "ann_date": ann_date,
        "available_at": datetime(2025, 3, 3, 15, 59),
        "first_usable_trade_date": date(2025, 3, 4),
        "hypothesis_id": hypothesis_id,
        "threshold_bucket": (
            "upward_revision" if hypothesis_id == "forecast_upward_revision" else "non_standard_opinion"
        ),
        "signal_value": signal_value,
        "signal_known": signal_known,
        "year": ann_date.year,
        "source_row_hash": f"{hypothesis_id}-{symbol}-{ann_date.isoformat()}-{signal_value}",
        "fwd_raw_ret_5d": rel20,
        "fwd_raw_ret_10d": rel20,
        "fwd_raw_ret_20d": rel20,
        "fwd_rel_hs300_ret_5d": rel20,
        "fwd_rel_hs300_ret_10d": rel20,
        "fwd_rel_hs300_ret_20d": rel20,
        "label_known_5d": label_known,
        "label_known_10d": label_known,
        "label_known_20d": label_known,
    }


def _binary_frame(
    hypothesis_id: str,
    *,
    n1: int,
    n0: int,
    mean1: float,
    mean0: float,
    unknown: int = 0,
    incomplete: int = 0,
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for index in range(n1):
        rows.append(
            _obs_row(
                hypothesis_id=hypothesis_id,
                signal_value=1.0,
                signal_known=True,
                rel20=mean1,
                label_known=True,
                symbol=f"{index + 1:06d}.SZ",
            )
        )
    for index in range(n0):
        rows.append(
            _obs_row(
                hypothesis_id=hypothesis_id,
                signal_value=0.0,
                signal_known=True,
                rel20=mean0,
                label_known=True,
                symbol=f"{index + 1001:06d}.SZ",
            )
        )
    for index in range(unknown):
        rows.append(
            _obs_row(
                hypothesis_id=hypothesis_id,
                signal_value=None,
                signal_known=False,
                rel20=None,
                label_known=False,
                symbol=f"{index + 2001:06d}.SZ",
            )
        )
    for index in range(incomplete):
        rows.append(
            _obs_row(
                hypothesis_id=hypothesis_id,
                signal_value=1.0,
                signal_known=True,
                rel20=None,
                label_known=False,
                symbol=f"{index + 3001:06d}.SZ",
            )
        )
    return pl.DataFrame(rows).select(list(OBSERVATION_COLUMNS))


def test_committed_authorization_is_self_hashed_and_matches_freeze() -> None:
    contract = load_verified_committed_event_candidate_oos_authorization(COMMITTED_AUTH)
    assert_committed_authorization_bindings(contract)
    verify_authorization_against_freeze(contract, freeze_path=COMMITTED_FREEZE)
    assert contract.freeze_id == AUTHORIZED_FREEZE_ID
    assert contract.one_shot is True
    assert contract.consumed is False
    assert contract.ready_for_scoring is False
    assert contract.ready_for_trading is False
    assert contract.auto_deploy is False
    assert contract.human_review_required is True
    assert contract.nominated_hypothesis_ids == [
        "forecast_upward_revision",
        "audit_non_standard_opinion",
    ]
    rebuilt = build_committed_event_candidate_oos_authorization()
    assert rebuilt.authorization_id == contract.authorization_id


def test_tampered_authorization_hash_is_rejected(tmp_path: Path) -> None:
    auth = build_committed_event_candidate_oos_authorization()
    path = tmp_path / "auth.json"
    write_event_candidate_oos_authorization(path, auth)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["consumed"] = False
    payload["authorization_id"] = "ab" * 32
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="authorization ID does not match"):
        load_verified_event_candidate_oos_authorization(path)


def test_decide_oos_outcome_gates_and_directions() -> None:
    assert (
        decide_oos_outcome(
            candidate_direction="positive",
            known_coverage=0.95,
            labeled=120,
            labeled_signal_1=40,
            labeled_signal_0=40,
            primary_effect=0.02,
            incomplete_20d_label=False,
            min_known_coverage=0.9,
            min_labeled=100,
            min_binary_arm_labeled=20,
        )
        == "direction_replicated"
    )
    assert (
        decide_oos_outcome(
            candidate_direction="positive",
            known_coverage=0.95,
            labeled=120,
            labeled_signal_1=40,
            labeled_signal_0=40,
            primary_effect=-0.02,
            incomplete_20d_label=False,
            min_known_coverage=0.9,
            min_labeled=100,
            min_binary_arm_labeled=20,
        )
        == "direction_failed"
    )
    assert (
        decide_oos_outcome(
            candidate_direction="negative",
            known_coverage=0.95,
            labeled=120,
            labeled_signal_1=40,
            labeled_signal_0=40,
            primary_effect=-0.02,
            incomplete_20d_label=False,
            min_known_coverage=0.9,
            min_labeled=100,
            min_binary_arm_labeled=20,
        )
        == "direction_replicated"
    )
    assert (
        decide_oos_outcome(
            candidate_direction="negative",
            known_coverage=0.95,
            labeled=120,
            labeled_signal_1=40,
            labeled_signal_0=40,
            primary_effect=0.02,
            incomplete_20d_label=False,
            min_known_coverage=0.9,
            min_labeled=100,
            min_binary_arm_labeled=20,
        )
        == "direction_failed"
    )
    assert (
        decide_oos_outcome(
            candidate_direction="positive",
            known_coverage=0.80,
            labeled=120,
            labeled_signal_1=40,
            labeled_signal_0=40,
            primary_effect=0.02,
            incomplete_20d_label=False,
            min_known_coverage=0.9,
            min_labeled=100,
            min_binary_arm_labeled=20,
        )
        == "not_evaluable"
    )
    assert (
        decide_oos_outcome(
            candidate_direction="positive",
            known_coverage=0.95,
            labeled=80,
            labeled_signal_1=40,
            labeled_signal_0=40,
            primary_effect=0.02,
            incomplete_20d_label=False,
            min_known_coverage=0.9,
            min_labeled=100,
            min_binary_arm_labeled=20,
        )
        == "not_evaluable"
    )
    assert (
        decide_oos_outcome(
            candidate_direction="positive",
            known_coverage=0.95,
            labeled=120,
            labeled_signal_1=10,
            labeled_signal_0=40,
            primary_effect=0.02,
            incomplete_20d_label=False,
            min_known_coverage=0.9,
            min_labeled=100,
            min_binary_arm_labeled=20,
        )
        == "not_evaluable"
    )
    assert (
        decide_oos_outcome(
            candidate_direction="positive",
            known_coverage=0.95,
            labeled=120,
            labeled_signal_1=40,
            labeled_signal_0=40,
            primary_effect=None,
            incomplete_20d_label=False,
            min_known_coverage=0.9,
            min_labeled=100,
            min_binary_arm_labeled=20,
        )
        == "not_evaluable"
    )
    assert (
        decide_oos_outcome(
            candidate_direction="positive",
            known_coverage=0.95,
            labeled=120,
            labeled_signal_1=40,
            labeled_signal_0=40,
            primary_effect=0.02,
            incomplete_20d_label=True,
            min_known_coverage=0.9,
            min_labeled=100,
            min_binary_arm_labeled=20,
        )
        == "not_evaluable"
    )


def test_summary_outcomes_for_positive_and_negative_candidates() -> None:
    trading_days, day_index, label_hard_end = _summary_calendar()
    positive = _binary_frame(
        "forecast_upward_revision", n1=50, n0=50, mean1=0.05, mean0=0.01, unknown=2
    )
    negative = _binary_frame(
        "audit_non_standard_opinion", n1=50, n0=50, mean1=-0.04, mean0=0.01, unknown=2
    )
    observations = pl.concat([positive, negative], how="vertical_relaxed")
    summary = _build_candidate_summary(
        observations,
        candidates=list(AUTHORIZED_CANDIDATES),
        gates=AuthorizedEvaluationGates(),
        trading_days=trading_days,
        day_index=day_index,
        label_hard_end=label_hard_end,
    )
    by_id = {row["hypothesis_id"]: row for row in summary.iter_rows(named=True)}
    assert by_id["forecast_upward_revision"]["outcome"] == "direction_replicated"
    assert by_id["audit_non_standard_opinion"]["outcome"] == "direction_replicated"
    assert tuple(summary.columns) == CANDIDATE_SUMMARY_COLUMNS

    failed_positive = _binary_frame(
        "forecast_upward_revision", n1=50, n0=50, mean1=-0.05, mean0=0.01
    )
    failed_summary = _build_candidate_summary(
        failed_positive,
        candidates=[AUTHORIZED_CANDIDATES[0]],
        gates=AuthorizedEvaluationGates(),
        trading_days=trading_days,
        day_index=day_index,
        label_hard_end=label_hard_end,
    )
    assert failed_summary["outcome"].item() == "direction_failed"

    missing_price_only = _binary_frame(
        "forecast_upward_revision", n1=50, n0=50, mean1=0.05, mean0=0.01, incomplete=5
    )
    missing_price_summary = _build_candidate_summary(
        missing_price_only,
        candidates=[AUTHORIZED_CANDIDATES[0]],
        gates=AuthorizedEvaluationGates(),
        trading_days=trading_days,
        day_index=day_index,
        label_hard_end=label_hard_end,
    )
    assert missing_price_summary["incomplete_20d_label"].item() is False
    assert missing_price_summary["incomplete_20d_label_rows"].item() == 0
    assert missing_price_summary["outcome"].item() == "direction_replicated"


def test_missing_price_alone_does_not_set_incomplete_horizon_label() -> None:
    trading_days, day_index, label_hard_end = _summary_calendar()
    observations = _binary_frame(
        "forecast_upward_revision",
        n1=50,
        n0=50,
        mean1=0.05,
        mean0=0.01,
        incomplete=10,
    )
    summary = _build_candidate_summary(
        observations,
        candidates=[AUTHORIZED_CANDIDATES[0]],
        gates=AuthorizedEvaluationGates(),
        trading_days=trading_days,
        day_index=day_index,
        label_hard_end=label_hard_end,
    )
    row = summary.row(0, named=True)
    assert row["incomplete_20d_label"] is False
    assert row["incomplete_20d_label_rows"] == 0
    assert row["known"] > row["labeled"]
    assert row["outcome"] == "direction_replicated"


def test_horizon_cutoff_sets_incomplete_and_not_evaluable() -> None:
    trading_days, day_index, label_hard_end = _summary_calendar()
    late_entry = date(2026, 8, 15)
    labeled_rows = [
        _obs_row(
            hypothesis_id="forecast_upward_revision",
            signal_value=1.0 if index % 2 == 0 else 0.0,
            signal_known=True,
            rel20=0.02 if index % 2 == 0 else -0.01,
            label_known=True,
            symbol=f"{index + 10:06d}.SZ",
        )
        for index in range(120)
    ]
    late_row = _obs_row(
        hypothesis_id="forecast_upward_revision",
        signal_value=1.0,
        signal_known=True,
        rel20=None,
        label_known=False,
        ann_date=late_entry,
        symbol="000001.SZ",
    )
    late_row["first_usable_trade_date"] = late_entry
    observations = pl.DataFrame([*labeled_rows, late_row]).select(list(OBSERVATION_COLUMNS))
    assert (
        _count_incomplete_horizon_20d_rows(
            observations,
            day_index=day_index,
            trading_days=trading_days,
            label_hard_end=label_hard_end,
        )
        == 1
    )
    summary = _build_candidate_summary(
        observations,
        candidates=[AUTHORIZED_CANDIDATES[0]],
        gates=AuthorizedEvaluationGates(),
        trading_days=trading_days,
        day_index=day_index,
        label_hard_end=label_hard_end,
    )
    assert summary["incomplete_20d_label"].item() is True
    assert summary["incomplete_20d_label_rows"].item() == 1
    assert summary["outcome"].item() == "not_evaluable"


def test_pit_binary_unknown_and_entry_end_semantics(tmp_path: Path) -> None:
    market, event, auth = _prepare_bundle(tmp_path)
    report, receipt, output = evaluate_and_write_event_candidate_oos_one_shot(
        authorization=auth,
        freeze_path=COMMITTED_FREEZE,
        market_dir=market,
        event_dir=event,
        config=_strategy_config(),
        strategy_config_id=STRATEGY_ID,
        root=tmp_path,
    )
    loaded, observations, summary = load_verified_event_candidate_oos_evaluation(output)
    assert loaded.report_id == report.report_id
    assert receipt.receipt_id is not None
    assert set(observations["hypothesis_id"].unique().to_list()) <= set(
        auth.nominated_hypothesis_ids
    )
    assert observations.filter(pl.col("ann_date") < date(2025, 1, 1)).height == 0

    revision = observations.filter(
        (pl.col("hypothesis_id") == "forecast_upward_revision")
        & (pl.col("ann_date") == date(2025, 1, 3))
    )
    assert revision.height == 1
    row = revision.row(0, named=True)
    assert row["first_usable_trade_date"] == date(2025, 1, 6)
    assert row["first_usable_trade_date"] > row["ann_date"]
    assert row["signal_known"] is True
    assert row["signal_value"] == 1.0

    last_day = observations.filter(
        (pl.col("hypothesis_id") == "forecast_upward_revision")
        & (pl.col("ann_date") == date(2026, 7, 23))
    )
    assert last_day.height == 1
    assert last_day["first_usable_trade_date"].item() == date(2026, 7, 24)
    assert last_day["label_known_20d"].item() is True

    unknown = observations.filter(
        (pl.col("hypothesis_id") == "forecast_upward_revision")
        & (pl.col("symbol") == "000002.SZ")
        & (pl.col("ann_date") == date(2025, 1, 10))
    )
    assert unknown.height == 1
    assert unknown["signal_known"].item() is False
    assert unknown["signal_value"].item() is None

    audit_non_std = observations.filter(
        (pl.col("hypothesis_id") == "audit_non_standard_opinion")
        & (pl.col("symbol") == "000002.SZ")
    ).row(0, named=True)
    assert audit_non_std["signal_value"] == 1.0
    audit_std = observations.filter(
        (pl.col("hypothesis_id") == "audit_non_standard_opinion")
        & (pl.col("symbol") == "000001.SZ")
    ).row(0, named=True)
    assert audit_std["signal_value"] == 0.0
    assert summary.height == 2
    assert report.candidate_multiplicity == 2
    assert summary["incomplete_20d_label_rows"].sum() == 0
    assert summary["incomplete_20d_label"].sum() == 0
    assert report.ready_for_scoring is False
    assert report.ready_for_trading is False
    assert report.auto_deploy is False


def test_rejects_drifted_label_horizon_window(tmp_path: Path) -> None:
    market, event, auth = _prepare_bundle(tmp_path)
    drifted = seal_authorization(
        auth.model_copy(
            update={
                "windows": auth.windows.model_copy(update={"label_hard_end": date(2026, 7, 30)})
            }
        )
    )
    with pytest.raises(ValueError, match="label_hard_end does not equal"):
        evaluate_and_write_event_candidate_oos_one_shot(
            authorization=drifted,
            freeze_path=COMMITTED_FREEZE,
            market_dir=market,
            event_dir=event,
            config=_strategy_config(),
            strategy_config_id=STRATEGY_ID,
            root=tmp_path,
        )


def test_rejects_wrong_snapshot_and_changed_candidates(tmp_path: Path) -> None:
    market, event, auth = _prepare_bundle(tmp_path)
    wrong = seal_authorization(
        auth.model_copy(update={"market_snapshot_id": "ab" * 32, "base_market_snapshot_id": "ab" * 32})
    )
    with pytest.raises(ValueError, match="market snapshot_id"):
        evaluate_and_write_event_candidate_oos_one_shot(
            authorization=wrong,
            freeze_path=COMMITTED_FREEZE,
            market_dir=market,
            event_dir=event,
            config=_strategy_config(),
            strategy_config_id=STRATEGY_ID,
            root=tmp_path,
        )

    wrong_list = list(AUTHORIZED_CANDIDATES)
    wrong_list[0] = wrong_list[0].model_copy(update={"candidate_direction": "negative"})
    tampered_candidates = seal_authorization(
        auth.model_copy(update={"nominated_candidates": wrong_list})
    )
    with pytest.raises(ValueError, match="does not match freeze semantics"):
        evaluate_and_write_event_candidate_oos_one_shot(
            authorization=tampered_candidates,
            freeze_path=COMMITTED_FREEZE,
            market_dir=market,
            event_dir=event,
            config=_strategy_config(),
            strategy_config_id=STRATEGY_ID,
            root=tmp_path,
        )


def test_rejects_different_data_binding_and_freeze_mismatch(tmp_path: Path) -> None:
    market, event, auth = _prepare_bundle(tmp_path)
    other_market = _oos_market(tmp_path / "market-b", end=date(2026, 8, 31), seed=DEMO_SEED + 9)
    with pytest.raises(ValueError, match="market snapshot"):
        evaluate_and_write_event_candidate_oos_one_shot(
            authorization=auth,
            freeze_path=COMMITTED_FREEZE,
            market_dir=other_market,
            event_dir=event,
            config=_strategy_config(),
            strategy_config_id=STRATEGY_ID,
            root=tmp_path,
        )

    bad_freeze_id = seal_authorization(auth.model_copy(update={"freeze_id": "cd" * 32}))
    with pytest.raises(ValueError, match="freeze_id"):
        evaluate_and_write_event_candidate_oos_one_shot(
            authorization=bad_freeze_id,
            freeze_path=COMMITTED_FREEZE,
            market_dir=market,
            event_dir=event,
            config=_strategy_config(),
            strategy_config_id=STRATEGY_ID,
            root=tmp_path,
        )


def test_output_and_receipt_replay_refusal(tmp_path: Path) -> None:
    market, event, auth = _prepare_bundle(tmp_path)
    evaluate_and_write_event_candidate_oos_one_shot(
        authorization=auth,
        freeze_path=COMMITTED_FREEZE,
        market_dir=market,
        event_dir=event,
        config=_strategy_config(),
        strategy_config_id=STRATEGY_ID,
        root=tmp_path,
    )
    with pytest.raises(ValueError, match="already exists"):
        assert_authorization_paths_unused(auth, root=tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        evaluate_and_write_event_candidate_oos_one_shot(
            authorization=auth,
            freeze_path=COMMITTED_FREEZE,
            market_dir=market,
            event_dir=event,
            config=_strategy_config(),
            strategy_config_id=STRATEGY_ID,
            root=tmp_path,
        )

    # Receipt alone is enough to refuse even if output is removed.
    output = tmp_path / auth.output_dir
    import shutil

    shutil.rmtree(output)
    with pytest.raises(ValueError, match="consumption receipt already exists"):
        evaluate_and_write_event_candidate_oos_one_shot(
            authorization=auth,
            freeze_path=COMMITTED_FREEZE,
            market_dir=market,
            event_dir=event,
            config=_strategy_config(),
            strategy_config_id=STRATEGY_ID,
            root=tmp_path,
        )


def test_atomic_failure_cleans_temporary_and_refuses_overwrite(tmp_path: Path) -> None:
    observations = _binary_frame(
        "forecast_upward_revision", n1=40, n0=40, mean1=0.02, mean0=0.0
    )
    trading_days, day_index, label_hard_end = _summary_calendar()
    summary = _build_candidate_summary(
        observations,
        candidates=[AUTHORIZED_CANDIDATES[0]],
        gates=AuthorizedEvaluationGates(),
        trading_days=trading_days,
        day_index=day_index,
        label_hard_end=label_hard_end,
    )
    from app.research.event_candidate_oos_evaluation import EventCandidateOosEvaluationReport

    report = EventCandidateOosEvaluationReport(
        authorization_id="ab" * 32,
        freeze_id=AUTHORIZED_FREEZE_ID,
        strategy_config_id=STRATEGY_ID,
        strategy_config_hash="796b793856dcd02a",
        market_snapshot_id="cd" * 32,
        event_snapshot_id="ef" * 32,
        base_market_snapshot_id="cd" * 32,
        event_source_manifest_sha256="11" * 32,
        event_source_coverage_start=date(2024, 10, 8),
        event_source_coverage_end=date(2026, 8, 21),
        announcement_window_start=date(2025, 1, 1),
        announcement_window_end=date(2026, 7, 23),
        first_2025_trading_day=date(2025, 1, 2),
        last_complete_label_entry_date=date(2026, 7, 24),
        label_hard_end=date(2026, 8, 21),
        forward_horizons=[5, 10, 20],
        benchmark_symbol="000300.SH",
        nominated_hypothesis_ids=["forecast_upward_revision"],
        candidate_multiplicity=1,
        observation_rows=observations.height,
        candidate_summary_rows=summary.height,
        candidate_outcomes={"forecast_upward_revision": "direction_replicated"},
    )
    output = tmp_path / "out"
    receipt = tmp_path / "receipt.json"
    sealed, _ = write_event_candidate_oos_evaluation_atomically(
        output,
        report,
        observations,
        summary,
        receipt_path=receipt,
        authorization_id="ab" * 32,
        authorization_output_dir="out",
    )
    assert sealed.report_id is not None
    load_verified_consumption_receipt(receipt)
    with pytest.raises(ValueError, match="immutable"):
        write_event_candidate_oos_evaluation_atomically(
            output,
            report,
            observations,
            summary,
            receipt_path=tmp_path / "receipt2.json",
            authorization_id="ab" * 32,
            authorization_output_dir="out",
        )
    leftovers = list(tmp_path.glob(".event-candidate-oos-*"))
    assert leftovers == []


def test_cli_rejects_custom_resealed_authorization_with_changed_bindings(tmp_path: Path) -> None:
    committed = build_committed_event_candidate_oos_authorization()
    tampered = seal_authorization(
        committed.model_copy(
            update={
                "windows": committed.windows.model_copy(
                    update={"announcement_end": date(2026, 7, 22)}
                ),
                "output_dir": "data/evil/event-candidate-oos-evaluations/one-shot-v1",
            }
        )
    )
    custom_path = tmp_path / "custom-auth.json"
    write_event_candidate_oos_authorization(custom_path, tampered)
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "evaluate-a-share-event-candidate-oos-one-shot",
            "--strategy",
            STRATEGY_ID,
            "--authorization-file",
            str(custom_path),
        ],
    )
    assert result.exit_code == 1
    assert "sealed" in result.output.lower() or "does not match" in result.output.lower()


def test_cli_rejects_path_mismatch_and_does_not_require_replace_flag(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "evaluate-a-share-event-candidate-oos-one-shot",
            "--strategy",
            STRATEGY_ID,
            "--market-dir",
            str(tmp_path / "other-market"),
        ],
    )
    assert result.exit_code == 1
    assert "market-dir" in result.output.lower() or "does not match" in result.output.lower()


def test_oos_observation_builder_limits_labels_to_nominated_hypotheses(tmp_path: Path) -> None:
    market, event, auth = _prepare_bundle(tmp_path)
    event_snap, tables = load_verified_event_snapshot(event)
    from app.research.event_candidate_oos_evaluation import build_event_candidate_oos_evaluation

    report, observations, _ = build_event_candidate_oos_evaluation(
        authorization=auth,
        freeze_path=COMMITTED_FREEZE,
        market_dir=market,
        event_snapshot=event_snap,
        event_source_manifest=EventSourceManifest.model_validate_json(
            (event / "source_manifest.json").read_bytes()
        ),
        event_tables=tables,
        config=_strategy_config(),
        strategy_config_id=STRATEGY_ID,
    )
    observed_ids = set(observations["hypothesis_id"].unique().to_list())
    assert observed_ids == set(auth.nominated_hypothesis_ids)
    assert observed_ids <= PREDECLARED_HYPOTHESIS_IDS
    unfrozen = PREDECLARED_HYPOTHESIS_IDS - observed_ids
    assert len(unfrozen) == len(PREDECLARED_HYPOTHESIS_IDS) - 2
    assert report.observation_rows == observations.height


def test_build_observations_rejects_unknown_allowed_hypothesis_id(tmp_path: Path) -> None:
    market, event, auth = _prepare_bundle(tmp_path)
    event_snap, tables = load_verified_event_snapshot(event)
    market_tables = read_tables(market)
    calendar = market_tables["calendar"]["date"].to_list()
    trading_days = [day for day in calendar if isinstance(day, date)]
    day_index = {day: idx for idx, day in enumerate(trading_days)}
    from app.research.event_candidate_diagnostics import _load_label_prices

    prices, benchmark_prices = _load_label_prices(
        market_tables,
        benchmark_symbol=auth.benchmark_symbol,
        label_hard_end=auth.windows.label_hard_end,
    )
    with pytest.raises(ValueError, match="not predeclared"):
        _build_observations(
            event_tables=tables,
            window_start=auth.windows.announcement_start,
            window_end=auth.windows.announcement_end,
            trading_days=trading_days,
            day_index=day_index,
            prices=prices,
            benchmark_prices=benchmark_prices,
            config=_strategy_config(),
            label_hard_end=auth.windows.label_hard_end,
            entry_end=auth.windows.last_complete_label_entry_date,
            allowed_hypothesis_ids=frozenset({"forecast_upward_revision", "unknown_hypothesis"}),
        )


def test_development_diagnostics_still_build_after_parameterization(tmp_path: Path) -> None:
    write_demo_parquet(generate_demo_market(seed=DEMO_SEED), tmp_path / "market")
    # Reuse development fixture builder from candidate diagnostics tests via minimal overlay.
    from tests.test_event_candidate_diagnostics import _candidate_source_dir

    event = tmp_path / "event"
    materialize_tushare_event_overlay(
        source_dir=_candidate_source_dir(tmp_path / "source"),
        market_dir=tmp_path / "market",
        dest_dir=event,
    )
    snapshot, tables = load_verified_event_snapshot(event)
    source = EventSourceManifest.model_validate_json(
        (event / "source_manifest.json").read_bytes()
    )
    report, observations, _ = build_event_candidate_diagnostics(
        market_dir=tmp_path / "market",
        event_snapshot=snapshot,
        event_source_manifest=source,
        event_tables=tables,
        config=load_test_config(),
        window_start=DEVELOPMENT_WINDOW_START,
        window_end=DEVELOPMENT_WINDOW_END,
    )
    assert report.window_start == DEVELOPMENT_WINDOW_START
    assert observations.filter(pl.col("ann_date") == date(2022, 6, 10)).height > 0
