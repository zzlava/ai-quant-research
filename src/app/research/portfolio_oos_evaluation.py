from __future__ import annotations

import hashlib
import json
import math
import shutil
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.backtest.metrics import compute_attribution, compute_metrics
from app.errors import DataQualityError, MissingBenchmarkError, PreflightError, SnapshotError
from app.models.backtest import BacktestResult, BacktestWindow
from app.models.config import StrategyConfig
from app.models.scores import ScoreResult
from app.preflight import preflight_research
from app.research.portfolio_construction import (
    BenchmarkResult,
    evaluate_price_index_benchmark,
    run_backtest_with_signal_cutoff,
)
from app.research.portfolio_oos_authorization import (
    AUTHORIZED_RUNTIME_CONFIG_HASH,
    DEFAULT_PORTFOLIO_OOS_AUTH_PATH,
    PortfolioOosOneShotAuthorization,
    assert_authorization_paths_unused,
    assert_authorization_self_consistent,
    load_verified_committed_portfolio_oos_authorization,
    verify_authorization_against_freeze,
)
from app.research.portfolio_oos_freeze import (
    FROZEN_CONFIG_HASH,
    FROZEN_RUNTIME_EQUIVALENT_ANCHOR,
    PortfolioOosFreezeContract,
    assert_committed_portfolio_oos_freeze_bindings,
    verify_portfolio_oos_freeze,
)
from app.research.portfolio_robustness import (
    ExposureSummary,
    SectorAttribution,
    _cost_stress_config,
    _exposure_summary,
    _sector_attribution,
    _symbol_attribution,
)
from app.research_scope import research_notice
from app.scoring.engine import ScoringEngine
from app.storage.duckdb_store import DuckDBParquetStore
from app.storage.fundamental_io import load_verified_fundamental_snapshot
from app.storage.fundamental_overlay import FundamentalOverlayStore
from app.storage.hashing import sha256_text
from app.storage.protocol import MarketStore
from app.storage.snapshot_io import load_verified_snapshot

PORTFOLIO_OOS_EVAL_SCHEMA_VERSION: Literal["1"] = "1"
PORTFOLIO_OOS_EVAL_VERSION: Literal["one-shot-v1"] = "one-shot-v1"
PORTFOLIO_OOS_RECEIPT_VERSION: Literal["all-a-share-portfolio-oos-one-shot-receipt-v1"] = (
    "all-a-share-portfolio-oos-one-shot-receipt-v1"
)

BASELINE_SCENARIO_ID: Literal["baseline"] = "baseline"
MODERATE_SCENARIO_ID: Literal["moderate_2x_commission_2x_slippage"] = "moderate_2x_commission_2x_slippage"
SEVERE_SCENARIO_ID: Literal["severe_4x_commission_5x_slippage"] = "severe_4x_commission_5x_slippage"

REQUIRED_SCENARIO_IDS: tuple[str, ...] = (
    BASELINE_SCENARIO_ID,
    MODERATE_SCENARIO_ID,
    SEVERE_SCENARIO_ID,
)
SCENARIO_RESULT_FILES: dict[str, str] = {
    BASELINE_SCENARIO_ID: "baseline_backtest.json",
    MODERATE_SCENARIO_ID: "moderate_2x_commission_2x_slippage_backtest.json",
    SEVERE_SCENARIO_ID: "severe_4x_commission_5x_slippage_backtest.json",
}
SCENARIO_COST_MULTIPLIERS: dict[str, tuple[float, float]] = {
    BASELINE_SCENARIO_ID: (1.0, 1.0),
    MODERATE_SCENARIO_ID: (2.0, 2.0),
    SEVERE_SCENARIO_ID: (4.0, 5.0),
}

# Typed data / preflight failures may seal not_evaluable after consumption starts.
# Contract, hash, path, calendar, and generic ValueError must fail before consume.
_OOS_DATA_EVALUABILITY_ERRORS = (
    PreflightError,
    DataQualityError,
    SnapshotError,
    MissingBenchmarkError,
)

OosOutcome = Literal["not_evaluable", "no_go", "conditional_go"]
FailureStage = Literal["none", "preflight", "scenario"]
ScoreFn = Callable[[date], list[ScoreResult]]
ProgressFn = Callable[[str, int, int], None]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortfolioOosGateResult(_StrictModel):
    gate: str
    category: Literal["evaluability", "primary", "hard_risk", "cost_stress", "descriptive"]
    passed: bool
    observed: float | int | str | bool | None
    threshold: float | int | str | bool | None
    decides_oos_result: bool
    note: str


class PortfolioOosScenarioArtifact(_StrictModel):
    scenario_id: str
    result_file: str
    result_file_sha256: str | None = None
    commission_rate: float
    minimum_commission: float
    slippage_bps: float
    stamp_tax_unchanged: bool = True
    total_return: float | None = None
    sharpe_ratio: float | None = None
    max_drawdown: float | None = None
    number_of_trades: int | None = None
    open_positions_at_end: int | None = None
    final_equity: float | None = None
    total_trading_costs: float | None = None
    pnl_reconciliation_error: float | None = None
    largest_symbol_loss_to_initial_cash: float | None = None


class PortfolioOosDescriptiveSummary(_StrictModel):
    benchmark: BenchmarkResult | None = None
    return_minus_benchmark: float | None = None
    exposure: ExposureSummary | None = None
    sectors: list[SectorAttribution] = Field(default_factory=list)
    moderate_total_return: float | None = None
    sector_taxonomy_note: str = (
        "Instrument sector labels are current stock-basic classifications, not historical "
        "point-in-time industry membership. They are diagnostic only and never trading inputs."
    )


class PortfolioOosEvaluationReport(_StrictModel):
    schema_version: Literal["1"] = PORTFOLIO_OOS_EVAL_SCHEMA_VERSION
    evaluation_version: Literal["one-shot-v1"] = PORTFOLIO_OOS_EVAL_VERSION
    authorization_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    freeze_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_path: str
    strategy_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_config_id: str
    frozen_config_hash: str
    runtime_config_hash: str
    runtime_signal_anchor_date: date
    runtime_config_diff: list[str]
    market_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    fundamental_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    fundamental_base_market_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    composite_store_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_start: date
    evaluation_end: date
    first_2025_plus_signal: date
    signal_cutoff: date
    last_scheduled_exit: date
    preflight_passed: bool
    preflight_error: str | None = None
    scenario_execution_complete: bool = True
    failure_stage: FailureStage = "none"
    scenario_error: str | None = None
    observed_first_signal: date | None = None
    scenarios: dict[str, PortfolioOosScenarioArtifact] = Field(default_factory=dict)
    gates: list[PortfolioOosGateResult] = Field(default_factory=list)
    descriptive: PortfolioOosDescriptiveSummary = Field(default_factory=PortfolioOosDescriptiveSummary)
    outcome: OosOutcome
    outcome_reason: str
    report_id: str | None = None
    one_shot: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    human_review_required: Literal[True] = True
    research_boundary: str = (
        "One-shot 2025+ portfolio OOS evaluation only. No p-value, IC, parameter search, "
        "auto scoring, paper trading, live trading, or automatic promotion."
    )


class PortfolioOosConsumptionReceipt(_StrictModel):
    schema_version: Literal["1"] = "1"
    receipt_version: Literal["all-a-share-portfolio-oos-one-shot-receipt-v1"] = PORTFOLIO_OOS_RECEIPT_VERSION
    authorization_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    freeze_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_dir: str = Field(min_length=1)
    output_file_sha256: dict[str, str] = Field(default_factory=dict)
    one_shot: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    human_review_required: Literal[True] = True
    receipt_id: str | None = None


class InMemoryScoreProvider:
    """In-process score cache shared across baseline and cost-stress scenarios.

    Never writes a persistent score cache or scores parquet table.
    """

    def __init__(self, *, store: MarketStore, config: StrategyConfig) -> None:
        self.store = store
        self.config = config
        self.engine = ScoringEngine(store, config)
        self.memory: dict[date, list[ScoreResult]] = {}
        self.hits = 0
        self.misses = 0

    def __call__(self, as_of: date) -> list[ScoreResult]:
        cached = self.memory.get(as_of)
        if cached is not None:
            self.hits += 1
            return cached
        results = self.engine.run(as_of)
        self.memory[as_of] = results
        self.misses += 1
        return results


