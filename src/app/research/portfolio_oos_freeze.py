from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl
import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.models.config import StrategyConfig
from app.models.fundamentals import (
    FUNDAMENTAL_SCHEMA_VERSION,
    FundamentalSnapshot,
)
from app.models.snapshot import SCHEMA_VERSION, DataSnapshot
from app.storage.fundamental_io import (
    REPORT_AVAILABILITY_POLICY,
    VALUATION_AVAILABILITY_POLICY,
    _combine_fundamental_hashes,
)
from app.storage.hashing import combine_hashes, hash_table

PORTFOLIO_OOS_FREEZE_SCHEMA_VERSION: Literal["1"] = "1"
PORTFOLIO_OOS_FREEZE_VERSION: Literal["all-a-share-portfolio-oos-freeze-v1"] = "all-a-share-portfolio-oos-freeze-v1"
DEFAULT_PORTFOLIO_OOS_FREEZE_PATH = Path("config/research/all-a-share-portfolio-oos-freeze-v1.json")

FROZEN_STRATEGY_PATH = "config/strategies/all_a_share_historical_value_portfolio_selected_v2.yaml"
FROZEN_STRATEGY_CONFIG_ID = "all_a_share_historical_value_quality_v1_portfolio_p10_h20_selected_v2"
FROZEN_STRATEGY_FILE_SHA256 = "ba39935d0329f7c2354990f3875e55945a0c348f4eaa829f4e0fe6ae50597e26"
FROZEN_CONFIG_HASH = "796b793856dcd02a"
FROZEN_CANDIDATE_ID: Literal["p10_h20"] = "p10_h20"
FROZEN_INITIAL_CASH: Literal[80000] = 80000
FROZEN_MAX_POSITIONS: Literal[10] = 10
FROZEN_WEIGHTING: Literal["equal_weight"] = "equal_weight"
FROZEN_EXIT_POLICY: Literal["fixed_horizon"] = "fixed_horizon"
FROZEN_HORIZON_DAYS: Literal[20] = 20
FROZEN_SIGNAL_INTERVAL_DAYS: Literal[20] = 20
FROZEN_SIGNAL_ANCHOR_DATE = date(2022, 1, 4)
FROZEN_MARKET_GATE_MAX_NEW_POSITIONS: tuple[Literal[0], Literal[3], Literal[7], Literal[10]] = (
    0,
    3,
    7,
    10,
)

FROZEN_SELECTION_REPORT_PATH = "data/all-a-share-historical-v1/portfolio-construction-v2.json"
FROZEN_SELECTION_REPORT_SHA256 = "9442a71b50502f9557f26d0238cc6c198c7905542ab408558e4a7a9225866621"
FROZEN_ROBUSTNESS_REPORT_PATH = "data/all-a-share-historical-v1/frozen-portfolio-robustness-v2.json"
FROZEN_ROBUSTNESS_REPORT_SHA256 = "c0fa17a2b6a90f89b358ba48e9140c17bae292e9ea1ef055c38bff5041ba6512"
FROZEN_ROBUSTNESS_STATUS: Literal["CONDITIONAL_GO"] = "CONDITIONAL_GO"
FROZEN_DEVELOPMENT_DATA_SNAPSHOT_ID = "cf3a6e5ba108e61bbf899dcf34cb2b5acfb6f098a56b30213720c1bba2db9a11"

FROZEN_DEVELOPMENT_MARKET_DIR = "data/all-a-share-historical-v1/parquet"
FROZEN_DEVELOPMENT_MARKET_SNAPSHOT_ID = "de546fbbf5a6308a76fbfbd077a918cbbedfb3ad0ca361a24212c1bfe3e06857"
FROZEN_DEVELOPMENT_MARKET_CALENDAR_TABLE_HASH = "944af02ee4bedd8745bc64350aeebb99de3916b48fe825ffb1ffba6833817c41"
FROZEN_DEVELOPMENT_FUNDAMENTAL_DIR = "data/all-a-share-historical-v1/fundamentals-value-quality-v1"
FROZEN_DEVELOPMENT_FUNDAMENTAL_SNAPSHOT_ID = "6a3406cb5424f6d86005ba1b5a571a27872e24accb2b2457b24187ac5ad425d3"

FROZEN_OOS_MARKET_DIR = "data/all-a-share-oos-20241001-20260821-v1/parquet"
FROZEN_OOS_MARKET_SNAPSHOT_ID = "b6f664d31d8ffcdabbb655e888467c75dbfa6a7f8bd863d698febb015f5b0427"
FROZEN_OOS_MARKET_CALENDAR_TABLE_HASH = "1202a9fee574f2a9ade3f38a5a298b95f215e8edecc5c3465db75d669a93a560"
FROZEN_OOS_FUNDAMENTAL_DIR = "data/all-a-share-oos-20241001-20260821-v1/fundamentals-value-quality-v1"
FROZEN_OOS_FUNDAMENTAL_SNAPSHOT_ID = "6ae37b22c6884e81d3ddeb18f47e62911a21d3ea63db4acfb48e22644bc74bd9"
FROZEN_OOS_COVERAGE_START = date(2024, 10, 8)
FROZEN_OOS_COVERAGE_END = date(2026, 8, 21)

FROZEN_CALENDAR_OVERLAP_START = date(2024, 10, 8)
FROZEN_CALENDAR_OVERLAP_END = date(2024, 12, 31)
FROZEN_CALENDAR_OVERLAP_TRADING_DAYS: Literal[61] = 61
FROZEN_RUNTIME_EQUIVALENT_ANCHOR = date(2024, 10, 29)
FROZEN_FIRST_2025_PLUS_SIGNAL = date(2025, 1, 22)
FROZEN_LAST_COMPLETE_SIGNAL = date(2026, 7, 22)
FROZEN_LAST_SCHEDULED_EXIT = date(2026, 8, 20)

FROZEN_EVALUATION_START = date(2025, 1, 2)
FROZEN_EVALUATION_END = date(2026, 8, 21)
FROZEN_SIGNAL_CUTOFF = date(2026, 7, 22)

FROZEN_MIN_CLOSED_TRADES: Literal[20] = 20
FROZEN_MAX_DRAWDOWN_FLOOR: float = -0.15
FROZEN_LARGEST_SYMBOL_LOSS_FRACTION: float = 0.03
FROZEN_PNL_RECONCILIATION_ABS_TOL: float = 1e-6

