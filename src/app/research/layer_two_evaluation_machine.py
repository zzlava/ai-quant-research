"""Frozen development-only evaluation machine for the layer-two research sleeve.

The report separates safety filtering from factor tilt with random controls,
evaluates financial warnings as left-tail classifiers, and measures IC decay.
It never reads 2025+ rows and never creates an executable portfolio.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.layer_two_alpha_diagnostic_engine import (
    newey_west_bartlett_inference,
    paired_spearman,
)
from app.research.layer_two_alpha_diagnostic_v2 import (
    FACTOR_RAW_COLUMNS,
    _build_factor_frame,
)
from app.research.repo_file_safety import resolve_repo_regular_file

SCHEMA_VERSION: Literal["1"] = "1"
REPORT_VERSION: Literal["layer-two-evaluation-machine-v1"] = "layer-two-evaluation-machine-v1"
PROTOCOL_PATH = Path("config/research/layer-two-evaluation-machine-v1.json")
ALPHA_REPORT_PATH = Path("data/all-a-share-historical-v1/research/layer-two-alpha-diagnostic-v2/report.json")
POWER_REVIEW_PATH = Path("data/all-a-share-historical-v1/research/statistical-power-review-v1.json")
FINANCIAL_OVERLAY = Path("data/all-a-share-historical-v1/research/financial-negative-list-verdict-overlay-v1")
CANDIDATE_FILE = Path(
    "data/all-a-share-historical-v1/research/candidate-eligibility-pack-v1/eligibility_verdicts.parquet"
)
MARKET_DIR = Path("data/all-a-share-historical-v1/parquet")
INDEX_DIR = Path("data/research/csi-all-share-index-2005-2024-v1")
DEFAULT_OUTPUT_DIR = Path("data/all-a-share-historical-v1/research/layer-two-evaluation-machine-v1")
DEFAULT_REPORT_PATH = DEFAULT_OUTPUT_DIR / "report.json"
DEFAULT_MONTE_CARLO_PATH = DEFAULT_OUTPUT_DIR / "monte-carlo-paths.parquet"
DEFAULT_IC_DECAY_PATH = DEFAULT_OUTPUT_DIR / "ic-decay.parquet"
DEFAULT_LEFT_TAIL_PATH = DEFAULT_OUTPUT_DIR / "left-tail-classification.parquet"

DEVELOPMENT_START = date(2022, 1, 1)
DEVELOPMENT_END = date(2023, 12, 31)
HORIZONS = (5, 10, 20, 40, 60)
FACTORS = tuple(FACTOR_RAW_COLUMNS)
FINANCIAL_COLUMNS = {
    "non_standard_audit": "audit_state",
    "large_cash_and_interest_bearing_debt": "cash_debt_state",
    "receivables_inventory_growth_vs_revenue_two_periods": "receivables_revenue_state",
    "other_receivables_to_assets_over_5pct": "other_receivables_state",
    "goodwill_to_net_assets_over_30pct": "goodwill_state",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluationMachineReport(_StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    report_version: Literal["layer-two-evaluation-machine-v1"] = REPORT_VERSION
    report_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hashes: dict[str, str]
    development_window: Literal["2022-01-01..2023-12-31"]
    forbidden_consumed_oos: Literal["2025-01-01..2026-08-21"]
    four_arm: dict[str, Any]
    left_tail: dict[str, Any]
    ic_decay: dict[str, Any]
    monte_carlo_file: str
    monte_carlo_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    monte_carlo_rows: int = Field(gt=0)
    ic_decay_file: str
    ic_decay_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ic_decay_rows: int = Field(gt=0)
    left_tail_file: str
    left_tail_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    left_tail_rows: int = Field(gt=0)
    disclosure_selection_stratification: dict[str, Any]
    confirmatory_status: Literal["not_evaluable"]
    readiness: dict[str, bool]

    @model_validator(mode="after")
    def _boundaries(self) -> EvaluationMachineReport:
        required_false = (
            "ready_for_scoring",
            "ready_for_backtest",
            "ready_for_portfolio_construction",
            "ready_for_orders",
            "ready_for_trading",
            "auto_apply",
        )
        if any(self.readiness.get(key) is not False for key in required_false):
            raise ValueError("evaluation report cannot authorize downstream use")
        if self.readiness.get("research_only") is not True:
            raise ValueError("evaluation report must remain research-only")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_id(payload: dict[str, Any], *, omit: str) -> str:
    copy = dict(payload)
    copy.pop(omit, None)
    encoded = json.dumps(copy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _load_protocol(root: Path) -> dict[str, Any]:
    path = root / PROTOCOL_PATH
    payload = json.loads(path.read_text())
    observed = payload.get("protocol_id")
    if observed != _canonical_id(payload, omit="protocol_id"):
        raise ValueError("evaluation protocol self-hash mismatch")
    if payload.get("forbidden_consumed_oos") != "2025-01-01..2026-08-21":
        raise ValueError("consumed OOS boundary drifted")
    return payload


def _financial_frame(root: Path) -> pl.DataFrame:
    pattern = str(root / FINANCIAL_OVERLAY / "verdicts" / "*.parquet")
    return (
        pl.scan_parquet(pattern)
        .select(
            "symbol",
            pl.col("as_of").str.to_date().alias("as_of_date"),
            "decision_status",
            "target_multiplier",
            *FINANCIAL_COLUMNS.values(),
        )
        .filter(pl.col("as_of_date") <= DEVELOPMENT_END)
        .collect()
        .unique(subset=["symbol", "as_of_date"], keep="first")
    )


def _add_custom_horizon_labels(root: Path, frame: pl.DataFrame, horizons: tuple[int, ...]) -> pl.DataFrame:
    calendar = (
        pl.read_parquet(root / MARKET_DIR / "calendar.parquet")
        .filter(pl.col("date") <= DEVELOPMENT_END)
        .select("date")
        .unique()
        .sort("date")
    )
    days = calendar["date"].to_list()
    index = {day: i for i, day in enumerate(days)}
    prices = (
        pl.scan_parquet(root / MARKET_DIR / "daily_bars.parquet")
        .filter(pl.col("date") <= DEVELOPMENT_END)
        .select("symbol", "date", "adj_close")
        .collect()
    )
    result = frame
    for horizon in horizons:
        endpoint_rows = [
            {"as_of_date": day, f"endpoint_{horizon}": days[index[day] + horizon]}
            for day in days
            if day >= DEVELOPMENT_START and index[day] + horizon < len(days)
        ]
        mapping = pl.DataFrame(
            endpoint_rows,
            schema={"as_of_date": pl.Date, f"endpoint_{horizon}": pl.Date},
        )
        endpoint = f"endpoint_{horizon}"
        close = f"adj_close_h{horizon}"
        result = result.join(mapping, on="as_of_date", how="left").join(
            prices.rename({"date": endpoint, "adj_close": close}),
            on=["symbol", endpoint],
            how="left",
        )
        result = result.with_columns(
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
    return result


def _benchmark_returns(root: Path, anchors: list[date], horizon: int) -> dict[date, float]:
    frame = pl.read_parquet(root / INDEX_DIR / "total_return_index.parquet").sort("date")
    frame = frame.filter(pl.col("date") <= DEVELOPMENT_END)
    days = frame["date"].to_list()
    closes = [float(x) for x in frame["close"].to_list()]
    index = {day: i for i, day in enumerate(days)}
    result: dict[date, float] = {}
    for day in anchors:
        i = index.get(day)
        if i is not None and i + horizon < len(days):
            result[day] = closes[i + horizon] / closes[i] - 1.0
    return result


def _mean(values: list[float]) -> float | None:
    return None if not values else sum(values) / len(values)


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _four_arm(
    root: Path, frame: pl.DataFrame, financial: pl.DataFrame, protocol: dict[str, Any]
) -> tuple[dict[str, Any], pl.DataFrame]:
    spec = protocol["four_arm"]
    stride = int(spec["anchor_stride_market_days"])
    width = int(spec["research_portfolio_width"])
    repeats = int(spec["monte_carlo_repeats"])
    seed = int(spec["seed"])
    calendar = (
        pl.read_parquet(root / MARKET_DIR / "calendar.parquet")
        .filter(pl.col("date").is_between(DEVELOPMENT_START, DEVELOPMENT_END))
        .sort("date")["date"]
        .to_list()
    )
    anchors = calendar[::stride]
    benchmark = _benchmark_returns(root, anchors, 40)
    joined = frame.filter(pl.col("as_of_date") <= DEVELOPMENT_END).join(
        financial, on=["symbol", "as_of_date"], how="left"
    )
    groups = joined.partition_by("as_of_date", as_dict=True, maintain_order=True)
    usable: list[tuple[date, list[float], list[float], float, float]] = []
    for day in anchors:
        day_frame = groups.get((day,))
        if day_frame is None or day not in benchmark:
            continue
        r0 = [
            float(value)
            for value in day_frame["forward_return_h40"].to_list()
            if isinstance(value, int | float) and math.isfinite(float(value))
        ]
        safe = day_frame.filter(
            pl.col("decision_status").is_in(["clean", "halved"])
            & pl.col("target_multiplier").is_not_null()
            & (pl.col("target_multiplier") > 0)
            & pl.col("forward_return_h40").is_not_null()
        )
        r1 = [float(value) for value in safe["forward_return_h40"].to_list()]
        tilt = safe.filter(pl.col("defensive_low_vol").is_not_null()).sort(
            ["defensive_low_vol", "symbol"], descending=[True, False]
        )
        if len(r0) < width or len(r1) < width or tilt.height < width:
            continue
        tilt_mean_value = tilt.head(width)["forward_return_h40"].mean()
        if not isinstance(tilt_mean_value, int | float):
            raise ValueError("tilt forward-return mean is not numeric")
        tilt_return = float(tilt_mean_value)
        usable.append((day, r0, r1, tilt_return, benchmark[day]))
    if not usable:
        raise ValueError("four-arm diagnostic has no complete anchors")
    rows: list[dict[str, Any]] = []
    for repeat in range(repeats):
        r0_path: list[float] = []
        r1_path: list[float] = []
        for day, r0, r1, _, _ in usable:
            rng0 = random.Random(seed + repeat * 1_000_003 + day.toordinal() * 17)
            rng1 = random.Random(seed + repeat * 1_000_033 + day.toordinal() * 31)
            r0_path.append(sum(rng0.sample(r0, width)) / width)
            r1_path.append(sum(rng1.sample(r1, width)) / width)
        rows.append(
            {
                "repeat": repeat,
                "r0_mean_forward_return": _mean(r0_path),
                "r1_mean_forward_return": _mean(r1_path),
                "safety_increment": _mean(r1_path) - _mean(r0_path),  # type: ignore[operator]
            }
        )
    paths = pl.DataFrame(rows)
    r0_means = [float(x) for x in paths["r0_mean_forward_return"].to_list()]
    r1_means = [float(x) for x in paths["r1_mean_forward_return"].to_list()]
    safety = [float(x) for x in paths["safety_increment"].to_list()]
    tilt_mean = _mean([row[3] for row in usable])
    benchmark_mean = _mean([row[4] for row in usable])
    assert tilt_mean is not None and benchmark_mean is not None
    report = {
        "anchor_count": len(usable),
        "anchor_start": usable[0][0].isoformat(),
        "anchor_end": usable[-1][0].isoformat(),
        "research_portfolio_width": width,
        "monte_carlo_repeats": repeats,
        "seed": seed,
        "r0_distribution": {
            "mean": _mean(r0_means),
            "q05": _quantile(r0_means, 0.05),
            "median": _quantile(r0_means, 0.5),
            "q95": _quantile(r0_means, 0.95),
        },
        "r1_distribution": {
            "mean": _mean(r1_means),
            "q05": _quantile(r1_means, 0.05),
            "median": _quantile(r1_means, 0.5),
            "q95": _quantile(r1_means, 0.95),
        },
        "safety_increment": {
            "mean": _mean(safety),
            "probability_positive": sum(x > 0 for x in safety) / len(safety),
        },
        "tilt_mean_forward_return": tilt_mean,
        "tilt_percentile_vs_r1_random": sum(x <= tilt_mean for x in r1_means) / len(r1_means),
        "benchmark_mean_forward_return": benchmark_mean,
        "tilt_minus_benchmark": tilt_mean - benchmark_mean,
        "costs_included": False,
        "execution_claim": False,
        "confirmatory_alpha_claim": False,
    }
    return report, paths


def _ic_decay(frame: pl.DataFrame) -> tuple[dict[str, Any], pl.DataFrame]:
    development = frame.filter(pl.col("as_of_date") <= DEVELOPMENT_END)
    groups = development.partition_by("as_of_date", as_dict=True, maintain_order=True)
    rows: list[dict[str, Any]] = []
    for factor in FACTORS:
        for horizon in HORIZONS:
            values: list[float] = []
            for day_frame in groups.values():
                pairs = [
                    (float(x), float(y))
                    for x, y in zip(
                        day_frame[factor].to_list(),
                        day_frame[f"forward_return_h{horizon}"].to_list(),
                        strict=True,
                    )
                    if isinstance(x, int | float)
                    and isinstance(y, int | float)
                    and math.isfinite(float(x))
                    and math.isfinite(float(y))
                ]
                if len(pairs) >= 500:
                    value = paired_spearman(pairs)
                    if value is not None:
                        values.append(value)
            inference = newey_west_bartlett_inference(values, lag=horizon - 1)
            rows.append(
                {
                    "factor_id": factor,
                    "horizon_days": horizon,
                    "valid_dates": len(values),
                    "mean_ic": _mean(values),
                    "hac_lag": horizon - 1,
                    "hac_statistic": inference.statistic,
                    "positive_hac_p_value": inference.positive_p_value,
                }
            )
    table = pl.DataFrame(rows).sort(["factor_id", "horizon_days"])
    decisions: list[dict[str, Any]] = []
    for factor in FACTORS:
        subset = table.filter(pl.col("factor_id") == factor).sort("horizon_days")
        means = {int(h): float(v) for h, v in zip(subset["horizon_days"], subset["mean_ic"], strict=True)}
        base = means[5]
        half_life: int | None = None
        for horizon in HORIZONS[1:]:
            value = means[horizon]
            if base == 0 or value * base <= 0 or abs(value) <= abs(base) * 0.5:
                half_life = horizon
                break
        gate = means[40] > 0 and (half_life is None or half_life >= 40)
        decisions.append(
            {
                "factor_id": factor,
                "mean_ic_by_horizon": {str(k): means[k] for k in HORIZONS},
                "first_tested_half_life_or_reversal_days": half_life,
                "forty_day_holding_gate_pass": gate,
            }
        )
    return {"factor_results": decisions, "selection_or_weight_change_allowed": False}, table


def _left_tail(root: Path, frame: pl.DataFrame, financial: pl.DataFrame) -> tuple[dict[str, Any], pl.DataFrame]:
    calendar = (
        pl.read_parquet(root / MARKET_DIR / "calendar.parquet")
        .filter(pl.col("date") <= DEVELOPMENT_END)
        .sort("date")
        .with_row_index("market_index")
    )
    bars = (
        pl.scan_parquet(root / MARKET_DIR / "daily_bars.parquet")
        .filter(pl.col("date") <= DEVELOPMENT_END)
        .select("symbol", "date", "adj_close")
        .collect()
        .join(calendar, on="date", how="inner")
        .sort(["symbol", "market_index"])
        .with_columns(
            pl.col("adj_close")
            .rolling_min(window_size=121, min_samples=121)
            .shift(-120)
            .over("symbol")
            .alias("future_min_120"),
            pl.col("market_index").shift(-120).over("symbol").alias("future_index_120"),
            pl.col("date").shift(-120).over("symbol").alias("endpoint_120"),
        )
        .filter(pl.col("future_index_120") - pl.col("market_index") == 120)
        .select(
            "symbol",
            pl.col("date").alias("as_of_date"),
            "adj_close",
            "future_min_120",
            "endpoint_120",
        )
    )
    future_state = (
        pl.scan_parquet(root / CANDIDATE_FILE)
        .select(
            "symbol",
            pl.col("as_of").str.to_date().alias("as_of_date"),
            "st_delist_pass",
        )
        .collect()
        .unique(subset=["symbol", "as_of_date"], keep="first")
        .join(calendar, left_on="as_of_date", right_on="date", how="inner")
        .sort(["symbol", "market_index"])
        .with_columns(
            pl.col("st_delist_pass")
            .cast(pl.Int8)
            .rolling_min(window_size=121, min_samples=121)
            .shift(-120)
            .over("symbol")
            .alias("future_st_delist_min_120"),
            pl.col("market_index").shift(-120).over("symbol").alias("future_state_index_120"),
        )
        .filter(pl.col("future_state_index_120") - pl.col("market_index") == 120)
        .select(
            "symbol",
            "as_of_date",
            (pl.col("future_st_delist_min_120") == 0).alias("future_st_delist_fail_any"),
        )
    )
    labeled = (
        frame.filter(pl.col("as_of_date") <= DEVELOPMENT_END)
        .select("symbol", "as_of_date")
        .join(bars, on=["symbol", "as_of_date"], how="left")
        .join(future_state, on=["symbol", "as_of_date"], how="left")
        .join(financial, on=["symbol", "as_of_date"], how="left")
        .with_columns(
            pl.when(pl.col("future_st_delist_fail_any"))
            .then(True)
            .when(
                pl.col("future_min_120").is_not_null()
                & pl.col("adj_close").is_not_null()
                & pl.col("future_st_delist_fail_any").is_not_null()
            )
            .then(pl.col("future_min_120") / pl.col("adj_close") - 1.0 <= -0.4)
            .otherwise(None)
            .alias("adverse_120")
        )
    )
    rows: list[dict[str, Any]] = []
    for rule, column in FINANCIAL_COLUMNS.items():
        known = labeled.filter(pl.col(column).is_in(["true", "false"]) & pl.col("adverse_120").is_not_null())
        tp = known.filter((pl.col(column) == "true") & pl.col("adverse_120")).height
        fp = known.filter((pl.col(column) == "true") & ~pl.col("adverse_120")).height
        tn = known.filter((pl.col(column) == "false") & ~pl.col("adverse_120")).height
        fn = known.filter((pl.col(column) == "false") & pl.col("adverse_120")).height
        rows.append(
            {
                "rule_id": rule,
                "known_labeled_rows": known.height,
                "unknown_or_unlabeled_rows": labeled.height - known.height,
                "true_positive": tp,
                "false_positive": fp,
                "true_negative": tn,
                "false_negative": fn,
                "precision": tp / (tp + fp) if tp + fp else None,
                "recall": tp / (tp + fn) if tp + fn else None,
                "specificity": tn / (tn + fp) if tn + fp else None,
                "adverse_prevalence": (tp + fn) / known.height if known.height else None,
            }
        )
    table = pl.DataFrame(rows)
    return {
        "labeled_population_rows": labeled.filter(pl.col("adverse_120").is_not_null()).height,
        "total_candidate_rows": labeled.height,
        "unknown_labels_remain_unknown": True,
        "classification_rows": table.height,
        "threshold_selection_allowed": False,
    }, table


def _parquet_content_hash(frame: pl.DataFrame) -> str:
    rows = []
    for row in frame.to_dicts():
        rows.append({k: v.isoformat() if type(v) is date else v for k, v in row.items()})
    encoded = json.dumps({"columns": frame.columns, "rows": rows}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def build_evaluation_machine(
    *, repo_root: Path
) -> tuple[EvaluationMachineReport, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    root = Path(repo_root).resolve(strict=True)
    protocol = _load_protocol(root)
    alpha = json.loads((root / ALPHA_REPORT_PATH).read_text())
    power = json.loads((root / POWER_REVIEW_PATH).read_text())
    if alpha.get("new_frozen_oos_unauthorized_from") != "2026-08-22":
        raise ValueError("alpha diagnostic OOS boundary drifted")
    if power.get("family_outcome") != "not_evaluable":
        raise ValueError("power review boundary drifted")
    frame = _build_factor_frame(root).filter(pl.col("as_of_date") <= DEVELOPMENT_END)
    frame = _add_custom_horizon_labels(root, frame, (10, 60))
    financial = _financial_frame(root)
    four_arm, monte_carlo = _four_arm(root, frame, financial, protocol)
    ic_decay, ic_table = _ic_decay(frame)
    left_tail, left_table = _left_tail(root, frame, financial)
    source_hashes = {
        "src/app/research/layer_two_evaluation_machine.py": _sha256_file(Path(__file__)),
        PROTOCOL_PATH.as_posix(): _sha256_file(root / PROTOCOL_PATH),
        ALPHA_REPORT_PATH.as_posix(): _sha256_file(root / ALPHA_REPORT_PATH),
        POWER_REVIEW_PATH.as_posix(): _sha256_file(root / POWER_REVIEW_PATH),
        (FINANCIAL_OVERLAY / "manifest.json").as_posix(): _sha256_file(root / FINANCIAL_OVERLAY / "manifest.json"),
        "data/research/csi-all-share-index-2005-2024-v1/manifest.json": _sha256_file(
            root / INDEX_DIR / "manifest.json"
        ),
    }
    readiness = {
        "research_only": True,
        "ready_for_scoring": False,
        "ready_for_backtest": False,
        "ready_for_portfolio_construction": False,
        "ready_for_orders": False,
        "ready_for_trading": False,
        "auto_apply": False,
    }
    report = EvaluationMachineReport(
        protocol_id=str(protocol["protocol_id"]),
        protocol_sha256=source_hashes[PROTOCOL_PATH.as_posix()],
        source_hashes=source_hashes,
        development_window="2022-01-01..2023-12-31",
        forbidden_consumed_oos="2025-01-01..2026-08-21",
        four_arm=four_arm,
        left_tail=left_tail,
        ic_decay=ic_decay,
        monte_carlo_file=DEFAULT_MONTE_CARLO_PATH.as_posix(),
        monte_carlo_sha256=_parquet_content_hash(monte_carlo),
        monte_carlo_rows=monte_carlo.height,
        ic_decay_file=DEFAULT_IC_DECAY_PATH.as_posix(),
        ic_decay_sha256=_parquet_content_hash(ic_table),
        ic_decay_rows=ic_table.height,
        left_tail_file=DEFAULT_LEFT_TAIL_PATH.as_posix(),
        left_tail_sha256=_parquet_content_hash(left_table),
        left_tail_rows=left_table.height,
        disclosure_selection_stratification={
            "status": "not_evaluable",
            "reason": "mandatory_disclosure_trigger_population_not_present_in_sealed_inputs",
            "missing_must_not_be_inferred_from_non_disclosure": True,
        },
        confirmatory_status="not_evaluable",
        readiness=readiness,
    )
    payload = report.model_dump(mode="json", exclude={"report_id"})
    return (
        report.model_copy(update={"report_id": _canonical_id(payload, omit="report_id")}),
        monte_carlo,
        ic_table,
        left_table,
    )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temp.replace(path)


def _write_parquet_atomic(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    frame.write_parquet(temp)
    temp.replace(path)


def write_evaluation_machine(*, repo_root: Path) -> EvaluationMachineReport:
    root = Path(repo_root).resolve(strict=True)
    report, monte_carlo, ic_table, left_table = build_evaluation_machine(repo_root=root)
    _write_parquet_atomic(root / report.monte_carlo_file, monte_carlo)
    _write_parquet_atomic(root / report.ic_decay_file, ic_table)
    _write_parquet_atomic(root / report.left_tail_file, left_table)
    _write_json_atomic(root / DEFAULT_REPORT_PATH, report.model_dump(mode="json"))
    return report


def verify_evaluation_machine_file(
    *, repo_root: Path, report_path: Path = DEFAULT_REPORT_PATH
) -> EvaluationMachineReport:
    root = Path(repo_root).resolve(strict=True)
    path = resolve_repo_regular_file(report_path, repo_root=root, field_name="report_path")
    observed = EvaluationMachineReport.model_validate_json(path.read_text())
    payload = observed.model_dump(mode="json", exclude={"report_id"})
    if observed.report_id != _canonical_id(payload, omit="report_id"):
        raise ValueError("evaluation report self-hash mismatch")
    expected, monte_carlo, ic_table, left_table = build_evaluation_machine(repo_root=root)
    if expected.model_dump(mode="json") != observed.model_dump(mode="json"):
        raise ValueError("evaluation report differs from full recomputation")
    for relative, expected_frame, expected_hash in (
        (observed.monte_carlo_file, monte_carlo, observed.monte_carlo_sha256),
        (observed.ic_decay_file, ic_table, observed.ic_decay_sha256),
        (observed.left_tail_file, left_table, observed.left_tail_sha256),
    ):
        disk = pl.read_parquet(resolve_repo_regular_file(Path(relative), repo_root=root, field_name="table"))
        if _parquet_content_hash(disk) != expected_hash or _parquet_content_hash(expected_frame) != expected_hash:
            raise ValueError(f"evaluation table content hash mismatch: {relative}")
    return observed


__all__ = [
    "DEFAULT_REPORT_PATH",
    "EvaluationMachineReport",
    "build_evaluation_machine",
    "verify_evaluation_machine_file",
    "write_evaluation_machine",
]
