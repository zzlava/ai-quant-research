from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.research.index_shadow_execution import (
    DEFAULT_PROTOCOL_PATH,
    IndexShadowExecutionProtocol,
    _optimal_board_lots,
    verify_index_shadow_execution_protocol,
    verify_index_shadow_initialization_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _payload() -> dict[str, object]:
    return json.loads((PROJECT_ROOT / DEFAULT_PROTOCOL_PATH).read_text())


def test_committed_shadow_protocol_is_self_hashed_without_local_evidence() -> None:
    protocol = verify_index_shadow_execution_protocol(
        repo_root=PROJECT_ROOT,
        require_evidence=False,
    )
    assert protocol.product_mappings["equity"].symbol == "510300.SH"
    assert protocol.product_mappings["defensive"].symbol == "511010.SH"
    assert all(
        product.exact_research_proxy_match is False
        for product in protocol.product_mappings.values()
    )
    assert protocol.authorization_boundary["capital_deployment_authorized"] is False
    assert protocol.authorization_boundary["broker_connection_authorized"] is False
    assert protocol.readiness["ready_for_orders"] is False
    assert protocol.readiness["ready_for_trading"] is False


def test_shadow_protocol_rejects_authorization_or_rebalance_drift() -> None:
    payload = _payload()
    boundary = payload["authorization_boundary"]
    assert isinstance(boundary, dict)
    boundary["trading_authorized"] = True
    with pytest.raises(ValidationError, match="authorization boundary drifted"):
        IndexShadowExecutionProtocol.model_validate(payload)

    payload = _payload()
    observation = payload["observation_policy"]
    assert isinstance(observation, dict)
    observation["initial_shadow_ledger_creation_is_not_a_policy_rebalance"] = False
    with pytest.raises(ValidationError, match="observation rule drifted"):
        IndexShadowExecutionProtocol.model_validate(payload)


def test_shadow_protocol_self_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    payload = _payload()
    gate = payload["manual_promotion_gate"]
    assert isinstance(gate, dict)
    gate["prominent_warning"] = "⚠️ tampered but structurally valid warning"
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="self-hash mismatch"):
        verify_index_shadow_execution_protocol(
            repo_root=tmp_path,
            path=protocol_path,
            require_evidence=False,
        )


def test_board_lot_allocator_reproduces_frozen_shadow_initialization() -> None:
    lots = _optimal_board_lots(
        capital=Decimal("80000"),
        equity_price=Decimal("4.692") * Decimal("1.0005"),
        defensive_price=Decimal("141.134") * Decimal("1.0005"),
        board_lot=100,
        equity_target=Decimal("0.3"),
        defensive_target=Decimal("0.7"),
        commission_rate=Decimal("0.00025"),
        minimum_commission=Decimal("5"),
    )
    assert lots == (50, 4)


def test_board_lot_allocator_fails_when_both_legs_cannot_be_funded() -> None:
    with pytest.raises(ValueError, match="cannot fund one board lot"):
        _optimal_board_lots(
            capital=Decimal("100"),
            equity_price=Decimal("5"),
            defensive_price=Decimal("140"),
            board_lot=100,
            equity_target=Decimal("0.3"),
            defensive_target=Decimal("0.7"),
            commission_rate=Decimal("0.00025"),
            minimum_commission=Decimal("5"),
        )


@pytest.mark.local_data
def test_local_sealed_shadow_initialization_fully_recomputes() -> None:
    report = verify_index_shadow_initialization_report(repo_root=PROJECT_ROOT)
    assert report.quote_sha256 == {
        "equity": "5cb214dbb525c1ff119c5a7b90264efa7de112a04d3f205c746fbc72300d27ab",
        "defensive": "c192c4e7d6a3aae0cd4c5b8e5431e2d00477e2948aba192a7de5dd4e59972a9c",
    }
    assert report.initialization_is_policy_rebalance is False
    assert report.capital_deployment_authorized is False
    assert report.broker_connection_authorized is False
    assert report.ready_for_orders is False
    assert report.ready_for_trading is False