def build_runtime_equivalent_config(frozen_config: StrategyConfig) -> StrategyConfig:
    """Return a runtime copy that changes only signal_anchor_date to the equivalent anchor."""
    if frozen_config.config_hash() != FROZEN_CONFIG_HASH:
        raise ValueError("frozen strategy config_hash does not match the sealed protocol")
    if frozen_config.trade.signal_anchor_date != date(2022, 1, 4):
        raise ValueError("frozen strategy signal_anchor_date drifted")
    runtime = _apply_runtime_anchor(frozen_config, FROZEN_RUNTIME_EQUIVALENT_ANCHOR)
    if runtime.config_hash() != AUTHORIZED_RUNTIME_CONFIG_HASH:
        raise ValueError(
            f"runtime config_hash does not match the sealed expected runtime hash {AUTHORIZED_RUNTIME_CONFIG_HASH}"
        )
    return runtime


def _runtime_config_for_evaluation(
    frozen_config: StrategyConfig,
    authorization: PortfolioOosOneShotAuthorization,
) -> StrategyConfig:
    expected_anchor = authorization.runtime_override.runtime_value
    if frozen_config.trade.signal_anchor_date != authorization.runtime_override.frozen_value:
        raise ValueError("frozen strategy signal_anchor_date does not match authorization")
    runtime = _apply_runtime_anchor(frozen_config, expected_anchor)
    if runtime.config_hash() != authorization.runtime_override.expected_runtime_config_hash:
        raise ValueError("runtime config_hash does not match the authorization expected_runtime_config_hash")
    return runtime


def _apply_runtime_anchor(frozen_config: StrategyConfig, runtime_anchor: date) -> StrategyConfig:
    runtime = frozen_config.model_copy(
        update={"trade": frozen_config.trade.model_copy(update={"signal_anchor_date": runtime_anchor})}
    )
    diff = canonical_config_diff(frozen_config, runtime)
    if diff != ["trade.signal_anchor_date"]:
        raise ValueError(f"runtime config diff is not anchor-only: {diff}")
    return runtime


def canonical_config_diff(left: StrategyConfig, right: StrategyConfig) -> list[str]:
    return _walk_diff(left.model_dump(mode="json"), right.model_dump(mode="json"), prefix="")


def classify_portfolio_oos_outcome(
    *,
    preflight_passed: bool,
    baseline: BacktestResult | None,
    severe: BacktestResult | None,
    initial_cash: float,
    min_closed_trades: int,
    max_drawdown_floor: float,
    largest_symbol_loss_fraction: float,
    pnl_reconciliation_abs_tol: float,
    scenario_execution_complete: bool = True,
    failure_stage: FailureStage = "none",
    failure_detail: str | None = None,
) -> tuple[OosOutcome, str, list[PortfolioOosGateResult], float | None]:
    gates: list[PortfolioOosGateResult] = []
    gates.append(
        PortfolioOosGateResult(
            gate="full_preflight",
            category="evaluability",
            passed=preflight_passed,
            observed=preflight_passed,
            threshold=True,
            decides_oos_result=True,
            note="full preflight must pass on the authorized evaluation window",
        )
    )
    overall_complete = (
        preflight_passed and scenario_execution_complete and baseline is not None and severe is not None
    )
    if not overall_complete:
        if failure_stage == "preflight" or not preflight_passed:
            reason = (
                f"preflight failed: {failure_detail}"
                if failure_detail
                else "predeclared data/preflight/completeness failure before usable scenario results"
            )
        elif failure_stage == "scenario" or not scenario_execution_complete:
            reason = (
                f"scenario data/availability failure: {failure_detail}"
                if failure_detail
                else "scenario execution incomplete before usable scenario results"
            )
        else:
            reason = "predeclared data/preflight/completeness failure before usable scenario results"
        return ("not_evaluable", reason, gates, None)

    assert baseline is not None
    assert severe is not None
    baseline_metrics = baseline.metrics
    severe_metrics = severe.metrics
    closed_trades = baseline_metrics.number_of_trades
    pnl_error = _pnl_reconciliation_error(baseline, initial_cash)
    largest_loss = _largest_symbol_loss_fraction(baseline, initial_cash)
    finite = _required_metrics_finite(baseline_metrics) and _required_metrics_finite(severe_metrics)

    evaluability = [
        PortfolioOosGateResult(
            gate="baseline_closed_trades",
            category="evaluability",
            passed=closed_trades >= min_closed_trades,
            observed=closed_trades,
            threshold=min_closed_trades,
            decides_oos_result=True,
            note="baseline closed trades must meet the sealed minimum",
        ),
        PortfolioOosGateResult(
            gate="baseline_open_positions_at_end",
            category="evaluability",
            passed=baseline.open_positions_at_end == 0,
            observed=baseline.open_positions_at_end,
            threshold=0,
            decides_oos_result=True,
            note="baseline must fully liquidate by evaluation end",
        ),
        PortfolioOosGateResult(
            gate="severe_open_positions_at_end",
            category="evaluability",
            passed=severe.open_positions_at_end == 0,
            observed=severe.open_positions_at_end,
            threshold=0,
            decides_oos_result=True,
            note="severe cost scenario must fully liquidate by evaluation end",
        ),
        PortfolioOosGateResult(
            gate="required_metrics_finite",
            category="evaluability",
            passed=finite,
            observed=finite,
            threshold=True,
            decides_oos_result=True,
            note="required baseline and severe metrics must be finite",
        ),
        PortfolioOosGateResult(
            gate="pnl_reconciliation",
            category="evaluability",
            passed=abs(pnl_error) <= pnl_reconciliation_abs_tol,
            observed=pnl_error,
            threshold=pnl_reconciliation_abs_tol,
            decides_oos_result=True,
            note="closed-trade P&L must reconcile to equity change",
        ),
    ]
    gates.extend(evaluability)
    if any(not gate.passed for gate in evaluability):
        return (
            "not_evaluable",
            "predeclared completeness or reconciliation gate failed",
            gates,
            largest_loss,
        )

    primary_return = baseline_metrics.total_return
    sharpe = baseline_metrics.sharpe_ratio
    drawdown = baseline_metrics.max_drawdown
    severe_return = severe_metrics.total_return
    deciding = [
        PortfolioOosGateResult(
            gate="primary_total_return",
            category="primary",
            passed=primary_return > 0,
            observed=primary_return,
            threshold=0,
            decides_oos_result=True,
            note="baseline total_return after declared/realized costs must be > 0",
        ),
        PortfolioOosGateResult(
            gate="baseline_sharpe",
            category="hard_risk",
            passed=sharpe is not None and sharpe > 0,
            observed=sharpe,
            threshold=0,
            decides_oos_result=True,
            note="baseline Sharpe must be > 0",
        ),
        PortfolioOosGateResult(
            gate="baseline_max_drawdown",
            category="hard_risk",
            passed=drawdown is not None and drawdown >= max_drawdown_floor,
            observed=drawdown,
            threshold=max_drawdown_floor,
            decides_oos_result=True,
            note=f"baseline max drawdown must be >= {max_drawdown_floor}",
        ),
        PortfolioOosGateResult(
            gate="largest_single_symbol_loss",
            category="hard_risk",
            passed=largest_loss <= largest_symbol_loss_fraction,
            observed=largest_loss,
            threshold=largest_symbol_loss_fraction,
            decides_oos_result=True,
            note=(
                "largest per-symbol aggregate net loss / initial_cash must be "
                f"<= {largest_symbol_loss_fraction}"
            ),
        ),
        PortfolioOosGateResult(
            gate="severe_total_return",
            category="cost_stress",
            passed=severe_return > 0,
            observed=severe_return,
            threshold=0,
            decides_oos_result=True,
            note="severe cost stress total_return must be > 0",
        ),
    ]
    gates.extend(deciding)
    if any(not gate.passed for gate in deciding):
        return (
            "no_go",
            "evaluable, but primary endpoint or a hard risk / severe cost gate failed",
            gates,
            largest_loss,
        )
    return (
        "conditional_go",
        "all predeclared gates passed; human_review_required remains true",
        gates,
        largest_loss,
    )


