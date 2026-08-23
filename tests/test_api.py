from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.demo.generator import generate_demo_market, write_demo_parquet
from tests.helpers import PROJECT_ROOT


def test_health_and_strategies() -> None:
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    listed = client.get("/strategies")
    assert listed.status_code == 200
    names = {item["name"] for item in listed.json()}
    assert "baseline_v1" in names
    assert "controlled_sample_anchor_intersection30_v1" in names
    assert "all_a_share_latest_v1" in names
    assert "baseline_real_cn_v1.example" not in names


def test_ranking_and_backtest_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("AIQ_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    monkeypatch.setenv("AIQ_DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    bundle = generate_demo_market(
        seed=42,
        n_stocks=12,
        start=date(2023, 1, 3),
        end=date(2024, 3, 29),
    )
    write_demo_parquet(bundle, data_dir / "parquet")

    client = TestClient(app)
    ranking = client.get("/ranking", params={"date": "2024-01-15", "strategy": "baseline_v1", "top": 5})
    assert ranking.status_code == 200
    body = ranking.json()
    assert body["items"]
    assert "final_score" in body["items"][0]
    assert "breakdown" in body["items"][0]
    assert body["data_snapshot_id"]
    assert body["items"][0]["data_snapshot_id"] == body["data_snapshot_id"]

    created = client.post(
        "/backtests",
        json={"strategy": "baseline_v1", "start": "2024-01-02", "end": "2024-01-31"},
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["status"] == "done"
    result = payload["result"]
    assert result["window"]["valuation_end"] <= "2024-01-31"
    assert result["equity_curve"][-1]["date"] <= "2024-01-31"
    assert result["data_snapshot_id"] == body["data_snapshot_id"]
    assert result["data_snapshot"]["snapshot_id"] == body["data_snapshot_id"]

    fetched = client.get(f"/backtests/{payload['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "done"
    assert fetched.json()["result"]["window"]["valuation_end"] == result["window"]["valuation_end"]
