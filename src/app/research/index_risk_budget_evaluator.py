"""Authorization-gated index-level equity/bond risk-budget research replay."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from statistics import stdev
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.providers.csi_all_share_long_history import (
    DEFAULT_CONTRACT_PATH as DEFAULT_EQUITY_IDENTITY_PATH,
)
from app.providers.csi_all_share_long_history import (
    verify_csi_all_share_long_history_snapshot,
)
from app.research.defensive_leg_history import verify_official_defensive_leg_history
from app.research.index_etf_risk_budget_protocol import verify_index_etf_risk_budget_protocol
from app.research.index_research_product_cost_contract import (
    CostScenario,
    verify_index_research_product_cost_contract,
)
from app.research.index_time_series_trial_ledger import verify_index_time_series_trial_ledger
from app.research.repo_file_safety import resolve_repo_regular_file

EQUITY_STAGING_DIR = Path("data/raw/csi-all-share-index-2005-2024-v1")
EQUITY_SNAPSHOT_DIR = Path("data/research/csi-all-share-index-2005-2024-v1")
DEFAULT_POWER_REVIEW_PATH = Path("data/research/index-risk-budget-power-v1/review.json")
DEFAULT_OUTPUT_DIR = Path("data/research/index-risk-budget-historical-replay-v1")
DEFAULT_REPORT_PATH = DEFAULT_OUTPUT_DIR / "report.json"
DEFAULT_DAILY_PATH = DEFAULT_OUTPUT_DIR / "daily-path.parquet"
DEFAULT_AUTHORIZATION_PATH = Path("config/research/index-risk-budget-run-authorization-v1.json")

INITIAL_CAPITAL_CNY = 80_000.0
TRADING_DAYS_PER_YEAR = 242
STATIC_WEIGHTS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
FULL_EQUITY_REFERENCE = 1.0
HISTORICAL_START = date(2005, 1, 4)
HISTORICAL_END = date(2024, 12, 31)
REQUIRED_CONFIRMATION_TEXT = (
    "我确认按照已封印的指数风险预算协议执行2005-01-04至2024-12-31一次性历史回放；"
    "该回放仅为已见历史研究，不是OOS、评分、荐股、组合指令或交易授权。"
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArmMetrics(_StrictModel):
    arm_id: str = Field(min_length=1)
    scenario: Literal["base", "stress"]
    trading_days: int = Field(gt=0)
    final_equity: float = Field(gt=0.0)
    total_return: float
    annualized_return: float
    realized_volatility: float
    maximum_drawdown: float
    calmar: float | None
    cumulative_explicit_costs: float = Field(ge=0.0)
    turnover: float = Field(ge=0.0)
    rebalance_count: int = Field(ge=0)
    average_equity_weight: float = Field(ge=0.0, le=1.0)


class CandidateComparison(_StrictModel):
    candidate_id: str
    best_static_arm_id: str
    stress_calmar_difference: float
    bootstrap_probability_not_positive: float = Field(ge=0.0, le=1.0)
    holm_adjusted_probability: float = Field(ge=0.0, le=1.0)
    sealed_mde_calmar_difference: float = Field(gt=0.0)
    observed_improvement_meets_mde: bool
    familywise_significance_pass: bool
    maximum_drawdown_floor_pass: bool
    not_pareto_dominated_pass: bool
    stress_cost_primary_gate_pass: bool
    all_hard_gates_pass: bool


class IndexRiskBudgetHistoricalReport(_StrictModel):
    schema_version: Literal["1"] = "1"
    report_version: Literal["index-risk-budget-historical-replay-v1"] = (
        "index-risk-budget-historical-replay-v1"
    )
    report_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authorization_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    trial_ledger_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    equity_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    defensive_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    product_cost_contract_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    power_review_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_start: date
    evaluation_end: date
    first_invested_return_date: date
    execution_convention: Literal[
        "signal_through_P_close_trade_at_D_close_previous_weight_earns_P_to_D_return"
    ]
    bootstrap: dict[str, int]
    arm_metrics: list[ArmMetrics]
    candidate_comparisons: list[CandidateComparison]
    historical_replay_only: Literal[True] = True
    seen_history_only: Literal[True] = True
    oos_claim: Literal[False] = False
    consumed_oos_reused: Literal[False] = False
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @model_validator(mode="after")
    def _fail_closed(self) -> IndexRiskBudgetHistoricalReport:
        if len(self.candidate_comparisons) != 2:
            raise ValueError("historical report must contain exactly two candidate comparisons")
        if self.evaluation_start != HISTORICAL_START or self.evaluation_end != HISTORICAL_END:
            raise ValueError("historical report evaluation window drifted")
        if self.first_invested_return_date <= self.evaluation_start:
            raise ValueError("historical report warm-up boundary is invalid")
        return self


class IndexRiskBudgetRunAuthorization(_StrictModel):
    schema_version: Literal["1"]
    authorization_version: Literal["index-risk-budget-run-authorization-v1"]
    authorization_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed_at: datetime
    confirmation_text: str
    protocol_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    trial_ledger_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    defensive_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    product_cost_contract_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    power_review_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_window: Literal["2005-01-04..2024-12-31"]
    one_time_historical_replay_authorized: Literal[True]
    prospective_or_oos_evaluation_authorized: Literal[False]
    scoring_or_stock_selection_authorized: Literal[False]
    portfolio_construction_authorized: Literal[False]
    orders_or_trading_authorized: Literal[False]

    @model_validator(mode="after")
    def _exact_confirmation(self) -> IndexRiskBudgetRunAuthorization:
        if self.confirmation_text != REQUIRED_CONFIRMATION_TEXT:
            raise ValueError("prominent manual confirmation text does not exactly match")
        return self


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_authorization_id(authorization: IndexRiskBudgetRunAuthorization) -> str:
    return _json_hash(authorization.model_dump(mode="json", exclude={"authorization_id"}))


def verify_run_authorization(
    *, repo_root: Path, path: Path = DEFAULT_AUTHORIZATION_PATH
) -> IndexRiskBudgetRunAuthorization:
    root = Path(repo_root).resolve(strict=True)
    try:
        resolved = resolve_repo_regular_file(path, repo_root=root, field_name="authorization_path")
        authorization = IndexRiskBudgetRunAuthorization.model_validate_json(resolved.read_text())
    except Exception as exc:
        raise ValueError(
            "PROMINENT MANUAL CONFIRMATION REQUIRED: index risk-budget historical "
            "replay authorization is missing or invalid"
        ) from exc
    if authorization.authorization_id != compute_authorization_id(authorization):
        raise ValueError("index risk-budget run authorization self-hash mismatch")
    return authorization


def _sample_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        raise ValueError("sample standard deviation requires at least two values")
    return stdev(values)


def _maximum_drawdown(equity: Sequence[float]) -> float:
    if not equity:
        raise ValueError("equity path is empty")
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def _metrics_from_equity(
    *,
    arm_id: str,
    scenario: Literal["base", "stress"],
    equity: Sequence[float],
    daily_returns: Sequence[float],
    costs: Sequence[float],
    turnover: Sequence[float],
    weights: Sequence[float],
) -> ArmMetrics:
    if len(equity) < 2 or len(daily_returns) != len(equity):
        raise ValueError("arm path is too short or misaligned")
    years = (len(equity) - 1) / TRADING_DAYS_PER_YEAR
    total_return = equity[-1] / INITIAL_CAPITAL_CNY - 1.0
    annualized = (equity[-1] / INITIAL_CAPITAL_CNY) ** (1.0 / years) - 1.0
    max_dd = _maximum_drawdown([INITIAL_CAPITAL_CNY, *equity])
    realized = _sample_std(daily_returns[1:]) * math.sqrt(TRADING_DAYS_PER_YEAR)
    calmar = annualized / abs(max_dd) if max_dd < 0.0 else None
    return ArmMetrics(
        arm_id=arm_id,
        scenario=scenario,
        trading_days=len(equity) - 1,
        final_equity=equity[-1],
        total_return=total_return,
        annualized_return=annualized,
        realized_volatility=realized,
        maximum_drawdown=max_dd,
        calmar=calmar,
        cumulative_explicit_costs=sum(costs),
        turnover=sum(turnover),
        rebalance_count=sum(value > 0.0 for value in turnover),
        average_equity_weight=sum(weights) / len(weights),
    )


def _trade_cost(nav: float, delta_weight: float, scenario: CostScenario) -> tuple[float, float]:
    if delta_weight <= 1e-15:
        return 0.0, 0.0
    notional_per_leg = nav * delta_weight
    commission = max(
        notional_per_leg * scenario.commission_rate_per_side,
        scenario.minimum_commission_cny_per_leg,
    )
    slippage = notional_per_leg * scenario.slippage_bps_per_side / 10_000.0
    return 2.0 * (commission + slippage), 2.0 * delta_weight


def _initial_cost(nav: float, weight: float, scenario: CostScenario) -> tuple[float, float]:
    cost = 0.0
    turnover = 0.0
    for leg_weight in (weight, 1.0 - weight):
        if leg_weight <= 1e-15:
            continue
        notional = nav * leg_weight
        cost += max(
            notional * scenario.commission_rate_per_side,
            scenario.minimum_commission_cny_per_leg,
        ) + notional * scenario.slippage_bps_per_side / 10_000.0
        turnover += leg_weight
    return cost, turnover


def _realized_volatility(levels: Sequence[float], through_index: int, lookback: int) -> float:
    if through_index < lookback:
        raise ValueError("insufficient volatility lookback")
    returns = [
        levels[index] / levels[index - 1] - 1.0
        for index in range(through_index - lookback + 1, through_index + 1)
    ]
    volatility = _sample_std(returns) * math.sqrt(TRADING_DAYS_PER_YEAR)
    if not math.isfinite(volatility) or volatility <= 0.0:
        raise ValueError("realized volatility is non-positive or invalid")
    return volatility


def _first_market_day_of_week(dates: Sequence[date], index: int) -> bool:
    return index <= 0 or dates[index - 1].isocalendar()[:2] != dates[index].isocalendar()[:2]


def simulate_arm(
    *,
    arm_id: str,
    dates: Sequence[date],
    risk_levels: Sequence[float],
    equity_levels: Sequence[float],
    defensive_levels: Sequence[float],
    scenario_label: Literal["base", "stress"],
    scenario: CostScenario,
    static_weight: float | None = None,
    volatility_lookback: int | None = None,
    target_volatility: float = 0.12,
) -> tuple[ArmMetrics, pl.DataFrame]:
    if not (len(dates) == len(risk_levels) == len(equity_levels) == len(defensive_levels)):
        raise ValueError("index input series are not aligned")
    if dates != sorted(set(dates)):
        raise ValueError("index dates must be strictly increasing")
    if (static_weight is None) == (volatility_lookback is None):
        raise ValueError("arm must define exactly one of static_weight or volatility_lookback")
    if static_weight is not None and not 0.0 <= static_weight <= 1.0:
        raise ValueError("static weight is outside [0,1]")
    max_lookback = 60
    action_index = max_lookback + 1
    if len(dates) <= action_index + 1:
        raise ValueError("index input has insufficient common warm-up")
    if any(not math.isfinite(value) or value <= 0.0 for value in [*risk_levels, *equity_levels, *defensive_levels]):
        raise ValueError("index levels must be finite and positive")

    signal_index = action_index - 1
    if static_weight is not None:
        weight = static_weight
    else:
        assert volatility_lookback is not None
        volatility = _realized_volatility(risk_levels, signal_index, volatility_lookback)
        weight = min(max(target_volatility / volatility, 0.0), 1.0)
    nav = INITIAL_CAPITAL_CNY
    initial_cost, initial_turnover = _initial_cost(nav, weight, scenario)
    nav -= initial_cost
    if nav <= 0.0:
        raise ValueError("initial implementation cost exhausted capital")
    rows: list[dict[str, Any]] = [
        {
            "date": dates[action_index],
            "equity_weight": weight,
            "net_return": nav / INITIAL_CAPITAL_CNY - 1.0,
            "trade_cost": initial_cost,
            "turnover": initial_turnover,
            "equity": nav,
        }
    ]
    for index in range(action_index + 1, len(dates)):
        equity_return = equity_levels[index] / equity_levels[index - 1] - 1.0
        defensive_return = defensive_levels[index] / defensive_levels[index - 1] - 1.0
        daily_drag = (
            weight * scenario.equity_proxy_annual_drag
            + (1.0 - weight) * scenario.defensive_proxy_annual_drag
        ) / TRADING_DAYS_PER_YEAR
        nav_before = nav
        nav *= 1.0 + weight * equity_return + (1.0 - weight) * defensive_return - daily_drag
        if not math.isfinite(nav) or nav <= 0.0:
            raise ValueError("portfolio NAV became non-positive")
        trade_cost = 0.0
        day_turnover = 0.0
        target = weight
        if volatility_lookback is not None and _first_market_day_of_week(dates, index):
            signal_index = index - 1
            volatility = _realized_volatility(risk_levels, signal_index, volatility_lookback)
            target = min(max(target_volatility / volatility, 0.0), 1.0)
            trade_cost, day_turnover = _trade_cost(nav, abs(target - weight), scenario)
            nav -= trade_cost
            if nav <= 0.0:
                raise ValueError("rebalance cost exhausted capital")
        net_return = nav / nav_before - 1.0
        weight = target
        rows.append(
            {
                "date": dates[index],
                "equity_weight": weight,
                "net_return": net_return,
                "trade_cost": trade_cost,
                "turnover": day_turnover,
                "equity": nav,
            }
        )
    frame = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    metrics = _metrics_from_equity(
        arm_id=arm_id,
        scenario=scenario_label,
        equity=[float(value) for value in frame.get_column("equity").to_list()],
        daily_returns=[float(value) for value in frame.get_column("net_return").to_list()],
        costs=[float(value) for value in frame.get_column("trade_cost").to_list()],
        turnover=[float(value) for value in frame.get_column("turnover").to_list()],
        weights=[float(value) for value in frame.get_column("equity_weight").to_list()],
    )
    return metrics, frame


def load_verified_index_inputs(
    *, repo_root: Path
) -> tuple[list[date], list[float], list[float], list[float], str, str]:
    root = Path(repo_root).resolve(strict=True)
    equity_snapshot = verify_csi_all_share_long_history_snapshot(
        repo_root=root,
        staging_dir=EQUITY_STAGING_DIR,
        snapshot_dir=EQUITY_SNAPSHOT_DIR,
        identity_contract_path=DEFAULT_EQUITY_IDENTITY_PATH,
    )
    defensive_manifest = verify_official_defensive_leg_history(repo_root=root)
    calendar = pl.read_parquet(root / EQUITY_SNAPSHOT_DIR / "calendar.parquet")
    risk = pl.read_parquet(root / EQUITY_SNAPSHOT_DIR / "price_index.parquet").sort("date")
    equity = pl.read_parquet(root / EQUITY_SNAPSHOT_DIR / "total_return_index.parquet").sort("date")
    defensive = pl.read_parquet(
        root / "data/research/csi-1-bond-2005-2024-v1/total_return_index.parquet"
    ).sort("date")
    dates = calendar.get_column("date").to_list()
    if any(type(item) is not date for item in dates):
        raise ValueError("verified index calendar contains invalid dates")
    for name, frame in (("risk", risk), ("equity", equity), ("defensive", defensive)):
        if frame.get_column("date").to_list() != dates:
            raise ValueError(f"{name} index dates do not exactly match the sealed calendar")
        if [item.date() for item in frame.get_column("available_at").to_list()] != dates:
            raise ValueError(f"{name} index available_at dates drifted")
    if dates[0] != HISTORICAL_START or dates[-1] != HISTORICAL_END:
        raise ValueError("verified index pair coverage drifted")
    return (
        dates,
        [float(value) for value in risk.get_column("close").to_list()],
        [float(value) for value in equity.get_column("close").to_list()],
        [float(value) for value in defensive.get_column("close").to_list()],
        equity_snapshot.snapshot_id,
        str(defensive_manifest["snapshot_id"]),
    )


def _calmar_from_returns(returns: Sequence[float]) -> float:
    nav = 1.0
    values = [nav]
    for value in returns:
        nav *= 1.0 + value
        if not math.isfinite(nav) or nav <= 0.0:
            raise ValueError("bootstrap return path became non-positive")
        values.append(nav)
    annualized = nav ** (TRADING_DAYS_PER_YEAR / len(returns)) - 1.0
    drawdown = _maximum_drawdown(values)
    return annualized / abs(drawdown) if drawdown < 0.0 else math.inf


def _circular_block_indices(
    *, length: int, block_length: int, randomizer: random.Random
) -> list[int]:
    indices: list[int] = []
    while len(indices) < length:
        start = randomizer.randrange(length)
        indices.extend((start + offset) % length for offset in range(block_length))
    return indices[:length]


def _bootstrap_probability_not_positive(
    *,
    candidate_returns: Sequence[float],
    static_returns: Mapping[str, Sequence[float]],
    replications: int,
    block_length: int,
    seed: int,
) -> float:
    length = len(candidate_returns)
    if any(len(values) != length for values in static_returns.values()):
        raise ValueError("bootstrap arm returns are not aligned")
    randomizer = random.Random(seed)
    non_positive = 0
    for _ in range(replications):
        indices = _circular_block_indices(
            length=length, block_length=block_length, randomizer=randomizer
        )
        candidate = _calmar_from_returns([candidate_returns[index] for index in indices])
        best_static = max(
            _calmar_from_returns([values[index] for index in indices])
            for values in static_returns.values()
        )
        if candidate - best_static <= 0.0:
            non_positive += 1
    return (non_positive + 1.0) / (replications + 1.0)


def _holm_adjust(probabilities: dict[str, float]) -> dict[str, float]:
    ordered = sorted(probabilities.items(), key=lambda item: item[1])
    adjusted: dict[str, float] = {}
    running = 0.0
    family_size = len(ordered)
    for rank, (name, value) in enumerate(ordered):
        running = max(running, min(1.0, (family_size - rank) * value))
        adjusted[name] = running
    return adjusted


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_parquet(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    try:
        frame.write_parquet(temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _power_review(repo_root: Path, path: Path) -> dict[str, Any]:
    resolved = resolve_repo_regular_file(path, repo_root=repo_root, field_name="power_review_path")
    try:
        review = json.loads(resolved.read_text())
    except Exception as exc:
        raise ValueError("index risk-budget power review is missing or invalid") from exc
    payload = {key: value for key, value in review.items() if key != "review_id"}
    if review.get("review_id") != _json_hash(payload):
        raise ValueError("index risk-budget power review self-hash mismatch")
    if review.get("family_outcome") != "evaluable_for_sealed_mde":
        raise ValueError("index risk-budget power review is not evaluable")
    return review


def build_index_risk_budget_historical_replay(
    *,
    repo_root: Path,
    authorization_path: Path = DEFAULT_AUTHORIZATION_PATH,
    power_review_path: Path = DEFAULT_POWER_REVIEW_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> IndexRiskBudgetHistoricalReport:
    root = Path(repo_root).resolve(strict=True)
    authorization = verify_run_authorization(repo_root=root, path=authorization_path)
    protocol = verify_index_etf_risk_budget_protocol(repo_root=root)
    ledger = verify_index_time_series_trial_ledger(repo_root=root)
    cost_contract = verify_index_research_product_cost_contract(repo_root=root)
    power_review = _power_review(root, power_review_path)
    dates, risk, equity, defensive, equity_snapshot_id, defensive_snapshot_id = (
        load_verified_index_inputs(repo_root=root)
    )
    expected_bindings = {
        "protocol_id": protocol.protocol_id,
        "trial_ledger_id": ledger.ledger_id,
        "defensive_snapshot_id": defensive_snapshot_id,
        "product_cost_contract_id": cost_contract.contract_id,
        "power_review_id": power_review["review_id"],
    }
    if any(getattr(authorization, key) != value for key, value in expected_bindings.items()):
        raise ValueError("index risk-budget authorization artifact binding mismatch")
    consumption_path = root / output_dir / "authorization-consumption.json"
    if consumption_path.exists():
        raise ValueError("index risk-budget historical replay authorization was already consumed")

    metrics: list[ArmMetrics] = []
    frames: dict[tuple[str, str], pl.DataFrame] = {}
    for scenario_label, scenario in cost_contract.research_cost_scenarios.items():
        for weight in (*STATIC_WEIGHTS, FULL_EQUITY_REFERENCE):
            arm_id = f"static_equity_{int(weight * 100):03d}"
            arm_metrics, frame = simulate_arm(
                arm_id=arm_id,
                dates=dates,
                risk_levels=risk,
                equity_levels=equity,
                defensive_levels=defensive,
                scenario_label=scenario_label,  # type: ignore[arg-type]
                scenario=scenario,
                static_weight=weight,
            )
            metrics.append(arm_metrics)
            frames[(scenario_label, arm_id)] = frame
        for candidate_id, lookback in (
            ("vol_target_20d_12pct_weekly_v1", 20),
            ("vol_target_60d_12pct_weekly_v1", 60),
        ):
            arm_metrics, frame = simulate_arm(
                arm_id=candidate_id,
                dates=dates,
                risk_levels=risk,
                equity_levels=equity,
                defensive_levels=defensive,
                scenario_label=scenario_label,  # type: ignore[arg-type]
                scenario=scenario,
                volatility_lookback=lookback,
            )
            metrics.append(arm_metrics)
            frames[(scenario_label, candidate_id)] = frame

    stress_metrics = {
        item.arm_id: item for item in metrics if item.scenario == "stress"
    }
    static_ids = [f"static_equity_{int(weight * 100):03d}" for weight in STATIC_WEIGHTS]
    def _stress_calmar(arm_id: str) -> float:
        value = stress_metrics[arm_id].calmar
        return value if value is not None else -math.inf

    best_static_id = max(static_ids, key=_stress_calmar)
    best_static = stress_metrics[best_static_id]
    static_returns = {
        arm_id: [
            float(value)
            for value in frames[("stress", arm_id)].get_column("net_return").to_list()
        ]
        for arm_id in static_ids
    }
    raw_probabilities: dict[str, float] = {}
    candidate_ids = ["vol_target_20d_12pct_weekly_v1", "vol_target_60d_12pct_weekly_v1"]
    for offset, candidate_id in enumerate(candidate_ids):
        raw_probabilities[candidate_id] = _bootstrap_probability_not_positive(
            candidate_returns=[
                float(value)
                for value in frames[("stress", candidate_id)].get_column("net_return").to_list()[1:]
            ],
            static_returns={arm_id: values[1:] for arm_id, values in static_returns.items()},
            replications=int(power_review["evaluation_bootstrap_replications"]),
            block_length=int(power_review["block_length_trading_days"]),
            seed=int(power_review["evaluation_bootstrap_seed"]) + offset,
        )
    adjusted = _holm_adjust(raw_probabilities)
    mde = float(power_review["sealed_mde_calmar_difference"])
    comparisons: list[CandidateComparison] = []
    for candidate_id in candidate_ids:
        candidate = stress_metrics[candidate_id]
        if candidate.calmar is None or best_static.calmar is None:
            raise ValueError("stress Calmar is undefined")
        difference = candidate.calmar - best_static.calmar
        pareto_dominated = any(
            stress_metrics[arm_id].annualized_return >= candidate.annualized_return
            and stress_metrics[arm_id].maximum_drawdown >= candidate.maximum_drawdown
            and (
                stress_metrics[arm_id].annualized_return > candidate.annualized_return
                or stress_metrics[arm_id].maximum_drawdown > candidate.maximum_drawdown
            )
            for arm_id in static_ids
        )
        mde_pass = difference >= mde
        significance = adjusted[candidate_id] <= 0.05
        drawdown_pass = candidate.maximum_drawdown >= -0.20
        pareto_pass = not pareto_dominated
        all_pass = mde_pass and significance and drawdown_pass and pareto_pass
        comparisons.append(
            CandidateComparison(
                candidate_id=candidate_id,
                best_static_arm_id=best_static_id,
                stress_calmar_difference=difference,
                bootstrap_probability_not_positive=raw_probabilities[candidate_id],
                holm_adjusted_probability=adjusted[candidate_id],
                sealed_mde_calmar_difference=mde,
                observed_improvement_meets_mde=mde_pass,
                familywise_significance_pass=significance,
                maximum_drawdown_floor_pass=drawdown_pass,
                not_pareto_dominated_pass=pareto_pass,
                stress_cost_primary_gate_pass=all_pass,
                all_hard_gates_pass=all_pass,
            )
        )
    report = IndexRiskBudgetHistoricalReport(
        authorization_id=authorization.authorization_id,
        protocol_id=protocol.protocol_id,
        trial_ledger_id=ledger.ledger_id,
        equity_snapshot_id=equity_snapshot_id,
        defensive_snapshot_id=defensive_snapshot_id,
        product_cost_contract_id=cost_contract.contract_id,
        power_review_id=str(power_review["review_id"]),
        evaluation_start=HISTORICAL_START,
        evaluation_end=HISTORICAL_END,
        first_invested_return_date=dates[62],
        execution_convention=(
            "signal_through_P_close_trade_at_D_close_previous_weight_earns_P_to_D_return"
        ),
        bootstrap={
            "replications": int(power_review["evaluation_bootstrap_replications"]),
            "block_length_trading_days": int(power_review["block_length_trading_days"]),
            "seed": int(power_review["evaluation_bootstrap_seed"]),
        },
        arm_metrics=metrics,
        candidate_comparisons=comparisons,
    )
    report = report.model_copy(
        update={"report_id": _json_hash(report.model_dump(mode="json", exclude={"report_id"}))}
    )
    destination = (root / output_dir).resolve()
    if not destination.is_relative_to(root):
        raise ValueError("historical replay output directory escapes repository")
    if destination.exists():
        raise FileExistsError("historical replay output already exists; single-use overwrite forbidden")
    combined: pl.DataFrame | None = None
    for (scenario_name, arm_id), frame in sorted(frames.items()):
        renamed = frame.rename(
            {
                "equity_weight": f"{scenario_name}__{arm_id}__equity_weight",
                "net_return": f"{scenario_name}__{arm_id}__net_return",
                "trade_cost": f"{scenario_name}__{arm_id}__trade_cost",
                "turnover": f"{scenario_name}__{arm_id}__turnover",
                "equity": f"{scenario_name}__{arm_id}__equity",
            }
        )
        combined = renamed if combined is None else combined.join(renamed, on="date", how="inner")
    assert combined is not None
    daily_path = destination / "daily-path.parquet"
    report_path = destination / "report.json"
    _atomic_parquet(daily_path, combined)
    _atomic_json(report_path, report.model_dump(mode="json"))
    consumption = {
        "schema_version": "1",
        "authorization_id": authorization.authorization_id,
        "report_id": report.report_id,
        "report_sha256": _sha256_file(report_path),
        "daily_path_sha256": _sha256_file(daily_path),
        "consumed_oos_reused": False,
        "ready_for_orders": False,
        "ready_for_trading": False,
    }
    _atomic_json(consumption_path, {**consumption, "receipt_id": _json_hash(consumption)})
    return report


__all__ = [
    "DEFAULT_AUTHORIZATION_PATH",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_POWER_REVIEW_PATH",
    "IndexRiskBudgetHistoricalReport",
    "IndexRiskBudgetRunAuthorization",
    "REQUIRED_CONFIRMATION_TEXT",
    "STATIC_WEIGHTS",
    "build_index_risk_budget_historical_replay",
    "compute_authorization_id",
    "load_verified_index_inputs",
    "simulate_arm",
    "verify_run_authorization",
]
