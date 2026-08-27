"""E11b-0a: pure deterministic math kernels for the sealed E11a alpha protocol.

Research-only numerical primitives. This milestone does **not** assemble PIT
inputs, bind market/eligibility/financial/cluster files, seal content-addressed
reports, run data, score, backtest, or trade. Report sealing and the PIT input
assembler are the next milestone.

Bound E11a constants are declared for audit only; this module never reads disk.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from types import MappingProxyType
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LAYER_TWO_ALPHA_DIAGNOSTIC_ENGINE_VERSION: Literal["layer-two-alpha-diagnostic-engine-v0a"] = (
    "layer-two-alpha-diagnostic-engine-v0a"
)

BOUND_E11A_PROTOCOL_PATH: Literal["config/research/layer-two-alpha-development-protocol-v1.json"] = (
    "config/research/layer-two-alpha-development-protocol-v1.json"
)
BOUND_E11A_PROTOCOL_ID: Literal["fa91f0e260beb59a7f639dd3650a3842c817e470e9c3614abf2583dd691d2f86"] = (
    "fa91f0e260beb59a7f639dd3650a3842c817e470e9c3614abf2583dd691d2f86"
)

FROZEN_FACTOR_FAMILY_IDS: tuple[str, ...] = (
    "quality",
    "value",
    "medium_momentum_12_1",
    "defensive_low_vol",
)
FactorFamilyId = Literal["quality", "value", "medium_momentum_12_1", "defensive_low_vol"]

MOMENTUM_REQUIRED_BARS: Literal[243] = 243
MOMENTUM_INDEX_T_MINUS_242: Literal[0] = 0
MOMENTUM_INDEX_T_MINUS_21: Literal[221] = 221  # 242 - 21 within a 243-bar window ending at t
LOW_VOL_REQUIRED_BARS: Literal[61] = 61
LOW_VOL_RETURN_COUNT: Literal[60] = 60
LOW_VOL_STDEV_DDOF: Literal[1] = 1
ANNUALIZATION_TRADING_DAYS: Literal[242] = 242
QUINTILE_COUNT: Literal[5] = 5
COVERAGE_MIN_KNOWN_COUNT: Literal[500] = 500
COVERAGE_MIN_KNOWN_FRACTION: float = 0.60
QUALITY_MIN_KNOWN_COMPONENTS: Literal[3] = 3
VALUE_MIN_KNOWN_METRICS: Literal[2] = 2
HOLM_FAMILY_WISE_ALPHA: float = 0.05
HOLM_HYPOTHESIS_COUNT: Literal[4] = 4

SIZE_BAND_3BN = 3_000_000_000.0
SIZE_BAND_5BN = 5_000_000_000.0
SIZE_BAND_10BN = 10_000_000_000.0

ComponentRule = Literal["high", "low", "positive_only_inverted"]
SizeBandLabel = Literal["3bn_5bn", "5bn_10bn", "10bn_plus", "below_lowest", "unknown"]

QUALITY_SEALED_RULES: Mapping[str, ComponentRule] = MappingProxyType(
    {
        "roe": "high",
        "roic": "high",
        "grossprofit_margin": "high",
        "debt_to_assets": "low",
        "ocf_to_or": "high",
    }
)
VALUE_SEALED_KEYS: frozenset[str] = frozenset({"pe_ttm", "pb", "ps_ttm"})

_A_SHARE_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ)$")

# Window defect rule (momentum / low-vol / forward label):
# - Raise ValueError on structural/type defects (bool, nonfinite close, date
#   duplicates, length/order/extra/missing vs exact expected calendar list).
# - Return None (factor/label unknown) when the exact expected window is present
#   and typed correctly but any bar is unverified or non-positive.
# Gaps are never skip-compressed.


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _parse_calendar_date(value: object, *, field_name: str) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str) and value.strip():
        return date.fromisoformat(value.strip())
    raise ValueError(f"{field_name} must be a datetime.date")


class MarketObservation(_StrictModel):
    """One market-calendar bar for pure kernels; never skip-compressed."""

    date: date
    adj_close: float
    verified: bool

    @field_validator("date", mode="before")
    @classmethod
    def _coerce_date(cls, value: object) -> object:
        return _parse_calendar_date(value, field_name="date")

    @field_validator("adj_close", mode="before")
    @classmethod
    def _require_finite_close(cls, value: object) -> float:
        return _require_finite_number(value, field_name="adj_close")

    @field_validator("verified", mode="before")
    @classmethod
    def _require_strict_bool(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("verified must be a strict bool")
        return value


class SymbolCloseObservation(_StrictModel):
    """Same-symbol close observation for exact forward labels."""

    symbol: str = Field(min_length=1)
    date: date
    adj_close: float
    verified: bool

    @field_validator("symbol", mode="before")
    @classmethod
    def _validate_symbol(cls, value: object) -> str:
        return _validate_a_share_symbol(value, field_name="symbol")

    @field_validator("date", mode="before")
    @classmethod
    def _coerce_date(cls, value: object) -> object:
        return _parse_calendar_date(value, field_name="date")

    @field_validator("adj_close", mode="before")
    @classmethod
    def _require_finite_close(cls, value: object) -> float:
        return _require_finite_number(value, field_name="adj_close")

    @field_validator("verified", mode="before")
    @classmethod
    def _require_strict_bool(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("verified must be a strict bool")
        return value


class FamilyCompositeEntry(_StrictModel):
    """Distinguish raw equal-mean composite from final CS rerank percentile."""

    symbol: str = Field(min_length=1)
    known_component_count: int = Field(ge=0)
    raw_composite: float | None = None
    final_percentile: float | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _validate_symbol(cls, value: object) -> str:
        return _validate_a_share_symbol(value, field_name="symbol")

    @field_validator("raw_composite", "final_percentile", mode="before")
    @classmethod
    def _optional_finite(cls, value: object) -> float | None:
        if value is None:
            return None
        return _require_finite_number(value, field_name="composite_field")


class MultiComponentFamilyResult(_StrictModel):
    entries: dict[str, FamilyCompositeEntry]

    @model_validator(mode="after")
    def _keys_match(self) -> MultiComponentFamilyResult:
        for symbol, entry in self.entries.items():
            if entry.symbol != symbol:
                raise ValueError("FamilyCompositeEntry.symbol must match map key")
        return self


class NeweyWestBartlettResult(_StrictModel):
    """Exact NW/Bartlett inference; undefined cases keep statistic/p as None."""

    n: int = Field(ge=0)
    lag: int = Field(ge=0)
    mean: float | None = None
    long_run_variance: float | None = None
    variance_of_mean: float | None = None
    statistic: float | None = None
    positive_p_value: float | None = None
    negative_p_value: float | None = None
    defined: bool

    @field_validator(
        "mean",
        "long_run_variance",
        "variance_of_mean",
        "statistic",
        "positive_p_value",
        "negative_p_value",
        mode="before",
    )
    @classmethod
    def _optional_finite(cls, value: object) -> float | None:
        if value is None:
            return None
        return _require_finite_number(value, field_name="nw_field")


class HolmFactorResult(_StrictModel):
    factor_id: FactorFamilyId
    raw_p_value: float | None = None
    effective_p_value: float
    sorted_position: int = Field(ge=1, le=4)
    threshold: float
    rejected: bool

    @field_validator("raw_p_value", mode="before")
    @classmethod
    def _optional_raw_p(cls, value: object) -> float | None:
        if value is None:
            return None
        return _require_finite_number(value, field_name="raw_p_value")

    @field_validator("effective_p_value", "threshold", mode="before")
    @classmethod
    def _required_finite_p(cls, value: object) -> float:
        return _require_finite_number(value, field_name="holm_numeric")

    @field_validator("rejected", mode="before")
    @classmethod
    def _strict_bool(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("rejected must be a strict bool")
        return value


class HolmStepDownResult(_StrictModel):
    alpha: float
    results: tuple[HolmFactorResult, HolmFactorResult, HolmFactorResult, HolmFactorResult]

    @field_validator("alpha", mode="before")
    @classmethod
    def _sealed_alpha(cls, value: object) -> float:
        v = _require_finite_number(value, field_name="alpha")
        if v != HOLM_FAMILY_WISE_ALPHA:
            raise ValueError(f"alpha must be exactly {HOLM_FAMILY_WISE_ALPHA}")
        return v

    @model_validator(mode="after")
    def _exactly_four_frozen(self) -> HolmStepDownResult:
        ids = [item.factor_id for item in self.results]
        if sorted(ids, key=lambda fid: FROZEN_FACTOR_FAMILY_IDS.index(fid)) != list(FROZEN_FACTOR_FAMILY_IDS):
            # results may be sorted by Holm order; membership must be exact four
            if set(ids) != set(FROZEN_FACTOR_FAMILY_IDS) or len(ids) != 4:
                raise ValueError("Holm results must cover exactly the four frozen factor IDs")
        return self


def _require_finite_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a real number (bool rejected)")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite (NaN/Inf rejected)")
    return number


def _validate_a_share_symbol(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string, got {type(value).__name__}")
    if not _A_SHARE_SYMBOL_RE.match(value):
        raise ValueError(f"{field_name} must be a 6-digit A-share code ending in .SH or .SZ, got {value!r}")
    return value


def _require_non_bool_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an int (bool rejected)")
    return value


def _require_positive_denominator(value: object, *, field_name: str) -> int:
    number = _require_non_bool_int(value, field_name=field_name)
    if number <= 0:
        raise ValueError(f"{field_name} must be positive")
    return number


def standard_normal_cdf(x: float) -> float:
    """Phi(x) via erfc; fail-closed on nonfinite input."""
    z = _require_finite_number(x, field_name="x")
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def _canonical_symbol_items(
    values: Mapping[str, float | None],
) -> list[tuple[str, float | None]]:
    if not isinstance(values, Mapping):
        raise ValueError("values must be a mapping of symbol -> optional float")
    items: list[tuple[str, float | None]] = []
    seen: set[str] = set()
    for symbol in sorted(values.keys()):
        _validate_a_share_symbol(symbol, field_name="symbol")
        if symbol in seen:
            raise ValueError(f"duplicate symbol {symbol!r}")
        seen.add(symbol)
        raw = values[symbol]
        if raw is None:
            items.append((symbol, None))
            continue
        items.append((symbol, _require_finite_number(raw, field_name=f"values[{symbol}]")))
    return items


def _average_ranks_1_based(values: Sequence[float]) -> list[float]:
    """Average ranks with ties sharing the mean of 1-based positions; order-stable."""
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    pos = 0
    while pos < len(ordered):
        end = pos + 1
        while end < len(ordered) and ordered[end][1] == ordered[pos][1]:
            end += 1
        average = (pos + 1 + end) / 2.0
        for index, _ in ordered[pos:end]:
            ranks[index] = average
        pos = end
    return ranks


def average_rank_percentiles(
    values: Mapping[str, float | None],
    *,
    invert: bool = False,
) -> dict[str, float | None]:
    """Cross-sectional average-rank percentiles on a 0..100 scale.

    Formula: ``(average_rank_1_based - 1) / (n - 1) * 100`` with ties averaged.
    ``n == 1`` is unknown (not 50). Missing stays unknown. Bool/NaN/Inf raise.
    When ``invert`` is true, known percentiles become ``100 - p`` (low-direction /
    value inversion). Deterministic under any input mapping order.
    """
    if type(invert) is not bool:
        raise ValueError("invert must be a strict bool")
    items = _canonical_symbol_items(values)
    known_indices = [i for i, (_, value) in enumerate(items) if value is not None]
    n = len(known_indices)
    out: dict[str, float | None] = {symbol: None for symbol, _ in items}
    if n == 0:
        return out
    if n == 1:
        return out
    known_values = [items[i][1] for i in known_indices]
    assert all(v is not None for v in known_values)
    ranks = _average_ranks_1_based([float(v) for v in known_values])  # type: ignore[arg-type]
    for local_i, global_i in enumerate(known_indices):
        percentile = (ranks[local_i] - 1.0) / (n - 1) * 100.0
        if invert:
            percentile = 100.0 - percentile
        out[items[global_i][0]] = percentile
    return out


def multi_component_family_composite(
    component_values: Mapping[str, Mapping[str, float | None]],
    *,
    rules: Mapping[str, ComponentRule],
    min_known_components: int,
) -> MultiComponentFamilyResult:
    """Quality/value-style composite: per-component CS percentile, mean, final rerank.

    - ``high``: higher raw → higher percentile.
    - ``low``: percentile then ``100 - p``.
    - ``positive_only_inverted``: only strictly positive finite values rank; then invert.
    Symbols with fewer than ``min_known_components`` known component percentiles get
    ``raw_composite=None``. Final percentiles rerank known raw composites only.
    """
    min_known = _require_non_bool_int(min_known_components, field_name="min_known_components")
    if min_known < 1:
        raise ValueError("min_known_components must be >= 1")
    if set(component_values.keys()) != set(rules.keys()):
        raise ValueError("component_values keys must exactly match rules keys")
    if not component_values:
        raise ValueError("at least one component is required")

    component_ids = sorted(component_values.keys())
    symbols: set[str] = set()
    normalized: dict[str, dict[str, float | None]] = {}
    for component_id in component_ids:
        rule = rules[component_id]
        if rule not in ("high", "low", "positive_only_inverted"):
            raise ValueError(f"unsupported component rule {rule!r}")
        items = _canonical_symbol_items(component_values[component_id])
        normalized[component_id] = dict(items)
        symbols.update(normalized[component_id].keys())

    ordered_symbols = sorted(symbols)
    component_percentiles: dict[str, dict[str, float | None]] = {}
    for component_id in component_ids:
        rule = rules[component_id]
        raw_map = normalized[component_id]
        ranked_input: dict[str, float | None] = {}
        for symbol in ordered_symbols:
            value = raw_map.get(symbol)
            if value is None:
                ranked_input[symbol] = None
                continue
            if rule == "positive_only_inverted" and value <= 0.0:
                ranked_input[symbol] = None
                continue
            ranked_input[symbol] = value
        invert = rule in ("low", "positive_only_inverted")
        component_percentiles[component_id] = average_rank_percentiles(ranked_input, invert=invert)

    raw_by_symbol: dict[str, float | None] = {}
    known_counts: dict[str, int] = {}
    for symbol in ordered_symbols:
        known_ps: list[float] = []
        for component_id in component_ids:
            p = component_percentiles[component_id].get(symbol)
            if p is not None:
                known_ps.append(p)
        known_counts[symbol] = len(known_ps)
        if len(known_ps) < min_known:
            raw_by_symbol[symbol] = None
        else:
            raw_by_symbol[symbol] = sum(known_ps) / len(known_ps)

    final = average_rank_percentiles(raw_by_symbol, invert=False)
    entries = {
        symbol: FamilyCompositeEntry(
            symbol=symbol,
            known_component_count=known_counts[symbol],
            raw_composite=raw_by_symbol[symbol],
            final_percentile=final[symbol],
        )
        for symbol in ordered_symbols
    }
    return MultiComponentFamilyResult(entries=entries)


def quality_family_composite(
    component_values: Mapping[str, Mapping[str, float | None]],
) -> MultiComponentFamilyResult:
    """Quality family: sealed E11a protocol — roe, roic, grossprofit_margin (high),
    debt_to_assets (low), ocf_to_or (high); min 3 known. Not caller-configurable."""
    if set(component_values.keys()) != set(QUALITY_SEALED_RULES.keys()):
        raise ValueError(
            "quality_family_composite requires exactly the five sealed component keys: "
            + ", ".join(sorted(QUALITY_SEALED_RULES.keys()))
        )
    return multi_component_family_composite(
        component_values,
        rules=dict(QUALITY_SEALED_RULES),
        min_known_components=QUALITY_MIN_KNOWN_COMPONENTS,
    )


def value_family_composite(
    metric_values: Mapping[str, Mapping[str, float | None]],
) -> MultiComponentFamilyResult:
    """Value family: sealed E11a protocol — pe_ttm, pb, ps_ttm (positive_only_inverted);
    min 2 known. Not caller-configurable."""
    if set(metric_values.keys()) != VALUE_SEALED_KEYS:
        raise ValueError(
            "value_family_composite requires exactly the three sealed metric keys: "
            + ", ".join(sorted(VALUE_SEALED_KEYS))
        )
    rules: dict[str, ComponentRule] = {key: "positive_only_inverted" for key in VALUE_SEALED_KEYS}
    return multi_component_family_composite(
        metric_values,
        rules=rules,
        min_known_components=VALUE_MIN_KNOWN_METRICS,
    )


def _coerce_market_observations(
    observations: Sequence[MarketObservation | Mapping[str, object]],
) -> list[MarketObservation]:
    out: list[MarketObservation] = []
    for item in observations:
        if isinstance(item, MarketObservation):
            out.append(item)
        else:
            out.append(MarketObservation.model_validate(item))
    return out


def _validate_exact_expected_window(
    observations: Sequence[MarketObservation | Mapping[str, object]],
    *,
    expected_dates: Sequence[date],
    required_bars: int,
) -> list[MarketObservation] | None:
    """Return validated bars, None if unverified/nonpositive, else raise."""
    expected = list(expected_dates)
    if len(expected) != required_bars:
        raise ValueError(f"expected_dates must have length {required_bars}")
    if len(expected) != len(set(expected)):
        raise ValueError("expected_dates must be unique")
    if expected != sorted(expected):
        raise ValueError("expected_dates must be strictly ascending")

    bars = _coerce_market_observations(observations)
    if len(bars) != required_bars:
        raise ValueError(
            f"observations must have exactly {required_bars} bars matching expected_dates (never skip-compress gaps)"
        )
    bar_dates = [bar.date for bar in bars]
    if len(bar_dates) != len(set(bar_dates)):
        raise ValueError("observation dates must be unique (duplicate rejected)")
    if bar_dates != expected:
        raise ValueError(
            "observation dates must exactly equal expected_dates in order "
            "(missing/extra/order mismatch rejected; never skip-compress)"
        )

    for bar in bars:
        if not bar.verified or bar.adj_close <= 0.0:
            return None
    return bars


def medium_momentum_12_1(
    observations: Sequence[MarketObservation | Mapping[str, object]],
    *,
    expected_dates: Sequence[date],
) -> float | None:
    """``adj_close[t-21] / adj_close[t-242] - 1`` on an exact 243-bar window ending at t."""
    bars = _validate_exact_expected_window(
        observations,
        expected_dates=expected_dates,
        required_bars=MOMENTUM_REQUIRED_BARS,
    )
    if bars is None:
        return None
    start = bars[MOMENTUM_INDEX_T_MINUS_242].adj_close
    end = bars[MOMENTUM_INDEX_T_MINUS_21].adj_close
    return end / start - 1.0


def defensive_low_vol(
    observations: Sequence[MarketObservation | Mapping[str, object]],
    *,
    expected_dates: Sequence[date],
) -> float | None:
    """Negative annualized sample stdev of exactly 60 simple returns (ddof=1, sqrt 242)."""
    bars = _validate_exact_expected_window(
        observations,
        expected_dates=expected_dates,
        required_bars=LOW_VOL_REQUIRED_BARS,
    )
    if bars is None:
        return None
    returns: list[float] = []
    for index in range(LOW_VOL_RETURN_COUNT):
        left = bars[index].adj_close
        right = bars[index + 1].adj_close
        returns.append(right / left - 1.0)
    n = len(returns)
    mean = sum(returns) / n
    ss = sum((value - mean) ** 2 for value in returns)
    variance = ss / (n - LOW_VOL_STDEV_DDOF)
    if variance < 0.0 or not math.isfinite(variance):
        return None
    stdev = math.sqrt(variance)
    annualized = stdev * math.sqrt(float(ANNUALIZATION_TRADING_DAYS))
    return -annualized


def exact_forward_return(
    *,
    symbol: str,
    decision_date: date,
    horizon_market_days: int,
    expected_endpoint_date: date,
    observation_t: SymbolCloseObservation | Mapping[str, object],
    observation_endpoint: SymbolCloseObservation | Mapping[str, object] | None,
) -> float | None:
    """Exact same-symbol close-to-close forward return; horizon never shifts."""
    _validate_a_share_symbol(symbol, field_name="symbol")
    if type(decision_date) is not date:
        raise ValueError("decision_date must be a datetime.date")
    if type(expected_endpoint_date) is not date:
        raise ValueError("expected_endpoint_date must be a datetime.date")
    horizon = _require_non_bool_int(horizon_market_days, field_name="horizon_market_days")
    if horizon < 1:
        raise ValueError("horizon_market_days must be >= 1")

    t_obs = (
        observation_t
        if isinstance(observation_t, SymbolCloseObservation)
        else SymbolCloseObservation.model_validate(observation_t)
    )
    if t_obs.symbol != symbol:
        raise ValueError("observation_t.symbol must equal symbol")
    if t_obs.date != decision_date:
        raise ValueError("observation_t.date must equal decision_date")
    if not t_obs.verified or t_obs.adj_close <= 0.0:
        return None

    if observation_endpoint is None:
        return None
    end_obs = (
        observation_endpoint
        if isinstance(observation_endpoint, SymbolCloseObservation)
        else SymbolCloseObservation.model_validate(observation_endpoint)
    )
    if end_obs.symbol != symbol:
        raise ValueError("observation_endpoint.symbol must equal symbol (same-symbol endpoint)")
    if end_obs.date != expected_endpoint_date:
        raise ValueError(
            "observation_endpoint.date must equal expected_endpoint_date "
            "(horizon never shifts; wrong endpoint rejected)"
        )
    if not end_obs.verified or end_obs.adj_close <= 0.0:
        return None
    return end_obs.adj_close / t_obs.adj_close - 1.0


def paired_spearman(
    pairs: Sequence[tuple[float | None, float | None]],
) -> float | None:
    """Spearman IC on paired known finite rows; ties averaged; all-equal unknown."""
    xs: list[float] = []
    ys: list[float] = []
    for factor, label in pairs:
        if factor is None or label is None:
            continue
        xs.append(_require_finite_number(factor, field_name="factor"))
        ys.append(_require_finite_number(label, field_name="label"))
    n = len(xs)
    if n < 2:
        return None
    if max(xs) == min(xs) or max(ys) == min(ys):
        return None
    x_rank = _average_ranks_1_based(xs)
    y_rank = _average_ranks_1_based(ys)
    x_mean = sum(x_rank) / n
    y_mean = sum(y_rank) / n
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_rank, y_rank, strict=True))
    x_var = sum((x - x_mean) ** 2 for x in x_rank)
    y_var = sum((y - y_mean) ** 2 for y in y_rank)
    denominator = math.sqrt(x_var * y_var)
    if denominator <= 0.0 or not math.isfinite(denominator):
        return None
    value = numerator / denominator
    if not math.isfinite(value):
        return None
    return value


def quintile_top_minus_bottom_spread(
    pairs: Sequence[tuple[float, float]],
) -> float | None:
    """Equal-weight top-minus-bottom quintile spread; ties never split across buckets.

    Bucket = ``min(floor((rank - 1) / n * 5), 4)``. All-equal factor CS or empty
    extreme buckets → unknown.
    """
    if not pairs:
        return None
    factors: list[float] = []
    labels: list[float] = []
    for factor, label in pairs:
        factors.append(_require_finite_number(factor, field_name="factor"))
        labels.append(_require_finite_number(label, field_name="label"))
    n = len(factors)
    if n < QUINTILE_COUNT:
        return None
    if max(factors) == min(factors):
        return None
    ranks = _average_ranks_1_based(factors)
    buckets: list[list[float]] = [[] for _ in range(QUINTILE_COUNT)]
    for rank, label in zip(ranks, labels, strict=True):
        bucket = min(int(math.floor((rank - 1.0) / n * QUINTILE_COUNT)), QUINTILE_COUNT - 1)
        buckets[bucket].append(label)
    highest = buckets[-1]
    lowest = buckets[0]
    if not highest or not lowest:
        return None
    spread = sum(highest) / len(highest) - sum(lowest) / len(lowest)
    if not math.isfinite(spread):
        return None
    return spread


def coverage_gate(*, known_count: int, eligible_count: int) -> bool:
    """Pass iff ``known_count >= 500`` and ``known_count / eligible_count >= 0.60``."""
    known = _require_non_bool_int(known_count, field_name="known_count")
    eligible = _require_positive_denominator(eligible_count, field_name="eligible_count")
    if known < 0:
        raise ValueError("known_count must be >= 0")
    if known > eligible:
        raise ValueError("known_count cannot exceed eligible_count")
    fraction = known / eligible
    return known >= COVERAGE_MIN_KNOWN_COUNT and fraction >= COVERAGE_MIN_KNOWN_FRACTION


def newey_west_bartlett_inference(
    series: Sequence[float],
    *,
    lag: int,
) -> NeweyWestBartlettResult:
    """Exact Newey-West / Bartlett one-sided inference on an ordered finite series.

    ``gamma_k`` uses divisor ``n``; Bartlett ``w_k = 1 - k/(L+1)``;
    ``LRV = gamma_0 + 2 * sum w_k gamma_k``; ``var(mean) = LRV / n``.
    When ``n <= L`` or LRV/variance is nonfinite or ``<= 0``, statistic and both
    p-values stay ``None`` (never coerced). Positive p = ``1 - Phi(stat)``;
    negative p = ``Phi(stat)``.
    """
    lag_i = _require_non_bool_int(lag, field_name="lag")
    if lag_i < 0:
        raise ValueError("lag must be >= 0")
    values = [_require_finite_number(value, field_name="series") for value in series]
    n = len(values)
    if n == 0:
        return NeweyWestBartlettResult(n=0, lag=lag_i, defined=False)
    mean = sum(values) / n
    if n <= lag_i:
        return NeweyWestBartlettResult(n=n, lag=lag_i, mean=mean, defined=False)

    centered = [value - mean for value in values]
    gamma_0 = sum(value * value for value in centered) / n
    lrv = gamma_0
    max_k = min(lag_i, n - 1)
    for order in range(1, max_k + 1):
        weight = 1.0 - order / (lag_i + 1)
        gamma = sum(centered[index] * centered[index - order] for index in range(order, n)) / n
        lrv += 2.0 * weight * gamma
    variance_of_mean = lrv / n
    if not math.isfinite(lrv) or not math.isfinite(variance_of_mean) or lrv <= 0.0 or variance_of_mean <= 0.0:
        return NeweyWestBartlettResult(
            n=n,
            lag=lag_i,
            mean=mean,
            long_run_variance=lrv if math.isfinite(lrv) else None,
            variance_of_mean=variance_of_mean if math.isfinite(variance_of_mean) else None,
            defined=False,
        )

    statistic = mean / math.sqrt(variance_of_mean)
    if not math.isfinite(statistic):
        return NeweyWestBartlettResult(
            n=n,
            lag=lag_i,
            mean=mean,
            long_run_variance=lrv,
            variance_of_mean=variance_of_mean,
            defined=False,
        )
    phi = standard_normal_cdf(statistic)
    return NeweyWestBartlettResult(
        n=n,
        lag=lag_i,
        mean=mean,
        long_run_variance=lrv,
        variance_of_mean=variance_of_mean,
        statistic=statistic,
        positive_p_value=1.0 - phi,
        negative_p_value=phi,
        defined=True,
    )


def holm_step_down_four_factors(
    raw_p_values: Mapping[str, float | None],
) -> HolmStepDownResult:
    """Holm step-down over exactly the four frozen factor family IDs.

    Family-wise alpha is sealed at 0.05 and not caller-configurable.
    Effective p = raw p when finite, else 1. Sort by (effective p ascending,
    frozen family order). Threshold at sorted position i is ``alpha / (4 - i + 1)``.
    Reject sequentially until the first failure; remaining stay non-rejected.
    """
    alpha_f = HOLM_FAMILY_WISE_ALPHA
    if set(raw_p_values.keys()) != set(FROZEN_FACTOR_FAMILY_IDS):
        raise ValueError("raw_p_values keys must be exactly the four frozen factor IDs")

    prepared: list[tuple[str, float | None, float]] = []
    for factor_id in FROZEN_FACTOR_FAMILY_IDS:
        raw = raw_p_values[factor_id]
        if raw is None:
            prepared.append((factor_id, None, 1.0))
            continue
        raw_f = _require_finite_number(raw, field_name=f"raw_p_values[{factor_id}]")
        if raw_f < 0.0 or raw_f > 1.0:
            raise ValueError(f"raw_p_values[{factor_id}] must lie in [0, 1]")
        prepared.append((factor_id, raw_f, raw_f))

    ordered = sorted(
        prepared,
        key=lambda item: (item[2], FROZEN_FACTOR_FAMILY_IDS.index(item[0])),
    )
    rejected_flags = [False, False, False, False]
    still_rejecting = True
    for index, (_factor_id, _raw, effective) in enumerate(ordered):
        threshold = alpha_f / (HOLM_HYPOTHESIS_COUNT - index)
        if still_rejecting and effective <= threshold:
            rejected_flags[index] = True
        else:
            still_rejecting = False
            rejected_flags[index] = False

    results: list[HolmFactorResult] = []
    for index, (factor_id, raw, effective) in enumerate(ordered):
        position = index + 1
        threshold = alpha_f / (HOLM_HYPOTHESIS_COUNT - index)
        results.append(
            HolmFactorResult(
                factor_id=cast(FactorFamilyId, factor_id),
                raw_p_value=raw,
                effective_p_value=effective,
                sorted_position=position,
                threshold=threshold,
                rejected=rejected_flags[index],
            )
        )
    return HolmStepDownResult(alpha=alpha_f, results=results)


def size_band(free_float_market_cap_cny: float | None) -> SizeBandLabel:
    """Map free-float CNY market cap to sealed size bands; below/unknown outside."""
    if free_float_market_cap_cny is None:
        return "unknown"
    cap = _require_finite_number(free_float_market_cap_cny, field_name="free_float_market_cap_cny")
    if cap < 0.0:
        raise ValueError("free_float_market_cap_cny must not be negative")
    if cap < SIZE_BAND_3BN:
        return "below_lowest"
    if cap < SIZE_BAND_5BN:
        return "3bn_5bn"
    if cap < SIZE_BAND_10BN:
        return "5bn_10bn"
    return "10bn_plus"


def within_cluster_percentiles(
    values: Mapping[str, float | None],
    cluster_assignment: Mapping[str, str | None],
) -> dict[str, float | None]:
    """Rerank known values within each non-singleton prevalidated cluster only.

    Does **not** create clusters or claim PIT industry history. Unassigned
    (``None``) and singleton clusters yield unknown. Symbols missing from either
    map raise. Deterministic under input order.
    """
    value_items = _canonical_symbol_items(values)
    value_map = dict(value_items)
    if set(value_map.keys()) != set(cluster_assignment.keys()):
        raise ValueError("values and cluster_assignment must cover the same symbol set")

    clusters: dict[str, list[str]] = defaultdict(list)
    ordered_symbols = sorted(value_map.keys())
    for symbol in ordered_symbols:
        cluster_id = cluster_assignment[symbol]
        if cluster_id is None:
            continue
        if not isinstance(cluster_id, str) or cluster_id.strip() == "":
            raise ValueError(f"cluster_id for {symbol!r} must be a non-empty string or None")
        clusters[cluster_id].append(symbol)

    out: dict[str, float | None] = {symbol: None for symbol in ordered_symbols}
    for _cluster_id, members in sorted(clusters.items(), key=lambda item: item[0]):
        if len(members) < 2:
            continue
        subset = {symbol: value_map[symbol] for symbol in members}
        ranked = average_rank_percentiles(subset, invert=False)
        for symbol, percentile in ranked.items():
            out[symbol] = percentile
    return out


__all__ = [
    "ANNUALIZATION_TRADING_DAYS",
    "BOUND_E11A_PROTOCOL_ID",
    "BOUND_E11A_PROTOCOL_PATH",
    "COVERAGE_MIN_KNOWN_COUNT",
    "COVERAGE_MIN_KNOWN_FRACTION",
    "FROZEN_FACTOR_FAMILY_IDS",
    "FamilyCompositeEntry",
    "HOLM_FAMILY_WISE_ALPHA",
    "HOLM_HYPOTHESIS_COUNT",
    "HolmFactorResult",
    "HolmStepDownResult",
    "LAYER_TWO_ALPHA_DIAGNOSTIC_ENGINE_VERSION",
    "LOW_VOL_REQUIRED_BARS",
    "LOW_VOL_RETURN_COUNT",
    "LOW_VOL_STDEV_DDOF",
    "MOMENTUM_INDEX_T_MINUS_21",
    "MOMENTUM_INDEX_T_MINUS_242",
    "MOMENTUM_REQUIRED_BARS",
    "MarketObservation",
    "MultiComponentFamilyResult",
    "NeweyWestBartlettResult",
    "QUALITY_MIN_KNOWN_COMPONENTS",
    "QUALITY_SEALED_RULES",
    "QUINTILE_COUNT",
    "SymbolCloseObservation",
    "VALUE_MIN_KNOWN_METRICS",
    "VALUE_SEALED_KEYS",
    "average_rank_percentiles",
    "coverage_gate",
    "defensive_low_vol",
    "exact_forward_return",
    "holm_step_down_four_factors",
    "medium_momentum_12_1",
    "multi_component_family_composite",
    "newey_west_bartlett_inference",
    "paired_spearman",
    "quality_family_composite",
    "quintile_top_minus_bottom_spread",
    "size_band",
    "standard_normal_cdf",
    "value_family_composite",
    "within_cluster_percentiles",
]
