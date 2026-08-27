"""Sealed layer-one index data evidence over verified local artifacts.

This is a downstream evidence receipt.  It deliberately does not rewrite the
immutable identity/source probe or the earlier decision protocol.  Instead it
binds the exact identities, raw collection, offline materialized snapshot and
historical stamp-tax schedule that now exist on disk.

The receipt authorizes historical layer-one evaluation only.  It never enables
stock scoring, orders, live trading or automatic deployment.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.providers.csi_all_share_long_history import (
    verify_csi_all_share_long_history_snapshot,
)
from app.research.a_share_stamp_tax_schedule import (
    verify_a_share_stamp_tax_schedule_file,
)
from app.research.csi_all_share_index_identity import (
    PRICE_TS_CODE,
    TOTAL_RETURN_TS_CODE,
    verify_contract_file,
)
from app.research.repo_file_safety import resolve_repo_regular_file

LAYER_ONE_INDEX_DATA_EVIDENCE_SCHEMA_VERSION: Literal["1"] = "1"
LAYER_ONE_INDEX_DATA_EVIDENCE_VERSION: Literal["layer-one-index-data-evidence-v1"] = (
    "layer-one-index-data-evidence-v1"
)
LAYER_ONE_INDEX_DATA_EVIDENCE_CONFIRMATION_AS_OF = date(2026, 8, 27)

DEFAULT_IDENTITY_CONTRACT_PATH = Path("config/research/csi-all-share-index-identity-v1.json")
DEFAULT_RAW_COLLECTION_DIR = Path("data/raw/csi-all-share-index-2005-2024-v1")
DEFAULT_SNAPSHOT_DIR = Path("data/research/csi-all-share-index-2005-2024-v1")
DEFAULT_STAMP_TAX_CONTRACT_PATH = Path("config/research/a-share-stamp-tax-schedule-v1.json")
DEFAULT_EVIDENCE_PATH = Path("config/research/layer-one-index-data-evidence-v1.json")

_EXPECTED_START = date(2005, 1, 4)
_EXPECTED_END = date(2024, 12, 31)
_EXPECTED_ROWS = 4858
_EXPECTED_OVERRIDE_DATES = [date(2011, 8, 2), date(2011, 8, 3)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class IndexSeriesBinding(_StrictModel):
    role: Literal["market_risk_state", "performance_comparison"]
    symbol: str = Field(min_length=1)
    return_definition: Literal["price_index", "total_return"]
    table: Literal["price_index.parquet", "total_return_index.parquet"]
    row_count: int = Field(gt=0)
    coverage_start: date
    coverage_end: date
    table_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exact_identity_verified: Literal[True] = True


class LayerOneIndexDataEvidence(_StrictModel):
    schema_version: Literal["1"] = LAYER_ONE_INDEX_DATA_EVIDENCE_SCHEMA_VERSION
    evidence_version: Literal["layer-one-index-data-evidence-v1"] = (
        LAYER_ONE_INDEX_DATA_EVIDENCE_VERSION
    )
    evidence_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmation_as_of: date = LAYER_ONE_INDEX_DATA_EVIDENCE_CONFIRMATION_AS_OF
    identity_contract: ArtifactBinding
    raw_collection_manifest: ArtifactBinding
    snapshot_manifest: ArtifactBinding
    stamp_tax_contract: ArtifactBinding
    risk_state_index: IndexSeriesBinding
    performance_benchmark: IndexSeriesBinding
    calendar_table_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    calendar_rows: int = Field(gt=0)
    official_override_dates: list[date]
    availability_policy: Literal[
        "trade-date 15:00 Asia/Shanghai encoded as 07:00Z; T+1 action only"
    ]
    no_interpolation_or_forward_fill: Literal[True] = True
    snapshot_full_raw_recomputation_verified: Literal[True] = True
    stamp_tax_schedule_verified: Literal[True] = True
    ready_for_layer_one_historical_evaluation: Literal[True] = True
    ready_for_stock_scoring: Literal[False] = False
    ready_for_orders: Literal[False] = False
    ready_for_trading: Literal[False] = False
    auto_apply: Literal[False] = False
    does_not_use_consumed_oos: Literal[True] = True

    @model_validator(mode="after")
    def _freeze_semantics(self) -> LayerOneIndexDataEvidence:
        if self.confirmation_as_of != LAYER_ONE_INDEX_DATA_EVIDENCE_CONFIRMATION_AS_OF:
            raise ValueError("confirmation_as_of must remain 2026-08-27")
        if self.risk_state_index.role != "market_risk_state":
            raise ValueError("risk_state_index role mismatch")
        if self.risk_state_index.symbol != PRICE_TS_CODE:
            raise ValueError("risk_state_index symbol must be the verified CSI All-Share price index")
        if self.risk_state_index.return_definition != "price_index":
            raise ValueError("risk_state_index return_definition mismatch")
        if self.performance_benchmark.role != "performance_comparison":
            raise ValueError("performance_benchmark role mismatch")
        if self.performance_benchmark.symbol != TOTAL_RETURN_TS_CODE:
            raise ValueError("performance_benchmark symbol must be the verified CSI All-Share total return index")
        if self.performance_benchmark.return_definition != "total_return":
            raise ValueError("performance_benchmark return_definition mismatch")
        for series in (self.risk_state_index, self.performance_benchmark):
            if series.coverage_start != _EXPECTED_START or series.coverage_end != _EXPECTED_END:
                raise ValueError("index evidence coverage must remain 2005-01-04..2024-12-31")
            if series.row_count != _EXPECTED_ROWS:
                raise ValueError("index evidence row count must remain 4858")
        if self.calendar_rows != _EXPECTED_ROWS:
            raise ValueError("calendar_rows must remain 4858")
        if self.official_override_dates != _EXPECTED_OVERRIDE_DATES:
            raise ValueError("official_override_dates must remain the two sealed repair dates")
        if (
            self.ready_for_stock_scoring
            or self.ready_for_orders
            or self.ready_for_trading
            or self.auto_apply
        ):
            raise ValueError("index data evidence cannot authorize scoring, orders, trading or auto-apply")
        return self


def _canonical_payload(evidence: LayerOneIndexDataEvidence) -> dict[str, Any]:
    return evidence.model_dump(mode="json", exclude={"evidence_id"})


def compute_evidence_id(evidence: LayerOneIndexDataEvidence) -> str:
    payload = json.dumps(
        _canonical_payload(evidence),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def seal_layer_one_index_data_evidence(
    evidence: LayerOneIndexDataEvidence,
) -> LayerOneIndexDataEvidence:
    return evidence.model_copy(update={"evidence_id": compute_evidence_id(evidence)})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_regular_file(repo_root: Path, path: Path, field_name: str) -> tuple[Path, str]:
    resolved = resolve_repo_regular_file(path, repo_root=repo_root, field_name=field_name)
    return resolved, resolved.relative_to(Path(repo_root).resolve(strict=True)).as_posix()


def build_layer_one_index_data_evidence(
    *,
    repo_root: Path,
    identity_contract_path: Path = DEFAULT_IDENTITY_CONTRACT_PATH,
    raw_collection_dir: Path = DEFAULT_RAW_COLLECTION_DIR,
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
    stamp_tax_contract_path: Path = DEFAULT_STAMP_TAX_CONTRACT_PATH,
) -> LayerOneIndexDataEvidence:
    root = Path(repo_root).resolve(strict=True)
    identity_path, identity_relative = _relative_regular_file(
        root, identity_contract_path, "identity_contract_path"
    )
    identity, identity_result = verify_contract_file(
        repo_root=root,
        contract_path=identity_path,
    )
    if not identity_result.disk_binding_ok:
        raise ValueError("CSI All-Share identity contract disk binding failed")

    snapshot = verify_csi_all_share_long_history_snapshot(
        repo_root=root,
        staging_dir=raw_collection_dir,
        snapshot_dir=snapshot_dir,
        identity_contract_path=identity_path,
    )
    snapshot_manifest_path, snapshot_manifest_relative = _relative_regular_file(
        root, snapshot.snapshot_dir / "manifest.json", "snapshot_manifest_path"
    )
    snapshot_manifest = json.loads(snapshot_manifest_path.read_text(encoding="utf-8"))

    raw_manifest_path, raw_manifest_relative = _relative_regular_file(
        root, Path(raw_collection_dir) / "collection_manifest.json", "raw_collection_manifest_path"
    )
    raw_manifest = json.loads(raw_manifest_path.read_text(encoding="utf-8"))

    stamp_path, stamp_relative = _relative_regular_file(
        root, stamp_tax_contract_path, "stamp_tax_contract_path"
    )
    stamp_contract, stamp_verification = verify_a_share_stamp_tax_schedule_file(
        contract_path=stamp_path,
        repo_root=root,
    )
    if not stamp_verification.disk_binding_ok:
        raise ValueError("stamp-tax schedule disk binding failed")

    table_hashes = snapshot_manifest.get("table_hashes")
    if not isinstance(table_hashes, dict):
        raise ValueError("snapshot manifest table_hashes missing")
    coverage = snapshot_manifest.get("coverage")
    if coverage != {"start": _EXPECTED_START.isoformat(), "end": _EXPECTED_END.isoformat()}:
        raise ValueError("snapshot coverage does not match frozen long-history window")

    evidence = LayerOneIndexDataEvidence(
        identity_contract=ArtifactBinding(
            path=identity_relative,
            sha256=_sha256_file(identity_path),
            artifact_id=str(identity.contract_id),
        ),
        raw_collection_manifest=ArtifactBinding(
            path=raw_manifest_relative,
            sha256=_sha256_file(raw_manifest_path),
            artifact_id=str(raw_manifest["collection_id"]),
        ),
        snapshot_manifest=ArtifactBinding(
            path=snapshot_manifest_relative,
            sha256=_sha256_file(snapshot_manifest_path),
            artifact_id=snapshot.snapshot_id,
        ),
        stamp_tax_contract=ArtifactBinding(
            path=stamp_relative,
            sha256=_sha256_file(stamp_path),
            artifact_id=str(stamp_contract.contract_id),
        ),
        risk_state_index=IndexSeriesBinding(
            role="market_risk_state",
            symbol=PRICE_TS_CODE,
            return_definition="price_index",
            table="price_index.parquet",
            row_count=snapshot.price_rows,
            coverage_start=_EXPECTED_START,
            coverage_end=_EXPECTED_END,
            table_sha256=str(table_hashes["price_index.parquet"]),
        ),
        performance_benchmark=IndexSeriesBinding(
            role="performance_comparison",
            symbol=TOTAL_RETURN_TS_CODE,
            return_definition="total_return",
            table="total_return_index.parquet",
            row_count=snapshot.total_return_rows,
            coverage_start=_EXPECTED_START,
            coverage_end=_EXPECTED_END,
            table_sha256=str(table_hashes["total_return_index.parquet"]),
        ),
        calendar_table_sha256=str(table_hashes["calendar.parquet"]),
        calendar_rows=snapshot.calendar_rows,
        official_override_dates=[date.fromisoformat(value) for value in snapshot_manifest["official_override_dates"]],
        availability_policy=snapshot_manifest["availability_policy"],
    )
    return seal_layer_one_index_data_evidence(evidence)


def verify_layer_one_index_data_evidence(
    evidence: LayerOneIndexDataEvidence,
    *,
    repo_root: Path,
) -> LayerOneIndexDataEvidence:
    if evidence.evidence_id is None:
        raise ValueError("layer-one index data evidence_id is missing")
    if evidence.evidence_id != compute_evidence_id(evidence):
        raise ValueError("layer-one index data evidence_id does not match canonical content")
    expected = build_layer_one_index_data_evidence(
        repo_root=repo_root,
        identity_contract_path=Path(evidence.identity_contract.path),
        raw_collection_dir=Path(evidence.raw_collection_manifest.path).parent,
        snapshot_dir=Path(evidence.snapshot_manifest.path).parent,
        stamp_tax_contract_path=Path(evidence.stamp_tax_contract.path),
    )
    if evidence.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise ValueError("layer-one index data evidence does not match verified disk artifacts")
    return evidence


def load_layer_one_index_data_evidence(path: Path) -> LayerOneIndexDataEvidence:
    try:
        return LayerOneIndexDataEvidence.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("layer-one index data evidence file is missing or invalid") from exc


def verify_layer_one_index_data_evidence_file(
    *,
    evidence_path: Path,
    repo_root: Path,
) -> LayerOneIndexDataEvidence:
    resolved = resolve_repo_regular_file(
        evidence_path,
        repo_root=repo_root,
        field_name="evidence_path",
    )
    return verify_layer_one_index_data_evidence(
        load_layer_one_index_data_evidence(resolved),
        repo_root=repo_root,
    )


def write_layer_one_index_data_evidence(
    evidence: LayerOneIndexDataEvidence,
    output: Path,
) -> None:
    if output.exists():
        raise FileExistsError("layer-one index data evidence output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    sealed = seal_layer_one_index_data_evidence(evidence)
    output.write_text(
        json.dumps(sealed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "DEFAULT_EVIDENCE_PATH",
    "DEFAULT_IDENTITY_CONTRACT_PATH",
    "DEFAULT_RAW_COLLECTION_DIR",
    "DEFAULT_SNAPSHOT_DIR",
    "DEFAULT_STAMP_TAX_CONTRACT_PATH",
    "LAYER_ONE_INDEX_DATA_EVIDENCE_VERSION",
    "LayerOneIndexDataEvidence",
    "build_layer_one_index_data_evidence",
    "compute_evidence_id",
    "load_layer_one_index_data_evidence",
    "seal_layer_one_index_data_evidence",
    "verify_layer_one_index_data_evidence",
    "verify_layer_one_index_data_evidence_file",
    "write_layer_one_index_data_evidence",
]
