"""Layer-two PIT financial negative-list adjudicator (E10b).

Pure deterministic, read-only evidence combiner: consumes explicit ternary
PIT evidences for a closed rule registry and emits a sealed single-symbol
negative-list report. Established hard exclusions (known non-standard audit
true, or known warning hits >= 2) are never masked by other missing/unknown
rules. Never scores, backtests, trades, materializes balance-sheet fields
from fundamental overlays, or wires into StrategyConfig / portfolio /
trading paths.

Existing fundamental_reports snapshots lack the raw balance-sheet columns
required to compute the four warning rules; this module only adjudicates
caller-supplied evidences.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.two_layer_contract import (
    DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH,
    FinancialNegativeListConfirmed,
    TwoLayerStrategyDecisionContractV2,
    load_two_layer_decision_draft,
    verify_two_layer_decision_draft,
)

LAYER_TWO_FINANCIAL_NEGATIVE_LIST_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_FINANCIAL_NEGATIVE_LIST_ENGINE_VERSION: Literal["layer-two-financial-negative-list-engine-v1"] = (
    "layer-two-financial-negative-list-engine-v1"
)

BOUND_TWO_LAYER_DECISION_CONTRACT_PATH: Literal["config/research/two-layer-strategy-decision-draft-v1.json"] = (
    "config/research/two-layer-strategy-decision-draft-v1.json"
)
BOUND_TWO_LAYER_DECISION_CONTRACT_ID = "27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6"

NON_STANDARD_AUDIT_RULE: Literal["non_standard_audit"] = "non_standard_audit"

WARNING_RULE_CODES: tuple[str, ...] = (
    "large_cash_and_interest_bearing_debt",
    "receivables_inventory_growth_vs_revenue_two_periods",
    "other_receivables_to_assets_over_5pct",
    "goodwill_to_net_assets_over_30pct",
)

REQUIRED_RULE_CODES: tuple[str, ...] = (NON_STANDARD_AUDIT_RULE, *WARNING_RULE_CODES)

FinancialNegativeRuleCode = Literal[
    "non_standard_audit",
    "large_cash_and_interest_bearing_debt",
    "receivables_inventory_growth_vs_revenue_two_periods",
    "other_receivables_to_assets_over_5pct",
    "goodwill_to_net_assets_over_30pct",
]
EvidenceHitState = Literal["true", "false", "unknown"]
DecisionStatus = Literal[
    "clean",
    "halved",
    "hard_excluded",
    "insufficient_evidence",
]

_HEX64 = r"^[0-9a-f]{64}$"
_CANONICAL_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(SH|SZ)$")
_ALLOWED_RULE_SET = frozenset(REQUIRED_RULE_CODES)

REASON_CODE_ORDER: tuple[str, ...] = (
    "insufficient_evidence",
    "non_standard_audit_hard_exclude",
    "warning_hits_ge_2_exclude",
    "warning_hits_eq_1_halve",
    "clean_no_hits",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _reject_blank_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain leading or trailing whitespace")
    return value


def _require_aware_datetime(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _require_exact_date(value: object, *, field_name: str) -> date:
    if type(value) is date:
        return value
    if isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a date, not a datetime")
    if isinstance(value, str):
        text = value.strip()
        if text == "" or "T" in text or " " in text:
            raise ValueError(f"{field_name} must be an ISO calendar date")
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO calendar date") from exc
    raise ValueError(f"{field_name} must be a date")


def _decision_calendar_date(decision_at: datetime) -> date:
    return _require_aware_datetime(decision_at, field_name="decision_at").date()


def _validate_canonical_symbol(symbol: str) -> None:
    if symbol != symbol.strip():
        raise ValueError("symbol must not contain leading or trailing whitespace")
    if _CANONICAL_SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise ValueError("symbol must be exactly six digits plus uppercase .SH or .SZ")


def _repo_relative_posix(path: Path, *, repo_root: Path) -> str:
    resolved = Path(path).resolve()
    root = Path(repo_root).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("contract path must be inside repo_root") from exc


def _ordered_reason_codes(reasons: set[str]) -> list[str]:
    ordered = [code for code in REASON_CODE_ORDER if code in reasons]
    extras = sorted(reason for reason in reasons if reason not in REASON_CODE_ORDER)
    return ordered + extras


class LayerTwoFinancialNegativeEvidence(_StrictModel):
    """One PIT-auditable ternary evidence row for a closed-registry rule."""

    symbol: str = Field(min_length=1)
    rule_code: FinancialNegativeRuleCode
    hit_state: EvidenceHitState
    observation_as_of: date
    report_period: date
    available_at: datetime
    source: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)

    @field_validator("symbol", "source", "evidence_id", mode="before")
    @classmethod
    def _non_blank(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=str(info.field_name))

    @field_validator("available_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, field_name="available_at")

    @field_validator("observation_as_of", "report_period", mode="before")
    @classmethod
    def _dates(cls, value: object, info: Any) -> date:
        return _require_exact_date(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _symbol_and_rule(self) -> LayerTwoFinancialNegativeEvidence:
        _validate_canonical_symbol(self.symbol)
        if self.rule_code not in _ALLOWED_RULE_SET:
            raise ValueError(f"illegal financial negative rule_code: {self.rule_code}")
        return self


class LayerTwoFinancialNegativeListPolicy(_StrictModel):
    financial_negative_list: FinancialNegativeListConfirmed


class LayerTwoFinancialNegativeListReport(_StrictModel):
    schema_version: Literal["1"] = LAYER_TWO_FINANCIAL_NEGATIVE_LIST_SCHEMA_VERSION
    engine_version: Literal["layer-two-financial-negative-list-engine-v1"] = (
        LAYER_TWO_FINANCIAL_NEGATIVE_LIST_ENGINE_VERSION
    )
    report_id: str | None = Field(default=None, pattern=_HEX64)
    as_of: date
    decision_at: datetime
    symbol: str = Field(min_length=1)
    data_snapshot_id: str = Field(min_length=1)
    two_layer_decision_contract_id: str = Field(pattern=_HEX64)
    two_layer_decision_contract_path: str = Field(min_length=1)
    evidences: list[LayerTwoFinancialNegativeEvidence]
    input_evidence_hashes: list[str]
    decision_status: DecisionStatus
    reason_codes: list[str]
    known_hit_codes: list[str]
    unknown_codes: list[str]
    known_warning_hit_count: int | None = Field(default=None, ge=0)
    target_multiplier: float | None = None
    eligible_for_new_entry: bool
    ownership_role: Literal["diagnostic_not_used"] = "diagnostic_not_used"
    alpha_role: Literal["not_used_cannot_offset_exclusion"] = "not_used_cannot_offset_exclusion"
    event_roles_excluded: Literal["ownership_holder_proxy_pledge_unlock_event_candidates_not_in_adjudication"] = (
        "ownership_holder_proxy_pledge_unlock_event_candidates_not_in_adjudication"
    )
    research_only: Literal[True] = True
    implementation_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_trading: Literal[False] = False
    does_not_trade: Literal[True] = True

    @field_validator("data_snapshot_id", "symbol", mode="before")
    @classmethod
    def _non_blank(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=str(info.field_name))

    @field_validator("decision_at")
    @classmethod
    def _decision_at(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, field_name="decision_at")

    @model_validator(mode="after")
    def _gate_flags(self) -> LayerTwoFinancialNegativeListReport:
        _validate_canonical_symbol(self.symbol)
        if self.research_only is not True or self.implementation_only is not True:
            raise ValueError("research_only and implementation_only must remain true")
        if (
            self.ready_for_scoring
            or self.ready_for_portfolio_construction
            or self.ready_for_trading
            or self.does_not_trade is not True
        ):
            raise ValueError("report cannot authorize scoring, portfolio construction, or trading")
        if self.ownership_role != "diagnostic_not_used":
            raise ValueError("ownership_role must remain diagnostic_not_used")
        if self.alpha_role != "not_used_cannot_offset_exclusion":
            raise ValueError("alpha cannot offset financial negative-list exclusion")
        if len(self.input_evidence_hashes) != len(self.evidences):
            raise ValueError("input_evidence_hashes length must match evidences")
        for digest in self.input_evidence_hashes:
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("input_evidence_hashes entries must be 64-char lowercase hex")
        if self.known_hit_codes != sorted(self.known_hit_codes):
            raise ValueError("known_hit_codes must be sorted")
        if self.unknown_codes != sorted(self.unknown_codes):
            raise ValueError("unknown_codes must be sorted")
        if len(set(self.known_hit_codes)) != len(self.known_hit_codes):
            raise ValueError("known_hit_codes must be unique")
        if len(set(self.unknown_codes)) != len(self.unknown_codes):
            raise ValueError("unknown_codes must be unique")
        if self.decision_status == "insufficient_evidence":
            if self.target_multiplier is not None:
                raise ValueError("insufficient_evidence requires target_multiplier=None")
            if self.eligible_for_new_entry is not False:
                raise ValueError("insufficient_evidence requires eligible_for_new_entry=false")
            if self.known_warning_hit_count is not None:
                raise ValueError("insufficient_evidence must not report known_warning_hit_count")
        elif self.target_multiplier not in (0.0, 0.5, 1.0):
            raise ValueError("target_multiplier must be 0.0, 0.5, or 1.0 when evidence is sufficient")
        return self


def bind_two_layer_financial_negative_list_policy(
    *,
    repo_root: Path,
    contract_path: Path | None = None,
) -> tuple[str, str, LayerTwoFinancialNegativeListPolicy]:
    root = Path(repo_root).resolve()
    resolved_path = Path(contract_path) if contract_path is not None else root / BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
    if not resolved_path.is_file():
        raise ValueError(f"two-layer decision contract missing: {resolved_path}")
    draft = load_two_layer_decision_draft(resolved_path)
    if not isinstance(draft, TwoLayerStrategyDecisionContractV2):
        raise ValueError("layer-two financial negative list requires schema-v2 two-layer contract")
    result = verify_two_layer_decision_draft(draft)
    if result.contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
        raise ValueError("two-layer decision contract_id drifted from E10b bound constant")
    if str(DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH) != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two-layer decision default path drifted from E10b binding")
    rel_path = _repo_relative_posix(resolved_path, repo_root=root)
    if rel_path != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two-layer decision contract path must match bound relative path")
    policy_flags = draft.layer_two.financial_negative_list
    if (
        policy_flags.non_standard_audit_single_hit_excludes is not True
        or policy_flags.other_known_pit_auditable_warning_hits_ge_2_excludes is not True
        or policy_flags.other_known_pit_auditable_warning_hits_eq_1_halves_target is not True
        or policy_flags.missing_stays_unknown_and_is_not_a_miss is not True
        or policy_flags.exclusion_cannot_be_offset_by_alpha is not True
    ):
        raise ValueError("financial_negative_list frozen flags drifted from confirmed contract")
    if draft.execution.decision_after_close_on_t is not True:
        raise ValueError("execution.decision_after_close_on_t must remain true")
    if draft.layer_two.candidate_shortage.retain_cash_when_critical_input_missing is not True:
        raise ValueError("candidate shortage retain-cash-on-missing must remain true")
    policy = LayerTwoFinancialNegativeListPolicy(financial_negative_list=policy_flags)
    return result.contract_id, rel_path, policy


def canonical_evidence_payload(evidence: LayerTwoFinancialNegativeEvidence) -> dict[str, Any]:
    return evidence.model_dump(mode="json")


def canonical_evidence_bytes(evidence: LayerTwoFinancialNegativeEvidence) -> bytes:
    return json.dumps(
        canonical_evidence_payload(evidence),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_evidence_hash(evidence: LayerTwoFinancialNegativeEvidence) -> str:
    return hashlib.sha256(canonical_evidence_bytes(evidence)).hexdigest()


def _sort_evidences(
    evidences: Sequence[LayerTwoFinancialNegativeEvidence],
) -> list[LayerTwoFinancialNegativeEvidence]:
    return sorted(
        evidences,
        key=lambda item: (
            item.rule_code,
            item.evidence_id,
            item.source,
            item.observation_as_of.isoformat(),
            item.report_period.isoformat(),
            item.available_at.isoformat(),
            item.hit_state,
        ),
    )


def assert_evidence_integrity(
    evidences: Sequence[LayerTwoFinancialNegativeEvidence],
    *,
    symbol: str,
    decision_at: datetime,
) -> list[LayerTwoFinancialNegativeEvidence]:
    """Reject late/future/duplicate/alias/illegal evidence before adjudication."""
    symbol = _reject_blank_string(symbol, field_name="symbol")
    _validate_canonical_symbol(symbol)
    decision_at = _require_aware_datetime(decision_at, field_name="decision_at")
    as_of = _decision_calendar_date(decision_at)

    seen_rules: dict[str, LayerTwoFinancialNegativeEvidence] = {}
    for evidence in evidences:
        if evidence.symbol != symbol:
            raise ValueError("evidence symbol must equal adjudication symbol")
        if evidence.rule_code not in _ALLOWED_RULE_SET:
            raise ValueError(f"illegal financial negative rule_code: {evidence.rule_code}")
        if evidence.available_at > decision_at:
            raise ValueError("evidence available_at must be on or before decision_at")
        if evidence.observation_as_of > as_of:
            raise ValueError("evidence observation_as_of cannot be after decision date")
        if evidence.report_period > as_of:
            raise ValueError("evidence report_period cannot be after decision date")
        prior = seen_rules.get(evidence.rule_code)
        if prior is not None:
            raise ValueError(f"duplicate conflicting evidence for rule_code={evidence.rule_code}")
        seen_rules[evidence.rule_code] = evidence

    return _sort_evidences(evidences)


def _resolve_rule_states(
    evidences: Sequence[LayerTwoFinancialNegativeEvidence],
) -> tuple[dict[str, EvidenceHitState | None], list[str], list[str]]:
    by_rule: dict[str, EvidenceHitState] = {str(item.rule_code): item.hit_state for item in evidences}
    states: dict[str, EvidenceHitState | None] = {}
    known_hits: list[str] = []
    unknowns: list[str] = []
    for rule in REQUIRED_RULE_CODES:
        state = by_rule.get(rule)
        states[rule] = state
        if state is None or state == "unknown":
            unknowns.append(rule)
        elif state == "true":
            known_hits.append(rule)
    return states, known_hits, unknowns


def _adjudicate(
    *,
    states: dict[str, EvidenceHitState | None],
    unknowns: list[str],
    policy: LayerTwoFinancialNegativeListPolicy,
) -> tuple[DecisionStatus, list[str], float | None, bool, int | None]:
    # Policy flags are validated at bind time; retain the argument for explicit contract coupling.
    if (
        policy.financial_negative_list.non_standard_audit_single_hit_excludes is not True
        or policy.financial_negative_list.exclusion_cannot_be_offset_by_alpha is not True
    ):
        raise ValueError("financial_negative_list policy flags must remain fail-closed")

    # Established hard exclusions must not be masked by other missing/unknown rules.
    # Priority: known non_standard true → known warning hits >= 2 → insufficient → all-known 1/0.
    warning_hit_count = sum(1 for code in WARNING_RULE_CODES if states[code] == "true")
    non_standard = states[NON_STANDARD_AUDIT_RULE]

    if non_standard == "true":
        return (
            "hard_excluded",
            _ordered_reason_codes({"non_standard_audit_hard_exclude"}),
            0.0,
            False,
            warning_hit_count,
        )

    if warning_hit_count >= 2:
        return (
            "hard_excluded",
            _ordered_reason_codes({"warning_hits_ge_2_exclude"}),
            0.0,
            False,
            warning_hit_count,
        )

    if unknowns:
        return (
            "insufficient_evidence",
            _ordered_reason_codes({"insufficient_evidence"}),
            None,
            False,
            None,
        )

    if warning_hit_count == 1:
        return (
            "halved",
            _ordered_reason_codes({"warning_hits_eq_1_halve"}),
            0.5,
            True,
            warning_hit_count,
        )
    return (
        "clean",
        _ordered_reason_codes({"clean_no_hits"}),
        1.0,
        True,
        0,
    )


def evaluate_layer_two_financial_negative_list(
    *,
    symbol: str,
    decision_at: datetime,
    data_snapshot_id: str,
    evidences: Sequence[LayerTwoFinancialNegativeEvidence],
    repo_root: Path,
    contract_path: Path | None = None,
) -> LayerTwoFinancialNegativeListReport:
    """Adjudicate one symbol at one after-close decision_at from explicit evidences."""
    symbol = _reject_blank_string(symbol, field_name="symbol")
    _validate_canonical_symbol(symbol)
    decision_at = _require_aware_datetime(decision_at, field_name="decision_at")
    as_of = _decision_calendar_date(decision_at)
    snapshot_id = _reject_blank_string(data_snapshot_id, field_name="data_snapshot_id")

    contract_id, contract_rel_path, policy = bind_two_layer_financial_negative_list_policy(
        repo_root=repo_root,
        contract_path=contract_path,
    )
    sorted_evidences = assert_evidence_integrity(
        evidences,
        symbol=symbol,
        decision_at=decision_at,
    )
    evidence_hashes = [compute_evidence_hash(item) for item in sorted_evidences]
    _states, known_hits, unknowns = _resolve_rule_states(sorted_evidences)
    decision_status, reason_codes, multiplier, eligible, warning_hit_count = _adjudicate(
        states=_states,
        unknowns=unknowns,
        policy=policy,
    )

    report = LayerTwoFinancialNegativeListReport(
        as_of=as_of,
        decision_at=decision_at,
        symbol=symbol,
        data_snapshot_id=snapshot_id,
        two_layer_decision_contract_id=contract_id,
        two_layer_decision_contract_path=contract_rel_path,
        evidences=sorted_evidences,
        input_evidence_hashes=evidence_hashes,
        decision_status=decision_status,
        reason_codes=reason_codes,
        known_hit_codes=sorted(known_hits),
        unknown_codes=sorted(unknowns),
        known_warning_hit_count=warning_hit_count,
        target_multiplier=multiplier,
        eligible_for_new_entry=eligible,
    )
    return seal_layer_two_financial_negative_list_report(report)


def canonical_report_payload(report: LayerTwoFinancialNegativeListReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: LayerTwoFinancialNegativeListReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: LayerTwoFinancialNegativeListReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_layer_two_financial_negative_list_report(
    report: LayerTwoFinancialNegativeListReport,
) -> LayerTwoFinancialNegativeListReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: LayerTwoFinancialNegativeListReport) -> None:
    if report.report_id is None:
        raise ValueError("layer-two financial negative list report_id is missing")
    expected = compute_report_id(report)
    if report.report_id != expected:
        raise ValueError("layer-two financial negative list report_id does not match canonical content hash")


def assert_report_logic_consistent(
    report: LayerTwoFinancialNegativeListReport,
    *,
    repo_root: Path,
) -> None:
    recomputed = evaluate_layer_two_financial_negative_list(
        symbol=report.symbol,
        decision_at=report.decision_at,
        data_snapshot_id=report.data_snapshot_id,
        evidences=report.evidences,
        repo_root=repo_root,
        contract_path=Path(repo_root) / report.two_layer_decision_contract_path,
    )
    left = report.model_dump(mode="json", exclude={"report_id"})
    right = recomputed.model_dump(mode="json", exclude={"report_id"})
    if left != right:
        raise ValueError("financial negative list report does not recompute from sealed semantic inputs")
    if report.report_id != recomputed.report_id:
        raise ValueError("financial negative list report_id does not match recomputed report_id")


def load_layer_two_financial_negative_list_report(path: Path) -> LayerTwoFinancialNegativeListReport:
    try:
        return LayerTwoFinancialNegativeListReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("layer-two financial negative list report is missing or invalid") from exc


def verify_layer_two_financial_negative_list_report(
    report: LayerTwoFinancialNegativeListReport,
    *,
    repo_root: Path,
) -> LayerTwoFinancialNegativeListReport:
    assert_report_self_hash(report)
    if (
        report.ready_for_scoring
        or report.ready_for_portfolio_construction
        or report.ready_for_trading
        or report.does_not_trade is not True
    ):
        raise ValueError("layer-two financial negative list report cannot authorize downstream execution")
    contract_id, contract_path, _policy = bind_two_layer_financial_negative_list_policy(repo_root=repo_root)
    if report.two_layer_decision_contract_id != contract_id:
        raise ValueError("report two_layer_decision_contract_id does not match disk binding")
    if report.two_layer_decision_contract_path != contract_path:
        raise ValueError("report two_layer_decision_contract_path does not match disk binding")
    assert_report_logic_consistent(report, repo_root=repo_root)
    return report


def verify_layer_two_financial_negative_list_report_file(
    path: Path,
    *,
    repo_root: Path,
) -> LayerTwoFinancialNegativeListReport:
    report = load_layer_two_financial_negative_list_report(path)
    return verify_layer_two_financial_negative_list_report(report, repo_root=repo_root)


def write_layer_two_financial_negative_list_report(
    report: LayerTwoFinancialNegativeListReport,
    output: Path,
) -> None:
    sealed = seal_layer_two_financial_negative_list_report(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "BOUND_TWO_LAYER_DECISION_CONTRACT_ID",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_PATH",
    "LAYER_TWO_FINANCIAL_NEGATIVE_LIST_ENGINE_VERSION",
    "LAYER_TWO_FINANCIAL_NEGATIVE_LIST_SCHEMA_VERSION",
    "NON_STANDARD_AUDIT_RULE",
    "REASON_CODE_ORDER",
    "REQUIRED_RULE_CODES",
    "WARNING_RULE_CODES",
    "LayerTwoFinancialNegativeEvidence",
    "LayerTwoFinancialNegativeListPolicy",
    "LayerTwoFinancialNegativeListReport",
    "assert_evidence_integrity",
    "assert_report_logic_consistent",
    "assert_report_self_hash",
    "bind_two_layer_financial_negative_list_policy",
    "compute_evidence_hash",
    "compute_report_id",
    "evaluate_layer_two_financial_negative_list",
    "load_layer_two_financial_negative_list_report",
    "seal_layer_two_financial_negative_list_report",
    "verify_layer_two_financial_negative_list_report",
    "verify_layer_two_financial_negative_list_report_file",
    "write_layer_two_financial_negative_list_report",
]
