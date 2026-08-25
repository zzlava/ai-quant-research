from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.research.portfolio_oos_freeze import (
    COST_STRESS_GATES,
    DEFAULT_PORTFOLIO_OOS_FREEZE_PATH,
    DESCRIPTIVE_ENDPOINTS,
    EVALUABILITY_GATES,
    FROZEN_CONFIG_HASH,
    FROZEN_FIRST_2025_PLUS_SIGNAL,
    FROZEN_LAST_COMPLETE_SIGNAL,
    FROZEN_LAST_SCHEDULED_EXIT,
    FROZEN_OOS_FUNDAMENTAL_DIR,
    FROZEN_OOS_FUNDAMENTAL_SNAPSHOT_ID,
    FROZEN_OOS_MARKET_DIR,
    FROZEN_OOS_MARKET_SNAPSHOT_ID,
    FROZEN_RUNTIME_EQUIVALENT_ANCHOR,
    FROZEN_SIGNAL_CUTOFF,
    FROZEN_STRATEGY_CONFIG_ID,
    FROZEN_STRATEGY_FILE_SHA256,
    FROZEN_STRATEGY_PATH,
    HARD_RISK_GATES,
    PRIMARY_OOS_ENDPOINT,
    RESULT_SEMANTICS,
    CostStressGates,
    DescriptiveEndpoint,
    EvaluabilityGates,
    HardRiskGates,
    PrimaryOosEndpoint,
    ResultSemantics,
    load_verified_portfolio_oos_freeze,
)

PORTFOLIO_OOS_AUTH_SCHEMA_VERSION: Literal["1"] = "1"
PORTFOLIO_OOS_AUTH_VERSION: Literal["all-a-share-portfolio-oos-one-shot-v1"] = "all-a-share-portfolio-oos-one-shot-v1"
DEFAULT_PORTFOLIO_OOS_AUTH_PATH = Path("config/research/all-a-share-portfolio-oos-one-shot-authorization-v1.json")

