"""Layer-two contract-bound statistical risk-cluster constraint evidence (E10c).

Wraps the generic E6b diagnostic with the frozen two-layer decision contract.
This is a **statistical risk proxy**, not industry classification or alpha.
Complete reports may later feed an E10d constraint allocator; this milestone
never auto-applies clusters to scoring, portfolios, orders, or trading.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.statistical_risk_clusters import (
    RISK_PROXY_NOTE,
    StatisticalRiskClusterReport,
    diagnose_statistical_risk_clusters,
    report_uses_only_as_of_or_earlier,
)
from app.research.two_layer_contract import (
    DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH,
    StatisticalRiskClusterPolicyConfirmed,
    TwoLayerStrategyDecisionContractV2,
    load_two_layer_decision_draft,
    verify_two_layer_decision_draft,
)
from app.storage.protocol import MarketStore

LAYER_TWO_STATISTICAL_RISK_CLUSTERS_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_TWO_STATISTICAL_RISK_CLUSTERS_ENGINE_VERSION: Literal["layer-two-statistical-risk-clusters-engine-v1"] = (
    "layer-two-statistical-risk-clusters-engine-v1"
)

BOUND_TWO_LAYER_DECISION_CONTRACT_PATH: Literal["config/research/two-layer-strategy-decision-draft-v1.json"] = (
    "config/research/two-layer-strategy-decision-draft-v1.json"
)
BOUND_TWO_LAYER_DECISION_CONTRACT_ID = "27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6"

# Frozen economics from the bound two-layer contract (caller cannot override).
BOUND_LOOKBACK_TRADING_DAYS: Literal[120] = 120
BOUND_CORRELATION_THRESHOLD: float = 0.65
BOUND_MAX_SLEEVE_WEIGHT_PER_CLUSTER: float = 0.35
BOUND_MAX_POSITIONS_PER_CLUSTER: Literal[2] = 2
BOUND_REQUIRED_PRICE_POINTS: Literal[121] = 121
BOUND_LINKAGE: Literal["connected_components_chain"] = "connected_components_chain"

PROMINENT_RISK_PROXY_ANNOTATION: Literal[
    "STATISTICAL RISK PROXY — NOT INDUSTRY CLASSIFICATION. "
    "Clusters are connected components of pairwise Pearson correlations at or above "
    "the contract threshold; chain linkage is possible. adj_close is used only for "
    "sealed-snapshot return correlation. This is not a future industry label, not alpha, "
    "and must not be silently treated as sector neutrality. PIT industry history remains "
    "a future enhancement. Only a complete report may be read by a later E10d constraint "
    "allocator; this milestone never auto-applies constraints."
] = (
    "STATISTICAL RISK PROXY — NOT INDUSTRY CLASSIFICATION. "
    "Clusters are connected components of pairwise Pearson correlations at or above "
    "the contract threshold; chain linkage is possible. adj_close is used only for "
    "sealed-snapshot return correlation. This is not a future industry label, not alpha, "
    "and must not be silently treated as sector neutrality. PIT industry history remains "
    "a future enhancement. Only a complete report may be read by a later E10d constraint "
    "allocator; this milestone never auto-applies constraints."
)

_HEX64 = r"^[0-9a-f]{64}$"
_CANONICAL_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(SH|SZ)$")
_BUDGET_ABS_TOL = 1e-12


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


def _require_exact_float(value: float, expected: float, *, field_name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if abs(value - expected) > _BUDGET_ABS_TOL:
        raise ValueError(f"{field_name} must equal {expected}")
    return expected


def _normalize_canonical_candidates(symbols: Sequence[str]) -> list[str]:
    if not isinstance(symbols, Sequence) or isinstance(symbols, (str, bytes)):
        raise ValueError("symbols must be a sequence of unique canonical A-share codes")
    if len(symbols) == 0:
        raise ValueError("symbols must be non-empty")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in symbols:
        if not isinstance(item, str):
            raise ValueError("symbols entries must be non-empty strings")
        symbol = _reject_blank_string(item, field_name="symbol")
        _validate_canonical_symbol(symbol)
        if symbol in seen:
            raise ValueError(f"duplicate candidate symbol: {symbol}")
        seen.add(symbol)
        normalized.append(symbol)
    return sorted(normalized)


class LayerTwoStatisticalRiskClusterPolicy(_StrictModel):
    statistical_risk_cluster: StatisticalRiskClusterPolicyConfirmed
    pit_industry_current_proxy: Literal["statistical_risk_clusters"] = "statistical_risk_clusters"
    current_industry_backfill_forbidden: Literal[True] = True
    must_be_prominently_annotated: Literal[True] = True


class LayerTwoStatisticalRiskClusterReport(_StrictModel):
    """Contract-bound cluster constraint evidence; never authorizes trading."""

    schema_version: Literal["1"] = LAYER_TWO_STATISTICAL_RISK_CLUSTERS_SCHEMA_VERSION
    engine_version: Literal["layer-two-statistical-risk-clusters-engine-v1"] = (
        LAYER_TWO_STATISTICAL_RISK_CLUSTERS_ENGINE_VERSION
    )
    report_id: str | None = Field(default=None, pattern=_HEX64)
    as_of: date
    decision_at: datetime
    data_snapshot_id: str = Field(min_length=1)
    two_layer_decision_contract_id: str = Field(pattern=_HEX64)
    two_layer_decision_contract_path: str = Field(min_length=1)
    lookback_trading_days: Literal[120] = BOUND_LOOKBACK_TRADING_DAYS
    correlation_threshold: float = BOUND_CORRELATION_THRESHOLD
    max_sleeve_weight_per_cluster: float = BOUND_MAX_SLEEVE_WEIGHT_PER_CLUSTER
    max_positions_per_cluster: Literal[2] = BOUND_MAX_POSITIONS_PER_CLUSTER
    linkage: Literal["connected_components_chain"] = BOUND_LINKAGE
    required_price_points: Literal[121] = BOUND_REQUIRED_PRICE_POINTS
    candidates: list[str]
    diagnostic: StatisticalRiskClusterReport
    risk_proxy_annotation: Literal[
        "STATISTICAL RISK PROXY — NOT INDUSTRY CLASSIFICATION. "
        "Clusters are connected components of pairwise Pearson correlations at or above "
        "the contract threshold; chain linkage is possible. adj_close is used only for "
        "sealed-snapshot return correlation. This is not a future industry label, not alpha, "
        "and must not be silently treated as sector neutrality. PIT industry history remains "
        "a future enhancement. Only a complete report may be read by a later E10d constraint "
        "allocator; this milestone never auto-applies constraints."
    ] = PROMINENT_RISK_PROXY_ANNOTATION
    generic_risk_proxy_note: Literal[
        "Clusters are connected components of pairwise correlations at or above the "
        "threshold. Chain linkage is possible; this is a statistical risk proxy only, "
        "not an industry classification or alpha signal."
    ] = RISK_PROXY_NOTE
    is_not_industry_classification: Literal[True] = True
    current_industry_backfill_forbidden: Literal[True] = True
    adj_close_is_return_correlation_only: Literal[True] = True
    pit_industry_history_is_future_enhancement: Literal[True] = True
    ready_for_cluster_constraints: bool
    ready_for_scoring: Literal[False] = False
    ready_for_portfolio_construction: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False

    @field_validator("data_snapshot_id", "two_layer_decision_contract_path", mode="before")
    @classmethod
    def _non_blank(cls, value: object, info: Any) -> object:
        return _reject_blank_string(value, field_name=str(info.field_name))

    @field_validator("decision_at")
    @classmethod
    def _decision_at(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, field_name="decision_at")

    @field_validator("correlation_threshold", "max_sleeve_weight_per_cluster")
    @classmethod
    def _finite_unit(cls, value: float, info: Any) -> float:
        expected = (
            BOUND_CORRELATION_THRESHOLD
            if info.field_name == "correlation_threshold"
            else BOUND_MAX_SLEEVE_WEIGHT_PER_CLUSTER
        )
        return _require_exact_float(value, expected, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _gate_flags(self) -> LayerTwoStatisticalRiskClusterReport:
        if _decision_calendar_date(self.decision_at) != self.as_of:
            raise ValueError("decision_at calendar date must equal as_of")
        if self.candidates != sorted(self.candidates):
            raise ValueError("candidates must be stably sorted")
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("candidates must be unique")
        for symbol in self.candidates:
            _validate_canonical_symbol(symbol)
        if self.diagnostic.candidates != self.candidates:
            raise ValueError("diagnostic.candidates must equal report candidates")
        if self.diagnostic.as_of != self.as_of:
            raise ValueError("diagnostic.as_of must equal report as_of")
        if self.diagnostic.data_snapshot_id != self.data_snapshot_id:
            raise ValueError("diagnostic.data_snapshot_id must equal report data_snapshot_id")
        if self.diagnostic.lookback_bars != BOUND_LOOKBACK_TRADING_DAYS:
            raise ValueError("diagnostic.lookback_bars must equal bound lookback")
        if abs(self.diagnostic.correlation_threshold - BOUND_CORRELATION_THRESHOLD) > _BUDGET_ABS_TOL:
            raise ValueError("diagnostic.correlation_threshold must equal bound threshold")
        has_unresolved = bool(self.diagnostic.unresolved_symbols) or bool(self.diagnostic.unresolved_pairs)
        if has_unresolved and self.ready_for_cluster_constraints:
            raise ValueError("ready_for_cluster_constraints must be false when unresolved symbols/pairs exist")
        if not has_unresolved and not self.ready_for_cluster_constraints:
            raise ValueError("ready_for_cluster_constraints must be true when the cluster diagnostic is complete")
        if self.diagnostic.ready_for_portfolio_constraints != self.ready_for_cluster_constraints:
            raise ValueError("ready_for_cluster_constraints must track diagnostic.ready_for_portfolio_constraints")
        if (
            self.ready_for_scoring
            or self.ready_for_portfolio_construction
            or self.ready_for_orders
            or self.ready_for_trading
            or self.auto_apply
        ):
            raise ValueError("report cannot authorize scoring, portfolio construction, orders, or trading")
        if self.is_not_industry_classification is not True:
            raise ValueError("is_not_industry_classification must remain true")
        if self.current_industry_backfill_forbidden is not True:
            raise ValueError("current_industry_backfill_forbidden must remain true")
        return self


def bind_two_layer_statistical_risk_cluster_policy(
    *,
    repo_root: Path,
    contract_path: Path | None = None,
) -> tuple[str, str, LayerTwoStatisticalRiskClusterPolicy]:
    root = Path(repo_root).resolve()
    resolved_path = Path(contract_path) if contract_path is not None else root / BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
    if not resolved_path.is_file():
        raise ValueError(f"two-layer decision contract missing: {resolved_path}")
    draft = load_two_layer_decision_draft(resolved_path)
    if not isinstance(draft, TwoLayerStrategyDecisionContractV2):
        raise ValueError("layer-two statistical risk clusters require schema-v2 two-layer contract")
    result = verify_two_layer_decision_draft(draft)
    if result.contract_id != BOUND_TWO_LAYER_DECISION_CONTRACT_ID:
        raise ValueError("two-layer decision contract_id drifted from E10c bound constant")
    if str(DEFAULT_TWO_LAYER_DECISION_CONTRACT_PATH) != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two-layer decision default path drifted from E10c binding")
    rel_path = _repo_relative_posix(resolved_path, repo_root=root)
    if rel_path != BOUND_TWO_LAYER_DECISION_CONTRACT_PATH:
        raise ValueError("two-layer decision contract path must match bound relative path")

    pit = draft.layer_two.pit_industry
    if (
        pit.user_blocker is not False
        or pit.current_proxy != "statistical_risk_clusters"
        or pit.pit_industry_enhancement != "future_enhancement_not_completed"
        or pit.current_industry_backfill_forbidden is not True
        or pit.research_and_30pct_controlled_trial_allowed_with_clusters_and_annotation is not True
    ):
        raise ValueError("layer_two.pit_industry frozen flags drifted from confirmed contract")

    cluster = draft.layer_two.statistical_risk_cluster
    if cluster.lookback_trading_days != BOUND_LOOKBACK_TRADING_DAYS:
        raise ValueError("statistical_risk_cluster.lookback_trading_days must equal 120")
    _require_exact_float(
        cluster.correlation_threshold,
        BOUND_CORRELATION_THRESHOLD,
        field_name="statistical_risk_cluster.correlation_threshold",
    )
    _require_exact_float(
        cluster.max_sleeve_weight_per_cluster,
        BOUND_MAX_SLEEVE_WEIGHT_PER_CLUSTER,
        field_name="statistical_risk_cluster.max_sleeve_weight_per_cluster",
    )
    if cluster.max_positions_per_cluster != BOUND_MAX_POSITIONS_PER_CLUSTER:
        raise ValueError("statistical_risk_cluster.max_positions_per_cluster must equal 2")
    if (
        cluster.uses_only_data_on_or_before_decision_date is not True
        or cluster.required_when_pit_industry_history_missing is not True
        or cluster.must_be_prominently_annotated is not True
        or cluster.current_industry_backfill_forbidden is not True
    ):
        raise ValueError("statistical_risk_cluster frozen flags drifted from confirmed contract")

    policy = LayerTwoStatisticalRiskClusterPolicy(
        statistical_risk_cluster=cluster,
        pit_industry_current_proxy="statistical_risk_clusters",
        current_industry_backfill_forbidden=True,
        must_be_prominently_annotated=True,
    )
    return result.contract_id, rel_path, policy


def _assert_as_of_is_trading_day_terminal(store: MarketStore, *, as_of: date) -> None:
    calendar = store.get_calendar(date(1970, 1, 1), as_of)
    if as_of not in calendar:
        raise ValueError("as_of must be a market trading day")
    if len(calendar) < BOUND_REQUIRED_PRICE_POINTS:
        # Insufficient history is handled as unresolved by the generic diagnostic;
        # still require as_of itself to be present as the window terminal when enough days exist.
        return
    window = calendar[-BOUND_REQUIRED_PRICE_POINTS:]
    if window[-1] != as_of:
        raise ValueError("as_of must be the final trading day of the 121-point price window")


def diagnose_layer_two_statistical_risk_clusters(
    store: MarketStore,
    as_of: date,
    decision_at: datetime,
    symbols: Sequence[str],
    repo_root: Path,
    *,
    contract_path: Path | None = None,
) -> LayerTwoStatisticalRiskClusterReport:
    """Build sealed contract-bound statistical risk-cluster constraint evidence.

    Economic parameters are taken only from the bound two-layer contract.
    Callers cannot override lookback, threshold, sleeve cap, or max positions.
    """
    as_of = _require_exact_date(as_of, field_name="as_of")
    decision_at = _require_aware_datetime(decision_at, field_name="decision_at")
    if _decision_calendar_date(decision_at) != as_of:
        raise ValueError("decision_at calendar date must equal as_of")

    candidates = _normalize_canonical_candidates(symbols)
    contract_id, contract_rel_path, _policy = bind_two_layer_statistical_risk_cluster_policy(
        repo_root=repo_root,
        contract_path=contract_path,
    )

    snapshot_id = store.snapshot().snapshot_id
    if not snapshot_id:
        raise ValueError("data_snapshot_id must be non-empty")

    _assert_as_of_is_trading_day_terminal(store, as_of=as_of)

    diagnostic = diagnose_statistical_risk_clusters(
        store,
        as_of,
        candidates,
        BOUND_LOOKBACK_TRADING_DAYS,
        BOUND_CORRELATION_THRESHOLD,
    )
    report_uses_only_as_of_or_earlier(diagnostic, as_of=as_of)
    if diagnostic.data_snapshot_id != snapshot_id:
        raise ValueError("diagnostic data_snapshot_id does not match store snapshot")
    if diagnostic.required_trading_days and diagnostic.required_trading_days[-1] != as_of:
        raise ValueError("required trading-day window must terminate on as_of")

    ready = diagnostic.ready_for_portfolio_constraints
    report = LayerTwoStatisticalRiskClusterReport(
        as_of=as_of,
        decision_at=decision_at,
        data_snapshot_id=snapshot_id,
        two_layer_decision_contract_id=contract_id,
        two_layer_decision_contract_path=contract_rel_path,
        lookback_trading_days=BOUND_LOOKBACK_TRADING_DAYS,
        correlation_threshold=BOUND_CORRELATION_THRESHOLD,
        max_sleeve_weight_per_cluster=BOUND_MAX_SLEEVE_WEIGHT_PER_CLUSTER,
        max_positions_per_cluster=BOUND_MAX_POSITIONS_PER_CLUSTER,
        linkage=BOUND_LINKAGE,
        required_price_points=BOUND_REQUIRED_PRICE_POINTS,
        candidates=candidates,
        diagnostic=diagnostic,
        ready_for_cluster_constraints=ready,
    )
    return seal_layer_two_statistical_risk_cluster_report(report)


def canonical_report_payload(report: LayerTwoStatisticalRiskClusterReport) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"report_id"})


def canonical_report_bytes(report: LayerTwoStatisticalRiskClusterReport) -> bytes:
    return json.dumps(
        canonical_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_report_id(report: LayerTwoStatisticalRiskClusterReport) -> str:
    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def seal_layer_two_statistical_risk_cluster_report(
    report: LayerTwoStatisticalRiskClusterReport,
) -> LayerTwoStatisticalRiskClusterReport:
    return report.model_copy(update={"report_id": compute_report_id(report)})


def assert_report_self_hash(report: LayerTwoStatisticalRiskClusterReport) -> None:
    if report.report_id is None:
        raise ValueError("layer-two statistical risk cluster report_id is missing")
    expected = compute_report_id(report)
    if report.report_id != expected:
        raise ValueError("layer-two statistical risk cluster report_id does not match canonical content hash")


def assert_report_logic_consistent(
    report: LayerTwoStatisticalRiskClusterReport,
    *,
    store: MarketStore,
    repo_root: Path,
) -> None:
    """Recompute from MarketStore + disk contract; do not trust report self-hash alone."""
    if store.snapshot().snapshot_id != report.data_snapshot_id:
        raise ValueError("store snapshot_id does not match report data_snapshot_id")
    recomputed = diagnose_layer_two_statistical_risk_clusters(
        store,
        report.as_of,
        report.decision_at,
        report.candidates,
        repo_root,
        contract_path=Path(repo_root) / report.two_layer_decision_contract_path,
    )
    left = report.model_dump(mode="json", exclude={"report_id"})
    right = recomputed.model_dump(mode="json", exclude={"report_id"})
    if left != right:
        raise ValueError("layer-two statistical risk cluster report does not recompute from store and disk contract")
    if report.report_id != recomputed.report_id:
        raise ValueError("layer-two statistical risk cluster report_id does not match recomputed report_id")


def load_layer_two_statistical_risk_cluster_report(path: Path) -> LayerTwoStatisticalRiskClusterReport:
    try:
        return LayerTwoStatisticalRiskClusterReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("layer-two statistical risk cluster report is missing or invalid") from exc


def verify_layer_two_statistical_risk_cluster_report(
    report: LayerTwoStatisticalRiskClusterReport,
    *,
    store: MarketStore,
    repo_root: Path,
) -> LayerTwoStatisticalRiskClusterReport:
    assert_report_self_hash(report)
    if (
        report.ready_for_scoring
        or report.ready_for_portfolio_construction
        or report.ready_for_orders
        or report.ready_for_trading
        or report.auto_apply
    ):
        raise ValueError(
            "layer-two statistical risk cluster report cannot authorize scoring, "
            "portfolio construction, orders, or trading"
        )
    if report.is_not_industry_classification is not True:
        raise ValueError("is_not_industry_classification must remain true")
    if report.current_industry_backfill_forbidden is not True:
        raise ValueError("current_industry_backfill_forbidden must remain true")
    contract_id, contract_path, _policy = bind_two_layer_statistical_risk_cluster_policy(repo_root=repo_root)
    if report.two_layer_decision_contract_id != contract_id:
        raise ValueError("report two_layer_decision_contract_id does not match disk binding")
    if report.two_layer_decision_contract_path != contract_path:
        raise ValueError("report two_layer_decision_contract_path does not match disk binding")
    assert_report_logic_consistent(report, store=store, repo_root=repo_root)
    return report


def verify_layer_two_statistical_risk_cluster_report_file(
    path: Path,
    *,
    store: MarketStore,
    repo_root: Path,
) -> LayerTwoStatisticalRiskClusterReport:
    report = load_layer_two_statistical_risk_cluster_report(path)
    return verify_layer_two_statistical_risk_cluster_report(
        report,
        store=store,
        repo_root=repo_root,
    )


def write_layer_two_statistical_risk_cluster_report(
    report: LayerTwoStatisticalRiskClusterReport,
    output: Path,
) -> None:
    sealed = seal_layer_two_statistical_risk_cluster_report(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "BOUND_CORRELATION_THRESHOLD",
    "BOUND_LINKAGE",
    "BOUND_LOOKBACK_TRADING_DAYS",
    "BOUND_MAX_POSITIONS_PER_CLUSTER",
    "BOUND_MAX_SLEEVE_WEIGHT_PER_CLUSTER",
    "BOUND_REQUIRED_PRICE_POINTS",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_ID",
    "BOUND_TWO_LAYER_DECISION_CONTRACT_PATH",
    "LAYER_TWO_STATISTICAL_RISK_CLUSTERS_ENGINE_VERSION",
    "LAYER_TWO_STATISTICAL_RISK_CLUSTERS_SCHEMA_VERSION",
    "PROMINENT_RISK_PROXY_ANNOTATION",
    "LayerTwoStatisticalRiskClusterPolicy",
    "LayerTwoStatisticalRiskClusterReport",
    "assert_report_logic_consistent",
    "assert_report_self_hash",
    "bind_two_layer_statistical_risk_cluster_policy",
    "canonical_report_bytes",
    "canonical_report_payload",
    "compute_report_id",
    "diagnose_layer_two_statistical_risk_clusters",
    "load_layer_two_statistical_risk_cluster_report",
    "seal_layer_two_statistical_risk_cluster_report",
    "verify_layer_two_statistical_risk_cluster_report",
    "verify_layer_two_statistical_risk_cluster_report_file",
    "write_layer_two_statistical_risk_cluster_report",
]
