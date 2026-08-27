from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from app.cli import app
from app.research.index_shadow_execution import (
    SseQuote,
    parse_index_shadow_sse_quote_bytes,
    verify_index_shadow_execution_protocol,
)
from app.research.index_shadow_observation import (
    DEFAULT_PLAN_PATH,
    IndexShadowObservationPlan,
    OfficialSseQuoteClient,
    _build_leg,
    _validate_record_date,
    summarize_index_shadow_observation_readiness,
    verify_index_shadow_observation_chain,
    verify_index_shadow_observation_plan,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = CliRunner()


def _plan_payload() -> dict[str, object]:
    return json.loads((PROJECT_ROOT / DEFAULT_PLAN_PATH).read_text())


def _quote_payload(*, quote_date: str = "20260828", quote_time: str = "150101") -> bytes:
    return json.dumps(
        {
            "code": "510300",
            "date": quote_date,
            "time": quote_time,
            "snap": [
                "300ETF",
                4.7,
                4.69,
                4.72,
                4.68,
                123456,
                580000.0,
                [4.699, 987600],
                [4.701, 876500],
            ],
        }
    ).encode()


def test_committed_shadow_observation_plan_is_self_hashed() -> None:
    plan = verify_index_shadow_observation_plan(repo_root=PROJECT_ROOT)
    assert plan.cadence["minimum_observations_before_execution_review"] == 12
    assert plan.diagnostic_scope["after_close_quote_is_executable"] is False
    assert plan.broker_tariff_input["status"] == "missing_user_supplied_evidence"
    assert plan.authorization_boundary["broker_connection_authorized"] is False
    assert plan.readiness["ready_for_orders"] is False
    assert plan.readiness["ready_for_trading"] is False


def test_shadow_observation_cli_is_constructible_and_closed() -> None:
    result = RUNNER.invoke(
        app,
        ["verify-index-shadow-observation-plan", "--repo-root", str(PROJECT_ROOT)],
    )
    assert result.exit_code == 0, result.output
    assert "after_close_quote_is_executable=false" in result.output
    assert "ready_for_orders=false" in result.output
    assert "ready_for_trading=false" in result.output


def test_shadow_observation_plan_rejects_authorization_or_claim_drift() -> None:
    payload = _plan_payload()
    boundary = payload["authorization_boundary"]
    assert isinstance(boundary, dict)
    boundary["trading_authorized"] = True
    with pytest.raises(ValidationError, match="authorization boundary drifted"):
        IndexShadowObservationPlan.model_validate(payload)

    payload = _plan_payload()
    scope = payload["diagnostic_scope"]
    assert isinstance(scope, dict)
    scope["performance_or_alpha_claim"] = True
    with pytest.raises(ValidationError, match="diagnostic scope drifted"):
        IndexShadowObservationPlan.model_validate(payload)


def test_shadow_observation_plan_self_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    payload = _plan_payload()
    binding = payload["shadow_protocol_binding"]
    assert isinstance(binding, dict)
    binding["path"] = "config/research/tampered-shadow-protocol.json"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="self-hash mismatch"):
        verify_index_shadow_observation_plan(repo_root=tmp_path, path=plan_path)


def test_sse_quote_parser_preserves_l1_depth_and_date_boundary() -> None:
    protocol = verify_index_shadow_execution_protocol(repo_root=PROJECT_ROOT, require_evidence=False)
    payload = _quote_payload()
    quote = parse_index_shadow_sse_quote_bytes(
        protocol=protocol,
        role="equity",
        payload_bytes=payload,
        source_path="synthetic.json",
        source_sha256=hashlib.sha256(payload).hexdigest(),
        expected_date=date(2026, 8, 28),
    )
    assert quote.best_bid_size == 987600
    assert quote.best_ask_size == 876500

    with pytest.raises(ValueError, match="date mismatch"):
        parse_index_shadow_sse_quote_bytes(
            protocol=protocol,
            role="equity",
            payload_bytes=payload,
            source_path="synthetic.json",
            source_sha256=hashlib.sha256(payload).hexdigest(),
            expected_date=date(2026, 8, 27),
        )


def test_sse_quote_parser_rejects_preclose_snapshot() -> None:
    protocol = verify_index_shadow_execution_protocol(repo_root=PROJECT_ROOT, require_evidence=False)
    payload = _quote_payload(quote_time="145959")
    with pytest.raises(ValueError, match="not an after-close snapshot"):
        parse_index_shadow_sse_quote_bytes(
            protocol=protocol,
            role="equity",
            payload_bytes=payload,
            source_path="synthetic.json",
            source_sha256=hashlib.sha256(payload).hexdigest(),
            expected_date=date(2026, 8, 28),
        )


