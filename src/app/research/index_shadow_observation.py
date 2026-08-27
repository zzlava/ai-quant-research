"""Append-only, broker-free shadow observation ledger for the sealed ETF pair."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.index_shadow_execution import (
    SseQuote,
    _commission,
    parse_index_shadow_sse_quote_bytes,
    verify_index_shadow_execution_protocol,
    verify_index_shadow_initialization_report,
)
from app.research.repo_file_safety import resolve_repo_regular_file

DEFAULT_PLAN_PATH = Path("config/research/index-shadow-observation-plan-v1.json")
DEFAULT_INITIALIZATION_PATH = Path("data/shadow/index-risk-budget-shadow-v1/initialization-20260827.json")
DEFAULT_RAW_ROOT = Path("data/raw/index-shadow-observations-v1")
DEFAULT_OBSERVATION_ROOT = Path("data/shadow/index-risk-budget-shadow-v1/observations")
ShadowRole = Literal["equity", "defensive"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShadowProtocolBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class IndexShadowObservationPlan(_StrictModel):
    schema_version: Literal["1"]
    plan_version: Literal["index-shadow-observation-plan-v1"]
    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_on: date
    role: Literal["append_only_execution_semantics_monitoring"]
    shadow_protocol_binding: ShadowProtocolBinding
    cadence: dict[str, Any]
    diagnostic_scope: dict[str, bool]
    append_only_policy: dict[str, bool]
    broker_tariff_input: dict[str, Any]
    authorization_boundary: dict[str, bool]
    readiness: dict[str, bool]

    @model_validator(mode="after")
    def _fail_closed(self) -> IndexShadowObservationPlan:
        if self.sealed_on != date(2026, 8, 28):
            raise ValueError("shadow observation plan seal date drifted")
        expected_cadence = {
            "rule": "weekly_friday_after_close",
            "timezone": "Asia/Shanghai",
            "scheduled_local_time": "16:30",
            "non_trading_friday_policy": "skip_without_backfill",
            "minimum_observations_before_execution_review": 12,
            "minimum_elapsed_calendar_days_before_execution_review": 84,
            "year_end_final_market_day_observation_required": True,
            "next_year_first_market_day_observation_required": True,
        }
        if self.cadence != expected_cadence:
            raise ValueError("shadow observation cadence drifted")
        expected_scope = {
            "quoted_spread": True,
            "visible_level1_depth": True,
            "integer_board_lot_allocation_error": True,
            "minimum_commission_floor": True,
            "assumed_slippage_coverage": True,
            "mark_to_market_weight_drift": True,
            "after_close_quote_is_executable": False,
            "actual_fill_claim": False,
            "performance_or_alpha_claim": False,
        }
        if self.diagnostic_scope != expected_scope:
            raise ValueError("shadow diagnostic scope drifted")
        expected_append_only = {
            "one_record_per_market_date": True,
            "strictly_increasing_market_dates": True,
            "previous_record_hash_chain_required": True,
            "raw_quote_hashes_required": True,
            "overwrite_or_reseal_forbidden": True,
        }
        if self.append_only_policy != expected_append_only:
            raise ValueError("shadow append-only policy drifted")
        expected_tariff = {
            "status": "missing_user_supplied_evidence",
            "current_values_are_research_assumptions_only": True,
            "actual_commission_rate_unknown": True,
            "actual_minimum_commission_unknown": True,
            "actual_etf_eligibility_unknown": True,
        }
        if self.broker_tariff_input != expected_tariff:
            raise ValueError("broker tariff unknown boundaries drifted")
        expected_boundary = {
            "official_sse_public_quote_network_access_authorized": True,
            "local_append_only_artifact_write_authorized": True,
            "broker_credential_access_authorized": False,
            "broker_connection_authorized": False,
            "order_submission_authorized": False,
            "capital_deployment_authorized": False,
            "trading_authorized": False,
        }
        if self.authorization_boundary != expected_boundary:
            raise ValueError("shadow observation authorization boundary drifted")
        expected_readiness = {
            "ready_for_scheduled_shadow_observation": True,
            "ready_for_execution_review": False,
            "ready_for_live_product_mapping": False,
            "ready_for_orders": False,
            "ready_for_trading": False,
        }
        if self.readiness != expected_readiness:
            raise ValueError("shadow observation readiness drifted")
        return self


class ShadowOrderLifecycle(_StrictModel):
    status: Literal["not_submitted"] = "not_submitted"
    requested_quantity: Literal[0] = 0
    submitted_quantity: Literal[0] = 0
    filled_quantity: Literal[0] = 0
    cancelled_quantity: Literal[0] = 0
    broker_order_id: None = None
    broker_timestamp: None = None


class ShadowObservationLeg(_StrictModel):
    role: ShadowRole
    symbol: str
    shadow_quantity: int = Field(gt=0)
    last_price: Decimal = Field(gt=0)
    best_bid: Decimal = Field(gt=0)
    best_bid_size: int = Field(ge=0)
    best_ask: Decimal = Field(gt=0)
    best_ask_size: int = Field(ge=0)
    quoted_spread_bps: Decimal = Field(ge=0)
    quoted_half_spread_bps: Decimal = Field(ge=0)
    assumed_slippage_bps: Decimal = Field(ge=0)
    slippage_assumption_covers_half_spread: bool
    visible_best_ask_depth_ratio: Decimal = Field(ge=0)
    visible_best_ask_covers_shadow_quantity: bool
    marked_value_cny: Decimal = Field(ge=0)
    liquidation_notional_at_best_bid_cny: Decimal = Field(ge=0)
    estimated_liquidation_commission_cny: Decimal = Field(ge=0)
    commission_floor_binding: bool
    order_lifecycle: ShadowOrderLifecycle = Field(default_factory=ShadowOrderLifecycle)
    hypothetical_order_status: Literal["not_submitted"] = "not_submitted"
    after_close_quote_is_executable: Literal[False] = False
    actual_fill_claim: Literal[False] = False


class ShadowObservationReport(_StrictModel):
    schema_version: Literal["1"] = "1"
    report_version: Literal["index-shadow-observation-report-v1"] = "index-shadow-observation-report-v1"
    observation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    initialization_report_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(gt=0)
    record_reason: Literal["weekly", "year_end", "year_start"]
    market_date: date
    observed_at_cst: str
    quote_source: Literal["Shanghai Stock Exchange official snapshot API"]
    quote_raw_paths: dict[ShadowRole, str]
    quote_sha256: dict[ShadowRole, str]
    legs: list[ShadowObservationLeg] = Field(min_length=2, max_length=2)
    residual_virtual_cash_cny: Decimal = Field(ge=0)
    portfolio_marked_value_cny: Decimal = Field(ge=0)
    portfolio_liquidation_estimate_cny: Decimal = Field(ge=0)
    equity_marked_weight: Decimal = Field(ge=0, le=1)
    defensive_marked_weight: Decimal = Field(ge=0, le=1)
    allocation_l1_error_from_30_70: Decimal = Field(ge=0)
    prominent_warning: str
    performance_interpretation_forbidden: Literal[True] = True
    capital_deployment_authorized: Literal[False] = False
    broker_connection_authorized: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False


class ShadowObservationReadiness(_StrictModel):
    observation_count: int = Field(ge=0)
    elapsed_calendar_days: int = Field(ge=0)
    year_end_observation_present: bool
    next_year_start_observation_present: bool
    consecutive_annual_boundary_pair_present: bool
    minimum_count_met: bool
    minimum_elapsed_days_met: bool
    execution_review_eligible: bool
    performance_or_alpha_proven: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False


class BytesClient(Protocol):
    def fetch(self, url: str) -> bytes: ...


class OfficialSseQuoteClient:
    def fetch(self, url: str) -> bytes:
        sealed_prefix = "https://yunhq.sse.com.cn:32042/v1/sh1/snap/"
        if not url.startswith(sealed_prefix):
            raise ValueError("shadow quote URL is outside the sealed SSE snapshot endpoint")
        request = Request(
            url,
            headers={
                "User-Agent": "ai-quant-research/0.1",
                "Referer": "https://www.sse.com.cn/",
            },
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310
            if not response.geturl().startswith(sealed_prefix):
                raise ValueError("shadow quote redirect left the sealed SSE snapshot endpoint")
            payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError("official SSE quote response exceeds size limit")
        return payload


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def compute_shadow_observation_plan_id(plan: IndexShadowObservationPlan) -> str:
    return _json_hash(plan.model_dump(mode="json", exclude={"plan_id"}))


def compute_shadow_observation_id(report: ShadowObservationReport) -> str:
    return _json_hash(report.model_dump(mode="json", exclude={"observation_id"}))


def verify_index_shadow_observation_plan(
    *, repo_root: Path, path: Path = DEFAULT_PLAN_PATH
) -> IndexShadowObservationPlan:
    root = Path(repo_root).resolve(strict=True)
    source = resolve_repo_regular_file(path, repo_root=root, field_name="shadow_observation_plan")
    try:
        plan = IndexShadowObservationPlan.model_validate_json(source.read_text())
    except Exception as exc:
        raise ValueError("index shadow observation plan is missing or invalid") from exc
    if plan.plan_id != compute_shadow_observation_plan_id(plan):
        raise ValueError("index shadow observation plan self-hash mismatch")
    binding = plan.shadow_protocol_binding
    protocol_path = resolve_repo_regular_file(
        Path(binding.path), repo_root=root, field_name="shadow_protocol_binding.path"
    )
    if _sha256_file(protocol_path) != binding.sha256:
        raise ValueError("shadow observation plan protocol hash mismatch")
    protocol = verify_index_shadow_execution_protocol(repo_root=root, path=Path(binding.path), require_evidence=False)
    if protocol.protocol_id != binding.protocol_id:
        raise ValueError("shadow observation plan protocol ID mismatch")
    return plan


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


def _ratio(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.00000001"))


def _validate_record_date(*, expected_date: date, record_reason: Literal["weekly", "year_end", "year_start"]) -> None:
    if record_reason == "weekly" and expected_date.weekday() != 4:
        raise ValueError("weekly shadow observation must be a Friday")
    if record_reason == "year_end" and not (expected_date.month == 12 and expected_date.day >= 24):
        raise ValueError("year-end quote is outside the sealed final-week window")
    if record_reason == "year_start" and not (expected_date.month == 1 and expected_date.day <= 10):
        raise ValueError("year-start quote is outside the sealed first-ten-day window")


def _build_leg(
    *,
    role: ShadowRole,
    symbol: str,
    quantity: int,
    quote: SseQuote,
    slippage_bps: Decimal,
    commission_rate: Decimal,
    minimum_commission: Decimal,
) -> ShadowObservationLeg:
    midpoint = (quote.best_bid + quote.best_ask) / Decimal("2")
    spread_bps = (quote.best_ask - quote.best_bid) / midpoint * Decimal("10000")
    half_spread_bps = spread_bps / Decimal("2")
    marked_value = quote.last * quantity
    liquidation_notional = quote.best_bid * quantity
    raw_commission = liquidation_notional * commission_rate
    commission = _commission(
        liquidation_notional,
        rate=commission_rate,
        minimum=minimum_commission,
    )
    depth_ratio = Decimal(quote.best_ask_size) / Decimal(quantity)
    return ShadowObservationLeg(
        role=role,
        symbol=symbol,
        shadow_quantity=quantity,
        last_price=_money(quote.last),
        best_bid=_money(quote.best_bid),
        best_bid_size=quote.best_bid_size,
        best_ask=_money(quote.best_ask),
        best_ask_size=quote.best_ask_size,
        quoted_spread_bps=_ratio(spread_bps),
        quoted_half_spread_bps=_ratio(half_spread_bps),
        assumed_slippage_bps=slippage_bps,
        slippage_assumption_covers_half_spread=slippage_bps >= half_spread_bps,
        visible_best_ask_depth_ratio=_ratio(depth_ratio),
        visible_best_ask_covers_shadow_quantity=quote.best_ask_size >= quantity,
        marked_value_cny=_money(marked_value),
        liquidation_notional_at_best_bid_cny=_money(liquidation_notional),
        estimated_liquidation_commission_cny=_money(commission),
        commission_floor_binding=raw_commission < minimum_commission,
        order_lifecycle=ShadowOrderLifecycle(),
        hypothetical_order_status="not_submitted",
        after_close_quote_is_executable=False,
        actual_fill_claim=False,
    )


def build_index_shadow_observation(
    *,
    repo_root: Path,
    expected_date: date,
    quote_paths: dict[ShadowRole, Path],
    sequence: int,
    previous_record_id: str,
    record_reason: Literal["weekly", "year_end", "year_start"] = "weekly",
    plan_path: Path = DEFAULT_PLAN_PATH,
    initialization_path: Path = DEFAULT_INITIALIZATION_PATH,
) -> ShadowObservationReport:
    root = Path(repo_root).resolve(strict=True)
    plan = verify_index_shadow_observation_plan(repo_root=root, path=plan_path)
    protocol = verify_index_shadow_execution_protocol(
        repo_root=root,
        path=Path(plan.shadow_protocol_binding.path),
        require_evidence=True,
    )
    initialization = verify_index_shadow_initialization_report(
        repo_root=root,
        report_path=initialization_path,
        protocol_path=Path(plan.shadow_protocol_binding.path),
    )
    if expected_date <= initialization.observed_at_cst.date():
        raise ValueError("shadow observation date must follow initialization")
    _validate_record_date(expected_date=expected_date, record_reason=record_reason)
    quotes: dict[ShadowRole, SseQuote] = {}
    relative_paths: dict[ShadowRole, str] = {}
    roles: tuple[ShadowRole, ShadowRole] = ("equity", "defensive")
    for role in roles:
        source = resolve_repo_regular_file(
            quote_paths[role], repo_root=root, field_name=f"shadow_observation.{role}_quote"
        )
        relative_paths[role] = source.relative_to(root).as_posix()
        quotes[role] = parse_index_shadow_sse_quote_bytes(
            protocol=protocol,
            role=role,
            payload_bytes=source.read_bytes(),
            source_path=relative_paths[role],
            source_sha256=_sha256_file(source),
            expected_date=expected_date,
        )
    if quotes["equity"].observed_at_cst != quotes["defensive"].observed_at_cst:
        raise ValueError("shadow observation quotes must share the exact snapshot time")

    quantities = {leg.role: leg.quantity for leg in initialization.legs}
    slippage_bps = Decimal(str(protocol.cost_policy["slippage_bps_per_side"]))
    commission_rate = Decimal(str(protocol.cost_policy["commission_rate_per_side"]))
    minimum_commission = Decimal(str(protocol.cost_policy["minimum_commission_cny_per_leg"]))
    legs = [
        _build_leg(
            role=role,
            symbol=protocol.product_mappings[role].symbol,
            quantity=quantities[role],
            quote=quotes[role],
            slippage_bps=slippage_bps,
            commission_rate=commission_rate,
            minimum_commission=minimum_commission,
        )
        for role in roles
    ]
    cash = initialization.residual_virtual_cash_cny
    marked_total = cash + sum((leg.marked_value_cny for leg in legs), Decimal("0"))
    liquidation_total = cash + sum(
        (leg.liquidation_notional_at_best_bid_cny - leg.estimated_liquidation_commission_cny for leg in legs),
        Decimal("0"),
    )
    marked_weights = {leg.role: leg.marked_value_cny / marked_total for leg in legs}
    allocation_error = abs(marked_weights["equity"] - Decimal("0.3")) + abs(
        marked_weights["defensive"] - Decimal("0.7")
    )
    payload = {
        "schema_version": "1",
        "report_version": "index-shadow-observation-report-v1",
        "observation_id": "0" * 64,
        "plan_id": plan.plan_id,
        "protocol_id": protocol.protocol_id,
        "initialization_report_id": initialization.report_id,
        "previous_record_id": previous_record_id,
        "sequence": sequence,
        "record_reason": record_reason,
        "market_date": expected_date,
        "observed_at_cst": quotes["equity"].observed_at_cst.isoformat(),
        "quote_source": "Shanghai Stock Exchange official snapshot API",
        "quote_raw_paths": relative_paths,
        "quote_sha256": {role: quotes[role].source_sha256 for role in quotes},
        "legs": legs,
        "residual_virtual_cash_cny": cash,
        "portfolio_marked_value_cny": _money(marked_total),
        "portfolio_liquidation_estimate_cny": _money(liquidation_total),
        "equity_marked_weight": _ratio(marked_weights["equity"]),
        "defensive_marked_weight": _ratio(marked_weights["defensive"]),
        "allocation_l1_error_from_30_70": _ratio(allocation_error),
        "prominent_warning": ("⚠️ 收盘后盘口不可成交；本记录没有提交订单、没有真实成交，且不得解释为收益证据。"),
        "performance_interpretation_forbidden": True,
        "capital_deployment_authorized": False,
        "broker_connection_authorized": False,
        "ready_for_orders": False,
        "ready_for_trading": False,
    }
    report = ShadowObservationReport.model_validate(payload)
    return report.model_copy(update={"observation_id": compute_shadow_observation_id(report)})


def verify_index_shadow_observation_chain(
    *,
    repo_root: Path,
    observation_root: Path = DEFAULT_OBSERVATION_ROOT,
    plan_path: Path = DEFAULT_PLAN_PATH,
    initialization_path: Path = DEFAULT_INITIALIZATION_PATH,
) -> list[ShadowObservationReport]:
    root = Path(repo_root).resolve(strict=True)
    plan = verify_index_shadow_observation_plan(repo_root=root, path=plan_path)
    initialization = verify_index_shadow_initialization_report(
        repo_root=root,
        report_path=initialization_path,
        protocol_path=Path(plan.shadow_protocol_binding.path),
    )
    directory = (root / observation_root).resolve()
    if not directory.is_relative_to(root):
        raise ValueError("shadow observation directory must stay inside repository")
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("shadow observation path must be a regular directory")
    reports: list[ShadowObservationReport] = []
    previous_id = initialization.report_id
    previous_date = initialization.observed_at_cst.date()
    for expected_sequence, path in enumerate(sorted(directory.glob("*.json")), start=1):
        if path.is_symlink() or not path.is_file():
            raise ValueError("shadow observation record must be a regular file")
        try:
            report = ShadowObservationReport.model_validate_json(path.read_text())
        except Exception as exc:
            raise ValueError(f"shadow observation record is invalid: {path.name}") from exc
        if report.observation_id != compute_shadow_observation_id(report):
            raise ValueError(f"shadow observation self-hash mismatch: {path.name}")
        if path.name != f"{report.market_date.isoformat()}.json":
            raise ValueError("shadow observation filename must equal its market date")
        if report.sequence != expected_sequence:
            raise ValueError("shadow observation sequence is not contiguous")
        if report.previous_record_id != previous_id:
            raise ValueError("shadow observation previous-record chain mismatch")
        if report.market_date <= previous_date:
            raise ValueError("shadow observation dates are not strictly increasing")
        expected = build_index_shadow_observation(
            repo_root=root,
            expected_date=report.market_date,
            quote_paths={role: Path(raw_path) for role, raw_path in report.quote_raw_paths.items()},
            sequence=report.sequence,
            previous_record_id=report.previous_record_id,
            record_reason=report.record_reason,
            plan_path=plan_path,
            initialization_path=initialization_path,
        )
        if report != expected:
            raise ValueError(f"shadow observation does not match sealed quote inputs: {path.name}")
        reports.append(report)
        previous_id = report.observation_id
        previous_date = report.market_date
    return reports


def summarize_index_shadow_observation_readiness(
    reports: list[ShadowObservationReport],
) -> ShadowObservationReadiness:
    count = len(reports)
    elapsed_days = (reports[-1].market_date - reports[0].market_date).days if reports else 0
    year_end_present = any(report.record_reason == "year_end" for report in reports)
    year_start_present = any(report.record_reason == "year_start" for report in reports)
    year_end_years = {report.market_date.year for report in reports if report.record_reason == "year_end"}
    year_start_years = {report.market_date.year for report in reports if report.record_reason == "year_start"}
    boundary_pair_present = any(year + 1 in year_start_years for year in year_end_years)
    count_met = count >= 12
    elapsed_met = elapsed_days >= 84
    return ShadowObservationReadiness(
        observation_count=count,
        elapsed_calendar_days=elapsed_days,
        year_end_observation_present=year_end_present,
        next_year_start_observation_present=year_start_present,
        consecutive_annual_boundary_pair_present=boundary_pair_present,
        minimum_count_met=count_met,
        minimum_elapsed_days_met=elapsed_met,
        execution_review_eligible=(count_met and elapsed_met and boundary_pair_present),
        performance_or_alpha_proven=False,
        ready_for_orders=False,
        ready_for_trading=False,
    )


def collect_index_shadow_observation(
    *,
    repo_root: Path,
    expected_date: date,
    client: BytesClient | None = None,
    prefetched_payloads: dict[ShadowRole, bytes] | None = None,
    record_reason: Literal["weekly", "year_end", "year_start"] = "weekly",
    raw_root: Path = DEFAULT_RAW_ROOT,
    observation_root: Path = DEFAULT_OBSERVATION_ROOT,
    plan_path: Path = DEFAULT_PLAN_PATH,
    initialization_path: Path = DEFAULT_INITIALIZATION_PATH,
) -> ShadowObservationReport:
    root = Path(repo_root).resolve(strict=True)
    plan = verify_index_shadow_observation_plan(repo_root=root, path=plan_path)
    protocol = verify_index_shadow_execution_protocol(
        repo_root=root,
        path=Path(plan.shadow_protocol_binding.path),
        require_evidence=True,
    )
    reports = verify_index_shadow_observation_chain(
        repo_root=root,
        observation_root=observation_root,
        plan_path=plan_path,
        initialization_path=initialization_path,
    )
    if reports and expected_date == reports[-1].market_date:
        if reports[-1].record_reason != record_reason:
            raise ValueError("existing shadow observation has a different sealed record reason")
        return reports[-1]
    if reports and expected_date < reports[-1].market_date:
        raise ValueError("shadow observation date cannot precede the append-only ledger tail")
    initialization = verify_index_shadow_initialization_report(
        repo_root=root,
        report_path=initialization_path,
        protocol_path=Path(plan.shadow_protocol_binding.path),
    )
    if expected_date <= initialization.observed_at_cst.date():
        raise ValueError("shadow observation date must follow initialization")
    _validate_record_date(expected_date=expected_date, record_reason=record_reason)
    previous_id = reports[-1].observation_id if reports else initialization.report_id
    sequence = len(reports) + 1
    if client is not None and prefetched_payloads is not None:
        raise ValueError("provide either a quote client or prefetched payloads, not both")
    source_client = client or OfficialSseQuoteClient()
    payloads: dict[ShadowRole, bytes] = {}
    quotes: dict[ShadowRole, SseQuote] = {}
    roles: tuple[ShadowRole, ShadowRole] = ("equity", "defensive")
    for role in roles:
        url = protocol.official_evidence[f"{role}_initial_quote"].url
        payload = prefetched_payloads[role] if prefetched_payloads is not None else source_client.fetch(url)
        if len(payload) > 1024 * 1024:
            raise ValueError(f"shadow quote payload exceeds size limit: {role}")
        payloads[role] = payload
        quotes[role] = parse_index_shadow_sse_quote_bytes(
            protocol=protocol,
            role=role,
            payload_bytes=payload,
            source_path=f"pending:{role}",
            source_sha256=_sha256_bytes(payload),
            expected_date=expected_date,
        )
    if quotes["equity"].observed_at_cst != quotes["defensive"].observed_at_cst:
        raise ValueError("live SSE shadow quotes must share the exact snapshot time")
    raw_directory = (root / raw_root / expected_date.isoformat()).resolve()
    if not raw_directory.is_relative_to(root):
        raise ValueError("shadow raw quote directory must stay inside repository")
    quote_paths: dict[ShadowRole, Path] = {}
    for role in roles:
        symbol = protocol.product_mappings[role].symbol.split(".", 1)[0]
        destination = raw_directory / f"{symbol}-sse-snap.json"
        payload = payloads[role]
        if destination.exists():
            if destination.read_bytes() != payload:
                raise ValueError(f"existing raw shadow quote drifted: {role}")
        else:
            _atomic_bytes(destination, payload)
        quote_paths[role] = destination.relative_to(root)
    report = build_index_shadow_observation(
        repo_root=root,
        expected_date=expected_date,
        quote_paths=quote_paths,
        sequence=sequence,
        previous_record_id=previous_id,
        record_reason=record_reason,
        plan_path=plan_path,
        initialization_path=initialization_path,
    )
    destination = (root / observation_root / f"{expected_date.isoformat()}.json").resolve()
    if not destination.is_relative_to(root):
        raise ValueError("shadow observation output must stay inside repository")
    report_payload = report.model_dump(mode="json")
    if destination.exists():
        if json.loads(destination.read_text()) != report_payload:
            raise ValueError("existing shadow observation record drifted")
    else:
        _atomic_json(destination, report_payload)
    verified = verify_index_shadow_observation_chain(
        repo_root=root,
        observation_root=observation_root,
        plan_path=plan_path,
        initialization_path=initialization_path,
    )
    if not verified or verified[-1] != report:
        raise ValueError("new shadow observation failed append-only verification")
    return report


def collect_index_shadow_year_boundary_observation(
    *,
    repo_root: Path,
    calendar_year: int,
    record_reason: Literal["year_end", "year_start"],
    client: BytesClient | None = None,
) -> ShadowObservationReport:
    root = Path(repo_root).resolve(strict=True)
    reports = verify_index_shadow_observation_chain(repo_root=root)
    existing = [
        report
        for report in reports
        if report.market_date.year == calendar_year and report.record_reason == record_reason
    ]
    if len(existing) > 1:
        raise ValueError("multiple shadow observations claim the same annual boundary")
    if existing:
        return existing[0]

    plan = verify_index_shadow_observation_plan(repo_root=root)
    protocol = verify_index_shadow_execution_protocol(
        repo_root=root,
        path=Path(plan.shadow_protocol_binding.path),
        require_evidence=True,
    )
    source_client = client or OfficialSseQuoteClient()
    roles: tuple[ShadowRole, ShadowRole] = ("equity", "defensive")
    payloads: dict[ShadowRole, bytes] = {}
    raw_dates: dict[ShadowRole, date] = {}
    for role in roles:
        payload = source_client.fetch(protocol.official_evidence[f"{role}_initial_quote"].url)
        payloads[role] = payload
        try:
            raw_dates[role] = datetime.strptime(str(json.loads(payload)["date"]), "%Y%m%d").date()
        except Exception as exc:
            raise ValueError(f"invalid annual-boundary SSE quote: {role}") from exc
    if raw_dates["equity"] != raw_dates["defensive"]:
        raise ValueError("annual-boundary SSE quote dates do not match")
    market_date = raw_dates["equity"]
    if market_date.year != calendar_year:
        raise ValueError("annual-boundary quote is not yet in the requested calendar year")
    if record_reason == "year_end" and not (market_date.month == 12 and market_date.day >= 24):
        raise ValueError("year-end quote is outside the sealed final-week window")
    if record_reason == "year_start" and not (market_date.month == 1 and market_date.day <= 10):
        raise ValueError("year-start quote is outside the sealed first-ten-day window")
    return collect_index_shadow_observation(
        repo_root=root,
        expected_date=market_date,
        prefetched_payloads=payloads,
        record_reason=record_reason,
    )


__all__ = [
    "DEFAULT_OBSERVATION_ROOT",
    "DEFAULT_PLAN_PATH",
    "DEFAULT_RAW_ROOT",
    "IndexShadowObservationPlan",
    "OfficialSseQuoteClient",
    "ShadowOrderLifecycle",
    "ShadowObservationReport",
    "ShadowObservationReadiness",
    "build_index_shadow_observation",
    "collect_index_shadow_observation",
    "collect_index_shadow_year_boundary_observation",
    "compute_shadow_observation_id",
    "compute_shadow_observation_plan_id",
    "summarize_index_shadow_observation_readiness",
    "verify_index_shadow_observation_chain",
    "verify_index_shadow_observation_plan",
]
