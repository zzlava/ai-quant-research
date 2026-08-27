from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.research.defensive_leg_history import (
    DEFAULT_CONTRACT_PATH,
    materialize_official_defensive_leg_history,
    verify_defensive_leg_contract,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def fetch(self, url: str) -> bytes:
        assert "indexCode=H11010" in url
        return self.payload


def test_committed_defensive_leg_contract_is_sealed() -> None:
    contract = verify_defensive_leg_contract(repo_root=PROJECT_ROOT)
    assert contract.return_definition == "full_price_plus_coupon_reinvestment"
    assert contract.credit_risk_is_nonzero is True
    assert contract.ready_for_live_product_mapping is False


def test_defensive_leg_materialization_requires_exact_sealed_bytes(tmp_path: Path) -> None:
    calendar = pl.DataFrame({"date": [date(2005, 1, 4)]}, schema={"date": pl.Date})
    calendar_path = tmp_path / "calendar.parquet"
    calendar.write_parquet(calendar_path)
    payload = {
        "code": "200",
        "success": True,
        "data": [
            {"tradeDate": "20050101", "indexCode": "H11010", "close": 100.0},
            {"tradeDate": "20050104", "indexCode": "H11010", "close": 100.1},
        ],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    contract = json.loads((PROJECT_ROOT / DEFAULT_CONTRACT_PATH).read_text())
    contract["official_sources"]["history"]["sha256"] = __import__("hashlib").sha256(raw).hexdigest()
    contract["calendar_binding"] = {
        "path": "calendar.parquet",
        "sha256": __import__("hashlib").sha256(calendar_path.read_bytes()).hexdigest(),
        "expected_rows": 4858,
        "coverage_start": "2005-01-04",
        "coverage_end": "2024-12-31",
    }
    contract["staging_dir"] = "data/raw/csi-1-bond-2005-2024-v1"
    contract["snapshot_dir"] = "data/research/csi-1-bond-2005-2024-v1"
    without_id = {key: value for key, value in contract.items() if key != "contract_id"}
    contract["contract_id"] = __import__("hashlib").sha256(
        json.dumps(without_id, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    with pytest.raises(ValueError, match="calendar schema or row count"):
        materialize_official_defensive_leg_history(
            repo_root=tmp_path,
            client=_FakeClient(raw),
            contract_path=Path("contract.json"),
        )
