"""Layer-one index regime / risk-budget pure state machine (E9a).

Maps sealed index-risk feature diagnostics plus account equity evidence into a
discrete stock risk-budget proposal for target trading day D, using only
information as_of P (strict prior market day). Research/implementation only:
never scores, backtests, trades, loads market bars, or writes persistence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.index_risk_features import (
    IndexRiskFeatureReport,
    assert_report_date_window_structure,
    assert_report_self_hash,
)
from app.research.layer_one_index_data_evidence import (
    DEFAULT_EVIDENCE_PATH as DEFAULT_LAYER_ONE_INDEX_DATA_EVIDENCE_PATH,
)
from app.research.layer_one_index_data_evidence import (
    verify_layer_one_index_data_evidence_file,
)
from app.research.layer_one_index_protocol import (
    DEFAULT_LAYER_ONE_INDEX_PROTOCOL_DRAFT_PATH,
    load_layer_one_index_protocol_draft,
    verify_layer_one_index_protocol_draft,
)
from app.research.two_layer_contract import (
    DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH,
    load_two_layer_decision_draft,
    verify_two_layer_decision_draft,
)

LAYER_ONE_REGIME_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_ONE_REGIME_ENGINE_VERSION: Literal["layer-one-regime-engine-v1"] = "layer-one-regime-engine-v1"

BOUND_TWO_LAYER_DECISION_CONTRACT_PATH: Literal["config/research/two-layer-strategy-decision-draft-v1.json"] = (
    "config/research/two-layer-strategy-decision-draft-v1.json"
)
BOUND_TWO_LAYER_DECISION_CONTRACT_ID = "27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6"
BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH: Literal["config/research/layer-one-index-development-protocol-draft-v1.json"] = (
    "config/research/layer-one-index-development-protocol-draft-v1.json"
)
BOUND_LAYER_ONE_INDEX_PROTOCOL_ID = "b7aa9de1539cdd791aee5b74ca8ec3f269b6ed809a070caa917686742c4b1b2f"
BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_PATH: Literal[
    "config/research/layer-one-index-data-evidence-v1.json"
] = "config/research/layer-one-index-data-evidence-v1.json"
BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_ID = "6d7cdbb7ba25191f9d4718ec94b61acf6a18e0ca4ffa6a0984c1abbdc6e42e77"

REQUIRED_TREND_LOOKBACK_BARS: Literal[200] = 200
REQUIRED_VOLATILITY_LOOKBACK_BARS: Literal[60] = 60
REQUIRED_DRAWDOWN_LOOKBACK_BARS: Literal[242] = 242
REQUIRED_ANNUALIZATION_TRADING_DAYS: Literal[242] = 242

ALLOWED_BUDGET_LEVELS: tuple[float, ...] = (0.0, 0.3, 0.6, 0.9)
_BUDGET_ABS_TOL = 1e-12

TREND_POSITIVE_RATIO = 1.03
TREND_NEGATIVE_RATIO = 0.97
VOL_NO_CAP_AT_OR_BELOW = 0.18
VOL_CAP_0_6_THROUGH = 0.27
VOL_CAP_0_3_THROUGH = 0.36
INDEX_DD_FORCE_ZERO = -0.20
INDEX_DD_CAP_0_3 = -0.15
INDEX_DD_CAP_0_6 = -0.10
ACCOUNT_DD_RED_LINE = Decimal("-0.20")
ACCOUNT_DD_RISK_LOCK = Decimal("-0.18")
ACCOUNT_DD_CAP_0_3 = Decimal("-0.15")
ACCOUNT_DD_CAP_0_6 = Decimal("-0.10")
RISK_LOCK_MIN_COOLING_TRADING_DAYS: Literal[20] = 20
UNLOCK_VOL_MUST_BE_STRICTLY_BELOW = 0.27

_HEX64 = r"^[0-9a-f]{64}$"

TrendRegime = Literal["positive", "neutral", "negative"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _finite_number(value: float, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _require_budget_level(value: float, *, field_name: str) -> float:
    number = _finite_number(value, field_name=field_name)
    for level in ALLOWED_BUDGET_LEVELS:
        if abs(number - level) <= _BUDGET_ABS_TOL:
            return level
    raise ValueError(f"{field_name} must be one of {ALLOWED_BUDGET_LEVELS}")


def _reject_blank_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_exact_date(value: object, *, field_name: str) -> date:
    if type(value) is not date:
        raise ValueError(f"{field_name} must be a date")
    return value


def _require_aware_datetime(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _require_hex64(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field_name} must be a 64-char lowercase hex digest")
    return value


def _as_decimal(value: float | int | str | Decimal, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise ValueError(f"{field_name} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class LayerOneRegimePriorState(_StrictModel):
    """Caller-supplied persistence snapshot; never default-constructed as unlocked."""

    applied_stock_budget: float
    risk_lock_active: bool
    risk_lock_triggered_as_of: date | None = None
    red_line_breached: bool = False
    state_id: str | None = Field(default=None, pattern=_HEX64)

    @field_validator("applied_stock_budget")
    @classmethod
    def _budget(cls, value: float) -> float:
        return _require_budget_level(value, field_name="applied_stock_budget")

    @model_validator(mode="after")
    def _lock_fields(self) -> LayerOneRegimePriorState:
        if self.risk_lock_active:
            if self.risk_lock_triggered_as_of is None:
                raise ValueError("risk_lock_triggered_as_of is required when risk_lock_active is true")
            if abs(self.applied_stock_budget) > _BUDGET_ABS_TOL:
                raise ValueError("applied_stock_budget must be 0 while risk_lock_active")
        elif self.risk_lock_triggered_as_of is not None:
            raise ValueError("risk_lock_triggered_as_of must be null when risk_lock_active is false")
        return self


class LayerOneUnlockRequest(_StrictModel):
    """Explicit unlock request; absence means unlock is never considered."""

    request_id: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    requested_at: datetime
    user_confirmed: bool

    @field_validator("request_id", "operator", "reason", mode="before")
    @classmethod
    def _reject_blank(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=info.field_name)

    @field_validator("requested_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, field_name="requested_at")


class LayerOneRegimeNewState(_StrictModel):
    """Persistence payload produced by one evaluation; caller writes storage."""

    applied_stock_budget: float
    risk_lock_active: bool
    risk_lock_triggered_as_of: date | None = None
    red_line_breached: bool
    state_id: str | None = Field(default=None, pattern=_HEX64)

    @field_validator("applied_stock_budget")
    @classmethod
    def _budget(cls, value: float) -> float:
        return _require_budget_level(value, field_name="applied_stock_budget")

    @model_validator(mode="after")
    def _lock_fields(self) -> LayerOneRegimeNewState:
        if self.risk_lock_active:
            if self.risk_lock_triggered_as_of is None:
                raise ValueError("risk_lock_triggered_as_of is required when risk_lock_active is true")
            if abs(self.applied_stock_budget) > _BUDGET_ABS_TOL:
                raise ValueError("applied_stock_budget must be 0 while risk_lock_active")
        elif self.risk_lock_triggered_as_of is not None:
            raise ValueError("risk_lock_triggered_as_of must be null when risk_lock_active is false")
        return self


class LayerOneRegimeDecisionReport(_StrictModel):
    """Sealed layer-one regime decision; never authorizes trading or orders."""

    schema_version: Literal["1"] = LAYER_ONE_REGIME_SCHEMA_VERSION
    engine_version: Literal["layer-one-regime-engine-v1"] = LAYER_ONE_REGIME_ENGINE_VERSION
    decision_id: str | None = Field(default=None, pattern=_HEX64)
    target_trading_day: date
    as_of: date
    evaluated_at: datetime
    data_snapshot_id: str = Field(min_length=1)
    index_risk_feature_report_id: str = Field(pattern=_HEX64)
    index_risk_feature_report: IndexRiskFeatureReport
    index_symbol_input: str = Field(min_length=1)
    market_calendar: list[date]
    market_calendar_id: str = Field(pattern=_HEX64)
    account_equity_evidence_id: str = Field(pattern=_HEX64)
    manual_ceiling_authorization_id: str = Field(pattern=_HEX64)
    two_layer_decision_contract_id: str = Field(pattern=_HEX64)
    two_layer_decision_contract_path: str = Field(min_length=1)
    layer_one_index_protocol_id: str = Field(pattern=_HEX64)
    layer_one_index_protocol_path: str = Field(min_length=1)
    layer_one_index_data_evidence_id: str = Field(pattern=_HEX64)
    layer_one_index_data_evidence_path: str = Field(min_length=1)
    prior_state_id: str = Field(pattern=_HEX64)
    prior_risk_lock_triggered_as_of: date | None = None
    prior_red_line_breached: bool
    account_peak_equity: float
    account_current_equity: float
    account_drawdown: float
    close_to_sma_ratio: float
    realized_volatility_annualized: float
    index_drawdown: float
    trend_regime: TrendRegime
    trend_base_budget: float
    volatility_cap: float
    index_drawdown_cap: float
    account_drawdown_cap: float
    manual_open_ceiling: float
    raw_target_budget: float
    previous_applied_stock_budget: float
    applied_stock_budget: float
    increase_deferred: bool
    increase_deferred_reason: str | None = None
    target_day_is_first_market_trading_day_of_week: bool
    risk_lock_prior_active: bool
    risk_lock_new_active: bool
    risk_lock_triggered_this_decision: bool
    risk_lock_unlocked_this_decision: bool
    risk_lock_triggered_as_of: date | None = None
    risk_lock_cooling_trading_days: int = Field(ge=0)
    unlock_request_id: str | None = None
    unlock_operator: str | None = None
    unlock_reason: str | None = None
    unlock_requested_at: datetime | None = None
    unlock_user_confirmed: bool | None = None
    unlock_request_evidence_id: str | None = Field(default=None, pattern=_HEX64)
    unlock_rejection_reasons: list[str] = Field(default_factory=list)
    red_line_breached: bool
    new_state: LayerOneRegimeNewState
    exact_symbol_identity_verified: Literal[True] = True
    snapshot_full_raw_recomputation_verified: Literal[True] = True
    ready_for_historical_evaluation: Literal[True] = True
    research_only: Literal[True] = True
    implementation_only: Literal[True] = True
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    does_not_trade: Literal[True] = True

    @field_validator(
        "account_peak_equity",
        "account_current_equity",
        "account_drawdown",
        "close_to_sma_ratio",
        "realized_volatility_annualized",
        "index_drawdown",
    )
    @classmethod
    def _finite_features(cls, value: float) -> float:
        return _finite_number(value, field_name="feature")

    @field_validator(
        "trend_base_budget",
        "volatility_cap",
        "index_drawdown_cap",
        "account_drawdown_cap",
        "manual_open_ceiling",
        "raw_target_budget",
        "previous_applied_stock_budget",
        "applied_stock_budget",
    )
    @classmethod
    def _budgets(cls, value: float) -> float:
        return _require_budget_level(value, field_name="budget")

    @field_validator("evaluated_at")
    @classmethod
    def _evaluated_at_aware(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, field_name="evaluated_at")

    @field_validator("unlock_requested_at")
    @classmethod
    def _unlock_requested_at_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_aware_datetime(value, field_name="unlock_requested_at")

    @model_validator(mode="after")
    def _gate_flags(self) -> LayerOneRegimeDecisionReport:
        if self.research_only is not True or self.implementation_only is not True:
            raise ValueError("research_only and implementation_only must remain true")
        if self.ready_for_orders or self.ready_for_trading or self.does_not_trade is not True:
            raise ValueError("ready_for_orders/trading must stay false; does_not_trade must stay true")
        if self.exact_symbol_identity_verified is not True:
            raise ValueError("exact_symbol_identity_verified must remain true")
        if self.snapshot_full_raw_recomputation_verified is not True:
            raise ValueError("snapshot_full_raw_recomputation_verified must remain true")
        if self.ready_for_historical_evaluation is not True:
            raise ValueError("ready_for_historical_evaluation must remain true")
        if self.as_of >= self.target_trading_day:
            raise ValueError("as_of must be strictly before target_trading_day")
        if self.increase_deferred:
            if self.increase_deferred_reason is None or self.increase_deferred_reason.strip() == "":
                raise ValueError("increase_deferred requires increase_deferred_reason")
        elif self.increase_deferred_reason is not None:
            raise ValueError("increase_deferred_reason must be null when increase is not deferred")
        unlock_fields = (
            self.unlock_request_id,
            self.unlock_operator,
            self.unlock_reason,
            self.unlock_requested_at,
            self.unlock_user_confirmed,
            self.unlock_request_evidence_id,
        )
        if any(field is not None for field in unlock_fields) and any(field is None for field in unlock_fields):
            raise ValueError("unlock audit fields must all be present together or all null")
        return self


def map_trend_regime(close_to_sma_ratio: float) -> tuple[TrendRegime, float]:
    ratio = _finite_number(close_to_sma_ratio, field_name="close_to_sma_ratio")
    if ratio > TREND_POSITIVE_RATIO:
        return "positive", 0.9
    if ratio >= TREND_NEGATIVE_RATIO:
        return "neutral", 0.6
    return "negative", 0.3


def map_volatility_cap(realized_volatility_annualized: float) -> float:
    vol = _finite_number(realized_volatility_annualized, field_name="realized_volatility_annualized")
    if vol < 0.0:
        raise ValueError("realized_volatility_annualized must be non-negative")
    if vol <= VOL_NO_CAP_AT_OR_BELOW:
        return 0.9
    if vol <= VOL_CAP_0_6_THROUGH:
        return 0.6
    if vol <= VOL_CAP_0_3_THROUGH:
        return 0.3
    return 0.0


def map_index_drawdown_cap(drawdown: float) -> float:
    dd = _finite_number(drawdown, field_name="index_drawdown")
    if dd <= INDEX_DD_FORCE_ZERO:
        return 0.0
    if dd <= INDEX_DD_CAP_0_3:
        return 0.3
    if dd <= INDEX_DD_CAP_0_6:
        return 0.6
    return 0.9


def compute_account_drawdown_decimal(*, peak_equity: float, current_equity: float) -> Decimal:
    """Exact account drawdown via Decimal(str(...)); no float round fuzz at endpoints."""
    peak = _as_decimal(peak_equity, field_name="account_peak_equity")
    current = _as_decimal(current_equity, field_name="account_current_equity")
    if peak <= 0:
        raise ValueError("account_peak_equity must be finite and strictly positive")
    if current <= 0:
        raise ValueError("account_current_equity must be finite and strictly positive")
    if current > peak:
        raise ValueError(
            "account_current_equity exceeds account_peak_equity; "
            "caller must supply the true historical peak before evaluation"
        )
    return current / peak - Decimal("1")


def assert_account_equity(*, peak_equity: float, current_equity: float) -> float:
    """Validate equities and return drawdown as float(Decimal) for report storage."""
    return float(compute_account_drawdown_decimal(peak_equity=peak_equity, current_equity=current_equity))


def map_account_drawdown_cap(drawdown: float | Decimal) -> tuple[float, bool, bool]:
    """Return (cap, trigger_risk_lock, red_line_breached_this_obs) with Decimal endpoints."""
    dd = _as_decimal(drawdown, field_name="account_drawdown")
    red_line = dd <= ACCOUNT_DD_RED_LINE
    if dd <= ACCOUNT_DD_RISK_LOCK:
        return 0.0, True, red_line
    if dd <= ACCOUNT_DD_CAP_0_3:
        return 0.3, False, red_line
    if dd <= ACCOUNT_DD_CAP_0_6:
        return 0.6, False, red_line
    return 0.9, False, red_line


def assert_market_calendar(values: Sequence[date | Any]) -> list[date]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError("market_calendar must be a sequence of dates")
    if not values:
        raise ValueError("market_calendar must be non-empty")
    cleaned: list[date] = []
    for value in values:
        if type(value) is not date:
            raise ValueError("market_calendar contains a non-date value")
        if cleaned and value <= cleaned[-1]:
            raise ValueError("market_calendar must be strictly increasing with unique dates")
        cleaned.append(value)
    return cleaned


def compute_market_calendar_id(market_calendar: Sequence[date]) -> str:
    calendar = assert_market_calendar(market_calendar)
    payload = json.dumps(
        [day.isoformat() for day in calendar],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def resolve_decision_timing(
    *,
    target_trading_day: date,
    market_calendar: Sequence[date],
) -> tuple[date, bool, list[date]]:
    """Prove P adjacent to D and whether D is first market day of its ISO week."""
    calendar = assert_market_calendar(market_calendar)
    d = _require_exact_date(target_trading_day, field_name="target_trading_day")
    try:
        d_index = calendar.index(d)
    except ValueError as exc:
        raise ValueError("target_trading_day must appear in market_calendar") from exc
    if d_index == 0:
        raise ValueError("no market trading day strictly before target_trading_day")
    p = calendar[d_index - 1]
    if calendar[d_index] != d or p >= d:
        raise ValueError("as_of/P and target_trading_day/D must be adjacent market days")
    prior_same_week = any(day.isocalendar()[:2] == d.isocalendar()[:2] for day in calendar[:d_index])
    is_first = not prior_same_week
    return p, is_first, calendar


def assert_feature_windows_on_calendar(
    report: IndexRiskFeatureReport,
    *,
    calendar: Sequence[date],
) -> None:
    calendar_set = set(assert_market_calendar(calendar))
    for label, window in (
        ("trend_window_dates", report.trend_window_dates),
        ("volatility_price_window_dates", report.volatility_price_window_dates),
        ("drawdown_window_dates", report.drawdown_window_dates),
    ):
        for day in window:
            if day not in calendar_set:
                raise ValueError(f"{label} contains a date absent from market_calendar: {day.isoformat()}")


def assert_index_risk_report_for_regime(
    report: IndexRiskFeatureReport,
    *,
    as_of: date,
    market_calendar: Sequence[date],
) -> None:
    assert_report_self_hash(report)
    assert_report_date_window_structure(report)
    if report.as_of != as_of:
        raise ValueError("index risk feature report as_of must equal decision as_of/P")
    if report.trend_lookback_bars != REQUIRED_TREND_LOOKBACK_BARS:
        raise ValueError("trend_lookback_bars must be exactly 200")
    if report.volatility_lookback_bars != REQUIRED_VOLATILITY_LOOKBACK_BARS:
        raise ValueError("volatility_lookback_bars must be exactly 60")
    if report.drawdown_lookback_bars != REQUIRED_DRAWDOWN_LOOKBACK_BARS:
        raise ValueError("drawdown_lookback_bars must be exactly 242")
    if report.annualization_trading_days_per_year != REQUIRED_ANNUALIZATION_TRADING_DAYS:
        raise ValueError("annualization_trading_days_per_year must be exactly 242")
    if report.ready_for_trading or report.ready_for_scoring or report.ready_for_backtest or report.auto_apply:
        raise ValueError("index risk feature report must not authorize scoring/backtest/trading")
    if not report.index_symbol or report.index_symbol.strip() == "":
        raise ValueError("index_symbol must be a non-empty evidence string")
    report_uses_only_as_of_or_earlier = max(
        report.trend_window_dates[-1],
        report.volatility_price_window_dates[-1],
        report.drawdown_window_dates[-1],
    )
    if report_uses_only_as_of_or_earlier > as_of:
        raise ValueError("index risk feature windows must not include dates after as_of/P")
    assert_feature_windows_on_calendar(report, calendar=market_calendar)


def bind_upstream_contracts(
    *,
    repo_root: Path,
    two_layer_path: Path | None = None,
    layer_one_path: Path | None = None,
) -> tuple[str, str, str, str]:
    """Read upstream sealed configs from disk; fail closed on id drift."""
    root = Path(repo_root).resolve()
    if two_layer_path is not None:
        contract_path = Path(two_layer_path)
    else:
        contract_path = root / BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
    if layer_one_path is not None:
        protocol_path = Path(layer_one_path)
    else:
        protocol_path = root / BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH
    if not contract_path.is_file():
        raise ValueError(f"two-layer decision contract missing: {contract_path}")
    if not protocol_path.is_file():
        raise ValueError(f"layer-one index protocol missing: {protocol_path}")

    contract = load_two_layer_decision_draft(contract_path)
    contract_result = verify_two_layer_decision_draft(contract)
    if contract_result.contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
        raise ValueError("two-layer decision contract_id drifted from E9a bound constant")
    if str(DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH) != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two-layer decision default path drifted from E9a binding")

    protocol = load_layer_one_index_protocol_draft(protocol_path)
    protocol_result = verify_layer_one_index_protocol_draft(protocol)
    if protocol_result.protocol_id != BOUND_LAYER_ONE_INDEX_PROTOCOL_ID:
        raise ValueError("layer-one index protocol_id drifted from E9a bound constant")
    if str(DEFAULT_LAYER_ONE_INDEX_PROTOCOL_DRAFT_PATH) != BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH:
        raise ValueError("layer-one protocol default path drifted from E9a binding")
    protocol_bound_contract = getattr(protocol, "two_layer_decision_contract_id", None)
    if protocol_bound_contract is not None and protocol_bound_contract != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
        raise ValueError("layer-one protocol bound two-layer contract_id drifted")

    rel_contract = _repo_relative_posix(contract_path, repo_root=root)
    rel_protocol = _repo_relative_posix(protocol_path, repo_root=root)
    if rel_contract != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two-layer decision contract path must match bound relative path")
    if rel_protocol != BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH:
        raise ValueError("layer-one index protocol path must match bound relative path")
    return (
        contract_result.contract_id,
        rel_contract,
        protocol_result.protocol_id,
        rel_protocol,
    )


def bind_index_data_evidence(
    *,
    repo_root: Path,
    evidence_path: Path | None = None,
) -> tuple[str, str, str, str]:
    """Verify the exact CSI All-Share data receipt and return its frozen bindings."""
    root = Path(repo_root).resolve()
    requested_path = evidence_path or Path(BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_PATH)
    path = requested_path if requested_path.is_absolute() else root / requested_path
    evidence = verify_layer_one_index_data_evidence_file(
        evidence_path=path,
        repo_root=root,
    )
    if evidence.evidence_id != BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_ID:
        raise ValueError("layer-one index data evidence_id drifted from E9a binding")
    if str(DEFAULT_LAYER_ONE_INDEX_DATA_EVIDENCE_PATH) != BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_PATH:
        raise ValueError("layer-one index data evidence default path drifted")
    relative = _repo_relative_posix(Path(path), repo_root=root)
    if relative != BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_PATH:
        raise ValueError("layer-one index data evidence path must match bound relative path")
    if evidence.evidence_id is None:
        raise ValueError("layer-one index data evidence_id is missing")
    return (
        evidence.evidence_id,
        relative,
        evidence.snapshot_manifest.artifact_id,
        evidence.risk_state_index.symbol,
    )


def _repo_relative_posix(path: Path, *, repo_root: Path) -> str:
    resolved = Path(path).resolve()
    root = Path(repo_root).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("upstream path must be inside repo_root") from exc


def _count_cooling_days(
    *,
    calendar: Sequence[date],
    lock_triggered_as_of: date,
    as_of: date,
) -> int:
    if lock_triggered_as_of > as_of:
        raise ValueError("risk_lock_triggered_as_of cannot be after current as_of")
    return sum(1 for day in calendar if lock_triggered_as_of < day <= as_of)


def assert_prior_state_sealed(prior_state: LayerOneRegimePriorState) -> None:
    if prior_state.state_id is None:
        raise ValueError("prior_state.state_id is required; unsealed prior is rejected")
    expected = compute_state_id(prior_state)
    if prior_state.state_id != expected:
        raise ValueError("prior_state.state_id does not match canonical content hash")


def assert_new_state_sealed(state: LayerOneRegimeNewState) -> None:
    if state.state_id is None:
        raise ValueError("new_state.state_id is required")
    expected = compute_state_id(state)
    if state.state_id != expected:
        raise ValueError("new_state.state_id does not match canonical content hash")


def evaluate_unlock_request(
    *,
    prior_locked: bool,
    lock_triggered_as_of: date | None,
    cooling_trading_days: int,
    trend_regime: TrendRegime,
    realized_volatility_annualized: float,
    unlock_request: LayerOneUnlockRequest | None,
    target_trading_day: date,
    evaluated_at: datetime,
    current_observation_triggers_lock: bool,
) -> tuple[bool, list[str]]:
    """Return (unlocked, rejection_reasons). Never auto-unlock without a request."""
    if not prior_locked:
        return False, []
    if unlock_request is None:
        return False, ["no_explicit_unlock_request"]

    reasons: list[str] = []
    if current_observation_triggers_lock:
        reasons.append("current_observation_triggers_risk_lock")
    if not unlock_request.user_confirmed:
        reasons.append("user_confirmation_missing")
    if cooling_trading_days < RISK_LOCK_MIN_COOLING_TRADING_DAYS:
        reasons.append(
            f"cooling_trading_days_below_minimum:{cooling_trading_days}<{RISK_LOCK_MIN_COOLING_TRADING_DAYS}"
        )
    if trend_regime == "negative":
        reasons.append("trend_regime_negative")
    vol = _finite_number(realized_volatility_annualized, field_name="realized_volatility_annualized")
    if not (vol < UNLOCK_VOL_MUST_BE_STRICTLY_BELOW):
        reasons.append(f"realized_vol_not_strictly_below_{UNLOCK_VOL_MUST_BE_STRICTLY_BELOW}")
    _require_aware_datetime(unlock_request.requested_at, field_name="requested_at")
    evaluated = _require_aware_datetime(evaluated_at, field_name="evaluated_at")
    if unlock_request.requested_at > evaluated:
        reasons.append("unlock_requested_at_after_evaluated_at")
    requested_day = unlock_request.requested_at.date()
    if requested_day > target_trading_day:
        reasons.append("unlock_requested_at_in_future_vs_target_trading_day")
    if lock_triggered_as_of is None:
        reasons.append("missing_risk_lock_triggered_as_of")
    elif requested_day < lock_triggered_as_of:
        reasons.append("unlock_requested_at_before_risk_lock_triggered_as_of")
    return (len(reasons) == 0), reasons


def apply_weekly_budget_adjustment(
    *,
    raw_target_budget: float,
    previous_applied_stock_budget: float,
    target_day_is_first_market_trading_day_of_week: bool,
    risk_lock_active: bool,
) -> tuple[float, bool, str | None]:
    raw = _require_budget_level(raw_target_budget, field_name="raw_target_budget")
    previous = _require_budget_level(previous_applied_stock_budget, field_name="previous_applied_stock_budget")
    if risk_lock_active:
        return 0.0, False, None
    if raw < previous - _BUDGET_ABS_TOL:
        return raw, False, None
    if raw > previous + _BUDGET_ABS_TOL:
        if target_day_is_first_market_trading_day_of_week:
            return raw, False, None
        return (
            previous,
            True,
            "increase_only_on_first_market_trading_day_of_week",
        )
    return previous, False, None


def evaluate_layer_one_regime(
    *,
    target_trading_day: date,
    market_calendar: Sequence[date],
    index_risk_report: IndexRiskFeatureReport,
    account_peak_equity: float,
    account_current_equity: float,
    account_equity_evidence_id: str,
    manual_open_ceiling: float,
    manual_ceiling_authorization_id: str,
    prior_state: LayerOneRegimePriorState | None,
    evaluated_at: datetime,
    repo_root: Path,
    unlock_request: LayerOneUnlockRequest | None = None,
    two_layer_path: Path | None = None,
    layer_one_path: Path | None = None,
    index_data_evidence_path: Path | None = None,
) -> LayerOneRegimeDecisionReport:
    """Pure evaluation for target day D using as_of=P evidence only."""
    if prior_state is None:
        raise ValueError("prior_state is required; service restart must not invent an unlocked default state")
    assert_prior_state_sealed(prior_state)
    evaluated = _require_aware_datetime(evaluated_at, field_name="evaluated_at")
    equity_evidence = _require_hex64(account_equity_evidence_id, field_name="account_equity_evidence_id")
    ceiling_auth = _require_hex64(manual_ceiling_authorization_id, field_name="manual_ceiling_authorization_id")

    as_of, is_first_of_week, calendar = resolve_decision_timing(
        target_trading_day=target_trading_day,
        market_calendar=market_calendar,
    )
    calendar_id = compute_market_calendar_id(calendar)
    assert_index_risk_report_for_regime(index_risk_report, as_of=as_of, market_calendar=calendar)
    if index_risk_report.report_id is None:
        raise ValueError("index risk feature report_id is missing")

    account_dd_decimal = compute_account_drawdown_decimal(
        peak_equity=account_peak_equity,
        current_equity=account_current_equity,
    )
    account_dd = float(account_dd_decimal)
    ceiling = _require_budget_level(manual_open_ceiling, field_name="manual_open_ceiling")

    contract_id, contract_path, protocol_id, protocol_path = bind_upstream_contracts(
        repo_root=repo_root,
        two_layer_path=two_layer_path,
        layer_one_path=layer_one_path,
    )
    data_evidence_id, data_evidence_path, bound_snapshot_id, bound_risk_symbol = bind_index_data_evidence(
        repo_root=repo_root,
        evidence_path=index_data_evidence_path,
    )
    if index_risk_report.data_snapshot_id != bound_snapshot_id:
        raise ValueError("index risk feature data_snapshot_id does not match verified CSI snapshot")
    if index_risk_report.index_symbol != bound_risk_symbol:
        raise ValueError("index risk feature symbol does not match verified CSI risk-state identity")

    trend_regime, trend_base = map_trend_regime(index_risk_report.close_to_sma_ratio)
    vol_cap = map_volatility_cap(index_risk_report.realized_volatility_annualized)
    index_dd_cap = map_index_drawdown_cap(index_risk_report.drawdown)
    account_dd_cap, trigger_lock_now, red_line_now = map_account_drawdown_cap(account_dd_decimal)

    raw_target = min(trend_base, vol_cap, index_dd_cap, account_dd_cap, ceiling)
    raw_target = _require_budget_level(raw_target, field_name="raw_target_budget")

    prior_locked = prior_state.risk_lock_active
    lock_triggered_as_of = prior_state.risk_lock_triggered_as_of
    cooling_days = 0
    if prior_locked:
        if lock_triggered_as_of is None:
            raise ValueError("prior risk lock missing risk_lock_triggered_as_of")
        if lock_triggered_as_of not in calendar:
            raise ValueError("risk_lock_triggered_as_of must appear in market_calendar")
        cooling_days = _count_cooling_days(
            calendar=calendar,
            lock_triggered_as_of=lock_triggered_as_of,
            as_of=as_of,
        )

    unlocked, unlock_rejections = evaluate_unlock_request(
        prior_locked=prior_locked,
        lock_triggered_as_of=lock_triggered_as_of,
        cooling_trading_days=cooling_days,
        trend_regime=trend_regime,
        realized_volatility_annualized=index_risk_report.realized_volatility_annualized,
        unlock_request=unlock_request,
        target_trading_day=target_trading_day,
        evaluated_at=evaluated,
        current_observation_triggers_lock=trigger_lock_now,
    )

    risk_lock_active = prior_locked
    risk_lock_triggered_this = False
    risk_lock_unlocked_this = False

    # Current observation trigger always wins over unlock.
    if trigger_lock_now:
        risk_lock_active = True
        if not prior_locked:
            risk_lock_triggered_this = True
            lock_triggered_as_of = as_of
            cooling_days = 0
        # Keep original trigger date when already locked.
    elif prior_locked and unlocked:
        risk_lock_active = False
        risk_lock_unlocked_this = True
        lock_triggered_as_of = None
        cooling_days = 0
    elif prior_locked:
        risk_lock_active = True

    if risk_lock_active:
        raw_for_apply = 0.0
    else:
        raw_for_apply = raw_target

    previous_budget = prior_state.applied_stock_budget
    applied, increase_deferred, deferred_reason = apply_weekly_budget_adjustment(
        raw_target_budget=raw_for_apply,
        previous_applied_stock_budget=previous_budget,
        target_day_is_first_market_trading_day_of_week=is_first_of_week,
        risk_lock_active=risk_lock_active,
    )

    red_line = bool(prior_state.red_line_breached or red_line_now)
    new_state = LayerOneRegimeNewState(
        applied_stock_budget=applied if not risk_lock_active else 0.0,
        risk_lock_active=risk_lock_active,
        risk_lock_triggered_as_of=lock_triggered_as_of,
        red_line_breached=red_line,
    )
    new_state = seal_layer_one_regime_state(new_state)

    unlock_request_id: str | None = None
    unlock_operator: str | None = None
    unlock_reason: str | None = None
    unlock_requested_at: datetime | None = None
    unlock_user_confirmed: bool | None = None
    unlock_request_evidence_id: str | None = None
    if unlock_request is not None:
        unlock_request_id = unlock_request.request_id
        unlock_operator = unlock_request.operator
        unlock_reason = unlock_request.reason
        unlock_requested_at = unlock_request.requested_at
        unlock_user_confirmed = unlock_request.user_confirmed
        unlock_request_evidence_id = compute_unlock_request_evidence_id(unlock_request)

    if prior_state.state_id is None:
        raise ValueError("prior_state.state_id is required")

    report = LayerOneRegimeDecisionReport(
        target_trading_day=target_trading_day,
        as_of=as_of,
        evaluated_at=evaluated,
        data_snapshot_id=index_risk_report.data_snapshot_id,
        index_risk_feature_report_id=index_risk_report.report_id,
        index_risk_feature_report=index_risk_report,
        index_symbol_input=index_risk_report.index_symbol,
        market_calendar=list(calendar),
        market_calendar_id=calendar_id,
        account_equity_evidence_id=equity_evidence,
        manual_ceiling_authorization_id=ceiling_auth,
        two_layer_decision_contract_id=contract_id,
        two_layer_decision_contract_path=contract_path,
        layer_one_index_protocol_id=protocol_id,
        layer_one_index_protocol_path=protocol_path,
        layer_one_index_data_evidence_id=data_evidence_id,
        layer_one_index_data_evidence_path=data_evidence_path,
        prior_state_id=prior_state.state_id,
        prior_risk_lock_triggered_as_of=prior_state.risk_lock_triggered_as_of,
        prior_red_line_breached=prior_state.red_line_breached,
        account_peak_equity=float(account_peak_equity),
        account_current_equity=float(account_current_equity),
        account_drawdown=account_dd,
        close_to_sma_ratio=index_risk_report.close_to_sma_ratio,
        realized_volatility_annualized=index_risk_report.realized_volatility_annualized,
        index_drawdown=index_risk_report.drawdown,
        trend_regime=trend_regime,
        trend_base_budget=trend_base,
        volatility_cap=vol_cap,
        index_drawdown_cap=index_dd_cap,
        account_drawdown_cap=account_dd_cap,
        manual_open_ceiling=ceiling,
        raw_target_budget=raw_target,
        previous_applied_stock_budget=previous_budget,
        applied_stock_budget=new_state.applied_stock_budget,
        increase_deferred=increase_deferred,
        increase_deferred_reason=deferred_reason,
        target_day_is_first_market_trading_day_of_week=is_first_of_week,
        risk_lock_prior_active=prior_locked,
        risk_lock_new_active=risk_lock_active,
        risk_lock_triggered_this_decision=risk_lock_triggered_this,
        risk_lock_unlocked_this_decision=risk_lock_unlocked_this,
        risk_lock_triggered_as_of=lock_triggered_as_of,
        risk_lock_cooling_trading_days=cooling_days,
        unlock_request_id=unlock_request_id,
        unlock_operator=unlock_operator,
        unlock_reason=unlock_reason,
        unlock_requested_at=unlock_requested_at,
        unlock_user_confirmed=unlock_user_confirmed,
        unlock_request_evidence_id=unlock_request_evidence_id,
        unlock_rejection_reasons=list(unlock_rejections),
        red_line_breached=red_line,
        new_state=new_state,
    )
    return seal_layer_one_regime_decision(report)


def canonical_state_payload(state: LayerOneRegimeNewState | LayerOneRegimePriorState) -> dict[str, Any]:
    return state.model_dump(mode="json", exclude={"state_id"})


def compute_state_id(state: LayerOneRegimeNewState | LayerOneRegimePriorState) -> str:
    payload = json.dumps(
        canonical_state_payload(state),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def seal_layer_one_regime_state(state: LayerOneRegimeNewState) -> LayerOneRegimeNewState:
    return state.model_copy(update={"state_id": compute_state_id(state)})


def seal_prior_state(state: LayerOneRegimePriorState) -> LayerOneRegimePriorState:
    return state.model_copy(update={"state_id": compute_state_id(state)})


def canonical_unlock_request_payload(request: LayerOneUnlockRequest) -> dict[str, Any]:
    return request.model_dump(mode="json")


def compute_unlock_request_evidence_id(request: LayerOneUnlockRequest) -> str:
    payload = json.dumps(
        canonical_unlock_request_payload(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_decision_payload(report: LayerOneRegimeDecisionReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"decision_id"})


def canonical_decision_bytes(report: LayerOneRegimeDecisionReport) -> bytes:
    return json.dumps(
        canonical_decision_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_decision_id(report: LayerOneRegimeDecisionReport) -> str:
    return hashlib.sha256(canonical_decision_bytes(report)).hexdigest()


def seal_layer_one_regime_decision(report: LayerOneRegimeDecisionReport) -> LayerOneRegimeDecisionReport:
    return report.model_copy(update={"decision_id": compute_decision_id(report)})


def assert_decision_self_hash(report: LayerOneRegimeDecisionReport) -> None:
    if report.decision_id is None:
        raise ValueError("layer-one regime decision_id is missing")
    expected = compute_decision_id(report)
    if report.decision_id != expected:
        raise ValueError("layer-one regime decision_id does not match canonical content hash")


def _budgets_close(left: float, right: float) -> bool:
    return abs(left - right) <= _BUDGET_ABS_TOL


def assert_decision_logic_consistent(report: LayerOneRegimeDecisionReport) -> None:
    """Recompute derived fields from sealed inputs; reject logic tamper + reseal."""
    calendar = assert_market_calendar(report.market_calendar)
    if compute_market_calendar_id(calendar) != report.market_calendar_id:
        raise ValueError("market_calendar_id does not match canonical market_calendar hash")

    as_of, is_first, _ = resolve_decision_timing(
        target_trading_day=report.target_trading_day,
        market_calendar=calendar,
    )
    if as_of != report.as_of:
        raise ValueError("as_of does not match recomputed adjacent prior market day")
    if is_first != report.target_day_is_first_market_trading_day_of_week:
        raise ValueError("target_day_is_first_market_trading_day_of_week does not recompute")

    feature_report = report.index_risk_feature_report
    assert_index_risk_report_for_regime(feature_report, as_of=as_of, market_calendar=calendar)
    if feature_report.report_id is None:
        raise ValueError("embedded index risk feature report_id is missing")
    if feature_report.report_id != report.index_risk_feature_report_id:
        raise ValueError("index_risk_feature_report_id does not match embedded feature report_id")
    if feature_report.data_snapshot_id != report.data_snapshot_id:
        raise ValueError("data_snapshot_id does not match embedded feature report")
    if feature_report.index_symbol != report.index_symbol_input:
        raise ValueError("index_symbol_input does not match embedded feature report")
    if feature_report.close_to_sma_ratio != report.close_to_sma_ratio:
        raise ValueError("close_to_sma_ratio does not match embedded feature report")
    if feature_report.realized_volatility_annualized != report.realized_volatility_annualized:
        raise ValueError("realized_volatility_annualized does not match embedded feature report")
    if feature_report.drawdown != report.index_drawdown:
        raise ValueError("index_drawdown does not match embedded feature report")

    account_dd = compute_account_drawdown_decimal(
        peak_equity=report.account_peak_equity,
        current_equity=report.account_current_equity,
    )
    # Evaluate stores float(Decimal(...)); accept that encoding or exact Decimal(str(stored)).
    if float(account_dd) != report.account_drawdown and Decimal(str(report.account_drawdown)) != account_dd:
        raise ValueError("account_drawdown does not match Decimal(str(current))/Decimal(str(peak))-1")

    trend_regime, trend_base = map_trend_regime(feature_report.close_to_sma_ratio)
    vol_cap = map_volatility_cap(feature_report.realized_volatility_annualized)
    index_dd_cap = map_index_drawdown_cap(feature_report.drawdown)
    account_dd_cap, trigger_lock_now, red_line_now = map_account_drawdown_cap(account_dd)
    if trend_regime != report.trend_regime:
        raise ValueError("trend_regime does not recompute")
    if not _budgets_close(trend_base, report.trend_base_budget):
        raise ValueError("trend_base_budget does not recompute")
    if not _budgets_close(vol_cap, report.volatility_cap):
        raise ValueError("volatility_cap does not recompute")
    if not _budgets_close(index_dd_cap, report.index_drawdown_cap):
        raise ValueError("index_drawdown_cap does not recompute")
    if not _budgets_close(account_dd_cap, report.account_drawdown_cap):
        raise ValueError("account_drawdown_cap does not recompute")

    raw_target = min(trend_base, vol_cap, index_dd_cap, account_dd_cap, report.manual_open_ceiling)
    raw_target = _require_budget_level(raw_target, field_name="raw_target_budget")
    if not _budgets_close(raw_target, report.raw_target_budget):
        raise ValueError("raw_target_budget does not recompute from min(caps, ceiling)")

    prior = LayerOneRegimePriorState(
        applied_stock_budget=report.previous_applied_stock_budget,
        risk_lock_active=report.risk_lock_prior_active,
        risk_lock_triggered_as_of=report.prior_risk_lock_triggered_as_of,
        red_line_breached=report.prior_red_line_breached,
        state_id=report.prior_state_id,
    )
    assert_prior_state_sealed(prior)

    cooling_days = 0
    lock_triggered_as_of = report.prior_risk_lock_triggered_as_of
    if report.risk_lock_prior_active:
        if lock_triggered_as_of is None:
            raise ValueError("prior risk lock missing prior_risk_lock_triggered_as_of")
        if lock_triggered_as_of not in calendar:
            raise ValueError("prior_risk_lock_triggered_as_of must appear in market_calendar")
        cooling_days = _count_cooling_days(
            calendar=calendar,
            lock_triggered_as_of=lock_triggered_as_of,
            as_of=as_of,
        )

    unlock_request: LayerOneUnlockRequest | None = None
    if report.unlock_request_id is not None:
        if (
            report.unlock_operator is None
            or report.unlock_reason is None
            or report.unlock_requested_at is None
            or report.unlock_user_confirmed is None
            or report.unlock_request_evidence_id is None
        ):
            raise ValueError("unlock audit fields incomplete")
        unlock_request = LayerOneUnlockRequest(
            request_id=report.unlock_request_id,
            operator=report.unlock_operator,
            reason=report.unlock_reason,
            requested_at=report.unlock_requested_at,
            user_confirmed=report.unlock_user_confirmed,
        )
        expected_unlock_evidence = compute_unlock_request_evidence_id(unlock_request)
        if report.unlock_request_evidence_id != expected_unlock_evidence:
            raise ValueError("unlock_request_evidence_id does not match sealed unlock request")

    unlocked, unlock_rejections = evaluate_unlock_request(
        prior_locked=report.risk_lock_prior_active,
        lock_triggered_as_of=lock_triggered_as_of,
        cooling_trading_days=cooling_days,
        trend_regime=trend_regime,
        realized_volatility_annualized=feature_report.realized_volatility_annualized,
        unlock_request=unlock_request,
        target_trading_day=report.target_trading_day,
        evaluated_at=report.evaluated_at,
        current_observation_triggers_lock=trigger_lock_now,
    )
    if unlock_rejections != list(report.unlock_rejection_reasons):
        raise ValueError("unlock_rejection_reasons do not recompute")

    risk_lock_active = report.risk_lock_prior_active
    risk_lock_triggered_this = False
    risk_lock_unlocked_this = False
    new_lock_as_of = lock_triggered_as_of
    new_cooling = cooling_days

    if trigger_lock_now:
        risk_lock_active = True
        if not report.risk_lock_prior_active:
            risk_lock_triggered_this = True
            new_lock_as_of = as_of
            new_cooling = 0
    elif report.risk_lock_prior_active and unlocked:
        risk_lock_active = False
        risk_lock_unlocked_this = True
        new_lock_as_of = None
        new_cooling = 0
    elif report.risk_lock_prior_active:
        risk_lock_active = True

    if risk_lock_active != report.risk_lock_new_active:
        raise ValueError("risk_lock_new_active does not recompute")
    if risk_lock_triggered_this != report.risk_lock_triggered_this_decision:
        raise ValueError("risk_lock_triggered_this_decision does not recompute")
    if risk_lock_unlocked_this != report.risk_lock_unlocked_this_decision:
        raise ValueError("risk_lock_unlocked_this_decision does not recompute")
    if new_lock_as_of != report.risk_lock_triggered_as_of:
        raise ValueError("risk_lock_triggered_as_of does not recompute")
    if new_cooling != report.risk_lock_cooling_trading_days:
        raise ValueError("risk_lock_cooling_trading_days does not recompute")

    raw_for_apply = 0.0 if risk_lock_active else raw_target
    applied, increase_deferred, deferred_reason = apply_weekly_budget_adjustment(
        raw_target_budget=raw_for_apply,
        previous_applied_stock_budget=report.previous_applied_stock_budget,
        target_day_is_first_market_trading_day_of_week=is_first,
        risk_lock_active=risk_lock_active,
    )
    expected_applied = 0.0 if risk_lock_active else applied
    if not _budgets_close(expected_applied, report.applied_stock_budget):
        raise ValueError("applied_stock_budget does not recompute")
    if increase_deferred != report.increase_deferred:
        raise ValueError("increase_deferred does not recompute")
    if deferred_reason != report.increase_deferred_reason:
        raise ValueError("increase_deferred_reason does not recompute")

    red_line = bool(report.prior_red_line_breached or red_line_now)
    if red_line != report.red_line_breached:
        raise ValueError("red_line_breached does not recompute")

    expected_state = seal_layer_one_regime_state(
        LayerOneRegimeNewState(
            applied_stock_budget=expected_applied,
            risk_lock_active=risk_lock_active,
            risk_lock_triggered_as_of=new_lock_as_of,
            red_line_breached=red_line,
        )
    )
    assert_new_state_sealed(report.new_state)
    if report.new_state.model_dump(mode="json") != expected_state.model_dump(mode="json"):
        raise ValueError("new_state does not match recomputed sealed state")


def load_layer_one_regime_decision(path: Path) -> LayerOneRegimeDecisionReport:
    try:
        return LayerOneRegimeDecisionReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("layer-one regime decision report is missing or invalid") from exc


def verify_layer_one_regime_decision_file(
    path: Path,
    *,
    repo_root: Path,
) -> LayerOneRegimeDecisionReport:
    """Verify sealed local decision JSON + upstream disk bindings; no market data.

    Requires the embedded sealed IndexRiskFeatureReport to self-hash and bind to
    top-level snapshot/symbol/feature scalars; caps are recomputed from that report.
    """
    report = load_layer_one_regime_decision(path)
    assert_decision_self_hash(report)
    if report.ready_for_orders or report.ready_for_trading or report.does_not_trade is not True:
        raise ValueError("layer-one regime decision cannot authorize orders or trading")
    if report.exact_symbol_identity_verified is not True:
        raise ValueError("exact_symbol_identity_verified must remain true")
    contract_id, contract_path, protocol_id, protocol_path = bind_upstream_contracts(repo_root=repo_root)
    evidence_id, evidence_path, snapshot_id, risk_symbol = bind_index_data_evidence(repo_root=repo_root)
    if report.two_layer_decision_contract_id != contract_id:
        raise ValueError("decision two_layer_decision_contract_id does not match disk binding")
    if report.layer_one_index_protocol_id != protocol_id:
        raise ValueError("decision layer_one_index_protocol_id does not match disk binding")
    if report.two_layer_decision_contract_path != contract_path:
        raise ValueError("decision two_layer_decision_contract_path does not match disk binding")
    if report.layer_one_index_protocol_path != protocol_path:
        raise ValueError("decision layer_one_index_protocol_path does not match disk binding")
    if report.layer_one_index_data_evidence_id != evidence_id:
        raise ValueError("decision layer_one_index_data_evidence_id does not match disk binding")
    if report.layer_one_index_data_evidence_path != evidence_path:
        raise ValueError("decision layer_one_index_data_evidence_path does not match disk binding")
    if report.data_snapshot_id != snapshot_id:
        raise ValueError("decision data_snapshot_id does not match verified CSI snapshot")
    if report.index_symbol_input != risk_symbol:
        raise ValueError("decision index_symbol_input does not match verified CSI identity")
    assert_decision_logic_consistent(report)
    return report


def write_layer_one_regime_decision(report: LayerOneRegimeDecisionReport, output: Path) -> None:
    sealed = seal_layer_one_regime_decision(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "ALLOWED_BUDGET_LEVELS",
    "BOUND_LAYER_ONE_INDEX_PROTOCOL_ID",
    "BOUND_LAYER_ONE_INDEX_PROTOCOL_PATH",
    "BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_ID",
    "BOUND_LAYER_ONE_INDEX_DATA_EVIDENCE_PATH",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_ID",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_PATH",
    "LAYER_ONE_REGIME_ENGINE_VERSION",
    "LAYER_ONE_REGIME_SCHEMA_VERSION",
    "LayerOneRegimeDecisionReport",
    "LayerOneRegimeNewState",
    "LayerOneRegimePriorState",
    "LayerOneUnlockRequest",
    "apply_weekly_budget_adjustment",
    "assert_account_equity",
    "assert_decision_logic_consistent",
    "assert_decision_self_hash",
    "assert_feature_windows_on_calendar",
    "assert_index_risk_report_for_regime",
    "bind_index_data_evidence",
    "assert_market_calendar",
    "assert_new_state_sealed",
    "assert_prior_state_sealed",
    "bind_upstream_contracts",
    "compute_account_drawdown_decimal",
    "compute_decision_id",
    "compute_market_calendar_id",
    "compute_state_id",
    "compute_unlock_request_evidence_id",
    "evaluate_layer_one_regime",
    "evaluate_unlock_request",
    "load_layer_one_regime_decision",
    "map_account_drawdown_cap",
    "map_index_drawdown_cap",
    "map_trend_regime",
    "map_volatility_cap",
    "resolve_decision_timing",
    "seal_layer_one_regime_decision",
    "seal_layer_one_regime_state",
    "seal_prior_state",
    "verify_layer_one_regime_decision_file",
    "write_layer_one_regime_decision",
]
