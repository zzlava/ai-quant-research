from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.research.index_time_series_trial_ledger import (
    DEFAULT_PATH,
    verify_index_time_series_trial_ledger,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_committed_index_time_series_trial_ledger_is_sealed() -> None:
    ledger = verify_index_time_series_trial_ledger(repo_root=PROJECT_ROOT)
    assert ledger.family_id == "index_time_series_risk_budget_v1"
    assert [item.realized_volatility_lookback_trading_days for item in ledger.hypotheses] == [20, 60]
    assert all(item.status == "registered_not_run" for item in ledger.hypotheses)
    assert ledger.ready_for_historical_replay is False


def test_index_time_series_trial_ledger_rejects_drift(tmp_path: Path) -> None:
    payload = json.loads((PROJECT_ROOT / DEFAULT_PATH).read_text())
    payload["hypotheses"][0]["annualized_target_volatility"] = 0.13
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="missing or invalid"):
        verify_index_time_series_trial_ledger(repo_root=tmp_path, path=Path("ledger.json"))
