"""Compact monthly PIT statistical-risk-cluster pack for layer-two alpha v2.

The pack contains only candidate-to-cluster assignments (or explicit unknown
reasons), never the quadratic pair table.  Correlations use exactly 120 simple
returns from 121 adjusted closes ending on each monthly anchor.  The graph is
the connected components of Pearson correlations >= 0.65.

This is a statistical risk proxy, not an industry classification or alpha
signal.  Materialization and verification are offline and never score,
backtest, construct portfolios, or trade.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.research.layer_two_alpha_v2_freeze_bundle import (
    CANDIDATE_PACK_PATH,
    DEFAULT_RUN_CONTRACT_PATH,
    LayerTwoAlphaDiagnosticRunContractV2,
    verify_bundle,
)
from app.research.layer_two_candidate_eligibility_pack import (
    verify_candidate_eligibility_pack,
)

PACK_SCHEMA_VERSION: Literal["1"] = "1"
PACK_VERSION: Literal["layer-two-statistical-cluster-pack-v2"] = (
    "layer-two-statistical-cluster-pack-v2"
)
DEFAULT_OUTPUT_DIR = Path(
    "data/all-a-share-historical-v1/research/layer-two-statistical-cluster-pack-v2"
)
MARKET_DIR = Path("data/all-a-share-historical-v1/parquet")
ASSIGNMENTS_FILE = "cluster_assignments.parquet"
MANIFEST_FILE = "manifest.json"
LOOKBACK_RETURNS: Literal[120] = 120
REQUIRED_CLOSES: Literal[121] = 121
CORRELATION_THRESHOLD = 0.65
BLOCK_SIZE = 256


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _hex64(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{field_name} must be a 64-char lowercase hex SHA-256")
    return value


class ClusterPackSourceBinding(_StrictFrozen):
    run_contract_path: str
    run_contract_id: str
    run_contract_file_sha256: str
    market_path: str
    market_snapshot_id: str
    market_manifest_sha256: str
    candidate_pack_path: str
    candidate_pack_id: str
    candidate_manifest_sha256: str
    candidate_parquet_sha256: str

    @field_validator(
        "run_contract_id",
        "run_contract_file_sha256",
        "market_snapshot_id",
        "market_manifest_sha256",
        "candidate_pack_id",
        "candidate_manifest_sha256",
        "candidate_parquet_sha256",
        mode="before",
    )
    @classmethod
    def _hashes(cls, value: object, info: Any) -> str:
        return _hex64(value, field_name=str(info.field_name))


class ClusterPackIntegrity(_StrictFrozen):
    assignments_file_sha256: str
    row_count: int = Field(ge=1)
    anchor_count: int = Field(ge=1)
    anchor_symbol_unique: Literal[True]

    @field_validator("assignments_file_sha256", mode="before")
    @classmethod
    def _hash(cls, value: object) -> str:
        return _hex64(value, field_name="assignments_file_sha256")


class ClusterPackCoverage(_StrictFrozen):
    start: Literal["2022-01-01"]
    end: Literal["2024-12-31"]
    monthly_anchor_rule: Literal["first_market_trading_day_of_calendar_month"]
    anchor_count: Literal[36]
    early_insufficient_history_remains_unknown: Literal[True]


class ClusterPackReadiness(_StrictFrozen):
    research_only: Literal[True]
    is_not_industry_classification: Literal[True]
    no_current_industry_backfill: Literal[True]
    ready_for_alpha_input_binding: Literal[True]
    ready_for_alpha_diagnostic_execution: Literal[False]
    ready_for_scoring: Literal[False]
    ready_for_backtest: Literal[False]
    ready_for_portfolio_construction: Literal[False]
    ready_for_orders: Literal[False]
    ready_for_trading: Literal[False]
    auto_apply: Literal[False]


class StatisticalClusterPackManifestV2(_StrictFrozen):
    schema_version: Literal["1"]
    pack_version: Literal["layer-two-statistical-cluster-pack-v2"]
    status: Literal["materialized_verified_input_not_executable"]
    source_binding: ClusterPackSourceBinding
    coverage: ClusterPackCoverage
    lookback_returns: Literal[120]
    required_adjusted_closes: Literal[121]
    correlation_threshold: float
    correlation_method: Literal["pearson_simple_returns_complete_window"]
    linkage: Literal["connected_components_chain"]
    assignment_population: Literal["candidate_complete_and_eligible_for_new_entry_at_anchor"]
    singleton_companion_score_remains_unknown: Literal[True]
    missing_or_nonfinite_history_remains_unknown: Literal[True]
    factor_known_filter_applied_later_per_family: Literal[True]
    integrity: ClusterPackIntegrity
    status_counts: dict[str, int]
    readiness: ClusterPackReadiness
    pack_id: str | None = Field(default=None)

    @field_validator("pack_id", mode="before")
    @classmethod
    def _pack_id(cls, value: object) -> str | None:
        return None if value is None else _hex64(value, field_name="pack_id")

    @field_validator("correlation_threshold", mode="before")
    @classmethod
    def _threshold(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("correlation_threshold must be numeric")
        result = float(value)
        if not math.isfinite(result) or result != CORRELATION_THRESHOLD:
            raise ValueError("correlation_threshold must remain exactly 0.65")
        return result

    @model_validator(mode="after")
    def _counts(self) -> StatisticalClusterPackManifestV2:
        if sum(self.status_counts.values()) != self.integrity.row_count:
            raise ValueError("status_counts must sum to row_count")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in self.status_counts.values()
        ):
            raise ValueError("status_counts must be non-negative integers")
        return self


@dataclass(frozen=True)
class _UnionFind:
    parent: list[int]
    rank: list[int]

    @classmethod
    def create(cls, count: int) -> _UnionFind:
        return cls(parent=list(range(count)), rank=[0] * count)

    def find(self, item: int) -> int:
        parent = self.parent
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left == root_right:
            return
        if self.rank[root_left] < self.rank[root_right]:
            root_left, root_right = root_right, root_left
        self.parent[root_right] = root_left
        if self.rank[root_left] == self.rank[root_right]:
            self.rank[root_left] += 1


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_manifest_sha(manifest: StatisticalClusterPackManifestV2) -> str:
    payload = manifest.model_dump(mode="json", exclude={"pack_id"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _seal_manifest(manifest: StatisticalClusterPackManifestV2) -> StatisticalClusterPackManifestV2:
    return manifest.model_copy(update={"pack_id": _canonical_manifest_sha(manifest)})


def _assert_manifest_self_hash(manifest: StatisticalClusterPackManifestV2) -> None:
    if manifest.pack_id is None or manifest.pack_id != _canonical_manifest_sha(manifest):
        raise ValueError("statistical cluster pack self-hash mismatch")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid JSON source: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON source must be an object: {path}")
    return payload


def _market_slot(contract: LayerTwoAlphaDiagnosticRunContractV2) -> Any:
    return next(slot for slot in contract.input_slots if slot.kind == "sealed_market_snapshot")


def _candidate_slot(contract: LayerTwoAlphaDiagnosticRunContractV2) -> Any:
    return next(slot for slot in contract.input_slots if slot.kind == "candidate_eligibility_reports")


def _monthly_anchors(calendar: list[date]) -> list[date]:
    selected: dict[tuple[int, int], date] = {}
    for item in calendar:
        if date(2022, 1, 1) <= item <= date(2024, 12, 31):
            selected.setdefault((item.year, item.month), item)
    anchors = [selected[key] for key in sorted(selected)]
    if len(anchors) != 36:
        raise ValueError(f"expected exactly 36 monthly anchors, got {len(anchors)}")
    return anchors


def _valid_through_dates(anchors: list[date], calendar: list[date]) -> dict[date, date]:
    result: dict[date, date] = {}
    calendar_set = [item for item in calendar if date(2022, 1, 1) <= item <= date(2024, 12, 31)]
    for index, anchor in enumerate(anchors):
        if index + 1 < len(anchors):
            next_anchor = anchors[index + 1]
            prior = [item for item in calendar_set if anchor <= item < next_anchor]
            if not prior:
                raise ValueError("monthly anchor has no validity dates")
            result[anchor] = prior[-1]
        else:
            result[anchor] = date(2024, 12, 31)
    return result


def _candidate_symbols(candidate: pl.DataFrame, anchor: date) -> list[str]:
    rows = candidate.filter(
        (pl.col("as_of") == anchor.isoformat())
        & pl.col("eligible_for_new_entry")
        & (~pl.col("unknown_critical_input"))
    ).get_column("symbol")
    symbols = sorted(rows.to_list())
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError(f"candidate symbols missing or duplicated for anchor {anchor}")
    return symbols


def _complete_return_matrix(
    daily: pl.DataFrame,
    *,
    symbols: list[str],
    window: list[date],
) -> tuple[list[str], np.ndarray, dict[str, str]]:
    frame = daily.filter(pl.col("date").is_in(window) & pl.col("symbol").is_in(symbols))
    if frame.is_empty():
        return [], np.empty((LOOKBACK_RETURNS, 0), dtype=np.float64), {
            symbol: "missing_or_nonfinite_121_close_window" for symbol in symbols
        }
    pivot = frame.pivot(on="symbol", index="date", values="adj_close", aggregate_function=None).sort("date")
    if pivot.height != REQUIRED_CLOSES or pivot.get_column("date").to_list() != window:
        return [], np.empty((LOOKBACK_RETURNS, 0), dtype=np.float64), {
            symbol: "missing_or_nonfinite_121_close_window" for symbol in symbols
        }
    unresolved: dict[str, str] = {}
    evaluable: list[str] = []
    columns: list[np.ndarray] = []
    available = set(pivot.columns) - {"date"}
    for symbol in symbols:
        if symbol not in available:
            unresolved[symbol] = "missing_or_nonfinite_121_close_window"
            continue
        values = pivot.get_column(symbol).cast(pl.Float64).to_numpy()
        if values.shape[0] != REQUIRED_CLOSES or not np.isfinite(values).all() or np.any(values <= 0.0):
            unresolved[symbol] = "missing_or_nonfinite_121_close_window"
            continue
        returns = values[1:] / values[:-1] - 1.0
        if not np.isfinite(returns).all():
            unresolved[symbol] = "missing_or_nonfinite_120_return_window"
            continue
        std = float(np.std(returns, ddof=1))
        if not math.isfinite(std) or std <= 0.0:
            unresolved[symbol] = "zero_or_nonfinite_return_variance"
            continue
        evaluable.append(symbol)
        columns.append((returns - float(np.mean(returns))) / std)
    matrix = np.column_stack(columns) if columns else np.empty((LOOKBACK_RETURNS, 0), dtype=np.float64)
    return evaluable, matrix, unresolved


def _components(symbols: list[str], standardized_returns: np.ndarray) -> list[list[str]]:
    count = len(symbols)
    union_find = _UnionFind.create(count)
    if standardized_returns.shape != (LOOKBACK_RETURNS, count):
        raise ValueError("standardized return matrix shape mismatch")
    denominator = float(LOOKBACK_RETURNS - 1)
    for start in range(0, count, BLOCK_SIZE):
        stop = min(count, start + BLOCK_SIZE)
        correlations = standardized_returns[:, start:stop].T @ standardized_returns / denominator
        for local, left in enumerate(range(start, stop)):
            right_indices = np.flatnonzero(correlations[local, left + 1 :] >= CORRELATION_THRESHOLD)
            for right in right_indices + left + 1:
                union_find.union(left, int(right))
    grouped: dict[int, list[str]] = {}
    for index, symbol in enumerate(symbols):
        grouped.setdefault(union_find.find(index), []).append(symbol)
    components = [sorted(items) for items in grouped.values()]
    return sorted(components, key=lambda items: (items[0], len(items), items))


def _anchor_rows(
    *,
    anchor: date,
    valid_through: date,
    calendar: list[date],
    candidate: pl.DataFrame,
    daily: pl.DataFrame,
) -> list[dict[str, Any]]:
    symbols = _candidate_symbols(candidate, anchor)
    anchor_index = calendar.index(anchor)
    if anchor_index + 1 < REQUIRED_CLOSES:
        return [
            {
                "anchor_date": anchor,
                "valid_through": valid_through,
                "symbol": symbol,
                "cluster_id": None,
                "cluster_size": None,
                "status": "unknown_insufficient_global_history",
                "unknown_reason": "fewer_than_121_market_trading_days_available",
            }
            for symbol in symbols
        ]
    window = calendar[anchor_index - LOOKBACK_RETURNS : anchor_index + 1]
    evaluable, returns, unresolved = _complete_return_matrix(daily, symbols=symbols, window=window)
    components = _components(evaluable, returns)
    assignments: dict[str, tuple[str, int]] = {}
    for number, component in enumerate(components, start=1):
        cluster_id = f"{anchor:%Y%m%d}-C{number:04d}"
        for symbol in component:
            assignments[symbol] = (cluster_id, len(component))
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        assignment = assignments.get(symbol)
        if assignment is None:
            rows.append(
                {
                    "anchor_date": anchor,
                    "valid_through": valid_through,
                    "symbol": symbol,
                    "cluster_id": None,
                    "cluster_size": None,
                    "status": "unknown_incomplete_history",
                    "unknown_reason": unresolved.get(symbol, "unresolved_cluster_input"),
                }
            )
        else:
            cluster_id, cluster_size = assignment
            rows.append(
                {
                    "anchor_date": anchor,
                    "valid_through": valid_through,
                    "symbol": symbol,
                    "cluster_id": cluster_id,
                    "cluster_size": cluster_size,
                    "status": "assigned" if cluster_size > 1 else "assigned_singleton",
                    "unknown_reason": None,
                }
            )
    return rows


def _build_assignments(*, repo_root: Path) -> tuple[pl.DataFrame, ClusterPackSourceBinding]:
    root = repo_root.resolve()
    _, _, contract = verify_bundle(repo_root=root)
    verify_candidate_eligibility_pack(root / CANDIDATE_PACK_PATH, repo_root=root)
    if contract.contract_id is None:
        raise ValueError("v2 run contract ID missing")
    market_slot = _market_slot(contract)
    candidate_slot = _candidate_slot(contract)
    market_manifest_path = root / MARKET_DIR / "manifest.json"
    candidate_manifest_path = root / CANDIDATE_PACK_PATH / "manifest.json"
    candidate_parquet_path = root / CANDIDATE_PACK_PATH / "eligibility_verdicts.parquet"
    if _sha256_file(market_manifest_path) != market_slot.file_sha256:
        raise ValueError("market manifest hash drift")
    if _sha256_file(candidate_manifest_path) != candidate_slot.file_sha256:
        raise ValueError("candidate manifest hash drift")
    candidate_manifest = _read_json(candidate_manifest_path)
    integrity = candidate_manifest.get("integrity")
    if not isinstance(integrity, dict):
        raise ValueError("candidate integrity missing")
    candidate_parquet_sha = _hex64(
        integrity.get("parquet_file_sha256"),
        field_name="candidate parquet SHA-256",
    )
    if _sha256_file(candidate_parquet_path) != candidate_parquet_sha:
        raise ValueError("candidate parquet hash drift")

    calendar = pl.read_parquet(root / MARKET_DIR / "calendar.parquet").get_column("date").to_list()
    if not calendar or calendar != sorted(calendar) or len(calendar) != len(set(calendar)):
        raise ValueError("market calendar must be sorted and unique")
    anchors = _monthly_anchors(calendar)
    valid_through = _valid_through_dates(anchors, calendar)
    candidate = pl.read_parquet(
        candidate_parquet_path,
        columns=["symbol", "as_of", "eligible_for_new_entry", "unknown_critical_input"],
    )
    daily = pl.read_parquet(
        root / MARKET_DIR / "daily_bars.parquet",
        columns=["symbol", "date", "adj_close"],
    )
    rows: list[dict[str, Any]] = []
    for anchor in anchors:
        rows.extend(
            _anchor_rows(
                anchor=anchor,
                valid_through=valid_through[anchor],
                calendar=calendar,
                candidate=candidate,
                daily=daily,
            )
        )
    frame = pl.DataFrame(
        rows,
        schema={
            "anchor_date": pl.Date,
            "valid_through": pl.Date,
            "symbol": pl.String,
            "cluster_id": pl.String,
            "cluster_size": pl.Int32,
            "status": pl.String,
            "unknown_reason": pl.String,
        },
        strict=False,
    ).sort(["anchor_date", "symbol"])
    if frame.select(pl.struct(["anchor_date", "symbol"]).n_unique()).item() != frame.height:
        raise ValueError("cluster assignments anchor/symbol key is not unique")
    binding = ClusterPackSourceBinding(
        run_contract_path=DEFAULT_RUN_CONTRACT_PATH.as_posix(),
        run_contract_id=contract.contract_id,
        run_contract_file_sha256=_sha256_file(root / DEFAULT_RUN_CONTRACT_PATH),
        market_path=MARKET_DIR.as_posix(),
        market_snapshot_id=market_slot.artifact_id,
        market_manifest_sha256=market_slot.file_sha256,
        candidate_pack_path=CANDIDATE_PACK_PATH.as_posix(),
        candidate_pack_id=candidate_slot.artifact_id,
        candidate_manifest_sha256=candidate_slot.file_sha256,
        candidate_parquet_sha256=candidate_parquet_sha,
    )
    return frame, binding


def materialize_cluster_pack(
    *,
    repo_root: Path,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> StatisticalClusterPackManifestV2:
    root = repo_root.resolve()
    output = output_dir if output_dir.is_absolute() else root / output_dir
    if output.exists():
        raise FileExistsError(f"cluster pack output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        frame, binding = _build_assignments(repo_root=root)
        assignments_path = temporary / ASSIGNMENTS_FILE
        frame.write_parquet(assignments_path, compression="zstd", statistics=True)
        counts = {
            str(row[0]): int(row[1])
            for row in frame.group_by("status").len().sort("status").iter_rows()
        }
        anchor_count = frame.get_column("anchor_date").n_unique()
        manifest = _seal_manifest(
            StatisticalClusterPackManifestV2(
                schema_version=PACK_SCHEMA_VERSION,
                pack_version=PACK_VERSION,
                status="materialized_verified_input_not_executable",
                source_binding=binding,
                coverage=ClusterPackCoverage(
                    start="2022-01-01",
                    end="2024-12-31",
                    monthly_anchor_rule="first_market_trading_day_of_calendar_month",
                    anchor_count=36,
                    early_insufficient_history_remains_unknown=True,
                ),
                lookback_returns=LOOKBACK_RETURNS,
                required_adjusted_closes=REQUIRED_CLOSES,
                correlation_threshold=CORRELATION_THRESHOLD,
                correlation_method="pearson_simple_returns_complete_window",
                linkage="connected_components_chain",
                assignment_population="candidate_complete_and_eligible_for_new_entry_at_anchor",
                singleton_companion_score_remains_unknown=True,
                missing_or_nonfinite_history_remains_unknown=True,
                factor_known_filter_applied_later_per_family=True,
                integrity=ClusterPackIntegrity(
                    assignments_file_sha256=_sha256_file(assignments_path),
                    row_count=frame.height,
                    anchor_count=anchor_count,
                    anchor_symbol_unique=True,
                ),
                status_counts=counts,
                readiness=ClusterPackReadiness(
                    research_only=True,
                    is_not_industry_classification=True,
                    no_current_industry_backfill=True,
                    ready_for_alpha_input_binding=True,
                    ready_for_alpha_diagnostic_execution=False,
                    ready_for_scoring=False,
                    ready_for_backtest=False,
                    ready_for_portfolio_construction=False,
                    ready_for_orders=False,
                    ready_for_trading=False,
                    auto_apply=False,
                ),
            )
        )
        (temporary / MANIFEST_FILE).write_text(
            json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return verify_cluster_pack(repo_root=root, pack_dir=output, full_recomputation=False)


def verify_cluster_pack(
    *,
    repo_root: Path,
    pack_dir: Path = DEFAULT_OUTPUT_DIR,
    full_recomputation: bool = True,
) -> StatisticalClusterPackManifestV2:
    root = repo_root.resolve()
    pack = pack_dir if pack_dir.is_absolute() else root / pack_dir
    if pack.is_symlink() or any(path.is_symlink() for path in pack.rglob("*")):
        raise ValueError("cluster pack must not contain symlinks")
    manifest_path = pack / MANIFEST_FILE
    assignments_path = pack / ASSIGNMENTS_FILE
    if not manifest_path.is_file() or not assignments_path.is_file():
        raise ValueError("cluster pack manifest or assignments missing")
    try:
        manifest = StatisticalClusterPackManifestV2.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError("cluster pack manifest invalid") from exc
    _assert_manifest_self_hash(manifest)
    if _sha256_file(assignments_path) != manifest.integrity.assignments_file_sha256:
        raise ValueError("cluster assignments file hash mismatch")
    frame = pl.read_parquet(assignments_path)
    if frame.height != manifest.integrity.row_count:
        raise ValueError("cluster assignment row count mismatch")
    if frame.get_column("anchor_date").n_unique() != manifest.integrity.anchor_count:
        raise ValueError("cluster assignment anchor count mismatch")
    if frame.select(pl.struct(["anchor_date", "symbol"]).n_unique()).item() != frame.height:
        raise ValueError("cluster assignment key is not unique")
    counts = {
        str(row[0]): int(row[1])
        for row in frame.group_by("status").len().sort("status").iter_rows()
    }
    if counts != manifest.status_counts:
        raise ValueError("cluster assignment status counts mismatch")
    _, _, contract = verify_bundle(repo_root=root)
    if contract.contract_id != manifest.source_binding.run_contract_id:
        raise ValueError("cluster pack run contract ID drift")
    if _sha256_file(root / DEFAULT_RUN_CONTRACT_PATH) != manifest.source_binding.run_contract_file_sha256:
        raise ValueError("cluster pack run contract hash drift")
    if full_recomputation:
        rebuilt, binding = _build_assignments(repo_root=root)
        if binding != manifest.source_binding:
            raise ValueError("cluster pack source binding does not recompute")
        left = frame.sort(["anchor_date", "symbol"])
        right = rebuilt.sort(["anchor_date", "symbol"])
        if left.schema != right.schema or not left.equals(right, null_equal=True):
            raise ValueError("cluster assignments do not fully recompute from bound sources")
    return manifest


__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "StatisticalClusterPackManifestV2",
    "materialize_cluster_pack",
    "verify_cluster_pack",
]