def evaluate_and_write_portfolio_oos_one_shot(
    *,
    authorization: PortfolioOosOneShotAuthorization,
    freeze_path: Path,
    strategy_path: Path,
    market_dir: Path,
    fundamental_dir: Path,
    root: Path | None = None,
    project_root: Path | None = None,
    progress: ProgressFn | None = None,
    score_fn: ScoreFn | None = None,
) -> tuple[PortfolioOosEvaluationReport, PortfolioOosConsumptionReceipt, Path]:
    """Run the authorized one-shot portfolio OOS evaluation and seal immutable outputs."""
    assert_authorization_self_consistent(authorization)
    assert_authorization_paths_unused(authorization, root=root)
    verify_authorization_against_freeze(authorization, freeze_path=freeze_path)
    repo_root = Path(project_root) if project_root is not None else Path.cwd()
    # Full freeze verify (including calendar schedule proof) before any scoring/backtest.
    freeze = verify_portfolio_oos_freeze(freeze_path=freeze_path, project_root=repo_root)
    if freeze.freeze_id != authorization.freeze_id:
        raise ValueError("verified freeze_id does not match authorization")

    frozen_config = _load_verified_frozen_strategy(strategy_path, authorization)
    # Committed runs require the sealed runtime hash; synthetic tests may inject a local
    # frozen YAML whose only allowed runtime difference is still signal_anchor_date.
    runtime_config = _runtime_config_for_evaluation(frozen_config, authorization)
    runtime_diff = canonical_config_diff(frozen_config, runtime_config)
    if runtime_config.portfolio.initial_cash != float(freeze.bound_strategy.initial_cash):
        raise ValueError("runtime initial_cash does not match the freeze bound strategy")
    if runtime_config.portfolio.initial_cash != float(authorization.hard_risk_gates.initial_cash):
        raise ValueError("runtime initial_cash does not match authorization hard_risk_gates")

    store = _load_authorized_composite_store(
        authorization,
        market_dir=Path(market_dir),
        fundamental_dir=Path(fundamental_dir),
    )
    base = Path(root) if root is not None else Path()
    output_dir = base / authorization.output_dir
    receipt_path = base / authorization.consumption_receipt_path

    preflight_passed = False
    preflight_error: str | None = None
    scenario_execution_complete = False
    failure_stage: FailureStage = "none"
    scenario_error: str | None = None
    try:
        preflight_research(
            store=store,
            config=runtime_config,
            start=authorization.evaluation_window.evaluation_start,
            end=authorization.evaluation_window.evaluation_end,
        )
        preflight_passed = True
    except _OOS_DATA_EVALUABILITY_ERRORS as exc:
        preflight_error = str(exc)
        failure_stage = "preflight"

    scenario_results: dict[str, BacktestResult] = {}
    observed_first_signal: date | None = None
    descriptive = PortfolioOosDescriptiveSummary()
    if preflight_passed:
        # Calendar / schedule proof against the authorized store must pass before score.
        observed_first_signal = _first_signal_day(
            store=store,
            config=runtime_config,
            start=authorization.evaluation_window.evaluation_start,
            end=authorization.evaluation_window.signal_cutoff,
        )
        if (
            observed_first_signal is None
            or observed_first_signal != authorization.evaluation_window.first_2025_plus_signal
        ):
            raise ValueError(
                "observed first signal date does not match the authorized first_2025_plus_signal; "
                "refusing to score or consume"
            )
        provider: ScoreFn = score_fn or InMemoryScoreProvider(store=store, config=runtime_config)
        try:
            scenario_results, descriptive = _run_authorized_scenarios(
                store=store,
                runtime_config=runtime_config,
                authorization=authorization,
                provider=provider,
                progress=progress,
            )
            scenario_execution_complete = True
            failure_stage = "none"
        except _OOS_DATA_EVALUABILITY_ERRORS as exc:
            # Preflight really passed; seal not_evaluable without rewriting preflight flags.
            scenario_execution_complete = False
            failure_stage = "scenario"
            scenario_error = str(exc)
            scenario_results = {}
            descriptive = PortfolioOosDescriptiveSummary()

    failure_detail = preflight_error if failure_stage == "preflight" else scenario_error
    outcome, reason, gates, _largest_loss = classify_portfolio_oos_outcome(
        preflight_passed=preflight_passed,
        scenario_execution_complete=scenario_execution_complete,
        failure_stage=failure_stage,
        failure_detail=failure_detail,
        baseline=scenario_results.get(BASELINE_SCENARIO_ID),
        severe=scenario_results.get(SEVERE_SCENARIO_ID),
        initial_cash=float(authorization.hard_risk_gates.initial_cash),
        min_closed_trades=authorization.evaluability_gates.min_closed_trades,
        max_drawdown_floor=authorization.hard_risk_gates.max_drawdown_floor,
        largest_symbol_loss_fraction=(
            authorization.hard_risk_gates.largest_single_symbol_loss_fraction_of_initial_cash
        ),
        pnl_reconciliation_abs_tol=authorization.evaluability_gates.pnl_reconciliation_abs_tol,
    )

    scenarios = _scenario_artifacts_from_bound_results(
        results=scenario_results,
        initial_cash=float(authorization.hard_risk_gates.initial_cash),
    )
    if MODERATE_SCENARIO_ID in scenarios:
        gates.append(
            PortfolioOosGateResult(
                gate="moderate_cost_descriptive",
                category="descriptive",
                passed=True,
                observed=scenarios[MODERATE_SCENARIO_ID].total_return,
                threshold=None,
                decides_oos_result=False,
                note="moderate 2x/2x cost stress is descriptive only",
            )
        )

    report = PortfolioOosEvaluationReport(
        authorization_id=_require_authorization_id(authorization),
        freeze_id=authorization.freeze_id,
        strategy_path=authorization.strategy_path,
        strategy_file_sha256=authorization.strategy_file_sha256,
        strategy_config_id=authorization.strategy_config_id,
        frozen_config_hash=authorization.frozen_config_hash,
        runtime_config_hash=runtime_config.config_hash(),
        runtime_signal_anchor_date=authorization.runtime_override.runtime_value,
        runtime_config_diff=runtime_diff,
        market_snapshot_id=authorization.market_snapshot_id,
        fundamental_snapshot_id=authorization.fundamental_snapshot_id,
        fundamental_base_market_snapshot_id=authorization.fundamental_base_market_snapshot_id,
        composite_store_snapshot_id=authorization.expected_composite_store_snapshot_id,
        evaluation_start=authorization.evaluation_window.evaluation_start,
        evaluation_end=authorization.evaluation_window.evaluation_end,
        first_2025_plus_signal=authorization.evaluation_window.first_2025_plus_signal,
        signal_cutoff=authorization.evaluation_window.signal_cutoff,
        last_scheduled_exit=authorization.evaluation_window.last_scheduled_exit,
        preflight_passed=preflight_passed,
        preflight_error=preflight_error,
        scenario_execution_complete=scenario_execution_complete,
        failure_stage=failure_stage,
        scenario_error=scenario_error,
        observed_first_signal=observed_first_signal,
        scenarios=scenarios,
        gates=gates,
        descriptive=descriptive,
        outcome=outcome,
        outcome_reason=reason,
    )
    sealed, receipt = write_portfolio_oos_evaluation_atomically(
        output_dir,
        report,
        scenario_results,
        receipt_path=receipt_path,
        authorization_id=_require_authorization_id(authorization),
        freeze_id=authorization.freeze_id,
        authorization_output_dir=authorization.output_dir,
    )
    return sealed, receipt, output_dir


