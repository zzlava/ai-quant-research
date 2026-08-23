from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.main import app


def test_health_and_strategies() -> None:
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    listed = client.get("/strategies")
    assert listed.status_code == 200
    names = {item["name"] for item in listed.json()}
    assert "baseline_v1" in names
