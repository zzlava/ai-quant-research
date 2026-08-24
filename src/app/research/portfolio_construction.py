from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.backtest.engine import BacktestEngine
from app.models.backtest import BacktestResult
from app.models.config import MarketGateBand, StrategyConfig
from app.models.scores import ScoreResult
from app.scoring.engine import ScoringEngine
from app.storage.protocol import MarketStore

ScoreFn = Callable[[date], list[ScoreResult]]
ProgressFn = Callable[[str, int, int, str], None]


class PortfolioCandidate(BaseModel):
    candidate_id: str
    max_positions: int
    holding_days: int
    signal_interval_days: int
    market_gate_max_new_positions: list[int]
    config_hash: str


class PeriodResult(BaseModel):
    label: str
    start: date
    end: date
    signal_cutoff: date
    total_return: float
    annualized_return: float | None
    sharpe_ratio: float | None
    max_drawdown: float | None
    number_of_trades: int
    win_rate: float | None
    final_equity: float
    total_trading_costs: float
    orders_generated: int
    orders_filled: int
    open_positions_at_end: int


class CandidateEvaluation(BaseModel):
    candidate: PortfolioCandidate
    training: PeriodResult
    validation: PeriodResult
    training_eligible: bool
    training_exclusion_reason: str | None = None


class HoldoutResult(BaseModel):
    candidate: PortfolioCandidate
    period: PeriodResult


class BenchmarkResult(BaseModel):
    symbol: str
    start: date
    end: date
    observations: int
    total_return: float
    sharpe_ratio: float | None
    max_drawdown: float
    benchmark_type: str = "price_index_excluding_dividends"


class PortfolioConstructionReport(BaseModel):
    strategy_config_hash: str
    scoring_config_id: str
    data_snapshot_id: str
    initial_cash: float
    weighting: str
    candidate_positions: list[int]
    candidate_holding_days: list[int]
    gate_scaling_rule: str
    selection_rule: str
    minimum_training_trades: int
    liquidation_buffer_days: int
    selected_candidate_id: str
    selected_config_hash: str
    selected_config_path: str
    evaluations: list[CandidateEvaluation] = Field(default_factory=list)
    holdout: HoldoutResult | None = None
    holdout_benchmark: BenchmarkResult | None = None
    holdout_return_minus_benchmark: float | None = None
    score_cache_hits: int = 0
    score_cache_misses: int = 0
    holdout_policy: str = "winner_only_after_selection_and_config_freeze"