AUTHORIZED_FREEZE_ID = "e5cdb0ff04e5eb78c331d6e4af77d4f8932a683e3f1558f83945708d48d00cc0"
AUTHORIZED_USER_PHRASE = "我授权按照冻结协议执行 p10_h20 的 2025+ 一次性 OOS 评估"
AUTHORIZED_AUTHORIZATION_DATE = date(2026, 8, 25)
AUTHORIZED_RUNTIME_CONFIG_HASH = "b06e86cac8041f84"
AUTHORIZED_COMPOSITE_STORE_SNAPSHOT_ID = "558ca159bba802dcb0c5746c1c7910c4ab1521a3153580469c31ae753d6151a1"
AUTHORIZED_EVALUATION_START = date(2025, 1, 2)
AUTHORIZED_EVALUATION_END = date(2026, 8, 21)
AUTHORIZED_OUTPUT_DIR = "data/all-a-share-oos-20241001-20260821-v1/portfolio-oos-evaluations/one-shot-v1"
AUTHORIZED_RECEIPT_PATH = (
    "data/all-a-share-oos-20241001-20260821-v1/portfolio-oos-evaluations/one-shot-v1.consumption-receipt.json"
)
RESEARCH_BOUNDARY = (
    "Authorized one-shot 2025+ portfolio OOS evaluation of frozen p10_h20 only. "
    "Runtime may override signal_anchor_date to the calendar-equivalent 2024-10-29 and "
    "nothing else. No p-value, IC, parameter search, auto scoring, paper trading, live "
    "trading, or automatic promotion."
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthorizedEvaluationWindow(_StrictModel):
    evaluation_start: date
    evaluation_end: date
    first_2025_plus_signal: date
    signal_cutoff: date
    last_scheduled_exit: date


class AuthorizedRuntimeOverride(_StrictModel):
    field: Literal["trade.signal_anchor_date"] = "trade.signal_anchor_date"
    frozen_value: date
    runtime_value: date
    expected_runtime_config_hash: str = Field(min_length=1)
    note: str = (
        "runtime_equivalent_anchor authorizes schedule equivalence only; every other "
        "frozen strategy parameter must remain unchanged"
    )


class PortfolioOosOneShotAuthorization(_StrictModel):
    schema_version: Literal["1"] = PORTFOLIO_OOS_AUTH_SCHEMA_VERSION
    authorization_version: Literal["all-a-share-portfolio-oos-one-shot-v1"] = PORTFOLIO_OOS_AUTH_VERSION
    authorization_date: date
    authorized: Literal[True] = True
    one_shot: Literal[True] = True
    consumed: Literal[False] = False
    user_authorization_phrase: str = Field(min_length=1)
    authorization_basis: str = Field(min_length=1)
    freeze_file: str = Field(min_length=1)
    freeze_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_path: str = Field(min_length=1)
    strategy_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_config_id: str = Field(min_length=1)
    frozen_config_hash: str = Field(min_length=1)
    runtime_override: AuthorizedRuntimeOverride
    market_dir: str = Field(min_length=1)
    market_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    fundamental_dir: str = Field(min_length=1)
    fundamental_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    fundamental_base_market_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_composite_store_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_window: AuthorizedEvaluationWindow
    primary_oos_endpoint: PrimaryOosEndpoint
    evaluability_gates: EvaluabilityGates
    hard_risk_gates: HardRiskGates
    cost_stress_gates: CostStressGates
    descriptive_endpoints: list[DescriptiveEndpoint]
    result_semantics: ResultSemantics
    output_dir: str = Field(min_length=1)
    consumption_receipt_path: str = Field(min_length=1)
    ready_for_scoring: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_deploy: Literal[False] = False
    human_review_required: Literal[True] = True
    authorization_id: str | None = None
    research_boundary: str = RESEARCH_BOUNDARY


def build_committed_portfolio_oos_authorization() -> PortfolioOosOneShotAuthorization:
    """Build the sealed committed authorization for the first p10_h20 2025+ one-shot OOS."""
    contract = PortfolioOosOneShotAuthorization(
        authorization_date=AUTHORIZED_AUTHORIZATION_DATE,
        user_authorization_phrase=AUTHORIZED_USER_PHRASE,
        authorization_basis=(
            f"Explicit user authorization on {AUTHORIZED_AUTHORIZATION_DATE.isoformat()}: {AUTHORIZED_USER_PHRASE}"
        ),
        freeze_file=str(DEFAULT_PORTFOLIO_OOS_FREEZE_PATH),
        freeze_id=AUTHORIZED_FREEZE_ID,
        strategy_path=FROZEN_STRATEGY_PATH,
        strategy_file_sha256=FROZEN_STRATEGY_FILE_SHA256,
        strategy_config_id=FROZEN_STRATEGY_CONFIG_ID,
        frozen_config_hash=FROZEN_CONFIG_HASH,
        runtime_override=AuthorizedRuntimeOverride(
            frozen_value=date(2022, 1, 4),
            runtime_value=FROZEN_RUNTIME_EQUIVALENT_ANCHOR,
            expected_runtime_config_hash=AUTHORIZED_RUNTIME_CONFIG_HASH,
        ),
        market_dir=FROZEN_OOS_MARKET_DIR,
        market_snapshot_id=FROZEN_OOS_MARKET_SNAPSHOT_ID,
        fundamental_dir=FROZEN_OOS_FUNDAMENTAL_DIR,
        fundamental_snapshot_id=FROZEN_OOS_FUNDAMENTAL_SNAPSHOT_ID,
        fundamental_base_market_snapshot_id=FROZEN_OOS_MARKET_SNAPSHOT_ID,
        expected_composite_store_snapshot_id=AUTHORIZED_COMPOSITE_STORE_SNAPSHOT_ID,
        evaluation_window=AuthorizedEvaluationWindow(
            evaluation_start=AUTHORIZED_EVALUATION_START,
            evaluation_end=AUTHORIZED_EVALUATION_END,
            first_2025_plus_signal=FROZEN_FIRST_2025_PLUS_SIGNAL,
            signal_cutoff=FROZEN_SIGNAL_CUTOFF,
            last_scheduled_exit=FROZEN_LAST_SCHEDULED_EXIT,
        ),
        primary_oos_endpoint=PRIMARY_OOS_ENDPOINT,
        evaluability_gates=EVALUABILITY_GATES,
        hard_risk_gates=HARD_RISK_GATES,
        cost_stress_gates=COST_STRESS_GATES,
        descriptive_endpoints=list(DESCRIPTIVE_ENDPOINTS),
        result_semantics=RESULT_SEMANTICS,
        output_dir=AUTHORIZED_OUTPUT_DIR,
        consumption_receipt_path=AUTHORIZED_RECEIPT_PATH,
    )
    return seal_authorization(contract)


def seal_authorization(
    contract: PortfolioOosOneShotAuthorization,
) -> PortfolioOosOneShotAuthorization:
    return contract.model_copy(update={"authorization_id": _authorization_id(contract)})


def write_portfolio_oos_authorization(
    path: Path,
    contract: PortfolioOosOneShotAuthorization,
) -> PortfolioOosOneShotAuthorization:
    sealed = contract if contract.authorization_id == _authorization_id(contract) else seal_authorization(contract)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


def load_verified_portfolio_oos_authorization(
    path: Path,
) -> PortfolioOosOneShotAuthorization:
    auth_path = Path(path)
    try:
        contract = PortfolioOosOneShotAuthorization.model_validate_json(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("portfolio OOS one-shot authorization is missing or invalid") from exc
    assert_authorization_self_consistent(contract)
    return contract


def load_verified_committed_portfolio_oos_authorization(
    path: Path,
) -> PortfolioOosOneShotAuthorization:
    contract = load_verified_portfolio_oos_authorization(path)
    assert_committed_authorization_bindings(contract)
    return contract


def verify_authorization_against_freeze(
    authorization: PortfolioOosOneShotAuthorization,
    *,
    freeze_path: Path,
) -> None:
    freeze = load_verified_portfolio_oos_freeze(freeze_path)
    if freeze.freeze_id != authorization.freeze_id:
        raise ValueError("authorization freeze_id does not match the verified freeze contract")
    if freeze.bound_strategy.strategy_path != authorization.strategy_path:
        raise ValueError("authorization strategy_path does not match the freeze")
    if freeze.bound_strategy.strategy_file_sha256 != authorization.strategy_file_sha256:
        raise ValueError("authorization strategy_file_sha256 does not match the freeze")
    if freeze.bound_strategy.strategy_config_id != authorization.strategy_config_id:
        raise ValueError("authorization strategy_config_id does not match the freeze")
    if freeze.bound_strategy.config_hash != authorization.frozen_config_hash:
        raise ValueError("authorization frozen_config_hash does not match the freeze")
    if freeze.bound_oos_data.market_dir != authorization.market_dir:
        raise ValueError("authorization market_dir does not match the freeze")
    if freeze.bound_oos_data.market_snapshot_id != authorization.market_snapshot_id:
        raise ValueError("authorization market_snapshot_id does not match the freeze")
    if freeze.bound_oos_data.fundamental_dir != authorization.fundamental_dir:
        raise ValueError("authorization fundamental_dir does not match the freeze")
    if freeze.bound_oos_data.fundamental_snapshot_id != authorization.fundamental_snapshot_id:
        raise ValueError("authorization fundamental_snapshot_id does not match the freeze")
    if freeze.bound_oos_data.fundamental_base_market_snapshot_id != authorization.fundamental_base_market_snapshot_id:
        raise ValueError("authorization fundamental base market binding does not match the freeze")
    window = authorization.evaluation_window
    if (
        freeze.evaluation_window.evaluation_start != window.evaluation_start
        or freeze.evaluation_window.evaluation_end != window.evaluation_end
        or freeze.evaluation_window.signal_cutoff != window.signal_cutoff
        or freeze.evaluation_window.last_scheduled_exit != window.last_scheduled_exit
        or freeze.calendar_equivalence.first_2025_plus_signal != window.first_2025_plus_signal
        or freeze.calendar_equivalence.runtime_equivalent_anchor != authorization.runtime_override.runtime_value
        or freeze.calendar_equivalence.last_complete_signal != FROZEN_LAST_COMPLETE_SIGNAL
    ):
        raise ValueError("authorization evaluation window does not match the freeze")
    if freeze.primary_oos_endpoint != authorization.primary_oos_endpoint:
        raise ValueError("authorization primary endpoint does not match the freeze")
    if freeze.evaluability_gates != authorization.evaluability_gates:
        raise ValueError("authorization evaluability gates do not match the freeze")
    if freeze.hard_risk_gates != authorization.hard_risk_gates:
        raise ValueError("authorization hard risk gates do not match the freeze")
    if freeze.cost_stress_gates != authorization.cost_stress_gates:
        raise ValueError("authorization cost stress gates do not match the freeze")
    if freeze.descriptive_endpoints != authorization.descriptive_endpoints:
        raise ValueError("authorization descriptive endpoints do not match the freeze")
    if freeze.result_semantics != authorization.result_semantics:
        raise ValueError("authorization result semantics do not match the freeze")


def assert_authorization_self_consistent(
    contract: PortfolioOosOneShotAuthorization,
) -> None:
    if contract.authorization_id is None or contract.authorization_id != _authorization_id(contract):
        raise ValueError("portfolio OOS authorization ID does not match its content")
    if contract.authorized is not True or contract.one_shot is not True or contract.consumed is not False:
        raise ValueError("authorization authorized/one-shot/consumed flags are invalid")
    if (
        contract.ready_for_scoring
        or contract.ready_for_trading
        or contract.auto_deploy
        or not contract.human_review_required
    ):
        raise ValueError("authorization violates research boundaries")
    for value in (
        contract.freeze_file,
        contract.strategy_path,
        contract.market_dir,
        contract.fundamental_dir,
        contract.output_dir,
        contract.consumption_receipt_path,
    ):
        _assert_relative_path(value)
    if contract.fundamental_base_market_snapshot_id != contract.market_snapshot_id:
        raise ValueError("authorization fundamental_base_market_snapshot_id must equal market_snapshot_id")
    window = contract.evaluation_window
    if window.evaluation_end < window.evaluation_start:
        raise ValueError("authorization evaluation window is inverted")
    if window.signal_cutoff < window.evaluation_start or window.signal_cutoff > window.evaluation_end:
        raise ValueError("authorization signal_cutoff is outside the evaluation window")
    if window.first_2025_plus_signal < window.evaluation_start or window.first_2025_plus_signal > window.signal_cutoff:
        raise ValueError("authorization first_2025_plus_signal is outside the signal window")
    if window.last_scheduled_exit < window.signal_cutoff or window.last_scheduled_exit > window.evaluation_end:
        raise ValueError("authorization last_scheduled_exit is outside the evaluation window")
    override = contract.runtime_override
    if override.field != "trade.signal_anchor_date":
        raise ValueError("authorization runtime_override.field is invalid")
    if override.runtime_value == override.frozen_value:
        raise ValueError("authorization runtime_override must change the signal anchor")
    if not override.expected_runtime_config_hash.strip():
        raise ValueError("authorization expected_runtime_config_hash is empty")
    if contract.primary_oos_endpoint != PRIMARY_OOS_ENDPOINT:
        raise ValueError("authorization primary endpoint does not match the sealed protocol")
    if contract.evaluability_gates != EVALUABILITY_GATES:
        raise ValueError("authorization evaluability gates do not match the sealed protocol")
    if contract.hard_risk_gates != HARD_RISK_GATES:
        raise ValueError("authorization hard risk gates do not match the sealed protocol")
    if contract.cost_stress_gates != COST_STRESS_GATES:
        raise ValueError("authorization cost stress gates do not match the sealed protocol")
    if contract.descriptive_endpoints != list(DESCRIPTIVE_ENDPOINTS):
        raise ValueError("authorization descriptive endpoints do not match the sealed protocol")
    if contract.result_semantics != RESULT_SEMANTICS:
        raise ValueError("authorization result semantics do not match the sealed protocol")
    if not contract.user_authorization_phrase.strip():
        raise ValueError("authorization user_authorization_phrase is empty")
    if contract.user_authorization_phrase not in contract.authorization_basis:
        raise ValueError("authorization_basis must include the user authorization phrase")


def assert_committed_authorization_bindings(
    contract: PortfolioOosOneShotAuthorization,
) -> None:
    expected = build_committed_portfolio_oos_authorization()
    if contract.authorization_id != expected.authorization_id:
        raise ValueError("authorization is not the sealed committed portfolio OOS one-shot authorization")
    if contract.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise ValueError("authorization bindings drifted from the sealed committed portfolio OOS protocol")


def assert_authorization_paths_unused(
    authorization: PortfolioOosOneShotAuthorization,
    *,
    root: Path | None = None,
) -> None:
    base = Path(root) if root is not None else Path()
    output = base / authorization.output_dir
    receipt = base / authorization.consumption_receipt_path
    if output.exists():
        raise ValueError("one-shot portfolio OOS evaluation output already exists and is immutable; refuse replay")
    if receipt.exists():
        raise ValueError("one-shot portfolio OOS evaluation consumption receipt already exists; refuse replay")


def _assert_relative_path(value: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError(f"path must be relative without parent traversal: {value}")


def _authorization_id(contract: PortfolioOosOneShotAuthorization) -> str:
    payload = contract.model_dump(mode="json", exclude={"authorization_id"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
