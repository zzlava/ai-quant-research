"""Frozen layer-two alpha v2 development diagnostic.

The diagnostic evaluates the four pre-registered factor families on the full
eligible cross-section.  Selection uses 2022-2023 only.  Calendar year 2024 is
seen robustness report-only and can never change the selected set or weights.
No row from 2025 onward is read.

This module produces research evidence, not scores, a backtest, a portfolio,
orders, or trading instructions.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_alpha_diagnostic_engine import (
    FROZEN_FACTOR_FAMILY_IDS,
    holm_step_down_four_factors,
    newey_west_bartlett_inference,
    paired_spearman,
    quintile_top_minus_bottom_spread,
)
from app.research.layer_two_alpha_input_bundle_v2 import (
    DEFAULT_OUTPUT_PATH as DEFAULT_INPUT_BUNDLE_PATH,
)
from app.research.layer_two_alpha_input_bundle_v2 import verify_input_bundle

SCHEMA_VERSION: Literal["1"] = "1"
DIAGNOSTIC_VERSION: Literal["layer-two-alpha-diagnostic-v2"] = (
    "layer-two-alpha-diagnostic-v2"
)
DEFAULT_OUTPUT_DIR = Path(
    "data/all-a-share-historical-v1/research/layer-two-alpha-diagnostic-v2"
)
DAILY_FILE = "daily_metrics.parquet"
SUMMARY_FILE = "factor_summary.parquet"
SIZE_FILE = "size_band_summary.parquet"
REPORT_FILE = "report.json"

MARKET_DIR = Path("data/all-a-share-historical-v1/parquet")
CANDIDATE_FILE = Path(
    "data/all-a-share-historical-v1/research/candidate-eligibility-pack-v1/"
    "eligibility_verdicts.parquet"
)
FUNDAMENTAL_DIR = Path("data/all-a-share-historical-v1/fundamentals-value-quality-v1")
FINANCIAL_REVIEW_FILE = Path(
    "data/all-a-share-historical-v1/research/financial-negative-list-verdict-overlay-v1/"
    "coverage_pit_review.json"
)
CLUSTER_FILE = Path(
    "data/all-a-share-historical-v1/research/layer-two-statistical-cluster-pack-v2/"
    "cluster_assignments.parquet"
)
PROTOCOL_PATH = Path("config/research/layer-two-alpha-development-protocol-v2.json")
REGISTRATION_PATH = Path("config/research/layer-two-alpha-trial-registration-v2.json")

DEVELOPMENT_START = date(2022, 1, 1)
DEVELOPMENT_END = date(2023, 12, 31)
ROBUSTNESS_START = date(2024, 1, 1)
ROBUSTNESS_END = date(2024, 12, 31)
HORIZONS: tuple[int, ...] = (5, 20, 40)
PRIMARY_HORIZON = 40
PRIMARY_HAC_LAG = 39
MIN_KNOWN = 500
MIN_KNOWN_FRACTION = 0.60
MIN_PRIMARY_DATES = 120
MIN_PRIMARY_DATES_PER_YEAR = 40
MIN_SIZE_BAND_DATES = 40
SIZE_BANDS: tuple[str, ...] = ("3bn_5bn", "5bn_10bn", "10bn_plus")
FACTOR_RAW_COLUMNS: Mapping[str, str] = {
    "quality": "quality_raw",
    "value": "value_raw",
    "medium_momentum_12_1": "medium_momentum_12_1_raw",
    "defensive_low_vol": "defensive_low_vol_raw",
}

DAILY_COLUMNS: tuple[str, ...] = (
    "window",
    "date",
    "year",
    "factor_id",
    "horizon_days",
    "companion",
    "eligible_count",
    "factor_known_count",
    "factor_known_fraction",
    "coverage_pass",
    "labeled_count",
    "ic",
    "top_minus_bottom_spread",
    "top_quintile_mean_return",
)

SUMMARY_COLUMNS: tuple[str, ...] = (
    "window",
    "factor_id",
    "horizon_days",
    "companion",
    "valid_ic_dates",
    "valid_spread_dates",
    "pooled_mean_ic",
    "pooled_mean_spread",
    "pooled_mean_top_quintile_return",
    "hac_lag",
    "hac_statistic",
    "positive_hac_p_value",
)

SIZE_COLUMNS: tuple[str, ...] = (
    "factor_id",
    "size_band",
    "valid_ic_dates",
    "valid_spread_dates",
    "pooled_mean_ic",
    "pooled_mean_spread",
    "negative_hac_p_value",
    "positive_band",
    "significantly_negative",
)


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _hex64(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError(f"{field_name} must be a 64-character lowercase SHA-256")
    return value


class DiagnosticSourceBinding(_StrictFrozen):
    input_bundle_path: str
    input_bundle_id: str
    input_bundle_file_sha256: str
    protocol_path: str
    protocol_id: str
    registration_path: str
    registration_id: str
    diagnostic_module_sha256: str
    kernel_module_sha256: str

    @field_validator(
        "input_bundle_id",
        "input_bundle_file_sha256",
        "protocol_id",
        "registration_id",
        "diagnostic_module_sha256",
        "kernel_module_sha256",
        mode="before",
    )
    @classmethod
    def _hashes(cls, value: object, info: Any) -> str:
        return _hex64(value, field_name=str(info.field_name))


class DiagnosticReadiness(_StrictFrozen):
    research_only: Literal[True]
    development_selection_complete: bool
    seen_2024_report_only_complete: bool
    auto_apply: Literal[False]
    ready_for_scoring: Literal[False]
    ready_for_backtest: Literal[False]
    ready_for_portfolio_construction: Literal[False]
    ready_for_orders: Literal[False]
    ready_for_trading: Literal[False]
    new_oos_authorized: Literal[False]


class LayerTwoAlphaDiagnosticReportV2(_StrictFrozen):
    schema_version: Literal["1"]
    diagnostic_version: Literal["layer-two-alpha-diagnostic-v2"]
    status: Literal["development_diagnostic_complete_new_oos_not_authorized"]
    source_binding: DiagnosticSourceBinding
    development_window: Literal["2022-01-01..2023-12-31"]
    robustness_2024_window: Literal["2024-01-01..2024-12-31"]
    consumed_oos_forbidden: Literal["2025-01-01..2026-08-21"]
    new_frozen_oos_unauthorized_from: Literal["2026-08-22"]
    factors: tuple[str, str, str, str]
    horizons_market_days: tuple[int, int, int]
    primary_horizon_market_days: Literal[40]
    primary_hac_lag: Literal[39]
    alpha_evidence_denominator: Literal[
        "candidate_complete_and_eligible_for_new_entry_and_factor_known"
    ]
    financial_overlay_role: Literal[
        "independent_fail_closed_new_entry_safety_overlay_not_ic_denominator"
    ]
    financial_safety_coverage: dict[str, Any]
    factor_decisions: list[dict[str, Any]]
    holm_results: list[dict[str, Any]]
    selected_factor_ids: list[str]
    frozen_equal_weights: dict[str, float] | None
    robustness_2024: dict[str, Any]
    daily_metrics_file: Literal["daily_metrics.parquet"]
    daily_metrics_sha256: str | None = None
    daily_metrics_rows: int = Field(ge=0)
    factor_summary_file: Literal["factor_summary.parquet"]
    factor_summary_sha256: str | None = None
    factor_summary_rows: int = Field(ge=0)
    size_band_summary_file: Literal["size_band_summary.parquet"]
    size_band_summary_sha256: str | None = None
    size_band_summary_rows: int = Field(ge=0)
    readiness: DiagnosticReadiness
    report_id: str | None = None

    @field_validator(
        "daily_metrics_sha256",
        "factor_summary_sha256",
        "size_band_summary_sha256",
        "report_id",
        mode="before",
    )
    @classmethod
    def _optional_hashes(cls, value: object, info: Any) -> str | None:
        return None if value is None else _hex64(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _boundaries(self) -> LayerTwoAlphaDiagnosticReportV2:
        if tuple(self.factors) != FROZEN_FACTOR_FAMILY_IDS:
            raise ValueError("factor order must remain the four frozen families")
        if self.horizons_market_days != HORIZONS:
            raise ValueError("horizons must remain exactly 5, 20, 40")
        if any(factor not in self.factors for factor in self.selected_factor_ids):
            raise ValueError("selected_factor_ids contains an unregistered factor")
        if self.frozen_equal_weights is None:
            if self.selected_factor_ids:
                raise ValueError("selected factors require frozen equal weights")
        else:
            if set(self.frozen_equal_weights) != set(self.selected_factor_ids):
                raise ValueError("frozen weights must cover exactly selected factors")
            if not math.isclose(sum(self.frozen_equal_weights.values()), 1.0, abs_tol=1e-12):
                raise ValueError("frozen equal weights must sum to one")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"missing or invalid JSON source: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON source must be an object: {path}")
    return payload


def _report_id(report: LayerTwoAlphaDiagnosticReportV2) -> str:
    payload = report.model_dump(mode="json", exclude={"report_id"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _finite_mean(values: Iterable[object]) -> float | None:
    clean = [float(value) for value in values if isinstance(value, int | float) and math.isfinite(float(value))]
    return None if not clean else sum(clean) / len(clean)


def _rank_percentile_expr(column: str, *, groups: list[str]) -> pl.Expr:
    count = pl.col(column).count().over(groups)
    rank = pl.col(column).rank(method="average").over(groups)
    return (
        pl.when(count > 1)
        .then((rank - 1.0) / (count - 1.0) * 100.0)
        .otherwise(None)
    )


def _clean_numeric(column: str) -> pl.Expr:
    return pl.when(pl.col(column).is_finite()).then(pl.col(column)).otherwise(None)


def _prepare_candidates(root: Path) -> pl.DataFrame:
    frame = (
        pl.scan_parquet(root / CANDIDATE_FILE)
        .filter(pl.col("eligible_for_new_entry") & ~pl.col("unknown_critical_input"))
        .select(
            "symbol",
            pl.col("as_of").str.to_date().alias("as_of_date"),
            "pit_free_float_market_cap_cny",
        )
        .collect()
        .unique(subset=["symbol", "as_of_date"], keep="first")
        .sort(["as_of_date", "symbol"])
    )
    if frame.is_empty():
        raise ValueError("candidate eligibility frame is empty or outside the sealed window")
    min_date = frame["as_of_date"].min()
    max_date = frame["as_of_date"].max()
    if not isinstance(min_date, date) or min_date < DEVELOPMENT_START:
        raise ValueError("candidate eligibility frame is empty or outside the sealed window")
    if not isinstance(max_date, date) or max_date > ROBUSTNESS_END:
        raise ValueError("candidate eligibility frame reads beyond 2024-12-31")
    return frame.with_columns(
        (
            pl.col("as_of_date").cast(pl.Datetime("us"))
            + pl.duration(hours=17, minutes=30)
        ).alias("decision_at")
    )


def _prepare_market_factors(root: Path) -> pl.DataFrame:
    calendar = (
        pl.read_parquet(root / MARKET_DIR / "calendar.parquet")
        .select("date")
        .filter(pl.col("date") <= ROBUSTNESS_END)
        .unique()
        .sort("date")
        .with_row_index("market_index")
    )
    bars = (
        pl.scan_parquet(root / MARKET_DIR / "daily_bars.parquet")
        .filter(pl.col("date") <= ROBUSTNESS_END)
        .select("symbol", "date", "adj_close")
        .collect()
        .join(calendar, on="date", how="inner")
        .sort(["symbol", "market_index"])
    )
    if bars.select(pl.struct(["symbol", "date"]).is_duplicated().any()).item():
        raise ValueError("daily bars contain duplicate symbol-date keys")
    bars = bars.with_columns(
        pl.col("adj_close").shift(1).over("symbol").alias("_close_lag1"),
        pl.col("adj_close").shift(21).over("symbol").alias("_close_lag21"),
        pl.col("adj_close").shift(60).over("symbol").alias("_close_lag60"),
        pl.col("adj_close").shift(242).over("symbol").alias("_close_lag242"),
        pl.col("market_index").shift(1).over("symbol").alias("_idx_lag1"),
        pl.col("market_index").shift(60).over("symbol").alias("_idx_lag60"),
        pl.col("market_index").shift(242).over("symbol").alias("_idx_lag242"),
    ).with_columns(
        pl.when(
            (pl.col("market_index") - pl.col("_idx_lag1") == 1)
            & (pl.col("adj_close") > 0)
            & (pl.col("_close_lag1") > 0)
        )
        .then(pl.col("adj_close") / pl.col("_close_lag1") - 1.0)
        .otherwise(None)
        .alias("_ret1")
    )
    bars = bars.with_columns(
        pl.col("_ret1")
        .rolling_std(window_size=60, min_samples=60, ddof=1)
        .over("symbol")
        .alias("_vol60")
    ).with_columns(
        pl.when(
            (pl.col("market_index") - pl.col("_idx_lag242") == 242)
            & (pl.col("_close_lag242") > 0)
            & (pl.col("_close_lag21") > 0)
        )
        .then(pl.col("_close_lag21") / pl.col("_close_lag242") - 1.0)
        .otherwise(None)
        .alias("medium_momentum_12_1_raw"),
        pl.when(
            (pl.col("market_index") - pl.col("_idx_lag60") == 60)
            & pl.col("_vol60").is_finite()
        )
        .then(-pl.col("_vol60") * math.sqrt(242.0))
        .otherwise(None)
        .alias("defensive_low_vol_raw"),
    )
    return bars.select(
        "symbol",
        pl.col("date").alias("as_of_date"),
        pl.col("adj_close").alias("adj_close_t"),
        "medium_momentum_12_1_raw",
        "defensive_low_vol_raw",
    )


def _deduplicate_strict_initial_reports(reports: pl.DataFrame) -> pl.DataFrame:
    """Return one deterministic latest report period per symbol/availability key."""
    return (
        reports.sort(
            ["symbol", "available_at", "report_period", "source_row_hash"]
        )
        .unique(subset=["symbol", "report_period", "available_at"], keep="last")
        .sort(["symbol", "available_at", "report_period", "source_row_hash"])
        .unique(subset=["symbol", "available_at"], keep="last")
        .sort(["available_at", "symbol"])
    )


def _join_quality(root: Path, candidates: pl.DataFrame) -> pl.DataFrame:
    raw_reports = (
        pl.scan_parquet(root / FUNDAMENTAL_DIR / "fundamental_reports.parquet")
        .filter(pl.col("update_flag") == "0")
        .select(
            "symbol",
            "report_period",
            "available_at",
            "source_row_hash",
            "roe",
            "roic",
            "grossprofit_margin",
            "debt_to_assets",
            "ocf_to_or",
        )
        .collect()
    )
    reports = _deduplicate_strict_initial_reports(raw_reports)
    joined = candidates.sort(["decision_at", "symbol"]).join_asof(
        reports,
        left_on="decision_at",
        right_on="available_at",
        by="symbol",
        strategy="backward",
        check_sortedness=False,
    )
    valid = (
        pl.col("report_period").is_not_null()
        & (pl.col("report_period") <= pl.col("as_of_date"))
        & ((pl.col("as_of_date") - pl.col("report_period")).dt.total_days() <= 550)
    )
    metrics = ("roe", "roic", "grossprofit_margin", "debt_to_assets", "ocf_to_or")
    joined = joined.with_columns(
        [pl.when(valid).then(_clean_numeric(metric)).otherwise(None).alias(metric) for metric in metrics]
    )
    joined = joined.with_columns(
        _rank_percentile_expr("roe", groups=["as_of_date"]).alias("_q_roe"),
        _rank_percentile_expr("roic", groups=["as_of_date"]).alias("_q_roic"),
        _rank_percentile_expr("grossprofit_margin", groups=["as_of_date"]).alias("_q_margin"),
        (100.0 - _rank_percentile_expr("debt_to_assets", groups=["as_of_date"])).alias("_q_debt"),
        _rank_percentile_expr("ocf_to_or", groups=["as_of_date"]).alias("_q_ocf"),
    )
    qcols = ["_q_roe", "_q_roic", "_q_margin", "_q_debt", "_q_ocf"]
    joined = joined.with_columns(
        pl.sum_horizontal([pl.col(name).is_not_null().cast(pl.Int8) for name in qcols]).alias("_q_n")
    ).with_columns(
        pl.when(pl.col("_q_n") >= 3)
        .then(pl.sum_horizontal([pl.col(name).fill_null(0.0) for name in qcols]) / pl.col("_q_n"))
        .otherwise(None)
        .alias("quality_raw")
    )
    return joined


def _join_value(root: Path, frame: pl.DataFrame) -> pl.DataFrame:
    valuation = (
        pl.scan_parquet(root / FUNDAMENTAL_DIR / "daily_valuation.parquet")
        .filter((pl.col("date") >= DEVELOPMENT_START) & (pl.col("date") <= ROBUSTNESS_END))
        .select(
            "symbol",
            pl.col("date").alias("as_of_date"),
            pl.col("available_at").alias("valuation_available_at"),
            "pe_ttm",
            "pb",
            "ps_ttm",
        )
        .collect()
    )
    joined = frame.join(valuation, on=["symbol", "as_of_date"], how="left")
    valid_time = pl.col("valuation_available_at") <= pl.col("decision_at")
    for metric in ("pe_ttm", "pb", "ps_ttm"):
        joined = joined.with_columns(
            pl.when(valid_time & pl.col(metric).is_finite() & (pl.col(metric) > 0))
            .then(pl.col(metric))
            .otherwise(None)
            .alias(metric)
        )
    joined = joined.with_columns(
        (100.0 - _rank_percentile_expr("pe_ttm", groups=["as_of_date"])).alias("_v_pe"),
        (100.0 - _rank_percentile_expr("pb", groups=["as_of_date"])).alias("_v_pb"),
        (100.0 - _rank_percentile_expr("ps_ttm", groups=["as_of_date"])).alias("_v_ps"),
    )
    vcols = ["_v_pe", "_v_pb", "_v_ps"]
    return joined.with_columns(
        pl.sum_horizontal([pl.col(name).is_not_null().cast(pl.Int8) for name in vcols]).alias("_v_n")
    ).with_columns(
        pl.when(pl.col("_v_n") >= 2)
        .then(pl.sum_horizontal([pl.col(name).fill_null(0.0) for name in vcols]) / pl.col("_v_n"))
        .otherwise(None)
        .alias("value_raw")
    )


def _add_factor_percentiles(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        _rank_percentile_expr("quality_raw", groups=["as_of_date"]).alias("quality"),
        _rank_percentile_expr("value_raw", groups=["as_of_date"]).alias("value"),
        _rank_percentile_expr("medium_momentum_12_1_raw", groups=["as_of_date"]).alias(
            "medium_momentum_12_1"
        ),
        _rank_percentile_expr("defensive_low_vol_raw", groups=["as_of_date"]).alias(
            "defensive_low_vol"
        ),
    )


def _add_cluster_companions(root: Path, frame: pl.DataFrame) -> pl.DataFrame:
    assignments = pl.read_parquet(root / CLUSTER_FILE).sort("anchor_date")
    joined = frame.sort("as_of_date").join_asof(
        assignments,
        left_on="as_of_date",
        right_on="anchor_date",
        by="symbol",
        strategy="backward",
        check_sortedness=False,
    )
    assignment_valid = (
        pl.col("valid_through").is_not_null()
        & (pl.col("as_of_date") <= pl.col("valid_through"))
        & (pl.col("status") == "assigned")
        & (pl.col("cluster_size") >= 2)
    )
    joined = joined.with_columns(
        pl.when(assignment_valid).then(pl.col("cluster_id")).otherwise(None).alias("_cluster")
    )
    for factor_id, raw_column in FACTOR_RAW_COLUMNS.items():
        base = f"_{factor_id}_cluster_base"
        joined = joined.with_columns(
            pl.when(pl.col("_cluster").is_not_null() & pl.col(raw_column).is_not_null())
            .then(pl.col(raw_column))
            .otherwise(None)
            .alias(base)
        ).with_columns(
            _rank_percentile_expr(base, groups=["as_of_date", "_cluster"]).alias(
                f"{factor_id}_cluster"
            )
        )
    return joined


def _add_labels(root: Path, frame: pl.DataFrame) -> pl.DataFrame:
    calendar = (
        pl.read_parquet(root / MARKET_DIR / "calendar.parquet")
        .select("date")
        .unique()
        .sort("date")["date"]
        .to_list()
    )
    days = [item for item in calendar if isinstance(item, date) and item <= ROBUSTNESS_END]
    index = {day: position for position, day in enumerate(days)}
    maps: dict[int, pl.DataFrame] = {}
    for horizon in HORIZONS:
        rows: list[dict[str, date]] = []
        for day in days:
            if day < DEVELOPMENT_START:
                continue
            endpoint_index = index[day] + horizon
            hard_end = DEVELOPMENT_END if day <= DEVELOPMENT_END else ROBUSTNESS_END
            if endpoint_index < len(days) and days[endpoint_index] <= hard_end:
                rows.append({"as_of_date": day, f"endpoint_{horizon}": days[endpoint_index]})
        maps[horizon] = pl.DataFrame(rows, schema={"as_of_date": pl.Date, f"endpoint_{horizon}": pl.Date})

    prices = (
        pl.scan_parquet(root / MARKET_DIR / "daily_bars.parquet")
        .filter(pl.col("date") <= ROBUSTNESS_END)
        .select("symbol", "date", "adj_close")
        .collect()
    )
    out = frame
    for horizon in HORIZONS:
        endpoint = f"endpoint_{horizon}"
        close = f"adj_close_h{horizon}"
        out = out.join(maps[horizon], on="as_of_date", how="left").join(
            prices.rename({"date": endpoint, "adj_close": close}),
            on=["symbol", endpoint],
            how="left",
        )
        out = out.with_columns(
            pl.when(
                pl.col(endpoint).is_not_null()
                & pl.col("adj_close_t").is_finite()
                & (pl.col("adj_close_t") > 0)
                & pl.col(close).is_finite()
                & (pl.col(close) > 0)
            )
            .then(pl.col(close) / pl.col("adj_close_t") - 1.0)
            .otherwise(None)
            .alias(f"forward_return_h{horizon}")
        )
    return out.with_columns(
        pl.when(pl.col("pit_free_float_market_cap_cny") < 3_000_000_000.0)
        .then(pl.lit("below_lowest"))
        .when(pl.col("pit_free_float_market_cap_cny") < 5_000_000_000.0)
        .then(pl.lit("3bn_5bn"))
        .when(pl.col("pit_free_float_market_cap_cny") < 10_000_000_000.0)
        .then(pl.lit("5bn_10bn"))
        .when(pl.col("pit_free_float_market_cap_cny").is_not_null())
        .then(pl.lit("10bn_plus"))
        .otherwise(pl.lit("unknown"))
        .alias("size_band")
    )


def _quintile_top_mean(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 5:
        return None
    factors = [pair[0] for pair in pairs]
    if max(factors) == min(factors):
        return None
    ordered = sorted(enumerate(factors), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(factors)
    position = 0
    while position < len(ordered):
        end = position + 1
        while end < len(ordered) and ordered[end][1] == ordered[position][1]:
            end += 1
        average_rank = (position + 1 + end) / 2.0
        for index, _ in ordered[position:end]:
            ranks[index] = average_rank
        position = end
    highest: list[float] = []
    count = len(pairs)
    for rank, pair in zip(ranks, pairs, strict=True):
        bucket = min(int(math.floor((rank - 1.0) / count * 5)), 4)
        if bucket == 4:
            highest.append(pair[1])
    return None if not highest else sum(highest) / len(highest)


def _metric_row(
    *,
    window: str,
    day: date,
    factor_id: str,
    horizon: int,
    companion: bool,
    factors: list[object],
    labels: list[object],
) -> dict[str, Any]:
    eligible_count = len(factors)
    known_count = sum(
        isinstance(value, int | float) and math.isfinite(float(value)) for value in factors
    )
    fraction = known_count / eligible_count if eligible_count else 0.0
    coverage_pass = known_count >= MIN_KNOWN and fraction >= MIN_KNOWN_FRACTION
    pairs = [
        (float(factor), float(label))
        for factor, label in zip(factors, labels, strict=True)
        if isinstance(factor, int | float)
        and isinstance(label, int | float)
        and math.isfinite(float(factor))
        and math.isfinite(float(label))
    ]
    ic = paired_spearman(pairs) if coverage_pass else None
    spread = quintile_top_minus_bottom_spread(pairs) if coverage_pass else None
    top = _quintile_top_mean(pairs) if coverage_pass else None
    return {
        "window": window,
        "date": day,
        "year": day.year,
        "factor_id": factor_id,
        "horizon_days": horizon,
        "companion": companion,
        "eligible_count": eligible_count,
        "factor_known_count": known_count,
        "factor_known_fraction": fraction,
        "coverage_pass": coverage_pass,
        "labeled_count": len(pairs),
        "ic": ic,
        "top_minus_bottom_spread": spread,
        "top_quintile_mean_return": top,
    }


def _build_daily_metrics(frame: pl.DataFrame) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    partitions = frame.sort(["as_of_date", "symbol"]).partition_by(
        "as_of_date", as_dict=True, maintain_order=True
    )
    for key, day_frame in partitions.items():
        day = key[0]
        if not isinstance(day, date):
            raise ValueError("candidate decision date is invalid")
        window = "development" if day <= DEVELOPMENT_END else "robustness_2024_report_only"
        for factor_id in FROZEN_FACTOR_FAMILY_IDS:
            raw_values = day_frame[factor_id].to_list()
            companion_values = day_frame[f"{factor_id}_cluster"].to_list()
            for horizon in HORIZONS:
                labels = day_frame[f"forward_return_h{horizon}"].to_list()
                rows.append(
                    _metric_row(
                        window=window,
                        day=day,
                        factor_id=factor_id,
                        horizon=horizon,
                        companion=False,
                        factors=raw_values,
                        labels=labels,
                    )
                )
                rows.append(
                    _metric_row(
                        window=window,
                        day=day,
                        factor_id=factor_id,
                        horizon=horizon,
                        companion=True,
                        factors=companion_values,
                        labels=labels,
                    )
                )
    return pl.DataFrame(rows).select(list(DAILY_COLUMNS)).sort(
        ["window", "factor_id", "horizon_days", "companion", "date"]
    )


def _build_factor_summary(daily: pl.DataFrame) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    groups = daily.partition_by(
        ["window", "factor_id", "horizon_days", "companion"],
        as_dict=True,
        maintain_order=True,
    )
    for key, frame in groups.items():
        window, factor_id, horizon, companion = key
        ic_values = [value for value in frame["ic"].to_list() if isinstance(value, int | float)]
        spread_values = [
            value
            for value in frame["top_minus_bottom_spread"].to_list()
            if isinstance(value, int | float)
        ]
        top_values = [
            value
            for value in frame["top_quintile_mean_return"].to_list()
            if isinstance(value, int | float)
        ]
        inference = newey_west_bartlett_inference(ic_values, lag=int(horizon) - 1)
        rows.append(
            {
                "window": window,
                "factor_id": factor_id,
                "horizon_days": int(horizon),
                "companion": bool(companion),
                "valid_ic_dates": len(ic_values),
                "valid_spread_dates": len(spread_values),
                "pooled_mean_ic": _finite_mean(ic_values),
                "pooled_mean_spread": _finite_mean(spread_values),
                "pooled_mean_top_quintile_return": _finite_mean(top_values),
                "hac_lag": int(horizon) - 1,
                "hac_statistic": inference.statistic,
                "positive_hac_p_value": inference.positive_p_value,
            }
        )
    return pl.DataFrame(rows).select(list(SUMMARY_COLUMNS)).sort(
        ["window", "factor_id", "horizon_days", "companion"]
    )


def _build_size_summary(frame: pl.DataFrame, daily: pl.DataFrame) -> pl.DataFrame:
    allowed_dates: dict[str, set[date]] = {}
    for factor_id in FROZEN_FACTOR_FAMILY_IDS:
        dates = (
            daily.filter(
                (pl.col("window") == "development")
                & (pl.col("factor_id") == factor_id)
                & (pl.col("horizon_days") == PRIMARY_HORIZON)
                & ~pl.col("companion")
                & pl.col("coverage_pass")
            )["date"]
            .to_list()
        )
        allowed_dates[factor_id] = {item for item in dates if isinstance(item, date)}

    rows: list[dict[str, Any]] = []
    day_frames = frame.filter(pl.col("as_of_date") <= DEVELOPMENT_END).partition_by(
        "as_of_date", as_dict=True, maintain_order=True
    )
    for factor_id in FROZEN_FACTOR_FAMILY_IDS:
        by_band_ic: dict[str, list[float]] = {band: [] for band in SIZE_BANDS}
        by_band_spread: dict[str, list[float]] = {band: [] for band in SIZE_BANDS}
        for key, day_frame in day_frames.items():
            day = key[0]
            if not isinstance(day, date) or day not in allowed_dates[factor_id]:
                continue
            for band in SIZE_BANDS:
                subset = day_frame.filter(pl.col("size_band") == band)
                factors = subset[factor_id].to_list()
                labels = subset[f"forward_return_h{PRIMARY_HORIZON}"].to_list()
                pairs = [
                    (float(factor), float(label))
                    for factor, label in zip(factors, labels, strict=True)
                    if isinstance(factor, int | float)
                    and isinstance(label, int | float)
                    and math.isfinite(float(factor))
                    and math.isfinite(float(label))
                ]
                ic = paired_spearman(pairs)
                spread = quintile_top_minus_bottom_spread(pairs)
                if ic is not None:
                    by_band_ic[band].append(ic)
                if spread is not None:
                    by_band_spread[band].append(spread)
        for band in SIZE_BANDS:
            ic_values = by_band_ic[band]
            spread_values = by_band_spread[band]
            inference = newey_west_bartlett_inference(ic_values, lag=PRIMARY_HAC_LAG)
            mean_ic = _finite_mean(ic_values)
            mean_spread = _finite_mean(spread_values)
            enough = (
                len(ic_values) >= MIN_SIZE_BAND_DATES
                and len(spread_values) >= MIN_SIZE_BAND_DATES
            )
            positive = bool(
                enough
                and mean_ic is not None
                and mean_ic > 0
                and mean_spread is not None
                and mean_spread > 0
            )
            negative = bool(
                enough
                and inference.negative_p_value is not None
                and inference.negative_p_value <= 0.05
            )
            rows.append(
                {
                    "factor_id": factor_id,
                    "size_band": band,
                    "valid_ic_dates": len(ic_values),
                    "valid_spread_dates": len(spread_values),
                    "pooled_mean_ic": mean_ic,
                    "pooled_mean_spread": mean_spread,
                    "negative_hac_p_value": inference.negative_p_value,
                    "positive_band": positive,
                    "significantly_negative": negative,
                }
            )
    return pl.DataFrame(rows).select(list(SIZE_COLUMNS)).sort(["factor_id", "size_band"])


def _summary_row(
    summary: pl.DataFrame,
    *,
    window: str,
    factor_id: str,
    horizon: int,
    companion: bool,
) -> dict[str, Any]:
    selected = summary.filter(
        (pl.col("window") == window)
        & (pl.col("factor_id") == factor_id)
        & (pl.col("horizon_days") == horizon)
        & (pl.col("companion") == companion)
    )
    if selected.height != 1:
        raise ValueError("factor summary row is missing or duplicated")
    return selected.row(0, named=True)


def _year_direction(daily: pl.DataFrame, *, factor_id: str, year: int) -> dict[str, Any]:
    selected = daily.filter(
        (pl.col("window") == "development")
        & (pl.col("factor_id") == factor_id)
        & (pl.col("horizon_days") == PRIMARY_HORIZON)
        & ~pl.col("companion")
        & (pl.col("year") == year)
    )
    ic = _finite_mean(selected["ic"].to_list())
    spread = _finite_mean(selected["top_minus_bottom_spread"].to_list())
    valid = selected.filter(pl.col("ic").is_not_null() & pl.col("top_minus_bottom_spread").is_not_null()).height
    return {
        "year": year,
        "valid_primary_dates": valid,
        "mean_ic": ic,
        "mean_spread": spread,
        "direction_positive": bool(ic is not None and ic > 0 and spread is not None and spread > 0),
    }


def _factor_decisions(
    daily: pl.DataFrame,
    summary: pl.DataFrame,
    size_summary: pl.DataFrame,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], dict[str, float] | None]:
    primary = {
        factor_id: _summary_row(
            summary,
            window="development",
            factor_id=factor_id,
            horizon=PRIMARY_HORIZON,
            companion=False,
        )
        for factor_id in FROZEN_FACTOR_FAMILY_IDS
    }
    raw_p = {factor_id: primary[factor_id]["positive_hac_p_value"] for factor_id in FROZEN_FACTOR_FAMILY_IDS}
    holm = holm_step_down_four_factors(raw_p)
    holm_by_factor: dict[str, Any] = {str(item.factor_id): item for item in holm.results}
    decisions: list[dict[str, Any]] = []
    selected: list[str] = []
    for factor_id in FROZEN_FACTOR_FAMILY_IDS:
        row = primary[factor_id]
        companion = _summary_row(
            summary,
            window="development",
            factor_id=factor_id,
            horizon=PRIMARY_HORIZON,
            companion=True,
        )
        year_2022 = _year_direction(daily, factor_id=factor_id, year=2022)
        year_2023 = _year_direction(daily, factor_id=factor_id, year=2023)
        band_rows = size_summary.filter(pl.col("factor_id") == factor_id)
        positive_bands = int(band_rows.filter(pl.col("positive_band")).height)
        negative_bands = int(band_rows.filter(pl.col("significantly_negative")).height)
        pooled_dates_pass = row["valid_ic_dates"] >= MIN_PRIMARY_DATES
        yearly_dates_pass = (
            year_2022["valid_primary_dates"] >= MIN_PRIMARY_DATES_PER_YEAR
            and year_2023["valid_primary_dates"] >= MIN_PRIMARY_DATES_PER_YEAR
        )
        holm_item = holm_by_factor.get(factor_id)
        if holm_item is None:
            raise ValueError(f"Holm result missing for {factor_id}")
        gates = {
            "pooled_primary_dates": pooled_dates_pass,
            "per_year_primary_dates": yearly_dates_pass,
            "pooled_ic_positive": row["pooled_mean_ic"] is not None and row["pooled_mean_ic"] > 0,
            "pooled_spread_positive": row["pooled_mean_spread"] is not None and row["pooled_mean_spread"] > 0,
            "direction_positive_2022": year_2022["direction_positive"],
            "direction_positive_2023": year_2023["direction_positive"],
            "cluster_companion_ic_positive": (
                companion["pooled_mean_ic"] is not None
                and companion["pooled_mean_ic"] > 0
            ),
            "cluster_companion_spread_positive": (
                companion["pooled_mean_spread"] is not None
                and companion["pooled_mean_spread"] > 0
            ),
            "holm_rejected": holm_item.rejected,
            "at_least_two_size_bands_positive": positive_bands >= 2,
            "no_size_band_significantly_negative": negative_bands == 0,
        }
        qualifies = all(gates.values())
        if qualifies:
            selected.append(factor_id)
        decisions.append(
            {
                "factor_id": factor_id,
                "primary": row,
                "year_2022": year_2022,
                "year_2023": year_2023,
                "cluster_companion_primary": companion,
                "positive_size_band_count": positive_bands,
                "significantly_negative_size_band_count": negative_bands,
                "gates": gates,
                "qualifies": qualifies,
            }
        )
    weights = None if not selected else {factor_id: 1.0 / len(selected) for factor_id in selected}
    return decisions, [item.model_dump(mode="json") for item in holm.results], selected, weights


def _robustness_report(
    summary: pl.DataFrame,
    selected: list[str],
    weights: dict[str, float] | None,
) -> dict[str, Any]:
    factor_rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for factor_id in selected:
        row = _summary_row(
            summary,
            window="robustness_2024_report_only",
            factor_id=factor_id,
            horizon=PRIMARY_HORIZON,
            companion=False,
        )
        reversal = bool(
            row["pooled_mean_ic"] is None
            or row["pooled_mean_ic"] <= 0
            or row["pooled_mean_spread"] is None
            or row["pooled_mean_spread"] <= 0
        )
        if reversal:
            failures.append(factor_id)
        factor_rows.append({"factor_id": factor_id, "metrics": row, "direction_reversal": reversal})
    return {
        "report_only": True,
        "must_not_select_or_alter_weights": True,
        "frozen_selected_factor_ids": selected,
        "frozen_equal_weights": weights,
        "factor_results": factor_rows,
        "direction_reversal_factor_ids": failures,
        "robustness_pass": bool(selected and not failures),
        "no_retuning_performed": True,
    }


def _build_factor_frame(root: Path) -> pl.DataFrame:
    candidates = _prepare_candidates(root)
    frame = _join_quality(root, candidates)
    frame = _join_value(root, frame)
    frame = frame.join(_prepare_market_factors(root), on=["symbol", "as_of_date"], how="left")
    frame = _add_factor_percentiles(frame)
    frame = _add_cluster_companions(root, frame)
    return _add_labels(root, frame).sort(["as_of_date", "symbol"])


def build_diagnostic(
    *,
    repo_root: Path,
) -> tuple[LayerTwoAlphaDiagnosticReportV2, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    root = repo_root.resolve()
    bundle = verify_input_bundle(repo_root=root, path=DEFAULT_INPUT_BUNDLE_PATH)
    protocol = _read_json(root / PROTOCOL_PATH)
    registration = _read_json(root / REGISTRATION_PATH)
    if protocol.get("protocol_id") != "7cd295ab6dcf596aef4d117b1b7db9abab7057d34063d6659f886e324e57fe74":
        raise ValueError("v2 protocol ID drift")
    if registration.get("registration_id") != "8a5a2e03595f7df48a6da0d01317e27c9cb189aadd4ca5d4df96dfe0df36012a":
        raise ValueError("v2 registration ID drift")
    factor_frame = _build_factor_frame(root)
    daily = _build_daily_metrics(factor_frame)
    summary = _build_factor_summary(daily)
    size_summary = _build_size_summary(factor_frame, daily)
    decisions, holm, selected, weights = _factor_decisions(daily, summary, size_summary)
    financial = _read_json(root / FINANCIAL_REVIEW_FILE)
    report = LayerTwoAlphaDiagnosticReportV2(
        schema_version=SCHEMA_VERSION,
        diagnostic_version=DIAGNOSTIC_VERSION,
        status="development_diagnostic_complete_new_oos_not_authorized",
        source_binding=DiagnosticSourceBinding(
            input_bundle_path=DEFAULT_INPUT_BUNDLE_PATH.as_posix(),
            input_bundle_id=bundle.bundle_id or "",
            input_bundle_file_sha256=_sha256_file(root / DEFAULT_INPUT_BUNDLE_PATH),
            protocol_path=PROTOCOL_PATH.as_posix(),
            protocol_id=str(protocol["protocol_id"]),
            registration_path=REGISTRATION_PATH.as_posix(),
            registration_id=str(registration["registration_id"]),
            diagnostic_module_sha256=_sha256_file(Path(__file__)),
            kernel_module_sha256=_sha256_file(
                root / "src/app/research/layer_two_alpha_diagnostic_engine.py"
            ),
        ),
        development_window="2022-01-01..2023-12-31",
        robustness_2024_window="2024-01-01..2024-12-31",
        consumed_oos_forbidden="2025-01-01..2026-08-21",
        new_frozen_oos_unauthorized_from="2026-08-22",
        factors=FROZEN_FACTOR_FAMILY_IDS,
        horizons_market_days=HORIZONS,
        primary_horizon_market_days=PRIMARY_HORIZON,
        primary_hac_lag=PRIMARY_HAC_LAG,
        alpha_evidence_denominator="candidate_complete_and_eligible_for_new_entry_and_factor_known",
        financial_overlay_role="independent_fail_closed_new_entry_safety_overlay_not_ic_denominator",
        financial_safety_coverage={
            "coverage_start": financial.get("coverage_start"),
            "coverage_end": financial.get("coverage_end"),
            "decision_status_counts": financial.get("decision_status_counts"),
            "missing_stays_unknown": financial.get("missing_stays_unknown"),
            "ready_for_scoring": False,
            "not_used_in_alpha_ic_denominator": True,
        },
        factor_decisions=decisions,
        holm_results=holm,
        selected_factor_ids=selected,
        frozen_equal_weights=weights,
        robustness_2024=_robustness_report(summary, selected, weights),
        daily_metrics_file=DAILY_FILE,
        daily_metrics_rows=daily.height,
        factor_summary_file=SUMMARY_FILE,
        factor_summary_rows=summary.height,
        size_band_summary_file=SIZE_FILE,
        size_band_summary_rows=size_summary.height,
        readiness=DiagnosticReadiness(
            research_only=True,
            development_selection_complete=True,
            seen_2024_report_only_complete=True,
            auto_apply=False,
            ready_for_scoring=False,
            ready_for_backtest=False,
            ready_for_portfolio_construction=False,
            ready_for_orders=False,
            ready_for_trading=False,
            new_oos_authorized=False,
        ),
    )
    return report, daily, summary, size_summary


def write_diagnostic(
    *,
    output_dir: Path,
    report: LayerTwoAlphaDiagnosticReportV2,
    daily: pl.DataFrame,
    summary: pl.DataFrame,
    size_summary: pl.DataFrame,
    replace_existing: bool = False,
) -> LayerTwoAlphaDiagnosticReportV2:
    if tuple(daily.columns) != DAILY_COLUMNS:
        raise ValueError("daily metric columns do not match the frozen schema")
    if tuple(summary.columns) != SUMMARY_COLUMNS:
        raise ValueError("factor summary columns do not match the frozen schema")
    if tuple(size_summary.columns) != SIZE_COLUMNS:
        raise ValueError("size-band summary columns do not match the frozen schema")
    destination = output_dir
    if destination.exists() and not replace_existing:
        raise FileExistsError(f"diagnostic output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="layer-two-alpha-v2-", dir=destination.parent))
    backup = destination.parent / f".{destination.name}.backup"
    try:
        daily.write_parquet(temporary / DAILY_FILE)
        summary.write_parquet(temporary / SUMMARY_FILE)
        size_summary.write_parquet(temporary / SIZE_FILE)
        sealed = report.model_copy(
            update={
                "daily_metrics_sha256": _sha256_file(temporary / DAILY_FILE),
                "factor_summary_sha256": _sha256_file(temporary / SUMMARY_FILE),
                "size_band_summary_sha256": _sha256_file(temporary / SIZE_FILE),
            }
        )
        sealed = sealed.model_copy(update={"report_id": _report_id(sealed)})
        (temporary / REPORT_FILE).write_text(
            json.dumps(sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(destination, backup)
        os.replace(temporary, destination)
        if backup.exists():
            shutil.rmtree(backup)
        return sealed
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise


def _assert_frame_equal(actual: pl.DataFrame, expected: pl.DataFrame, *, name: str) -> None:
    if actual.schema != expected.schema or actual.shape != expected.shape or not actual.equals(expected):
        raise ValueError(f"{name} does not match full deterministic recomputation")


def verify_diagnostic(
    *,
    repo_root: Path,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    full_recomputation: bool = True,
) -> LayerTwoAlphaDiagnosticReportV2:
    root = repo_root.resolve()
    directory = output_dir if output_dir.is_absolute() else root / output_dir
    try:
        report = LayerTwoAlphaDiagnosticReportV2.model_validate_json(
            (directory / REPORT_FILE).read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError("layer-two alpha v2 report is missing or invalid") from exc
    if report.report_id is None or report.report_id != _report_id(report):
        raise ValueError("layer-two alpha v2 report self-hash mismatch")
    paths = {
        "daily": (directory / report.daily_metrics_file, report.daily_metrics_sha256),
        "summary": (directory / report.factor_summary_file, report.factor_summary_sha256),
        "size": (directory / report.size_band_summary_file, report.size_band_summary_sha256),
    }
    for name, (path, expected_hash) in paths.items():
        if expected_hash is None or not path.is_file() or _sha256_file(path) != expected_hash:
            raise ValueError(f"{name} parquet hash mismatch")
    daily = pl.read_parquet(paths["daily"][0])
    summary = pl.read_parquet(paths["summary"][0])
    size_summary = pl.read_parquet(paths["size"][0])
    if tuple(daily.columns) != DAILY_COLUMNS or daily.height != report.daily_metrics_rows:
        raise ValueError("daily metric schema or row-count mismatch")
    if tuple(summary.columns) != SUMMARY_COLUMNS or summary.height != report.factor_summary_rows:
        raise ValueError("factor summary schema or row-count mismatch")
    if tuple(size_summary.columns) != SIZE_COLUMNS or size_summary.height != report.size_band_summary_rows:
        raise ValueError("size summary schema or row-count mismatch")
    if full_recomputation:
        expected_report, expected_daily, expected_summary, expected_size = build_diagnostic(
            repo_root=root
        )
        _assert_frame_equal(daily, expected_daily, name="daily metrics")
        _assert_frame_equal(summary, expected_summary, name="factor summary")
        _assert_frame_equal(size_summary, expected_size, name="size-band summary")
        logical_exclusions = {
            "report_id",
            "daily_metrics_sha256",
            "factor_summary_sha256",
            "size_band_summary_sha256",
        }
        if report.model_dump(mode="json", exclude=logical_exclusions) != expected_report.model_dump(
            mode="json", exclude=logical_exclusions
        ):
            raise ValueError("report logic does not match full deterministic recomputation")
    return report


__all__ = [
    "DAILY_COLUMNS",
    "DEFAULT_OUTPUT_DIR",
    "DIAGNOSTIC_VERSION",
    "LayerTwoAlphaDiagnosticReportV2",
    "SIZE_COLUMNS",
    "SUMMARY_COLUMNS",
    "build_diagnostic",
    "verify_diagnostic",
    "write_diagnostic",
]
