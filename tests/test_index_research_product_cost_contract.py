from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.research.index_research_product_cost_contract import (
    DEFAULT_CONTRACT_PATH,
    materialize_official_product_cost_evidence,
    verify_index_research_product_cost_contract,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeClient:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def fetch(self, url: str) -> bytes:
        return self.payloads[url]


def test_committed_product_cost_contract_and_evidence_verify() -> None:
    contract = verify_index_research_product_cost_contract(repo_root=PROJECT_ROOT)
    assert contract.readiness["ready_for_index_level_historical_replay"] is True
    assert contract.readiness["ready_for_live_product_mapping"] is False
    assert contract.exact_live_product_findings["equity_exact_linked_product_count"] == 0
    assert contract.research_cost_scenarios["stress"].slippage_bps_per_side == 15.0


def test_materializer_refuses_any_evidence_hash_drift(tmp_path: Path) -> None:
    payload = json.loads((PROJECT_ROOT / DEFAULT_CONTRACT_PATH).read_text())
    payload["official_evidence"] = {
        name: {**item, "raw_path": f"data/raw/evidence/{name}.bin"}
        for name, item in payload["official_evidence"].items()
    }
    without_id = {key: value for key, value in payload.items() if key != "contract_id"}
    payload["contract_id"] = __import__("hashlib").sha256(
        json.dumps(without_id, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(payload))
    client = _FakeClient(
        {item["url"]: b"drifted" for item in payload["official_evidence"].values()}
    )
    with pytest.raises(ValueError, match="bytes drifted"):
        materialize_official_product_cost_evidence(
            repo_root=tmp_path, client=client, contract_path=Path("contract.json")
        )


def test_product_query_is_recomputed_not_only_hash_checked(tmp_path: Path) -> None:
    payload = json.loads((PROJECT_ROOT / DEFAULT_CONTRACT_PATH).read_text())
    source = b'{"code":"200","msg":"Success","data":[{"fund":"x"}],"success":true}'
    digest = __import__("hashlib").sha256(source).hexdigest()
    for key in ("equity_exact_product_query", "defensive_exact_product_query"):
        payload["official_evidence"][key]["sha256"] = digest
        payload["official_evidence"][key]["raw_path"] = f"data/raw/{key}.json"
    without_id = {key: value for key, value in payload.items() if key != "contract_id"}
    payload["contract_id"] = __import__("hashlib").sha256(
        json.dumps(without_id, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(payload))
    for key in ("equity_exact_product_query", "defensive_exact_product_query"):
        path = tmp_path / payload["official_evidence"][key]["raw_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source)
    for key, item in payload["official_evidence"].items():
        if key in ("equity_exact_product_query", "defensive_exact_product_query"):
            continue
        path = tmp_path / item["raw_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((PROJECT_ROOT / item["raw_path"]).read_bytes())
    with pytest.raises(ValueError, match="no longer proves"):
        verify_index_research_product_cost_contract(
            repo_root=tmp_path, path=Path("contract.json")
        )
