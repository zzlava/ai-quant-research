"""Read-only long-history INDEX risk feature diagnostic (no regime / risk-budget).

Descriptive continuous features only. This module never scores, backtests, trades,
applies risk budgets, or invents lookback / threshold defaults.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.snapshot import DataSnapshot
from app.storage.protocol import MarketStore

INDEX_RISK_FEATURE_SCHEMA_VERSION: Literal["1"] = "1"
INDEX_RISK_FEATURE_DIAGNOSTIC_VERSION: Literal["index-risk-feature-diagnostic-v1"] = "index-risk-feature-diagnostic-v1"

# Matches app.backtest.metrics.TRADING_DAYS_PER_YEAR for A-share descriptive stats.
# Not an economic threshold; documented so reports stay auditable.
INDEX_RISK_ANNUALIZATION_TRADING_DAYS_PER_YEAR: Literal[242] = 242
INDEX_RISK_ANNUALIZATION_CONVENTION: Literal["sample_std_simple_daily_returns_times_sqrt_242"] = (
    "sample_std_simple_daily_returns_times_sqrt_242"
)
INDEX_RISK_RETURN_DEFINITION: Literal["simple_close_to_close"] = "simple_close_to_close"
INDEX_RISK_PRICE_FIELD: Literal["close"] = "close"


@runtime_checkable
class IndexBarSource(Protocol):
    """Narrow read-only source already satisfied by MarketStore."""

    def get_calendar(self, start: date, end: date) -> list[date]: ...

    def get_index_bars(
        self,
        as_of: date,
        symbol: str | None = None,
        start: date | None = None,
    ) -> Any: ...

    def snapshot(self) -> DataSnapshot: ...


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndexRiskFeatureReport(_StrictModel):
    """Sealed diagnostic report; never authorizes scoring, backtest, or trading."""

    schema_version: Literal["1"] = INDEX_RISK_FEATURE_SCHEMA_VERSION
    diagnostic_version: Literal["index-risk-feature-diagnostic-v1"] = INDEX_RISK_FEATURE_DIAGNOSTIC_VERSION
    report_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    data_snapshot_id: str = Field(min_length=1)
    index_symbol: str = Field(min_length=1)
    as_of: date
    trend_lookback_bars: int = Field(ge=1)
    volatility_lookback_bars: int = Field(ge=2)
    drawdown_lookback_bars: int = Field(ge=1)
    price_field: Literal["close"] = INDEX_RISK_PRICE_FIELD
    return_definition: Literal["simple_close_to_close"] = INDEX_RISK_RETURN_DEFINITION
    annualization_convention: Literal["sample_std_simple_daily_returns_times_sqrt_242"] = (
        INDEX_RISK_ANNUALIZATION_CONVENTION
    )
    annualization_trading_days_per_year: Literal[242] = INDEX_RISK_ANNUALIZATION_TRADING_DAYS_PER_YEAR
    trend_window_dates: list[date]
    volatility_price_window_dates: list[date]
    drawdown_window_dates: list[date]
    observation_count_trend: int = Field(ge=0)
    observation_count_volatility_returns: int = Field(ge=0)
    observation_count_drawdown: int = Field(ge=0)
    latest_close: float
    simple_moving_average: float
    close_to_sma_ratio: float
    realized_volatility_annualized: float
    rolling_peak: float
    drawdown: float
    diagnostic_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator(
        "latest_close",
        "simple_moving_average",
        "close_to_sma_ratio",
        "realized_volatility_annualized",
        "rolling_peak",
        "drawdown",
    )
    @classmethod
    def _finite_feature(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("feature values must be finite")
        return value

    @model_validator(mode="after")
    def _gate_flags_and_windows(self) -> IndexRiskFeatureReport:
        if self.diagnostic_only is not True:
            raise ValueError("diagnostic_only must remain true")
        if self.ready_for_scoring or self.ready_for_backtest or self.ready_for_trading or self.auto_apply:
            raise ValueError("scoring/backtest/trading/auto_apply must remain false")
        if len(self.trend_window_dates) != self.trend_lookback_bars:
            raise ValueError("trend_window_dates length must equal trend_lookback_bars")
        if len(self.volatility_price_window_dates) != self.volatility_lookback_bars + 1:
            raise ValueError("volatility_price_window_dates length must equal volatility_lookback_bars + 1")
        if len(self.drawdown_window_dates) != self.drawdown_lookback_bars:
            raise ValueError("drawdown_window_dates length must equal drawdown_lookback_bars")
        if self.observation_count_trend != self.trend_lookback_bars:
            raise ValueError("observation_count_trend must equal trend_lookback_bars")
        if self.observation_count_volatility_returns != self.volatility_lookback_bars:
            raise ValueError("observation_count_volatility_returns must equal volatility_lookback_bars")
        if self.observation_count_drawdown != self.drawdown_lookback_bars:
            raise ValueError("observation_count_drawdown must equal drawdown_lookback_bars")
        assert_report_date_window_structure(self)
        return self


def diagnose_index_risk_features(
    store: MarketStore | IndexBarSource,
    *,
    index_symbol: str,
    as_of: date,
    trend_lookback_bars: int,
    volatility_lookback_bars: int,
    drawdown_lookback_bars: int,
) -> IndexRiskFeatureReport:
    """Build a sealed index risk-feature diagnostic.

    All lookbacks are required; there are no defaults. Output contains continuous
    descriptive features only — no regime label and no risk-budget decision.
    """
    symbol = _require_symbol(index_symbol)
    _require_positive_int(trend_lookback_bars, field_name="trend_lookback_bars")
    _require_lookback_at_least(volatility_lookback_bars, minimum=2, field_name="volatility_lookback_bars")
    _require_positive_int(drawdown_lookback_bars, field_name="drawdown_lookback_bars")

    snapshot_id = store.snapshot().snapshot_id
    if not snapshot_id:
        raise ValueError("data_snapshot_id must be non-empty")

    needed_bars = max(
        trend_lookback_bars,
        volatility_lookback_bars + 1,
        drawdown_lookback_bars,
    )
    calendar = _validate_market_calendar(store.get_calendar(date(1970, 1, 1), as_of), as_of=as_of)
    if len(calendar) < needed_bars:
        raise ValueError(f"insufficient market calendar days <= as_of: need {needed_bars}, have {len(calendar)}")

    trend_window = calendar[-trend_lookback_bars:]
    volatility_window = calendar[-(volatility_lookback_bars + 1) :]
    drawdown_window = calendar[-drawdown_lookback_bars:]

    union_start = min(trend_window[0], volatility_window[0], drawdown_window[0])
    closes_by_day = _load_index_closes(
        store,
        symbol=symbol,
        as_of=as_of,
        start=union_start,
        required_days=_unique_sorted_dates(trend_window + volatility_window + drawdown_window),
    )

    trend_closes = [closes_by_day[day] for day in trend_window]
    volatility_closes = [closes_by_day[day] for day in volatility_window]
    drawdown_closes = [closes_by_day[day] for day in drawdown_window]

    latest_close = trend_closes[-1]
    if latest_close != drawdown_closes[-1] or latest_close != volatility_closes[-1]:
        raise ValueError("window terminal closes must agree on the latest trading day <= as_of")

    sma = sum(trend_closes) / len(trend_closes)
    if sma <= 0.0 or not math.isfinite(sma):
        raise ValueError("simple_moving_average must be finite and positive")
    close_to_sma = latest_close / sma

    returns = [
        volatility_closes[index] / volatility_closes[index - 1] - 1.0 for index in range(1, len(volatility_closes))
    ]
    if len(returns) != volatility_lookback_bars:
        raise ValueError("volatility return count must equal volatility_lookback_bars")
    if any(not math.isfinite(value) for value in returns):
        raise ValueError("volatility returns must be finite")
    realized_vol = _sample_std(returns) * math.sqrt(float(INDEX_RISK_ANNUALIZATION_TRADING_DAYS_PER_YEAR))

    peak = max(drawdown_closes)
    if peak <= 0.0 or not math.isfinite(peak):
        raise ValueError("rolling_peak must be finite and positive")
    drawdown = latest_close / peak - 1.0

    report = IndexRiskFeatureReport(
        data_snapshot_id=snapshot_id,
        index_symbol=symbol,
        as_of=as_of,
        trend_lookback_bars=trend_lookback_bars,
        volatility_lookback_bars=volatility_lookback_bars,
        drawdown_lookback_bars=drawdown_lookback_bars,
        trend_window_dates=list(trend_window),
        volatility_price_window_dates=list(volatility_window),
        drawdown_window_dates=list(drawdown_window),
        observation_count_trend=len(trend_closes),
        observation_count_volatility_returns=len(returns),
        observation_count_drawdown=len(drawdown_closes),
        latest_close=latest_close,
        simple_moving_average=sma,
        close_to_sma_ratio=close_to_sma,
        realized_volatility_annualized=realized_vol,
        rolling_peak=peak,
        drawdown=drawdown,
    )
    return seal_index_risk_feature_report(report)


def canonical_report_payload(report: IndexRiskFeatureReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: IndexRiskFeatureReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: IndexRiskFeatureReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_index_risk_feature_report(report: IndexRiskFeatureReport) -> IndexRiskFeatureReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: IndexRiskFeatureReport) -> None:
    if report.report_id is None:
        raise ValueError("index risk feature report_id is missing")
    expected = compute_report_id(report)
    if report.report_id != expected:
        raise ValueError("index risk feature report_id does not match canonical content hash")


def load_index_risk_feature_report(path: Path) -> IndexRiskFeatureReport:
    try:
        return IndexRiskFeatureReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("index risk feature report is missing or invalid") from exc


def assert_report_date_window_structure(report: IndexRiskFeatureReport) -> None:
    """Fail closed on future / unsorted / duplicate / mismatched terminal windows.

    Terminal trading dates must agree across windows. Terminal need not equal
    ``as_of`` because ``as_of`` may be a non-trading calendar day.
    """
    windows: tuple[tuple[str, Sequence[date]], ...] = (
        ("trend_window_dates", report.trend_window_dates),
        ("volatility_price_window_dates", report.volatility_price_window_dates),
        ("drawdown_window_dates", report.drawdown_window_dates),
    )
    terminals: list[date] = []
    for name, values in windows:
        _assert_strictly_increasing_unique_dates(values, as_of=report.as_of, context=name)
        if not values:
            raise ValueError(f"{name} must be non-empty")
        terminals.append(values[-1])
    if len(set(terminals)) != 1:
        raise ValueError("date windows must share a common terminal trading date")


def verify_index_risk_feature_report_file(path: Path) -> IndexRiskFeatureReport:
    report = load_index_risk_feature_report(path)
    assert_report_self_hash(report)
    assert_report_date_window_structure(report)
    if report.ready_for_scoring or report.ready_for_backtest or report.ready_for_trading or report.auto_apply:
        raise ValueError("index risk feature report cannot authorize scoring, backtest, trading, or auto-apply")
    return report


def write_index_risk_feature_report(report: IndexRiskFeatureReport, output: Path) -> None:
    sealed = seal_index_risk_feature_report(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def report_uses_only_as_of_or_earlier(report: IndexRiskFeatureReport, *, as_of: date) -> None:
    """Fail closed if a sealed report somehow references a post-as_of day."""
    if report.as_of > as_of:
        raise ValueError("report.as_of is after the diagnosis as_of")
    assert_report_date_window_structure(report)


def _require_symbol(value: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError("index_symbol must be a non-empty string")
    return value


def _require_positive_int(value: int, *, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be an integer >= 1")


def _require_lookback_at_least(value: int, *, minimum: int, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")


def _unique_sorted_dates(values: Sequence[date]) -> list[date]:
    return sorted(set(values))


def _assert_strictly_increasing_unique_dates(
    values: Sequence[date | Any],
    *,
    as_of: date,
    context: str,
) -> list[date]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError(f"{context} must be a sequence of dates")
    cleaned: list[date] = []
    for value in values:
        # Reject datetime subclasses: require exact date instances only.
        if type(value) is not date:
            raise ValueError(f"{context} contains a non-date value")
        if value > as_of:
            raise ValueError(f"{context} contains date {value.isoformat()} after as_of {as_of.isoformat()}")
        if cleaned and value <= cleaned[-1]:
            raise ValueError(f"{context} must be strictly increasing with unique dates")
        cleaned.append(value)
    return cleaned


def _validate_market_calendar(values: Sequence[date | Any], *, as_of: date) -> list[date]:
    return _assert_strictly_increasing_unique_dates(values, as_of=as_of, context="calendar")


def _sample_std(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        raise ValueError("sample standard deviation requires at least two returns")
    mean = sum(values) / n
    variance = sum((value - mean) ** 2 for value in values) / (n - 1)
    if variance < 0.0 or not math.isfinite(variance):
        raise ValueError("return variance must be finite and non-negative")
    return math.sqrt(variance)


def _load_index_closes(
    store: MarketStore | IndexBarSource,
    *,
    symbol: str,
    as_of: date,
    start: date,
    required_days: Sequence[date],
) -> dict[date, float]:
    frame = store.get_index_bars(as_of=as_of, symbol=symbol, start=start)
    if "close" not in frame.columns or "date" not in frame.columns:
        raise ValueError("index bars must include date and close columns")
    rows = frame.select(["date", "close"]).to_dicts()
    dates = [row["date"] for row in rows]
    _reject_future_dates(dates, as_of=as_of, context=f"index_bars[{symbol}]")

    required_set = set(required_days)
    by_day: dict[date, float] = {}
    for row in rows:
        day = row["date"]
        if not isinstance(day, date):
            raise ValueError(f"index_bars[{symbol}] contains a non-date value")
        if day not in required_set:
            continue
        raw = row["close"]
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise ValueError(f"index_bars[{symbol}] has non-numeric close on {day.isoformat()}")
        price = float(raw)
        if not math.isfinite(price) or price <= 0.0:
            raise ValueError(f"index_bars[{symbol}] has non-finite or non-positive close on {day.isoformat()}")
        if day in by_day:
            raise ValueError(f"index_bars[{symbol}] has duplicate date {day.isoformat()}")
        by_day[day] = price

    missing = [day for day in required_days if day not in by_day]
    if missing:
        raise ValueError(
            f"index_bars[{symbol}] missing required trading days: " + ",".join(day.isoformat() for day in missing[:5])
        )
    return by_day


def _reject_future_dates(values: Sequence[date | Any], *, as_of: date, context: str) -> None:
    for value in values:
        if isinstance(value, date) and value > as_of:
            raise ValueError(f"{context} contains date {value.isoformat()} after as_of {as_of.isoformat()}")


__all__ = [
    "INDEX_RISK_ANNUALIZATION_CONVENTION",
    "INDEX_RISK_ANNUALIZATION_TRADING_DAYS_PER_YEAR",
    "INDEX_RISK_FEATURE_DIAGNOSTIC_VERSION",
    "INDEX_RISK_FEATURE_SCHEMA_VERSION",
    "INDEX_RISK_PRICE_FIELD",
    "INDEX_RISK_RETURN_DEFINITION",
    "IndexBarSource",
    "IndexRiskFeatureReport",
    "assert_report_date_window_structure",
    "assert_report_self_hash",
    "canonical_report_bytes",
    "canonical_report_payload",
    "compute_report_id",
    "diagnose_index_risk_features",
    "load_index_risk_feature_report",
    "report_uses_only_as_of_or_earlier",
    "seal_index_risk_feature_report",
    "verify_index_risk_feature_report_file",
    "write_index_risk_feature_report",
]
