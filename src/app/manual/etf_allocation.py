"""Manual multi-asset ETF allocation: policy, append-only ledger, and order tickets.

This module never connects to a broker. It only turns a fixed strategic weight
policy, the user's recorded holdings, and quotes into a manual order ticket, and
records the fills the user reports back.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_POLICY_PATH = Path("config/manual/etf-multi-asset-policy-v1.json")
DEFAULT_LEDGER_DIR = Path("data/manual/etf-multi-asset-v1")
LEDGER_FILENAME = "ledger.jsonl"
_CST = timezone(timedelta(hours=8))
_SSE_SNAPSHOT_PREFIX = "https://yunhq.sse.com.cn:32042/v1/sh1/snap/"
_GENESIS_HASH = "0" * 64


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyAsset(_StrictModel):
    symbol: str = Field(pattern=r"^\d{6}\.SH$")
    role: str = Field(min_length=1)
    expected_name: str = Field(min_length=1)
    target_weight: Decimal = Field(gt=0, lt=1)
    board_lot: int = Field(gt=0)
    qdii: bool


class RebalancePolicy(_StrictModel):
    absolute_band: Decimal = Field(gt=0, lt=1)
    relative_band: Decimal = Field(gt=0, lt=1)
    annual_month: int = Field(ge=1, le=12)
    min_trade_cny: Decimal = Field(ge=0)


class CostPolicy(_StrictModel):
    commission_rate_per_side: Decimal = Field(ge=0, lt=Decimal("0.01"))
    minimum_commission_cny: Decimal = Field(ge=0)
    slippage_bps_when_no_quote: Decimal = Field(ge=0)
    user_must_confirm_actual_broker_rates: bool


class EtfPolicy(_StrictModel):
    schema_version: Literal["1"]
    policy_version: str = Field(min_length=1)
    adopted_on: date
    user_instruction_text: str = Field(min_length=1)
    execution_mode: Literal["manual_orders_only"]
    broker_connection: Literal[False]
    weights_basis: str = Field(min_length=1)
    initial_capital_cny: Decimal = Field(gt=0)
    assets: list[PolicyAsset] = Field(min_length=1, max_length=8)
    cash_target_weight: Decimal = Field(ge=0, lt=1)
    rebalance: RebalancePolicy
    cost: CostPolicy
    qdii_max_premium: Decimal = Field(ge=0)
    hard_limits: dict[str, Literal[True]]

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> EtfPolicy:
        total = sum((asset.target_weight for asset in self.assets), Decimal("0")) + self.cash_target_weight
        if total != Decimal("1"):
            raise ValueError(f"asset target weights plus cash must equal 1, got {total}")
        symbols = [asset.symbol for asset in self.assets]
        if len(set(symbols)) != len(symbols):
            raise ValueError("policy symbols must be unique")
        return self

    def asset(self, symbol: str) -> PolicyAsset:
        for asset in self.assets:
            if asset.symbol == symbol:
                return asset
        raise ValueError(f"symbol is not in the ETF policy: {symbol}")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> tuple[EtfPolicy, str]:
    raw = Path(path).read_bytes()
    return EtfPolicy.model_validate_json(raw), _sha256_bytes(raw)


def commission(notional: Decimal, cost: CostPolicy) -> Decimal:
    if notional <= 0:
        return Decimal("0")
    return max(notional * cost.commission_rate_per_side, cost.minimum_commission_cny).quantize(Decimal("0.01"))


# --------------------------------------------------------------------------- ledger


class LedgerEvent(_StrictModel):
    seq: int = Field(ge=0)
    kind: Literal["init", "fill", "cash"]
    event_date: date
    recorded_at: datetime
    symbol: str | None = None
    side: Literal["buy", "sell"] | None = None
    quantity: int | None = Field(default=None, gt=0)
    price: Decimal | None = Field(default=None, gt=0)
    commission: Decimal | None = Field(default=None, ge=0)
    cash_amount: Decimal | None = None
    note: str = ""
    plan_id: str | None = None
    prev_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _shape(self) -> LedgerEvent:
        if self.kind == "fill":
            if None in (self.symbol, self.side, self.quantity, self.price, self.commission):
                raise ValueError("fill events need symbol, side, quantity, price and commission")
        elif self.cash_amount is None:
            raise ValueError(f"{self.kind} events need cash_amount")
        return self


def _event_hash(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "event_hash"}
    return _sha256_bytes(_canonical(body))


class LedgerState(_StrictModel):
    cash: Decimal
    holdings: dict[str, int]
    events: list[LedgerEvent]

    @property
    def last_hash(self) -> str:
        return self.events[-1].event_hash if self.events else _GENESIS_HASH

    def fill_dates(self) -> list[date]:
        return [event.event_date for event in self.events if event.kind == "fill"]


def _apply(state_cash: Decimal, holdings: dict[str, int], event: LedgerEvent) -> Decimal:
    if event.kind in ("init", "cash"):
        assert event.cash_amount is not None
        state_cash += event.cash_amount
    else:
        assert event.symbol and event.quantity and event.price is not None and event.commission is not None
        notional = event.price * event.quantity
        held = holdings.get(event.symbol, 0)
        if event.side == "buy":
            state_cash -= notional + event.commission
            holdings[event.symbol] = held + event.quantity
        else:
            if event.quantity > held:
                raise ValueError(
                    f"ledger seq {event.seq}: sell {event.quantity} exceeds holding {held} of {event.symbol}"
                )
            state_cash += notional - event.commission
            holdings[event.symbol] = held - event.quantity
    if state_cash < 0:
        raise ValueError(f"ledger seq {event.seq}: cash would become negative ({state_cash})")
    return state_cash


def load_ledger(ledger_dir: Path = DEFAULT_LEDGER_DIR) -> LedgerState:
    path = Path(ledger_dir) / LEDGER_FILENAME
    if not path.exists():
        raise ValueError(f"ledger not initialized: run `etf init` first ({path})")
    cash = Decimal("0")
    holdings: dict[str, int] = {}
    events: list[LedgerEvent] = []
    prev = _GENESIS_HASH
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        event = LedgerEvent.model_validate(payload)
        if event.seq != len(events):
            raise ValueError(f"ledger line {line_no}: sequence gap")
        if event.prev_hash != prev or _event_hash(payload) != event.event_hash:
            raise ValueError(f"ledger line {line_no}: hash chain broken — the ledger was edited")
        if (event.kind == "init") != (event.seq == 0):
            raise ValueError(f"ledger line {line_no}: init must be the first and only init event")
        cash = _apply(cash, holdings, event)
        events.append(event)
        prev = event.event_hash
    if not events:
        raise ValueError("ledger is empty")
    held = {symbol: quantity for symbol, quantity in holdings.items() if quantity}
    return LedgerState(cash=cash.quantize(Decimal("0.01")), holdings=held, events=events)


def _append(ledger_dir: Path, state: LedgerState | None, fields: dict[str, Any]) -> LedgerEvent:
    seq = len(state.events) if state else 0
    payload: dict[str, Any] = {
        "seq": seq,
        "recorded_at": datetime.now(_CST).isoformat(),
        "prev_hash": state.last_hash if state else _GENESIS_HASH,
        "note": "",
        **fields,
    }
    payload = json.loads(_canonical(payload))
    payload["event_hash"] = _event_hash(payload)
    event = LedgerEvent.model_validate(payload)
    if state is not None:
        _apply(state.cash, dict(state.holdings), event)
    path = Path(ledger_dir) / LEDGER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return event


def init_ledger(ledger_dir: Path, *, cash: Decimal, event_date: date, note: str = "") -> LedgerEvent:
    if (Path(ledger_dir) / LEDGER_FILENAME).exists():
        raise ValueError("ledger already exists; it is append-only and cannot be re-initialized")
    if cash <= 0:
        raise ValueError("initial cash must be positive")
    return _append(
        ledger_dir, None, {"kind": "init", "event_date": event_date.isoformat(), "cash_amount": str(cash), "note": note}
    )


def record_fill(
    ledger_dir: Path,
    policy: EtfPolicy,
    *,
    symbol: str,
    side: Literal["buy", "sell"],
    quantity: int,
    price: Decimal,
    commission_paid: Decimal | None,
    event_date: date,
    plan_id: str | None = None,
    note: str = "",
) -> LedgerEvent:
    asset = policy.asset(symbol)
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if side == "buy" and quantity % asset.board_lot:
        raise ValueError(f"buy quantity must be a multiple of the {asset.board_lot}-unit board lot")
    fee = commission_paid if commission_paid is not None else commission(price * quantity, policy.cost)
    state = load_ledger(ledger_dir)
    return _append(
        ledger_dir,
        state,
        {
            "kind": "fill",
            "event_date": event_date.isoformat(),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": str(price),
            "commission": str(fee),
            "plan_id": plan_id,
            "note": note,
        },
    )


def record_cash(ledger_dir: Path, *, amount: Decimal, event_date: date, note: str) -> LedgerEvent:
    if amount == 0:
        raise ValueError("cash adjustment must be non-zero")
    if not note.strip():
        raise ValueError("cash adjustments need a reason (deposit, withdrawal, 逆回购利息, 分红 …)")
    state = load_ledger(ledger_dir)
    fields = {"kind": "cash", "event_date": event_date.isoformat(), "cash_amount": str(amount), "note": note}
    return _append(ledger_dir, state, fields)


# --------------------------------------------------------------------------- quotes


class Quote(_StrictModel):
    symbol: str
    name: str = ""
    last: Decimal = Field(gt=0)
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    iopv: Decimal | None = Field(default=None, gt=0)
    source: str


def load_quotes_csv(path: Path) -> dict[str, Quote]:
    """Read `symbol,last[,bid,ask,iopv,name]` rows typed from the trading app."""
    quotes: dict[str, Quote] = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = (row.get("symbol") or "").strip()
            if not symbol:
                continue

            def _opt(key: str, current: dict[str, str] = row) -> Decimal | None:
                value = (current.get(key) or "").strip()
                return Decimal(value) if value else None

            quotes[symbol] = Quote(
                symbol=symbol,
                name=(row.get("name") or "").strip(),
                last=Decimal(row["last"].strip()),
                bid=_opt("bid"),
                ask=_opt("ask"),
                iopv=_opt("iopv"),
                source=f"csv:{Path(path).name}",
            )
    return quotes


class BytesClient(Protocol):
    def fetch(self, url: str) -> bytes: ...


class SseSnapshotClient:
    def fetch(self, url: str) -> bytes:
        if not url.startswith(_SSE_SNAPSHOT_PREFIX):
            raise ValueError("quote URL is outside the SSE snapshot endpoint")
        request = Request(url, headers={"User-Agent": "ai-quant-research/0.1", "Referer": "https://www.sse.com.cn/"})
        with urlopen(request, timeout=30) as response:  # noqa: S310
            if not response.geturl().startswith(_SSE_SNAPSHOT_PREFIX):
                raise ValueError("SSE quote redirect left the snapshot endpoint")
            payload: bytes = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError("SSE quote response exceeds size limit")
        return payload


def parse_sse_snapshot(symbol: str, payload: bytes) -> Quote:
    try:
        data = json.loads(payload)
        snap = data["snap"]
        quote = Quote(
            symbol=symbol,
            name=str(snap[0]),
            last=Decimal(str(snap[1])),
            bid=Decimal(str(snap[7][0])) if Decimal(str(snap[7][0])) > 0 else None,
            ask=Decimal(str(snap[8][0])) if Decimal(str(snap[8][0])) > 0 else None,
            source=f"sse:{data['date']}T{str(data['time']).zfill(6)}",
        )
    except Exception as exc:
        raise ValueError(f"invalid SSE snapshot for {symbol}") from exc
    if data.get("code") != symbol.split(".", 1)[0]:
        raise ValueError(f"SSE snapshot symbol mismatch for {symbol}")
    return quote


def fetch_sse_quotes(policy: EtfPolicy, client: BytesClient | None = None) -> dict[str, Quote]:
    client = client or SseSnapshotClient()
    quotes = {}
    for asset in policy.assets:
        code = asset.symbol.split(".", 1)[0]
        url = f"{_SSE_SNAPSHOT_PREFIX}{code}?select=name,last,open,high,low,volume,amount,bid,ask"
        quotes[asset.symbol] = parse_sse_snapshot(asset.symbol, client.fetch(url))
    return quotes


# --------------------------------------------------------------------------- planning


class LegView(_StrictModel):
    symbol: str
    name: str
    role: str
    target_weight: Decimal
    quantity: int
    last: Decimal
    value: Decimal
    weight: Decimal
    band: Decimal
    out_of_band: bool


class OrderLine(_StrictModel):
    symbol: str
    name: str
    side: Literal["buy", "sell"]
    quantity: int
    limit_price: Decimal
    notional: Decimal
    est_commission: Decimal
    manual_checks: list[str]


class PostTradeLeg(_StrictModel):
    symbol: str
    quantity: int
    weight: Decimal
    target_weight: Decimal


class EtfPlan(_StrictModel):
    plan_id: str
    as_of: date
    policy_version: str
    policy_sha256: str
    ledger_head_hash: str
    quote_sources: dict[str, str]
    total_value: Decimal
    cash: Decimal
    cash_weight: Decimal
    legs: list[LegView]
    triggers: list[str]
    orders: list[OrderLine]
    post_trade: list[PostTradeLeg]
    post_trade_cash: Decimal
    post_trade_cash_weight: Decimal
    blocked: list[str]
    warnings: list[str]
    manual_orders_only: Literal[True] = True
    broker_connection: Literal[False] = False


def _q(value: Decimal, places: str = "0.0001") -> Decimal:
    return value.quantize(Decimal(places))


def _buy_price(quote: Quote, cost: CostPolicy) -> Decimal:
    if quote.ask is not None:
        return quote.ask
    return _q(quote.last * (1 + cost.slippage_bps_when_no_quote / Decimal("10000")), "0.001")


def _sell_price(quote: Quote, cost: CostPolicy) -> Decimal:
    if quote.bid is not None:
        return quote.bid
    return _q(quote.last * (1 - cost.slippage_bps_when_no_quote / Decimal("10000")), "0.001")


def _qdii_premium(quote: Quote) -> Decimal | None:
    if quote.iopv is None:
        return None
    return quote.last / quote.iopv - 1


def build_plan(
    *,
    policy: EtfPolicy,
    policy_sha256: str,
    state: LedgerState,
    quotes: dict[str, Quote],
    as_of: date,
    force_rebalance: bool = False,
) -> EtfPlan:
    missing = [asset.symbol for asset in policy.assets if asset.symbol not in quotes]
    if missing:
        raise ValueError(f"missing quotes for: {', '.join(missing)}")
    unknown = sorted(set(state.holdings) - {asset.symbol for asset in policy.assets})
    if unknown:
        raise ValueError(f"ledger holds symbols outside the policy: {', '.join(unknown)}")

    cost = policy.cost
    values = {a.symbol: quotes[a.symbol].last * state.holdings.get(a.symbol, 0) for a in policy.assets}
    total = state.cash + sum(values.values(), Decimal("0"))
    if total <= 0:
        raise ValueError("account value must be positive")

    legs: list[LegView] = []
    triggers: list[str] = []
    for asset in policy.assets:
        quote = quotes[asset.symbol]
        weight = values[asset.symbol] / total
        lot_weight = quote.last * asset.board_lot / total
        rebalance = policy.rebalance
        band = max(rebalance.absolute_band, rebalance.relative_band * asset.target_weight) + lot_weight / 2
        out = abs(weight - asset.target_weight) > band
        if out:
            triggers.append(f"band_breach:{asset.symbol}")
        legs.append(
            LegView(
                symbol=asset.symbol,
                name=quote.name or asset.expected_name,
                role=asset.role,
                target_weight=asset.target_weight,
                quantity=state.holdings.get(asset.symbol, 0),
                last=quote.last,
                value=_q(values[asset.symbol], "0.01"),
                weight=_q(weight),
                band=_q(band),
                out_of_band=out,
            )
        )
    if not any(state.holdings.values()):
        triggers.insert(0, "initial_deployment")
    elif as_of.month == policy.rebalance.annual_month and not any(d.year == as_of.year for d in state.fill_dates()):
        triggers.insert(0, "annual_calendar_rebalance")
    if force_rebalance:
        triggers.insert(0, "user_forced")

    warnings: list[str] = []
    if cost.user_must_confirm_actual_broker_rates:
        warnings.append("佣金按策略默认值估算（万2.5，最低5元）；请在策略文件中改成你券商的实际费率。")
    blocked: list[str] = []
    buy_blocked: set[str] = set()
    for asset in policy.assets:
        if not asset.qdii:
            continue
        premium = _qdii_premium(quotes[asset.symbol])
        if premium is not None and premium > policy.qdii_max_premium:
            buy_blocked.add(asset.symbol)
            blocked.append(
                f"{asset.symbol} 溢价率 {premium:.2%} 超过上限 {policy.qdii_max_premium:.2%}，"
                "本次不买入，资金留作现金。"
            )

    current = {a.symbol: state.holdings.get(a.symbol, 0) for a in policy.assets}
    chosen = current
    if triggers:
        chosen = _optimal_quantities(policy, state.cash, total, quotes, current, buy_blocked)

    orders: list[OrderLine] = []
    cash_after = state.cash
    for asset in policy.assets:
        delta = chosen[asset.symbol] - current[asset.symbol]
        if delta == 0:
            continue
        quote = quotes[asset.symbol]
        side: Literal["buy", "sell"] = "buy" if delta > 0 else "sell"
        price = _buy_price(quote, cost) if side == "buy" else _sell_price(quote, cost)
        notional = price * abs(delta)
        fee = commission(notional, cost)
        cash_after += -notional - fee if side == "buy" else notional - fee
        checks = [f"确认交易软件里证券名称为「{asset.expected_name}」（代码 {asset.symbol[:6]}）。"]
        if asset.qdii and side == "buy":
            premium = _qdii_premium(quote)
            if premium is None:
                checks.append(f"下单前在交易软件查看溢价率（IOPV），超过 {policy.qdii_max_premium:.0%} 就不要买。")
        if quote.ask is None or quote.bid is None:
            checks.append("报价里没有买一/卖一，限价是用最新价加减滑点估出来的，请按实时盘口调整。")
        orders.append(
            OrderLine(
                symbol=asset.symbol,
                name=asset.expected_name,
                side=side,
                quantity=abs(delta),
                limit_price=price,
                notional=_q(notional, "0.01"),
                est_commission=fee,
                manual_checks=checks,
            )
        )
    orders.sort(key=lambda order: order.side != "sell")  # sells first to free cash

    post_total = cash_after + sum((quotes[s].last * q for s, q in chosen.items()), Decimal("0"))
    post_trade = [
        PostTradeLeg(
            symbol=asset.symbol,
            quantity=chosen[asset.symbol],
            weight=_q(quotes[asset.symbol].last * chosen[asset.symbol] / post_total),
            target_weight=asset.target_weight,
        )
        for asset in policy.assets
    ]
    if triggers and not orders:
        warnings.append("触发了再平衡检查，但每一笔调整都低于最小交易额或受整手限制，本次无需下单。")

    body: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "policy_version": policy.policy_version,
        "policy_sha256": policy_sha256,
        "ledger_head_hash": state.last_hash,
        "quote_sources": {s: q.source for s, q in quotes.items() if s in current},
        "total_value": str(_q(total, "0.01")),
        "cash": str(state.cash),
        "cash_weight": str(_q(state.cash / total)),
        "legs": [leg.model_dump(mode="json") for leg in legs],
        "triggers": triggers,
        "orders": [order.model_dump(mode="json") for order in orders],
        "post_trade": [leg.model_dump(mode="json") for leg in post_trade],
        "post_trade_cash": str(_q(cash_after, "0.01")),
        "post_trade_cash_weight": str(_q(cash_after / post_total)),
        "blocked": blocked,
        "warnings": warnings,
    }
    plan_id = _sha256_bytes(_canonical(body))[:16]
    return EtfPlan.model_validate({"plan_id": plan_id, **body})


def _optimal_quantities(
    policy: EtfPolicy,
    cash: Decimal,
    total: Decimal,
    quotes: dict[str, Quote],
    current: dict[str, int],
    buy_blocked: set[str],
) -> dict[str, int]:
    """Pick board-lot quantities closest to target weights, trading as little as possible.

    Each leg may stay unchanged or move to the floor/ceiling lot count of its target.
    Trades below the minimum ticket size are not allowed, and cash may never go
    negative after estimated commissions.
    """
    cost = policy.cost
    options: list[list[int]] = []
    for asset in policy.assets:
        quote = quotes[asset.symbol]
        lot_value = quote.last * asset.board_lot
        ideal_lots = (asset.target_weight * total / lot_value).to_integral_value(rounding=ROUND_FLOOR)
        candidates = {current[asset.symbol]}
        for lots in (int(ideal_lots), int(ideal_lots) + 1):
            quantity = lots * asset.board_lot
            if quantity > current[asset.symbol] and asset.symbol in buy_blocked:
                continue
            candidates.add(quantity)
        options.append(sorted(candidates))

    best: tuple[Decimal, int, tuple[int, ...]] | None = None
    for combo in itertools.product(*options):
        cash_after = cash
        trades = 0
        feasible = True
        for asset, quantity in zip(policy.assets, combo, strict=True):
            delta = quantity - current[asset.symbol]
            if delta == 0:
                continue
            quote = quotes[asset.symbol]
            price = _buy_price(quote, cost) if delta > 0 else _sell_price(quote, cost)
            notional = price * abs(delta)
            if notional < policy.rebalance.min_trade_cny:
                feasible = False
                break
            fee = commission(notional, cost)
            cash_after += -notional - fee if delta > 0 else notional - fee
            trades += 1
        if not feasible or cash_after < 0:
            continue
        error = abs(cash_after / total - policy.cash_target_weight)
        for asset, quantity in zip(policy.assets, combo, strict=True):
            error += abs(quotes[asset.symbol].last * quantity / total - asset.target_weight)
        candidate = (_q(error, "0.000001"), trades, combo)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        return dict(current)
    return {asset.symbol: quantity for asset, quantity in zip(policy.assets, best[2], strict=True)}


def save_plan(plan: EtfPlan, ledger_dir: Path = DEFAULT_LEDGER_DIR) -> Path:
    path = Path(ledger_dir) / "plans" / f"{plan.as_of.isoformat()}-{plan.plan_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return path


def render_plan(plan: EtfPlan) -> str:
    lines = [
        f"ETF 手动调仓单  plan_id={plan.plan_id}  日期={plan.as_of}  策略={plan.policy_version}",
        f"账户总值 {plan.total_value} 元，现金 {plan.cash} 元（{plan.cash_weight:.2%}）",
        "",
        f"{'代码':<11}{'角色':<22}{'持有':>8}{'市值':>12}{'当前':>9}{'目标':>9}{'容忍带':>9}",
    ]
    for leg in plan.legs:
        flag = "  ← 超出" if leg.out_of_band else ""
        lines.append(
            f"{leg.symbol:<11}{leg.role:<22}{leg.quantity:>8}{leg.value:>12}"
            f"{leg.weight:>9.2%}{leg.target_weight:>9.2%}{leg.band:>9.2%}{flag}"
        )
    lines.append("")
    reasons = "、".join(plan.triggers) if plan.triggers else "无（所有资产都在容忍带内，今天不用交易）"
    lines.append("触发原因：" + reasons)
    if plan.orders:
        lines.append("")
        lines.append("请按顺序在券商 App 手动下限价单（先卖后买）：")
        for index, order in enumerate(plan.orders, start=1):
            side = "卖出" if order.side == "sell" else "买入"
            lines.append(
                f"  {index}. {side} {order.symbol[:6]} {order.name} {order.quantity} 份，"
                f"限价 {order.limit_price}，约 {order.notional} 元，预估佣金 {order.est_commission} 元"
            )
            for check in order.manual_checks:
                lines.append(f"     - {check}")
        lines.append("")
        lines.append("调仓后预计权重：")
        for post in plan.post_trade:
            lines.append(f"  {post.symbol} {post.quantity:>6} 份  {post.weight:.2%}（目标 {post.target_weight:.2%}）")
        lines.append(f"  现金 {plan.post_trade_cash} 元（{plan.post_trade_cash_weight:.2%}）")
        lines.append("")
        lines.append(
            "成交后逐笔记账：ai-quant etf record-fill --symbol 代码 --side buy|sell "
            f"--quantity 份数 --price 成交均价 --commission 实际佣金 --plan-id {plan.plan_id}"
        )
    for item in plan.blocked:
        lines.append(f"⛔ {item}")
    for item in plan.warnings:
        lines.append(f"⚠️ {item}")
    lines.append("⚠️ 本工具只生成手动调仓单，不连接券商、不自动下单；权重是事先设定的，没有经过回测优化，不保证收益。")
    return "\n".join(lines)
