from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from app.cli import app
from app.manual.muse import BLOCK_BEGIN, BLOCK_END, crontab_block
from app.manual.notify import (
    OutboxNotifier,
    TelegramNotifier,
    ack_messages,
    notifier_from_env,
    pending_messages,
)
from tests.test_etf_manual_allocation import _deployed_ledger

REF = date(2026, 9, 28)


def _jobs(block: str) -> dict[str, str]:
    return {line.split()[6]: " ".join(line.split()[:5]) for line in block.splitlines() if "run.sh" in line}


def test_crontab_converts_shanghai_times_to_local_time() -> None:
    utc, warnings = crontab_block(Path("/r"), local_tz=ZoneInfo("UTC"), reference=REF)
    assert block_ok(utc) and warnings == []
    assert _jobs(utc) == {"repo": "40 6 * * 1,2,3,4,5", "daily": "20 7 * * 1,2,3,4,5", "weekly": "40 7 * * 5"}
    shanghai, _ = crontab_block(Path("/r"), local_tz=ZoneInfo("Asia/Shanghai"), reference=REF)
    assert _jobs(shanghai)["repo"] == "40 14 * * 1,2,3,4,5"


def test_crontab_shifts_weekdays_across_midnight_and_warns_on_dst() -> None:
    block, warnings = crontab_block(Path("/r"), local_tz=ZoneInfo("America/Los_Angeles"), reference=REF)
    jobs = _jobs(block)
    assert jobs["repo"] == "40 23 * * 0,1,2,3,4"  # Mon 14:40 Shanghai = Sun 23:40 PDT
    assert jobs["daily"] == "20 0 * * 1,2,3,4,5"
    assert any("daylight saving" in w for w in warnings)


def block_ok(block: str) -> bool:
    lines = block.splitlines()
    return lines[0] == BLOCK_BEGIN and lines[-1] == BLOCK_END


def test_outbox_notifier_queue_and_ack(tmp_path: Path) -> None:
    outbox = OutboxNotifier(tmp_path / "outbox")
    outbox.send_text("第一条")
    report = tmp_path / "r.html"
    report.write_text("<html></html>")
    outbox.send_document(report, caption="报告")
    messages = pending_messages(tmp_path / "outbox")
    assert [m["text"] for m in messages] == ["第一条", "报告"]
    assert messages[1]["attachments"] == [str(report.resolve())]
    assert ack_messages(tmp_path / "outbox", [str(messages[0]["id"])]) == 1
    assert [m["text"] for m in pending_messages(tmp_path / "outbox")] == ["报告"]
    assert ack_messages(tmp_path / "outbox", []) == 1
    assert pending_messages(tmp_path / "outbox") == []
    assert len(list((tmp_path / "outbox" / "sent").glob("*.json"))) == 2


def test_notifier_channel_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIQ_NOTIFY_CHANNEL", raising=False)
    monkeypatch.delenv("AIQ_TELEGRAM_BOT_TOKEN", raising=False)
    assert isinstance(notifier_from_env(tmp_path), OutboxNotifier)
    monkeypatch.setenv("AIQ_TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("AIQ_TELEGRAM_CHAT_ID", "42")
    assert isinstance(notifier_from_env(tmp_path), TelegramNotifier)
    monkeypatch.setenv("AIQ_NOTIFY_CHANNEL", "outbox")
    assert isinstance(notifier_from_env(tmp_path), OutboxNotifier)
    monkeypatch.setenv("AIQ_NOTIFY_CHANNEL", "whatsapp")
    with pytest.raises(ValueError, match="telegram or outbox"):
        notifier_from_env(tmp_path)


def test_cli_dry_run_never_writes_and_plan_uses_outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.manual.etf_allocation import load_ledger

    ledger = _deployed_ledger(tmp_path / "ledger")
    before = load_ledger(ledger)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["etf", "record-fill", "--symbol", "510300", "--side", "sell", "--quantity", "1200",
         "--price", "7.999", "--commission", "5", "--dry-run", "--ledger-dir", str(ledger)],
    )
    assert result.exit_code == 0, result.output
    assert "预览（未写入）" in result.output and "持仓 3000 份" in result.output
    bad = runner.invoke(
        app,
        ["etf", "record-fill", "--symbol", "518880", "--side", "sell", "--quantity", "99999",
         "--price", "7.5", "--dry-run", "--ledger-dir", str(ledger)],
    )
    assert bad.exit_code == 1 and "exceeds holding" in bad.output
    cash = runner.invoke(
        app, ["etf", "cash", "--amount", "0.31", "--note", "逆回购利息", "--dry-run", "--ledger-dir", str(ledger)]
    )
    assert cash.exit_code == 0 and f"{before.cash + Decimal('0.31')}" in cash.output
    assert load_ledger(ledger).last_hash == before.last_hash

    monkeypatch.setenv("AIQ_NOTIFY_CHANNEL", "outbox")
    quotes = tmp_path / "q.csv"
    quotes.write_text("symbol,last\n510300.SH,8.00\n513500.SH,2.2\n518880.SH,7.5\n511010.SH,141.2\n")
    planned = runner.invoke(
        app,
        ["etf", "plan", "--quotes-file", str(quotes), "--notify", "action", "--ledger-dir", str(ledger),
         "--date", "2026-11-02"],
    )
    assert planned.exit_code == 0, planned.output
    listed = runner.invoke(app, ["etf", "outbox", "--ledger-dir", str(ledger)])
    assert "pending=2" in listed.output and "需要调仓" in listed.output and "[attachment]" in listed.output
