from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.research.event_candidate_diagnostics import CandidateHypothesisSpec
from app.research.event_candidate_freeze import (
    DEFAULT_EVENT_CANDIDATE_OOS_FREEZE_PATH,
    PRIMARY_BENCHMARK_SYMBOL,
    PRIMARY_OOS_ENDPOINT,
    PrimaryOosEndpoint,
    load_verified_event_candidate_oos_freeze,
)

EVENT_CANDIDATE_OOS_AUTH_SCHEMA_VERSION: Literal["1"] = "1"
EVENT_CANDIDATE_OOS_AUTH_VERSION: Literal["a-share-event-candidate-oos-one-shot-v1"] = (
    "a-share-event-candidate-oos-one-shot-v1"
)
DEFAULT_EVENT_CANDIDATE_OOS_AUTH_PATH = Path(
    "config/research/a-share-event-candidate-oos-one-shot-authorization-v1.json"
)
AUTHORIZED_FREEZE_ID = "5d5298f0115f883c29d96cf2a1892ce4de7295c2068cabea96f23db393bad92e"
AUTHORIZED_MARKET_SNAPSHOT_ID = "b6f664d31d8ffcdabbb655e888467c75dbfa6a7f8bd863d698febb015f5b0427"
AUTHORIZED_EVENT_SNAPSHOT_ID = "73f1dedf83b0c28d0ba5ae933205e2777b02e27d356d4dd5cf62dcb10155b28f"
AUTHORIZED_MARKET_DIR = "data/all-a-share-oos-20241001-20260821-v1/parquet"
AUTHORIZED_EVENT_DIR = "data/all-a-share-oos-20241001-20260821-v1/events-v1"
AUTHORIZED_OUTPUT_DIR = (
    "data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1"
)
AUTHORIZED_RECEIPT_PATH = (
    "data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/"
    "one-shot-v1.consumption-receipt.json"
)
AUTHORIZED_ANNOUNCEMENT_START = date(2025, 1, 1)
AUTHORIZED_ANNOUNCEMENT_END = date(2026, 7, 23)
AUTHORIZED_FIRST_2025_TRADING_DAY = date(2025, 1, 2)
AUTHORIZED_LAST_COMPLETE_LABEL_ENTRY = date(2026, 7, 24)
AUTHORIZED_LABEL_HARD_END = date(2026, 8, 21)
AUTHORIZED_SOURCE_COVERAGE_START = date(2024, 10, 8)
AUTHORIZED_SOURCE_COVERAGE_END = date(2026, 8, 21)
AUTHORIZED_STRATEGY_CONFIG_ID = "all_a_share_historical_value_portfolio_selected_v2"
AUTHORIZED_STRATEGY_CONFIG_HASH = "796b793856dcd02a"
AUTHORIZED_CANDIDATES: tuple[CandidateHypothesisSpec, ...] = (
    CandidateHypothesisSpec(
        hypothesis_id="forecast_upward_revision",
        source="forecast",
        signal_kind="binary_bucket",
        threshold_bucket="upward_revision",
        candidate_direction="positive",
        economic_meaning="同一报告期后续公告上调变动中点，预期正向反应",
    ),
    CandidateHypothesisSpec(
        hypothesis_id="audit_non_standard_opinion",
        source="fina_audit",
        signal_kind="binary_bucket",
        threshold_bucket="non_standard_opinion",
        candidate_direction="negative",
        economic_meaning="审计意见非精确标准无保留意见，预期负向反应",
    ),
)
MIN_KNOWN_COVERAGE = 0.90
MIN_LABELED = 100
MIN_BINARY_ARM_LABELED = 20


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthorizedOosWindows(_StrictModel):
    announcement_start: date
    announcement_end: date
    first_2025_trading_day: date
    last_complete_label_entry_date: date
    label_hard_end: date
    event_source_coverage_start: date
    event_source_coverage_end: date


class AuthorizedEvaluationGates(_StrictModel):
    min_known_coverage: float = MIN_KNOWN_COVERAGE
    min_labeled: int = MIN_LABELED
    min_binary_arm_labeled: int = MIN_BINARY_ARM_LABELED
    primary_statistic: Literal["mean_rel_hs300_return_spread_1_minus_0"] = (
        "mean_rel_hs300_return_spread_1_minus_0"
    )
    no_p_value: Literal[True] = True
    no_alpha_claim: Literal[True] = True


