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
    fetch_sse_quotes,
    init_ledger,
    load_ledger,
    load_policy,
    load_quotes_csv,
    record_cash,
    record_fill,
    render_plan,
)
from app.manual.notify import (
    CHAT_ID_ENV,
    TOKEN_ENV,
    NotifyMode,
    TelegramNotifier,
    discover_chat_ids,
    plan_summary,
    should_notify,
)
from app.manual.service import run_plan, stale_quote_dates

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
    notify: Annotated[
        NotifyMode,
        typer.Option("--notify", help="Telegram push: never, action (only when orders/blocks), always"),
    ] = "never",
    skip_stale_quotes: Annotated[
        bool,
        typer.Option("--skip-stale-quotes", help="Exit quietly when SSE quotes are not from today (holidays)"),
    ] = False,
    policy_file: PolicyOpt = DEFAULT_POLICY_PATH,
    ledger_dir: LedgerOpt = DEFAULT_LEDGER_DIR,
    on: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD, default today (Asia/Shanghai)")] = None,
) -> None:
    """Print a manual order ticket, optionally pushing it to Telegram. It never sends orders."""
    if (quotes_file is None) == (not fetch_sse):
        raise _fail(ValueError("pass exactly one of --quotes-file or --fetch-sse"))
    notifier = None
    try:
        if notify != "never":
            notifier = TelegramNotifier.from_env()
        as_of = date.fromisoformat(on) if on else _today()
        policy, _ = load_policy(policy_file)
        quotes = fetch_sse_quotes(policy) if fetch_sse else load_quotes_csv(quotes_file)  # type: ignore[arg-type]
        stale = stale_quote_dates(quotes, as_of)
        if skip_stale_quotes and stale:
            typer.echo(f"quotes are from {stale[0]}, not {as_of} (market closed?); nothing to do")
            return
        run = run_plan(
            policy_file=policy_file,
            ledger_dir=ledger_dir,
            quotes=quotes,
            as_of=as_of,
            force_rebalance=force_rebalance,
        )
        plan, path, html_path, annual_done = run.plan, run.json_path, run.html_path, run.annual_done
    except Exception as exc:  # noqa: BLE001
        if notifier is not None:
            try:
                notifier.send_text(f"❗ ETF 调仓检查运行失败：{sanitize_error_message(exc)}")
            except Exception:  # noqa: BLE001
                typer.echo("telegram alert also failed", err=True)
        raise _fail(exc) from None
    typer.echo(render_plan(plan))
    if annual_done is not None:
        typer.echo(f"\n年度再平衡已完成，已写入账本（seq={annual_done.seq}），今年不会再次触发。")
    typer.echo(f"\nplan saved: {path}")
    typer.echo(f"图形报告（用浏览器打开）: {html_path.resolve()}")
    if notifier is not None and should_notify(plan, notify):
        try:
            notifier.send_text(plan_summary(plan))
            notifier.send_document(html_path, caption=f"ETF 调仓报告 {plan.as_of} · {plan.plan_id}")
        except Exception as exc:  # noqa: BLE001
            raise _fail(exc) from None
        typer.echo("telegram: sent")


@etf_app.command("telegram-chat-id")
def telegram_chat_id_cmd() -> None:
    """After you message your bot once, list chat ids that can receive notifications."""
    import os

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise _fail(ValueError(f"set {TOKEN_ENV} first"))
    try:
        chats = discover_chat_ids(token)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    if not chats:
        typer.echo("no chats found: open Telegram, send any message to your bot, then rerun")
    for chat_id, name in chats:
        typer.echo(f"{CHAT_ID_ENV}={chat_id}    # {name}")


@etf_app.command("telegram-test")
def telegram_test_cmd() -> None:
    """Send a test message to the configured chat."""
    try:
        TelegramNotifier.from_env().send_text("✅ ai-quant ETF 提醒已连通。之后调仓提醒会发到这里。")
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    typer.echo("telegram: sent")


