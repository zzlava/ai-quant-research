from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.manual.reverse_repo import DEFAULT_REPO_POLICY_PATH, advise, fetch_repo_rate, load_repo_policy, repo_message
from tests.test_etf_manual_allocation import PROJECT_ROOT

THURSDAY = date(2026, 10, 8)
TUESDAY = date(2026, 10, 13)
SATURDAY = date(2026, 10, 10)


def _policy():  # type: ignore[no-untyped-def]
    return load_repo_policy(PROJECT_ROOT / DEFAULT_REPO_POLICY_PATH)


def test_thursday_reminds_with_three_day_interest() -> None:
    advice = advise(_policy(), cash=Decimal("8451.41"), as_of=THURSDAY, rate_pct=Decimal("1.80"), rate_date=THURSDAY)
    assert advice.remind and advice.lendable_cny == Decimal("8000")
    assert advice.accrual_days == 3
    assert advice.est_interest_cny == Decimal("1.18")  # 8000 * 1.8% * 3 / 365
    text = repo_message(_policy(), advice)
    assert "204001" in text and "8,000" in text and "/cash" in text


def test_high_rate_reminds_on_other_days_and_low_rate_does_not() -> None:
    policy = _policy()
    high = advise(policy, cash=Decimal("5000"), as_of=TUESDAY, rate_pct=Decimal("4.20"), rate_date=TUESDAY)
    assert high.remind and high.accrual_days == 1 and "4.20%" in high.reasons[0]
    low = advise(policy, cash=Decimal("5000"), as_of=TUESDAY, rate_pct=Decimal("1.50"), rate_date=TUESDAY)
    assert not low.remind


def test_no_reminder_without_lendable_cash_weekend_or_stale_rate() -> None:
    policy = _policy()
    assert not advise(policy, cash=Decimal("999.99"), as_of=THURSDAY, rate_pct=Decimal("5"), rate_date=THURSDAY).remind
    assert not advise(policy, cash=Decimal("9000"), as_of=SATURDAY, rate_pct=Decimal("5"), rate_date=SATURDAY).remind
    stale = advise(policy, cash=Decimal("9000"), as_of=TUESDAY, rate_pct=Decimal("5"), rate_date=date(2026, 10, 12))
    assert stale.rate_pct is None and not stale.remind
    unknown = advise(policy, cash=Decimal("9000"), as_of=THURSDAY, rate_pct=None)
    assert unknown.remind and unknown.est_interest_cny is None
    assert "未取到" in repo_message(policy, unknown)


def test_fetch_repo_rate_parses_sse_snapshot() -> None:
    class Client:
        def fetch(self, url: str) -> bytes:
            assert url.startswith("https://yunhq.sse.com.cn:32042/v1/sh1/snap/204001")
            return json.dumps(
                {"code": "204001", "date": 20261008, "time": 144000,
                 "snap": ["GC001", 1.85, 1.9, 2.1, 1.6, 100, 1000.0, [1.84, 10], [1.85, 10]]}
            ).encode()

    rate, quote_date = fetch_repo_rate(_policy(), client=Client())
    assert rate == Decimal("1.85") and quote_date == THURSDAY


def test_bot_repo_command(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.manual.etf_allocation import init_ledger
    from tests.test_etf_telegram_bot import CHAT, FakeTelegram, _bot, _msg

    init_ledger(tmp_path, cash=Decimal("8200"), event_date=THURSDAY)

    class ThursdayClock:
        def __call__(self) -> float:
            from datetime import datetime

            return datetime(2026, 10, 8, 14, 40).timestamp()

    fake = FakeTelegram()
    bot = _bot(tmp_path, fake, ThursdayClock())  # type: ignore[arg-type]
    bot.fetch_repo_rate = lambda _p: (Decimal("2.0"), THURSDAY)
    bot.handle(_msg("/repo", at=ThursdayClock()()))
    assert "国债逆回购提醒" in fake.texts()[-1] and "8,000" in fake.texts()[-1]
    assert CHAT
    with pytest.raises(AssertionError):
        fake.last_buttons()
