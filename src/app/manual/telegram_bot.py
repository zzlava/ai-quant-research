"""Telegram bot for recording manual fills from a phone.

Security model:
- Long polling only (no inbound port); only messages from the configured chat id are handled.
- Every ledger write is a two-step preview → confirm button; nothing is written from a message alone.
- A confirmation is bound to the ledger head it was previewed against and expires after a few minutes.
- The bot never talks to a broker and never places orders.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.request import Request, urlopen

from app.errors import sanitize_error_message
from app.manual.etf_allocation import (
    DEFAULT_LEDGER_DIR,
    DEFAULT_POLICY_PATH,
    EtfPolicy,
    Quote,
    cash_fields,
    fetch_sse_quotes,
    fill_fields,
    latest_plan,
    load_ledger,
    load_policy,
    record_cash,
    record_fill,
    simulate,
)
from app.manual.notify import TelegramNotifier, plan_summary
from app.manual.service import run_plan

_CST = timezone(timedelta(hours=8))
CONFIRM_TTL_SECONDS = 600
MAX_MESSAGE_AGE_SECONDS = 900

_BUY_WORDS = {"buy", "b", "买", "买入"}
_SELL_WORDS = {"sell", "s", "卖", "卖出"}

HELP_TEXT = """ETF 记账机器人（只记账，不下单）

记一笔成交：
/fill 代码 买|卖 份数 成交价 [佣金] [plan_id]
例：/fill 510300 卖 1200 7.999 5
    /fill 518880 买 300 7.5
不填佣金就按策略费率估算；不填 plan_id 会自动关联最近一张含这笔单子的调仓单。

记现金变动：
/cash 金额 原因
例：/cash 3.21 逆回购利息
    /cash -5000 取出

/status  查看现金和持仓
/plan    抓上交所行情重新生成调仓单（带图形报告）
/help    显示本说明

