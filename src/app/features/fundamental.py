from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Protocol, cast

import polars as pl

from app.models.config import FundamentalDataConfig
from app.models.features import StockFeatureVector


class FundamentalReadable(Protocol):
    def get_fundamental_reports(self, available_by: datetime) -> pl.DataFrame: ...

    def get_daily_valuation(self, available_by: datetime) -> pl.DataFrame: ...


QUALITY_METRICS = (
    ("roe", True),
    ("roic", True),
    ("grossprofit_margin", True),
    ("debt_to_assets", False),
    ("ocf_to_or", True),
)
IMPROVEMENT_METRICS = (
    ("q_sales_yoy", True),
    ("q_netprofit_yoy", True),
    ("dt_netprofit_yoy", True),
)
VALUE_METRICS = (("pe_ttm", False), ("pb", False), ("ps_ttm", False))


def enrich_fundamental_features(
    vectors: list[StockFeatureVector],
    *,
    store: object,
    as_of: date,
    available_by: datetime,
    config: FundamentalDataConfig,
) -> list[StockFeatureVector]:
    """Attach cross-sectional PIT ranks and drop incomplete rows fail-closed."""
    if not hasattr(store, "get_fundamental_reports") or not hasattr(store, "get_daily_valuation"):
        if config.required:
            raise ValueError("strategy requires a verified fundamental overlay")
        return vectors
    readable = cast(FundamentalReadable, store)
    reports = _latest_reports(readable.get_fundamental_reports(available_by), as_of, config)
    valuation = _latest_valuation(readable.get_daily_valuation(available_by), as_of, config)
    symbols = {item.symbol for item in vectors}
    report_rows = {
        str(row["symbol"]): row
        for row in reports.filter(pl.col("symbol").is_in(list(symbols))).to_dicts()
    }
    valuation_rows = {
        str(row["symbol"]): row
        for row in valuation.filter(pl.col("symbol").is_in(list(symbols))).to_dicts()
    }
    combined: dict[str, dict[str, object]] = {}
    for symbol in symbols:
        report = report_rows.get(symbol)
        value = valuation_rows.get(symbol)
        if report is None or value is None:
            continue
        combined[symbol] = {**report, **value}

    quality = _component_scores(combined, QUALITY_METRICS, config.min_quality_components)
    improvement = _component_scores(
        combined,
        IMPROVEMENT_METRICS,
        config.min_improvement_components,
    )
    value = _component_scores(combined, VALUE_METRICS, config.min_value_components, positive_only=True)
    out: list[StockFeatureVector] = []
    for vector in vectors:
        symbol = vector.symbol
        if symbol not in quality or symbol not in improvement or symbol not in value:
            continue
        report = report_rows[symbol]
        valuation_row = valuation_rows[symbol]
        report_period = report["report_period"]
        valuation_date = valuation_row["date"]
        if not isinstance(report_period, date) or not isinstance(valuation_date, date):
            continue
        extra = dict(vector.extra)
        extra.update(
            {
                "quality_score": quality[symbol],
                "improvement_score": improvement[symbol],
                "value_score": value[symbol],
                "report_age_days": float((as_of - report_period).days),
                "valuation_age_days": float((as_of - valuation_date).days),
            }
        )
        out.append(vector.model_copy(update={"extra": extra}))
    return out


def _latest_reports(
    frame: pl.DataFrame,
    as_of: date,
    config: FundamentalDataConfig,
) -> pl.DataFrame:
    if frame.is_empty():
        return frame
    eligible = frame.filter(
        (pl.col("report_period") <= as_of)
        & ((pl.lit(as_of) - pl.col("report_period")).dt.total_days() <= config.max_report_age_days)
    )
    if config.revision_policy not in {
        "initial_as_announced",
        "strict_initial_as_announced",
    }:
        raise ValueError(f"unsupported fundamental revision policy: {config.revision_policy}")
    if config.revision_policy == "strict_initial_as_announced":
        # Tushare does not expose the publication timestamp of a revision.
        # A lone update_flag=1 row can therefore contain information that was
        # not known on ann_date.  Full-market historical research excludes it.
        eligible = eligible.filter(pl.col("update_flag") == "0")
    # Tushare can return update_flag=0 (initial record) and update_flag=1
    # (current revision) with the same ann_date, but no revision publication
    # timestamp. The legacy policy prefers the initial record when both are
    # present; the strict policy above also rejects revision-only groups.
    initial_per_announcement = (
        eligible.with_columns(
            pl.when(pl.col("update_flag") == "0")
            .then(1)
            .otherwise(0)
            .alias("_initial_priority")
        )
        .sort(
            [
                "symbol",
                "report_period",
                "ann_date",
                "_initial_priority",
                "source_row_hash",
            ]
        )
        .unique(subset=["symbol", "report_period", "ann_date"], keep="last")
        .drop("_initial_priority")
    )
    return (
        initial_per_announcement.sort(
            ["symbol", "report_period", "available_at", "source_row_hash"]
        ).unique(subset=["symbol"], keep="last")
    )


def _latest_valuation(
    frame: pl.DataFrame,
    as_of: date,
    config: FundamentalDataConfig,
) -> pl.DataFrame:
    if frame.is_empty():
        return frame
    return (
        frame.filter(
            (pl.col("date") <= as_of)
            & ((pl.lit(as_of) - pl.col("date")).dt.total_days() <= config.max_valuation_age_days)
        )
        .sort(["symbol", "date", "source_row_hash"])
        .unique(subset=["symbol"], keep="last")
    )


def _component_scores(
    rows: dict[str, dict[str, object]],
    metrics: tuple[tuple[str, bool], ...],
    min_components: int,
    *,
    positive_only: bool = False,
) -> dict[str, float]:
    ranks: dict[str, list[float]] = {symbol: [] for symbol in rows}
    for metric, high_is_good in metrics:
        observed = [
            (symbol, float(cast(Any, row[metric])))
            for symbol, row in rows.items()
            if _finite(row.get(metric))
            and (not positive_only or float(cast(Any, row[metric])) > 0)
        ]
        if not observed:
            continue
        values = [value for _, value in observed]
        for symbol, value in observed:
            percentile = _percentile(value, values)
            ranks[symbol].append(percentile if high_is_good else 100.0 - percentile)
    return {
        symbol: sum(values) / len(values)
        for symbol, values in ranks.items()
        if len(values) >= min_components
    }


def _percentile(value: float, values: list[float]) -> float:
    if len(values) <= 1:
        return 50.0
    below = sum(item < value for item in values)
    equal = sum(item == value for item in values)
    average_rank_zero_based = below + (equal - 1) / 2.0
    return average_rank_zero_based / (len(values) - 1) * 100.0


def _finite(value: object) -> bool:
    return isinstance(value, int | float) and math.isfinite(float(value))