RESEARCH_BOUNDARY = (
    "Development-only freeze for the first future authorized 2025+ one-shot OOS evaluation "
    "of frozen p10_h20. No preflight, score, IC, backtest, return, trade, portfolio result, "
    "authorization, evaluation output, or consumption receipt is created by this freeze. "
    "runtime_equivalent_anchor allows schedule equivalence only; every other strategy "
    "parameter remains locked. Never auto-promote to scoring, paper trading, or live trading."
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BoundStrategyEvidence(_StrictModel):
    strategy_path: str = Field(min_length=1)
    strategy_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_config_id: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    candidate_id: Literal["p10_h20"] = FROZEN_CANDIDATE_ID
    initial_cash: Literal[80000] = FROZEN_INITIAL_CASH
    max_positions: Literal[10] = FROZEN_MAX_POSITIONS
    weighting: Literal["equal_weight"] = FROZEN_WEIGHTING
    exit_policy: Literal["fixed_horizon"] = FROZEN_EXIT_POLICY
    fixed_horizon_days: Literal[20] = FROZEN_HORIZON_DAYS
    signal_interval_days: Literal[20] = FROZEN_SIGNAL_INTERVAL_DAYS
    signal_anchor_date: date
    market_gate_max_new_positions: list[Literal[0, 3, 7, 10]]


class BoundSelectionEvidence(_StrictModel):
    report_path: str = Field(min_length=1)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_candidate_id: Literal["p10_h20"] = FROZEN_CANDIDATE_ID
    selected_config_hash: str = Field(min_length=1)
    data_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class BoundRobustnessEvidence(_StrictModel):
    report_path: str = Field(min_length=1)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_config_hash: str = Field(min_length=1)
    status: Literal["CONDITIONAL_GO"] = FROZEN_ROBUSTNESS_STATUS
    data_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class BoundDevelopmentSnapshots(_StrictModel):
    market_dir: str = Field(min_length=1)
    market_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_calendar_table_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    fundamental_dir: str = Field(min_length=1)
    fundamental_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    development_data_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class BoundOosDataBinding(_StrictModel):
    market_dir: str = Field(min_length=1)
    market_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_calendar_table_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_coverage_start: date
    market_coverage_end: date
    fundamental_dir: str = Field(min_length=1)
    fundamental_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    fundamental_base_market_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    fundamental_coverage_start: date
    fundamental_coverage_end: date


LAST_SCHEDULED_EXIT_SEMANTICS = (
    "last_scheduled_exit is the scheduled unblocked fixed-horizon liquidation date under "
    "BacktestEngine lifecycle: next-trading-day entry after the signal, entry session not "
    "exit-eligible, then exit on the horizon-th subsequent eligible session. It is not "
    "signal+horizon. Suspended or limit-down sessions may leave positions open past this "
    "date; open_positions_at_end!=0 then triggers not_evaluable under the existing gate."
)


class CalendarEquivalenceProof(_StrictModel):
    overlap_start: date
    overlap_end: date
    overlap_trading_days: Literal[61] = FROZEN_CALENDAR_OVERLAP_TRADING_DAYS
    original_signal_anchor_date: date
    signal_interval_days: Literal[20] = FROZEN_SIGNAL_INTERVAL_DAYS
    runtime_equivalent_anchor: date
    first_2025_plus_signal: date
    last_complete_signal: date
    last_scheduled_exit: date
    equivalence_scope: Literal["schedule_only"] = "schedule_only"
    note: str = (
        "runtime_equivalent_anchor only authorizes calendar-equivalent signal scheduling "
        "because the OOS snapshot lacks the original 2022-01-04 anchor. Every other frozen "
        "strategy parameter must remain unchanged. " + LAST_SCHEDULED_EXIT_SEMANTICS
    )


class EvaluationWindow(_StrictModel):
    evaluation_start: date
    evaluation_end: date
    signal_cutoff: date
    last_scheduled_exit: date
    last_scheduled_exit_semantics: str = LAST_SCHEDULED_EXIT_SEMANTICS


class PrimaryOosEndpoint(_StrictModel):
    metric: Literal["total_return_after_declared_and_realized_trading_costs"] = (
        "total_return_after_declared_and_realized_trading_costs"
    )
    comparator: Literal[">"] = ">"
    threshold: Literal[0] = 0
    decides_oos_result: Literal[True] = True
    may_promote_to_scoring: Literal[False] = False
    may_promote_to_trading: Literal[False] = False


class EvaluabilityGates(_StrictModel):
    require_full_preflight_after_future_authorization: Literal[True] = True
    min_closed_trades: Literal[20] = FROZEN_MIN_CLOSED_TRADES
    open_positions_at_end: Literal[0] = 0
    all_metrics_finite: Literal[True] = True
    pnl_reconciliation_abs_tol: float = FROZEN_PNL_RECONCILIATION_ABS_TOL


class HardRiskGates(_StrictModel):
    baseline_sharpe_gt: Literal[0] = 0
    max_drawdown_floor: float = FROZEN_MAX_DRAWDOWN_FLOOR
    largest_single_symbol_loss_fraction_of_initial_cash: float = FROZEN_LARGEST_SYMBOL_LOSS_FRACTION
    initial_cash: Literal[80000] = FROZEN_INITIAL_CASH


class CostStressGates(_StrictModel):
    severe_scenario_id: Literal["severe_4x_commission_5x_slippage"] = "severe_4x_commission_5x_slippage"
    severe_total_return_gt: Literal[0] = 0
    severe_open_positions_at_end: Literal[0] = 0
    moderate_scenario_id: Literal["moderate_2x_commission_2x_slippage"] = "moderate_2x_commission_2x_slippage"
    moderate_is_descriptive_only: Literal[True] = True


class DescriptiveEndpoint(_StrictModel):
    endpoint_id: str = Field(min_length=1)
    decides_oos_result: Literal[False] = False
    may_promote: Literal[False] = False
    reason: str = Field(min_length=1)


class ResultSemantics(_StrictModel):
    not_evaluable: str = (
        "data, preflight, completeness, closed-trade count, open_positions_at_end!=0 "
        "(including positions left open past last_scheduled_exit by suspension or "
        "limit-down blocks), metric finiteness, or P&L reconciliation failure"
    )
    no_go: str = "evaluable, but primary endpoint or any hard risk / severe cost gate fails"
    conditional_go: str = (
        "all predeclared gates pass; human_review_required remains true; never auto "
        "promote to scoring, paper trading, or live trading"
    )
    human_review_required_on_conditional_go: Literal[True] = True


class FrozenOosPolicy(_StrictModel):
    evaluation_mode: Literal["one_shot"] = "one_shot"
    one_shot_required: Literal[True] = True
    authorized: Literal[False] = False
    authorized_oos_window: Literal["future_2025_plus_not_yet_authorized"] = "future_2025_plus_not_yet_authorized"
    forbidden: list[str]
    lock_strategy_parameters: Literal[True] = True
    lock_signal_anchor_except_runtime_equivalent_schedule: Literal[True] = True
    lock_universe_and_industry_constraints: Literal[True] = True
    lock_scoring_and_trade_engine: Literal[True] = True
    auto_promote_to_scoring: Literal[False] = False
    auto_promote_to_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    human_review_required: Literal[True] = True


class PortfolioOosFreezeContract(_StrictModel):
    schema_version: Literal["1"] = PORTFOLIO_OOS_FREEZE_SCHEMA_VERSION
    freeze_version: Literal["all-a-share-portfolio-oos-freeze-v1"] = PORTFOLIO_OOS_FREEZE_VERSION
    bound_strategy: BoundStrategyEvidence
    bound_selection: BoundSelectionEvidence
    bound_robustness: BoundRobustnessEvidence
    bound_development_snapshots: BoundDevelopmentSnapshots
    bound_oos_data: BoundOosDataBinding
    calendar_equivalence: CalendarEquivalenceProof
    evaluation_window: EvaluationWindow
    primary_oos_endpoint: PrimaryOosEndpoint
    evaluability_gates: EvaluabilityGates
    hard_risk_gates: HardRiskGates
    cost_stress_gates: CostStressGates
    descriptive_endpoints: list[DescriptiveEndpoint]
    result_semantics: ResultSemantics
    oos_policy: FrozenOosPolicy
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    authorized: Literal[False] = False
    one_shot_required: Literal[True] = True
    development_only: Literal[True] = True
    freeze_id: str | None = None
    research_boundary: str = RESEARCH_BOUNDARY


PRIMARY_OOS_ENDPOINT = PrimaryOosEndpoint()
EVALUABILITY_GATES = EvaluabilityGates()
HARD_RISK_GATES = HardRiskGates()
COST_STRESS_GATES = CostStressGates()
RESULT_SEMANTICS = ResultSemantics()
DESCRIPTIVE_ENDPOINTS: tuple[DescriptiveEndpoint, ...] = (
    DescriptiveEndpoint(
        endpoint_id="csi300_price_index_total_return_sharpe_drawdown",
        reason="price-index benchmark without dividends; descriptive only",
    ),
    DescriptiveEndpoint(
        endpoint_id="strategy_minus_csi300_price_index",
        reason="not a total-return benchmark; cannot decide the OOS result",
    ),
    DescriptiveEndpoint(
        endpoint_id="moderate_2x_commission_2x_slippage",
        reason="moderate cost stress is descriptive; only severe cost is a hard gate",
    ),
    DescriptiveEndpoint(
        endpoint_id="sector_attribution",
        reason="static sector labels are not point-in-time; never a pass/fail gate",
    ),
    DescriptiveEndpoint(
        endpoint_id="invested_capital_fraction",
        reason="exposure diagnostics only; not a pass/fail gate",
    ),
)
FROZEN_OOS_POLICY = FrozenOosPolicy(
    forbidden=[
        "p_values",
        "information_coefficient",
        "parameter_search",
        "retune_signal_anchor",
        "reuse_2024_for_tuning",
        "event_candidate_integration",
        "auto_scoring",
        "auto_paper_trading",
        "auto_live_trading",
    ]
)


def build_committed_portfolio_oos_freeze() -> PortfolioOosFreezeContract:
    """Seal the committed p10_h20 first-2025+ OOS freeze contract."""
    return build_portfolio_oos_freeze(
        selection_report_sha256=FROZEN_SELECTION_REPORT_SHA256,
        robustness_report_sha256=FROZEN_ROBUSTNESS_REPORT_SHA256,
    )


def build_portfolio_oos_freeze(
    *,
    selection_report_sha256: str,
    robustness_report_sha256: str,
) -> PortfolioOosFreezeContract:
    """Seal a portfolio OOS freeze with protocol constants and evidence file digests."""
    contract = PortfolioOosFreezeContract(
        bound_strategy=BoundStrategyEvidence(
            strategy_path=FROZEN_STRATEGY_PATH,
            strategy_file_sha256=FROZEN_STRATEGY_FILE_SHA256,
            strategy_config_id=FROZEN_STRATEGY_CONFIG_ID,
            config_hash=FROZEN_CONFIG_HASH,
            signal_anchor_date=FROZEN_SIGNAL_ANCHOR_DATE,
            market_gate_max_new_positions=list(FROZEN_MARKET_GATE_MAX_NEW_POSITIONS),
        ),
        bound_selection=BoundSelectionEvidence(
            report_path=FROZEN_SELECTION_REPORT_PATH,
            report_sha256=selection_report_sha256,
            selected_config_hash=FROZEN_CONFIG_HASH,
            data_snapshot_id=FROZEN_DEVELOPMENT_DATA_SNAPSHOT_ID,
        ),
        bound_robustness=BoundRobustnessEvidence(
            report_path=FROZEN_ROBUSTNESS_REPORT_PATH,
            report_sha256=robustness_report_sha256,
            selected_config_hash=FROZEN_CONFIG_HASH,
            data_snapshot_id=FROZEN_DEVELOPMENT_DATA_SNAPSHOT_ID,
        ),
        bound_development_snapshots=BoundDevelopmentSnapshots(
            market_dir=FROZEN_DEVELOPMENT_MARKET_DIR,
            market_snapshot_id=FROZEN_DEVELOPMENT_MARKET_SNAPSHOT_ID,
            market_calendar_table_hash=FROZEN_DEVELOPMENT_MARKET_CALENDAR_TABLE_HASH,
            fundamental_dir=FROZEN_DEVELOPMENT_FUNDAMENTAL_DIR,
            fundamental_snapshot_id=FROZEN_DEVELOPMENT_FUNDAMENTAL_SNAPSHOT_ID,
            development_data_snapshot_id=FROZEN_DEVELOPMENT_DATA_SNAPSHOT_ID,
        ),
        bound_oos_data=BoundOosDataBinding(
            market_dir=FROZEN_OOS_MARKET_DIR,
            market_snapshot_id=FROZEN_OOS_MARKET_SNAPSHOT_ID,
            market_calendar_table_hash=FROZEN_OOS_MARKET_CALENDAR_TABLE_HASH,
            market_coverage_start=FROZEN_OOS_COVERAGE_START,
            market_coverage_end=FROZEN_OOS_COVERAGE_END,
            fundamental_dir=FROZEN_OOS_FUNDAMENTAL_DIR,
            fundamental_snapshot_id=FROZEN_OOS_FUNDAMENTAL_SNAPSHOT_ID,
            fundamental_base_market_snapshot_id=FROZEN_OOS_MARKET_SNAPSHOT_ID,
            fundamental_coverage_start=FROZEN_OOS_COVERAGE_START,
            fundamental_coverage_end=FROZEN_OOS_COVERAGE_END,
        ),
        calendar_equivalence=CalendarEquivalenceProof(
            overlap_start=FROZEN_CALENDAR_OVERLAP_START,
            overlap_end=FROZEN_CALENDAR_OVERLAP_END,
            original_signal_anchor_date=FROZEN_SIGNAL_ANCHOR_DATE,
            runtime_equivalent_anchor=FROZEN_RUNTIME_EQUIVALENT_ANCHOR,
            first_2025_plus_signal=FROZEN_FIRST_2025_PLUS_SIGNAL,
            last_complete_signal=FROZEN_LAST_COMPLETE_SIGNAL,
            last_scheduled_exit=FROZEN_LAST_SCHEDULED_EXIT,
        ),
        evaluation_window=EvaluationWindow(
            evaluation_start=FROZEN_EVALUATION_START,
            evaluation_end=FROZEN_EVALUATION_END,
            signal_cutoff=FROZEN_SIGNAL_CUTOFF,
            last_scheduled_exit=FROZEN_LAST_SCHEDULED_EXIT,
        ),
        primary_oos_endpoint=PRIMARY_OOS_ENDPOINT,
        evaluability_gates=EVALUABILITY_GATES,
        hard_risk_gates=HARD_RISK_GATES,
        cost_stress_gates=COST_STRESS_GATES,
        descriptive_endpoints=list(DESCRIPTIVE_ENDPOINTS),
        result_semantics=RESULT_SEMANTICS,
        oos_policy=FROZEN_OOS_POLICY,
    )
    return seal_portfolio_oos_freeze(contract)


def seal_portfolio_oos_freeze(
    contract: PortfolioOosFreezeContract,
) -> PortfolioOosFreezeContract:
    return contract.model_copy(update={"freeze_id": _freeze_id(contract)})


def build_copy_with_id(contract: PortfolioOosFreezeContract) -> PortfolioOosFreezeContract:
    return seal_portfolio_oos_freeze(contract)


def write_portfolio_oos_freeze(
    path: Path,
    contract: PortfolioOosFreezeContract,
) -> PortfolioOosFreezeContract:
    sealed = contract if contract.freeze_id == _freeze_id(contract) else seal_portfolio_oos_freeze(contract)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


def load_verified_portfolio_oos_freeze(path: Path) -> PortfolioOosFreezeContract:
    freeze_path = Path(path)
    try:
        contract = PortfolioOosFreezeContract.model_validate_json(freeze_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("portfolio OOS freeze contract is missing or invalid") from exc
    assert_portfolio_oos_freeze_self_consistent(contract)
    return contract


def verify_portfolio_oos_freeze(
    *,
    freeze_path: Path,
    project_root: Path | None = None,
) -> PortfolioOosFreezeContract:
    """Verify freeze bindings using only contract, evidence JSON, strategy YAML,
    four manifests, and two calendar Parquet files. Never reads bars, fundamentals
    tables, scores, IC, backtests, or OOS evaluation outputs.
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    contract = load_verified_portfolio_oos_freeze(freeze_path)
    assert_committed_portfolio_oos_freeze_bindings(contract)
    _verify_strategy_binding(contract, root)
    _verify_selection_binding(contract, root)
    _verify_robustness_binding(contract, root)
    _verify_manifest_bindings(contract, root)
    _verify_calendar_equivalence(contract, root)
    return contract


def assert_committed_portfolio_oos_freeze_bindings(
    contract: PortfolioOosFreezeContract,
) -> None:
    """Fail closed: production verify always requires the single committed binding."""
    committed = build_committed_portfolio_oos_freeze()
    if contract.freeze_id != committed.freeze_id:
        raise ValueError("portfolio OOS freeze is not the committed binding")
    if contract.bound_selection.report_sha256 != FROZEN_SELECTION_REPORT_SHA256:
        raise ValueError("committed selection report SHA-256 drifted")
    if contract.bound_robustness.report_sha256 != FROZEN_ROBUSTNESS_REPORT_SHA256:
        raise ValueError("committed robustness report SHA-256 drifted")


def assert_portfolio_oos_freeze_self_consistent(
    contract: PortfolioOosFreezeContract,
) -> None:
    if contract.freeze_id is None or contract.freeze_id != _freeze_id(contract):
        raise ValueError("portfolio OOS freeze ID does not match its content")
    if (
        contract.ready_for_scoring
        or contract.ready_for_trading
        or contract.auto_deploy
        or contract.authorized
        or not contract.one_shot_required
        or not contract.development_only
    ):
        raise ValueError("portfolio OOS freeze violates research boundaries")
    if contract.primary_oos_endpoint != PRIMARY_OOS_ENDPOINT:
        raise ValueError("portfolio OOS freeze primary endpoint drifted")
    if contract.evaluability_gates != EVALUABILITY_GATES:
        raise ValueError("portfolio OOS freeze evaluability gates drifted")
    if contract.hard_risk_gates != HARD_RISK_GATES:
        raise ValueError("portfolio OOS freeze hard risk gates drifted")
    if contract.cost_stress_gates != COST_STRESS_GATES:
        raise ValueError("portfolio OOS freeze cost stress gates drifted")
    if contract.descriptive_endpoints != list(DESCRIPTIVE_ENDPOINTS):
        raise ValueError("portfolio OOS freeze descriptive endpoints drifted")
    if any(item.decides_oos_result or item.may_promote for item in contract.descriptive_endpoints):
        raise ValueError("descriptive endpoints cannot decide or promote")
    if contract.result_semantics != RESULT_SEMANTICS:
        raise ValueError("portfolio OOS freeze result semantics drifted")
    if contract.oos_policy != FROZEN_OOS_POLICY:
        raise ValueError("portfolio OOS freeze OOS policy drifted")
    if contract.research_boundary != RESEARCH_BOUNDARY:
        raise ValueError("portfolio OOS freeze research boundary drifted")

    strategy = contract.bound_strategy
    _assert_relative_path(strategy.strategy_path, "strategy_path")
    if strategy.strategy_path != FROZEN_STRATEGY_PATH:
        raise ValueError("frozen strategy path drifted")
    if strategy.strategy_file_sha256 != FROZEN_STRATEGY_FILE_SHA256:
        raise ValueError("frozen strategy file SHA drifted")
    if strategy.strategy_config_id != FROZEN_STRATEGY_CONFIG_ID:
        raise ValueError("frozen strategy config id drifted")
    if strategy.config_hash != FROZEN_CONFIG_HASH:
        raise ValueError("frozen strategy config hash drifted")
    if strategy.signal_anchor_date != FROZEN_SIGNAL_ANCHOR_DATE:
        raise ValueError("frozen signal_anchor_date drifted")
    if strategy.market_gate_max_new_positions != list(FROZEN_MARKET_GATE_MAX_NEW_POSITIONS):
        raise ValueError("frozen market_gate drifted")

    selection = contract.bound_selection
    _assert_relative_path(selection.report_path, "selection report_path")
    if selection.report_path != FROZEN_SELECTION_REPORT_PATH:
        raise ValueError("frozen selection report path drifted")
    if selection.report_sha256 != FROZEN_SELECTION_REPORT_SHA256:
        raise ValueError("frozen selection report SHA-256 drifted")
    if selection.selected_config_hash != FROZEN_CONFIG_HASH:
        raise ValueError("selection selected_config_hash must match strategy config_hash")
    if selection.data_snapshot_id != FROZEN_DEVELOPMENT_DATA_SNAPSHOT_ID:
        raise ValueError("selection data_snapshot_id drifted")

    robustness = contract.bound_robustness
    _assert_relative_path(robustness.report_path, "robustness report_path")
    if robustness.report_path != FROZEN_ROBUSTNESS_REPORT_PATH:
        raise ValueError("frozen robustness report path drifted")
    if robustness.report_sha256 != FROZEN_ROBUSTNESS_REPORT_SHA256:
        raise ValueError("frozen robustness report SHA-256 drifted")
    if robustness.selected_config_hash != FROZEN_CONFIG_HASH:
        raise ValueError("robustness selected_config_hash must match strategy config_hash")
    if robustness.status != FROZEN_ROBUSTNESS_STATUS:
        raise ValueError("robustness status drifted")
    if robustness.data_snapshot_id != FROZEN_DEVELOPMENT_DATA_SNAPSHOT_ID:
        raise ValueError("robustness data_snapshot_id drifted")

    development = contract.bound_development_snapshots
    _assert_relative_path(development.market_dir, "development market_dir")
    _assert_relative_path(development.fundamental_dir, "development fundamental_dir")
    if development.market_dir != FROZEN_DEVELOPMENT_MARKET_DIR:
        raise ValueError("development market_dir drifted")
    if development.market_snapshot_id != FROZEN_DEVELOPMENT_MARKET_SNAPSHOT_ID:
        raise ValueError("development market snapshot drifted")
    if development.market_calendar_table_hash != FROZEN_DEVELOPMENT_MARKET_CALENDAR_TABLE_HASH:
        raise ValueError("development calendar table hash drifted")
    if development.fundamental_dir != FROZEN_DEVELOPMENT_FUNDAMENTAL_DIR:
        raise ValueError("development fundamental_dir drifted")
    if development.fundamental_snapshot_id != FROZEN_DEVELOPMENT_FUNDAMENTAL_SNAPSHOT_ID:
        raise ValueError("development fundamental snapshot drifted")
    if development.development_data_snapshot_id != FROZEN_DEVELOPMENT_DATA_SNAPSHOT_ID:
        raise ValueError("development data_snapshot_id drifted")

    oos = contract.bound_oos_data
    _assert_relative_path(oos.market_dir, "OOS market_dir")
    _assert_relative_path(oos.fundamental_dir, "OOS fundamental_dir")
    if oos.market_dir != FROZEN_OOS_MARKET_DIR:
        raise ValueError("OOS market_dir drifted")
    if oos.market_snapshot_id != FROZEN_OOS_MARKET_SNAPSHOT_ID:
        raise ValueError("OOS market snapshot drifted")
    if oos.market_calendar_table_hash != FROZEN_OOS_MARKET_CALENDAR_TABLE_HASH:
        raise ValueError("OOS calendar table hash drifted")
    if oos.market_coverage_start != FROZEN_OOS_COVERAGE_START:
        raise ValueError("OOS market coverage_start drifted")
    if oos.market_coverage_end != FROZEN_OOS_COVERAGE_END:
        raise ValueError("OOS market coverage_end drifted")
    if oos.fundamental_dir != FROZEN_OOS_FUNDAMENTAL_DIR:
        raise ValueError("OOS fundamental_dir drifted")
    if oos.fundamental_snapshot_id != FROZEN_OOS_FUNDAMENTAL_SNAPSHOT_ID:
        raise ValueError("OOS fundamental snapshot drifted")
    if oos.fundamental_base_market_snapshot_id != oos.market_snapshot_id:
        raise ValueError("OOS fundamental base_market must equal OOS market snapshot")
    if oos.fundamental_base_market_snapshot_id != FROZEN_OOS_MARKET_SNAPSHOT_ID:
        raise ValueError("OOS fundamental base_market drifted")
    if oos.fundamental_coverage_start != FROZEN_OOS_COVERAGE_START:
        raise ValueError("OOS fundamental coverage_start drifted")
    if oos.fundamental_coverage_end != FROZEN_OOS_COVERAGE_END:
        raise ValueError("OOS fundamental coverage_end drifted")

    proof = contract.calendar_equivalence
    if proof.overlap_start != FROZEN_CALENDAR_OVERLAP_START:
        raise ValueError("calendar overlap_start drifted")
    if proof.overlap_end != FROZEN_CALENDAR_OVERLAP_END:
        raise ValueError("calendar overlap_end drifted")
    if proof.original_signal_anchor_date != FROZEN_SIGNAL_ANCHOR_DATE:
        raise ValueError("calendar original anchor drifted")
    if proof.runtime_equivalent_anchor != FROZEN_RUNTIME_EQUIVALENT_ANCHOR:
        raise ValueError("runtime_equivalent_anchor drifted")
    if proof.first_2025_plus_signal != FROZEN_FIRST_2025_PLUS_SIGNAL:
        raise ValueError("first_2025_plus_signal drifted")
    if proof.last_complete_signal != FROZEN_LAST_COMPLETE_SIGNAL:
        raise ValueError("last_complete_signal drifted")
    if proof.last_scheduled_exit != FROZEN_LAST_SCHEDULED_EXIT:
        raise ValueError("last_scheduled_exit drifted")

    window = contract.evaluation_window
    if window.evaluation_start != FROZEN_EVALUATION_START:
        raise ValueError("evaluation_start drifted")
    if window.evaluation_end != FROZEN_EVALUATION_END:
        raise ValueError("evaluation_end drifted")
    if window.signal_cutoff != FROZEN_SIGNAL_CUTOFF:
        raise ValueError("signal_cutoff drifted")
    if window.last_scheduled_exit != FROZEN_LAST_SCHEDULED_EXIT:
        raise ValueError("evaluation last_scheduled_exit drifted")
    if window.last_scheduled_exit_semantics != LAST_SCHEDULED_EXIT_SEMANTICS:
        raise ValueError("last_scheduled_exit_semantics drifted")
    if window.signal_cutoff != proof.last_complete_signal:
        raise ValueError("signal_cutoff must equal last_complete_signal")
    if window.last_scheduled_exit != proof.last_scheduled_exit:
        raise ValueError("evaluation exit must equal calendar last_scheduled_exit")
    if LAST_SCHEDULED_EXIT_SEMANTICS not in proof.note:
        raise ValueError("calendar equivalence note missing last_scheduled_exit semantics")


def compute_calendar_equivalence_proof(
    development_calendar: list[date],
    oos_calendar: list[date],
) -> CalendarEquivalenceProof:
    """Fail-closed calendar equivalence from development + OOS trading days only."""
    dev = _normalize_calendar(development_calendar, "development calendar")
    oos = _normalize_calendar(oos_calendar, "OOS calendar")
    overlap_dev = [day for day in dev if FROZEN_CALENDAR_OVERLAP_START <= day <= FROZEN_CALENDAR_OVERLAP_END]
    overlap_oos = [day for day in oos if FROZEN_CALENDAR_OVERLAP_START <= day <= FROZEN_CALENDAR_OVERLAP_END]
    if overlap_dev != overlap_oos:
        raise ValueError("development and OOS calendars differ on the overlap window")
    if len(overlap_dev) != FROZEN_CALENDAR_OVERLAP_TRADING_DAYS:
        raise ValueError("calendar overlap trading-day count is invalid")
    merged = sorted(set(dev) | set(oos))
    if FROZEN_SIGNAL_ANCHOR_DATE not in merged:
        raise ValueError("original signal_anchor_date is missing from the merged calendar")
    signals = _signal_schedule(
        merged,
        anchor=FROZEN_SIGNAL_ANCHOR_DATE,
        interval_days=FROZEN_SIGNAL_INTERVAL_DAYS,
    )
    runtime_anchor = next(
        (day for day in signals if day >= FROZEN_OOS_COVERAGE_START),
        None,
    )
    if runtime_anchor is None:
        raise ValueError("no runtime-equivalent anchor found in the OOS coverage window")
    first_2025 = next((day for day in signals if day >= FROZEN_EVALUATION_START), None)
    if first_2025 is None:
        raise ValueError("no 2025+ signal date found on the merged schedule")
    last_complete: date | None = None
    last_exit: date | None = None
    for signal in signals:
        exit_day = scheduled_unblocked_fixed_horizon_liquidation(
            merged,
            signal,
            horizon_days=FROZEN_HORIZON_DAYS,
        )
        if exit_day is None:
            break
        if signal >= FROZEN_EVALUATION_START and exit_day <= FROZEN_EVALUATION_END:
            last_complete = signal
            last_exit = exit_day
    if last_complete is None or last_exit is None:
        raise ValueError("no complete 2025+ signal/exit pair found before evaluation_end")
    proof = CalendarEquivalenceProof(
        overlap_start=FROZEN_CALENDAR_OVERLAP_START,
        overlap_end=FROZEN_CALENDAR_OVERLAP_END,
        original_signal_anchor_date=FROZEN_SIGNAL_ANCHOR_DATE,
        runtime_equivalent_anchor=runtime_anchor,
        first_2025_plus_signal=first_2025,
        last_complete_signal=last_complete,
        last_scheduled_exit=last_exit,
    )
    if proof.runtime_equivalent_anchor != FROZEN_RUNTIME_EQUIVALENT_ANCHOR:
        raise ValueError("runtime_equivalent_anchor does not match the frozen protocol")
    if proof.first_2025_plus_signal != FROZEN_FIRST_2025_PLUS_SIGNAL:
        raise ValueError("first_2025_plus_signal does not match the frozen protocol")
    if proof.last_complete_signal != FROZEN_LAST_COMPLETE_SIGNAL:
        raise ValueError("last_complete_signal does not match the frozen protocol")
    if proof.last_scheduled_exit != FROZEN_LAST_SCHEDULED_EXIT:
        raise ValueError("last_scheduled_exit does not match the frozen protocol")
    if FROZEN_SIGNAL_CUTOFF != proof.last_complete_signal:
        raise ValueError("signal_cutoff drifted from last_complete_signal")
    return proof


def _verify_strategy_binding(contract: PortfolioOosFreezeContract, root: Path) -> None:
    path = root / contract.bound_strategy.strategy_path
    if not path.is_file():
        raise ValueError("frozen strategy YAML is missing")
    digest = _sha256_file(path)
    if digest != contract.bound_strategy.strategy_file_sha256:
        raise ValueError("strategy file SHA-256 does not match the freeze contract")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("frozen strategy YAML is invalid")
    config = StrategyConfig.model_validate(payload)
    if config.run_id() != contract.bound_strategy.strategy_config_id:
        raise ValueError("strategy config_id/run_id does not match the freeze contract")
    if config.config_id != contract.bound_strategy.strategy_config_id:
        raise ValueError("strategy config_id does not match the freeze contract")
    if config.config_hash() != contract.bound_strategy.config_hash:
        raise ValueError("strategy config_hash does not match the freeze contract")
    if config.portfolio.initial_cash != float(contract.bound_strategy.initial_cash):
        raise ValueError("strategy initial_cash does not match the freeze contract")
    if config.portfolio.max_positions != contract.bound_strategy.max_positions:
        raise ValueError("strategy max_positions does not match the freeze contract")
    if config.portfolio.weighting != contract.bound_strategy.weighting:
        raise ValueError("strategy weighting does not match the freeze contract")
    if config.trade.exit_policy != contract.bound_strategy.exit_policy:
        raise ValueError("strategy exit_policy does not match the freeze contract")
    if config.trade.max_holding_days != contract.bound_strategy.fixed_horizon_days:
        raise ValueError("strategy fixed horizon does not match the freeze contract")
    if config.trade.min_holding_days != contract.bound_strategy.fixed_horizon_days:
        raise ValueError("strategy min_holding_days must equal fixed horizon")
    if config.trade.signal_interval_days != contract.bound_strategy.signal_interval_days:
        raise ValueError("strategy signal_interval_days does not match the freeze contract")
    if config.trade.signal_anchor_date != contract.bound_strategy.signal_anchor_date:
        raise ValueError("strategy signal_anchor_date does not match the freeze contract")
    observed_gate = [band.max_new_positions for band in config.market_gate]
    if observed_gate != contract.bound_strategy.market_gate_max_new_positions:
        raise ValueError("strategy market_gate does not match the freeze contract")


def _verify_selection_binding(contract: PortfolioOosFreezeContract, root: Path) -> None:
    path = root / contract.bound_selection.report_path
    payload = _load_json_object(path, "selection report")
    if _sha256_file(path) != contract.bound_selection.report_sha256:
        raise ValueError("selection report SHA-256 does not match the freeze contract")
    if payload.get("selected_candidate_id") != contract.bound_selection.selected_candidate_id:
        raise ValueError("selection selected_candidate_id does not match the freeze contract")
    if payload.get("selected_config_hash") != contract.bound_selection.selected_config_hash:
        raise ValueError("selection selected_config_hash does not match the freeze contract")
    if payload.get("selected_config_hash") != contract.bound_strategy.config_hash:
        raise ValueError("selection selected_config_hash must equal strategy config_hash")
    if payload.get("data_snapshot_id") != contract.bound_selection.data_snapshot_id:
        raise ValueError("selection data_snapshot_id does not match the freeze contract")


def _verify_robustness_binding(contract: PortfolioOosFreezeContract, root: Path) -> None:
    path = root / contract.bound_robustness.report_path
    payload = _load_json_object(path, "robustness report")
    if _sha256_file(path) != contract.bound_robustness.report_sha256:
        raise ValueError("robustness report SHA-256 does not match the freeze contract")
    if payload.get("selected_config_hash") != contract.bound_robustness.selected_config_hash:
        raise ValueError("robustness selected_config_hash does not match the freeze contract")
    if payload.get("selected_config_hash") != contract.bound_strategy.config_hash:
        raise ValueError("robustness selected_config_hash must equal strategy config_hash")
    if payload.get("status") != contract.bound_robustness.status:
        raise ValueError("robustness status does not match the freeze contract")
    if payload.get("data_snapshot_id") != contract.bound_robustness.data_snapshot_id:
        raise ValueError("robustness data_snapshot_id does not match the freeze contract")


def _verify_manifest_bindings(contract: PortfolioOosFreezeContract, root: Path) -> None:
    development = contract.bound_development_snapshots
    oos = contract.bound_oos_data
    _assert_market_manifest(
        root / development.market_dir / "manifest.json",
        expected_snapshot_id=development.market_snapshot_id,
        expected_calendar_hash=development.market_calendar_table_hash,
        expected_coverage_start=None,
        expected_coverage_end=None,
        label="development market",
    )
    _assert_fundamental_manifest(
        root / development.fundamental_dir / "manifest.json",
        expected_snapshot_id=development.fundamental_snapshot_id,
        expected_base_market_snapshot_id=development.market_snapshot_id,
        expected_coverage_start=None,
        expected_coverage_end=None,
        label="development fundamental",
    )
    _assert_market_manifest(
        root / oos.market_dir / "manifest.json",
        expected_snapshot_id=oos.market_snapshot_id,
        expected_calendar_hash=oos.market_calendar_table_hash,
        expected_coverage_start=oos.market_coverage_start,
        expected_coverage_end=oos.market_coverage_end,
        label="OOS market",
    )
    _assert_fundamental_manifest(
        root / oos.fundamental_dir / "manifest.json",
        expected_snapshot_id=oos.fundamental_snapshot_id,
        expected_base_market_snapshot_id=oos.market_snapshot_id,
        expected_coverage_start=oos.fundamental_coverage_start,
        expected_coverage_end=oos.fundamental_coverage_end,
        label="OOS fundamental",
    )


def _verify_calendar_equivalence(contract: PortfolioOosFreezeContract, root: Path) -> None:
    development = contract.bound_development_snapshots
    oos = contract.bound_oos_data
    dev_calendar_path = root / development.market_dir / "calendar.parquet"
    oos_calendar_path = root / oos.market_dir / "calendar.parquet"
    if not dev_calendar_path.is_file():
        raise ValueError("development calendar.parquet is missing")
    if not oos_calendar_path.is_file():
        raise ValueError("OOS calendar.parquet is missing")
    dev_frame = pl.read_parquet(dev_calendar_path)
    oos_frame = pl.read_parquet(oos_calendar_path)
    if hash_table(dev_frame, "calendar") != development.market_calendar_table_hash:
        raise ValueError("development calendar content hash does not match the freeze contract")
    if hash_table(oos_frame, "calendar") != oos.market_calendar_table_hash:
        raise ValueError("OOS calendar content hash does not match the freeze contract")
    proof = compute_calendar_equivalence_proof(
        _calendar_dates(dev_frame),
        _calendar_dates(oos_frame),
    )
    if proof != contract.calendar_equivalence:
        raise ValueError("calendar equivalence proof does not match the freeze contract")


def _assert_market_manifest(
    path: Path,
    *,
    expected_snapshot_id: str,
    expected_calendar_hash: str,
    expected_coverage_start: date | None,
    expected_coverage_end: date | None,
    label: str,
) -> None:
    if not path.is_file():
        raise ValueError(f"{label} manifest is missing")
    try:
        stored = DataSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"{label} manifest is invalid") from exc
    if stored.schema_version != SCHEMA_VERSION:
        raise ValueError(f"{label} manifest schema_version is unsupported")
    if stored.snapshot_id != expected_snapshot_id:
        raise ValueError(f"{label} snapshot_id does not match the freeze contract")
    if stored.snapshot_id != stored.content_hash:
        raise ValueError(f"{label} snapshot_id does not equal content_hash")
    recomputed = combine_hashes(
        stored.table_hashes,
        stored.adjustment,
        stored.price_basis,
        stored.schema_version,
    )
    if stored.content_hash != recomputed or stored.snapshot_id != recomputed:
        raise ValueError(f"{label} content_hash does not match table_hashes and hash-defining metadata")
    if stored.table_hashes.get("calendar") != expected_calendar_hash:
        raise ValueError(f"{label} calendar table hash does not match the freeze contract")
    if expected_coverage_start is not None and stored.coverage_start != expected_coverage_start:
        raise ValueError(f"{label} coverage_start does not match the freeze contract")
    if expected_coverage_end is not None and stored.coverage_end != expected_coverage_end:
        raise ValueError(f"{label} coverage_end does not match the freeze contract")


def _assert_fundamental_manifest(
    path: Path,
    *,
    expected_snapshot_id: str,
    expected_base_market_snapshot_id: str,
    expected_coverage_start: date | None,
    expected_coverage_end: date | None,
    label: str,
) -> None:
    if not path.is_file():
        raise ValueError(f"{label} manifest is missing")
    try:
        stored = FundamentalSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"{label} manifest is invalid") from exc
    if stored.schema_version != FUNDAMENTAL_SCHEMA_VERSION:
        raise ValueError(f"{label} manifest schema_version is unsupported")
    if stored.report_availability_policy != REPORT_AVAILABILITY_POLICY:
        raise ValueError(f"{label} report availability policy does not match the contract")
    if stored.valuation_availability_policy != VALUATION_AVAILABILITY_POLICY:
        raise ValueError(f"{label} valuation availability policy does not match the contract")
    if stored.snapshot_id != expected_snapshot_id:
        raise ValueError(f"{label} snapshot_id does not match the freeze contract")
    if stored.snapshot_id != stored.content_hash:
        raise ValueError(f"{label} snapshot_id does not equal content_hash")
    recomputed = _combine_fundamental_hashes(
        stored.table_hashes,
        base_market_snapshot_id=stored.base_market_snapshot_id,
        collection_request_id=stored.collection_request_id,
    )
    if stored.content_hash != recomputed or stored.snapshot_id != recomputed:
        raise ValueError(f"{label} content_hash does not match table_hashes and hash-defining metadata")
    if stored.base_market_snapshot_id != expected_base_market_snapshot_id:
        raise ValueError(f"{label} base_market_snapshot_id does not match the freeze contract")
    if expected_coverage_start is not None and stored.coverage_start != expected_coverage_start:
        raise ValueError(f"{label} coverage_start does not match the freeze contract")
    if expected_coverage_end is not None and stored.coverage_end != expected_coverage_end:
        raise ValueError(f"{label} coverage_end does not match the freeze contract")


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"{label} is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _calendar_dates(frame: pl.DataFrame) -> list[date]:
    if "date" not in frame.columns:
        raise ValueError("calendar parquet is missing the date column")
    values: list[date] = []
    for value in frame["date"].to_list():
        if isinstance(value, date):
            values.append(value)
        else:
            raise ValueError("calendar parquet contains a non-date value")
    return values


def _normalize_calendar(values: list[date], label: str) -> list[date]:
    if not values:
        raise ValueError(f"{label} is empty")
    if any(not isinstance(day, date) for day in values):
        raise ValueError(f"{label} contains a non-date value")
    ordered = sorted(values)
    if len(ordered) != len(set(ordered)):
        raise ValueError(f"{label} contains duplicate trading days")
    return ordered


def _signal_schedule(
    calendar: list[date],
    *,
    anchor: date,
    interval_days: int,
) -> list[date]:
    index = calendar.index(anchor)
    return [calendar[offset] for offset in range(index, len(calendar), interval_days)]


def scheduled_unblocked_fixed_horizon_liquidation(
    calendar: list[date],
    signal: date,
    *,
    horizon_days: int,
) -> date | None:
    """Scheduled unblocked fixed-horizon liquidation matching BacktestEngine.

    Lifecycle: signal day places an order; entry fills on the next trading day;
    the entry session is skipped for exit eligibility; each later session
    increments exit_eligible_days; the unblocked timeout exit is the session
    where that count first reaches ``horizon_days``. Suspension / limit-down
    may keep the position open past this scheduled date.
    """
    if horizon_days < 1:
        raise ValueError("horizon_days must be >= 1")
    try:
        signal_index = calendar.index(signal)
    except ValueError:
        return None
    entry_index = signal_index + 1
    if entry_index >= len(calendar):
        return None
    exit_index = entry_index + horizon_days
    if exit_index >= len(calendar):
        return None
    return calendar[exit_index]


def _assert_relative_path(value: str, label: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError(f"{label} must be a relative path without parent traversal")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _freeze_id(contract: PortfolioOosFreezeContract) -> str:
    payload = contract.model_dump(mode="json", exclude={"freeze_id"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
