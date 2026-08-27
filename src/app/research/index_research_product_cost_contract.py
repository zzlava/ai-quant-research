"""Sealed index-proxy product boundary and synthetic implementation-cost envelope."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.repo_file_safety import resolve_repo_regular_file

DEFAULT_CONTRACT_PATH = Path("config/research/index-research-product-cost-contract-v1.json")
DEFAULT_EVIDENCE_DIR = Path("data/raw/index-product-cost-evidence-v1")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceSource(_StrictModel):
    url: str = Field(min_length=1)
    raw_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_role: str = Field(min_length=1)


class CostScenario(_StrictModel):
    commission_rate_per_side: float = Field(ge=0.0)
    minimum_commission_cny_per_leg: float = Field(ge=0.0)
    slippage_bps_per_side: float = Field(ge=0.0)
    equity_proxy_annual_drag: float = Field(ge=0.0)
    defensive_proxy_annual_drag: float = Field(ge=0.0)


class IndexResearchProductCostContract(_StrictModel):
    schema_version: Literal["1"]
    contract_version: Literal["index-research-product-cost-contract-v1"]
    contract_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_as_of: date
    role: Literal["index_level_historical_research_cost_envelope"]
    account_capital_cny: Literal[80000]
    research_proxies: dict[str, str]
    official_evidence: dict[str, EvidenceSource]
    exact_live_product_findings: dict[str, Any]
    official_execution_facts: dict[str, Any]
    research_cost_scenarios: dict[str, CostScenario]
    scenario_interpretation: dict[str, Any]
    implementation_semantics: dict[str, Any]
    product_policy: dict[str, Any]
    readiness: dict[str, bool]

    @model_validator(mode="after")
    def _fail_closed(self) -> IndexResearchProductCostContract:
        if self.sealed_as_of != date(2026, 8, 27):
            raise ValueError("product/cost contract date drifted")
        if self.research_proxies != {
            "equity_total_return_index": "H00985.CSI",
            "defensive_total_return_index": "H11010.CSI",
        }:
            raise ValueError("research proxy identity drifted")
        expected_sources = {
            "equity_exact_product_query",
            "defensive_exact_product_query",
            "sse_fee_schedule",
            "szse_fee_schedule",
            "szse_etf_investor_guide",
        }
        if set(self.official_evidence) != expected_sources:
            raise ValueError("official product/cost evidence set drifted")
        findings = self.exact_live_product_findings
        if findings.get("equity_exact_linked_product_count") != 0:
            raise ValueError("equity exact-product finding drifted")
        if findings.get("defensive_exact_linked_product_count") != 0:
            raise ValueError("defensive exact-product finding drifted")
        if findings.get("absence_is_not_proof_no_market_product_exists") is not True:
            raise ValueError("official product-query interpretation drifted")
        facts = self.official_execution_facts
        required_facts = {
            "exchange_traded_fund_board_lot_units": 100,
            "etf_stamp_tax_rate": 0.0,
            "sse_etf_handling_fee_rate_bilateral": 0.00004,
            "sse_bond_and_money_etf_handling_fee_temporarily_exempt": True,
            "szse_guide_commission_ceiling_rate": 0.003,
            "szse_guide_minimum_commission_cny": 5.0,
        }
        if any(facts.get(key) != value for key, value in required_facts.items()):
            raise ValueError("official ETF execution fact drifted")
        expected_scenarios = {
            "base": (0.00025, 5.0, 5.0, 0.006, 0.003),
            "stress": (0.00025, 5.0, 15.0, 0.012, 0.006),
        }
        if set(self.research_cost_scenarios) != set(expected_scenarios):
            raise ValueError("research cost scenario set drifted")
        for label, expected in expected_scenarios.items():
            item = self.research_cost_scenarios[label]
            observed = (
                item.commission_rate_per_side,
                item.minimum_commission_cny_per_leg,
                item.slippage_bps_per_side,
                item.equity_proxy_annual_drag,
                item.defensive_proxy_annual_drag,
            )
            if observed != expected:
                raise ValueError(f"{label} research cost scenario drifted")
        interpretation = self.scenario_interpretation
        if interpretation.get("annual_drags_are_synthetic_penalties_not_product_facts") is not True:
            raise ValueError("synthetic annual-drag boundary drifted")
        if interpretation.get("live_cost_claim_forbidden") is not True:
            raise ValueError("live cost claim must remain forbidden")
        semantics = self.implementation_semantics
        required_true = (
            "same_scenario_applies_to_every_static_and_dynamic_arm",
            "commission_and_slippage_apply_to_absolute_weight_turnover",
            "minimum_commission_applies_once_per_traded_leg",
            "annual_drag_accrues_daily_by_prior_weight",
            "exchange_handling_fee_is_treated_as_included_in_commission_to_avoid_double_counting",
            "fractional_index_weight_replay_only",
        )
        if any(semantics.get(key) is not True for key in required_true):
            raise ValueError("cost implementation semantics drifted")
        if semantics.get("trading_days_per_year") != 242:
            raise ValueError("cost annualization basis drifted")
        if self.product_policy.get("dynamic_product_rotation_forbidden") is not True:
            raise ValueError("dynamic product rotation must remain forbidden")
        expected_readiness = {
            "ready_for_index_level_historical_replay": True,
            "ready_for_live_product_mapping": False,
            "ready_for_portfolio_construction": False,
            "ready_for_orders": False,
            "ready_for_trading": False,
        }
        if self.readiness != expected_readiness:
            raise ValueError("product/cost readiness boundary drifted")
        return self


class BytesClient(Protocol):
    def fetch(self, url: str) -> bytes: ...


class OfficialBytesClient:
    def fetch(self, url: str) -> bytes:
        allowed = (
            "https://www.csindex.com.cn/",
            "https://www.sse.com.cn/",
            "https://www.szse.cn/",
            "https://investor.szse.cn/",
        )
        if not url.startswith(allowed):
            raise ValueError("product/cost evidence URL is outside official hosts")
        request = Request(url, headers={"User-Agent": "ai-quant-research/0.1"})
        with urlopen(request, timeout=60) as response:  # noqa: S310
            payload = response.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("official product/cost evidence exceeds size limit")
        return payload


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def compute_contract_id(contract: IndexResearchProductCostContract) -> str:
    return _json_hash(contract.model_dump(mode="json", exclude={"contract_id"}))


def verify_index_research_product_cost_contract(
    *, repo_root: Path, path: Path = DEFAULT_CONTRACT_PATH, require_evidence: bool = True
) -> IndexResearchProductCostContract:
    root = Path(repo_root).resolve(strict=True)
    resolved = resolve_repo_regular_file(path, repo_root=root, field_name="contract_path")
    try:
        contract = IndexResearchProductCostContract.model_validate_json(resolved.read_text())
    except Exception as exc:
        raise ValueError("index product/cost contract is missing or invalid") from exc
    if contract.contract_id != compute_contract_id(contract):
        raise ValueError("index product/cost contract self-hash mismatch")
    if require_evidence:
        for name, evidence in contract.official_evidence.items():
            source = resolve_repo_regular_file(
                Path(evidence.raw_path), repo_root=root, field_name=f"official_evidence.{name}.raw_path"
            )
            if _sha256_file(source) != evidence.sha256:
                raise ValueError(f"official product/cost evidence hash mismatch: {name}")
        _verify_exact_product_responses(contract=contract, repo_root=root)
    return contract


def _verify_exact_product_responses(
    *, contract: IndexResearchProductCostContract, repo_root: Path
) -> None:
    for key in ("equity_exact_product_query", "defensive_exact_product_query"):
        source = repo_root / contract.official_evidence[key].raw_path
        try:
            payload = json.loads(source.read_text())
        except Exception as exc:
            raise ValueError(f"official exact-product response is invalid: {key}") from exc
        if payload != {"code": "200", "msg": "Success", "data": [], "success": True}:
            raise ValueError(f"official exact-product response no longer proves an empty linked-product list: {key}")


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())


def materialize_official_product_cost_evidence(
    *,
    repo_root: Path,
    client: BytesClient | None = None,
    contract_path: Path = DEFAULT_CONTRACT_PATH,
) -> dict[str, Any]:
    root = Path(repo_root).resolve(strict=True)
    contract = verify_index_research_product_cost_contract(
        repo_root=root, path=contract_path, require_evidence=False
    )
    source_client = client or OfficialBytesClient()
    hashes: dict[str, str] = {}
    for name, evidence in contract.official_evidence.items():
        path = root / evidence.raw_path
        payload = path.read_bytes() if path.exists() else source_client.fetch(evidence.url)
        if _sha256_bytes(payload) != evidence.sha256:
            raise ValueError(f"official product/cost evidence bytes drifted: {name}")
        if not path.exists():
            _atomic_bytes(path, payload)
        hashes[name] = evidence.sha256
    _verify_exact_product_responses(contract=contract, repo_root=root)
    manifest_payload = {
        "schema_version": "1",
        "manifest_version": "index-product-cost-evidence-manifest-v1",
        "contract_id": contract.contract_id,
        "evidence_hashes": dict(sorted(hashes.items())),
        "exact_live_product_mapping_complete": False,
        "historical_research_costs_are_synthetic_envelopes": True,
        "ready_for_index_level_historical_replay": True,
        "ready_for_live_product_mapping": False,
        "ready_for_orders": False,
        "ready_for_trading": False,
    }
    manifest = {**manifest_payload, "manifest_id": _json_hash(manifest_payload)}
    manifest_path = root / DEFAULT_EVIDENCE_DIR / "manifest.json"
    if manifest_path.exists():
        if json.loads(manifest_path.read_text()) != manifest:
            raise ValueError("existing product/cost evidence manifest drifted")
    else:
        _atomic_json(manifest_path, manifest)
    verify_index_research_product_cost_contract(repo_root=root, path=contract_path)
    return manifest


__all__ = [
    "DEFAULT_CONTRACT_PATH",
    "DEFAULT_EVIDENCE_DIR",
    "IndexResearchProductCostContract",
    "compute_contract_id",
    "materialize_official_product_cost_evidence",
    "verify_index_research_product_cost_contract",
]