class CachedScoreProvider:
    """Content-addressed score cache for fixed-horizon construction experiments.

    Portfolio, execution-cost, gate and holding-period settings do not alter
    cross-sectional scores. Cached rows intentionally omit feature vectors;
    fixed-horizon exits do not need ATR values.
    """

    def __init__(
        self,
        *,
        store: MarketStore,
        config: StrategyConfig,
        cache_root: Path,
    ) -> None:
        if config.trade.exit_policy != "fixed_horizon":
            raise ValueError("portfolio construction score cache requires fixed_horizon exits")
        self.store = store
        self.config = config
        self.engine = ScoringEngine(store, config)
        self.snapshot_id = store.snapshot().snapshot_id
        self.scoring_config_id = scoring_config_id(config)
        self.cache_dir = cache_root / self.snapshot_id / self.scoring_config_id
        self.memory: dict[date, list[ScoreResult]] = {}
        self.hits = 0
        self.misses = 0

    def __call__(self, as_of: date) -> list[ScoreResult]:
        if as_of in self.memory:
            self.hits += 1
            return self.memory[as_of]
        path = self.cache_dir / f"{as_of.isoformat()}.json"
        if path.exists():
            results = self._read(path, as_of)
            self.hits += 1
        else:
            results = self.engine.run(as_of)
            self._write(path, as_of, results)
            self.misses += 1
        self.memory[as_of] = results
        return results

    def _read(self, path: Path, as_of: date) -> list[ScoreResult]:
        outer = json.loads(path.read_text(encoding="utf-8"))
        payload = outer.get("payload")
        digest = outer.get("payload_sha256")
        if not isinstance(payload, dict) or not isinstance(digest, str):
            raise ValueError(f"invalid score cache envelope: {path}")
        encoded = _canonical_json(payload)
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError(f"score cache hash mismatch: {path}")
        expected = {
            "schema_version": 1,
            "score_date": as_of.isoformat(),
            "data_snapshot_id": self.snapshot_id,
            "scoring_config_id": self.scoring_config_id,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise ValueError(f"score cache {key} mismatch: {path}")
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise ValueError(f"score cache results must be a list: {path}")
        results = [ScoreResult.model_validate(item) for item in raw_results]
        if any(result.score_date != as_of for result in results):
            raise ValueError(f"score cache contains another decision date: {path}")
        if any(result.data_snapshot_id != self.snapshot_id for result in results):
            raise ValueError(f"score cache contains another data snapshot: {path}")
        return results

    def _write(self, path: Path, as_of: date, results: list[ScoreResult]) -> None:
        payload = {
            "schema_version": 1,
            "score_date": as_of.isoformat(),
            "data_snapshot_id": self.snapshot_id,
            "scoring_config_id": self.scoring_config_id,
            "results": [
                result.model_dump(mode="json", exclude={"feature"}) for result in results
            ],
        }
        encoded = _canonical_json(payload)
        outer = {
            "payload_sha256": hashlib.sha256(encoded).hexdigest(),
            "payload": payload,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(outer, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def scoring_config_id(config: StrategyConfig) -> str:
    payload = config.model_dump(mode="json", exclude={"source_path"})
    for field in ("config_id", "version", "market_gate", "trade", "portfolio", "costs"):
        payload.pop(field, None)
    return hashlib.sha256(_canonical_json(payload)).hexdigest()[:16]


def build_candidate_config(
    base: StrategyConfig,
    *,
    max_positions: int,
    holding_days: int,
) -> StrategyConfig:
    if max_positions <= 0 or holding_days <= 0:
        raise ValueError("candidate positions and holding days must be positive")
    if base.trade.exit_policy != "fixed_horizon":
        raise ValueError("portfolio construction search requires fixed_horizon exits")
    if base.portfolio.weighting != "equal_weight":
        raise ValueError("portfolio construction search currently supports equal_weight only")
    scaled_gate = _scale_market_gate(base, max_positions)
    return base.model_copy(
        update={
            "config_id": (
                f"{base.run_id()}_portfolio_p{max_positions}_h{holding_days}_selected_v2"
            ),
            "version": "2.0.0",
            "market_gate": scaled_gate,
            "trade": base.trade.model_copy(
                update={
                    "min_holding_days": holding_days,
                    "max_holding_days": holding_days,
                    "signal_interval_days": holding_days,
                }
            ),
            "portfolio": base.portfolio.model_copy(update={"max_positions": max_positions}),
        }
    )


def select_portfolio_construction(
    *,
    store: MarketStore,
    base_config: StrategyConfig,
    positions: list[int],
    holding_days: list[int],
    training_start: date,
    training_end: date,
    validation_start: date,
    validation_end: date,
    minimum_training_trades: int,
    liquidation_buffer_days: int,
    score_fn: ScoreFn,
    progress: ProgressFn | None = None,
) -> tuple[PortfolioConstructionReport, StrategyConfig]:
    _validate_search(
        base_config,
        positions,
        holding_days,
        training_start,
        training_end,
        validation_start,
        validation_end,
        minimum_training_trades,
        liquidation_buffer_days,
    )
    candidates = [
        build_candidate_config(base_config, max_positions=count, holding_days=horizon)
        for count in sorted(set(positions))
        for horizon in sorted(set(holding_days))
    ]
    total_runs = len(candidates) * 2
    completed = 0
    evaluations: list[CandidateEvaluation] = []
    max_horizon = max(holding_days)
    for candidate in candidates:
        spec = _candidate_spec(candidate)
        training = _run_period(
            store=store,
            config=candidate,
            label="training",
            start=training_start,
            end=training_end,
            maximum_candidate_horizon=max_horizon,
            liquidation_buffer_days=liquidation_buffer_days,
            score_fn=score_fn,
        )
        completed += 1
        if progress is not None:
            progress("training", completed, total_runs, spec.candidate_id)
        validation = _run_period(
            store=store,
            config=candidate,
            label="validation",
            start=validation_start,
            end=validation_end,
            maximum_candidate_horizon=max_horizon,
            liquidation_buffer_days=liquidation_buffer_days,
            score_fn=score_fn,
        )
        completed += 1
        if progress is not None:
            progress("validation", completed, total_runs, spec.candidate_id)
        reason = _training_exclusion_reason(training, minimum_training_trades)
        evaluations.append(
            CandidateEvaluation(
                candidate=spec,
                training=training,
                validation=validation,
                training_eligible=reason is None,
                training_exclusion_reason=reason,
            )
        )
    eligible = [item for item in evaluations if item.training_eligible]
    if not eligible:
        raise ValueError("no portfolio candidate passed the training feasibility screen")
    selectable = [item for item in eligible if _validation_is_usable(item.validation)]
    if not selectable:
        raise ValueError("no training-eligible candidate has usable validation metrics")
    selected = max(selectable, key=_validation_selection_key)
    selected_config = next(
        config for config in candidates if config.config_hash() == selected.candidate.config_hash
    )
    snapshot_id = store.snapshot().snapshot_id
    report = PortfolioConstructionReport(
        strategy_config_hash=base_config.config_hash(),
        scoring_config_id=scoring_config_id(base_config),
        data_snapshot_id=snapshot_id,
        initial_cash=base_config.portfolio.initial_cash,
        weighting=base_config.portfolio.weighting,
        candidate_positions=sorted(set(positions)),
        candidate_holding_days=sorted(set(holding_days)),
        gate_scaling_rule=(
            "round_half_up(base_max_new_positions * candidate_max_positions / "
            "base_max_positions), capped at candidate_max_positions"
        ),
        selection_rule=(
            "training requires finite metrics, zero open positions, and minimum closed trades; "
            "among eligible candidates maximize validation Sharpe, then validation total return, "
            "then smaller validation drawdown and lower cost; final ties prefer fewer positions "
            "and longer holding"
        ),
        minimum_training_trades=minimum_training_trades,
        liquidation_buffer_days=liquidation_buffer_days,
        selected_candidate_id=selected.candidate.candidate_id,
        selected_config_hash=selected.candidate.config_hash,
        selected_config_path="",
        evaluations=evaluations,
    )
    return report, selected_config


def evaluate_holdout(
    *,
    store: MarketStore,
    selected_config: StrategyConfig,
    start: date,
    end: date,
    maximum_candidate_horizon: int,
    liquidation_buffer_days: int,
    score_fn: ScoreFn,
) -> HoldoutResult:
    period = _run_period(
        store=store,
        config=selected_config,
        label="holdout",
        start=start,
        end=end,
        maximum_candidate_horizon=maximum_candidate_horizon,
        liquidation_buffer_days=liquidation_buffer_days,
        score_fn=score_fn,
    )
    return HoldoutResult(candidate=_candidate_spec(selected_config), period=period)


def evaluate_price_index_benchmark(
    *, store: MarketStore, symbol: str, start: date, end: date
) -> BenchmarkResult:
    frame = store.get_index_bars(as_of=end, symbol=symbol, start=start).sort("date")
    if frame.is_empty() or "close" not in frame.columns or "date" not in frame.columns:
        raise ValueError(f"price-index benchmark is unavailable for {symbol}")
    rows = frame.select(["date", "close"]).drop_nulls().to_dicts()
    if len(rows) < 3:
        raise ValueError(f"price-index benchmark has fewer than 3 observations for {symbol}")
    prices = [float(row["close"]) for row in rows]
    if any(price <= 0 or not math.isfinite(price) for price in prices):
        raise ValueError(f"price-index benchmark contains invalid closes for {symbol}")
    returns = [
        current / previous - 1.0
        for previous, current in zip(prices, prices[1:], strict=False)
    ]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    standard_deviation = math.sqrt(variance)
    sharpe = mean / standard_deviation * math.sqrt(242) if standard_deviation > 0 else None
    peak = prices[0]
    max_drawdown = 0.0
    for price in prices:
        peak = max(peak, price)
        max_drawdown = min(max_drawdown, price / peak - 1.0)
    first_day = rows[0]["date"]
    last_day = rows[-1]["date"]
    if not isinstance(first_day, date) or not isinstance(last_day, date):
        raise ValueError(f"price-index benchmark has invalid dates for {symbol}")
    return BenchmarkResult(
        symbol=symbol,
        start=first_day,
        end=last_day,
        observations=len(rows),
        total_return=prices[-1] / prices[0] - 1.0,
        sharpe_ratio=sharpe,
        max_drawdown=max_drawdown,
    )


def write_selected_config(config: StrategyConfig, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json", exclude={"source_path"})
    header = (
        "# Frozen by analyze-portfolio-construction before the holdout run.\n"
        "# Only portfolio capacity, fixed horizon/signal interval, and proportional gate capacity\n"
        "# differ from the declared base value strategy. Initial capital remains 80000.\n"
    )
    output.write_text(
        header + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def write_portfolio_construction_report(report: PortfolioConstructionReport, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")


def _run_period(
    *,
    store: MarketStore,
    config: StrategyConfig,
    label: str,
    start: date,
    end: date,
    maximum_candidate_horizon: int,
    liquidation_buffer_days: int,
    score_fn: ScoreFn,
) -> PeriodResult:
    calendar = store.get_calendar(start, end)
    warm_down = maximum_candidate_horizon + liquidation_buffer_days
    if len(calendar) <= warm_down + 2:
        raise ValueError(f"{label} window is too short for the declared liquidation buffer")
    signal_cutoff = calendar[-(warm_down + 1)]

    result = run_backtest_with_signal_cutoff(
        store=store,
        config=config,
        start=start,
        end=end,
        signal_cutoff=signal_cutoff,
        score_fn=score_fn,
    )
    return _period_result(label, signal_cutoff, result)


def run_backtest_with_signal_cutoff(
    *,
    store: MarketStore,
    config: StrategyConfig,
    start: date,
    end: date,
    signal_cutoff: date,
    score_fn: ScoreFn,
) -> BacktestResult:
    """Run a declared closed-window test without opening positions in its warm-down tail."""
    if signal_cutoff < start or signal_cutoff > end:
        raise ValueError("signal_cutoff must be inside the backtest window")

    def bounded_scores(day: date) -> list[ScoreResult]:
        return score_fn(day) if day <= signal_cutoff else []

    return BacktestEngine(store, config, signal_fn=bounded_scores).run(start, end)


def _period_result(label: str, signal_cutoff: date, result: BacktestResult) -> PeriodResult:
    metrics = result.metrics
    return PeriodResult(
        label=label,
        start=result.start,
        end=result.end,
        signal_cutoff=signal_cutoff,
        total_return=metrics.total_return,
        annualized_return=metrics.annualized_return,
        sharpe_ratio=metrics.sharpe_ratio,
        max_drawdown=metrics.max_drawdown,
        number_of_trades=metrics.number_of_trades,
        win_rate=metrics.win_rate,
        final_equity=metrics.final_equity,
        total_trading_costs=result.attribution.total_trading_costs,
        orders_generated=result.attribution.signal.orders_generated,
        orders_filled=result.attribution.signal.orders_filled,
        open_positions_at_end=result.open_positions_at_end,
    )


def _training_exclusion_reason(period: PeriodResult, minimum_trades: int) -> str | None:
    if period.open_positions_at_end:
        return "training window still has open positions after the liquidation buffer"
    if period.number_of_trades < minimum_trades:
        return f"training closed trades {period.number_of_trades} < {minimum_trades}"
    if period.sharpe_ratio is None or period.max_drawdown is None:
        return "training risk metrics are not finite"
    if not math.isfinite(period.total_return) or period.final_equity <= 0:
        return "training equity is invalid"
    return None


def _validation_selection_key(item: CandidateEvaluation) -> tuple[float, ...]:
    period = item.validation
    sharpe = period.sharpe_ratio
    drawdown = period.max_drawdown
    if sharpe is None or drawdown is None:
        raise ValueError("selection key requires usable validation metrics")
    return (
        sharpe,
        period.total_return,
        drawdown,
        -period.total_trading_costs,
        -float(item.candidate.max_positions),
        float(item.candidate.holding_days),
        -float(item.candidate.signal_interval_days),
    )


def _validation_is_usable(period: PeriodResult) -> bool:
    return (
        period.open_positions_at_end == 0
        and period.number_of_trades > 0
        and period.sharpe_ratio is not None
        and period.max_drawdown is not None
        and math.isfinite(period.total_return)
        and period.final_equity > 0
    )


def _candidate_spec(config: StrategyConfig) -> PortfolioCandidate:
    return PortfolioCandidate(
        candidate_id=f"p{config.portfolio.max_positions}_h{config.trade.max_holding_days}",
        max_positions=config.portfolio.max_positions,
        holding_days=config.trade.max_holding_days,
        signal_interval_days=config.trade.signal_interval_days,
        market_gate_max_new_positions=[band.max_new_positions for band in config.market_gate],
        config_hash=config.config_hash(),
    )


def _scale_market_gate(base: StrategyConfig, max_positions: int) -> list[MarketGateBand]:
    denominator = base.portfolio.max_positions
    return [
        band.model_copy(
            update={
                "max_new_positions": min(
                    max_positions,
                    int(math.floor(band.max_new_positions * max_positions / denominator + 0.5)),
                )
            }
        )
        for band in base.market_gate
    ]


def _validate_search(
    base: StrategyConfig,
    positions: list[int],
    holding_days: list[int],
    training_start: date,
    training_end: date,
    validation_start: date,
    validation_end: date,
    minimum_training_trades: int,
    liquidation_buffer_days: int,
) -> None:
    if base.portfolio.initial_cash != 80_000:
        raise ValueError("this milestone requires initial_cash=80000")
    if not positions or min(positions) <= 0:
        raise ValueError("positions must contain positive counts")
    if not holding_days or min(holding_days) <= 0:
        raise ValueError("holding_days must contain positive values")
    if training_end < training_start or validation_end < validation_start:
        raise ValueError("research window end must not precede start")
    if training_end >= validation_start:
        raise ValueError("training and validation windows must not overlap")
    if minimum_training_trades <= 0:
        raise ValueError("minimum_training_trades must be positive")
    if liquidation_buffer_days < 0:
        raise ValueError("liquidation_buffer_days must be non-negative")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
