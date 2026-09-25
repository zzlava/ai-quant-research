"""Telegram push notifications for ETF plans. Outbound only: the bot never receives or executes commands."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.request import Request, urlopen

from app.manual.etf_allocation import EtfPlan

TOKEN_ENV = "AIQ_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "AIQ_TELEGRAM_CHAT_ID"
_API = "https://api.telegram.org"
_MAX_TEXT = 4000


class HttpPost(Protocol):
    def __call__(self, url: str, body: bytes, content_type: str) -> bytes: ...


def _post(url: str, body: bytes, content_type: str) -> bytes:
    request = Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
    with urlopen(request, timeout=30) as response:  # noqa: S310
        payload: bytes = response.read(1024 * 1024)
    return payload


def _get(url: str) -> bytes:
    with urlopen(Request(url), timeout=30) as response:  # noqa: S310
        payload: bytes = response.read(1024 * 1024)
    return payload


@dataclass(frozen=True)
class TelegramNotifier:
    token: str
    chat_id: str
    post: HttpPost = _post

    @classmethod
    def from_env(cls) -> TelegramNotifier:
        token = os.environ.get(TOKEN_ENV, "").strip()
        chat_id = os.environ.get(CHAT_ID_ENV, "").strip()
        if not token or not chat_id:
            raise ValueError(f"set {TOKEN_ENV} and {CHAT_ID_ENV} to enable Telegram notifications")
        return cls(token=token, chat_id=chat_id)

    def _url(self, method: str) -> str:
        return f"{_API}/bot{self.token}/{method}"

    def _check(self, raw: bytes, method: str) -> None:
        try:
            ok = json.loads(raw).get("ok") is True
        except ValueError:
            ok = False
        if not ok:
            raise ValueError(f"Telegram {method} failed")

    def send_text(self, text: str) -> None:
        body = json.dumps(
            {"chat_id": self.chat_id, "text": text[:_MAX_TEXT], "disable_web_page_preview": True}
        ).encode()
        self._check(self.post(self._url("sendMessage"), body, "application/json"), "sendMessage")

    def send_document(self, path: Path, caption: str = "") -> None:
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []
        for name, value in (("chat_id", self.chat_id), ("caption", caption[:1000])):
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
            "Content-Type: text/html\r\n\r\n".encode()
        )
        parts.append(path.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        raw = self.post(self._url("sendDocument"), b"".join(parts), f"multipart/form-data; boundary={boundary}")
        self._check(raw, "sendDocument")


def discover_chat_ids(token: str) -> list[tuple[str, str]]:
    """Return (chat_id, display name) for chats that recently messaged the bot."""
    raw = json.loads(_get(f"{_API}/bot{token}/getUpdates"))
    if raw.get("ok") is not True:
        raise ValueError("Telegram getUpdates failed; check the bot token")
    found: dict[str, str] = {}
    for update in raw.get("result", []):
        chat = (update.get("message") or update.get("channel_post") or {}).get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            found[str(chat["id"])] = name or chat.get("username", "")
    return sorted(found.items())


NotifyMode = Literal["never", "action", "always"]


def should_notify(plan: EtfPlan, mode: NotifyMode) -> bool:
    if mode == "never":
        return False
    if mode == "always":
        return True
    return bool(plan.orders or plan.blocked)


def plan_summary(plan: EtfPlan) -> str:
    head = "🔴 需要调仓" if plan.orders else ("⛔ 有买入被拦截" if plan.blocked else "🟢 今天不用交易")
    lines = [
        f"{head} · ETF 组合 {plan.as_of}",
        f"总值 {plan.total_value:,.2f} 元 · 现金 {plan.cash_weight * 100:.1f}%",
    ]
    for leg in plan.legs:
        mark = " ⚠️超出容忍带" if leg.out_of_band else ""
        lines.append(f"· {leg.symbol[:6]} {leg.weight * 100:.1f}%（目标 {leg.target_weight * 100:.0f}%）{mark}")
    if plan.orders:
        lines.append("")
        lines.append("请按顺序手动下限价单（先卖后买）：")
        for index, order in enumerate(plan.orders, start=1):
            side = "卖出" if order.side == "sell" else "买入"
            lines.append(
                f"{index}. {side} {order.symbol[:6]} {order.name} {order.quantity} 份 @ {order.limit_price}"
                f"（约 {order.notional:,.0f} 元）"
            )
        lines.append(f"成交后记账时带上 plan_id={plan.plan_id}")
    lines.extend(f"⛔ {item}" for item in plan.blocked)
    lines.append("")
    lines.append("完整图形报告见附件。本消息只是提醒，不会自动下单。")
    return "\n".join(lines)
