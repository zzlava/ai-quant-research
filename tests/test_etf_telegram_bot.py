from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.manual.etf_allocation import DEFAULT_POLICY_PATH, init_ledger, load_ledger, record_cash
from app.manual.telegram_bot import CONFIRM_TTL_SECONDS, LedgerBot, TelegramApi
from tests.test_etf_manual_allocation import PROJECT_ROOT, _deployed_ledger, _quotes

CHAT = "4242"
NOW = datetime(2026, 11, 2, 16, 0).timestamp()


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []

    def __call__(self, url: str, payload: dict[str, Any], timeout: float) -> bytes:
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, payload))
        result: Any = True
        if method == "getUpdates":
            result, self.updates = self.updates, []
        return json.dumps({"ok": True, "result": result}).encode()

    def texts(self) -> list[str]:
        return [p["text"] for m, p in self.calls if m in ("sendMessage", "editMessageText")]

    def last_buttons(self) -> list[str]:
        for method, payload in reversed(self.calls):
            if method == "sendMessage" and "reply_markup" in payload:
                return [b["callback_data"] for b in payload["reply_markup"]["inline_keyboard"][0]]
        raise AssertionError("no confirmation buttons sent")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _bot(ledger: Path, fake: FakeTelegram, clock: Clock) -> LedgerBot:
    return LedgerBot(
        api=TelegramApi("123:abc", http=fake),
        chat_id=CHAT,
        policy_file=PROJECT_ROOT / DEFAULT_POLICY_PATH,
        ledger_dir=ledger,
        clock=clock,
        fetch_quotes=lambda _policy: _quotes({"510300.SH": Decimal("8.00")}),
    )


def _msg(text: str, *, chat: str = CHAT, at: float = NOW) -> dict[str, Any]:
    return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "date": int(at), "text": text}}


def _press(data: str, *, chat: str = CHAT) -> dict[str, Any]:
    message = {"chat": {"id": int(chat)}, "message_id": 7}
    return {"update_id": 2, "callback_query": {"id": "cb", "data": data, "message": message}}


@pytest.fixture()
def ledger(tmp_path: Path) -> Path:
    return _deployed_ledger(tmp_path)


def test_fill_needs_confirmation_before_writing(ledger: Path) -> None:
    fake, clock = FakeTelegram(), Clock()
    bot = _bot(ledger, fake, clock)
    before = load_ledger(ledger)
    bot.handle(_msg("/fill 510300 卖 1200 7.999 5"))
    assert len(load_ledger(ledger).events) == len(before.events)
    assert "卖出 510300" in fake.texts()[-1] and "记账后 510300 持仓：3000 份" in fake.texts()[-1]
    ok, _ = fake.last_buttons()
    bot.handle(_press(ok))
    after = load_ledger(ledger)
    assert after.holdings["510300.SH"] == 3000
    assert after.cash == before.cash + Decimal("1200") * Decimal("7.999") - Decimal("5")
    assert after.events[-1].note == "recorded via Telegram"
    assert "✅ 已记账" in fake.texts()[-1]
    bot.handle(_press(ok))
    assert "过期或已处理" in fake.texts()[-1]
    assert len(load_ledger(ledger).events) == len(after.events)


def test_fill_links_latest_plan_and_estimates_commission(ledger: Path) -> None:
    fake, clock = FakeTelegram(), Clock()
    bot = _bot(ledger, fake, clock)
    bot.handle(_msg("/plan"))
    plan_id = next(t for t in fake.texts() if "plan_id=" in t).split("plan_id=")[1].split()[0]
    bot.handle(_msg("/fill 510300.sh sell 1200 7.999"))
    assert f"关联调仓单：{plan_id}" in fake.texts()[-1]
    bot.handle(_press(fake.last_buttons()[0]))
    event = load_ledger(ledger).events[-1]
    assert event.plan_id == plan_id and event.commission == Decimal("5.00")


def test_cancel_and_expiry_do_not_write(ledger: Path) -> None:
    fake, clock = FakeTelegram(), Clock()
    bot = _bot(ledger, fake, clock)
    count = len(load_ledger(ledger).events)
    bot.handle(_msg("/cash 3.21 逆回购利息"))
    bot.handle(_press(fake.last_buttons()[1]))
    assert "已取消" in fake.texts()[-1]
    bot.handle(_msg("/cash 3.21 逆回购利息"))
    clock.now += CONFIRM_TTL_SECONDS + 1
    bot.handle(_press(fake.last_buttons()[0]))
    assert "过期" in fake.texts()[-1]
    assert len(load_ledger(ledger).events) == count


def test_confirm_rejected_when_ledger_changed_after_preview(ledger: Path) -> None:
    fake, clock = FakeTelegram(), Clock()
    bot = _bot(ledger, fake, clock)
    bot.handle(_msg("/cash 3.21 逆回购利息"))
    record_cash(ledger, amount=Decimal("1"), event_date=date(2026, 11, 2), note="other writer")
    bot.handle(_press(fake.last_buttons()[0]))
    assert "没有记账" in fake.texts()[-1]
    assert load_ledger(ledger).events[-1].note == "other writer"


def test_other_chats_stale_messages_and_bad_input_are_refused(ledger: Path) -> None:
    fake, clock = FakeTelegram(), Clock()
    bot = _bot(ledger, fake, clock)
    count = len(load_ledger(ledger).events)
    bot.handle(_msg("/fill 510300 卖 100 7 5", chat="999"))
    bot.handle(_press("ok:whatever", chat="999"))
    assert fake.calls == []
    bot.handle(_msg("/fill 510300 卖 100 7 5", at=NOW - 3600))
    assert "太久" in fake.texts()[-1]
    bot.handle(_msg("/fill 518880 卖 99999 7.5"))
    assert "exceeds holding" in fake.texts()[-1]
    bot.handle(_msg("/fill 600000 买 100 10"))
    assert "not in the ETF policy" in fake.texts()[-1]
    bot.handle(_msg("/fill 510300 买 150 4.7"))
    assert "board lot" in fake.texts()[-1]
    bot.handle(_msg("/fill 510300 持有 100 4.7"))
    assert "方向" in fake.texts()[-1]
    assert len(load_ledger(ledger).events) == count


def test_status_help_and_polling_offset(ledger: Path) -> None:
    fake, clock = FakeTelegram(), Clock()
    bot = _bot(ledger, fake, clock)
    fake.updates = [
        {**_msg("/status"), "update_id": 10},
        {**_msg("你好"), "update_id": 11},
    ]
    bot.poll_once(timeout=0)
    assert bot.offset == 12
    assert "现金" in fake.texts()[0] and "510300：4200 份" in fake.texts()[0]
    assert "/fill" in fake.texts()[1]


def test_bot_refuses_without_ledger(tmp_path: Path) -> None:
    fake, clock = FakeTelegram(), Clock()
    init_ledger(tmp_path, cash=Decimal("1000"), event_date=date(2026, 11, 2))
    bot = _bot(tmp_path, fake, clock)
    bot.handle(_msg("/cash -2000 取出"))
    assert "negative" in fake.texts()[-1]