def _run_authorized_scenarios(
    *,
    store: MarketStore,
    runtime_config: StrategyConfig,
    authorization: PortfolioOosOneShotAuthorization,
    provider: ScoreFn,
    progress: ProgressFn | None,
) -> tuple[dict[str, BacktestResult], PortfolioOosDescriptiveSummary]:
    window = authorization.evaluation_window
    if progress is not None:
        progress("baseline", 0, 3)
    baseline_raw = run_backtest_with_signal_cutoff(
        store=store,
        config=runtime_config,
        start=window.evaluation_start,
        end=window.evaluation_end,
        signal_cutoff=window.signal_cutoff,
        score_fn=provider,
    )
    baseline = _bind_portfolio_oos_scenario_result(
        baseline_raw,
        scenario_id=BASELINE_SCENARIO_ID,
        config=runtime_config,
        baseline_config=runtime_config,
        authorization=authorization,
    )
    if progress is not None:
        progress("moderate", 1, 3)
    moderate_config = _cost_stress_config(
        runtime_config,
        scenario_id=MODERATE_SCENARIO_ID,
        commission_multiplier=SCENARIO_COST_MULTIPLIERS[MODERATE_SCENARIO_ID][0],
        slippage_multiplier=SCENARIO_COST_MULTIPLIERS[MODERATE_SCENARIO_ID][1],
    )
    moderate_raw = run_backtest_with_signal_cutoff(
        store=store,
        config=moderate_config,
        start=window.evaluation_start,
        end=window.evaluation_end,
        signal_cutoff=window.signal_cutoff,
        score_fn=provider,
    )
    moderate = _bind_portfolio_oos_scenario_result(
        moderate_raw,
        scenario_id=MODERATE_SCENARIO_ID,
        config=moderate_config,
        baseline_config=runtime_config,
        authorization=authorization,
    )
    if progress is not None:
        progress("severe", 2, 3)
    severe_config = _cost_stress_config(
        runtime_config,
        scenario_id=SEVERE_SCENARIO_ID,
        commission_multiplier=SCENARIO_COST_MULTIPLIERS[SEVERE_SCENARIO_ID][0],
        slippage_multiplier=SCENARIO_COST_MULTIPLIERS[SEVERE_SCENARIO_ID][1],
    )
    severe_raw = run_backtest_with_signal_cutoff(
        store=store,
        config=severe_config,
        start=window.evaluation_start,
        end=window.evaluation_end,
        signal_cutoff=window.signal_cutoff,
        score_fn=provider,
    )
    severe = _bind_portfolio_oos_scenario_result(
        severe_raw,
        scenario_id=SEVERE_SCENARIO_ID,
        config=severe_config,
        baseline_config=runtime_config,
        authorization=authorization,
    )
    if progress is not None:
        progress("complete", 3, 3)
    descriptive = _build_descriptive_summary(
        store=store,
        config=runtime_config,
        baseline=baseline,
        moderate=moderate,
    )
    return (
        {
            BASELINE_SCENARIO_ID: baseline,
            MODERATE_SCENARIO_ID: moderate,
            SEVERE_SCENARIO_ID: severe,
        },
        descriptive,
    )


def write_portfolio_oos_evaluation_atomically(
    output_dir: Path,
    report: PortfolioOosEvaluationReport,
    scenario_results: dict[str, BacktestResult],
    *,
    receipt_path: Path,
    authorization_id: str,
    freeze_id: str,
    authorization_output_dir: str,
) -> tuple[PortfolioOosEvaluationReport, PortfolioOosConsumptionReceipt]:
    if (
        report.ready_for_scoring
        or report.ready_for_trading
        or report.auto_deploy
        or not report.human_review_required
        or not report.one_shot
    ):
        raise ValueError("portfolio OOS evaluation report violates research boundaries")
    if report.authorization_id != authorization_id:
        raise ValueError("evaluation report authorization_id mismatch")
    if report.freeze_id != freeze_id:
        raise ValueError("evaluation report freeze_id mismatch")
    if report.outcome not in {"not_evaluable", "no_go", "conditional_go"}:
        raise ValueError("portfolio OOS outcome must be not_evaluable, no_go, or conditional_go")

    destination = Path(output_dir)
    receipt_destination = Path(receipt_path)
    if destination.exists():
        raise ValueError("one-shot portfolio OOS evaluation output already exists and is immutable; refuse overwrite")
    if receipt_destination.exists():
        raise ValueError("one-shot portfolio OOS evaluation consumption receipt already exists; refuse overwrite")

    destination.parent.mkdir(parents=True, exist_ok=True)
    receipt_destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".portfolio-oos-eval-{uuid.uuid4().hex}"
    receipt_temporary = receipt_destination.parent / f".portfolio-oos-receipt-{uuid.uuid4().hex}"
    try:
        temporary.mkdir(parents=True)
        scenarios = dict(report.scenarios)
        output_hashes: dict[str, str] = {}
        for scenario_id, result in scenario_results.items():
            artifact = scenarios.get(scenario_id)
            if artifact is None:
                raise ValueError(f"missing scenario artifact for {scenario_id}")
            result_path = temporary / artifact.result_file
            result_path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
            digest = _sha256_file(result_path)
            scenarios[scenario_id] = artifact.model_copy(update={"result_file_sha256": digest})
            output_hashes[artifact.result_file] = digest
        with_hashes = report.model_copy(update={"scenarios": scenarios})
        sealed = with_hashes.model_copy(update={"report_id": _report_id(with_hashes)})
        report_path = temporary / "report.json"
        report_path.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
        output_hashes["report.json"] = _sha256_file(report_path)
        temporary.rename(destination)

        receipt = PortfolioOosConsumptionReceipt(
            authorization_id=authorization_id,
            freeze_id=freeze_id,
            report_id=_require_report_id(sealed),
            output_dir=str(Path(authorization_output_dir)),
            output_file_sha256=output_hashes,
        )
        receipt = receipt.model_copy(update={"receipt_id": _receipt_id(receipt)})
        receipt_temporary.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
        receipt_temporary.rename(receipt_destination)
        return sealed, receipt
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if receipt_temporary.exists():
            receipt_temporary.unlink(missing_ok=True)
        if destination.exists() and not receipt_destination.exists():
            # Partial success still marks consumption via output presence; leave immutable dir.
            pass
        raise