@etf_app.command("check-network")
def check_network_cmd(policy_file: PolicyOpt = DEFAULT_POLICY_PATH) -> None:
    """Check that this machine can reach both the SSE quote API and Telegram."""
    import os
    from urllib.request import urlopen

    ok = True
    try:
        policy, _ = load_policy(policy_file)
        quotes = fetch_sse_quotes(policy)
        first = next(iter(quotes.values()))
        typer.echo(f"SSE quotes: OK ({len(quotes)} symbols, {first.symbol} last={first.last} date={first.quote_date})")
    except Exception as exc:  # noqa: BLE001
        ok = False
        typer.echo(f"SSE quotes: FAILED ({sanitize_error_message(exc)})")
    try:
        token = os.environ.get(TOKEN_ENV, "").strip() or "0:invalid"
        with urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=15) as response:  # noqa: S310
            response.read(4096)
        typer.echo("Telegram API: OK")
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "code", None)
        if status in (401, 404):
            typer.echo("Telegram API: reachable (token missing or invalid)")
        else:
            ok = False
            typer.echo(f"Telegram API: FAILED ({exc.__class__.__name__})")
    if not ok:
        raise typer.Exit(code=1)


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


@etf_app.command("bot")
def bot_cmd(
    policy_file: PolicyOpt = DEFAULT_POLICY_PATH,
    ledger_dir: LedgerOpt = DEFAULT_LEDGER_DIR,
) -> None:
    """Run the Telegram bookkeeping bot (long polling). Only the configured chat can use it."""
    from app.manual.telegram_bot import LedgerBot, TelegramApi

    try:
        notifier = TelegramNotifier.from_env()
        load_ledger(ledger_dir)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    bot = LedgerBot(
        api=TelegramApi(notifier.token),
        chat_id=notifier.chat_id,
        policy_file=policy_file,
        ledger_dir=ledger_dir,
        notifier_factory=lambda: notifier,
    )
    typer.echo("telegram bot running (Ctrl+C to stop)")
    bot.run_forever()


@etf_app.command("repo-check")
def repo_check_cmd(
    notify: Annotated[
        NotifyMode,
        typer.Option("--notify", help="Telegram push: never, action (only when a reminder fires), always"),
    ] = "never",
    rate: Annotated[
        str | None, typer.Option("--rate", help="Annualized rate in %, skips fetching from SSE")
    ] = None,
    repo_policy_file: Annotated[Path, typer.Option("--repo-policy-file", dir_okay=False)] = Path(
        "config/manual/reverse-repo-v1.json"
    ),
    ledger_dir: LedgerOpt = DEFAULT_LEDGER_DIR,
    on: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD, default today (Asia/Shanghai)")] = None,
) -> None:
    """Remind to lend idle cash via 1-day exchange reverse repo on Thursdays or when the rate is high."""
    from app.manual.reverse_repo import advise, fetch_repo_rate, load_repo_policy, repo_message

    try:
        notifier = TelegramNotifier.from_env() if notify != "never" else None
        policy = load_repo_policy(repo_policy_file)
        state = load_ledger(ledger_dir)
        as_of = date.fromisoformat(on) if on else _today()
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    rate_pct, rate_date = (Decimal(rate), as_of) if rate is not None else (None, None)
    if rate is None:
        try:
            rate_pct, rate_date = fetch_repo_rate(policy)
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"repo rate unavailable: {sanitize_error_message(exc)}", err=True)
    advice = advise(policy, cash=state.cash, as_of=as_of, rate_pct=rate_pct, rate_date=rate_date)
    rate_text = f"{advice.rate_pct}%" if advice.rate_pct is not None else "未知"
    text = (
        repo_message(policy, advice)
        if advice.remind
        else f"今天不提醒逆回购（可借出 {advice.lendable_cny:,.0f} 元，利率 {rate_text}）"
    )
    typer.echo(text)
    if notifier is not None and (notify == "always" or advice.remind):
        try:
            notifier.send_text(text)
        except Exception as exc:  # noqa: BLE001
            raise _fail(exc) from None
        typer.echo("telegram: sent")
