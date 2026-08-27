"""Layer-two PIT candidate eligibility (E10a).

Pure deterministic, read-only research module: consumes explicit as-of inputs
and emits a sealed per-symbol eligibility/capacity report. Never scores, backtests,
trades, or wires into StrategyConfig / portfolio construction.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.two_layer_contract import (
    DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH,
    LiquidityPolicyConfirmed,
    SmallCapPolicyConfirmed,
    TwoLayerStrategyDecisionContractV2,
    UniversePolicyConfirmed,
    load_two_layer_decision_draft,
    verify_two_layer_decision_draft,
)

LAYER_TWO_CANDIDATE_ELIGIBILITY_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_CANDIDATE_ELIGIBILITY_ENGINE_VERSION: Literal["layer-two-candidate-eligibility-engine-v1"] = (
    "layer-two-candidate-eligibility-engine-v1"
)

BOUND_TWO_LAYER_DECISION_CONTRACT_PATH: Literal["config/research/two-layer-strategy-decision-draft-v1.json"] = (
    "config/research/two-layer-strategy-decision-draft-v1.json"
)
BOUND_TWO_LAYER_DECISION_CONTRACT_ID = "27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6"

MarketCode = Literal["SSE", "SZSE"]
TradabilityStatus = Literal["tradable", "known_full_day_suspension"]
OwnershipRole = Literal["diagnostic_not_used"]

_HEX64 = r"^[0-9a-f]{64}$"
_CANONICAL_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(SH|SZ)$")

FAILURE_REASON_ORDER: tuple[str, ...] = (
    "unknown_critical_input",
    "market_scope_fail",
    "bse_forbidden",
    "st_or_delist_risk_fail",
    "suspended_on_decision_date_fail",
    "listing_history_fail",
    "liquidity_observation_structure_fail",
    "liquidity_tradable_days_fail",
    "liquidity_median_amount_fail",
    "liquidity_capacity_fail",
    "size_cap_hard_exclude_fail",
)
ELIGIBLE_REASON_CODE: Literal["eligible_for_new_entry"] = "eligible_for_new_entry"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _reject_blank_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_aware_datetime(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _finite_non_negative(value: float, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    if number < 0.0:
        raise ValueError(f"{field_name} must be non-negative")
    return number


def _finite_positive(value: float, *, field_name: str) -> float:
    number = _finite_non_negative(value, field_name=field_name)
    if number <= 0.0:
        raise ValueError(f"{field_name} must be strictly positive")
    return number


def _require_exact_date(value: object, *, field_name: str) -> date:
    if type(value) is not date:
        raise ValueError(f"{field_name} must be a date")
    return value


def _decision_calendar_date(decision_at: datetime) -> date:
    decision_at = _require_aware_datetime(decision_at, field_name="decision_at")
    return decision_at.date()


def _assert_provenance_as_of(*, provenance_as_of: date, report_as_of: date, field_name: str) -> None:
    if provenance_as_of != report_as_of:
        raise ValueError(f"{field_name} must equal report as_of")


def _assert_provenance_available_at(
    *,
    available_at: datetime,
    decision_at: datetime,
    field_name: str,
) -> None:
    _require_aware_datetime(available_at, field_name=field_name)
    if available_at > decision_at:
        raise ValueError(f"{field_name} must be on or before decision_at")


def _assert_metadata_pair_complete(
    *,
    left: object | None,
    right: object | None,
    pair_name: str,
) -> None:
    if (left is None) ^ (right is None):
        raise ValueError(f"{pair_name} must be supplied together or both null")


def _assert_metadata_triple_complete(
    *,
    a: object | None,
    b: object | None,
    c: object | None,
    group_name: str,
) -> None:
    present = [value is not None for value in (a, b, c)]
    if any(present) and not all(present):
        raise ValueError(f"{group_name} must be supplied together or all null")


def _validate_canonical_symbol(symbol: str) -> None:
    if symbol != symbol.strip():
        raise ValueError("symbol must not contain leading or trailing whitespace")
    if _CANONICAL_SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise ValueError("symbol must be exactly six digits plus uppercase .SH or .SZ")


def _validate_symbol_market_consistency(*, symbol: str, market: MarketCode | None) -> None:
    _validate_canonical_symbol(symbol)
    if market is None:
        return
    if market == "SSE" and not symbol.endswith(".SH"):
        raise ValueError("SSE market requires .SH symbol suffix")
    if market == "SZSE" and not symbol.endswith(".SZ"):
        raise ValueError("SZSE market requires .SZ symbol suffix")


@dataclass(frozen=True)
class _LiquidityValidationResult:
    structure_ok: bool
    has_unknown: bool
    amounts: list[float] | None
    tradable_count: int | None


class LayerTwoLiquidityObservation(_StrictModel):
    observation_date: date
    tradability: TradabilityStatus | None = None
    amount_cny: float | None = None
    available_at: datetime

    @field_validator("available_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, field_name="available_at")


class LayerTwoCandidateInput(_StrictModel):
    symbol: str = Field(min_length=1)
    market: MarketCode | None = None
    is_ordinary_a_share: bool | None = None
    is_bse: bool | None = None
    is_st_or_delist_risk: bool | None = None
    is_suspended_on_decision_date: bool | None = None
    listed_market_trading_days: int | None = Field(default=None, ge=0)
    security_status_as_of: date | None = None
    security_status_available_at: datetime | None = None
    planned_buy_notional_cny: float
    liquidity_observations: list[LayerTwoLiquidityObservation]
    pit_free_float_market_cap_cny: float | None = None
    pit_free_float_market_cap_as_of: date | None = None
    pit_free_float_market_cap_available_at: datetime | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _symbol(cls, value: object) -> object:
        return _reject_blank_string(value, field_name="symbol")

    @field_validator("planned_buy_notional_cny")
    @classmethod
    def _planned_buy(cls, value: float) -> float:
        return _finite_positive(value, field_name="planned_buy_notional_cny")

    @field_validator("security_status_available_at", "pit_free_float_market_cap_available_at")
    @classmethod
    def _aware_available_at(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _require_aware_datetime(value, field_name=str(info.field_name))


class LayerTwoCandidateEvaluation(_StrictModel):
    symbol: str = Field(min_length=1)
    reason_codes: list[str]
    eligible_for_new_entry: bool
    unknown_critical_input: bool
    market_scope_pass: bool | None = None
    tradability_pass: bool | None = None
    listing_history_pass: bool | None = None
    st_delist_pass: bool | None = None
    liquidity_structure_pass: bool | None = None
    liquidity_tradable_count_pass: bool | None = None
    liquidity_median_pass: bool | None = None
    liquidity_capacity_pass: bool | None = None
    size_cap_pass: bool | None = None
    median_daily_amount_cny: float | None = None
    average_daily_amount_cny: float | None = None
    tradable_days_in_lookback: int | None = Field(default=None, ge=0)
    size_multiplier: float | None = None
    adjusted_planned_notional_cny: float | None = None
    ownership_role: OwnershipRole = "diagnostic_not_used"
    candidate_input: LayerTwoCandidateInput


class LayerTwoCandidateEligibilityReport(_StrictModel):
    schema_version: Literal["1"] = LAYER_TWO_CANDIDATE_ELIGIBILITY_SCHEMA_VERSION
    engine_version: Literal["layer-two-candidate-eligibility-engine-v1"] = (
        LAYER_TWO_CANDIDATE_ELIGIBILITY_ENGINE_VERSION
    )
    report_id: str | None = Field(default=None, pattern=_HEX64)
    as_of: date
    decision_at: datetime
    data_snapshot_id: str = Field(min_length=1)
    two_layer_decision_contract_id: str = Field(pattern=_HEX64)
    two_layer_decision_contract_path: str = Field(min_length=1)
    requested_symbols: list[str]
    candidate_inputs: list[LayerTwoCandidateInput]
    evaluations: list[LayerTwoCandidateEvaluation]
    ownership_role: OwnershipRole = "diagnostic_not_used"
    research_only: Literal[True] = True
    implementation_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    does_not_trade: Literal[True] = True

    @field_validator("data_snapshot_id", mode="before")
    @classmethod
    def _snapshot(cls, value: object) -> object:
        return _reject_blank_string(value, field_name="data_snapshot_id")

    @field_validator("decision_at")
    @classmethod
    def _decision_at(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, field_name="decision_at")

    @model_validator(mode="after")
    def _gate_flags(self) -> LayerTwoCandidateEligibilityReport:
        if self.research_only is not True or self.implementation_only is not True:
            raise ValueError("research_only and implementation_only must remain true")
        if (
            self.ready_for_scoring
            or self.ready_for_portfolio_construction
            or self.ready_for_orders
            or self.ready_for_trading
            or self.does_not_trade is not True
        ):
            raise ValueError("report cannot authorize scoring, portfolio construction, orders, or trading")
        if self.ownership_role != "diagnostic_not_used":
            raise ValueError("ownership_role must remain diagnostic_not_used")
        input_symbols = [item.symbol for item in self.candidate_inputs]
        eval_symbols = [item.symbol for item in self.evaluations]
        if input_symbols != sorted(input_symbols):
            raise ValueError("candidate_inputs must be sorted by symbol")
        if eval_symbols != sorted(eval_symbols):
            raise ValueError("evaluations must be sorted by symbol")
        if self.requested_symbols != input_symbols:
            raise ValueError("requested_symbols must equal candidate_inputs symbols in sorted order")
        if len(self.candidate_inputs) != len(self.evaluations):
            raise ValueError("candidate_inputs and evaluations length mismatch")
        if len(set(input_symbols)) != len(input_symbols):
            raise ValueError("candidate_inputs symbols must be unique")
        for candidate, evaluation in zip(self.candidate_inputs, self.evaluations, strict=True):
            if candidate.symbol != evaluation.symbol:
                raise ValueError("evaluation symbol must match paired candidate_input symbol")
            if evaluation.candidate_input.symbol != evaluation.symbol:
                raise ValueError("evaluation.candidate_input.symbol must match evaluation.symbol")
        return self


class LayerTwoEligibilityPolicy(_StrictModel):
    universe: UniversePolicyConfirmed
    liquidity: LiquidityPolicyConfirmed
    small_cap: SmallCapPolicyConfirmed


def bind_two_layer_eligibility_policy(
    *,
    repo_root: Path,
    contract_path: Path | None = None,
) -> tuple[str, str, LayerTwoEligibilityPolicy]:
    root = Path(repo_root).resolve()
    resolved_path = Path(contract_path) if contract_path is not None else root / BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
    if not resolved_path.is_file():
        raise ValueError(f"two-layer decision contract missing: {resolved_path}")
    draft = load_two_layer_decision_draft(resolved_path)
    if not isinstance(draft, TwoLayerStrategyDecisionContractV2):
        raise ValueError("layer-two candidate eligibility requires schema-v2 two-layer contract")
    result = verify_two_layer_decision_draft(draft)
    if result.contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
        raise ValueError("two-layer decision contract_id drifted from E10a bound constant")
    if str(DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH) != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two-layer decision default path drifted from E10a binding")
    rel_path = _repo_relative_posix(resolved_path, repo_root=root)
    if rel_path != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two-layer decision contract path must match bound relative path")
    policy = LayerTwoEligibilityPolicy(
        universe=draft.layer_two.universe,
        liquidity=draft.layer_two.liquidity,
        small_cap=draft.layer_two.small_cap,
    )
    return result.contract_id, rel_path, policy


def _repo_relative_posix(path: Path, *, repo_root: Path) -> str:
    resolved = Path(path).resolve()
    root = Path(repo_root).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("contract path must be inside repo_root") from exc


def assert_candidate_input_integrity(
    candidate: LayerTwoCandidateInput,
    *,
    as_of: date,
    decision_at: datetime,
) -> None:
    """Reject corrupt/half-pair inputs before eligibility evaluation."""
    as_of = _require_exact_date(as_of, field_name="as_of")
    decision_at = _require_aware_datetime(decision_at, field_name="decision_at")
    if _decision_calendar_date(decision_at) != as_of:
        raise ValueError("decision_at calendar date must equal as_of")

    _validate_symbol_market_consistency(symbol=candidate.symbol, market=candidate.market)

    _assert_metadata_pair_complete(
        left=candidate.security_status_as_of,
        right=candidate.security_status_available_at,
        pair_name="security_status_as_of and security_status_available_at",
    )
    _assert_metadata_triple_complete(
        a=candidate.pit_free_float_market_cap_cny,
        b=candidate.pit_free_float_market_cap_as_of,
        c=candidate.pit_free_float_market_cap_available_at,
        group_name="pit_free_float_market_cap fields",
    )

    if candidate.security_status_as_of is not None and candidate.security_status_available_at is not None:
        _assert_provenance_as_of(
            provenance_as_of=candidate.security_status_as_of,
            report_as_of=as_of,
            field_name="security_status_as_of",
        )
        _assert_provenance_available_at(
            available_at=candidate.security_status_available_at,
            decision_at=decision_at,
            field_name="security_status_available_at",
        )

    if (
        candidate.pit_free_float_market_cap_cny is not None
        and candidate.pit_free_float_market_cap_as_of is not None
        and candidate.pit_free_float_market_cap_available_at is not None
    ):
        _assert_provenance_as_of(
            provenance_as_of=candidate.pit_free_float_market_cap_as_of,
            report_as_of=as_of,
            field_name="pit_free_float_market_cap_as_of",
        )
        _assert_provenance_available_at(
            available_at=candidate.pit_free_float_market_cap_available_at,
            decision_at=decision_at,
            field_name="pit_free_float_market_cap_available_at",
        )
        _finite_non_negative(
            candidate.pit_free_float_market_cap_cny,
            field_name="pit_free_float_market_cap_cny",
        )

    _validate_liquidity_observations(
        candidate.liquidity_observations,
        as_of=as_of,
        decision_at=decision_at,
        lookback_days=20,
        allow_unknown=True,
    )


def _validate_liquidity_observations(
    observations: Sequence[LayerTwoLiquidityObservation],
    *,
    as_of: date,
    decision_at: datetime,
    lookback_days: int,
    allow_unknown: bool,
) -> _LiquidityValidationResult:
    """Validate all slots before returning unknown/structure outcomes."""
    if len(observations) > lookback_days:
        raise ValueError(
            f"liquidity_observations count {len(observations)} exceeds lookback_market_trading_days {lookback_days}"
        )
    if len(observations) < lookback_days:
        return _LiquidityValidationResult(
            structure_ok=False,
            has_unknown=True,
            amounts=None,
            tradable_count=None,
        )

    dates: list[date] = []
    amounts: list[float] = []
    tradable_count = 0
    has_unknown = False

    for slot in observations:
        _assert_provenance_available_at(
            available_at=slot.available_at,
            decision_at=decision_at,
            field_name="liquidity_observations.available_at",
        )
        obs_date = _require_exact_date(slot.observation_date, field_name="observation_date")
        if obs_date > as_of:
            raise ValueError("liquidity observation_date cannot be after as_of")
        if dates and obs_date <= dates[-1]:
            raise ValueError("liquidity observation dates must be unique and strictly increasing")
        dates.append(obs_date)

        if slot.amount_cny is not None:
            _finite_non_negative(slot.amount_cny, field_name="amount_cny")

        if slot.tradability is None or slot.amount_cny is None:
            has_unknown = True
            amounts.append(0.0)
            continue

        if slot.tradability == "known_full_day_suspension":
            if slot.amount_cny != 0.0:
                raise ValueError("known_full_day_suspension requires amount_cny=0")
            amount = 0.0
        else:
            amount = float(slot.amount_cny)
            tradable_count += 1
        amounts.append(amount)

    if dates[-1] != as_of:
        raise ValueError("liquidity window final observation_date must equal as_of")

    if has_unknown:
        if not allow_unknown:
            raise ValueError("liquidity observations contain unknown tradability or amount")
        return _LiquidityValidationResult(
            structure_ok=True,
            has_unknown=True,
            amounts=None,
            tradable_count=None,
        )

    return _LiquidityValidationResult(
        structure_ok=True,
        has_unknown=False,
        amounts=amounts,
        tradable_count=tradable_count,
    )


def _map_size_multiplier(
    cap_cny: float,
    *,
    policy: SmallCapPolicyConfirmed,
) -> float | None:
    cap = _finite_non_negative(cap_cny, field_name="pit_free_float_market_cap_cny")
    if cap < float(policy.exclude_below_cny):
        return None
    for band in policy.size_multipliers:
        if cap >= band.min_inclusive:
            if band.max_exclusive is None or cap < band.max_exclusive:
                return band.multiplier
    raise ValueError("pit free-float market cap does not match any confirmed size band")


def _ordered_failure_reasons(reasons: set[str]) -> list[str]:
    ordered = [code for code in FAILURE_REASON_ORDER if code in reasons]
    extras = sorted(reason for reason in reasons if reason not in FAILURE_REASON_ORDER)
    return ordered + extras


def evaluate_layer_two_candidate(
    candidate: LayerTwoCandidateInput,
    *,
    as_of: date,
    decision_at: datetime,
    policy: LayerTwoEligibilityPolicy,
) -> LayerTwoCandidateEvaluation:
    as_of = _require_exact_date(as_of, field_name="as_of")
    decision_at = _require_aware_datetime(decision_at, field_name="decision_at")
    assert_candidate_input_integrity(candidate, as_of=as_of, decision_at=decision_at)

    failures: set[str] = set()
    unknown_critical = False

    def mark_unknown() -> None:
        nonlocal unknown_critical
        unknown_critical = True
        failures.add("unknown_critical_input")

    market_scope_pass: bool | None = None
    tradability_pass: bool | None = None
    listing_history_pass: bool | None = None
    st_delist_pass: bool | None = None
    liquidity_structure_pass: bool | None = None
    liquidity_tradable_count_pass: bool | None = None
    liquidity_median_pass: bool | None = None
    liquidity_capacity_pass: bool | None = None
    size_cap_pass: bool | None = None

    median_amount: float | None = None
    average_amount: float | None = None
    tradable_days: int | None = None
    size_multiplier: float | None = None
    adjusted_planned: float | None = None

    security_metadata_present = (
        candidate.security_status_as_of is not None and candidate.security_status_available_at is not None
    )
    if not security_metadata_present:
        mark_unknown()

    if security_metadata_present:
        if candidate.market is None or candidate.is_ordinary_a_share is None:
            mark_unknown()
        else:
            market_ok = candidate.market in policy.universe.markets and candidate.is_ordinary_a_share is True
            market_scope_pass = market_ok
            if not market_ok:
                failures.add("market_scope_fail")

        if candidate.is_bse is None:
            mark_unknown()
        elif candidate.is_bse is True:
            failures.add("bse_forbidden")

        if candidate.is_st_or_delist_risk is None:
            mark_unknown()
        else:
            st_delist_pass = candidate.is_st_or_delist_risk is False
            if not st_delist_pass:
                failures.add("st_or_delist_risk_fail")

        if candidate.is_suspended_on_decision_date is None:
            mark_unknown()
        else:
            tradability_pass = candidate.is_suspended_on_decision_date is False
            if not tradability_pass:
                failures.add("suspended_on_decision_date_fail")

        if candidate.listed_market_trading_days is None:
            mark_unknown()
        else:
            listing_history_pass = (
                candidate.listed_market_trading_days >= policy.universe.min_listed_market_trading_days
            )
            if not listing_history_pass:
                failures.add("listing_history_fail")

    liquidity_result = _validate_liquidity_observations(
        candidate.liquidity_observations,
        as_of=as_of,
        decision_at=decision_at,
        lookback_days=policy.liquidity.lookback_market_trading_days,
        allow_unknown=True,
    )
    if not liquidity_result.structure_ok:
        mark_unknown()
        liquidity_structure_pass = False
        failures.add("liquidity_observation_structure_fail")
    elif liquidity_result.has_unknown:
        mark_unknown()
        liquidity_structure_pass = None
    else:
        liquidity_structure_pass = True

    if (
        liquidity_result.structure_ok
        and not liquidity_result.has_unknown
        and liquidity_result.amounts is not None
        and liquidity_result.tradable_count is not None
    ):
        tradable_days = liquidity_result.tradable_count
        liquidity_tradable_count_pass = tradable_days >= policy.liquidity.min_tradable_days_in_lookback
        if not liquidity_tradable_count_pass:
            failures.add("liquidity_tradable_days_fail")

        median_amount = float(statistics.median(liquidity_result.amounts))
        average_amount = float(sum(liquidity_result.amounts) / len(liquidity_result.amounts))
        liquidity_median_pass = median_amount >= float(policy.liquidity.median_daily_amount_min_cny)
        if not liquidity_median_pass:
            failures.add("liquidity_median_amount_fail")

        capacity_limit = average_amount * policy.liquidity.max_planned_buy_vs_20d_avg_amount
        liquidity_capacity_pass = candidate.planned_buy_notional_cny <= capacity_limit
        if not liquidity_capacity_pass:
            failures.add("liquidity_capacity_fail")

    cap_complete = (
        candidate.pit_free_float_market_cap_cny is not None
        and candidate.pit_free_float_market_cap_as_of is not None
        and candidate.pit_free_float_market_cap_available_at is not None
    )
    if not cap_complete:
        mark_unknown()
    else:
        cap_value = candidate.pit_free_float_market_cap_cny
        assert cap_value is not None
        mapped_multiplier = _map_size_multiplier(cap_value, policy=policy.small_cap)
        if mapped_multiplier is None:
            size_cap_pass = False
            failures.add("size_cap_hard_exclude_fail")
        else:
            size_cap_pass = True
            size_multiplier = mapped_multiplier
            adjusted_planned = candidate.planned_buy_notional_cny * size_multiplier

    eligible = len(failures) == 0 and not unknown_critical
    reason_codes: list[str]
    if eligible:
        reason_codes = [ELIGIBLE_REASON_CODE]
    else:
        reason_codes = _ordered_failure_reasons(failures)

    return LayerTwoCandidateEvaluation(
        symbol=candidate.symbol,
        reason_codes=reason_codes,
        eligible_for_new_entry=eligible,
        unknown_critical_input=unknown_critical,
        market_scope_pass=market_scope_pass,
        tradability_pass=tradability_pass,
        listing_history_pass=listing_history_pass,
        st_delist_pass=st_delist_pass,
        liquidity_structure_pass=liquidity_structure_pass,
        liquidity_tradable_count_pass=liquidity_tradable_count_pass,
        liquidity_median_pass=liquidity_median_pass,
        liquidity_capacity_pass=liquidity_capacity_pass,
        size_cap_pass=size_cap_pass,
        median_daily_amount_cny=median_amount,
        average_daily_amount_cny=average_amount,
        tradable_days_in_lookback=tradable_days,
        size_multiplier=size_multiplier,
        adjusted_planned_notional_cny=adjusted_planned,
        candidate_input=candidate,
    )


def evaluate_layer_two_candidate_eligibility(
    *,
    as_of: date,
    decision_at: datetime,
    data_snapshot_id: str,
    candidates: Sequence[LayerTwoCandidateInput],
    repo_root: Path,
    contract_path: Path | None = None,
) -> LayerTwoCandidateEligibilityReport:
    as_of = _require_exact_date(as_of, field_name="as_of")
    decision_at = _require_aware_datetime(decision_at, field_name="decision_at")
    if _decision_calendar_date(decision_at) != as_of:
        raise ValueError("decision_at calendar date must equal as_of")
    snapshot_id = _reject_blank_string(data_snapshot_id, field_name="data_snapshot_id")

    if not candidates:
        raise ValueError("requested_symbols must be non-empty")

    symbols = [candidate.symbol for candidate in candidates]
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate symbol inputs are forbidden")

    contract_id, contract_rel_path, policy = bind_two_layer_eligibility_policy(
        repo_root=repo_root,
        contract_path=contract_path,
    )

    sorted_candidates = sorted(candidates, key=lambda item: item.symbol)
    evaluations = [
        evaluate_layer_two_candidate(
            candidate,
            as_of=as_of,
            decision_at=decision_at,
            policy=policy,
        )
        for candidate in sorted_candidates
    ]

    report = LayerTwoCandidateEligibilityReport(
        as_of=as_of,
        decision_at=decision_at,
        data_snapshot_id=snapshot_id,
        two_layer_decision_contract_id=contract_id,
        two_layer_decision_contract_path=contract_rel_path,
        requested_symbols=[candidate.symbol for candidate in sorted_candidates],
        candidate_inputs=list(sorted_candidates),
        evaluations=evaluations,
    )
    return seal_layer_two_candidate_eligibility_report(report)


def canonical_report_payload(report: LayerTwoCandidateEligibilityReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: LayerTwoCandidateEligibilityReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: LayerTwoCandidateEligibilityReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_layer_two_candidate_eligibility_report(
    report: LayerTwoCandidateEligibilityReport,
) -> LayerTwoCandidateEligibilityReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: LayerTwoCandidateEligibilityReport) -> None:
    if report.report_id is None:
        raise ValueError("layer-two candidate eligibility report_id is missing")
    expected = compute_report_id(report)
    if report.report_id != expected:
        raise ValueError("layer-two candidate eligibility report_id does not match canonical content hash")


def assert_report_logic_consistent(
    report: LayerTwoCandidateEligibilityReport,
    *,
    repo_root: Path,
) -> None:
    if _decision_calendar_date(report.decision_at) != report.as_of:
        raise ValueError("decision_at calendar date must equal report as_of")
    _, _, policy = bind_two_layer_eligibility_policy(repo_root=repo_root)
    recomputed = [
        evaluate_layer_two_candidate(
            candidate,
            as_of=report.as_of,
            decision_at=report.decision_at,
            policy=policy,
        )
        for candidate in report.candidate_inputs
    ]
    for stored, expected in zip(report.evaluations, recomputed, strict=True):
        if stored.model_dump(mode="json") != expected.model_dump(mode="json"):
            raise ValueError(f"evaluation for {stored.symbol} does not recompute from sealed inputs")


def load_layer_two_candidate_eligibility_report(path: Path) -> LayerTwoCandidateEligibilityReport:
    try:
        return LayerTwoCandidateEligibilityReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("layer-two candidate eligibility report is missing or invalid") from exc


def verify_layer_two_candidate_eligibility_report(
    report: LayerTwoCandidateEligibilityReport,
    *,
    repo_root: Path,
) -> LayerTwoCandidateEligibilityReport:
    assert_report_self_hash(report)
    if (
        report.ready_for_scoring
        or report.ready_for_portfolio_construction
        or report.ready_for_orders
        or report.ready_for_trading
        or report.does_not_trade is not True
    ):
        raise ValueError("layer-two candidate eligibility report cannot authorize downstream execution")
    contract_id, contract_path, _policy = bind_two_layer_eligibility_policy(repo_root=repo_root)
    if report.two_layer_decision_contract_id != contract_id:
        raise ValueError("report two_layer_decision_contract_id does not match disk binding")
    if report.two_layer_decision_contract_path != contract_path:
        raise ValueError("report two_layer_decision_contract_path does not match disk binding")
    if len(report.candidate_inputs) != len(report.evaluations):
        raise ValueError("candidate_inputs and evaluations length mismatch")
    assert_report_logic_consistent(report, repo_root=repo_root)
    return report


def verify_layer_two_candidate_eligibility_report_file(
    path: Path,
    *,
    repo_root: Path,
) -> LayerTwoCandidateEligibilityReport:
    report = load_layer_two_candidate_eligibility_report(path)
    return verify_layer_two_candidate_eligibility_report(report, repo_root=repo_root)


def write_layer_two_candidate_eligibility_report(
    report: LayerTwoCandidateEligibilityReport,
    output: Path,
) -> None:
    sealed = seal_layer_two_candidate_eligibility_report(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "BOUND_TWO_LAYER_DECISION_CONTRACT_ID",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_PATH",
    "ELIGIBLE_REASON_CODE",
    "FAILURE_REASON_ORDER",
    "LAYER_TWO_CANDIDATE_ELIGIBILITY_ENGINE_VERSION",
    "LAYER_TWO_CANDIDATE_ELIGIBILITY_SCHEMA_VERSION",
    "LayerTwoCandidateEligibilityReport",
    "LayerTwoCandidateEvaluation",
    "LayerTwoCandidateInput",
    "LayerTwoEligibilityPolicy",
    "LayerTwoLiquidityObservation",
    "assert_candidate_input_integrity",
    "assert_report_logic_consistent",
    "assert_report_self_hash",
    "bind_two_layer_eligibility_policy",
    "compute_report_id",
    "evaluate_layer_two_candidate",
    "evaluate_layer_two_candidate_eligibility",
    "load_layer_two_candidate_eligibility_report",
    "seal_layer_two_candidate_eligibility_report",
    "verify_layer_two_candidate_eligibility_report",
    "verify_layer_two_candidate_eligibility_report_file",
    "write_layer_two_candidate_eligibility_report",
]
