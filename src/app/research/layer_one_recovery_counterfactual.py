"""Audited historical counterfactual for the frozen layer-one recovery policy.

This evaluator never mutates live risk state.  It applies the separately sealed
recovery overlay to the pre-consumed 2008-2021 index history and simulates the
explicit human confirmation at the first eligible weekly action after each
risk lock.  Old peaks, losses and red-line breaches remain in the audit path.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.a_share_stamp_tax_schedule import (
    AShareStampTaxScheduleContract,
    verify_a_share_stamp_tax_schedule_file,
)
from app.research.layer_one_historical_validation import (
    BASE_SLIPPAGE_BPS,
    BASELINE_STOCK_WEIGHT,
    COMBINED_END,
    COMBINED_START,
    CONFIRMED_VALIDATION_SEGMENTS,
    CONSUMED_OOS_START,
    DEFAULT_SNAPSHOT_DIR,
    INITIAL_CAPITAL_CNY,
    STRESS_SLIPPAGE_BPS,
    HistoricalValidationGates,
    WindowMetrics,
    _first_trading_day_of_week,
    _load_index_snapshot,
    _market_features,
    _trade_cost,
    _window_metrics,
)
from app.research.layer_one_index_data_evidence import (
    DEFAULT_EVIDENCE_PATH,
    verify_layer_one_index_data_evidence_file,
)
from app.research.layer_one_regime import (
    ALLOWED_BUDGET_LEVELS,
    apply_weekly_budget_adjustment,
    bind_upstream_contracts,
    map_account_drawdown_cap,
)
from app.research.layer_one_risk_lock_recovery_policy import verify_policy_file
from app.research.repo_file_safety import resolve_repo_regular_file

SCHEMA_VERSION: Literal["1"] = "1"
EVALUATOR_VERSION: Literal["layer-one-recovery-counterfactual-v1"] = "layer-one-recovery-counterfactual-v1"
DEFAULT_OUTPUT_DIR = Path("data/research/layer-one-recovery-counterfactual-v1")
DEFAULT_REPORT_PATH = DEFAULT_OUTPUT_DIR / "report.json"
DEFAULT_DAILY_PATH = DEFAULT_OUTPUT_DIR / "daily-path.parquet"
POLICY_PATH = Path("config/research/layer-one-risk-lock-recovery-policy-v1.json")

_DAILY_COLUMNS = (
    "date",
    "as_of",
    "price_close",
    "total_return_close",
    "total_return_daily_return",
    "close_to_sma_ratio",
    "realized_volatility_annualized",
    "index_drawdown",
    "trend_regime",
    "raw_market_target_budget",
    "base_budget_before",
    "base_budget_after",
    "base_account_drawdown_before",
    "base_risk_lock_active",
    "base_risk_lock_triggered",
    "base_simulated_confirmation",
    "base_epoch_id",
    "base_epoch_peak",
    "base_legacy_peak",
    "base_red_line_latched",
    "base_trade_cost",
    "base_cumulative_cost",
    "base_equity",
    "stress_budget_before",
    "stress_budget_after",
    "stress_account_drawdown_before",
    "stress_risk_lock_active",
    "stress_risk_lock_triggered",
    "stress_simulated_confirmation",
    "stress_epoch_id",
    "stress_epoch_peak",
    "stress_legacy_peak",
    "stress_red_line_latched",
    "stress_trade_cost",
    "stress_cumulative_cost",
    "stress_equity",
    "baseline_equity",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecoveryCounterfactualReport(_StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    evaluator_version: Literal["layer-one-recovery-counterfactual-v1"] = EVALUATOR_VERSION
    report_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evaluator_module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recovery_policy_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    recovery_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    layer_one_index_data_evidence_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    layer_one_index_protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    two_layer_decision_contract_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    stamp_tax_contract_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    risk_state_symbol: Literal["000985.CSI"]
    performance_benchmark_symbol: Literal["H00985.CSI"]
    validation_start: date
    validation_end: date
    first_action_day: date
    last_action_day: date
    daily_path: str
    daily_table_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    daily_row_count: int = Field(gt=0)
    simulated_confirmation_count: int = Field(ge=0)
    stress_simulated_confirmation_count: int = Field(ge=0)
    risk_lock_trigger_dates: list[date]
    stress_risk_lock_trigger_dates: list[date]
    simulated_confirmation_dates: list[date]
    stress_simulated_confirmation_dates: list[date]
    red_line_breach_latched: bool
    stress_red_line_breach_latched: bool
    validation_segments: list[WindowMetrics]
    combined: WindowMetrics
    stress_validation_segments: list[WindowMetrics]
    stress_combined: WindowMetrics
    budget_occupancy: dict[str, int]
    gates: HistoricalValidationGates
    historical_validation_evidence_pass: bool
    historical_counterfactual_only: Literal[True] = True
    simulated_confirmation_is_not_observed_user_action: Literal[True] = True
    upstream_loss_history_not_rewritten: Literal[True] = True
    consumed_oos_reused: Literal[False] = False
    oos_claim: Literal[False] = False
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @model_validator(mode="after")
    def _boundaries(self) -> RecoveryCounterfactualReport:
        if self.validation_start != COMBINED_START or self.validation_end != COMBINED_END:
            raise ValueError("historical validation window drifted")
        if self.last_action_day >= CONSUMED_OOS_START:
            raise ValueError("counterfactual reaches consumed OOS")
        if self.historical_validation_evidence_pass != self.gates.all_hard_gates_pass:
            raise ValueError("evidence pass must equal the hard-gate conjunction")
        if self.simulated_confirmation_count != len(self.simulated_confirmation_dates):
            raise ValueError("base confirmation count mismatch")
        if self.stress_simulated_confirmation_count != len(self.stress_simulated_confirmation_dates):
            raise ValueError("stress confirmation count mismatch")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_id(report: RecoveryCounterfactualReport) -> str:
    payload = report.model_dump(mode="json", exclude={"report_id"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _frame_content_sha256(frame: pl.DataFrame) -> str:
    if frame.columns != list(_DAILY_COLUMNS):
        raise ValueError("counterfactual daily path columns drifted")
    rows: list[dict[str, Any]] = []
    for row in frame.to_dicts():
        rows.append({key: value.isoformat() if type(value) is date else value for key, value in row.items()})
    encoded = json.dumps(
        {"columns": list(_DAILY_COLUMNS), "rows": rows},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _seal(report: RecoveryCounterfactualReport) -> RecoveryCounterfactualReport:
    return report.model_copy(update={"report_id": _report_id(report)})


def _advance(
    *,
    equity: float,
    epoch_peak: float,
    legacy_peak: float,
    previous_budget: float,
    risk_lock_active: bool,
    lock_start_index: int | None,
    epoch_id: int,
    red_line_latched: bool,
    market_return: float,
    raw_market_target: float,
    trend_regime: str,
    realized_volatility: float,
    first_of_week: bool,
    market_index: int,
    trade_date: date,
    slippage_bps: float,
    stamp_contract: AShareStampTaxScheduleContract,
) -> tuple[float, float, float, float, bool, int | None, int, bool, bool, bool, float, float]:
    account_drawdown = equity / epoch_peak - 1.0
    account_cap, triggers_lock, red_line = map_account_drawdown_cap(account_drawdown)
    red_line_latched = bool(red_line_latched or red_line)
    newly_triggered = bool(triggers_lock and not risk_lock_active)
    if newly_triggered:
        risk_lock_active = True
        lock_start_index = market_index

    simulated_confirmation = False
    if risk_lock_active:
        cooling_complete = lock_start_index is not None and market_index - lock_start_index >= 20
        eligible = cooling_complete and first_of_week and trend_regime != "negative" and realized_volatility < 0.27
        if eligible:
            risk_lock_active = False
            lock_start_index = None
            epoch_id += 1
            simulated_confirmation = True
            account_cap = 0.3

    target_raw = 0.0 if risk_lock_active else min(raw_market_target, account_cap)
    if simulated_confirmation:
        target_raw = min(target_raw, 0.3)
    target_budget, _, _ = apply_weekly_budget_adjustment(
        raw_target_budget=target_raw,
        previous_applied_stock_budget=previous_budget,
        target_day_is_first_market_trading_day_of_week=first_of_week,
        risk_lock_active=risk_lock_active,
    )
    gross_equity = equity * (1.0 + previous_budget * market_return)
    cost = _trade_cost(
        equity=gross_equity,
        previous_budget=previous_budget,
        target_budget=target_budget,
        trade_date=trade_date,
        slippage_bps=slippage_bps,
        stamp_contract=stamp_contract,
    )
    next_equity = gross_equity - cost
    if not math.isfinite(next_equity) or next_equity <= 0:
        raise ValueError("counterfactual equity became invalid")
    legacy_peak = max(legacy_peak, next_equity)
    epoch_peak = next_equity if simulated_confirmation else max(epoch_peak, next_equity)
    return (
        next_equity,
        epoch_peak,
        legacy_peak,
        target_budget,
        risk_lock_active,
        lock_start_index,
        epoch_id,
        red_line_latched,
        newly_triggered,
        simulated_confirmation,
        cost,
        account_drawdown,
    )


def _build_path(
    *,
    dates: Sequence[date],
    price_close: Sequence[float],
    total_close: Sequence[float],
    stamp_contract: AShareStampTaxScheduleContract,
) -> pl.DataFrame:
    start_index = max(
        242,
        min(index for index, day in enumerate(dates) if day >= stamp_contract.schedule_coverage_start),
    )
    state: dict[str, dict[str, Any]] = {
        "base": {
            "equity": INITIAL_CAPITAL_CNY,
            "peak": INITIAL_CAPITAL_CNY,
            "legacy": INITIAL_CAPITAL_CNY,
            "budget": 0.0,
            "lock": False,
            "lock_start": None,
            "epoch": 0,
            "red": False,
            "costs": 0.0,
            "slippage": BASE_SLIPPAGE_BPS,
        },
        "stress": {
            "equity": INITIAL_CAPITAL_CNY,
            "peak": INITIAL_CAPITAL_CNY,
            "legacy": INITIAL_CAPITAL_CNY,
            "budget": 0.0,
            "lock": False,
            "lock_start": None,
            "epoch": 0,
            "red": False,
            "costs": 0.0,
            "slippage": STRESS_SLIPPAGE_BPS,
        },
    }
    baseline_equity = INITIAL_CAPITAL_CNY
    rows: list[dict[str, Any]] = []
    for index in range(start_index, len(dates)):
        d = dates[index]
        p_index = index - 1
        ratio, vol, index_dd, regime, raw_target = _market_features(price_close, p_index)
        market_return = total_close[index] / total_close[p_index] - 1.0
        first_of_week = _first_trading_day_of_week(dates, index)
        row: dict[str, Any] = {
            "date": d,
            "as_of": dates[p_index],
            "price_close": float(price_close[index]),
            "total_return_close": float(total_close[index]),
            "total_return_daily_return": market_return,
            "close_to_sma_ratio": ratio,
            "realized_volatility_annualized": vol,
            "index_drawdown": index_dd,
            "trend_regime": regime,
            "raw_market_target_budget": raw_target,
        }
        for name in ("base", "stress"):
            item = state[name]
            before = float(item["budget"])
            result = _advance(
                equity=float(item["equity"]),
                epoch_peak=float(item["peak"]),
                legacy_peak=float(item["legacy"]),
                previous_budget=before,
                risk_lock_active=bool(item["lock"]),
                lock_start_index=item["lock_start"],
                epoch_id=int(item["epoch"]),
                red_line_latched=bool(item["red"]),
                market_return=market_return,
                raw_market_target=raw_target,
                trend_regime=regime,
                realized_volatility=vol,
                first_of_week=first_of_week,
                market_index=index,
                trade_date=d,
                slippage_bps=float(item["slippage"]),
                stamp_contract=stamp_contract,
            )
            (
                item["equity"],
                item["peak"],
                item["legacy"],
                item["budget"],
                item["lock"],
                item["lock_start"],
                item["epoch"],
                item["red"],
                triggered,
                confirmed,
                cost,
                drawdown,
            ) = result
            item["costs"] = float(item["costs"]) + cost
            row.update(
                {
                    f"{name}_budget_before": before,
                    f"{name}_budget_after": item["budget"],
                    f"{name}_account_drawdown_before": drawdown,
                    f"{name}_risk_lock_active": item["lock"],
                    f"{name}_risk_lock_triggered": triggered,
                    f"{name}_simulated_confirmation": confirmed,
                    f"{name}_epoch_id": item["epoch"],
                    f"{name}_epoch_peak": item["peak"],
                    f"{name}_legacy_peak": item["legacy"],
                    f"{name}_red_line_latched": item["red"],
                    f"{name}_trade_cost": cost,
                    f"{name}_cumulative_cost": item["costs"],
                    f"{name}_equity": item["equity"],
                }
            )
        baseline_equity *= 1.0 + BASELINE_STOCK_WEIGHT * market_return
        row["baseline_equity"] = baseline_equity
        rows.append(row)
    return pl.DataFrame(rows).select(_DAILY_COLUMNS)


def _gates(
    segments: list[WindowMetrics],
    combined: WindowMetrics,
    stress_segments: list[WindowMetrics],
    stress_combined: WindowMetrics,
) -> HistoricalValidationGates:
    retention = combined.positive_baseline_cagr_retention is None or combined.positive_baseline_cagr_retention >= 0.6
    values = {
        "per_segment_max_drawdown_floor_pass": all(x.max_drawdown >= -0.2 for x in segments),
        "combined_max_drawdown_floor_pass": combined.max_drawdown >= -0.2,
        "combined_positive_after_cost_annualized_return_pass": combined.annualized_return_after_cost > 0,
        "combined_calmar_pass": combined.calmar is not None and combined.calmar >= 0.5,
        "combined_baseline_drawdown_improvement_pass": (
            combined.max_drawdown_amplitude_improvement is not None
            and combined.max_drawdown_amplitude_improvement >= 0.25
        ),
        "combined_positive_baseline_cagr_retention_pass": retention,
        "stress_max_drawdown_floor_pass": (
            stress_combined.max_drawdown >= -0.2 and all(x.max_drawdown >= -0.2 for x in stress_segments)
        ),
    }
    return HistoricalValidationGates(**values, all_hard_gates_pass=all(values.values()))


def build_recovery_counterfactual(
    *, repo_root: Path, daily_path: Path = DEFAULT_DAILY_PATH
) -> tuple[RecoveryCounterfactualReport, pl.DataFrame]:
    root = Path(repo_root).resolve(strict=True)
    policy = verify_policy_file(repo_root=root, policy_path=POLICY_PATH)
    evidence = verify_layer_one_index_data_evidence_file(repo_root=root, evidence_path=DEFAULT_EVIDENCE_PATH)
    contract_id, _, protocol_id, _ = bind_upstream_contracts(repo_root=root)
    stamp, stamp_result = verify_a_share_stamp_tax_schedule_file(repo_root=root)
    if not stamp_result.disk_binding_ok:
        raise ValueError("stamp-tax contract disk binding failed")
    dates, price, total = _load_index_snapshot(root / DEFAULT_SNAPSHOT_DIR)
    end_index = max(i for i, day in enumerate(dates) if day <= COMBINED_END)
    frame = _build_path(
        dates=dates[: end_index + 1],
        price_close=price[: end_index + 1],
        total_close=total[: end_index + 1],
        stamp_contract=stamp,
    )
    segments = [
        _window_metrics(
            frame,
            label=f"recovery_{a.year}_{b.year}",
            declared_start=a,
            declared_end=b,
            equity_column="base_equity",
            cumulative_cost_column="base_cumulative_cost",
            trade_cost_column="base_trade_cost",
        )
        for a, b in CONFIRMED_VALIDATION_SEGMENTS
    ]
    combined = _window_metrics(
        frame,
        label="recovery_combined_2013_2021",
        declared_start=COMBINED_START,
        declared_end=COMBINED_END,
        equity_column="base_equity",
        cumulative_cost_column="base_cumulative_cost",
        trade_cost_column="base_trade_cost",
    )
    stress_segments = [
        _window_metrics(
            frame,
            label=f"stress_recovery_{a.year}_{b.year}",
            declared_start=a,
            declared_end=b,
            equity_column="stress_equity",
            cumulative_cost_column="stress_cumulative_cost",
            trade_cost_column="stress_trade_cost",
        )
        for a, b in CONFIRMED_VALIDATION_SEGMENTS
    ]
    stress_combined = _window_metrics(
        frame,
        label="stress_recovery_combined_2013_2021",
        declared_start=COMBINED_START,
        declared_end=COMBINED_END,
        equity_column="stress_equity",
        cumulative_cost_column="stress_cumulative_cost",
        trade_cost_column="stress_trade_cost",
    )
    gates = _gates(segments, combined, stress_segments, stress_combined)
    window = frame.filter(pl.col("date").is_between(COMBINED_START, COMBINED_END))
    occupancy = {
        f"{level:.1f}": window.filter(pl.col("base_budget_after") == level).height for level in ALLOWED_BUDGET_LEVELS
    }
    relative = Path(daily_path)
    if relative.is_absolute():
        relative = relative.resolve().relative_to(root)
    if ".." in relative.parts:
        raise ValueError("daily_path escapes repo root")
    report = RecoveryCounterfactualReport(
        evaluator_module_sha256=_sha256_file(Path(__file__)),
        recovery_policy_id=str(policy.policy_id),
        recovery_policy_sha256=_sha256_file(root / POLICY_PATH),
        layer_one_index_data_evidence_id=str(evidence.evidence_id),
        layer_one_index_protocol_id=protocol_id,
        two_layer_decision_contract_id=contract_id,
        data_snapshot_id=str(evidence.snapshot_manifest.artifact_id),
        stamp_tax_contract_id=str(stamp.contract_id),
        risk_state_symbol="000985.CSI",
        performance_benchmark_symbol="H00985.CSI",
        validation_start=COMBINED_START,
        validation_end=COMBINED_END,
        first_action_day=frame["date"][0],
        last_action_day=frame["date"][-1],
        daily_path=relative.as_posix(),
        daily_table_content_sha256=_frame_content_sha256(frame),
        daily_row_count=frame.height,
        simulated_confirmation_count=frame.filter(pl.col("base_simulated_confirmation")).height,
        stress_simulated_confirmation_count=frame.filter(pl.col("stress_simulated_confirmation")).height,
        risk_lock_trigger_dates=frame.filter(pl.col("base_risk_lock_triggered"))["date"].to_list(),
        stress_risk_lock_trigger_dates=frame.filter(pl.col("stress_risk_lock_triggered"))["date"].to_list(),
        simulated_confirmation_dates=frame.filter(pl.col("base_simulated_confirmation"))["date"].to_list(),
        stress_simulated_confirmation_dates=frame.filter(pl.col("stress_simulated_confirmation"))["date"].to_list(),
        red_line_breach_latched=bool(frame["base_red_line_latched"][-1]),
        stress_red_line_breach_latched=bool(frame["stress_red_line_latched"][-1]),
        validation_segments=segments,
        combined=combined,
        stress_validation_segments=stress_segments,
        stress_combined=stress_combined,
        budget_occupancy=occupancy,
        gates=gates,
        historical_validation_evidence_pass=gates.all_hard_gates_pass,
    )
    return _seal(report), frame


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_bytes(data)
    temp.replace(path)


def write_recovery_counterfactual(
    *,
    repo_root: Path,
    report_path: Path = DEFAULT_REPORT_PATH,
    daily_path: Path = DEFAULT_DAILY_PATH,
) -> RecoveryCounterfactualReport:
    root = Path(repo_root).resolve(strict=True)
    report, frame = build_recovery_counterfactual(repo_root=root, daily_path=daily_path)
    daily = root / report.daily_path
    daily.parent.mkdir(parents=True, exist_ok=True)
    temp = daily.with_name(f".{daily.name}.{uuid.uuid4().hex}.tmp")
    frame.write_parquet(temp)
    temp.replace(daily)
    payload = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    destination = report_path if report_path.is_absolute() else root / report_path
    _write_atomic(destination, payload.encode())
    return report


def verify_recovery_counterfactual_file(
    *,
    repo_root: Path,
    report_path: Path = DEFAULT_REPORT_PATH,
) -> RecoveryCounterfactualReport:
    root = Path(repo_root).resolve(strict=True)
    report_file = resolve_repo_regular_file(report_path, repo_root=root, field_name="report_path")
    observed = RecoveryCounterfactualReport.model_validate_json(report_file.read_text())
    if observed.report_id != _report_id(observed):
        raise ValueError("recovery report self-hash mismatch")
    daily = resolve_repo_regular_file(Path(observed.daily_path), repo_root=root, field_name="daily_path")
    disk_frame = pl.read_parquet(daily)
    if _frame_content_sha256(disk_frame) != observed.daily_table_content_sha256:
        raise ValueError("recovery daily path content hash mismatch")
    expected, expected_frame = build_recovery_counterfactual(repo_root=root, daily_path=Path(observed.daily_path))
    if expected.model_dump(mode="json") != observed.model_dump(mode="json"):
        raise ValueError("recovery report differs from full recomputation")
    if _frame_content_sha256(expected_frame) != _frame_content_sha256(disk_frame):
        raise ValueError("recovery daily path differs from full recomputation")
    return observed


__all__ = [
    "DEFAULT_DAILY_PATH",
    "DEFAULT_REPORT_PATH",
    "RecoveryCounterfactualReport",
    "build_recovery_counterfactual",
    "verify_recovery_counterfactual_file",
    "write_recovery_counterfactual",
]