每次记账都会先给你预览，点“✅ 确认记账”才会写入账本。"""


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> bytes:
    request = Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        body: bytes = response.read(4 * 1024 * 1024)
    return body


@dataclass
class TelegramApi:
    token: str
    http: Callable[[str, dict[str, Any], float], bytes] = _post_json

    def call(self, method: str, http_timeout: float = 30, **payload: Any) -> Any:
        raw = json.loads(self.http(f"https://api.telegram.org/bot{self.token}/{method}", payload, http_timeout))
        if raw.get("ok") is not True:
            raise ValueError(f"Telegram {method} failed")
        return raw.get("result")


@dataclass
class Pending:
    kind: Literal["fill", "cash"]
    fields: dict[str, Any]
    ledger_head: str
    created: float
    summary: str


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _parse_decimal(text: str, what: str) -> Decimal:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"{what}不是数字：{text}") from None


def _normalize_symbol(policy: EtfPolicy, raw: str) -> str:
    symbol = raw.upper() if "." in raw else f"{raw}.SH"
    policy.asset(symbol)
    return symbol


def parse_fill(policy: EtfPolicy, args: list[str], *, today: date, ledger_dir: Path) -> dict[str, Any]:
    if len(args) < 4:
        raise ValueError("格式：/fill 代码 买|卖 份数 成交价 [佣金] [plan_id]")
    symbol = _normalize_symbol(policy, args[0])
    word = args[1].lower()
    if word in _BUY_WORDS:
        side: Literal["buy", "sell"] = "buy"
    elif word in _SELL_WORDS:
        side = "sell"
    else:
        raise ValueError(f"方向要写 买 或 卖，收到：{args[1]}")
    try:
        quantity = int(args[2])
    except ValueError:
        raise ValueError(f"份数要是整数：{args[2]}") from None
    price = _parse_decimal(args[3], "成交价")
    commission_paid = _parse_decimal(args[4], "佣金") if len(args) > 4 else None
    plan_id = args[5] if len(args) > 5 else None
    if plan_id is None:
        plan = latest_plan(ledger_dir)
        if plan is not None and any(o.symbol == symbol and o.side == side for o in plan.orders):
            plan_id = plan.plan_id
    return fill_fields(
        policy,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        commission_paid=commission_paid,
        event_date=today,
        plan_id=plan_id,
        note="recorded via Telegram",
    )


def parse_cash(args: list[str], *, today: date) -> dict[str, Any]:
    if len(args) < 2:
        raise ValueError("格式：/cash 金额 原因，例如 /cash 3.21 逆回购利息")
    return cash_fields(amount=_parse_decimal(args[0], "金额"), event_date=today, note=" ".join(args[1:]))


@dataclass
class LedgerBot:
    api: TelegramApi
    chat_id: str
    policy_file: Path = DEFAULT_POLICY_PATH
    ledger_dir: Path = DEFAULT_LEDGER_DIR
    clock: Callable[[], float] = time.time
    fetch_quotes: Callable[[EtfPolicy], dict[str, Quote]] = fetch_sse_quotes
    notifier_factory: Callable[[], TelegramNotifier] | None = None
    pending: dict[str, Pending] = field(default_factory=dict)
    offset: int | None = None

    # ------------------------------------------------------------------ plumbing

    def _today(self) -> date:
        return datetime.fromtimestamp(self.clock(), _CST).date()

    def _say(self, text: str, **extra: Any) -> None:
        self.api.call("sendMessage", chat_id=self.chat_id, text=text[:4000], **extra)

    def poll_once(self, timeout: int = 50) -> None:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if self.offset is not None:
            payload["offset"] = self.offset
        for update in self.api.call("getUpdates", http_timeout=timeout + 10, **payload) or []:
            self.offset = int(update["update_id"]) + 1
            try:
                self.handle(update)
            except Exception as exc:  # noqa: BLE001
                self._say(f"❗ 处理失败：{sanitize_error_message(exc)}")

    def run_forever(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                time.sleep(5)

    # ------------------------------------------------------------------ dispatch

    def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id")) != self.chat_id:
            return
        if self.clock() - float(message.get("date", 0)) > MAX_MESSAGE_AGE_SECONDS:
            self._say("这条消息发出太久了（可能是机器人重启前积压的），为安全起见不处理，请重新发送。")
            return
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            self._say(HELP_TEXT)
            return
        command, *args = text.split()
        command = command.split("@", 1)[0].lower()
        handlers: dict[str, Callable[[list[str]], None]] = {
            "/fill": self._cmd_fill,
            "/cash": self._cmd_cash,
            "/status": lambda _: self._cmd_status(),
            "/plan": lambda _: self._cmd_plan(),
            "/start": lambda _: self._say(HELP_TEXT),
            "/help": lambda _: self._say(HELP_TEXT),
        }
        handler = handlers.get(command)
        if handler is None:
            self._say(f"不认识的命令 {command}\n\n{HELP_TEXT}")
            return
        try:
            handler(args)
        except ValueError as exc:
            self._say(f"⚠️ {sanitize_error_message(exc)}")

    # ------------------------------------------------------------------ commands

    def _preview(self, kind: Literal["fill", "cash"], fields: dict[str, Any], headline: str) -> None:
        state = load_ledger(self.ledger_dir)
        cash_after, holdings_after = simulate(state, fields)
        lines = [headline, f"记账后现金：{_money(cash_after)} 元"]
        if kind == "fill":
            lines.append(f"记账后 {fields['symbol'][:6]} 持仓：{holdings_after.get(fields['symbol'], 0)} 份")
            if fields.get("plan_id"):
                lines.append(f"关联调仓单：{fields['plan_id']}")
        lines.append("")
        lines.append(f"确认后写入账本；{CONFIRM_TTL_SECONDS // 60} 分钟内有效。")
        token = secrets.token_hex(6)
        self.pending = {k: v for k, v in self.pending.items() if self.clock() - v.created < CONFIRM_TTL_SECONDS}
        self.pending[token] = Pending(kind, fields, state.last_hash, self.clock(), headline)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ 确认记账", "callback_data": f"ok:{token}"},
                    {"text": "❌ 取消", "callback_data": f"no:{token}"},
                ]
            ]
        }
        self._say("\n".join(lines), reply_markup=keyboard)

    def _cmd_fill(self, args: list[str]) -> None:
        policy, _ = load_policy(self.policy_file)
        fields = parse_fill(policy, args, today=self._today(), ledger_dir=self.ledger_dir)
        asset = policy.asset(fields["symbol"])
        side = "买入" if fields["side"] == "buy" else "卖出"
        price = Decimal(fields["price"])
        headline = (
            f"📝 {side} {fields['symbol'][:6]} {asset.expected_name}\n"
            f"{fields['quantity']} 份 × {price} = {_money(price * fields['quantity'])} 元，"
            f"佣金 {fields['commission']} 元"
        )
        self._preview("fill", fields, headline)

    def _cmd_cash(self, args: list[str]) -> None:
        fields = parse_cash(args, today=self._today())
        amount = Decimal(fields["cash_amount"])
        direction = "转入" if amount > 0 else "转出"
        self._preview("cash", fields, f"📝 现金{direction} {_money(abs(amount))} 元（{fields['note']}）")

    def _cmd_status(self) -> None:
        state = load_ledger(self.ledger_dir)
        lines = [f"现金：{_money(state.cash)} 元"]
        lines += [f"{symbol[:6]}：{quantity} 份" for symbol, quantity in sorted(state.holdings.items())]
        lines.append(f"账本共 {len(state.events)} 条记录，校验通过。")
        self._say("\n".join(lines))

    def _cmd_plan(self) -> None:
        policy, _ = load_policy(self.policy_file)
        self._say("⏳ 正在抓取上交所行情并生成调仓单…")
        run = run_plan(
            policy_file=self.policy_file,
            ledger_dir=self.ledger_dir,
            quotes=self.fetch_quotes(policy),
            as_of=self._today(),
        )
        self._say(plan_summary(run.plan))
        if run.annual_done is not None:
            self._say("年度再平衡已完成，已写入账本。")
        if self.notifier_factory is not None:
            self.notifier_factory().send_document(
                run.html_path, caption=f"ETF 调仓报告 {run.plan.as_of} · {run.plan.plan_id}"
            )

    # ------------------------------------------------------------------ confirmations

    def _handle_callback(self, query: dict[str, Any]) -> None:
        chat = ((query.get("message") or {}).get("chat") or {}).get("id")
        if str(chat) != self.chat_id:
            return
        self.api.call("answerCallbackQuery", callback_query_id=query["id"])
        action, _, token = str(query.get("data", "")).partition(":")
        message_id = (query.get("message") or {}).get("message_id")
        pending = self.pending.pop(token, None)
        if pending is None or self.clock() - pending.created > CONFIRM_TTL_SECONDS:
            self._edit(message_id, "⌛ 这条预览已过期或已处理，请重新发送命令。")
            return
        if action != "ok":
            self._edit(message_id, f"❌ 已取消，没有记账。\n{pending.summary}")
            return
        policy, _ = load_policy(self.policy_file)
        try:
            event_date = date.fromisoformat(pending.fields["event_date"])
            if pending.kind == "fill":
                f = pending.fields
                event = record_fill(
                    self.ledger_dir,
                    policy,
                    symbol=f["symbol"],
                    side=f["side"],
                    quantity=int(f["quantity"]),
                    price=Decimal(f["price"]),
                    commission_paid=Decimal(f["commission"]),
                    event_date=event_date,
                    plan_id=f.get("plan_id"),
                    note=f.get("note", ""),
                    expected_head=pending.ledger_head,
                )
            else:
                event = record_cash(
                    self.ledger_dir,
                    amount=Decimal(pending.fields["cash_amount"]),
                    event_date=event_date,
                    note=pending.fields["note"],
                    expected_head=pending.ledger_head,
                )
        except ValueError as exc:
            self._edit(message_id, f"⚠️ 没有记账：{sanitize_error_message(exc)}")
            return
        state = load_ledger(self.ledger_dir)
        self._edit(
            message_id,
            f"✅ 已记账（第 {event.seq} 条）\n{pending.summary}\n现金：{_money(state.cash)} 元\n"
            "还有单子没成交完的话继续发 /fill；全部完成后可以发 /plan 复查。",
        )

    def _edit(self, message_id: Any, text: str) -> None:
        if message_id is None:
            self._say(text)
        else:
            self.api.call("editMessageText", chat_id=self.chat_id, message_id=message_id, text=text)
