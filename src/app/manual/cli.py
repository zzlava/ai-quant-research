"""`ai-quant etf …` commands for the manual multi-asset ETF allocation."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import typer

from app.errors import sanitize_error_message
from app.manual.etf_allocation import (
    DEFAULT_LEDGER_DIR,
    DEFAULT_POLICY_PATH,
    build_plan,
    fetch_sse_quotes,
    init_ledger,
    load_ledger,
    load_policy,
    load_quotes_csv,
    mark_annual_rebalance_done,
    record_cash,
    record_fill,
    render_plan,
    save_plan,
)
from app.manual.etf_report import save_plan_html

etf_app = typer.Typer(help="Manual multi-asset ETF allocation: order tickets and an append-only fill ledger.")

PolicyOpt = Annotated[Path, typer.Option("--policy-file", dir_okay=False)]
LedgerOpt = Annotated[Path, typer.Option("--ledger-dir", file_okay=False)]


def _today() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def _fail(exc: Exception) -> typer.Exit:
    typer.echo(sanitize_error_message(exc), err=True)
    return typer.Exit(code=1)


@etf_app.command("init")
def init_cmd(
    cash: Annotated[str, typer.Option("--cash", help="Cash committed to this allocation, CNY")],
    policy_file: PolicyOpt = DEFAULT_POLICY_PATH,
    ledger_dir: LedgerOpt = DEFAULT_LEDGER_DIR,
    on: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD, default today (Asia/Shanghai)")] = None,
) -> None:
    """Start the append-only ledger with the cash you will put into the ETF allocation."""
    try:
        policy, _ = load_policy(policy_file)
        event = init_ledger(
            ledger_dir,
            cash=Decimal(cash),
            event_date=date.fromisoformat(on) if on else _today(),
            note=f"policy={policy.policy_version}",
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    typer.echo(f"ledger initialized: cash={event.cash_amount} event_hash={event.event_hash}")


@etf_app.command("plan")
def plan_cmd(
    quotes_file: Annotated[
        Path | None,
        typer.Option("--quotes-file", dir_okay=False, help="CSV: symbol,last[,bid,ask,iopv,name]"),
    ] = None,
    fetch_sse: Annotated[bool, typer.Option("--fetch-sse", help="Fetch quotes from the SSE public snapshot")] = False,
    force_rebalance: Annotated[bool, typer.Option("--force-rebalance")] = False,
    policy_file: PolicyOpt = DEFAULT_POLICY_PATH,
    ledger_dir: LedgerOpt = DEFAULT_LEDGER_DIR,
    on: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD, default today (Asia/Shanghai)")] = None,
) -> None:
    """Print a manual order ticket. It never sends orders."""
    if (quotes_file is None) == (not fetch_sse):
        raise _fail(ValueError("pass exactly one of --quotes-file or --fetch-sse"))
    try:
        policy, policy_sha = load_policy(policy_file)
        state = load_ledger(ledger_dir)
        quotes = fetch_sse_quotes(policy) if fetch_sse else load_quotes_csv(quotes_file)  # type: ignore[arg-type]
        plan = build_plan(
            policy=policy,
            policy_sha256=policy_sha,
            state=state,
            quotes=quotes,
            as_of=date.fromisoformat(on) if on else _today(),
            force_rebalance=force_rebalance,
        )
        path = save_plan(plan, ledger_dir)
        html_path = save_plan_html(plan, path)
        annual_done = mark_annual_rebalance_done(ledger_dir, plan)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    typer.echo(render_plan(plan))
    if annual_done is not None:
        typer.echo(f"\n年度再平衡已完成，已写入账本（seq={annual_done.seq}），今年不会再次触发。")
    typer.echo(f"\nplan saved: {path}")
    typer.echo(f"图形报告（用浏览器打开）: {html_path.resolve()}")


@etf_app.command("record-fill")
def record_fill_cmd(
    symbol: Annotated[str, typer.Option("--symbol", help="e.g. 510300.SH")],
    side: Annotated[Literal["buy", "sell"], typer.Option("--side")],
    quantity: Annotated[int, typer.Option("--quantity", min=1)],
    price: Annotated[str, typer.Option("--price", help="Average fill price from the broker app")],
    commission: Annotated[
        str | None, typer.Option("--commission", help="Actual commission; estimated from policy if omitted")
    ] = None,
    plan_id: Annotated[str | None, typer.Option("--plan-id")] = None,
    note: Annotated[str, typer.Option("--note")] = "",
    policy_file: PolicyOpt = DEFAULT_POLICY_PATH,
    ledger_dir: LedgerOpt = DEFAULT_LEDGER_DIR,
    on: Annotated[str | None, typer.Option("--date", help="Trade date YYYY-MM-DD, default today")] = None,
) -> None:
    """Append one real fill you executed by hand."""
    try:
        policy, _ = load_policy(policy_file)
        event = record_fill(
            ledger_dir,
            policy,
            symbol=symbol if "." in symbol else f"{symbol}.SH",
            side=side,
            quantity=quantity,
            price=Decimal(price),
            commission_paid=Decimal(commission) if commission is not None else None,
            event_date=date.fromisoformat(on) if on else _today(),
            plan_id=plan_id,
            note=note,
        )
        state = load_ledger(ledger_dir)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    typer.echo(f"recorded seq={event.seq} {event.side} {event.symbol} x{event.quantity} @ {event.price}")
    typer.echo(f"cash={state.cash} holdings={state.holdings}")


@etf_app.command("cash")
def cash_cmd(
    amount: Annotated[
        str, typer.Option("--amount", help="Positive = deposit/interest/dividend, negative = withdrawal")
    ],
    note: Annotated[str, typer.Option("--note", help="Reason, required")],
    ledger_dir: LedgerOpt = DEFAULT_LEDGER_DIR,
    on: Annotated[str | None, typer.Option("--date")] = None,
) -> None:
    """Record a cash movement (deposit, withdrawal, 逆回购利息, ETF 分红 …)."""
    try:
        event_date = date.fromisoformat(on) if on else _today()
        record_cash(ledger_dir, amount=Decimal(amount), event_date=event_date, note=note)
        state = load_ledger(ledger_dir)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    typer.echo(f"cash={state.cash} holdings={state.holdings}")


@etf_app.command("status")
def status_cmd(ledger_dir: LedgerOpt = DEFAULT_LEDGER_DIR) -> None:
    """Verify the ledger hash chain and print cash and holdings."""
    try:
        state = load_ledger(ledger_dir)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    typer.echo(f"ledger_events={len(state.events)} head={state.last_hash}")
    typer.echo(f"cash={state.cash}")
    for symbol, quantity in sorted(state.holdings.items()):
        typer.echo(f"{symbol} {quantity}")
