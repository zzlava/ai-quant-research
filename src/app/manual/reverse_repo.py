"""Rule-based reminder for lending idle cash through exchange reverse repo (manual execution only).

Mechanism, not prediction: remind on Thursdays (weekend interest accrues) and whenever the live
1-day repo rate is above a threshold (quarter-end, year-end and pre-holiday squeezes).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.manual.etf_allocation import _SSE_SNAPSHOT_PREFIX, BytesClient, SseSnapshotClient, parse_sse_snapshot

DEFAULT_REPO_POLICY_PATH = Path("config/manual/reverse-repo-v1.json")
_WEEKDAY_NAMES = "一二三四五六日"


class RepoPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    policy_version: str = Field(min_length=1)
    symbol: str = Field(pattern=r"^\d{6}\.SH$")
    product_name: str = Field(min_length=1)
    unit_cny: Decimal = Field(gt=0)
    keep_cash_cny: Decimal = Field(ge=0)
    rate_threshold_pct: Decimal = Field(gt=0)
    remind_weekdays: list[int]
    remind_weekday_note: str
    day_count_basis: Literal[365]
    execution_mode: Literal["manual_orders_only"]


def load_repo_policy(path: Path = DEFAULT_REPO_POLICY_PATH) -> RepoPolicy:
    return RepoPolicy.model_validate_json(Path(path).read_bytes())


def fetch_repo_rate(policy: RepoPolicy, client: BytesClient | None = None) -> tuple[Decimal, date | None]:
    """Latest annualized rate in percent from the SSE snapshot (e.g. 1.85 means 1.85%)."""
    client = client or SseSnapshotClient()
    code = policy.symbol.split(".", 1)[0]
    url = f"{_SSE_SNAPSHOT_PREFIX}{code}?select=name,last,open,high,low,volume,amount,bid,ask"
    quote = parse_sse_snapshot(policy.symbol, client.fetch(url))
    return quote.last, quote.quote_date


@dataclass(frozen=True)
class RepoAdvice:
    as_of: date
    remind: bool
    reasons: list[str]
    lendable_cny: Decimal
    rate_pct: Decimal | None
    accrual_days: int
    est_interest_cny: Decimal | None


def advise(
    policy: RepoPolicy,
    *,
    cash: Decimal,
    as_of: date,
    rate_pct: Decimal | None,
    rate_date: date | None = None,
) -> RepoAdvice:
    usable = max(cash - policy.keep_cash_cny, Decimal("0"))
    lendable = (usable / policy.unit_cny).to_integral_value(rounding=ROUND_FLOOR) * policy.unit_cny
    if rate_date is not None and rate_date != as_of:
        rate_pct = None  # stale quote (market closed): do not act on it
    reasons: list[str] = []
    if as_of.weekday() in policy.remind_weekdays:
        reasons.append(f"今天是周{_WEEKDAY_NAMES[as_of.weekday()]}，做 1 天期通常能拿到周末的利息（约 3 天）")
    if rate_pct is not None and rate_pct >= policy.rate_threshold_pct:
        reasons.append(f"当前利率 {rate_pct}% 不低于提醒门槛 {policy.rate_threshold_pct}%")
    accrual_days = 3 if as_of.weekday() == 3 else 1
    interest = None
    if rate_pct is not None and lendable > 0:
        interest = (lendable * rate_pct / 100 * accrual_days / policy.day_count_basis).quantize(Decimal("0.01"))
    return RepoAdvice(
        as_of=as_of,
        remind=bool(reasons) and lendable > 0 and as_of.weekday() < 5,
        reasons=reasons,
        lendable_cny=lendable,
        rate_pct=rate_pct,
        accrual_days=accrual_days,
        est_interest_cny=interest,
    )


def repo_message(policy: RepoPolicy, advice: RepoAdvice) -> str:
    code = policy.symbol[:6]
    lines = [f"💰 国债逆回购提醒 · {advice.as_of}"]
    lines += [f"· {reason}" for reason in advice.reasons]
    rate = f"{advice.rate_pct}%" if advice.rate_pct is not None else "未取到（请在 App 里看实时利率）"
    lines += [
        "",
        f"可借出：{advice.lendable_cny:,.0f} 元（账本现金按 1000 元取整）",
        f"品种：{code} {policy.product_name}，当前年化 {rate}",
    ]
    if advice.est_interest_cny is not None:
        lines.append(f"预计利息：约 {advice.est_interest_cny} 元（按 {advice.accrual_days} 天估算，以到期实际为准）")
    lines += [
        "",
        f"操作：券商 App →「国债逆回购」→ 选 {code} → 借出（卖出）→ 输入金额。当日 15:30 前可操作。",
        "到期后资金自动回到账户，下一个交易日可用，不影响调仓。",
        "利息到账后发 /cash 利息金额 逆回购利息 记账。",
    ]
    return "\n".join(lines)
