"""Tests for the isolated PIT financial-negative-list verdict overlay."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl
from typer.testing import CliRunner

from app import cli
from app.research import layer_two_financial_negative_list_overlay as overlay
from app.research.layer_two_financial_negative_list import REQUIRED_RULE_CODES
from app.research.layer_two_financial_negative_list_data_protocol import (
    DEBT_COMPONENT_FIELDS,
)

SYMBOL = "000001.SZ"
RUNNER = CliRunner()


def _write_partitions(root: Path, rows_by_endpoint: dict[str, list[dict[str, Any]]]) -> None:
    stem = SYMBOL.replace(".", "_", 1)
    for endpoint in ("balancesheet", "income", "fina_indicator", "fina_audit"):
        output = root / "partitions" / endpoint / f"{stem}.parquet"
        output.parent.mkdir(parents=True, exist_ok=True)
        rows = rows_by_endpoint.get(endpoint, [])
        frame = (
            pl.DataFrame(rows)
            if rows
            else pl.DataFrame(
                schema={
                    "availability_status": pl.String,
                    "available_at": pl.String,
                    "end_date": pl.String,
                    "source_row_hash": pl.String,
                }
            )
        )
        frame.write_parquet(output)


def _balance_row(
    *,
    available_at: str,
    source_hash: str,
    money_cap: float = 25.0,
    total_assets: float = 100.0,
    oth_receiv: float = 5.0,
    goodwill: float = 30.0,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "availability_status": "usable",
        "available_at": available_at,
        "end_date": "20231231",
        "source_row_hash": source_hash,
        "report_type": 1,
        "comp_type": 1,
        "end_type": 4,
        "money_cap": money_cap,
        "total_assets": total_assets,
        "oth_receiv": oth_receiv,
        "goodwill": goodwill,
        "total_hldr_eqy_exc_min_int": 100.0,
        "notes_receiv": 1.0,
        "accounts_receiv": 1.0,
        "inventories": 1.0,
    }
    row.update(dict.fromkeys(DEBT_COMPONENT_FIELDS, 0.0))
    row[DEBT_COMPONENT_FIELDS[0]] = money_cap
    return row


def test_same_day_235959_disclosure_is_not_visible_at_173000(tmp_path: Path) -> None:
    _write_partitions(
        tmp_path,
        {
            "balancesheet": [
                _balance_row(
                    available_at="2024-04-30T23:59:59+08:00",
                    source_hash="a" * 64,
                    money_cap=30.0,
                    oth_receiv=6.0,
                    goodwill=31.0,
                )
            ]
        },
    )
    evaluator = overlay.FinancialSymbolEvaluator(symbol=SYMBOL, collection_root=tmp_path)

    before = evaluator.evaluate(datetime.fromisoformat("2024-04-30T17:30:00+08:00"))
    after = evaluator.evaluate(datetime.fromisoformat("2024-05-06T17:30:00+08:00"))

    assert before["large_cash_and_interest_bearing_debt"].state == "unknown"
    assert before["other_receivables_to_assets_over_5pct"].state == "unknown"
    assert before["goodwill_to_net_assets_over_30pct"].state == "unknown"
    assert after["large_cash_and_interest_bearing_debt"].state == "true"
    assert after["other_receivables_to_assets_over_5pct"].state == "true"
    assert after["goodwill_to_net_assets_over_30pct"].state == "true"


def test_same_availability_conflict_stays_unknown(tmp_path: Path) -> None:
    available_at = "2024-04-30T23:59:59+08:00"
    _write_partitions(
        tmp_path,
        {
            "balancesheet": [
                _balance_row(
                    available_at=available_at,
                    source_hash="a" * 64,
                    goodwill=31.0,
                ),
                _balance_row(
                    available_at=available_at,
                    source_hash="b" * 64,
                    goodwill=29.0,
                ),
            ]
        },
    )
    evaluator = overlay.FinancialSymbolEvaluator(symbol=SYMBOL, collection_root=tmp_path)

    result = evaluator.evaluate(datetime.fromisoformat("2024-05-06T17:30:00+08:00"))[
        "goodwill_to_net_assets_over_30pct"
    ]

    assert result.state == "unknown"
    assert result.issue_codes == ("FNLD-005", "FNLD-006")
    assert result.source_hashes == ("a" * 64, "b" * 64)


def test_frozen_thresholds_are_strictly_greater_than(tmp_path: Path) -> None:
    _write_partitions(
        tmp_path,
        {
            "balancesheet": [
                _balance_row(
                    available_at="2024-04-30T23:59:59+08:00",
                    source_hash="a" * 64,
                )
            ],
            "fina_audit": [
                {
                    "availability_status": "usable",
                    "available_at": "2024-04-30T23:59:59+08:00",
                    "end_date": "20231231",
                    "source_row_hash": "c" * 64,
                    "audit_result": "标准无保留意见",
                }
            ],
        },
    )
    evaluator = overlay.FinancialSymbolEvaluator(symbol=SYMBOL, collection_root=tmp_path)

    results = evaluator.evaluate(datetime.fromisoformat("2024-05-06T17:30:00+08:00"))

    assert results["non_standard_audit"].state == "false"
    assert results["large_cash_and_interest_bearing_debt"].state == "false"
    assert results["other_receivables_to_assets_over_5pct"].state == "false"
    assert results["goodwill_to_net_assets_over_30pct"].state == "false"


def test_unknown_rule_cannot_be_treated_as_zero_hit() -> None:
    results = {code: overlay.RuleResult("false", (), ("a" * 64,)) for code in REQUIRED_RULE_CODES}
    unknown_code = "receivables_inventory_growth_vs_revenue_two_periods"
    results[unknown_code] = overlay.RuleResult("unknown", ("missing_two_quarters",), ())

    status, reasons, hits, unknowns, warning_count, multiplier, eligible = overlay._adjudicate(results)

    assert status == "insufficient_evidence"
    assert reasons == ["insufficient_evidence"]
    assert hits == []
    assert unknowns == [unknown_code]
    assert warning_count is None
    assert multiplier is None
    assert eligible is False


def test_safe_repo_path_rejects_parent_traversal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    try:
        overlay._safe_repo_path(repo, Path("../escape"), field_name="output_dir")
    except ValueError as exc:
        assert "must stay inside repo_root" in str(exc)
    else:
        raise AssertionError("parent traversal must fail closed")


def _fake_overlay_result(tmp_path: Path) -> overlay.FinancialNegativeListOverlayResult:
    output = tmp_path / "overlay"
    return overlay.FinancialNegativeListOverlayResult(
        output_dir=output,
        overlay_id="a" * 64,
        row_count=10,
        partition_count=2,
        coverage_start="2022-01-04",
        coverage_end="2022-01-05",
        collection_id="b" * 64,
        manifest_path=output / "manifest.json",
        coverage_review_path=output / "coverage_pit_review.json",
    )


def test_materialize_cli_remains_offline_and_reports_progress(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    def _fake_materialize(**kwargs: Any) -> overlay.FinancialNegativeListOverlayResult:
        kwargs["progress_callback"](1, 5, "2022-01-04")
        return _fake_overlay_result(tmp_path)

    monkeypatch.setattr(
        overlay,
        "materialize_financial_negative_list_verdict_overlay",
        _fake_materialize,
    )

    result = RUNNER.invoke(cli.app, ["materialize-financial-negative-list-verdict-overlay"])

    assert result.exit_code == 0
    assert "does not read a token" in result.stdout
    assert "progress_partitions=1 rows=5 decision_date=2022-01-04" in result.stdout
    assert "ready_for_scoring=false" in result.stdout
    assert "ready_for_trading=false" in result.stdout


def test_verify_cli_reports_sealed_overlay(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(
        overlay,
        "verify_financial_negative_list_verdict_overlay",
        lambda **_: _fake_overlay_result(tmp_path),
    )

    result = RUNNER.invoke(cli.app, ["verify-financial-negative-list-verdict-overlay"])

    assert result.exit_code == 0
    assert f"overlay_id={'a' * 64}" in result.stdout
    assert "verification=passed" in result.stdout
    assert "ready_for_portfolio_construction=false" in result.stdout