def test_official_sse_client_rejects_unsealed_endpoint() -> None:
    with pytest.raises(ValueError, match="outside the sealed SSE snapshot endpoint"):
        OfficialSseQuoteClient().fetch("https://example.com/quote")


def test_empty_shadow_ledger_is_not_execution_review_eligible() -> None:
    status = summarize_index_shadow_observation_readiness([])
    assert status.observation_count == 0
    assert status.elapsed_calendar_days == 0
    assert status.consecutive_annual_boundary_pair_present is False
    assert status.execution_review_eligible is False
    assert status.performance_or_alpha_proven is False
    assert status.ready_for_orders is False
    assert status.ready_for_trading is False


def test_shadow_record_reason_cannot_label_an_arbitrary_date_as_a_boundary() -> None:
    with pytest.raises(ValueError, match="must be a Friday"):
        _validate_record_date(expected_date=date(2026, 8, 27), record_reason="weekly")
    with pytest.raises(ValueError, match="final-week window"):
        _validate_record_date(expected_date=date(2026, 8, 28), record_reason="year_end")
    with pytest.raises(ValueError, match="first-ten-day window"):
        _validate_record_date(expected_date=date(2026, 8, 28), record_reason="year_start")


def test_shadow_leg_diagnostics_separate_spread_depth_and_commission_floor() -> None:
    quote = SseQuote(
        symbol="510300.SH",
        name="300ETF",
        observed_at_cst=datetime(2026, 8, 28, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        last=Decimal("4.700"),
        best_bid=Decimal("4.699"),
        best_bid_size=2000,
        best_ask=Decimal("4.701"),
        best_ask_size=900,
        volume=100000,
        amount=Decimal("470000"),
        source_path="synthetic.json",
        source_sha256="a" * 64,
    )
    leg = _build_leg(
        role="equity",
        symbol="510300.SH",
        quantity=1000,
        quote=quote,
        slippage_bps=Decimal("5"),
        commission_rate=Decimal("0.00025"),
        minimum_commission=Decimal("5"),
    )
    assert leg.slippage_assumption_covers_half_spread is True
    assert leg.visible_best_ask_depth_ratio == Decimal("0.90000000")
    assert leg.visible_best_ask_covers_shadow_quantity is False
    assert leg.commission_floor_binding is True
    assert leg.estimated_liquidation_commission_cny == Decimal("5.0000")
    assert leg.order_lifecycle.status == "not_submitted"
    assert leg.order_lifecycle.requested_quantity == 0
    assert leg.order_lifecycle.submitted_quantity == 0
    assert leg.order_lifecycle.filled_quantity == 0
    assert leg.order_lifecycle.broker_order_id is None
    assert leg.hypothetical_order_status == "not_submitted"
    assert leg.after_close_quote_is_executable is False
    assert leg.actual_fill_claim is False


def test_shadow_leg_flags_when_half_spread_exceeds_slippage_assumption() -> None:
    quote = SseQuote(
        symbol="511010.SH",
        name="五年国债ETF",
        observed_at_cst=datetime(2026, 8, 28, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        last=Decimal("141.00"),
        best_bid=Decimal("140.80"),
        best_bid_size=1000,
        best_ask=Decimal("141.20"),
        best_ask_size=1000,
        volume=10000,
        amount=Decimal("1410000"),
        source_path="synthetic.json",
        source_sha256="b" * 64,
    )
    leg = _build_leg(
        role="defensive",
        symbol="511010.SH",
        quantity=400,
        quote=quote,
        slippage_bps=Decimal("5"),
        commission_rate=Decimal("0.00025"),
        minimum_commission=Decimal("5"),
    )
    assert leg.quoted_half_spread_bps > Decimal("5")
    assert leg.slippage_assumption_covers_half_spread is False


@pytest.mark.local_data
def test_local_shadow_observation_chain_is_valid_or_empty() -> None:
    reports = verify_index_shadow_observation_chain(repo_root=PROJECT_ROOT)
    assert all(leg.hypothetical_order_status == "not_submitted" for report in reports for leg in report.legs)
    assert not reports or reports[-1].ready_for_trading is False
