"""Offline PIT materializer for layer-two financial negative-list verdicts.

The output is an isolated research overlay.  It is bound to the frozen
candidate-eligibility pack and the verified financial collection, preserves
unknown states, and cannot authorize scoring, portfolio construction, or
trading.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import polars as pl
import pyarrow.parquet as pq

from app.providers.tushare_financial_negative_list_collection import (
    verify_financial_negative_list_collection,
)
from app.research.layer_two_financial_negative_list import (
    NON_STANDARD_AUDIT_RULE,
    REQUIRED_RULE_CODES,
    WARNING_RULE_CODES,
)
from app.research.layer_two_financial_negative_list_data_protocol import (
    AUDIT_CLEAN_VALUE,
    BOUND_CANDIDATE_PACK_ID,
    BOUND_CANDIDATE_PACK_PARQUET_SHA256,
    BOUND_CANDIDATE_PACK_PATH,
    DEBT_COMPONENT_FIELDS,
    DECISION_WINDOW_END,
    DECISION_WINDOW_START,
    INCLUDED_REPORT_TYPES,
    MAX_REPORT_PERIOD_AGE_AUDIT_DAYS,
    MAX_REPORT_PERIOD_AGE_STATEMENT_DAYS,
    PROTOCOL_FILE_PATH,
    THRESHOLD_CASH_DEBT_RATIO,
    THRESHOLD_GOODWILL_RATIO,
    THRESHOLD_OTHER_RECEIVABLES_RATIO,
    THRESHOLD_RECEIVABLES_REVENUE_GAP,
    verify_protocol_file,
)

OVERLAY_SCHEMA_VERSION: Literal["1"] = "1"
OVERLAY_VERSION: Literal["layer-two-financial-negative-list-verdict-overlay-v1"] = (
    "layer-two-financial-negative-list-verdict-overlay-v1"
)
DEFAULT_FINANCIAL_COLLECTION_DIR = Path("data/raw/a-share-financial-negative-list-20200101-20241231-v3")
DEFAULT_OUTPUT_DIR = Path("data/all-a-share-historical-v1/research/financial-negative-list-verdict-overlay-v1")
VERDICTS_DIR_NAME = "verdicts"
MANIFEST_NAME = "manifest.json"
COVERAGE_REVIEW_NAME = "coverage_pit_review.json"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_STANDARD_QUARTERS = {(3, 31): 1, (6, 30): 2, (9, 30): 3, (12, 31): 4}
_RULE_COLUMN_PREFIX = {
    "non_standard_audit": "audit",
    "large_cash_and_interest_bearing_debt": "cash_debt",
    "receivables_inventory_growth_vs_revenue_two_periods": "receivables_revenue",
    "other_receivables_to_assets_over_5pct": "other_receivables",
    "goodwill_to_net_assets_over_30pct": "goodwill",
}

_OUTPUT_SCHEMA: dict[str, Any] = {
    "symbol": pl.String,
    "as_of": pl.String,
    "decision_at": pl.String,
    "financial_collection_id": pl.String,
    "candidate_source_input_hash": pl.String,
    "audit_state": pl.String,
    "audit_issue_codes": pl.String,
    "audit_evidence_id": pl.String,
    "cash_debt_state": pl.String,
    "cash_debt_issue_codes": pl.String,
    "cash_debt_evidence_id": pl.String,
    "receivables_revenue_state": pl.String,
    "receivables_revenue_issue_codes": pl.String,
    "receivables_revenue_evidence_id": pl.String,
    "other_receivables_state": pl.String,
    "other_receivables_issue_codes": pl.String,
    "other_receivables_evidence_id": pl.String,
    "goodwill_state": pl.String,
    "goodwill_issue_codes": pl.String,
    "goodwill_evidence_id": pl.String,
    "decision_status": pl.String,
    "reason_codes": pl.String,
    "known_hit_codes": pl.String,
    "unknown_codes": pl.String,
    "known_warning_hit_count": pl.Int64,
    "target_multiplier": pl.Float64,
    "eligible_for_new_entry": pl.Boolean,
    "source_input_hash": pl.String,
    "ready_for_scoring": pl.Boolean,
    "ready_for_portfolio_construction": pl.Boolean,
    "ready_for_trading": pl.Boolean,
}


@dataclass(frozen=True)
class RuleResult:
    state: Literal["true", "false", "unknown"]
    issue_codes: tuple[str, ...]
    source_hashes: tuple[str, ...]


@dataclass
class _ActivePeriod:
    available_at: datetime
    rows: list[dict[str, Any]]


class _EndpointTimeline:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        events: list[tuple[datetime, date, dict[str, Any]]] = []
        for row in rows:
            if row.get("availability_status") != "usable" or row.get("available_at") is None:
                continue
            available_at = datetime.fromisoformat(str(row["available_at"]))
            end_date = _ymd_date(row.get("end_date"), field_name="end_date")
            events.append((available_at, end_date, row))
        self.events = sorted(events, key=lambda item: (item[0], item[1], str(item[2].get("source_row_hash"))))
        self.cursor = 0
        self.active: dict[date, _ActivePeriod] = {}

    def advance(self, decision_at: datetime) -> None:
        while self.cursor < len(self.events) and self.events[self.cursor][0] <= decision_at:
            available_at, period, row = self.events[self.cursor]
            current = self.active.get(period)
            if current is None or available_at > current.available_at:
                self.active[period] = _ActivePeriod(available_at=available_at, rows=[row])
            elif available_at == current.available_at:
                current.rows.append(row)
            self.cursor += 1


@dataclass(frozen=True)
class _SelectedRow:
    row: dict[str, Any] | None
    issue_codes: tuple[str, ...]
    source_hashes: tuple[str, ...]


class FinancialSymbolEvaluator:
    """Incremental as-of evaluator for one symbol."""

    def __init__(self, *, symbol: str, collection_root: Path) -> None:
        self.symbol = symbol
        stem = symbol.replace(".", "_", 1)
        self.timelines: dict[str, _EndpointTimeline] = {}
        for endpoint in ("balancesheet", "income", "fina_indicator", "fina_audit"):
            path = collection_root / "partitions" / endpoint / f"{stem}.parquet"
            frame = pl.read_parquet(path)
            self.timelines[endpoint] = _EndpointTimeline(frame.to_dicts())

    def evaluate(self, decision_at: datetime) -> dict[str, RuleResult]:
        for timeline in self.timelines.values():
            timeline.advance(decision_at)
        return {
            "non_standard_audit": self._rule_a(decision_at),
            "large_cash_and_interest_bearing_debt": self._rule_b(decision_at),
            "receivables_inventory_growth_vs_revenue_two_periods": self._rule_c(decision_at),
            "other_receivables_to_assets_over_5pct": self._rule_d(decision_at),
            "goodwill_to_net_assets_over_30pct": self._rule_e(decision_at),
        }

    def _latest_period(self, endpoint: str, *, annual_only: bool = False) -> date | None:
        periods = self.timelines[endpoint].active
        candidates = [period for period in periods if not annual_only or (period.month, period.day) == (12, 31)]
        return max(candidates) if candidates else None

    def _select(
        self,
        endpoint: str,
        period: date,
        *,
        fields: tuple[str, ...],
        general_industrial: bool,
    ) -> _SelectedRow:
        active = self.timelines[endpoint].active.get(period)
        if active is None:
            return _SelectedRow(None, ("missing_period",), ())
        hashes = tuple(sorted(str(row["source_row_hash"]) for row in active.rows))
        comparison_fields = fields
        if endpoint in {"balancesheet", "income"}:
            comparison_fields = ("report_type", "comp_type", "end_type", *fields)
        canonical_values = {
            json.dumps(
                {field: row.get(field) for field in comparison_fields},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for row in active.rows
        }
        if len(canonical_values) != 1:
            return _SelectedRow(None, ("FNLD-005", "FNLD-006"), hashes)
        row = active.rows[0]
        if endpoint in {"balancesheet", "income"}:
            report_type = row.get("report_type")
            if report_type not in INCLUDED_REPORT_TYPES:
                return _SelectedRow(None, ("FNLD-003",), hashes)
            if general_industrial and row.get("comp_type") != 1:
                return _SelectedRow(None, ("FNLD-004",), hashes)
        return _SelectedRow(row, (), hashes)

    def _rule_a(self, decision_at: datetime) -> RuleResult:
        period = self._latest_period("fina_audit", annual_only=True)
        if period is None:
            return _unknown("missing_annual_audit")
        if not _fresh(period, decision_at.date(), MAX_REPORT_PERIOD_AGE_AUDIT_DAYS):
            return _unknown("FNLD-007")
        selected = self._select("fina_audit", period, fields=("audit_result",), general_industrial=False)
        if selected.row is None:
            return RuleResult("unknown", selected.issue_codes, selected.source_hashes)
        value = selected.row.get("audit_result")
        if not isinstance(value, str) or not value.strip():
            return RuleResult("unknown", ("missing_audit_result",), selected.source_hashes)
        return RuleResult("false" if value == AUDIT_CLEAN_VALUE else "true", (), selected.source_hashes)

    def _latest_statement_period(self, decision_at: datetime) -> tuple[date | None, tuple[str, ...]]:
        period = self._latest_period("balancesheet")
        if period is None:
            return None, ("missing_statement",)
        if not _fresh(period, decision_at.date(), MAX_REPORT_PERIOD_AGE_STATEMENT_DAYS):
            return period, ("FNLD-007",)
        return period, ()

    def _rule_b(self, decision_at: datetime) -> RuleResult:
        period, period_issues = self._latest_statement_period(decision_at)
        if period is None or period_issues:
            return RuleResult("unknown", period_issues, ())
        balance = self._select(
            "balancesheet",
            period,
            fields=("money_cap", "total_assets", *DEBT_COMPONENT_FIELDS),
            general_industrial=True,
        )
        if balance.row is None:
            return RuleResult("unknown", balance.issue_codes, balance.source_hashes)
        assets = _number(balance.row.get("total_assets"))
        cash = _number(balance.row.get("money_cap"))
        if assets is None or assets <= 0:
            return RuleResult("unknown", ("FNLD-011",), balance.source_hashes)
        if cash is None:
            return RuleResult("unknown", ("missing_money_cap",), balance.source_hashes)
        if cash < 0:
            return RuleResult("unknown", ("FNLD-010",), balance.source_hashes)
        indicator = self._select(
            "fina_indicator",
            period,
            fields=("interestdebt",),
            general_industrial=False,
        )
        debt = None if indicator.row is None else _number(indicator.row.get("interestdebt"))
        hashes = tuple(sorted({*balance.source_hashes, *indicator.source_hashes}))
        if debt is None:
            components = [_number(balance.row.get(field)) for field in DEBT_COMPONENT_FIELDS]
            if any(value is None for value in components):
                return RuleResult("unknown", ("FNLD-008",), hashes)
            if any(value is not None and value < 0 for value in components):
                return RuleResult("unknown", ("FNLD-010",), hashes)
            debt = sum(value for value in components if value is not None)
        if debt < 0:
            return RuleResult("unknown", ("FNLD-010",), hashes)
        hit = cash / assets > THRESHOLD_CASH_DEBT_RATIO and debt / assets > THRESHOLD_CASH_DEBT_RATIO
        return RuleResult("true" if hit else "false", (), hashes)

    def _rule_c(self, decision_at: datetime) -> RuleResult:
        periods = sorted(
            period
            for period in self.timelines["balancesheet"].active
            if (period.month, period.day) in _STANDARD_QUARTERS
        )
        if len(periods) < 2:
            return _unknown("missing_two_quarters")
        current_periods = periods[-2:]
        if not _consecutive_quarters(current_periods[0], current_periods[1]):
            return _unknown("nonconsecutive_quarters")
        if not _fresh(current_periods[-1], decision_at.date(), MAX_REPORT_PERIOD_AGE_STATEMENT_DAYS):
            return _unknown("FNLD-007")
        gaps: list[float] = []
        all_hashes: set[str] = set()
        for current in current_periods:
            prior = date(current.year - 1, current.month, current.day)
            selected: list[_SelectedRow] = []
            for endpoint, fields in (
                ("balancesheet", ("notes_receiv", "accounts_receiv", "inventories")),
                ("income", ("revenue",)),
            ):
                selected.append(self._select(endpoint, current, fields=fields, general_industrial=True))
                selected.append(self._select(endpoint, prior, fields=fields, general_industrial=True))
            for item in selected:
                all_hashes.update(item.source_hashes)
                if item.row is None:
                    issues = tuple(sorted({code for value in selected for code in value.issue_codes}))
                    return RuleResult("unknown", issues or ("missing_comparison_period",), tuple(sorted(all_hashes)))
            balance_current, balance_prior, income_current, income_prior = (item.row for item in selected)
            assert balance_current is not None and balance_prior is not None
            assert income_current is not None and income_prior is not None
            exposure_current_values = [
                _number(balance_current.get(field)) for field in ("notes_receiv", "accounts_receiv", "inventories")
            ]
            exposure_prior_values = [
                _number(balance_prior.get(field)) for field in ("notes_receiv", "accounts_receiv", "inventories")
            ]
            revenue_current = _number(income_current.get("revenue"))
            revenue_prior = _number(income_prior.get("revenue"))
            numbers = [*exposure_current_values, *exposure_prior_values, revenue_current, revenue_prior]
            if any(value is None for value in numbers):
                return RuleResult("unknown", ("missing_rule_c_input",), tuple(sorted(all_hashes)))
            if any(value is not None and value < 0 for value in numbers):
                return RuleResult("unknown", ("FNLD-010",), tuple(sorted(all_hashes)))
            exposure_current = sum(value for value in exposure_current_values if value is not None)
            exposure_prior = sum(value for value in exposure_prior_values if value is not None)
            assert revenue_current is not None and revenue_prior is not None
            if exposure_prior <= 0 or revenue_prior <= 0:
                return RuleResult("unknown", ("FNLD-011",), tuple(sorted(all_hashes)))
            gaps.append((exposure_current / exposure_prior - 1.0) - (revenue_current / revenue_prior - 1.0))
        return RuleResult(
            "true" if all(gap > THRESHOLD_RECEIVABLES_REVENUE_GAP for gap in gaps) else "false",
            (),
            tuple(sorted(all_hashes)),
        )

    def _rule_d(self, decision_at: datetime) -> RuleResult:
        period, period_issues = self._latest_statement_period(decision_at)
        if period is None or period_issues:
            return RuleResult("unknown", period_issues, ())
        balance = self._select(
            "balancesheet",
            period,
            fields=("oth_receiv", "total_assets"),
            general_industrial=True,
        )
        if balance.row is None:
            return RuleResult("unknown", balance.issue_codes, balance.source_hashes)
        numerator = _number(balance.row.get("oth_receiv"))
        denominator = _number(balance.row.get("total_assets"))
        return _ratio_rule(numerator, denominator, THRESHOLD_OTHER_RECEIVABLES_RATIO, balance.source_hashes)

    def _rule_e(self, decision_at: datetime) -> RuleResult:
        period, period_issues = self._latest_statement_period(decision_at)
        if period is None or period_issues:
            return RuleResult("unknown", period_issues, ())
        balance = self._select(
            "balancesheet",
            period,
            fields=("goodwill", "total_hldr_eqy_exc_min_int"),
            general_industrial=True,
        )
        if balance.row is None:
            return RuleResult("unknown", balance.issue_codes, balance.source_hashes)
        numerator = _number(balance.row.get("goodwill"))
        denominator = _number(balance.row.get("total_hldr_eqy_exc_min_int"))
        return _ratio_rule(numerator, denominator, THRESHOLD_GOODWILL_RATIO, balance.source_hashes)


@dataclass(frozen=True)
class FinancialNegativeListOverlayResult:
    output_dir: Path
    overlay_id: str
    row_count: int
    partition_count: int
    coverage_start: str
    coverage_end: str
    collection_id: str
    manifest_path: Path
    coverage_review_path: Path


def _ymd_date(value: object, *, field_name: str) -> date:
    text = str(value or "")
    if not re.fullmatch(r"\d{8}", text):
        raise ValueError(f"invalid {field_name}")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _fresh(period: date, decision: date, max_age: int) -> bool:
    age = (decision - period).days
    return 0 <= age <= max_age


def _consecutive_quarters(left: date, right: date) -> bool:
    left_q = _STANDARD_QUARTERS.get((left.month, left.day))
    right_q = _STANDARD_QUARTERS.get((right.month, right.day))
    if left_q is None or right_q is None:
        return False
    return right.year * 4 + right_q == left.year * 4 + left_q + 1


def _unknown(code: str) -> RuleResult:
    return RuleResult("unknown", (code,), ())


def _ratio_rule(
    numerator: float | None,
    denominator: float | None,
    threshold: float,
    source_hashes: tuple[str, ...],
) -> RuleResult:
    if numerator is None:
        return RuleResult("unknown", ("missing_numerator",), source_hashes)
    if numerator < 0:
        return RuleResult("unknown", ("FNLD-010",), source_hashes)
    if denominator is None or denominator <= 0:
        return RuleResult("unknown", ("FNLD-011",), source_hashes)
    return RuleResult("true" if numerator / denominator > threshold else "false", (), source_hashes)


def _canonical_sha(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_text(values: list[str] | tuple[str, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _rule_evidence_id(*, symbol: str, as_of: str, rule_code: str, result: RuleResult, collection_id: str) -> str:
    return _canonical_sha(
        {
            "symbol": symbol,
            "as_of": as_of,
            "rule_code": rule_code,
            "state": result.state,
            "issue_codes": list(result.issue_codes),
            "source_hashes": list(result.source_hashes),
            "financial_collection_id": collection_id,
        }
    )


def _adjudicate(
    results: dict[str, RuleResult],
) -> tuple[str, list[str], list[str], list[str], int | None, float | None, bool]:
    known_hits = sorted(code for code, result in results.items() if result.state == "true")
    unknowns = sorted(code for code, result in results.items() if result.state == "unknown")
    warning_hits = sum(1 for code in WARNING_RULE_CODES if results[code].state == "true")
    if results[NON_STANDARD_AUDIT_RULE].state == "true":
        return "hard_excluded", ["non_standard_audit_hard_exclude"], known_hits, unknowns, warning_hits, 0.0, False
    if warning_hits >= 2:
        return "hard_excluded", ["warning_hits_ge_2_exclude"], known_hits, unknowns, warning_hits, 0.0, False
    if unknowns:
        return "insufficient_evidence", ["insufficient_evidence"], known_hits, unknowns, None, None, False
    if warning_hits == 1:
        return "halved", ["warning_hits_eq_1_halve"], known_hits, unknowns, warning_hits, 0.5, True
    return "clean", ["clean_no_hits"], known_hits, unknowns, 0, 1.0, True


def _output_row(
    *,
    symbol: str,
    as_of: str,
    decision_at_text: str,
    candidate_source_input_hash: str,
    collection_id: str,
    results: dict[str, RuleResult],
) -> dict[str, Any]:
    decision_status, reasons, hits, unknowns, warning_count, multiplier, eligible = _adjudicate(results)
    row: dict[str, Any] = {
        "symbol": symbol,
        "as_of": as_of,
        "decision_at": decision_at_text,
        "financial_collection_id": collection_id,
        "candidate_source_input_hash": candidate_source_input_hash,
        "decision_status": decision_status,
        "reason_codes": _json_text(reasons),
        "known_hit_codes": _json_text(hits),
        "unknown_codes": _json_text(unknowns),
        "known_warning_hit_count": warning_count,
        "target_multiplier": multiplier,
        "eligible_for_new_entry": eligible,
        "ready_for_scoring": False,
        "ready_for_portfolio_construction": False,
        "ready_for_trading": False,
    }
    evidence_ids: list[str] = []
    for rule_code in REQUIRED_RULE_CODES:
        result = results[rule_code]
        prefix = _RULE_COLUMN_PREFIX[rule_code]
        evidence_id = _rule_evidence_id(
            symbol=symbol,
            as_of=as_of,
            rule_code=rule_code,
            result=result,
            collection_id=collection_id,
        )
        evidence_ids.append(evidence_id)
        row[f"{prefix}_state"] = result.state
        row[f"{prefix}_issue_codes"] = _json_text(result.issue_codes)
        row[f"{prefix}_evidence_id"] = evidence_id
    row["source_input_hash"] = _canonical_sha(
        {
            "candidate_source_input_hash": candidate_source_input_hash,
            "financial_collection_id": collection_id,
            "evidence_ids": evidence_ids,
        }
    )
    return row


def _frame_from_rows(rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=_OUTPUT_SCHEMA)
    return pl.DataFrame(rows, schema=_OUTPUT_SCHEMA).select(list(_OUTPUT_SCHEMA))


def _dataset_hash(hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative, file_hash in sorted(hashes.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _candidate_rows(candidate_path: Path) -> Iterator[tuple[str, str, str, str]]:
    parquet_file = pq.ParquetFile(candidate_path)
    for batch in parquet_file.iter_batches(
        batch_size=65536,
        columns=["symbol", "as_of", "decision_at", "source_input_hash"],
    ):
        columns = batch.to_pydict()
        for symbol, as_of, decision_at, source_input_hash in zip(
            columns["symbol"],
            columns["as_of"],
            columns["decision_at"],
            columns["source_input_hash"],
            strict=True,
        ):
            yield str(symbol), str(as_of), str(decision_at), str(source_input_hash)


def _json_string_list(value: object, *, field_name: str) -> list[str]:
    if not isinstance(value, str):
        raise ValueError(f"financial negative-list overlay {field_name} must be JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"financial negative-list overlay {field_name} invalid JSON") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise ValueError(f"financial negative-list overlay {field_name} must be a string list")
    return parsed


def _safe_repo_path(repo_root: Path, path: Path, *, field_name: str) -> Path:
    root = repo_root.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field_name} must stay inside repo_root") from exc
    if resolved.is_symlink():
        raise ValueError(f"{field_name} must not be a symlink")
    return resolved


def materialize_financial_negative_list_verdict_overlay(
    *,
    repo_root: Path,
    collection_dir: Path = DEFAULT_FINANCIAL_COLLECTION_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    progress_callback: Any | None = None,
) -> FinancialNegativeListOverlayResult:
    root = Path(repo_root).resolve(strict=True)
    collection_root = _safe_repo_path(root, collection_dir, field_name="collection_dir")
    output = _safe_repo_path(root, output_dir, field_name="output_dir")
    if output.exists() or output.is_symlink():
        raise ValueError("financial negative-list overlay output_dir already exists")
    protocol = verify_protocol_file(root)
    verify_financial_negative_list_collection(repo_root=root, staging_dir=collection_root)
    collection_manifest_path = collection_root / "collection_manifest.json"
    collection_manifest = json.loads(collection_manifest_path.read_text(encoding="utf-8"))
    collection_id = str(collection_manifest["collection_id"])
    candidate_path = root / BOUND_CANDIDATE_PACK_PATH / "eligibility_verdicts.parquet"
    if _sha256_file(candidate_path) != BOUND_CANDIDATE_PACK_PARQUET_SHA256:
        raise ValueError("candidate eligibility parquet hash drift")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        verdict_dir = temporary / VERDICTS_DIR_NAME
        verdict_dir.mkdir()
        partition_hashes: dict[str, str] = {}
        state_counts: dict[str, int] = {}
        year_counts: dict[str, dict[str, int]] = {}
        rule_state_counts: dict[str, dict[str, int]] = {
            rule: {"true": 0, "false": 0, "unknown": 0} for rule in REQUIRED_RULE_CODES
        }
        row_count = 0
        partition_count = 0
        first_as_of: str | None = None
        last_as_of: str | None = None
        previous_key: tuple[str, str] | None = None
        current_as_of: str | None = None
        current_rows: list[dict[str, Any]] = []
        evaluators: dict[str, FinancialSymbolEvaluator] = {}

        def flush_decision_date() -> None:
            nonlocal current_as_of, current_rows, partition_count
            if current_as_of is None:
                return
            frame = _frame_from_rows(current_rows)
            relative = f"{VERDICTS_DIR_NAME}/{current_as_of}.parquet"
            path = temporary / relative
            frame.write_parquet(path)
            partition_hashes[relative] = _sha256_file(path)
            partition_count += 1
            if progress_callback is not None:
                progress_callback(partition_count, row_count, current_as_of)
            current_rows = []

        for symbol, as_of, decision_text, candidate_hash in _candidate_rows(candidate_path):
            key = (str(as_of), str(symbol))
            if previous_key is not None and key <= previous_key:
                raise ValueError("candidate eligibility pack is not strictly sorted and unique")
            previous_key = key
            if current_as_of != as_of:
                flush_decision_date()
                current_as_of = str(as_of)
            symbol_text = str(symbol)
            evaluator = evaluators.get(symbol_text)
            if evaluator is None:
                evaluator = FinancialSymbolEvaluator(
                    symbol=symbol_text,
                    collection_root=collection_root,
                )
                evaluators[symbol_text] = evaluator
            decision_at = datetime.fromisoformat(str(decision_text))
            decision_date = date.fromisoformat(str(as_of))
            if decision_at.date() != decision_date:
                raise ValueError("candidate decision_at/as_of mismatch")
            if not (DECISION_WINDOW_START <= decision_date <= DECISION_WINDOW_END):
                raise ValueError("candidate row outside frozen decision window")
            results = evaluator.evaluate(decision_at)
            output_row = _output_row(
                symbol=str(symbol),
                as_of=str(as_of),
                decision_at_text=str(decision_text),
                candidate_source_input_hash=str(candidate_hash),
                collection_id=collection_id,
                results=results,
            )
            current_rows.append(output_row)
            row_count += 1
            first_as_of = str(as_of) if first_as_of is None else min(first_as_of, str(as_of))
            last_as_of = str(as_of) if last_as_of is None else max(last_as_of, str(as_of))
            status = str(output_row["decision_status"])
            state_counts[status] = state_counts.get(status, 0) + 1
            year = str(as_of)[:4]
            per_year = year_counts.setdefault(year, {})
            per_year[status] = per_year.get(status, 0) + 1
            for rule in REQUIRED_RULE_CODES:
                state = results[rule].state
                rule_state_counts[rule][state] += 1
        flush_decision_date()
        if first_as_of is None or last_as_of is None:
            raise ValueError("candidate eligibility pack produced no rows")
        coverage_review = {
            "schema_version": OVERLAY_SCHEMA_VERSION,
            "review_version": "financial-negative-list-coverage-pit-review-v1",
            "coverage_start": first_as_of,
            "coverage_end": last_as_of,
            "row_count": row_count,
            "partition_count": partition_count,
            "partitioning": "decision_date",
            "decision_status_counts": dict(sorted(state_counts.items())),
            "decision_status_counts_by_year": {
                year: dict(sorted(counts.items())) for year, counts in sorted(year_counts.items())
            },
            "rule_state_counts": rule_state_counts,
            "missing_stays_unknown": True,
            "same_day_235959_disclosure_unusable_at_173000_decision": True,
            "future_payload_values_included": False,
            "ready_for_scoring": False,
            "ready_for_backtest": False,
            "ready_for_portfolio_construction": False,
            "ready_for_trading": False,
        }
        coverage_review_path = temporary / COVERAGE_REVIEW_NAME
        coverage_review_path.write_text(
            json.dumps(coverage_review, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        overlay_payload = {
            "schema_version": OVERLAY_SCHEMA_VERSION,
            "overlay_version": OVERLAY_VERSION,
            "protocol_id": str(protocol.protocol_id),
            "protocol_path": PROTOCOL_FILE_PATH,
            "protocol_file_sha256": _sha256_file(root / PROTOCOL_FILE_PATH),
            "candidate_pack_path": BOUND_CANDIDATE_PACK_PATH,
            "candidate_pack_id": BOUND_CANDIDATE_PACK_ID,
            "candidate_pack_parquet_sha256": BOUND_CANDIDATE_PACK_PARQUET_SHA256,
            "financial_collection_path": collection_root.relative_to(root).as_posix(),
            "financial_collection_id": collection_id,
            "financial_collection_manifest_sha256": _sha256_file(collection_manifest_path),
            "response_boundary_finalization": collection_manifest["response_boundary_finalization"],
            "coverage_start": first_as_of,
            "coverage_end": last_as_of,
            "row_count": row_count,
            "partition_count": partition_count,
            "partitioning": "decision_date",
            "partition_hashes": partition_hashes,
            "dataset_hash": _dataset_hash(partition_hashes),
            "coverage_review_sha256": _sha256_file(coverage_review_path),
            "missing_stays_unknown": True,
            "not_integrated_into_scoring_or_universe": True,
            "research_only": True,
            "ready_for_scoring": False,
            "ready_for_backtest": False,
            "ready_for_portfolio_construction": False,
            "ready_for_trading": False,
        }
        manifest = {**overlay_payload, "overlay_id": _canonical_sha(overlay_payload)}
        manifest_path = temporary / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return verify_financial_negative_list_verdict_overlay(repo_root=root, overlay_dir=output)


def verify_financial_negative_list_verdict_overlay(
    *,
    repo_root: Path,
    overlay_dir: Path = DEFAULT_OUTPUT_DIR,
) -> FinancialNegativeListOverlayResult:
    root = Path(repo_root).resolve(strict=True)
    overlay = _safe_repo_path(root, overlay_dir, field_name="overlay_dir")
    manifest_path = overlay / MANIFEST_NAME
    review_path = overlay / COVERAGE_REVIEW_NAME
    if not manifest_path.is_file() or not review_path.is_file():
        raise ValueError("financial negative-list overlay manifest/review missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    overlay_id = manifest.get("overlay_id")
    if not isinstance(overlay_id, str) or _HEX64_RE.fullmatch(overlay_id) is None:
        raise ValueError("financial negative-list overlay_id invalid")
    if _canonical_sha({k: v for k, v in manifest.items() if k != "overlay_id"}) != overlay_id:
        raise ValueError("financial negative-list overlay_id self-seal mismatch")
    if manifest.get("overlay_version") != OVERLAY_VERSION:
        raise ValueError("financial negative-list overlay_version mismatch")
    if manifest.get("schema_version") != OVERLAY_SCHEMA_VERSION:
        raise ValueError("financial negative-list overlay schema_version mismatch")
    if manifest.get("partitioning") != "decision_date":
        raise ValueError("financial negative-list overlay partitioning mismatch")
    protocol = verify_protocol_file(root)
    if manifest.get("protocol_id") != str(protocol.protocol_id):
        raise ValueError("financial negative-list overlay protocol_id drift")
    if manifest.get("protocol_file_sha256") != _sha256_file(root / PROTOCOL_FILE_PATH):
        raise ValueError("financial negative-list overlay protocol hash drift")
    collection_root = _safe_repo_path(
        root,
        Path(str(manifest.get("financial_collection_path"))),
        field_name="financial_collection_path",
    )
    verify_financial_negative_list_collection(repo_root=root, staging_dir=collection_root)
    collection_manifest_path = collection_root / "collection_manifest.json"
    collection_manifest = json.loads(collection_manifest_path.read_text(encoding="utf-8"))
    if manifest.get("financial_collection_id") != collection_manifest.get("collection_id"):
        raise ValueError("financial negative-list overlay collection_id drift")
    if manifest.get("financial_collection_manifest_sha256") != _sha256_file(collection_manifest_path):
        raise ValueError("financial negative-list overlay collection manifest hash drift")
    if manifest.get("response_boundary_finalization") != collection_manifest.get("response_boundary_finalization"):
        raise ValueError("financial negative-list overlay finalization binding drift")
    if manifest.get("candidate_pack_id") != BOUND_CANDIDATE_PACK_ID:
        raise ValueError("financial negative-list overlay candidate pack id drift")
    if manifest.get("candidate_pack_path") != BOUND_CANDIDATE_PACK_PATH:
        raise ValueError("financial negative-list overlay candidate pack path drift")
    candidate_path = root / BOUND_CANDIDATE_PACK_PATH / "eligibility_verdicts.parquet"
    if manifest.get("candidate_pack_parquet_sha256") != _sha256_file(candidate_path):
        raise ValueError("financial negative-list overlay candidate parquet hash drift")
    partition_hashes = manifest.get("partition_hashes")
    if not isinstance(partition_hashes, dict) or not partition_hashes:
        raise ValueError("financial negative-list overlay partition hashes missing")
    actual_paths = sorted((overlay / VERDICTS_DIR_NAME).glob("*.parquet"))
    actual_relatives = {path.relative_to(overlay).as_posix() for path in actual_paths}
    if actual_relatives != set(partition_hashes):
        raise ValueError("financial negative-list overlay partition set drift")
    if any(path.is_symlink() for path in overlay.rglob("*")):
        raise ValueError("financial negative-list overlay must not contain symlinks")
    actual_files = {path.relative_to(overlay).as_posix() for path in overlay.rglob("*") if path.is_file()}
    expected_files = {MANIFEST_NAME, COVERAGE_REVIEW_NAME, *partition_hashes}
    if actual_files != expected_files:
        raise ValueError("financial negative-list overlay contains undeclared files")
    row_count = 0
    prior_key: tuple[str, str] | None = None
    first_as_of: str | None = None
    last_as_of: str | None = None
    state_counts: dict[str, int] = {}
    year_counts: dict[str, dict[str, int]] = {}
    rule_state_counts: dict[str, dict[str, int]] = {
        rule: {"true": 0, "false": 0, "unknown": 0} for rule in REQUIRED_RULE_CODES
    }
    candidate_rows = _candidate_rows(candidate_path)
    for path in actual_paths:
        relative = path.relative_to(overlay).as_posix()
        if partition_hashes[relative] != _sha256_file(path):
            raise ValueError(f"financial negative-list overlay partition hash drift: {relative}")
        frame = pl.read_parquet(path)
        if frame.schema != _OUTPUT_SCHEMA:
            raise ValueError(f"financial negative-list overlay partition schema drift: {relative}")
        partition_as_of = path.stem
        if frame.is_empty() or frame["as_of"].n_unique() != 1 or frame["as_of"][0] != partition_as_of:
            raise ValueError(f"financial negative-list overlay decision-date partition drift: {relative}")
        for row in frame.iter_rows(named=True):
            row_key = (str(row["as_of"]), str(row["symbol"]))
            if prior_key is not None and row_key <= prior_key:
                raise ValueError("financial negative-list overlay keys are not strictly sorted/unique")
            prior_key = row_key
            if row["financial_collection_id"] != manifest["financial_collection_id"]:
                raise ValueError("financial negative-list overlay row collection binding drift")
            if row["ready_for_scoring"] or row["ready_for_portfolio_construction"] or row["ready_for_trading"]:
                raise ValueError("financial negative-list overlay row readiness must remain false")
            try:
                candidate_symbol, candidate_as_of, candidate_decision_at, candidate_hash = next(candidate_rows)
            except StopIteration as exc:
                raise ValueError("financial negative-list overlay contains extra candidate rows") from exc
            if (
                str(row["symbol"]),
                str(row["as_of"]),
                str(row["decision_at"]),
                str(row["candidate_source_input_hash"]),
            ) != (candidate_symbol, candidate_as_of, candidate_decision_at, candidate_hash):
                raise ValueError("financial negative-list overlay candidate row binding drift")
            row_results: dict[str, RuleResult] = {}
            evidence_ids: list[str] = []
            for rule_code in REQUIRED_RULE_CODES:
                prefix = _RULE_COLUMN_PREFIX[rule_code]
                state = str(row[f"{prefix}_state"])
                if state not in {"true", "false", "unknown"}:
                    raise ValueError("financial negative-list overlay rule state invalid")
                issues = _json_string_list(
                    row[f"{prefix}_issue_codes"],
                    field_name=f"{prefix}_issue_codes",
                )
                evidence_id = str(row[f"{prefix}_evidence_id"])
                if _HEX64_RE.fullmatch(evidence_id) is None:
                    raise ValueError("financial negative-list overlay evidence_id invalid")
                evidence_ids.append(evidence_id)
                row_results[rule_code] = RuleResult(
                    state=state,  # type: ignore[arg-type]
                    issue_codes=tuple(issues),
                    source_hashes=(),
                )
                rule_state_counts[rule_code][state] += 1
            status, reasons, hits, unknowns, warning_count, multiplier, eligible = _adjudicate(row_results)
            if (
                str(row["decision_status"]) != status
                or _json_string_list(row["reason_codes"], field_name="reason_codes") != reasons
                or _json_string_list(row["known_hit_codes"], field_name="known_hit_codes") != hits
                or _json_string_list(row["unknown_codes"], field_name="unknown_codes") != unknowns
                or row["known_warning_hit_count"] != warning_count
                or row["target_multiplier"] != multiplier
                or row["eligible_for_new_entry"] != eligible
            ):
                raise ValueError("financial negative-list overlay adjudication drift")
            expected_source_hash = _canonical_sha(
                {
                    "candidate_source_input_hash": candidate_hash,
                    "financial_collection_id": manifest["financial_collection_id"],
                    "evidence_ids": evidence_ids,
                }
            )
            if row["source_input_hash"] != expected_source_hash:
                raise ValueError("financial negative-list overlay source_input_hash drift")
            status_text = str(row["decision_status"])
            state_counts[status_text] = state_counts.get(status_text, 0) + 1
            year = str(row["as_of"])[:4]
            per_year = year_counts.setdefault(year, {})
            per_year[status_text] = per_year.get(status_text, 0) + 1
            first_as_of = str(row["as_of"]) if first_as_of is None else min(first_as_of, str(row["as_of"]))
            last_as_of = str(row["as_of"]) if last_as_of is None else max(last_as_of, str(row["as_of"]))
            row_count += 1
    try:
        next(candidate_rows)
    except StopIteration:
        pass
    else:
        raise ValueError("financial negative-list overlay omits candidate rows")
    if row_count != manifest.get("row_count"):
        raise ValueError("financial negative-list overlay row_count drift")
    if len(actual_paths) != manifest.get("partition_count"):
        raise ValueError("financial negative-list overlay partition_count drift")
    if _dataset_hash({str(k): str(v) for k, v in partition_hashes.items()}) != manifest.get("dataset_hash"):
        raise ValueError("financial negative-list overlay dataset hash drift")
    if _sha256_file(review_path) != manifest.get("coverage_review_sha256"):
        raise ValueError("financial negative-list overlay coverage review hash drift")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if review.get("row_count") != row_count or review.get("partition_count") != len(actual_paths):
        raise ValueError("financial negative-list overlay review counts drift")
    if (
        manifest.get("coverage_start") != first_as_of
        or manifest.get("coverage_end") != last_as_of
        or review.get("coverage_start") != first_as_of
        or review.get("coverage_end") != last_as_of
        or review.get("partitioning") != "decision_date"
        or review.get("decision_status_counts") != dict(sorted(state_counts.items()))
        or review.get("decision_status_counts_by_year")
        != {year: dict(sorted(counts.items())) for year, counts in sorted(year_counts.items())}
        or review.get("rule_state_counts") != rule_state_counts
    ):
        raise ValueError("financial negative-list overlay coverage review drift")
    for readiness_key in (
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_portfolio_construction",
        "ready_for_trading",
    ):
        if manifest.get(readiness_key) is not False or review.get(readiness_key) is not False:
            raise ValueError(f"financial negative-list overlay {readiness_key} must remain false")
    if (
        manifest.get("missing_stays_unknown") is not True
        or manifest.get("not_integrated_into_scoring_or_universe") is not True
        or manifest.get("research_only") is not True
        or review.get("missing_stays_unknown") is not True
        or review.get("same_day_235959_disclosure_unusable_at_173000_decision") is not True
        or review.get("future_payload_values_included") is not False
    ):
        raise ValueError("financial negative-list overlay research boundary drift")
    return FinancialNegativeListOverlayResult(
        output_dir=overlay,
        overlay_id=overlay_id,
        row_count=row_count,
        partition_count=len(actual_paths),
        coverage_start=str(manifest["coverage_start"]),
        coverage_end=str(manifest["coverage_end"]),
        collection_id=str(manifest["financial_collection_id"]),
        manifest_path=manifest_path,
        coverage_review_path=review_path,
    )


__all__ = [
    "COVERAGE_REVIEW_NAME",
    "DEFAULT_FINANCIAL_COLLECTION_DIR",
    "DEFAULT_OUTPUT_DIR",
    "FinancialNegativeListOverlayResult",
    "FinancialSymbolEvaluator",
    "MANIFEST_NAME",
    "OVERLAY_SCHEMA_VERSION",
    "OVERLAY_VERSION",
    "RuleResult",
    "materialize_financial_negative_list_verdict_overlay",
    "verify_financial_negative_list_verdict_overlay",
]