class EventCandidateOosOneShotAuthorization(_StrictModel):
    schema_version: Literal["1"] = EVENT_CANDIDATE_OOS_AUTH_SCHEMA_VERSION
    authorization_version: Literal["a-share-event-candidate-oos-one-shot-v1"] = (
        EVENT_CANDIDATE_OOS_AUTH_VERSION
    )
    authorization_date: date
    one_shot: Literal[True] = True
    consumed: Literal[False] = False
    freeze_file: str
    freeze_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_config_id: str = Field(min_length=1)
    strategy_config_hash: str = Field(min_length=1)
    market_dir: str = Field(min_length=1)
    market_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_dir: str = Field(min_length=1)
    event_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_market_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    windows: AuthorizedOosWindows
    benchmark_symbol: Literal["000300.SH"] = PRIMARY_BENCHMARK_SYMBOL
    primary_endpoint: PrimaryOosEndpoint
    nominated_candidates: list[CandidateHypothesisSpec]
    nominated_hypothesis_ids: list[str]
    candidate_multiplicity: Literal[2] = 2
    evaluation_gates: AuthorizedEvaluationGates
    output_dir: str = Field(min_length=1)
    consumption_receipt_path: str = Field(min_length=1)
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    human_review_required: Literal[True] = True
    authorization_id: str | None = None
    research_boundary: str = (
        "Authorized one-shot 2025+ directional replication diagnostic only. No score, IC, "
        "exclusion, portfolio, order, trade, p-value, alpha claim, or automatic promotion."
    )


def build_committed_event_candidate_oos_authorization() -> EventCandidateOosOneShotAuthorization:
    """Build the sealed committed authorization contract for the first 2025+ one-shot OOS."""
    contract = EventCandidateOosOneShotAuthorization(
        authorization_date=date(2026, 8, 25),
        freeze_file=str(DEFAULT_EVENT_CANDIDATE_OOS_FREEZE_PATH),
        freeze_id=AUTHORIZED_FREEZE_ID,
        strategy_config_id=AUTHORIZED_STRATEGY_CONFIG_ID,
        strategy_config_hash=AUTHORIZED_STRATEGY_CONFIG_HASH,
        market_dir=AUTHORIZED_MARKET_DIR,
        market_snapshot_id=AUTHORIZED_MARKET_SNAPSHOT_ID,
        event_dir=AUTHORIZED_EVENT_DIR,
        event_snapshot_id=AUTHORIZED_EVENT_SNAPSHOT_ID,
        base_market_snapshot_id=AUTHORIZED_MARKET_SNAPSHOT_ID,
        windows=AuthorizedOosWindows(
            announcement_start=AUTHORIZED_ANNOUNCEMENT_START,
            announcement_end=AUTHORIZED_ANNOUNCEMENT_END,
            first_2025_trading_day=AUTHORIZED_FIRST_2025_TRADING_DAY,
            last_complete_label_entry_date=AUTHORIZED_LAST_COMPLETE_LABEL_ENTRY,
            label_hard_end=AUTHORIZED_LABEL_HARD_END,
            event_source_coverage_start=AUTHORIZED_SOURCE_COVERAGE_START,
            event_source_coverage_end=AUTHORIZED_SOURCE_COVERAGE_END,
        ),
        primary_endpoint=PRIMARY_OOS_ENDPOINT,
        nominated_candidates=list(AUTHORIZED_CANDIDATES),
        nominated_hypothesis_ids=[item.hypothesis_id for item in AUTHORIZED_CANDIDATES],
        evaluation_gates=AuthorizedEvaluationGates(),
        output_dir=AUTHORIZED_OUTPUT_DIR,
        consumption_receipt_path=AUTHORIZED_RECEIPT_PATH,
    )
    return seal_authorization(contract)


def seal_authorization(
    contract: EventCandidateOosOneShotAuthorization,
) -> EventCandidateOosOneShotAuthorization:
    return contract.model_copy(update={"authorization_id": _authorization_id(contract)})


def write_event_candidate_oos_authorization(
    path: Path,
    contract: EventCandidateOosOneShotAuthorization,
) -> EventCandidateOosOneShotAuthorization:
    sealed = (
        contract
        if contract.authorization_id == _authorization_id(contract)
        else seal_authorization(contract)
    )
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


