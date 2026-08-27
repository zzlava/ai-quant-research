"""Attack-oriented tests for layer-two statistical risk-cluster evidence (E10c)."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from pydantic import ValidationError

from app.models.market import Instrument
from app.models.snapshot import DataSnapshot
from app.research.layer_two_statistical_risk_clusters import (
    BOUND_LOOKBACK_TRADING_DAYS,
    BOUND_REQUIRED_PRICE_POINTS,
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
    PROMINENT_RISK_PROXY_ANNOTATION,
    assert_report_self_hash,
    bind_two_layer_statistical_risk_cluster_policy,
    diagnose_layer_two_statistical_risk_clusters,
    seal_layer_two_statistical_risk_cluster_report,
    verify_layer_two_statistical_risk_cluster_report,
    verify_layer_two_statistical_risk_cluster_report_file,
    write_layer_two_statistical_risk_cluster_report,
)
from app.research.two_layer_contract import load_two_layer_decision_draft
from app.storage.memory import InMemoryStore
from tests.helpers import bar, store_from_rows, weekdays

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_CONTRACT = REPO_ROOT / BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
LOOKBACK = BOUND_LOOKBACK_TRADING_DAYS
PRICE_POINTS = BOUND_REQUIRED_PRICE_POINTS


def _prices_from_returns(returns: list[float], start: float = 100.0) -> list[float]:
    prices = [start]
    for value in returns:
        prices.append(prices[-1] * (1.0 + value))
    return prices


def _rows_for_symbol(symbol: str, calendar: list[date], prices: list[float]) -> list[dict[str, object]]:
    assert len(calendar) == len(prices)
    rows: list[dict[str, object]] = []
    for day, price in zip(calendar, prices, strict=True):
        rows.append(bar(symbol, day, price, price + 0.05, price - 0.05, price))
    return rows


def _store(calendar: list[date], series: dict[str, list[float]]) -> InMemoryStore:
    rows: list[dict[str, object]] = []
    for symbol, prices in series.items():
        rows.extend(_rows_for_symbol(symbol, calendar, prices))
    return store_from_rows(calendar, rows)


def _varying_returns(n: int, seed: int = 1) -> list[float]:
    return [0.01 * (((seed * (i + 3)) % 7) - 3) for i in range(n)]


def _complete_fixture(
    *,
    n_symbols: int = 4,
    extra_days: int = 0,
) -> tuple[list[date], date, datetime, InMemoryStore, list[str]]:
    calendar = weekdays(date(2023, 1, 3), PRICE_POINTS + extra_days)
    window = calendar[-PRICE_POINTS:]
    as_of = window[-1]
    decision_at = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC)
    symbols = [f"{i:06d}.SH" if i % 2 == 0 else f"{i:06d}.SZ" for i in range(1, n_symbols + 1)]
    # Pair (1,2) and (3,4) into two high-correlation sleeves.
    group_a = _varying_returns(LOOKBACK, seed=1)
    group_b = _varying_returns(LOOKBACK, seed=9)
    series = {
        symbols[0]: _prices_from_returns(group_a),
        symbols[1]: _prices_from_returns(group_a),
        symbols[2]: _prices_from_returns(group_b),
        symbols[3]: _prices_from_returns(group_b),
    }
    # Pad earlier calendar days if extra_days provided so full calendar is covered.
    if extra_days:
        padded: dict[str, list[float]] = {}
        for symbol in symbols:
            early_returns = [0.001 * ((i % 5) - 2) for i in range(extra_days)]
            body = group_a if symbol in symbols[:2] else group_b
            padded[symbol] = _prices_from_returns(early_returns + body)
            assert len(padded[symbol]) == len(calendar)
        store = _store(calendar, padded)
    else:
        store = _store(window, series)
        calendar = window
    return calendar, as_of, decision_at, store, symbols


class _FutureLeakStore:
    def __init__(self, base: InMemoryStore, *, leak_symbol: str, leak_day: date) -> None:
        self._base = base
        self._leak_symbol = leak_symbol
        self._leak_day = leak_day

    def get_instruments(self) -> list[Instrument]:
        return self._base.get_instruments()

    def get_calendar(self, start: date, end: date) -> list[date]:
        return self._base.get_calendar(start, end)

    def get_daily_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        frame = self._base.get_daily_bars(as_of=as_of, symbol=symbol, start=start)
        if symbol not in (None, self._leak_symbol):
            return frame
        leak = pl.DataFrame(
            [
                {
                    "symbol": self._leak_symbol,
                    "date": self._leak_day,
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.0,
                    "volume": 1_000_000.0,
                    "amount": 10_000_000.0,
                    "turnover_rate": 0.01,
                    "is_st": False,
                    "is_suspended": False,
                    "price_limit_pct": 0.1,
                    "adj_open": 10.0,
                    "adj_high": 10.1,
                    "adj_low": 9.9,
                    "adj_close": 10.0,
                }
            ]
        ).with_columns(pl.col("date").cast(pl.Date))
        return pl.concat([frame, leak], how="diagonal_relaxed")

    def get_index_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        return self._base.get_index_bars(as_of=as_of, symbol=symbol, start=start)

    def get_global_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> pl.DataFrame:
        return self._base.get_global_bars(as_of=as_of, symbol=symbol, start=start)

    def get_universe_members(
        self,
        universe_id: str,
        as_of: date,
        available_by: Any,
        *,
        expected_constituents: int | None = None,
        require_available_cross_section: bool = False,
    ) -> set[str]:
        return self._base.get_universe_members(
            universe_id,
            as_of,
            available_by,
            expected_constituents=expected_constituents,
            require_available_cross_section=require_available_cross_section,
        )

    def next_trading_day(self, after: date) -> date | None:
        return self._base.next_trading_day(after)

    def trading_days_after(self, after: date, n: int) -> list[date]:
        return self._base.trading_days_after(after, n)

    def snapshot(self) -> DataSnapshot:
        return self._base.snapshot()


def test_bind_exact_contract_path_and_id() -> None:
    contract_id, path, policy = bind_two_layer_statistical_risk_cluster_policy(repo_root=REPO_ROOT)
    assert contract_id == BOUND_TWO_LAYER_DECISION_CONTRACT_ID
    assert path == BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
    assert policy.statistical_risk_cluster.lookback_trading_days == 120
    assert policy.statistical_risk_cluster.correlation_threshold == 0.65
    assert policy.statistical_risk_cluster.max_sleeve_weight_per_cluster == 0.35
    assert policy.statistical_risk_cluster.max_positions_per_cluster == 2
    assert policy.pit_industry_current_proxy == "statistical_risk_clusters"
    draft = load_two_layer_decision_draft(COMMITTED_CONTRACT)
    assert draft.contract_id == BOUND_TWO_LAYER_DECISION_CONTRACT_ID


def test_tampered_disk_contract_value_rejected(tmp_path: Path) -> None:
    tampered_dir = tmp_path / "config" / "research"
    tampered_dir.mkdir(parents=True)
    # Ledger must also exist for path layout; copy committed contract bytes then mutate.
    ledger_src = REPO_ROOT / "config" / "research" / "research-trial-ledger-v1.json"
    (tampered_dir / "research-trial-ledger-v1.json").write_text(
        ledger_src.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    payload = json.loads(COMMITTED_CONTRACT.read_text(encoding="utf-8"))
    payload["layer_two"]["statistical_risk_cluster"]["correlation_threshold"] = 0.99
    tampered_path = tampered_dir / "two-layer-strategy-decision-draft-v1.json"
    tampered_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        bind_two_layer_statistical_risk_cluster_policy(repo_root=tmp_path, contract_path=tampered_path)


def test_tampered_disk_contract_bytes_rejected(tmp_path: Path) -> None:
    tampered_dir = tmp_path / "config" / "research"
    tampered_dir.mkdir(parents=True)
    ledger_src = REPO_ROOT / "config" / "research" / "research-trial-ledger-v1.json"
    (tampered_dir / "research-trial-ledger-v1.json").write_text(
        ledger_src.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    payload = json.loads(COMMITTED_CONTRACT.read_text(encoding="utf-8"))
    payload["confirmed"]["note"] = "tampered contract content"
    tampered_path = tampered_dir / "two-layer-strategy-decision-draft-v1.json"
    tampered_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        bind_two_layer_statistical_risk_cluster_policy(repo_root=tmp_path, contract_path=tampered_path)


def test_exact_parameter_binding_120_065_035_2() -> None:
    _calendar, as_of, decision_at, store, symbols = _complete_fixture()
    report = diagnose_layer_two_statistical_risk_clusters(
        store,
        as_of,
        decision_at,
        symbols,
        REPO_ROOT,
    )
    assert report.lookback_trading_days == 120
    assert report.correlation_threshold == 0.65
    assert report.max_sleeve_weight_per_cluster == 0.35
    assert report.max_positions_per_cluster == 2
    assert report.required_price_points == 121
    assert len(report.diagnostic.required_trading_days) == 121
    assert report.diagnostic.lookback_bars == 120
    assert report.diagnostic.correlation_threshold == 0.65
    assert report.linkage == "connected_components_chain"
    sig = inspect.signature(diagnose_layer_two_statistical_risk_clusters)
    assert "lookback" not in sig.parameters
    assert "correlation_threshold" not in sig.parameters
    assert "max_sleeve_weight_per_cluster" not in sig.parameters
    assert "max_positions_per_cluster" not in sig.parameters


def test_complete_report_ready_for_cluster_constraints_only() -> None:
    _calendar, as_of, decision_at, store, symbols = _complete_fixture()
    report = diagnose_layer_two_statistical_risk_clusters(
        store,
        as_of,
        decision_at,
        symbols,
        REPO_ROOT,
    )
    assert report.ready_for_cluster_constraints is True
    assert report.ready_for_scoring is False
    assert report.ready_for_portfolio_construction is False
    assert report.ready_for_orders is False
    assert report.ready_for_trading is False
    assert report.auto_apply is False
    assert report.is_not_industry_classification is True
    assert report.current_industry_backfill_forbidden is True
    assert report.risk_proxy_annotation == PROMINENT_RISK_PROXY_ANNOTATION
    assert report.data_snapshot_id == store.snapshot().snapshot_id
    assert report.two_layer_decision_contract_id == BOUND_TWO_LAYER_DECISION_CONTRACT_ID
    verify_layer_two_statistical_risk_cluster_report(report, store=store, repo_root=REPO_ROOT)


def test_candidate_order_does_not_affect_report_id() -> None:
    _calendar, as_of, decision_at, store, symbols = _complete_fixture()
    left = diagnose_layer_two_statistical_risk_clusters(
        store,
        as_of,
        decision_at,
        list(reversed(symbols)),
        REPO_ROOT,
    )
    right = diagnose_layer_two_statistical_risk_clusters(
        store,
        as_of,
        decision_at,
        symbols,
        REPO_ROOT,
    )
    assert left.report_id == right.report_id
    assert left.candidates == sorted(symbols)


def test_symbol_alias_bj_blank_duplicate_rejected() -> None:
    _calendar, as_of, decision_at, store, symbols = _complete_fixture()
    with pytest.raises(ValueError, match="canonical|SH or .SZ"):
        diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, ["600000"], REPO_ROOT)
    with pytest.raises(ValueError, match="canonical|SH or .SZ"):
        diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, ["430047.BJ"], REPO_ROOT)
    with pytest.raises(ValueError, match="non-empty|whitespace"):
        diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, [" 000001.SZ"], REPO_ROOT)
    with pytest.raises(ValueError, match="duplicate"):
        diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, [symbols[0], symbols[0]], REPO_ROOT)


def test_weekend_and_non_trading_as_of_rejected() -> None:
    calendar, as_of, _decision_at, store, symbols = _complete_fixture()
    weekend = as_of + timedelta(days=1)
    while weekend.weekday() < 5:
        weekend += timedelta(days=1)
    assert weekend not in calendar
    decision_at = datetime(weekend.year, weekend.month, weekend.day, 16, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="trading day"):
        diagnose_layer_two_statistical_risk_clusters(store, weekend, decision_at, symbols, REPO_ROOT)


def test_decision_at_must_be_aware_and_match_as_of() -> None:
    _calendar, as_of, _decision_at, store, symbols = _complete_fixture()
    naive = datetime(as_of.year, as_of.month, as_of.day, 16, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        diagnose_layer_two_statistical_risk_clusters(store, as_of, naive, symbols, REPO_ROOT)
    wrong_day = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC) - timedelta(days=1)
    with pytest.raises(ValueError, match="calendar date must equal as_of"):
        diagnose_layer_two_statistical_risk_clusters(store, as_of, wrong_day, symbols, REPO_ROOT)


def test_future_bar_rejected() -> None:
    # Build a longer calendar, decide on an interior trading day, leak a later bar.
    calendar = weekdays(date(2023, 1, 3), PRICE_POINTS + 5)
    as_of = calendar[PRICE_POINTS - 1]
    assert as_of < calendar[-1]
    decision_at = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC)
    symbols = ["000001.SZ", "000002.SZ"]
    # Prices cover the full calendar so get_daily_bars can return a post-as_of leak.
    full_returns = _varying_returns(len(calendar) - 1, seed=2)
    base = _store(
        calendar,
        {
            symbols[0]: _prices_from_returns(full_returns),
            symbols[1]: _prices_from_returns(full_returns),
        },
    )
    leak_day = calendar[PRICE_POINTS + 1]
    assert leak_day > as_of
    store = _FutureLeakStore(base, leak_symbol=symbols[0], leak_day=leak_day)
    with pytest.raises(ValueError, match="after as_of"):
        diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, symbols, REPO_ROOT)


def test_short_history_unresolved_not_ready() -> None:
    short_cal = weekdays(date(2024, 1, 2), 40)
    as_of = short_cal[-1]
    decision_at = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC)
    returns = _varying_returns(len(short_cal) - 1)
    store = _store(
        short_cal,
        {
            "000001.SZ": _prices_from_returns(returns),
            "000002.SZ": _prices_from_returns(returns),
        },
    )
    report = diagnose_layer_two_statistical_risk_clusters(
        store,
        as_of,
        decision_at,
        ["000001.SZ", "000002.SZ"],
        REPO_ROOT,
    )
    assert report.ready_for_cluster_constraints is False
    assert report.diagnostic.unresolved_symbols
    clustered = {s for c in report.diagnostic.clusters for s in c.symbols}
    assert "000001.SZ" not in clustered


def test_missing_day_duplicate_nan_zero_constant_unresolved() -> None:
    calendar = weekdays(date(2023, 1, 3), PRICE_POINTS)
    as_of = calendar[-1]
    decision_at = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC)
    good_returns = _varying_returns(LOOKBACK)
    good_prices = _prices_from_returns(good_returns)
    good = _rows_for_symbol("000001.SZ", calendar, good_prices)

    # Missing day for 000002.SZ
    gap_days = [d for d in calendar if d != calendar[10]]
    gap_prices = good_prices[:10] + good_prices[11:]
    missing = _rows_for_symbol("000002.SZ", gap_days, gap_prices)

    # Duplicate date for 000003.SZ
    dup = _rows_for_symbol("000003.SZ", calendar, good_prices)
    dup.append(bar("000003.SZ", calendar[5], 11.0, 11.1, 10.9, 11.0))

    # Non-positive / NaN-like bad price for 000004.SZ
    bad = _rows_for_symbol("000004.SZ", calendar, good_prices)
    bad[20] = bar("000004.SZ", calendar[20], 0.0, 0.1, -0.1, 0.0)

    # Constant returns for 000005.SZ paired with good → unresolved pair when both evaluable
    constant = _rows_for_symbol("000005.SZ", calendar, _prices_from_returns([0.0] * LOOKBACK))
    constant_b = _rows_for_symbol("000006.SZ", calendar, _prices_from_returns([0.0] * LOOKBACK))

    store = store_from_rows(calendar, good + missing + dup + bad + constant + constant_b)
    report = diagnose_layer_two_statistical_risk_clusters(
        store,
        as_of,
        decision_at,
        [
            "000001.SZ",
            "000002.SZ",
            "000003.SZ",
            "000004.SZ",
            "000005.SZ",
            "000006.SZ",
        ],
        REPO_ROOT,
    )
    by_symbol = {item.symbol: item.reason for item in report.diagnostic.unresolved_symbols}
    assert by_symbol["000002.SZ"] == "missing_trading_day_history"
    assert by_symbol["000003.SZ"] == "duplicate_dates"
    assert by_symbol["000004.SZ"] == "non_finite_or_non_positive_adj_close"
    assert report.ready_for_cluster_constraints is False
    # One unresolved anywhere keeps the whole report not ready.
    assert (
        any(item.reason == "constant_return_series" for item in report.diagnostic.unresolved_pairs)
        or {
            "000005.SZ",
            "000006.SZ",
        }.issubset(by_symbol)
        or report.diagnostic.unresolved_pairs
    )


def test_one_unresolved_makes_whole_report_not_ready() -> None:
    calendar = weekdays(date(2023, 1, 3), PRICE_POINTS)
    as_of = calendar[-1]
    decision_at = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC)
    returns = _varying_returns(LOOKBACK)
    full = _prices_from_returns(returns)
    short_cal = calendar[5:]
    rows = (
        _rows_for_symbol("000001.SZ", calendar, full)
        + _rows_for_symbol("000002.SZ", calendar, full)
        + _rows_for_symbol("000003.SZ", short_cal, full[5:])
    )
    store = store_from_rows(calendar, rows)
    report = diagnose_layer_two_statistical_risk_clusters(
        store,
        as_of,
        decision_at,
        ["000001.SZ", "000002.SZ", "000003.SZ"],
        REPO_ROOT,
    )
    assert report.ready_for_cluster_constraints is False
    clustered = {s for c in report.diagnostic.clusters for s in c.symbols}
    assert "000003.SZ" not in clustered


def test_chain_linkage_single_cluster() -> None:
    calendar = weekdays(date(2023, 1, 3), PRICE_POINTS)
    as_of = calendar[-1]
    decision_at = datetime(as_of.year, as_of.month, as_of.day, 16, 0, tzinfo=UTC)
    # Construct A–B and B–C above 0.65 while A–C stays below (classic chain).
    shared1 = [0.01 * ((i * 2) % 11 - 5) for i in range(LOOKBACK)]
    shared2 = [0.01 * ((i * 5) % 11 - 5) for i in range(LOOKBACK)]
    # Heavier shared weight so each edge clears 0.65; endpoints remain weaker.
    bridge_ab = [0.85 * a + 0.15 * b for a, b in zip(shared1, shared2, strict=True)]
    bridge_bc = [0.15 * a + 0.85 * b for a, b in zip(shared1, shared2, strict=True)]
    # Use A=shared1, B=average of the two bridges (connected to both), C=shared2.
    mid = [0.5 * left + 0.5 * right for left, right in zip(bridge_ab, bridge_bc, strict=True)]
    store = _store(
        calendar,
        {
            "000001.SZ": _prices_from_returns(shared1),
            "000002.SZ": _prices_from_returns(mid),
            "000003.SZ": _prices_from_returns(shared2),
        },
    )
    report = diagnose_layer_two_statistical_risk_clusters(
        store,
        as_of,
        decision_at,
        ["000003.SZ", "000001.SZ", "000002.SZ"],
        REPO_ROOT,
    )
    assert report.ready_for_cluster_constraints is True
    pair_map = {(p.symbol_a, p.symbol_b): p for p in report.diagnostic.pairs}
    assert pair_map[("000001.SZ", "000002.SZ")].above_threshold is True
    assert pair_map[("000002.SZ", "000003.SZ")].above_threshold is True
    assert pair_map[("000001.SZ", "000003.SZ")].above_threshold is False
    assert len(report.diagnostic.clusters) == 1
    assert report.diagnostic.clusters[0].symbols == ["000001.SZ", "000002.SZ", "000003.SZ"]
    assert report.linkage == "connected_components_chain"


def test_derived_field_tamper_reseal_rejected() -> None:
    _calendar, as_of, decision_at, store, symbols = _complete_fixture()
    report = diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, symbols, REPO_ROOT)
    # Pydantic freeze on exact floats rejects cap/threshold drift at validation.
    payload = report.model_dump(mode="python")
    payload["max_sleeve_weight_per_cluster"] = 0.99
    payload["report_id"] = None
    with pytest.raises(ValidationError):
        type(report).model_validate(payload)

    # Tamper clusters inside diagnostic and reseal outer hash.
    from app.research.statistical_risk_clusters import (
        StatisticalRiskClusterReport,
        seal_statistical_risk_cluster_report,
    )

    diag_payload = report.diagnostic.model_dump(mode="python")
    diag_payload["clusters"] = [
        {"cluster_id": "cluster_999", "symbols": [symbols[0], symbols[2]]},
    ]
    fake_diag = seal_statistical_risk_cluster_report(StatisticalRiskClusterReport.model_validate(diag_payload))
    drifted = report.model_copy(
        update={
            "diagnostic": fake_diag,
            "ready_for_cluster_constraints": fake_diag.ready_for_portfolio_constraints,
            "report_id": None,
        }
    )
    resealed = seal_layer_two_statistical_risk_cluster_report(drifted)
    assert_report_self_hash(resealed)
    with pytest.raises(ValueError, match="recompute|does not match"):
        verify_layer_two_statistical_risk_cluster_report(resealed, store=store, repo_root=REPO_ROOT)


def test_annotation_and_cap_tamper_reseal_rejected() -> None:
    _calendar, as_of, decision_at, store, symbols = _complete_fixture()
    report = diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, symbols, REPO_ROOT)
    payload = report.model_dump(mode="python")
    payload["risk_proxy_annotation"] = "harmless industry labels"
    payload["report_id"] = None
    with pytest.raises(ValidationError):
        type(report).model_validate(payload)

    payload = report.model_dump(mode="python")
    payload["max_positions_per_cluster"] = 9
    payload["report_id"] = None
    with pytest.raises(ValidationError):
        type(report).model_validate(payload)


def test_another_snapshot_store_verification_fails() -> None:
    calendar, as_of, decision_at, store, symbols = _complete_fixture()
    report = diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, symbols, REPO_ROOT)
    _other_calendar, _other_as_of, _other_decision, other_store, _other_symbols = _complete_fixture(extra_days=1)
    assert other_store.snapshot().snapshot_id != store.snapshot().snapshot_id
    with pytest.raises(ValueError, match="snapshot"):
        verify_layer_two_statistical_risk_cluster_report(report, store=other_store, repo_root=REPO_ROOT)
    # Same calendar shape but mutated prices → different snapshot / recompute.
    mutated_series = {
        symbols[0]: _prices_from_returns(_varying_returns(LOOKBACK, seed=99)),
        symbols[1]: _prices_from_returns(_varying_returns(LOOKBACK, seed=99)),
        symbols[2]: _prices_from_returns(_varying_returns(LOOKBACK, seed=3)),
        symbols[3]: _prices_from_returns(_varying_returns(LOOKBACK, seed=3)),
    }
    mutated = _store(calendar, mutated_series)
    assert mutated.snapshot().snapshot_id != report.data_snapshot_id
    with pytest.raises(ValueError):
        verify_layer_two_statistical_risk_cluster_report(report, store=mutated, repo_root=REPO_ROOT)


def test_forbid_industry_sector_alpha_injection() -> None:
    _calendar, as_of, decision_at, store, symbols = _complete_fixture()
    report = diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, symbols, REPO_ROOT)
    payload = report.model_dump(mode="python")
    payload["current_industry"] = "银行"
    payload["sector"] = "金融"
    payload["alpha"] = 1.23
    payload["report_id"] = None
    with pytest.raises(ValidationError):
        type(report).model_validate(payload)


def test_ready_flags_cannot_be_set_true() -> None:
    _calendar, as_of, decision_at, store, symbols = _complete_fixture()
    report = diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, symbols, REPO_ROOT)
    for field in (
        "ready_for_scoring",
        "ready_for_portfolio_construction",
        "ready_for_orders",
        "ready_for_trading",
        "auto_apply",
    ):
        payload = report.model_dump(mode="python")
        payload[field] = True
        payload["report_id"] = None
        with pytest.raises(ValidationError):
            type(report).model_validate(payload)


def test_write_and_verify_file_roundtrip(tmp_path: Path) -> None:
    _calendar, as_of, decision_at, store, symbols = _complete_fixture()
    report = diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, symbols, REPO_ROOT)
    out = tmp_path / "e10c-report.json"
    write_layer_two_statistical_risk_cluster_report(report, out)
    loaded = verify_layer_two_statistical_risk_cluster_report_file(out, store=store, repo_root=REPO_ROOT)
    assert loaded.report_id == report.report_id


def test_self_hash_tamper_detected() -> None:
    _calendar, as_of, decision_at, store, symbols = _complete_fixture()
    report = diagnose_layer_two_statistical_risk_clusters(store, as_of, decision_at, symbols, REPO_ROOT)
    tampered = report.model_copy(update={"report_id": "0" * 64})
    with pytest.raises(ValueError, match="report_id"):
        assert_report_self_hash(tampered)
