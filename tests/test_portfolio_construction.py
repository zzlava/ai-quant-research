from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest
import yaml

from app.models.config import StrategyConfig
from app.research.portfolio_construction import (
    CachedScoreProvider,
    CandidateEvaluation,
    PeriodResult,
    PortfolioCandidate,
    _validation_selection_key,
    build_candidate_config,
    evaluate_price_index_benchmark,
    scoring_config_id,
    write_selected_config,
)
from tests.helpers import constant_signal, fill_quiet_bars, store_from_rows, weekdays, zero_cost_config


def _fixed_horizon_config() -> StrategyConfig:
    base = zero_cost_config()
    return base.model_copy(
        update={
            "trade": base.trade.model_copy(
                update={
                    "exit_policy": "fixed_horizon",
                    "min_holding_days": 20,
                    "max_holding_days": 20,
                    "signal_interval_days": 20,
                    "signal_anchor_date": date(2024, 1, 2),
                }
            )
        }
    )


def _period(label: str, *, sharpe: float, total_return: float, cost: float = 100.0) -> PeriodResult:
    return PeriodResult(
        label=label,
        start=date(2024, 1, 2),
        end=date(2024, 12, 31),
        signal_cutoff=date(2024, 11, 1),
        total_return=total_return,
        annualized_return=total_return,
        sharpe_ratio=sharpe,
        max_drawdown=-0.10,
        number_of_trades=10,
        win_rate=0.5,
        final_equity=80_000 * (1 + total_return),
        total_trading_costs=cost,
        orders_generated=10,
        orders_filled=10,
        open_positions_at_end=0,
    )


def _evaluation(candidate_id: str, positions: int, sharpe: float, total_return: float) -> CandidateEvaluation:
    candidate = PortfolioCandidate(
        candidate_id=candidate_id,
        max_positions=positions,
        holding_days=20,
        signal_interval_days=20,
        market_gate_max_new_positions=[0, 1, 2, positions],
        config_hash=candidate_id,
    )
    return CandidateEvaluation(
        candidate=candidate,
        training=_period("training", sharpe=0.1, total_return=0.01),
        validation=_period("validation", sharpe=sharpe, total_return=total_return),
        training_eligible=True,
    )


def test_candidate_changes_only_declared_construction_fields_and_preserves_cash(tmp_path) -> None:
    base = _fixed_horizon_config()
    candidate = build_candidate_config(base, max_positions=8, holding_days=40)

    assert candidate.portfolio.initial_cash == 80_000
    assert candidate.portfolio.weighting == "equal_weight"
    assert candidate.portfolio.max_positions == 8
    assert candidate.trade.min_holding_days == 40
    assert candidate.trade.max_holding_days == 40
    assert candidate.trade.signal_interval_days == 40
    assert [band.max_new_positions for band in candidate.market_gate] == [0, 3, 5, 8]
    assert scoring_config_id(candidate) == scoring_config_id(base)

    output = tmp_path / "selected.yaml"
    write_selected_config(candidate, output)
    loaded = StrategyConfig.model_validate(yaml.safe_load(output.read_text(encoding="utf-8")))
    assert loaded.config_hash() == candidate.config_hash()


def test_selection_key_prioritizes_validation_sharpe_before_return() -> None:
    higher_return = _evaluation("higher-return", 3, sharpe=0.5, total_return=0.20)
    higher_sharpe = _evaluation("higher-sharpe", 8, sharpe=0.6, total_return=0.05)

    assert max([higher_return, higher_sharpe], key=_validation_selection_key) is higher_sharpe


def test_score_cache_round_trip_and_hash_tamper_detection(tmp_path) -> None:
    calendar = weekdays(date(2024, 1, 2), 3)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    config = _fixed_horizon_config()
    snapshot_id = store.snapshot().snapshot_id
    results = [
        result.model_copy(update={"data_snapshot_id": snapshot_id})
        for result in constant_signal(["AAA"], 60.0, calendar[0])
    ]
    writer = CachedScoreProvider(store=store, config=config, cache_root=tmp_path)
    path = writer.cache_dir / f"{calendar[0].isoformat()}.json"
    writer._write(path, calendar[0], results)

    reader = CachedScoreProvider(store=store, config=config, cache_root=tmp_path)
    loaded = reader(calendar[0])
    assert [item.symbol for item in loaded] == ["AAA"]
    assert loaded[0].feature is None
    assert reader.hits == 1
    assert reader.misses == 0

    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["results"][0]["final_score"] = 1.0
    path.write_text(json.dumps(envelope), encoding="utf-8")
    tampered = CachedScoreProvider(store=store, config=config, cache_root=tmp_path)
    with pytest.raises(ValueError, match="hash mismatch"):
        tampered(calendar[0])


def test_price_index_benchmark_uses_declared_window_closes() -> None:
    calendar = weekdays(date(2024, 1, 2), 3)
    store = store_from_rows(calendar, fill_quiet_bars("AAA", calendar))
    store.index = pl.DataFrame(
        {
            "symbol": ["IDX", "IDX", "IDX"],
            "date": calendar,
            "close": [100.0, 90.0, 110.0],
        }
    ).with_columns(pl.col("date").cast(pl.Date))

    result = evaluate_price_index_benchmark(
        store=store, symbol="IDX", start=calendar[0], end=calendar[-1]
    )

    assert result.observations == 3
    assert result.total_return == pytest.approx(0.10)
    assert result.max_drawdown == pytest.approx(-0.10)
    assert result.benchmark_type == "price_index_excluding_dividends"