def load_verified_portfolio_oos_evaluation(
    output_dir: Path,
) -> tuple[PortfolioOosEvaluationReport, dict[str, BacktestResult]]:
    root = Path(output_dir)
    try:
        report = PortfolioOosEvaluationReport.model_validate_json((root / "report.json").read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("portfolio OOS evaluation report is missing or invalid") from exc
    if report.report_id is None or report.report_id != _report_id(report):
        raise ValueError("portfolio OOS evaluation report ID does not match its content")
    if (
        report.ready_for_scoring
        or report.ready_for_trading
        or report.auto_deploy
        or not report.human_review_required
        or not report.one_shot
    ):
        raise ValueError("portfolio OOS evaluation report violates research boundaries")
    if report.outcome not in {"not_evaluable", "no_go", "conditional_go"}:
        raise ValueError("portfolio OOS evaluation outcome is invalid")

    results: dict[str, BacktestResult] = {}
    for scenario_id, artifact in report.scenarios.items():
        path = root / artifact.result_file
        if not path.is_file():
            raise ValueError(f"portfolio OOS scenario result missing: {artifact.result_file}")
        digest = _sha256_file(path)
        if artifact.result_file_sha256 != digest:
            raise ValueError(f"portfolio OOS scenario result hash mismatch: {artifact.result_file}")
        results[scenario_id] = BacktestResult.model_validate_json(path.read_text(encoding="utf-8"))
    return report, results


def load_verified_portfolio_oos_consumption_receipt(
    path: Path,
) -> PortfolioOosConsumptionReceipt:
    receipt_path = Path(path)
    try:
        receipt = PortfolioOosConsumptionReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("one-shot portfolio OOS consumption receipt is missing or invalid") from exc
    if receipt.receipt_id is None or receipt.receipt_id != _receipt_id(receipt):
        raise ValueError("one-shot portfolio OOS consumption receipt ID does not match its content")
    if (
        receipt.ready_for_scoring
        or receipt.ready_for_trading
        or receipt.auto_deploy
        or not receipt.human_review_required
        or not receipt.one_shot
    ):
        raise ValueError("one-shot portfolio OOS consumption receipt violates research boundaries")
    return receipt


def verify_sealed_portfolio_oos_one_shot(
    *,
    output_dir: Path,
    receipt_path: Path,
    authorization_path: Path | None = None,
    freeze_path: Path | None = None,
    project_root: Path | None = None,
) -> tuple[PortfolioOosEvaluationReport, PortfolioOosConsumptionReceipt]:
    """Read-only production verifier: committed auth/freeze, store, strategy, no score/backtest."""
    root = Path(project_root) if project_root is not None else Path.cwd()
    if authorization_path is not None:
        resolved_auth_path = Path(authorization_path)
    else:
        resolved_auth_path = root / DEFAULT_PORTFOLIO_OOS_AUTH_PATH
    default_auth_path = (root / DEFAULT_PORTFOLIO_OOS_AUTH_PATH).resolve()
    if authorization_path is not None and Path(authorization_path).resolve() != default_auth_path:
        raise ValueError(
            "--authorization-file does not match the committed portfolio OOS authorization path"
        )
    authorization = load_verified_committed_portfolio_oos_authorization(resolved_auth_path)

    resolved_freeze_path = (
        Path(freeze_path) if freeze_path is not None else root / Path(authorization.freeze_file)
    )
    default_freeze_path = (root / Path(authorization.freeze_file)).resolve()
    if freeze_path is not None and Path(freeze_path).resolve() != default_freeze_path:
        raise ValueError("--freeze-file does not match the committed authorization freeze_file")
    freeze = verify_portfolio_oos_freeze(freeze_path=resolved_freeze_path, project_root=root)
    verify_authorization_against_freeze(authorization, freeze_path=resolved_freeze_path)
    assert_committed_portfolio_oos_freeze_bindings(freeze)

    expected_output = (root / authorization.output_dir).resolve()
    expected_receipt = (root / authorization.consumption_receipt_path).resolve()
    if Path(output_dir).resolve() != expected_output:
        raise ValueError("output_dir does not match the committed authorization output_dir")
    if Path(receipt_path).resolve() != expected_receipt:
        raise ValueError("receipt_path does not match the committed authorization consumption_receipt_path")

    strategy_path = root / authorization.strategy_path
    frozen_config = _load_verified_frozen_strategy(strategy_path, authorization)
    runtime_config = _runtime_config_for_evaluation(frozen_config, authorization)
    scenario_configs = _expected_scenario_configs(runtime_config)
    store = _load_authorized_composite_store(
        authorization,
        market_dir=root / authorization.market_dir,
        fundamental_dir=root / authorization.fundamental_dir,
    )
    return verify_sealed_portfolio_oos_artifacts_against_authorization(
        output_dir=output_dir,
        receipt_path=receipt_path,
        authorization=authorization,
        freeze=freeze,
        runtime_config=runtime_config,
        scenario_configs=scenario_configs,
        store=store,
        require_calendar=True,
        require_descriptive=True,
        require_preflight_replay=True,
    )


def verify_sealed_portfolio_oos_artifacts_against_authorization(
    *,
    output_dir: Path,
    receipt_path: Path,
    authorization: PortfolioOosOneShotAuthorization,
    freeze: PortfolioOosFreezeContract,
    runtime_config: StrategyConfig | None = None,
    scenario_configs: dict[str, StrategyConfig] | None = None,
    store: MarketStore | None = None,
    require_calendar: bool = False,
    require_descriptive: bool = False,
    require_preflight_replay: bool = False,
) -> tuple[PortfolioOosEvaluationReport, PortfolioOosConsumptionReceipt]:
    """Read-only artifact verifier.

    Production path sets require_calendar/descriptive/preflight_replay and supplies store+configs.
    Synthetic tests may omit store and skip calendar-only checks; they must not weaken committed
    production requirements when those flags are enabled.
    """
    assert_authorization_self_consistent(authorization)
    report, results = load_verified_portfolio_oos_evaluation(output_dir)
    receipt = load_verified_portfolio_oos_consumption_receipt(receipt_path)
    _assert_report_matches_authorization(report, authorization, freeze)
    _assert_failure_stage_consistency(report, authorization)
    if receipt.authorization_id != report.authorization_id:
        raise ValueError("consumption receipt authorization_id does not match report")
    if receipt.freeze_id != report.freeze_id:
        raise ValueError("consumption receipt freeze_id does not match report")
    if receipt.report_id != report.report_id:
        raise ValueError("consumption receipt report_id does not match report")
    if receipt.output_dir != authorization.output_dir:
        raise ValueError("consumption receipt output_dir does not match authorization")
    expected_hashes = {"report.json": _sha256_file(Path(output_dir) / "report.json")}
    for artifact in report.scenarios.values():
        expected_hashes[artifact.result_file] = _sha256_file(Path(output_dir) / artifact.result_file)
    if receipt.output_file_sha256 != expected_hashes:
        raise ValueError("consumption receipt output hashes do not match sealed files")

    initial_cash = float(authorization.hard_risk_gates.initial_cash)
    if initial_cash != float(freeze.bound_strategy.initial_cash):
        raise ValueError("authorization initial_cash does not match freeze bound strategy")

    if runtime_config is None or scenario_configs is None:
        if require_calendar or require_descriptive or require_preflight_replay:
            raise ValueError("production verifier requires runtime and scenario configs")
    else:
        if runtime_config.config_hash() != authorization.runtime_override.expected_runtime_config_hash:
            raise ValueError("runtime config hash does not match authorization")
        if set(scenario_configs) != set(REQUIRED_SCENARIO_IDS):
            raise ValueError("scenario config map must cover baseline/moderate/severe")

    if require_preflight_replay:
        if store is None or runtime_config is None:
            raise ValueError("preflight replay requires store and runtime_config")
        try:
            preflight_research(
                store=store,
                config=runtime_config,
                start=authorization.evaluation_window.evaluation_start,
                end=authorization.evaluation_window.evaluation_end,
            )
        except _OOS_DATA_EVALUABILITY_ERRORS as exc:
            if report.preflight_passed:
                raise ValueError("read-only preflight replay failed but report.preflight_passed is true") from exc
            if report.preflight_error != str(exc):
                raise ValueError(
                    "read-only preflight replay error does not exactly match report.preflight_error"
                ) from exc
        else:
            if not report.preflight_passed:
                raise ValueError("read-only preflight replay passed but report.preflight_passed is false")
            if report.preflight_error is not None:
                raise ValueError("successful preflight replay requires report.preflight_error is None")

    if report.preflight_passed:
        if store is not None and runtime_config is not None:
            recomputed_signal = _first_signal_day(
                store=store,
                config=runtime_config,
                start=authorization.evaluation_window.evaluation_start,
                end=authorization.evaluation_window.signal_cutoff,
            )
            if recomputed_signal != report.observed_first_signal:
                raise ValueError("recomputed observed_first_signal does not match sealed report")
    elif report.observed_first_signal is not None:
        raise ValueError("preflight failure requires observed_first_signal is None")

    _assert_scenario_artifacts_match_results(
        report=report,
        results=results,
        authorization=authorization,
        initial_cash=initial_cash,
        scenario_configs=scenario_configs,
        store=store,
        require_calendar=require_calendar,
    )

    if require_descriptive:
        if store is None or runtime_config is None:
            raise ValueError("descriptive rebuild requires store and runtime_config")
        if report.scenario_execution_complete:
            baseline = results[BASELINE_SCENARIO_ID]
            moderate = results[MODERATE_SCENARIO_ID]
            rebuilt_descriptive = _build_descriptive_summary(
                store=store,
                config=runtime_config,
                baseline=baseline,
                moderate=moderate,
            )
            if rebuilt_descriptive.model_dump(mode="json") != report.descriptive.model_dump(mode="json"):
                raise ValueError("rebuilt descriptive summary does not match sealed report")
        elif report.descriptive != PortfolioOosDescriptiveSummary():
            raise ValueError("incomplete scenario execution must seal empty descriptive summary")

    failure_detail = report.preflight_error if report.failure_stage == "preflight" else report.scenario_error
    outcome, reason, gates, _loss = classify_portfolio_oos_outcome(
        preflight_passed=report.preflight_passed,
        scenario_execution_complete=report.scenario_execution_complete,
        failure_stage=report.failure_stage,
        failure_detail=failure_detail,
        baseline=results.get(BASELINE_SCENARIO_ID),
        severe=results.get(SEVERE_SCENARIO_ID),
        initial_cash=initial_cash,
        min_closed_trades=authorization.evaluability_gates.min_closed_trades,
        max_drawdown_floor=authorization.hard_risk_gates.max_drawdown_floor,
        largest_symbol_loss_fraction=(
            authorization.hard_risk_gates.largest_single_symbol_loss_fraction_of_initial_cash
        ),
        pnl_reconciliation_abs_tol=authorization.evaluability_gates.pnl_reconciliation_abs_tol,
    )
    recomputed_gates = list(gates)
    if set(results) == set(REQUIRED_SCENARIO_IDS) and MODERATE_SCENARIO_ID in results:
        recomputed_gates.append(
            PortfolioOosGateResult(
                gate="moderate_cost_descriptive",
                category="descriptive",
                passed=True,
                observed=results[MODERATE_SCENARIO_ID].metrics.total_return,
                threshold=None,
                decides_oos_result=False,
                note="moderate 2x/2x cost stress is descriptive only",
            )
        )
    if outcome != report.outcome:
        raise ValueError("recomputed portfolio OOS outcome does not match sealed report")
    if reason != report.outcome_reason:
        raise ValueError("recomputed portfolio OOS outcome_reason does not match sealed report")
    if [gate.model_dump(mode="json") for gate in recomputed_gates] != [
        gate.model_dump(mode="json") for gate in report.gates
    ]:
        raise ValueError("recomputed full gate list does not match sealed report")
    return report, receipt


def _expected_scenario_configs(runtime_config: StrategyConfig) -> dict[str, StrategyConfig]:
    return {
        BASELINE_SCENARIO_ID: runtime_config,
        MODERATE_SCENARIO_ID: _cost_stress_config(
            runtime_config,
            scenario_id=MODERATE_SCENARIO_ID,
            commission_multiplier=SCENARIO_COST_MULTIPLIERS[MODERATE_SCENARIO_ID][0],
            slippage_multiplier=SCENARIO_COST_MULTIPLIERS[MODERATE_SCENARIO_ID][1],
        ),
        SEVERE_SCENARIO_ID: _cost_stress_config(
            runtime_config,
            scenario_id=SEVERE_SCENARIO_ID,
            commission_multiplier=SCENARIO_COST_MULTIPLIERS[SEVERE_SCENARIO_ID][0],
            slippage_multiplier=SCENARIO_COST_MULTIPLIERS[SEVERE_SCENARIO_ID][1],
        ),
    }


def _assert_failure_stage_consistency(
    report: PortfolioOosEvaluationReport,
    authorization: PortfolioOosOneShotAuthorization,
) -> None:
    """Allow only the three states the evaluator can actually produce."""
    first_signal = authorization.evaluation_window.first_2025_plus_signal
    if report.failure_stage == "preflight":
        if report.preflight_passed:
            raise ValueError("preflight failure must set preflight_passed=false")
        if report.scenario_execution_complete:
            raise ValueError("preflight failure must set scenario_execution_complete=false")
        if not report.preflight_error:
            raise ValueError("preflight failure requires non-empty preflight_error")
        if report.scenario_error is not None:
            raise ValueError("preflight failure must not set scenario_error")
        if report.observed_first_signal is not None:
            raise ValueError("preflight failure requires observed_first_signal is None")
        if report.scenarios:
            raise ValueError("preflight failure must seal empty scenarios")
        return
    if report.failure_stage == "scenario":
        if not report.preflight_passed:
            raise ValueError("scenario failure must keep preflight_passed=true")
        if report.scenario_execution_complete:
            raise ValueError("scenario failure must set scenario_execution_complete=false")
        if report.preflight_error is not None:
            raise ValueError("scenario failure must not set preflight_error")
        if not report.scenario_error:
            raise ValueError("scenario failure requires non-empty scenario_error")
        if report.observed_first_signal != first_signal:
            raise ValueError("scenario failure observed_first_signal must equal authorized first signal")
        if report.scenarios:
            raise ValueError("scenario failure must seal empty scenarios")
        return
    if report.failure_stage == "none":
        if not report.preflight_passed:
            raise ValueError("success state cannot have preflight_passed=false")
        if not report.scenario_execution_complete:
            raise ValueError("success state requires scenario_execution_complete=true")
        if report.preflight_error is not None:
            raise ValueError("success state must not set preflight_error")
        if report.scenario_error is not None:
            raise ValueError("success state must not set scenario_error")
        if report.observed_first_signal is None:
            raise ValueError("success state requires non-empty observed_first_signal")
        if report.observed_first_signal != first_signal:
            raise ValueError("success observed_first_signal must equal authorized first signal")
        if set(report.scenarios) != set(REQUIRED_SCENARIO_IDS):
            raise ValueError("success state requires exact baseline/moderate/severe scenarios")
        return
    raise ValueError(f"unknown failure_stage: {report.failure_stage}")


def _assert_report_matches_authorization(
    report: PortfolioOosEvaluationReport,
    authorization: PortfolioOosOneShotAuthorization,
    freeze: PortfolioOosFreezeContract,
) -> None:
    if report.authorization_id != _require_authorization_id(authorization):
        raise ValueError("report authorization_id does not match authorization")
    if report.freeze_id != authorization.freeze_id or report.freeze_id != freeze.freeze_id:
        raise ValueError("report freeze_id does not match authorization/freeze")
    if report.strategy_path != authorization.strategy_path:
        raise ValueError("report strategy_path does not match authorization")
    if report.strategy_file_sha256 != authorization.strategy_file_sha256:
        raise ValueError("report strategy_file_sha256 does not match authorization")
    if report.strategy_config_id != authorization.strategy_config_id:
        raise ValueError("report strategy_config_id does not match authorization")
    if report.frozen_config_hash != authorization.frozen_config_hash:
        raise ValueError("report frozen_config_hash does not match authorization")
    if report.runtime_config_hash != authorization.runtime_override.expected_runtime_config_hash:
        raise ValueError("report runtime_config_hash does not match authorization")
    if report.runtime_signal_anchor_date != authorization.runtime_override.runtime_value:
        raise ValueError("report runtime_signal_anchor_date does not match authorization")
    if report.runtime_config_diff != ["trade.signal_anchor_date"]:
        raise ValueError("report runtime_config_diff is not the authorized anchor-only diff")
    if report.market_snapshot_id != authorization.market_snapshot_id:
        raise ValueError("report market_snapshot_id does not match authorization")
    if report.fundamental_snapshot_id != authorization.fundamental_snapshot_id:
        raise ValueError("report fundamental_snapshot_id does not match authorization")
    if report.fundamental_base_market_snapshot_id != authorization.fundamental_base_market_snapshot_id:
        raise ValueError("report fundamental_base_market_snapshot_id does not match authorization")
    if report.composite_store_snapshot_id != authorization.expected_composite_store_snapshot_id:
        raise ValueError("report composite_store_snapshot_id does not match authorization")
    window = authorization.evaluation_window
    if (
        report.evaluation_start != window.evaluation_start
        or report.evaluation_end != window.evaluation_end
        or report.first_2025_plus_signal != window.first_2025_plus_signal
        or report.signal_cutoff != window.signal_cutoff
        or report.last_scheduled_exit != window.last_scheduled_exit
    ):
        raise ValueError("report evaluation window does not match authorization")


def _assert_scenario_artifacts_match_results(
    *,
    report: PortfolioOosEvaluationReport,
    results: dict[str, BacktestResult],
    authorization: PortfolioOosOneShotAuthorization,
    initial_cash: float,
    scenario_configs: dict[str, StrategyConfig] | None,
    store: MarketStore | None,
    require_calendar: bool,
) -> None:
    if set(report.scenarios) != set(results):
        raise ValueError("report scenarios keys do not match on-disk BacktestResult set")
    if results and set(results) != set(REQUIRED_SCENARIO_IDS):
        raise ValueError("sealed scenario set must be empty or exact baseline/moderate/severe")
    if report.scenario_execution_complete and set(results) != set(REQUIRED_SCENARIO_IDS):
        raise ValueError("complete scenario execution must seal baseline/moderate/severe")
    rebuilt = _scenario_artifacts_from_bound_results(results=results, initial_cash=initial_cash)
    for scenario_id, artifact in report.scenarios.items():
        expected = rebuilt.get(scenario_id)
        if expected is None:
            raise ValueError(f"missing rebuilt scenario artifact for {scenario_id}")
        left = artifact.model_dump(mode="json", exclude={"result_file", "result_file_sha256"})
        right = expected.model_dump(mode="json", exclude={"result_file", "result_file_sha256"})
        if left != right:
            raise ValueError(f"rebuilt scenario artifact mismatch for {scenario_id}")
        if artifact.result_file != SCENARIO_RESULT_FILES[scenario_id]:
            raise ValueError(f"scenario result_file drifted for {scenario_id}")
        expected_config = scenario_configs.get(scenario_id) if scenario_configs is not None else None
        _assert_result_bindings(
            results[scenario_id],
            scenario_id=scenario_id,
            authorization=authorization,
            initial_cash=initial_cash,
            expected_config=expected_config,
            store=store,
            require_calendar=require_calendar,
        )
    if BASELINE_SCENARIO_ID in results:
        baseline_rate = results[BASELINE_SCENARIO_ID].portfolio_oos_commission_rate
        baseline_slip = results[BASELINE_SCENARIO_ID].portfolio_oos_slippage_bps
        baseline_min = results[BASELINE_SCENARIO_ID].portfolio_oos_minimum_commission
        if baseline_rate is None or baseline_slip is None or baseline_min is None:
            raise ValueError("baseline scenario is missing cost bindings")
        for scenario_id, result in results.items():
            commission_mult, slippage_mult = SCENARIO_COST_MULTIPLIERS[scenario_id]
            expected_commission = baseline_rate * commission_mult
            expected_slippage = baseline_slip * slippage_mult
            if result.portfolio_oos_commission_rate != expected_commission:
                raise ValueError(f"scenario commission_rate drift for {scenario_id}")
            if result.portfolio_oos_slippage_bps != expected_slippage:
                raise ValueError(f"scenario slippage_bps drift for {scenario_id}")
            if result.portfolio_oos_minimum_commission != baseline_min:
                raise ValueError(f"scenario minimum_commission drift for {scenario_id}")
            if result.portfolio_oos_stamp_tax_unchanged is not True:
                raise ValueError(f"scenario stamp_tax changed for {scenario_id}")


def _assert_result_bindings(
    result: BacktestResult,
    *,
    scenario_id: str,
    authorization: PortfolioOosOneShotAuthorization,
    initial_cash: float,
    expected_config: StrategyConfig | None,
    store: MarketStore | None,
    require_calendar: bool,
) -> None:
    if result.portfolio_oos_scenario_id != scenario_id:
        raise ValueError(f"BacktestResult scenario_id binding mismatch for {scenario_id}")
    if result.portfolio_oos_commission_rate is None:
        raise ValueError(f"BacktestResult missing commission_rate binding for {scenario_id}")
    if result.portfolio_oos_minimum_commission is None:
        raise ValueError(f"BacktestResult missing minimum_commission binding for {scenario_id}")
    if result.portfolio_oos_slippage_bps is None:
        raise ValueError(f"BacktestResult missing slippage_bps binding for {scenario_id}")
    if result.portfolio_oos_stamp_tax_unchanged is not True:
        raise ValueError(f"BacktestResult stamp_tax_unchanged binding missing for {scenario_id}")
    window = authorization.evaluation_window
    if result.start != window.evaluation_start or result.end != window.evaluation_end:
        raise ValueError(f"BacktestResult start/end drift for {scenario_id}")
    if (
        result.window.start != window.evaluation_start
        or result.window.signal_end != window.signal_cutoff
        or result.window.entry_end != window.evaluation_end
        or result.window.valuation_end != window.evaluation_end
    ):
        raise ValueError(f"BacktestResult window binding drift for {scenario_id}")
    if not result.data_snapshot_id:
        raise ValueError(f"BacktestResult data_snapshot_id must be non-empty for {scenario_id}")
    if result.data_snapshot_id != authorization.expected_composite_store_snapshot_id:
        raise ValueError(f"BacktestResult data_snapshot_id drift for {scenario_id}")
    if result.metrics.initial_capital != initial_cash:
        raise ValueError(f"BacktestResult initial_capital drift for {scenario_id}")
    if expected_config is not None:
        if result.strategy_config_hash != expected_config.config_hash():
            raise ValueError(f"BacktestResult strategy_config_hash drift for {scenario_id}")
        if result.strategy_name != expected_config.name:
            raise ValueError(f"BacktestResult strategy_name drift for {scenario_id}")
        if result.strategy_version != expected_config.version:
            raise ValueError(f"BacktestResult strategy_version drift for {scenario_id}")
        if result.research_scope != expected_config.research_scope:
            raise ValueError(f"BacktestResult research_scope drift for {scenario_id}")
        if result.portfolio_oos_commission_rate != expected_config.costs.commission_rate:
            raise ValueError(f"BacktestResult commission_rate != expected config for {scenario_id}")
        if result.portfolio_oos_minimum_commission != expected_config.costs.min_commission:
            raise ValueError(f"BacktestResult min_commission != expected config for {scenario_id}")
        if result.portfolio_oos_slippage_bps != expected_config.costs.slippage_bps:
            raise ValueError(f"BacktestResult slippage_bps != expected config for {scenario_id}")

    if result.metrics.number_of_trades != len(result.trades):
        raise ValueError(f"BacktestResult number_of_trades != len(trades) for {scenario_id}")
    if not result.equity_curve:
        raise ValueError(f"BacktestResult equity_curve is empty for {scenario_id}")
    curve_dates = [point.date for point in result.equity_curve]
    if any(left >= right for left, right in zip(curve_dates, curve_dates[1:], strict=False)):
        raise ValueError(f"BacktestResult equity_curve dates must strictly increase for {scenario_id}")
    if require_calendar:
        if store is None:
            raise ValueError("calendar equity-curve check requires store")
        expected_calendar = store.get_calendar(window.evaluation_start, window.evaluation_end)
        if curve_dates != expected_calendar:
            raise ValueError(
                f"BacktestResult equity_curve dates must equal authorized market calendar for {scenario_id}"
            )

    recomputed_metrics = compute_metrics(
        initial_cash,
        result.trades,
        result.equity_curve,
        result.start,
        result.end,
    )
    if recomputed_metrics.model_dump(mode="json") != result.metrics.model_dump(mode="json"):
        raise ValueError(f"BacktestResult metrics do not recompute from trades/equity for {scenario_id}")
    recomputed_attribution = compute_attribution(result.trades, result.attribution.signal)
    if recomputed_attribution.model_dump(mode="json") != result.attribution.model_dump(mode="json"):
        raise ValueError(f"BacktestResult attribution does not recompute from trades for {scenario_id}")
    _ = (
        _pnl_reconciliation_error(result, initial_cash),
        _largest_symbol_loss_fraction(result, initial_cash),
    )


def _load_verified_frozen_strategy(
    strategy_path: Path,
    authorization: PortfolioOosOneShotAuthorization,
) -> StrategyConfig:
    path = Path(strategy_path)
    if not path.is_file():
        raise ValueError("frozen strategy YAML is missing")
    digest = _sha256_file(path)
    if digest != authorization.strategy_file_sha256:
        raise ValueError("strategy file SHA-256 does not match the authorization contract")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("frozen strategy YAML is invalid")
    config = StrategyConfig.model_validate(payload)
    if config.config_id != authorization.strategy_config_id:
        raise ValueError("strategy config_id does not match the authorization contract")
    if config.config_hash() != authorization.frozen_config_hash:
        raise ValueError("strategy config_hash does not match the authorization contract")
    return config


def _load_authorized_composite_store(
    authorization: PortfolioOosOneShotAuthorization,
    *,
    market_dir: Path,
    fundamental_dir: Path,
) -> MarketStore:
    market = load_verified_snapshot(Path(market_dir))
    if market.snapshot_id != authorization.market_snapshot_id:
        raise ValueError("OOS market snapshot_id does not match the authorization contract")
    fundamental, tables = load_verified_fundamental_snapshot(Path(fundamental_dir))
    if fundamental.snapshot_id != authorization.fundamental_snapshot_id:
        raise ValueError("OOS fundamental snapshot_id does not match the authorization contract")
    if fundamental.base_market_snapshot_id != authorization.fundamental_base_market_snapshot_id:
        raise ValueError("OOS fundamental base_market_snapshot_id does not match authorization")
    if fundamental.base_market_snapshot_id != market.snapshot_id:
        raise ValueError("fundamental overlay is bound to a different market snapshot")
    store = FundamentalOverlayStore(
        DuckDBParquetStore(Path(market_dir), snapshot=market),
        fundamental,
        tables,
    )
    composite = store.snapshot().snapshot_id
    expected = sha256_text(
        f"market_snapshot_id={market.snapshot_id}\nfundamental_snapshot_id={fundamental.snapshot_id}\n"
    )
    if composite != expected or composite != authorization.expected_composite_store_snapshot_id:
        raise ValueError("composite store snapshot_id does not match the authorization contract")
    return store


def _scenario_artifacts_from_bound_results(
    *,
    results: dict[str, BacktestResult],
    initial_cash: float,
) -> dict[str, PortfolioOosScenarioArtifact]:
    artifacts: dict[str, PortfolioOosScenarioArtifact] = {}
    for scenario_id, result in results.items():
        if scenario_id not in SCENARIO_RESULT_FILES:
            raise ValueError(f"unknown portfolio OOS scenario_id: {scenario_id}")
        artifacts[scenario_id] = _artifact_from_bound_result(
            scenario_id=scenario_id,
            result_file=SCENARIO_RESULT_FILES[scenario_id],
            result=result,
            initial_cash=initial_cash,
        )
    return artifacts


def _bind_portfolio_oos_scenario_result(
    result: BacktestResult,
    *,
    scenario_id: str,
    config: StrategyConfig,
    baseline_config: StrategyConfig,
    authorization: PortfolioOosOneShotAuthorization,
) -> BacktestResult:
    stamp_unchanged = (
        config.costs.stamp_tax_rate == baseline_config.costs.stamp_tax_rate
        and config.costs.stamp_tax_schedule == baseline_config.costs.stamp_tax_schedule
    )
    if not stamp_unchanged:
        raise ValueError(f"cost stress scenario {scenario_id} changed stamp tax")
    if config.costs.min_commission != baseline_config.costs.min_commission:
        raise ValueError(f"cost stress scenario {scenario_id} changed minimum commission")
    window = authorization.evaluation_window
    if result.start != window.evaluation_start or result.end != window.evaluation_end:
        raise ValueError(f"scenario {scenario_id} window does not match authorization")
    if result.strategy_config_hash != config.config_hash():
        raise ValueError(f"scenario {scenario_id} strategy_config_hash does not match config")
    if (
        result.data_snapshot_id
        and result.data_snapshot_id != authorization.expected_composite_store_snapshot_id
    ):
        raise ValueError(f"scenario {scenario_id} data_snapshot_id does not match authorization")
    authorized_window = BacktestWindow(
        start=window.evaluation_start,
        signal_end=window.signal_cutoff,
        entry_end=window.evaluation_end,
        valuation_end=window.evaluation_end,
    )
    return result.model_copy(
        update={
            "strategy_name": config.name,
            "strategy_version": config.version,
            "strategy_config_hash": config.config_hash(),
            "start": window.evaluation_start,
            "end": window.evaluation_end,
            "window": authorized_window,
            "data_snapshot_id": authorization.expected_composite_store_snapshot_id,
            "research_scope": config.research_scope,
            "research_notice": research_notice(config.research_scope),
            "portfolio_oos_scenario_id": scenario_id,
            "portfolio_oos_commission_rate": config.costs.commission_rate,
            "portfolio_oos_minimum_commission": config.costs.min_commission,
            "portfolio_oos_slippage_bps": config.costs.slippage_bps,
            "portfolio_oos_stamp_tax_unchanged": True,
        }
    )


def _artifact_from_bound_result(
    *,
    scenario_id: str,
    result_file: str,
    result: BacktestResult,
    initial_cash: float,
) -> PortfolioOosScenarioArtifact:
    if result.portfolio_oos_scenario_id != scenario_id:
        raise ValueError(f"missing or mismatched portfolio_oos_scenario_id for {scenario_id}")
    if (
        result.portfolio_oos_commission_rate is None
        or result.portfolio_oos_minimum_commission is None
        or result.portfolio_oos_slippage_bps is None
        or result.portfolio_oos_stamp_tax_unchanged is None
    ):
        raise ValueError(f"BacktestResult missing portfolio OOS cost bindings for {scenario_id}")
    return PortfolioOosScenarioArtifact(
        scenario_id=scenario_id,
        result_file=result_file,
        commission_rate=result.portfolio_oos_commission_rate,
        minimum_commission=result.portfolio_oos_minimum_commission,
        slippage_bps=result.portfolio_oos_slippage_bps,
        stamp_tax_unchanged=result.portfolio_oos_stamp_tax_unchanged,
        total_return=result.metrics.total_return,
        sharpe_ratio=result.metrics.sharpe_ratio,
        max_drawdown=result.metrics.max_drawdown,
        number_of_trades=result.metrics.number_of_trades,
        open_positions_at_end=result.open_positions_at_end,
        final_equity=result.metrics.final_equity,
        total_trading_costs=result.attribution.total_trading_costs,
        pnl_reconciliation_error=_pnl_reconciliation_error(result, initial_cash),
        largest_symbol_loss_to_initial_cash=_largest_symbol_loss_fraction(result, initial_cash),
    )


def _build_descriptive_summary(
    *,
    store: MarketStore,
    config: StrategyConfig,
    baseline: BacktestResult,
    moderate: BacktestResult,
) -> PortfolioOosDescriptiveSummary:
    instruments = {item.symbol: item for item in store.get_instruments()}
    symbols = _symbol_attribution(baseline.trades, instruments, config.portfolio.initial_cash)
    sectors = _sector_attribution(symbols, config.portfolio.initial_cash)
    benchmark = evaluate_price_index_benchmark(
        store=store,
        symbol=config.data.market_index,
        start=baseline.start,
        end=baseline.end,
    )
    return PortfolioOosDescriptiveSummary(
        benchmark=benchmark,
        return_minus_benchmark=baseline.metrics.total_return - benchmark.total_return,
        exposure=_exposure_summary(baseline),
        sectors=sectors,
        moderate_total_return=moderate.metrics.total_return,
    )


def _first_signal_day(
    *,
    store: MarketStore,
    config: StrategyConfig,
    start: date,
    end: date,
) -> date | None:
    anchor = config.trade.signal_anchor_date
    if anchor is None:
        raise ValueError("runtime config is missing signal_anchor_date")
    schedule = store.get_calendar(anchor, end)
    if not schedule or schedule[0] != anchor:
        raise ValueError(f"signal_anchor_date {anchor} is not a trading day in the snapshot")
    interval = config.trade.signal_interval_days
    for day in schedule[::interval]:
        if start <= day <= end:
            return day
    return None


def _pnl_reconciliation_error(result: BacktestResult, initial_cash: float) -> float:
    net_trade_pnl = sum(trade.pnl for trade in result.trades)
    equity_change = result.metrics.final_equity - initial_cash
    return equity_change - net_trade_pnl


def _largest_symbol_loss_fraction(result: BacktestResult, initial_cash: float) -> float:
    symbols = _symbol_attribution(result.trades, {}, initial_cash)
    losses = sorted((-item.net_pnl for item in symbols if item.net_pnl < 0), reverse=True)
    return (losses[0] / initial_cash) if losses else 0.0


def _required_metrics_finite(metrics: Any) -> bool:
    values = [
        metrics.total_return,
        metrics.final_equity,
        metrics.number_of_trades,
        metrics.sharpe_ratio,
        metrics.max_drawdown,
    ]
    for value in values:
        if value is None:
            return False
        if isinstance(value, float) and not math.isfinite(value):
            return False
    return True


def _walk_diff(left: Any, right: Any, *, prefix: str) -> list[str]:
    if type(left) is not type(right):
        return [prefix or "<root>"]
    if isinstance(left, dict):
        keys = set(left) | set(right)
        out: list[str] = []
        for key in sorted(keys):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                out.append(path)
            else:
                out.extend(_walk_diff(left[key], right[key], prefix=path))
        return out
    if isinstance(left, list):
        if len(left) != len(right):
            return [prefix or "<root>"]
        out = []
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=True)):
            path = f"{prefix}[{index}]"
            out.extend(_walk_diff(left_item, right_item, prefix=path))
        return out
    if left != right:
        return [prefix or "<root>"]
    return []


