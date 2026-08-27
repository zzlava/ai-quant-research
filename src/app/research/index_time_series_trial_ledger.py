"""Fail-closed ledger for the index time-series risk-budget family."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.repo_file_safety import resolve_repo_regular_file

DEFAULT_PATH = Path("config/research/index-time-series-trial-ledger-v1.json")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisteredHypothesis(_StrictModel):
    trial_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    declared_before_historical_replay: Literal[True]
    status: Literal["registered_not_run"]
    realized_volatility_lookback_trading_days: int = Field(ge=1)
    annualized_target_volatility: float = Field(gt=0.0)
    rebalance_frequency: Literal["weekly_first_market_trading_day"]
    signal_lag: Literal["prior_market_close_only_T_plus_1_action"]
    primary_endpoint: Literal["net_of_cost_calmar_difference_vs_best_static_grid_arm"]
    historical_replay_consumed: Literal[False]
    prospective_record_consumed: Literal[False]


class IndexTimeSeriesTrialLedger(_StrictModel):
    schema_version: Literal["1"]
    ledger_version: Literal["index-time-series-trial-ledger-v1"]
    ledger_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_as_of: date
    family_id: Literal["index_time_series_risk_budget_v1"]
    separate_from_closed_individual_stock_family: Literal[True]
    inherited_individual_stock_trial_count: Literal[0]
    complete_for_current_family: Literal[True]
    familywise_alpha: float = Field(gt=0.0, lt=1.0)
    correction: Literal["holm"]
    hypotheses: list[RegisteredHypothesis]
    parameter_scanning_forbidden: Literal[True]
    result_dependent_additions_forbidden: Literal[True]
    ready_for_historical_replay: Literal[False]
    ready_for_prospective_evaluation: Literal[False]
    ready_for_orders: Literal[False]
    ready_for_trading: Literal[False]

    @model_validator(mode="after")
    def _fail_closed(self) -> IndexTimeSeriesTrialLedger:
        if self.created_as_of != date(2026, 8, 27):
            raise ValueError("index trial ledger creation date drifted")
        if self.familywise_alpha != 0.05:
            raise ValueError("index trial familywise alpha drifted")
        expected = {
            "vol_target_20d_12pct_weekly_v1": 20,
            "vol_target_60d_12pct_weekly_v1": 60,
        }
        observed = {
            item.candidate_id: item.realized_volatility_lookback_trading_days
            for item in self.hypotheses
        }
        if observed != expected or len(self.hypotheses) != 2:
            raise ValueError("index trial family must contain exactly the two sealed candidates")
        if len({item.trial_id for item in self.hypotheses}) != 2:
            raise ValueError("index trial IDs must be unique")
        if any(item.annualized_target_volatility != 0.12 for item in self.hypotheses):
            raise ValueError("index trial target volatility drifted")
        return self


def _canonical_payload(ledger: IndexTimeSeriesTrialLedger) -> dict[str, Any]:
    return ledger.model_dump(mode="json", exclude={"ledger_id"})


def compute_ledger_id(ledger: IndexTimeSeriesTrialLedger) -> str:
    encoded = json.dumps(
        _canonical_payload(ledger), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def verify_index_time_series_trial_ledger(
    *, repo_root: Path, path: Path = DEFAULT_PATH
) -> IndexTimeSeriesTrialLedger:
    root = Path(repo_root).resolve(strict=True)
    resolved = resolve_repo_regular_file(path, repo_root=root, field_name="ledger_path")
    try:
        ledger = IndexTimeSeriesTrialLedger.model_validate_json(resolved.read_text())
    except Exception as exc:
        raise ValueError("index time-series trial ledger is missing or invalid") from exc
    if ledger.ledger_id != compute_ledger_id(ledger):
        raise ValueError("index time-series trial ledger self-hash mismatch")
    return ledger


__all__ = [
    "DEFAULT_PATH",
    "IndexTimeSeriesTrialLedger",
    "compute_ledger_id",
    "verify_index_time_series_trial_ledger",
]