def load_verified_event_candidate_oos_authorization(
    path: Path,
) -> EventCandidateOosOneShotAuthorization:
    auth_path = Path(path)
    try:
        contract = EventCandidateOosOneShotAuthorization.model_validate_json(
            auth_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError(
            "event candidate OOS one-shot authorization is missing or invalid"
        ) from exc
    assert_authorization_self_consistent(contract)
    return contract


def load_verified_committed_event_candidate_oos_authorization(
    path: Path,
) -> EventCandidateOosOneShotAuthorization:
    contract = load_verified_event_candidate_oos_authorization(path)
    assert_committed_authorization_bindings(contract)
    return contract


def verify_authorization_against_freeze(
    authorization: EventCandidateOosOneShotAuthorization,
    *,
    freeze_path: Path,
) -> None:
    freeze = load_verified_event_candidate_oos_freeze(freeze_path)
    if freeze.freeze_id != authorization.freeze_id:
        raise ValueError("authorization freeze_id does not match the verified freeze contract")
    if freeze.nominated_hypothesis_ids != authorization.nominated_hypothesis_ids:
        raise ValueError("authorization nominated candidates do not match the freeze")
    by_id = {item.hypothesis_id: item for item in freeze.hypotheses}
    for expected in authorization.nominated_candidates:
        frozen = by_id.get(expected.hypothesis_id)
        if frozen is None or frozen != expected:
            raise ValueError(
                f"authorization candidate {expected.hypothesis_id} does not match freeze semantics"
            )
    if freeze.primary_oos_endpoint != authorization.primary_endpoint:
        raise ValueError("authorization primary endpoint does not match the freeze")
    if freeze.bound_diagnostic.strategy_config_hash != authorization.strategy_config_hash:
        raise ValueError("authorization strategy_config_hash does not match the freeze")
    if freeze.bound_diagnostic.benchmark_symbol != authorization.benchmark_symbol:
        raise ValueError("authorization benchmark does not match the freeze")


def assert_authorization_self_consistent(
    contract: EventCandidateOosOneShotAuthorization,
) -> None:
    if contract.authorization_id is None or contract.authorization_id != _authorization_id(contract):
        raise ValueError("event candidate OOS authorization ID does not match its content")
    if contract.one_shot is not True or contract.consumed is not False:
        raise ValueError("authorization one-shot/consumed flags are invalid")
    if (
        contract.ready_for_scoring
        or contract.ready_for_trading
        or contract.auto_deploy
        or not contract.human_review_required
    ):
        raise ValueError("authorization violates research boundaries")
    _assert_relative_path(contract.freeze_file)
    _assert_relative_path(contract.market_dir)
    _assert_relative_path(contract.event_dir)
    _assert_relative_path(contract.output_dir)
    _assert_relative_path(contract.consumption_receipt_path)
    if contract.base_market_snapshot_id != contract.market_snapshot_id:
        raise ValueError("authorization base_market_snapshot_id must equal market_snapshot_id")
    windows = contract.windows
    if windows.announcement_end < windows.announcement_start:
        raise ValueError("authorization announcement window is inverted")
    if windows.last_complete_label_entry_date < windows.announcement_end:
        raise ValueError("last complete label entry precedes announcement window end")
    if windows.label_hard_end < windows.last_complete_label_entry_date:
        raise ValueError("label_hard_end precedes last complete label entry")
    if windows.event_source_coverage_end < windows.event_source_coverage_start:
        raise ValueError("authorization event source coverage window is inverted")
    if contract.benchmark_symbol != PRIMARY_BENCHMARK_SYMBOL:
        raise ValueError("authorization benchmark is not 000300.SH")
    if contract.primary_endpoint != PRIMARY_OOS_ENDPOINT:
        raise ValueError("authorization primary endpoint does not match the freeze protocol")
    if not contract.nominated_candidates:
        raise ValueError("authorization nominated candidates cannot be empty")
    if contract.nominated_hypothesis_ids != [
        item.hypothesis_id for item in contract.nominated_candidates
    ]:
        raise ValueError("authorization nominated_hypothesis_ids do not match candidates")
    if len(set(contract.nominated_hypothesis_ids)) != len(contract.nominated_hypothesis_ids):
        raise ValueError("authorization nominated candidates contain duplicates")
    if contract.candidate_multiplicity != len(contract.nominated_candidates):
        raise ValueError("authorization candidate_multiplicity is invalid")
    if any(item.signal_kind != "binary_bucket" for item in contract.nominated_candidates):
        raise ValueError("authorization one-shot OOS only allows binary nominated candidates")
    if contract.evaluation_gates != AuthorizedEvaluationGates():
        raise ValueError("authorization evaluation gates do not match the sealed protocol")


def assert_committed_authorization_bindings(
    contract: EventCandidateOosOneShotAuthorization,
) -> None:
    if contract.authorization_date != date(2026, 8, 25):
        raise ValueError("authorization_date is not the sealed user authorization date")
    if contract.freeze_id != AUTHORIZED_FREEZE_ID:
        raise ValueError("authorization freeze_id is not the sealed development freeze")
    if contract.freeze_file != str(DEFAULT_EVENT_CANDIDATE_OOS_FREEZE_PATH):
        raise ValueError("authorization freeze_file is not the sealed freeze path")
    if contract.market_snapshot_id != AUTHORIZED_MARKET_SNAPSHOT_ID:
        raise ValueError("authorization market_snapshot_id is not the sealed OOS market")
    if contract.event_snapshot_id != AUTHORIZED_EVENT_SNAPSHOT_ID:
        raise ValueError("authorization event_snapshot_id is not the sealed OOS event overlay")
    if contract.market_dir != AUTHORIZED_MARKET_DIR:
        raise ValueError("authorization market_dir is not the sealed OOS market directory")
    if contract.event_dir != AUTHORIZED_EVENT_DIR:
        raise ValueError("authorization event_dir is not the sealed OOS event directory")
    if contract.output_dir != AUTHORIZED_OUTPUT_DIR:
        raise ValueError("authorization output_dir is not the sealed one-shot directory")
    if contract.consumption_receipt_path != AUTHORIZED_RECEIPT_PATH:
        raise ValueError("authorization consumption_receipt_path is invalid")
    windows = contract.windows
    if (
        windows.announcement_start != AUTHORIZED_ANNOUNCEMENT_START
        or windows.announcement_end != AUTHORIZED_ANNOUNCEMENT_END
        or windows.first_2025_trading_day != AUTHORIZED_FIRST_2025_TRADING_DAY
        or windows.last_complete_label_entry_date != AUTHORIZED_LAST_COMPLETE_LABEL_ENTRY
        or windows.label_hard_end != AUTHORIZED_LABEL_HARD_END
        or windows.event_source_coverage_start != AUTHORIZED_SOURCE_COVERAGE_START
        or windows.event_source_coverage_end != AUTHORIZED_SOURCE_COVERAGE_END
    ):
        raise ValueError("authorization windows do not match the sealed OOS protocol")
    if list(contract.nominated_candidates) != list(AUTHORIZED_CANDIDATES):
        raise ValueError("authorization nominated candidates do not match the sealed list")
    if contract.strategy_config_id != AUTHORIZED_STRATEGY_CONFIG_ID:
        raise ValueError("authorization strategy_config_id is invalid")
    if contract.strategy_config_hash != AUTHORIZED_STRATEGY_CONFIG_HASH:
        raise ValueError("authorization strategy_config_hash is invalid")
    if contract.candidate_multiplicity != 2:
        raise ValueError("authorization candidate_multiplicity must be 2")


def assert_authorization_paths_unused(
    authorization: EventCandidateOosOneShotAuthorization,
    *,
    root: Path | None = None,
) -> None:
    base = Path(root) if root is not None else Path()
    output = base / authorization.output_dir
    receipt = base / authorization.consumption_receipt_path
    if output.exists():
        raise ValueError(
            "one-shot OOS evaluation output already exists and is immutable; refuse replay"
        )
    if receipt.exists():
        raise ValueError(
            "one-shot OOS evaluation consumption receipt already exists; refuse replay"
        )


def _assert_relative_path(value: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError(f"path must be relative without parent traversal: {value}")


def _authorization_id(contract: EventCandidateOosOneShotAuthorization) -> str:
    payload = contract.model_dump(mode="json", exclude={"authorization_id"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
