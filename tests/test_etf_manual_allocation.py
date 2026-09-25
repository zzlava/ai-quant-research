from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.manual.etf_allocation import (
    DEFAULT_POLICY_PATH,
    EtfPolicy,
    LedgerState,
    Quote,
    build_plan,
    init_ledger,
    load_ledger,
    load_policy,
    load_quotes_csv,
    parse_sse_snapshot,
    record_cash,
    record_fill,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRICES = {
    "510300.SH": Decimal("4.700"),
    "513500.SH": Decimal("2.200"),
    "518880.SH": Decimal("7.500"),
    "511010.SH": Decimal("141.20"),
}


def _policy() -> tuple[EtfPolicy, str]:
    return load_policy(PROJECT_ROOT / DEFAULT_POLICY_PATH)


def _quotes(overrides: dict[str, Decimal] | None = None, iopv: Decimal | None = None) -> dict[str, Quote]:
    prices = {**PRICES, **(overrides or {})}
    return {
        symbol: Quote(
            symbol=symbol,
            last=price,
            bid=price,
            ask=price,
            iopv=iopv if symbol == "513500.SH" else None,
            source="test",
        )
        for symbol, price in prices.items()
    }


def _plan(state: LedgerState, quotes: dict[str, Quote], as_of: date = date(2026, 9, 25)):  # type: ignore[no-untyped-def]
    policy, sha = _policy()
    return build_plan(policy=policy, policy_sha256=sha, state=state, quotes=quotes, as_of=as_of)


def _deployed_ledger(tmp_path: Path) -> Path:
    policy, _ = _policy()
    init_ledger(tmp_path, cash=Decimal("80000"), event_date=date(2026, 9, 25))
    plan = _plan(load_ledger(tmp_path), _quotes())
    for order in plan.orders:
        record_fill(
            tmp_path,
            policy,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=order.limit_price,
            commission_paid=None,
            event_date=date(2026, 9, 25),
            plan_id=plan.plan_id,
        )
    return tmp_path


def test_committed_policy_is_manual_only_and_fully_allocated() -> None:
    policy, _ = _policy()
    total = sum((asset.target_weight for asset in policy.assets), policy.cash_target_weight)
    assert total == Decimal("1")
    assert policy.execution_mode == "manual_orders_only"
    assert policy.broker_connection is False
    assert all(asset.symbol.endswith(".SH") for asset in policy.assets)


def test_policy_rejects_weights_that_do_not_sum_to_one() -> None:
    payload = json.loads((PROJECT_ROOT / DEFAULT_POLICY_PATH).read_text())
    payload["cash_target_weight"] = "0.20"
    with pytest.raises(ValidationError, match="must equal 1"):
        EtfPolicy.model_validate(payload)


def test_initial_plan_deploys_board_lots_without_overspending(tmp_path: Path) -> None:
    init_ledger(tmp_path, cash=Decimal("80000"), event_date=date(2026, 9, 25))
    plan = _plan(load_ledger(tmp_path), _quotes())
    assert plan.triggers[0] == "initial_deployment"
    assert {order.side for order in plan.orders} == {"buy"}
    assert all(order.quantity % 100 == 0 for order in plan.orders)
    assert plan.post_trade_cash >= 0
    risk_weight = sum(
        (leg.weight for leg in plan.post_trade if leg.symbol != "511010.SH"), Decimal("0")
    )
    assert abs(risk_weight - Decimal("0.45")) < Decimal("0.02")


def test_recorded_fills_reproduce_the_plan_and_then_no_trade(tmp_path: Path) -> None:
    _deployed_ledger(tmp_path)
    state = load_ledger(tmp_path)
    assert state.cash > 0
    plan = _plan(state, _quotes())
    assert plan.triggers == []
    assert plan.orders == []


def test_band_breach_sells_winner_before_buying(tmp_path: Path) -> None:
    _deployed_ledger(tmp_path)
    plan = _plan(load_ledger(tmp_path), _quotes({"510300.SH": Decimal("8.00")}), as_of=date(2026, 11, 2))
    assert "band_breach:510300.SH" in plan.triggers
    assert plan.orders[0].side == "sell"
    assert plan.orders[0].symbol == "510300.SH"
    assert plan.post_trade_cash >= 0
    assert all(order.notional >= Decimal("2000") for order in plan.orders)


def test_annual_calendar_rebalance_fires_once_per_year(tmp_path: Path) -> None:
    _deployed_ledger(tmp_path)
    plan = _plan(load_ledger(tmp_path), _quotes(), as_of=date(2027, 1, 5))
    assert plan.triggers[0] == "annual_calendar_rebalance"


def test_qdii_buy_is_blocked_above_premium_limit(tmp_path: Path) -> None:
    init_ledger(tmp_path, cash=Decimal("80000"), event_date=date(2026, 9, 25))
    plan = _plan(load_ledger(tmp_path), _quotes(iopv=Decimal("2.10")))
    assert all(order.symbol != "513500.SH" for order in plan.orders)
    assert any("513500.SH" in item for item in plan.blocked)


def test_ledger_rejects_oversell_and_tampering(tmp_path: Path) -> None:
    policy, _ = _policy()
    _deployed_ledger(tmp_path)
    with pytest.raises(ValueError, match="exceeds holding"):
        record_fill(
            tmp_path,
            policy,
            symbol="518880.SH",
            side="sell",
            quantity=100_000,
            price=Decimal("7.5"),
            commission_paid=None,
            event_date=date(2026, 9, 26),
        )
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(ledger.read_text().replace('"cash_amount": "80000"', '"cash_amount": "90000"'))
    with pytest.raises(ValueError, match="hash chain broken"):
        load_ledger(tmp_path)


def test_ledger_is_append_only_and_cash_needs_reason(tmp_path: Path) -> None:
    init_ledger(tmp_path, cash=Decimal("1000"), event_date=date(2026, 9, 25))
    with pytest.raises(ValueError, match="append-only"):
        init_ledger(tmp_path, cash=Decimal("1000"), event_date=date(2026, 9, 25))
    with pytest.raises(ValueError, match="reason"):
        record_cash(tmp_path, amount=Decimal("5"), event_date=date(2026, 9, 26), note=" ")
    record_cash(tmp_path, amount=Decimal("1.23"), event_date=date(2026, 9, 26), note="逆回购利息")
    assert load_ledger(tmp_path).cash == Decimal("1001.23")


def test_quotes_csv_and_sse_snapshot_parsing(tmp_path: Path) -> None:
    path = tmp_path / "q.csv"
    path.write_text("symbol,last,bid,ask,iopv\n513500.SH,2.2,,,2.15\n", encoding="utf-8")
    quote = load_quotes_csv(path)["513500.SH"]
    assert quote.iopv == Decimal("2.15") and quote.bid is None
    payload = json.dumps(
        {
            "code": "510300",
            "date": 20260925,
            "time": 150001,
            "snap": ["沪深300ETF", 4.7, 4.68, 4.72, 4.66, 1000, 4700.0, [4.699, 100], [4.7, 200]],
        }
    ).encode()
    parsed = parse_sse_snapshot("510300.SH", payload)
    assert parsed.bid == Decimal("4.699") and parsed.ask == Decimal("4.7")
    with pytest.raises(ValueError, match="mismatch"):
        parse_sse_snapshot("518880.SH", payload)
