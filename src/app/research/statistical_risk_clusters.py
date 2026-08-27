"""Read-only statistical risk-cluster diagnostic (not industry classification).

Clusters are deterministic connected components of pairwise Pearson correlations
above an explicit threshold. Components may chain-link through intermediate
names and are only a transitional risk proxy — never industry alpha, never
auto-applied to scoring, portfolios, or trading in this milestone.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.storage.protocol import MarketStore

STATISTICAL_RISK_CLUSTER_SCHEMA_VERSION: Literal["1"] = "1"
STATISTICAL_RISK_CLUSTER_DIAGNOSTIC_VERSION: Literal["statistical-risk-cluster-diagnostic-v1"] = (
    "statistical-risk-cluster-diagnostic-v1"
)

# Connected components may chain A–B–C even when A and C are weakly correlated.
RISK_PROXY_NOTE: Literal[
    "Clusters are connected components of pairwise correlations at or above the "
    "threshold. Chain linkage is possible; this is a statistical risk proxy only, "
    "not an industry classification or alpha signal."
] = (
    "Clusters are connected components of pairwise correlations at or above the "
    "threshold. Chain linkage is possible; this is a statistical risk proxy only, "
    "not an industry classification or alpha signal."
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UnresolvedSymbol(_StrictModel):
    symbol: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class UnresolvedPair(_StrictModel):
    symbol_a: str = Field(min_length=1)
    symbol_b: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class EvaluatedPair(_StrictModel):
    symbol_a: str = Field(min_length=1)
    symbol_b: str = Field(min_length=1)
    correlation: float
    above_threshold: bool

    @field_validator("correlation")
    @classmethod
    def _finite_correlation(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("correlation must be finite")
        return value


class RiskCluster(_StrictModel):
    cluster_id: str = Field(min_length=1)
    symbols: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _symbols_sorted_unique(self) -> RiskCluster:
        if sorted(self.symbols) != self.symbols:
            raise ValueError("cluster symbols must be sorted")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("cluster symbols must be unique")
        return self


class StatisticalRiskClusterReport(_StrictModel):
    """Sealed diagnostic report; never authorizes scoring, trading, or auto-apply."""

    schema_version: Literal["1"] = STATISTICAL_RISK_CLUSTER_SCHEMA_VERSION
    diagnostic_version: Literal["statistical-risk-cluster-diagnostic-v1"] = (
        STATISTICAL_RISK_CLUSTER_DIAGNOSTIC_VERSION
    )
    report_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    data_snapshot_id: str = Field(min_length=1)
    as_of: date
    lookback_bars: int = Field(ge=1)
    correlation_threshold: float
    candidates: list[str]
    candidates_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_trading_days: list[date]
    pairs: list[EvaluatedPair]
    clusters: list[RiskCluster]
    unresolved_symbols: list[UnresolvedSymbol]
    unresolved_pairs: list[UnresolvedPair]
    risk_proxy_note: Literal[
        "Clusters are connected components of pairwise correlations at or above the "
        "threshold. Chain linkage is possible; this is a statistical risk proxy only, "
        "not an industry classification or alpha signal."
    ] = RISK_PROXY_NOTE
    is_not_industry_classification: Literal[True] = True
    diagnostic_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    ready_for_portfolio_constraints: bool

    @field_validator("correlation_threshold")
    @classmethod
    def _threshold_in_unit_interval(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("correlation_threshold must be finite")
        if not (0.0 < value <= 1.0):
            raise ValueError("correlation_threshold must be in (0, 1]")
        return value

    @model_validator(mode="after")
    def _ready_flags_consistent(self) -> StatisticalRiskClusterReport:
        has_unresolved = bool(self.unresolved_symbols) or bool(self.unresolved_pairs)
        if has_unresolved and self.ready_for_portfolio_constraints:
            raise ValueError(
                "ready_for_portfolio_constraints must be false when unresolved symbols/pairs exist"
            )
        if not has_unresolved and not self.ready_for_portfolio_constraints:
            raise ValueError(
                "ready_for_portfolio_constraints must be true when the diagnostic is complete"
            )
        if self.diagnostic_only is not True:
            raise ValueError("diagnostic_only must remain true")
        if self.ready_for_scoring or self.ready_for_trading or self.auto_apply:
            raise ValueError("scoring/trading/auto_apply must remain false")
        return self


def diagnose_statistical_risk_clusters(
    store: MarketStore,
    as_of: date,
    symbols: Sequence[str],
    lookback_bars: int,
    correlation_threshold: float,
) -> StatisticalRiskClusterReport:
    """Build a sealed statistical risk-cluster diagnostic.

    All arguments are required; there are no defaults. This function does not
    score, backtest, trade, or mutate portfolio constraints.
    """
    candidates = _normalize_candidates(symbols)
    if lookback_bars < 1:
        raise ValueError("lookback_bars must be >= 1")
    if not math.isfinite(correlation_threshold) or not (0.0 < correlation_threshold <= 1.0):
        raise ValueError("correlation_threshold must be in (0, 1]")

    snapshot_id = store.snapshot().snapshot_id
    if not snapshot_id:
        raise ValueError("data_snapshot_id must be non-empty")

    required_days = _required_trading_days(store, as_of=as_of, lookback_bars=lookback_bars)
    price_by_symbol, unresolved_symbols = _load_aligned_prices(
        store,
        as_of=as_of,
        candidates=candidates,
        required_days=required_days,
    )
    returns_by_symbol = {
        symbol: _returns_from_prices(prices) for symbol, prices in price_by_symbol.items()
    }

    pairs: list[EvaluatedPair] = []
    unresolved_pairs: list[UnresolvedPair] = []
    evaluable = sorted(returns_by_symbol)
    for index, symbol_a in enumerate(evaluable):
        for symbol_b in evaluable[index + 1 :]:
            correlation = _pearson(returns_by_symbol[symbol_a], returns_by_symbol[symbol_b])
            if correlation is None:
                unresolved_pairs.append(
                    UnresolvedPair(
                        symbol_a=symbol_a,
                        symbol_b=symbol_b,
                        reason=_pair_unresolved_reason(
                            returns_by_symbol[symbol_a],
                            returns_by_symbol[symbol_b],
                        ),
                    )
                )
                continue
            pairs.append(
                EvaluatedPair(
                    symbol_a=symbol_a,
                    symbol_b=symbol_b,
                    correlation=correlation,
                    above_threshold=correlation >= correlation_threshold,
                )
            )

    clusters = _connected_components(
        evaluable_symbols=evaluable,
        pairs=pairs,
        correlation_threshold=correlation_threshold,
    )
    ready = not unresolved_symbols and not unresolved_pairs
    report = StatisticalRiskClusterReport(
        data_snapshot_id=snapshot_id,
        as_of=as_of,
        lookback_bars=lookback_bars,
        correlation_threshold=correlation_threshold,
        candidates=candidates,
        candidates_hash=candidates_hash(candidates),
        required_trading_days=required_days if required_days is not None else [],
        pairs=pairs,
        clusters=clusters,
        unresolved_symbols=unresolved_symbols,
        unresolved_pairs=unresolved_pairs,
        ready_for_portfolio_constraints=ready,
    )
    return seal_statistical_risk_cluster_report(report)


def candidates_hash(symbols: Sequence[str]) -> str:
    payload = json.dumps(list(symbols), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_report_payload(report: StatisticalRiskClusterReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: StatisticalRiskClusterReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: StatisticalRiskClusterReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_statistical_risk_cluster_report(
    report: StatisticalRiskClusterReport,
) -> StatisticalRiskClusterReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: StatisticalRiskClusterReport) -> None:
    if report.report_id is None:
        raise ValueError("statistical risk cluster report_id is missing")
    expected = compute_report_id(report)
    if report.report_id != expected:
        raise ValueError("statistical risk cluster report_id does not match canonical content hash")


def write_statistical_risk_cluster_report(
    report: StatisticalRiskClusterReport,
    output: Path,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _normalize_candidates(symbols: Sequence[str]) -> list[str]:
    if not isinstance(symbols, Sequence) or isinstance(symbols, (str, bytes)):
        raise ValueError("symbols must be a sequence of unique non-empty strings")
    if len(symbols) == 0:
        raise ValueError("symbols must be non-empty")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in symbols:
        if not isinstance(item, str) or item.strip() == "":
            raise ValueError("symbols entries must be non-empty strings")
        if item in seen:
            raise ValueError(f"duplicate candidate symbol: {item}")
        seen.add(item)
        normalized.append(item)
    return sorted(normalized)


def _required_trading_days(
    store: MarketStore,
    *,
    as_of: date,
    lookback_bars: int,
) -> list[date] | None:
    calendar = store.get_calendar(date(1970, 1, 1), as_of)
    _reject_future_dates(calendar, as_of=as_of, context="calendar")
    if len(calendar) < lookback_bars + 1:
        return None
    window = calendar[-(lookback_bars + 1) :]
    _reject_future_dates(window, as_of=as_of, context="required_trading_days")
    return window


def _load_aligned_prices(
    store: MarketStore,
    *,
    as_of: date,
    candidates: Sequence[str],
    required_days: list[date] | None,
) -> tuple[dict[str, list[float]], list[UnresolvedSymbol]]:
    unresolved: list[UnresolvedSymbol] = []
    prices: dict[str, list[float]] = {}
    if required_days is None:
        for symbol in candidates:
            unresolved.append(
                UnresolvedSymbol(symbol=symbol, reason="insufficient_trading_calendar")
            )
        return prices, unresolved

    start = required_days[0]
    required_set = set(required_days)
    for symbol in candidates:
        daily = store.get_daily_bars(as_of=as_of, symbol=symbol, start=start)
        if "adj_close" not in daily.columns or "date" not in daily.columns:
            unresolved.append(UnresolvedSymbol(symbol=symbol, reason="missing_adj_close_column"))
            continue
        rows = daily.select(["date", "adj_close"]).to_dicts()
        dates = [row["date"] for row in rows]
        _reject_future_dates(dates, as_of=as_of, context=f"daily_bars[{symbol}]")
        by_day: dict[date, float] = {}
        duplicate = False
        bad_price = False
        for row in rows:
            day = row["date"]
            if not isinstance(day, date):
                bad_price = True
                break
            if day not in required_set:
                # Bars outside the required window are ignored only when <= as_of.
                continue
            raw = row["adj_close"]
            if isinstance(raw, bool) or not isinstance(raw, int | float):
                bad_price = True
                break
            price = float(raw)
            if not math.isfinite(price) or price <= 0.0:
                bad_price = True
                break
            if day in by_day:
                duplicate = True
                break
            by_day[day] = price
        if duplicate:
            unresolved.append(UnresolvedSymbol(symbol=symbol, reason="duplicate_dates"))
            continue
        if bad_price:
            unresolved.append(
                UnresolvedSymbol(symbol=symbol, reason="non_finite_or_non_positive_adj_close")
            )
            continue
        missing_days = [day for day in required_days if day not in by_day]
        if missing_days:
            unresolved.append(UnresolvedSymbol(symbol=symbol, reason="missing_trading_day_history"))
            continue
        prices[symbol] = [by_day[day] for day in required_days]
    unresolved.sort(key=lambda item: (item.symbol, item.reason))
    return prices, unresolved


def _returns_from_prices(prices: Sequence[float]) -> list[float]:
    return [prices[index] / prices[index - 1] - 1.0 for index in range(1, len(prices))]


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys):
        return None
    n = len(xs)
    if n < 1:
        return None
    if any(not math.isfinite(value) for value in xs) or any(not math.isfinite(value) for value in ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0.0 or var_y == 0.0:
        return None
    return numerator / math.sqrt(var_x * var_y)


def _pair_unresolved_reason(xs: Sequence[float], ys: Sequence[float]) -> str:
    if len(xs) != len(ys) or len(xs) < 1:
        return "insufficient_observations"
    if any(not math.isfinite(value) for value in xs) or any(not math.isfinite(value) for value in ys):
        return "insufficient_observations"
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0.0 or var_y == 0.0:
        return "constant_return_series"
    return "insufficient_observations"


def _connected_components(
    *,
    evaluable_symbols: Sequence[str],
    pairs: Sequence[EvaluatedPair],
    correlation_threshold: float,
) -> list[RiskCluster]:
    parent = {symbol: symbol for symbol in evaluable_symbols}

    def find(symbol: str) -> str:
        while parent[symbol] != symbol:
            parent[symbol] = parent[parent[symbol]]
            symbol = parent[symbol]
        return symbol

    def union(left: str, right: str) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left == root_right:
            return
        # Deterministic link: lexicographically smaller root becomes parent.
        if root_left < root_right:
            parent[root_right] = root_left
        else:
            parent[root_left] = root_right

    edges = sorted(
        (pair.symbol_a, pair.symbol_b)
        for pair in pairs
        if pair.above_threshold and pair.correlation >= correlation_threshold
    )
    for symbol_a, symbol_b in edges:
        union(symbol_a, symbol_b)

    groups: dict[str, list[str]] = {}
    for symbol in evaluable_symbols:
        root = find(symbol)
        groups.setdefault(root, []).append(symbol)

    ordered_groups = sorted(
        (sorted(members) for members in groups.values()),
        key=lambda members: (members[0], members),
    )
    clusters: list[RiskCluster] = []
    for index, members in enumerate(ordered_groups, start=1):
        clusters.append(
            RiskCluster(
                cluster_id=f"cluster_{index:03d}",
                symbols=members,
            )
        )
    return clusters


def _reject_future_dates(values: Sequence[date | Any], *, as_of: date, context: str) -> None:
    for value in values:
        if isinstance(value, date) and value > as_of:
            raise ValueError(f"{context} contains date {value.isoformat()} after as_of {as_of.isoformat()}")


def report_uses_only_as_of_or_earlier(
    report: StatisticalRiskClusterReport,
    *,
    as_of: date,
) -> None:
    """Fail closed if a sealed report somehow references a post-as_of day."""
    _reject_future_dates(report.required_trading_days, as_of=as_of, context="report.required_trading_days")
    if report.as_of > as_of:
        raise ValueError("report.as_of is after the diagnosis as_of")
