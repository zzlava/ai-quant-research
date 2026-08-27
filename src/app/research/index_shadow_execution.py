"""Fail-closed shadow execution protocol and deterministic board-lot allocator."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.repo_file_safety import resolve_repo_regular_file

DEFAULT_PROTOCOL_PATH = Path("config/research/index-shadow-execution-protocol-v1.json")
DEFAULT_REPORT_PATH = Path(
    "data/shadow/index-risk-budget-shadow-v1/initialization-20260827.json"
)
_CST = timezone(timedelta(hours=8))


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvidenceBinding(_StrictModel):
    url: str = Field(min_length=1)
    raw_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_role: str = Field(min_length=1)


class ProductMapping(_StrictModel):
    symbol: str = Field(pattern=r"^\d{6}\.SH$")
    exchange: Literal["SSE"]
    official_name: str = Field(min_length=1)
    tracked_benchmark_name: str = Field(min_length=1)
    original_research_proxy: str = Field(min_length=1)
    mapping_role: str = Field(min_length=1)
    exact_research_proxy_match: Literal[False]
    surrogate_difference_acknowledged: Literal[True]
    board_lot_units: Literal[100]
    evidence_keys: list[str] = Field(min_length=1)


class IndexShadowExecutionProtocol(_StrictModel):
    schema_version: Literal["1"]
    protocol_version: Literal["index-shadow-execution-protocol-v1"]
    protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_as_of: date
    user_authorization_text: Literal["进行下一步 进入影子执行验证"]
    role: Literal["shadow_only_execution_semantics_and_cost_validation"]
    source_bindings: dict[str, ArtifactBinding]
    product_mappings: dict[str, ProductMapping]
    official_evidence: dict[str, EvidenceBinding]
    allocation_policy: dict[str, Any]
    cost_policy: dict[str, Any]
    observation_policy: dict[str, Any]
    selection_policy: dict[str, Any]
    manual_promotion_gate: dict[str, Any]
    authorization_boundary: dict[str, bool]
    readiness: dict[str, bool]

    @model_validator(mode="after")
    def _fail_closed(self) -> IndexShadowExecutionProtocol:
        if self.authorized_as_of != date(2026, 8, 27):
            raise ValueError("shadow protocol authorization date drifted")
        if set(self.source_bindings) != {"closeout_protocol", "product_cost_contract"}:
            raise ValueError("shadow source binding set drifted")
        if set(self.product_mappings) != {"equity", "defensive"}:
            raise ValueError("shadow product mapping set drifted")
        expected_products = {
            "equity": ("510300.SH", "H00985.CSI"),
            "defensive": ("511010.SH", "H11010.CSI"),
        }
        for role, (symbol, proxy) in expected_products.items():
            product = self.product_mappings[role]
            if product.symbol != symbol or product.original_research_proxy != proxy:
                raise ValueError(f"shadow {role} product identity drifted")
            if set(product.evidence_keys) != {
                f"{role}_product_summary",
                f"{role}_scale_report",
                f"{role}_market_maker_notice",
                f"{role}_initial_quote",
            }:
                raise ValueError(f"shadow {role} evidence binding drifted")
        expected_evidence = {
            f"{role}_{kind}"
            for role in ("equity", "defensive")
            for kind in ("product_summary", "scale_report", "market_maker_notice", "initial_quote")
        }
        if set(self.official_evidence) != expected_evidence:
            raise ValueError("shadow official evidence set drifted")
        if any(
            not item.url.startswith(("https://www.sse.com.cn/", "https://yunhq.sse.com.cn:"))
            for item in self.official_evidence.values()
        ):
            raise ValueError("shadow evidence must use an official SSE host")

        allocation = self.allocation_policy
        expected_allocation = {
            "virtual_initial_capital_cny": 80000.0,
            "target_equity_weight": 0.3,
            "target_defensive_weight": 0.7,
            "cash_weight_target": 0.0,
            "annual_calendar_rebalance_only": True,
            "intrayear_signal_or_threshold_rebalance_forbidden": True,
            "integer_board_lots_required": True,
            "leverage_forbidden": True,
            "short_sales_forbidden": True,
        }
        if allocation != expected_allocation:
            raise ValueError("shadow allocation policy drifted")
        cost = self.cost_policy
        expected_cost = {
            "broker_tariff_status": "unknown_research_assumption_only",
            "commission_rate_per_side": 0.00025,
            "minimum_commission_cny_per_leg": 5.0,
            "slippage_bps_per_side": 5.0,
            "stamp_tax_rate": 0.0,
            "exchange_handling_fee_treated_as_included_in_commission": True,
            "live_cost_claim_forbidden": True,
        }
        if cost != expected_cost:
            raise ValueError("shadow cost policy drifted")
        observation = self.observation_policy
        if observation.get("initial_snapshot_date") != "2026-08-27":
            raise ValueError("shadow initial snapshot date drifted")
        for key in (
            "official_sse_after_close_quote_required",
            "best_ask_used_for_hypothetical_initial_purchase",
            "best_bid_and_last_recorded_for_spread_diagnostics",
            "quote_hashes_must_be_sealed_in_report",
            "initial_shadow_ledger_creation_is_not_a_policy_rebalance",
            "future_observations_must_be_append_only",
            "performance_claim_forbidden",
        ):
            if observation.get(key) is not True:
                raise ValueError(f"shadow observation rule drifted: {key}")
        selection = self.selection_policy
        for key in (
            "pair_frozen_for_shadow_validation",
            "dynamic_etf_rotation_forbidden",
            "performance_chasing_forbidden",
            "surrogate_mapping_is_not_an_investment_recommendation",
            "real_capital_use_requires_new_product_review_and_user_confirmation",
        ):
            if selection.get(key) is not True:
                raise ValueError(f"shadow selection rule drifted: {key}")
        gate = self.manual_promotion_gate
        if gate.get("required") is not True or gate.get("confirmation_present") is not False:
            raise ValueError("shadow manual promotion gate drifted")
        if not str(gate.get("prominent_warning", "")).startswith("⚠️"):
            raise ValueError("shadow promotion warning must remain prominent")
        expected_boundary = {
            "shadow_math_authorized": True,
            "local_shadow_artifact_write_authorized": True,
            "capital_deployment_authorized": False,
            "broker_connection_authorized": False,
            "broker_credential_access_authorized": False,
            "order_submission_authorized": False,
            "trading_authorized": False,
            "automatic_promotion_authorized": False,
        }
        if self.authorization_boundary != expected_boundary:
            raise ValueError("shadow authorization boundary drifted")
        expected_readiness = {
            "ready_for_shadow_initialization": True,
            "ready_for_shadow_observation": True,
            "ready_for_live_product_mapping": False,
            "ready_for_portfolio_construction": False,
            "ready_for_orders": False,
            "ready_for_trading": False,
        }
        if self.readiness != expected_readiness:
            raise ValueError("shadow readiness boundary drifted")
        return self


class SseQuote(_StrictModel):
    symbol: str
    name: str
    observed_at_cst: datetime
    last: Decimal = Field(gt=0)
    best_bid: Decimal = Field(gt=0)
    best_ask: Decimal = Field(gt=0)
    volume: int = Field(gt=0)
    amount: Decimal = Field(gt=0)
    source_path: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ShadowLeg(_StrictModel):
    role: Literal["equity", "defensive"]
    symbol: str
    official_name: str
    target_weight: Decimal
    last_price: Decimal
    best_bid: Decimal
    best_ask: Decimal
    quoted_spread_bps: Decimal
    assumed_fill_price: Decimal
    board_lot_units: int
    quantity: int
    notional_cny: Decimal
    estimated_commission_cny: Decimal
    initial_weight: Decimal


class ShadowInitializationReport(_StrictModel):
    schema_version: Literal["1"] = "1"
    report_version: Literal["index-shadow-initialization-report-v1"] = (
        "index-shadow-initialization-report-v1"
    )
    report_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at_cst: datetime
    quote_source: Literal["Shanghai Stock Exchange official snapshot API"]
    quote_sha256: dict[Literal["equity", "defensive"], str]
    virtual_initial_capital_cny: Decimal
    legs: list[ShadowLeg] = Field(min_length=2, max_length=2)
    estimated_total_commission_cny: Decimal
    total_hypothetical_debit_cny: Decimal
    residual_virtual_cash_cny: Decimal
    residual_virtual_cash_weight: Decimal
    allocation_l1_error: Decimal
    next_rebalance_rule: Literal[
        "observe prior calendar-year final close; attempt at next calendar-year first market-day close"
    ]
    prominent_warning: str
    initialization_is_policy_rebalance: Literal[False] = False
    surrogate_only: Literal[True] = True
    performance_claim: Literal[False] = False
    capital_deployment_authorized: Literal[False] = False
    broker_connection_authorized: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def compute_shadow_protocol_id(protocol: IndexShadowExecutionProtocol) -> str:
    return _json_hash(protocol.model_dump(mode="json", exclude={"protocol_id"}))


def compute_shadow_report_id(report: ShadowInitializationReport) -> str:
    return _json_hash(report.model_dump(mode="json", exclude={"report_id"}))


def verify_index_shadow_execution_protocol(
    *, repo_root: Path, path: Path = DEFAULT_PROTOCOL_PATH, require_evidence: bool = True
) -> IndexShadowExecutionProtocol:
    root = Path(repo_root).resolve(strict=True)
    resolved = resolve_repo_regular_file(path, repo_root=root, field_name="shadow_protocol_path")
    try:
        protocol = IndexShadowExecutionProtocol.model_validate_json(resolved.read_text())
    except Exception as exc:
        raise ValueError("index shadow execution protocol is missing or invalid") from exc
    if protocol.protocol_id != compute_shadow_protocol_id(protocol):
        raise ValueError("index shadow execution protocol self-hash mismatch")
    if not require_evidence:
        return protocol

    id_fields = {
        "closeout_protocol": "closeout_id",
        "product_cost_contract": "contract_id",
    }
    for name, binding in protocol.source_bindings.items():
        source = resolve_repo_regular_file(
            Path(binding.path), repo_root=root, field_name=f"source_bindings.{name}.path"
        )
        if _sha256_file(source) != binding.sha256:
            raise ValueError(f"shadow source hash mismatch: {name}")
        try:
            payload = json.loads(source.read_text())
        except Exception as exc:
            raise ValueError(f"shadow source is invalid JSON: {name}") from exc
        if payload.get(id_fields[name]) != binding.artifact_id:
            raise ValueError(f"shadow source ID mismatch: {name}")

    for name, evidence in protocol.official_evidence.items():
        source = resolve_repo_regular_file(
            Path(evidence.raw_path), repo_root=root, field_name=f"official_evidence.{name}.raw_path"
        )
        if _sha256_file(source) != evidence.sha256:
            raise ValueError(f"shadow official evidence hash mismatch: {name}")
        payload = source.read_bytes()
        if name.endswith(("product_summary", "scale_report")) and not payload.startswith(b"%PDF-"):
            raise ValueError(f"shadow official PDF evidence has invalid magic: {name}")
        if name.endswith("market_maker_notice") and b"<html" not in payload[:4096].lower():
            raise ValueError(f"shadow market-maker evidence is not HTML: {name}")
        if name.endswith("initial_quote"):
            _parse_sse_quote(protocol=protocol, role=name.split("_", 1)[0], source=source)
    return protocol


def _parse_sse_quote(
    *, protocol: IndexShadowExecutionProtocol, role: str, source: Path
) -> SseQuote:
    product = protocol.product_mappings[role]
    try:
        payload = json.loads(source.read_text())
        snap = payload["snap"]
        bids = snap[7]
        asks = snap[8]
        quote_date = datetime.strptime(str(payload["date"]), "%Y%m%d").date()
        raw_time = str(payload["time"]).zfill(6)
        quote_time = datetime.strptime(raw_time, "%H%M%S").time()
        observed = datetime.combine(quote_date, quote_time, tzinfo=_CST)
        quote = SseQuote(
            symbol=product.symbol,
            name=str(snap[0]),
            observed_at_cst=observed,
            last=Decimal(str(snap[1])),
            best_bid=Decimal(str(bids[0])),
            best_ask=Decimal(str(asks[0])),
            volume=int(snap[5]),
            amount=Decimal(str(snap[6])),
            source_path=source.as_posix(),
            source_sha256=_sha256_file(source),
        )
    except Exception as exc:
        raise ValueError(f"invalid official SSE quote snapshot: {role}") from exc
    if payload.get("code") != product.symbol.split(".", 1)[0]:
        raise ValueError(f"official SSE quote symbol mismatch: {role}")
    expected_date = date.fromisoformat(str(protocol.observation_policy["initial_snapshot_date"]))
    if quote.observed_at_cst.date() != expected_date:
        raise ValueError(f"official SSE quote date mismatch: {role}")
    if quote.observed_at_cst.time().hour < 15:
        raise ValueError(f"official SSE quote is not an after-close snapshot: {role}")
    if quote.best_bid > quote.best_ask:
        raise ValueError(f"official SSE quote is crossed: {role}")
    return quote


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


def _price(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))


def _ratio(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.00000001"))


def _commission(notional: Decimal, *, rate: Decimal, minimum: Decimal) -> Decimal:
    if notional <= 0:
        return Decimal("0")
    return max(notional * rate, minimum)


def _optimal_board_lots(
    *,
    capital: Decimal,
    equity_price: Decimal,
    defensive_price: Decimal,
    board_lot: int,
    equity_target: Decimal,
    defensive_target: Decimal,
    commission_rate: Decimal,
    minimum_commission: Decimal,
) -> tuple[int, int]:
    prices = (equity_price * board_lot, defensive_price * board_lot)
    maximums = tuple(int((capital / price).to_integral_value(rounding=ROUND_FLOOR)) for price in prices)
    best: tuple[Decimal, Decimal, int, int] | None = None
    for equity_lots in range(1, maximums[0] + 1):
        equity_notional = prices[0] * equity_lots
        equity_commission = _commission(
            equity_notional, rate=commission_rate, minimum=minimum_commission
        )
        for defensive_lots in range(1, maximums[1] + 1):
            defensive_notional = prices[1] * defensive_lots
            commission = equity_commission + _commission(
                defensive_notional, rate=commission_rate, minimum=minimum_commission
            )
            debit = equity_notional + defensive_notional + commission
            if debit > capital:
                continue
            error = abs(equity_notional / capital - equity_target) + abs(
                defensive_notional / capital - defensive_target
            )
            residual = capital - debit
            candidate = (error, residual, equity_lots, defensive_lots)
            if best is None or candidate < best:
                best = candidate
    if best is None:
        raise ValueError("virtual capital cannot fund one board lot of each shadow product")
    return best[2], best[3]


def build_index_shadow_initialization(
    *, repo_root: Path, protocol_path: Path = DEFAULT_PROTOCOL_PATH
) -> ShadowInitializationReport:
    root = Path(repo_root).resolve(strict=True)
    protocol = verify_index_shadow_execution_protocol(
        repo_root=root, path=protocol_path, require_evidence=True
    )
    quotes: dict[str, SseQuote] = {}
    for role in ("equity", "defensive"):
        evidence = protocol.official_evidence[f"{role}_initial_quote"]
        source = resolve_repo_regular_file(
            Path(evidence.raw_path), repo_root=root, field_name=f"{role}_initial_quote"
        )
        quotes[role] = _parse_sse_quote(protocol=protocol, role=role, source=source)
    if quotes["equity"].observed_at_cst != quotes["defensive"].observed_at_cst:
        raise ValueError("shadow quotes must share the exact official snapshot time")

    capital = Decimal(str(protocol.allocation_policy["virtual_initial_capital_cny"]))
    targets = {
        "equity": Decimal(str(protocol.allocation_policy["target_equity_weight"])),
        "defensive": Decimal(str(protocol.allocation_policy["target_defensive_weight"])),
    }
    rate = Decimal(str(protocol.cost_policy["commission_rate_per_side"]))
    minimum = Decimal(str(protocol.cost_policy["minimum_commission_cny_per_leg"]))
    slippage = Decimal(str(protocol.cost_policy["slippage_bps_per_side"])) / Decimal("10000")
    fill_prices = {role: quote.best_ask * (Decimal("1") + slippage) for role, quote in quotes.items()}
    board_lot = protocol.product_mappings["equity"].board_lot_units
    equity_lots, defensive_lots = _optimal_board_lots(
        capital=capital,
        equity_price=fill_prices["equity"],
        defensive_price=fill_prices["defensive"],
        board_lot=board_lot,
        equity_target=targets["equity"],
        defensive_target=targets["defensive"],
        commission_rate=rate,
        minimum_commission=minimum,
    )
    lot_counts = {"equity": equity_lots, "defensive": defensive_lots}
    legs: list[ShadowLeg] = []
    total_notional = Decimal("0")
    total_commission = Decimal("0")
    allocation_error = Decimal("0")
    for role in ("equity", "defensive"):
        quote = quotes[role]
        product = protocol.product_mappings[role]
        quantity = lot_counts[role] * product.board_lot_units
        notional = fill_prices[role] * quantity
        commission = _commission(notional, rate=rate, minimum=minimum)
        weight = notional / capital
        spread_bps = (quote.best_ask - quote.best_bid) / quote.last * Decimal("10000")
        legs.append(
            ShadowLeg(
                role=role,
                symbol=product.symbol,
                official_name=product.official_name,
                target_weight=targets[role],
                last_price=_money(quote.last),
                best_bid=_money(quote.best_bid),
                best_ask=_money(quote.best_ask),
                quoted_spread_bps=_ratio(spread_bps),
                assumed_fill_price=_price(fill_prices[role]),
                board_lot_units=product.board_lot_units,
                quantity=quantity,
                notional_cny=_money(notional),
                estimated_commission_cny=_money(commission),
                initial_weight=_ratio(weight),
            )
        )
        total_notional += notional
        total_commission += commission
        allocation_error += abs(weight - targets[role])
    debit = total_notional + total_commission
    residual = capital - debit
    if residual < 0:
        raise ValueError("shadow initialization exceeds virtual capital")
    payload = {
        "schema_version": "1",
        "report_version": "index-shadow-initialization-report-v1",
        "report_id": "0" * 64,
        "protocol_id": protocol.protocol_id,
        "observed_at_cst": quotes["equity"].observed_at_cst,
        "quote_source": "Shanghai Stock Exchange official snapshot API",
        "quote_sha256": {
            role: quotes[role].source_sha256 for role in ("equity", "defensive")
        },
        "virtual_initial_capital_cny": _money(capital),
        "legs": legs,
        "estimated_total_commission_cny": _money(total_commission),
        "total_hypothetical_debit_cny": _money(debit),
        "residual_virtual_cash_cny": _money(residual),
        "residual_virtual_cash_weight": _ratio(residual / capital),
        "allocation_l1_error": _ratio(allocation_error),
        "next_rebalance_rule": (
            "observe prior calendar-year final close; attempt at next calendar-year first market-day close"
        ),
        "prominent_warning": str(protocol.manual_promotion_gate["prominent_warning"]),
        "initialization_is_policy_rebalance": False,
        "surrogate_only": True,
        "performance_claim": False,
        "capital_deployment_authorized": False,
        "broker_connection_authorized": False,
        "ready_for_orders": False,
        "ready_for_trading": False,
    }
    report = ShadowInitializationReport.model_validate(payload)
    return report.model_copy(update={"report_id": compute_shadow_report_id(report)})


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def materialize_index_shadow_initialization(
    *,
    repo_root: Path,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    output_path: Path = DEFAULT_REPORT_PATH,
) -> ShadowInitializationReport:
    root = Path(repo_root).resolve(strict=True)
    report = build_index_shadow_initialization(repo_root=root, protocol_path=protocol_path)
    destination = (root / output_path).resolve()
    if not destination.is_relative_to(root):
        raise ValueError("shadow report output must stay inside the repository")
    payload = report.model_dump(mode="json")
    if destination.exists():
        if json.loads(destination.read_text()) != payload:
            raise ValueError("existing shadow initialization report drifted")
    else:
        _atomic_json(destination, payload)
    verify_index_shadow_initialization_report(
        repo_root=root, report_path=output_path, protocol_path=protocol_path
    )
    return report


def verify_index_shadow_initialization_report(
    *,
    repo_root: Path,
    report_path: Path = DEFAULT_REPORT_PATH,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
) -> ShadowInitializationReport:
    root = Path(repo_root).resolve(strict=True)
    resolved = resolve_repo_regular_file(report_path, repo_root=root, field_name="shadow_report_path")
    try:
        report = ShadowInitializationReport.model_validate_json(resolved.read_text())
    except Exception as exc:
        raise ValueError("index shadow initialization report is missing or invalid") from exc
    if report.report_id != compute_shadow_report_id(report):
        raise ValueError("index shadow initialization report self-hash mismatch")
    expected = build_index_shadow_initialization(repo_root=root, protocol_path=protocol_path)
    if report != expected:
        raise ValueError("index shadow initialization report does not match sealed inputs")
    return report


__all__ = [
    "DEFAULT_PROTOCOL_PATH",
    "DEFAULT_REPORT_PATH",
    "IndexShadowExecutionProtocol",
    "ShadowInitializationReport",
    "build_index_shadow_initialization",
    "compute_shadow_protocol_id",
    "compute_shadow_report_id",
    "materialize_index_shadow_initialization",
    "verify_index_shadow_execution_protocol",
    "verify_index_shadow_initialization_report",
]