def _require_authorization_id(authorization: PortfolioOosOneShotAuthorization) -> str:
    if authorization.authorization_id is None:
        raise ValueError("authorization_id is missing")
    return authorization.authorization_id


def _require_report_id(report: PortfolioOosEvaluationReport) -> str:
    if report.report_id is None:
        raise ValueError("report_id is missing")
    return report.report_id


def _report_id(report: PortfolioOosEvaluationReport) -> str:
    payload = report.model_dump(mode="json", exclude={"report_id"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _receipt_id(receipt: PortfolioOosConsumptionReceipt) -> str:
    payload = receipt.model_dump(mode="json", exclude={"receipt_id"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Keep PreflightResult available for typed preflight assertions in tests.
__all__ = [
    "BASELINE_SCENARIO_ID",
    "InMemoryScoreProvider",
    "MODERATE_SCENARIO_ID",
    "PORTFOLIO_OOS_EVAL_VERSION",
    "PORTFOLIO_OOS_RECEIPT_VERSION",
    "PortfolioOosConsumptionReceipt",
    "PortfolioOosEvaluationReport",
    "PortfolioOosGateResult",
    "REQUIRED_SCENARIO_IDS",
    "SEVERE_SCENARIO_ID",
    "build_runtime_equivalent_config",
    "canonical_config_diff",
    "classify_portfolio_oos_outcome",
    "evaluate_and_write_portfolio_oos_one_shot",
    "load_verified_portfolio_oos_consumption_receipt",
    "load_verified_portfolio_oos_evaluation",
    "verify_sealed_portfolio_oos_artifacts_against_authorization",
    "verify_sealed_portfolio_oos_one_shot",
    "write_portfolio_oos_evaluation_atomically",
]
