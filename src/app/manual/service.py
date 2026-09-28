"""Plan pipeline shared by the CLI and the Telegram bot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.manual.etf_allocation import (
    EtfPlan,
    LedgerEvent,
    Quote,
    build_plan,
    load_ledger,
    load_policy,
    mark_annual_rebalance_done,
    save_plan,
)
from app.manual.etf_report import save_plan_html


@dataclass(frozen=True)
class PlanRun:
    plan: EtfPlan
    json_path: Path
    html_path: Path
    annual_done: LedgerEvent | None


def stale_quote_dates(quotes: dict[str, Quote], as_of: date) -> list[date]:
    return sorted({q.quote_date for q in quotes.values() if q.quote_date is not None and q.quote_date != as_of})


def run_plan(
    *,
    policy_file: Path,
    ledger_dir: Path,
    quotes: dict[str, Quote],
    as_of: date,
    force_rebalance: bool = False,
) -> PlanRun:
    policy, policy_sha = load_policy(policy_file)
    plan = build_plan(
        policy=policy,
        policy_sha256=policy_sha,
        state=load_ledger(ledger_dir),
        quotes=quotes,
        as_of=as_of,
        force_rebalance=force_rebalance,
    )
    json_path = save_plan(plan, ledger_dir)
    html_path = save_plan_html(plan, json_path)
    return PlanRun(plan, json_path, html_path, mark_annual_rebalance_done(ledger_dir, plan))
