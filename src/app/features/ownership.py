from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol, cast

import polars as pl

from app.models.config import OwnershipDataConfig
from app.models.features import StockFeatureVector


class OwnershipStore(Protocol):
    def get_top10_float_holders(self, available_by: datetime) -> pl.DataFrame: ...


def enrich_ownership_features(
    vectors: list[StockFeatureVector],
    *,
    store: object,
    as_of: date,
    available_by: datetime,
    config: OwnershipDataConfig,
) -> list[StockFeatureVector]:
    """Attach a PIT sponsorship percentile; missing observations remain unknown."""
    if not vectors:
        return []
    if not hasattr(store, "get_top10_float_holders"):
        if config.required:
            raise ValueError("strategy requires a verified ownership overlay")
        return vectors
    readable = cast(OwnershipStore, store)
    holders = readable.get_top10_float_holders(available_by)
    latest = _latest_complete_groups(
        holders,
        as_of=as_of,
        max_age_days=config.max_report_age_days,
        min_complete_holders=config.min_complete_holders,
    )
    personal = {value.strip() for value in config.personal_holder_types}
    ratios: dict[str, float] = {}
    ages: dict[str, float] = {}
    for symbol, group in latest.items():
        ratio = sum(
            float(row["hold_float_ratio"])
            for row in group.iter_rows(named=True)
            if str(row["holder_type"]).strip() not in personal
        )
        ratios[symbol] = ratio
        report_period = group["report_period"][0]
        if isinstance(report_period, date):
            ages[symbol] = float((as_of - report_period).days)

    requested = {vector.symbol for vector in vectors}
    known = requested & ratios.keys()
    coverage = len(known) / len(requested)
    if config.required and coverage < config.min_cross_section_coverage:
        raise ValueError(
            "ownership proxy coverage is insufficient: "
            f"known={len(known)} requested={len(requested)} coverage={coverage:.4f} "
            f"required={config.min_cross_section_coverage:.4f}; "
            "missing ownership cannot be treated as zero"
        )
    scores = _percentile_scores({symbol: ratios[symbol] for symbol in known})
    out: list[StockFeatureVector] = []
    for vector in vectors:
        symbol = vector.symbol
        extra = {
            **vector.extra,
            "ownership_cross_section_coverage": coverage,
            "ownership_proxy_known": 1.0 if symbol in scores else 0.0,
        }
        if symbol in scores and symbol in ages:
            extra.update(
                {
                    "institutional_score": scores[symbol],
                    "institutional_proxy_ratio": ratios[symbol],
                    "ownership_age_days": ages[symbol],
                }
            )
        elif config.required:
            continue
        out.append(vector.model_copy(update={"extra": extra}))
    return out


def _latest_complete_groups(
    frame: pl.DataFrame,
    *,
    as_of: date,
    max_age_days: int,
    min_complete_holders: int,
) -> dict[str, pl.DataFrame]:
    required = {
        "symbol",
        "report_period",
        "ann_date",
        "available_at",
        "holder_name",
        "holder_type",
        "hold_float_ratio",
    }
    if frame.is_empty() or not required.issubset(frame.columns):
        return {}
    eligible = frame.filter(
        (pl.col("report_period") <= as_of)
        & (pl.col("report_period") >= as_of - timedelta(days=max_age_days))
    )
    if eligible.is_empty():
        return {}
    grouped = eligible.partition_by(
        ["symbol", "report_period", "ann_date"],
        as_dict=True,
        include_key=True,
        maintain_order=True,
    )
    candidates: dict[str, list[tuple[date, date, pl.DataFrame]]] = {}
    for key, group in grouped.items():
        symbol, report_period, ann_date = key
        if (
            not isinstance(symbol, str)
            or not isinstance(report_period, date)
            or not isinstance(ann_date, date)
        ):
            continue
        candidates.setdefault(symbol, []).append((report_period, ann_date, group))
    out: dict[str, pl.DataFrame] = {}
    for symbol, groups in candidates.items():
        latest = max(groups, key=lambda item: (item[0], item[1]))[2]
        distinct = latest["holder_name"].n_unique()
        has_unknown_ratio = latest["hold_float_ratio"].null_count() > 0
        has_invalid_ratio = latest.select(
            (
                (pl.col("hold_float_ratio") < 0)
                | (pl.col("hold_float_ratio") > 100)
            ).any()
        ).item()
        total = float(latest["hold_float_ratio"].sum()) if not has_unknown_ratio else 0.0
        has_unknown_type = latest.select(
            (
                pl.col("holder_type").is_null()
                | (pl.col("holder_type").str.strip_chars() == "")
            ).any()
        ).item()
        if (
            latest.height != min_complete_holders
            or distinct != min_complete_holders
            or total > 100.000001
            or has_invalid_ratio is True
            or has_unknown_type is True
            or has_unknown_ratio
        ):
            continue
        out[symbol] = latest
    return out


def _percentile_scores(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    if len(ordered) == 1:
        return {ordered[0][0]: 50.0}
    positions: dict[float, list[int]] = {}
    for index, (_, value) in enumerate(ordered):
        positions.setdefault(value, []).append(index)
    score_by_value = {
        value: 100.0 * (sum(indices) / len(indices)) / (len(ordered) - 1)
        for value, indices in positions.items()
    }
    return {symbol: score_by_value[value] for symbol, value in values.items()}
