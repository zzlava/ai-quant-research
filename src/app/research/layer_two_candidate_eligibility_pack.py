"""E11b-1b: Compact Parquet pack of E10a candidate eligibility verdicts.

Produces one row per (symbol, as_of) for every A-share candidate on each SSE
trading date in the configured window. Uses sealed market bars, PIT daily
valuation, and the raw verified collection's trade_cal / stock_basic /
namechange for domain discovery and listed-day counting.

This is research input only:
  - ready_for_scoring = false
  - ready_for_trading = false
  - Not alpha evidence and not authorization for trading.
  - Statistical clusters deliberately wait until financial verdicts define the
    final denominator.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.layer_two_allocation_protocol import (
    load_layer_two_allocation_protocol,
    verify_layer_two_allocation_protocol,
)
from app.research.layer_two_alpha_diagnostic_input_inventory import (
    BoundSlot,
    verify_inventory,
)
from app.research.layer_two_candidate_eligibility import (
    BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
    BOUND_TWO_LAYER_DECISION_CONTRACT_PATH,
    LAYER_TWO_CANDIDATE_ELIGIBILITY_ENGINE_VERSION,
    LayerTwoCandidateInput,
    LayerTwoLiquidityObservation,
    bind_two_layer_eligibility_policy,
    evaluate_layer_two_candidate,
)

# ---------------------------------------------------------------------------
# Constants and bound references
# ---------------------------------------------------------------------------

PACK_SCHEMA_VERSION: Literal["1"] = "1"
PACK_ENGINE_VERSION: Literal["layer-two-candidate-eligibility-pack-v1"] = "layer-two-candidate-eligibility-pack-v1"

BOUND_INVENTORY_PATH: Literal[
    "data/all-a-share-historical-v1/research/layer-two-alpha-diagnostic-input-inventory-v1.json"
] = "data/all-a-share-historical-v1/research/layer-two-alpha-diagnostic-input-inventory-v1.json"
BOUND_INVENTORY_ID: Literal["e11a2108dac3b5735a85d8dfdf529a72179c8e681033c1aa2648b688fb1a05c3"] = (
    "e11a2108dac3b5735a85d8dfdf529a72179c8e681033c1aa2648b688fb1a05c3"
)

BOUND_RAW_COLLECTION_DIR: Literal["data/raw/all-a-share-history-20211008-20241231-v1"] = (
    "data/raw/all-a-share-history-20211008-20241231-v1"
)
BOUND_RAW_COLLECTION_REQUEST_ID: Literal["0b1e4abf58af7c68e7e00e2ecddc7b205010e8a9f26c6c2bb9f7a81e0699f7d1"] = (
    "0b1e4abf58af7c68e7e00e2ecddc7b205010e8a9f26c6c2bb9f7a81e0699f7d1"
)
BOUND_RAW_COLLECTION_MANIFEST_SHA256: Literal["2e79423dbcfd49dca8148960071495d45abcb36c439b97f226f29ddd6757bbfa"] = (
    "2e79423dbcfd49dca8148960071495d45abcb36c439b97f226f29ddd6757bbfa"
)
BOUND_RAW_QUALITY_REPORT_SHA256: Literal["8fe834efd812d685228ad8a74733270e9526ea8b1ade876f349cb29da4b00081"] = (
    "8fe834efd812d685228ad8a74733270e9526ea8b1ade876f349cb29da4b00081"
)
BOUND_RAW_DATASET_HASH_TRADE_CAL: Literal["c69a93a08efb19c370ea7dfa72acd591ea2c2395044f940a2d2d96c38d07959b"] = (
    "c69a93a08efb19c370ea7dfa72acd591ea2c2395044f940a2d2d96c38d07959b"
)
BOUND_RAW_DATASET_HASH_STOCK_BASIC: Literal["2258ad54fb440b0d9a400d4bd6138ca0e26b79546db1df54a9c7f4cb710dcddc"] = (
    "2258ad54fb440b0d9a400d4bd6138ca0e26b79546db1df54a9c7f4cb710dcddc"
)
BOUND_RAW_DATASET_HASH_NAMECHANGE: Literal["4cd8bcd029987b3ed343b370d7a596f77c4ac8d2dbadc67ca5caa190bc37efcb"] = (
    "4cd8bcd029987b3ed343b370d7a596f77c4ac8d2dbadc67ca5caa190bc37efcb"
)

BOUND_TWO_LAYER_CONTRACT_ID = BOUND_TWO_LAYER_DECISION_CONTRACT_ID
BOUND_TWO_LAYER_CONTRACT_PATH = BOUND_TWO_LAYER_DECISION_CONTRACT_PATH
BOUND_TWO_LAYER_CONTRACT_FILE_SHA256: Literal["0e1afbf963c5d5b11e6db86d8fb5f7ccec3c364eb304c2227e7d9ae9eda345f6"] = (
    "0e1afbf963c5d5b11e6db86d8fb5f7ccec3c364eb304c2227e7d9ae9eda345f6"
)

BOUND_ALLOCATION_PROTOCOL_PATH: Literal["config/research/layer-two-allocation-implementation-protocol-v1.json"] = (
    "config/research/layer-two-allocation-implementation-protocol-v1.json"
)
BOUND_ALLOCATION_PROTOCOL_ID: Literal["0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"] = (
    "0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2"
)
BOUND_ALLOCATION_PROTOCOL_FILE_SHA256: Literal["b244904d1c440a24dcf0e019d2415b0a11a4f1f20a237187fcd580570b4de189"] = (
    "b244904d1c440a24dcf0e019d2415b0a11a4f1f20a237187fcd580570b4de189"
)

BOUND_PLANNED_BUY_NOTIONAL_CNY: Literal[8000] = 8000

BOUND_E10A_MODULE_SHA256: Literal["d6c29ee4da8eed4515c7444974afe944065c7b17e04d79f38b6bf57c30b4b4e0"] = (
    "d6c29ee4da8eed4515c7444974afe944065c7b17e04d79f38b6bf57c30b4b4e0"
)

COVERAGE_START = date(2022, 1, 1)
COVERAGE_END = date(2024, 12, 31)

ASIA_SHANGHAI = timezone(timedelta(hours=8))
DECISION_TIME = time(17, 30, 0)
MARKET_CLOSE_TIME = time(15, 0, 0)

CIRC_MV_UNIT_TO_CNY: Literal[10000] = 10000

_CANONICAL_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(SH|SZ)$")
_OOS_BOUNDARY_RE = re.compile(r"(?:^|[/\-_])oos(?:$|[/\-_])")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

_OUTPUT_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("symbol", pa.utf8()),
        pa.field("as_of", pa.utf8()),
        pa.field("decision_at", pa.utf8()),
        pa.field("eligible_for_new_entry", pa.bool_()),
        pa.field("unknown_critical_input", pa.bool_()),
        pa.field("market_scope_pass", pa.bool_()),
        pa.field("tradability_pass", pa.bool_()),
        pa.field("listing_history_pass", pa.bool_()),
        pa.field("st_delist_pass", pa.bool_()),
        pa.field("liquidity_structure_pass", pa.bool_()),
        pa.field("liquidity_tradable_count_pass", pa.bool_()),
        pa.field("liquidity_median_pass", pa.bool_()),
        pa.field("liquidity_capacity_pass", pa.bool_()),
        pa.field("size_cap_pass", pa.bool_()),
        pa.field("median_daily_amount_cny", pa.float64()),
        pa.field("average_daily_amount_cny", pa.float64()),
        pa.field("tradable_days_in_lookback", pa.int32()),
        pa.field("pit_free_float_market_cap_cny", pa.float64()),
        pa.field("size_multiplier", pa.float64()),
        pa.field("adjusted_planned_notional_cny", pa.float64()),
        pa.field("reason_codes", pa.utf8()),
        pa.field("source_input_hash", pa.utf8()),
    ]
)


# ---------------------------------------------------------------------------
# Frozen Pydantic models
# ---------------------------------------------------------------------------


class _StrictFrozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackSourceBinding(_StrictFrozen):
    inventory_path: str
    inventory_id: str = Field(min_length=64, max_length=64)
    market_snapshot_id: str = Field(min_length=64, max_length=64)
    market_manifest_sha256: str = Field(min_length=64, max_length=64)
    fundamental_snapshot_id: str = Field(min_length=64, max_length=64)
    fundamental_manifest_sha256: str = Field(min_length=64, max_length=64)
    valuation_file_sha256: str = Field(min_length=64, max_length=64)
    raw_collection_dir: str
    raw_collection_request_id: str = Field(min_length=64, max_length=64)
    raw_collection_manifest_sha256: str = Field(min_length=64, max_length=64)
    raw_quality_report_sha256: str = Field(min_length=64, max_length=64)
    raw_dataset_hash_trade_cal: str = Field(min_length=64, max_length=64)
    raw_dataset_hash_stock_basic: str = Field(min_length=64, max_length=64)
    raw_dataset_hash_namechange: str = Field(min_length=64, max_length=64)
    two_layer_contract_id: str = Field(min_length=64, max_length=64)
    two_layer_contract_path: str
    two_layer_contract_file_sha256: str = Field(min_length=64, max_length=64)
    allocation_protocol_id: str = Field(min_length=64, max_length=64)
    allocation_protocol_path: str
    allocation_protocol_file_sha256: str = Field(min_length=64, max_length=64)
    planned_buy_notional_cny: int
    e10a_engine_version: str
    e10a_module_sha256: str = Field(min_length=64, max_length=64)


class PackCoverageInfo(_StrictFrozen):
    start: date
    end: date
    trading_date_count: int = Field(ge=1)
    trading_date_set_sha256: str = Field(min_length=64, max_length=64)


class PackRowCounts(_StrictFrozen):
    total: int = Field(ge=0)
    year_2022: int = Field(ge=0)
    year_2023: int = Field(ge=0)
    year_2024: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_total(self) -> PackRowCounts:
        expected = self.year_2022 + self.year_2023 + self.year_2024
        if self.total != expected:
            raise ValueError(f"total ({self.total}) must equal sum of per-year counts ({expected})")
        return self


class PackIntegrity(_StrictFrozen):
    parquet_file_sha256: str = Field(min_length=64, max_length=64)
    canonical_table_sha256: str = Field(min_length=64, max_length=64)
    symbol_date_key_unique: Literal[True]
    row_count: int = Field(ge=0)


class PackReadinessFlags(_StrictFrozen):
    research_only: Literal[True]
    ready_for_scoring: Literal[False]
    ready_for_trading: Literal[False]
    ready_for_portfolio_construction: Literal[False]
    not_alpha_evidence: Literal[True]
    not_authorization: Literal[True]


class CandidateEligibilityPackManifest(_StrictFrozen):
    schema_version: Literal["1"]
    pack_version: Literal["layer-two-candidate-eligibility-pack-v1"]
    source_binding: PackSourceBinding
    coverage: PackCoverageInfo
    row_counts: PackRowCounts
    integrity: PackIntegrity
    readiness: PackReadinessFlags
    pack_module_sha256: str = Field(min_length=64, max_length=64)
    e10a_module_sha256: str = Field(min_length=64, max_length=64)
    pack_id: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def _validate_row_count_consistency(self) -> CandidateEligibilityPackManifest:
        if self.integrity.row_count != self.row_counts.total:
            raise ValueError("integrity.row_count must equal row_counts.total")
        return self

    @model_validator(mode="after")
    def _validate_readiness(self) -> CandidateEligibilityPackManifest:
        r = self.readiness
        if r.ready_for_scoring or r.ready_for_trading or r.ready_for_portfolio_construction:
            raise ValueError("pack cannot be ready for scoring/trading/portfolio construction")
        if not r.research_only or not r.not_alpha_evidence or not r.not_authorization:
            raise ValueError("pack must be research_only, not_alpha_evidence, not_authorization")
        return self


# ---------------------------------------------------------------------------
# Canonical hashing helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def canonical_manifest_payload(
    manifest: CandidateEligibilityPackManifest,
) -> dict[str, Any]:
    return manifest.model_dump(mode="json", exclude={"pack_id"})


def canonical_manifest_bytes(
    manifest: CandidateEligibilityPackManifest,
) -> bytes:
    payload = canonical_manifest_payload(manifest)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_pack_id(manifest: CandidateEligibilityPackManifest) -> str:
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def seal_manifest(
    manifest: CandidateEligibilityPackManifest,
) -> CandidateEligibilityPackManifest:
    pid = compute_pack_id(manifest)
    payload = manifest.model_dump(mode="json")
    payload["pack_id"] = pid
    return CandidateEligibilityPackManifest.model_validate(payload)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _validate_safe_path(path: Path, *, repo_root: Path, field_name: str) -> Path:
    root_resolved = repo_root.resolve()
    unresolved_abs = path if path.is_absolute() else (root_resolved / path)

    try:
        unresolved_rel_parts = unresolved_abs.relative_to(root_resolved).parts
    except ValueError as exc:
        raise ValueError(f"{field_name} escapes repo root") from exc

    current = root_resolved
    for component in unresolved_rel_parts:
        current = current / component
        if current.is_symlink():
            raise ValueError(f"{field_name} has a symlink component (forbidden)")

    resolved = path.resolve()
    try:
        rel_str = str(resolved.relative_to(root_resolved))
    except ValueError as exc:
        raise ValueError(f"{field_name} escapes repo root") from exc

    if ".." in Path(rel_str).parts:
        raise ValueError(f"{field_name} contains '..' path escape")
    lower = rel_str.lower()
    if "2025" in lower:
        raise ValueError(f"{field_name} references 2025/OOS namespace (forbidden)")
    if _OOS_BOUNDARY_RE.search(lower):
        raise ValueError(f"{field_name} references OOS namespace (forbidden)")
    return resolved


# ---------------------------------------------------------------------------
# Trading-date set hash
# ---------------------------------------------------------------------------


def compute_trading_date_set_hash(dates: Sequence[date]) -> str:
    sorted_dates = sorted(dates)
    payload = "\n".join(d.isoformat() for d in sorted_dates)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Canonical table hash (streaming, batch-boundary independent)
# Uses batch.column(col_name) for named access (Finding #7)
# ---------------------------------------------------------------------------


def compute_canonical_table_hash_streaming(reader: pq.ParquetFile) -> str:
    h = hashlib.sha256()
    col_names = sorted(reader.schema_arrow.names)
    h.update("|".join(col_names).encode("utf-8"))
    h.update(b"\n")
    for batch in reader.iter_batches():
        for row_idx in range(batch.num_rows):
            for col_name in col_names:
                val = batch.column(col_name)[row_idx].as_py()
                h.update(str(val).encode("utf-8"))
                h.update(b"\0")
            h.update(b"\n")
    return h.hexdigest()


def compute_canonical_table_hash(table: pa.Table) -> str:
    h = hashlib.sha256()
    col_names = sorted(table.column_names)
    h.update("|".join(col_names).encode("utf-8"))
    h.update(b"\n")
    for batch in table.to_batches():
        for row_idx in range(batch.num_rows):
            for col_name in col_names:
                val = batch.column(col_name)[row_idx].as_py()
                h.update(str(val).encode("utf-8"))
                h.update(b"\0")
            h.update(b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Decision-at and market-close helpers
# ---------------------------------------------------------------------------


def make_decision_at(as_of: date) -> datetime:
    """17:30 Asia/Shanghai on the given date."""
    return datetime.combine(as_of, DECISION_TIME, tzinfo=ASIA_SHANGHAI)


def _make_market_close(observation_date: date) -> datetime:
    """15:00 Asia/Shanghai on the given date (bars available after close)."""
    return datetime.combine(observation_date, MARKET_CLOSE_TIME, tzinfo=ASIA_SHANGHAI)


# ---------------------------------------------------------------------------
# Ordinary-A detection from stock_basic
# ---------------------------------------------------------------------------


def is_ordinary_a_share_from_stock_basic(
    ts_code: str,
    exchange: str | None,
) -> bool:
    if not _CANONICAL_SYMBOL_PATTERN.fullmatch(ts_code):
        return False
    numeric = ts_code[:6]
    if ts_code.endswith(".SH") and numeric.startswith("9"):
        return False
    if ts_code.endswith(".SZ") and numeric.startswith("2"):
        return False
    if exchange is not None and exchange.upper() in ("BSE", "BJ"):
        return False
    return True


# ---------------------------------------------------------------------------
# Source input hash per row (expanded envelope - Finding #4)
# ---------------------------------------------------------------------------


def compute_source_input_hash(
    candidate_input: LayerTwoCandidateInput,
    *,
    market_snapshot_id: str,
    raw_request_id: str,
    valuation_source_row_hash: str | None,
    inventory_id: str,
    fundamental_snapshot_id: str,
    valuation_file_sha256: str,
    two_layer_contract_id: str,
    allocation_protocol_id: str,
) -> str:
    base = candidate_input.model_dump(mode="json")
    envelope: dict[str, Any] = {
        "candidate_input": base,
        "market_snapshot_id": market_snapshot_id,
        "raw_request_id": raw_request_id,
        "valuation_source_row_hash": valuation_source_row_hash,
        "inventory_id": inventory_id,
        "fundamental_snapshot_id": fundamental_snapshot_id,
        "valuation_file_sha256": valuation_file_sha256,
        "two_layer_contract_id": two_layer_contract_id,
        "allocation_protocol_id": allocation_protocol_id,
    }
    payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Raw collection strict verification (mirrors tushare_all_market_history)
# ---------------------------------------------------------------------------


def _compute_dataset_hashes(root: Path) -> dict[str, str]:
    """Exact replica of tushare_all_market_history._dataset_hashes."""
    out: dict[str, str] = {}
    paths: list[Path] = sorted((root / "reference").glob("*.parquet"))
    paths.extend(sorted((root / "partitions").glob("*/*.parquet")))
    grouped: dict[str, list[Path]] = {}
    for p in paths:
        key = p.parent.name if p.parent.name != "reference" else f"reference/{p.stem}"
        grouped.setdefault(key, []).append(p)
    for key, files in sorted(grouped.items()):
        digest = hashlib.sha256()
        for p in sorted(files):
            digest.update(p.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(_sha256_file(p).encode("ascii"))
            digest.update(b"\n")
        out[key] = digest.hexdigest()
    return out


def verify_raw_collection_strict(
    raw_dir: Path,
    *,
    repo_root: Path,
) -> dict[str, Any]:
    raw_dir = _validate_safe_path(raw_dir, repo_root=repo_root, field_name="raw_collection_dir")

    manifest_path = raw_dir / "collection_manifest.json"
    if not manifest_path.is_file():
        raise ValueError("collection_manifest.json missing in raw collection dir")
    manifest_sha = _sha256_file(manifest_path)
    if manifest_sha != BOUND_RAW_COLLECTION_MANIFEST_SHA256:
        raise ValueError(
            f"collection_manifest.json SHA-256 mismatch: "
            f"expected {BOUND_RAW_COLLECTION_MANIFEST_SHA256}, got {manifest_sha}"
        )

    manifest: dict[str, Any] = json.loads(manifest_path.read_text("utf-8"))
    if manifest.get("request_id") != BOUND_RAW_COLLECTION_REQUEST_ID:
        raise ValueError("raw collection request_id mismatch")

    quality_path = raw_dir / "quality_report.json"
    if not quality_path.is_file():
        raise ValueError("quality_report.json missing in raw collection dir")
    quality_sha = _sha256_file(quality_path)
    if quality_sha != BOUND_RAW_QUALITY_REPORT_SHA256:
        raise ValueError(f"quality_report.json SHA-256 mismatch: got {quality_sha}")
    if manifest.get("quality_report_sha256") != quality_sha:
        raise ValueError("manifest quality_report_sha256 does not match actual quality report hash")

    manifest_dataset_hashes: dict[str, str] = manifest.get("dataset_hashes", {})

    # Finding #3: Recompute ALL dataset hashes, require full dict equality
    recomputed = _compute_dataset_hashes(raw_dir)
    if recomputed != manifest_dataset_hashes:
        raise ValueError("recomputed dataset hashes do not match manifest dataset_hashes (full dict equality failed)")

    # THEN also enforce the three bound constants
    for key, expected in [
        ("reference/trade_cal", BOUND_RAW_DATASET_HASH_TRADE_CAL),
        ("reference/stock_basic", BOUND_RAW_DATASET_HASH_STOCK_BASIC),
        ("reference/namechange", BOUND_RAW_DATASET_HASH_NAMECHANGE),
    ]:
        actual = manifest_dataset_hashes.get(key)
        if actual != expected:
            raise ValueError(f"raw dataset_hashes[{key}] mismatch: expected {expected}, got {actual}")

    return manifest


# ---------------------------------------------------------------------------
# Data loading helpers (import polars lazily)
# ---------------------------------------------------------------------------


def _parse_date_col(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    s = str(val)
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _load_trade_cal_dates(raw_dir: Path) -> list[date]:
    import polars as pl

    path = raw_dir / "reference" / "trade_cal.parquet"
    if not path.is_file():
        raise ValueError("reference/trade_cal.parquet missing")
    df: pl.DataFrame = pl.read_parquet(path)
    if "is_open" in df.columns:
        df = df.filter(pl.col("is_open") == 1)
    elif "status" in df.columns:
        df = df.filter(pl.col("status") == 1)
    if "cal_date" in df.columns:
        dates_col = df["cal_date"]
    elif "date" in df.columns:
        dates_col = df["date"]
    else:
        raise ValueError("trade_cal has no recognized date column")
    result: list[date] = []
    for val in dates_col.to_list():
        parsed = _parse_date_col(val)
        if parsed is not None:
            result.append(parsed)
    result.sort()
    return result


def _load_stock_basic(raw_dir: Path) -> dict[str, dict[str, Any]]:
    """Load stock_basic, rejecting duplicate ts_code at load time (Finding #1)."""
    import polars as pl

    path = raw_dir / "reference" / "stock_basic.parquet"
    if not path.is_file():
        raise ValueError("reference/stock_basic.parquet missing")
    df: pl.DataFrame = pl.read_parquet(path)
    stock_info: dict[str, dict[str, Any]] = {}
    for row in df.iter_rows(named=True):
        ts_code = str(row.get("ts_code", ""))
        if ts_code:
            if ts_code in stock_info:
                raise ValueError(f"duplicate ts_code in stock_basic: {ts_code}")
            stock_info[ts_code] = row
    return stock_info


BarTuple = tuple[date, float | None, bool | None, bool | None]

# (date, circ_mv, available_at_raw, source_row_hash)
ValTuple = tuple[date, float | None, Any, str | None]


def _load_daily_bars_index(
    market_dir: Path,
) -> dict[str, list[BarTuple]]:
    """Load daily_bars via streaming ParquetFile.iter_batches (required columns only).

    Preserves None for missing is_suspended/is_st.
    Rejects duplicate (symbol, date) via adjacent check after sorting.
    """
    path = market_dir / "daily_bars.parquet"
    if not path.is_file():
        raise ValueError("daily_bars.parquet missing in market dir")

    pf = pq.ParquetFile(str(path))
    schema_names = set(pf.schema_arrow.names)
    bar_dates_col = "trade_date" if "trade_date" in schema_names else "date"
    bar_symbol_col = "ts_code" if "ts_code" in schema_names else "symbol"

    needed = [bar_symbol_col, bar_dates_col]
    if "amount" in schema_names:
        needed.append("amount")
    if "is_suspended" in schema_names:
        needed.append("is_suspended")
    if "is_st" in schema_names:
        needed.append("is_st")

    symbol_bars: dict[str, list[BarTuple]] = {}
    for batch in pf.iter_batches(columns=needed):
        for row_idx in range(batch.num_rows):
            sym_raw = batch.column(bar_symbol_col)[row_idx].as_py()
            sym = str(sym_raw) if sym_raw is not None else ""
            d = _parse_date_col(batch.column(bar_dates_col)[row_idx].as_py())
            if not sym or d is None:
                continue
            amount: float | None = None
            if "amount" in needed:
                amount_raw = batch.column("amount")[row_idx].as_py()
                if amount_raw is not None:
                    try:
                        amount = float(amount_raw)
                    except (ValueError, TypeError):
                        pass
            is_suspended: bool | None = None
            if "is_suspended" in needed:
                is_suspended_raw = batch.column("is_suspended")[row_idx].as_py()
                if is_suspended_raw is not None:
                    is_suspended = bool(is_suspended_raw)
            is_st: bool | None = None
            if "is_st" in needed:
                is_st_raw = batch.column("is_st")[row_idx].as_py()
                if is_st_raw is not None:
                    is_st = bool(is_st_raw)
            symbol_bars.setdefault(sym, []).append((d, amount, is_suspended, is_st))

    for sym, bars in symbol_bars.items():
        bars.sort(key=lambda x: x[0])
        for i in range(1, len(bars)):
            if bars[i][0] == bars[i - 1][0]:
                raise ValueError(f"duplicate (symbol, date) bar row: {sym} on {bars[i][0].isoformat()}")
    return symbol_bars


def _load_daily_valuation_index(
    fundamental_dir: Path,
) -> dict[str, list[ValTuple]]:
    """Load daily_valuation via streaming ParquetFile.iter_batches (required columns only).

    Returns symbol -> sorted list of ValTuple(date, circ_mv, available_at_raw, source_row_hash).
    """
    path = fundamental_dir / "daily_valuation.parquet"
    if not path.is_file():
        raise ValueError("daily_valuation.parquet missing in fundamental dir")

    pf = pq.ParquetFile(str(path))
    schema_names = set(pf.schema_arrow.names)
    val_date_col = "trade_date" if "trade_date" in schema_names else "date"
    val_symbol_col = "ts_code" if "ts_code" in schema_names else "symbol"

    needed = [val_symbol_col, val_date_col, "circ_mv", "available_at", "source_row_hash"]
    needed = [c for c in needed if c in schema_names]
    if val_symbol_col not in needed:
        needed.insert(0, val_symbol_col)
    if val_date_col not in needed:
        needed.insert(1, val_date_col)

    symbol_val: dict[str, list[ValTuple]] = {}
    for batch in pf.iter_batches(columns=needed):
        sym_col = batch.column(val_symbol_col)
        date_col = batch.column(val_date_col)
        circ_col = batch.column("circ_mv") if "circ_mv" in needed else None
        avail_col = batch.column("available_at") if "available_at" in needed else None
        hash_col = batch.column("source_row_hash") if "source_row_hash" in needed else None

        for row_idx in range(batch.num_rows):
            sym_raw = sym_col[row_idx].as_py()
            sym = str(sym_raw) if sym_raw is not None else ""
            d = _parse_date_col(date_col[row_idx].as_py())
            if not sym or d is None:
                continue
            circ_mv: float | None = None
            if circ_col is not None:
                circ_mv_raw = circ_col[row_idx].as_py()
                if circ_mv_raw is not None:
                    try:
                        circ_mv = float(circ_mv_raw)
                    except (ValueError, TypeError):
                        pass
            available_at_raw: Any = None
            if avail_col is not None:
                available_at_raw = avail_col[row_idx].as_py()
            source_row_hash: str | None = None
            if hash_col is not None:
                srh = hash_col[row_idx].as_py()
                if srh is not None:
                    source_row_hash = str(srh)
            symbol_val.setdefault(sym, []).append((d, circ_mv, available_at_raw, source_row_hash))

    for sym in symbol_val:
        symbol_val[sym].sort(key=lambda x: x[0])
    return symbol_val


# ---------------------------------------------------------------------------
# Calendar bisect helpers
# ---------------------------------------------------------------------------


def _bisect_find(sorted_dates: list[date], target: date) -> int:
    """Return index of target in sorted_dates, or -1 if not found."""
    idx = bisect.bisect_left(sorted_dates, target)
    if idx < len(sorted_dates) and sorted_dates[idx] == target:
        return idx
    return -1


def _bisect_find_bar(
    bar_list: list[BarTuple],
    target: date,
) -> int:
    lo, hi = 0, len(bar_list) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if bar_list[mid][0] == target:
            return mid
        elif bar_list[mid][0] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


# ---------------------------------------------------------------------------
# Module provenance
# ---------------------------------------------------------------------------

_THIS_MODULE_PATH = Path(__file__).resolve()


def _compute_pack_module_sha256() -> str:
    return _sha256_file(_THIS_MODULE_PATH)


def _compute_e10a_module_sha256(repo_root: Path) -> str:
    e10a_path = repo_root / "src" / "app" / "research" / "layer_two_candidate_eligibility.py"
    if not e10a_path.is_file():
        raise ValueError(f"E10a module not found at {e10a_path}")
    return _sha256_file(e10a_path)


def _require_bound_e10a_sha(actual_sha: str) -> None:
    """Fail if the current E10a module SHA does not match the frozen constant."""
    if actual_sha != BOUND_E10A_MODULE_SHA256:
        raise ValueError(
            f"E10a module SHA-256 does not match frozen constant: expected {BOUND_E10A_MODULE_SHA256}, got {actual_sha}"
        )


def _require_bound_inventory_id(actual_id: str) -> None:
    """Fail if the inventory ID does not match the frozen constant."""
    if actual_id != BOUND_INVENTORY_ID:
        raise ValueError(f"inventory_id does not match frozen constant: expected {BOUND_INVENTORY_ID}, got {actual_id}")


# ---------------------------------------------------------------------------
# Shared row generation
# ---------------------------------------------------------------------------


def _bisect_find_val(
    val_list: list[ValTuple],
    target: date,
) -> int:
    """Binary search for target date in sorted ValTuple list. Returns first match index or -1."""
    lo, hi = 0, len(val_list) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if val_list[mid][0] == target:
            return mid
        elif val_list[mid][0] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


def _resolve_valuation_for_candidate(
    sym: str,
    as_of: date,
    *,
    decision_at: datetime,
    symbol_valuation: dict[str, list[ValTuple]],
) -> tuple[float | None, str | None, datetime | None]:
    """Resolve PIT valuation for a symbol on as_of using binary search on compact ValTuple.

    Returns (pit_cap_cny, valuation_source_row_hash, valuation_available_at).
    O(log n) per call — no per-call list allocation.
    """
    val_list = symbol_valuation.get(sym, [])
    if not val_list:
        return None, None, None

    idx = _bisect_find_val(val_list, as_of)
    if idx < 0:
        return None, None, None

    # Detect duplicate same-day by checking neighbors
    if (idx > 0 and val_list[idx - 1][0] == as_of) or (idx + 1 < len(val_list) and val_list[idx + 1][0] == as_of):
        return None, None, None

    val_entry = val_list[idx]
    _, circ_mv, raw_available_at, source_row_hash = val_entry

    # Validate source_row_hash is exactly 64 lowercase hex
    if not isinstance(source_row_hash, str) or not _HEX64.fullmatch(source_row_hash):
        return None, None, None

    available_at_utc: datetime | None = None
    if isinstance(raw_available_at, datetime):
        if raw_available_at.tzinfo is None:
            available_at_utc = raw_available_at.replace(tzinfo=UTC)
        else:
            available_at_utc = raw_available_at.astimezone(UTC)

    if available_at_utc is None:
        return None, None, None

    # Convert to Asia/Shanghai and require calendar date == as_of
    available_at_shanghai = available_at_utc.astimezone(ASIA_SHANGHAI)
    if available_at_shanghai.date() != as_of:
        return None, None, None

    if available_at_utc > decision_at.astimezone(UTC):
        return None, None, None

    if circ_mv is None:
        return None, None, None
    if not isinstance(circ_mv, (int, float)):
        return None, None, None
    if not math.isfinite(circ_mv):
        return None, None, None
    if circ_mv <= 0:
        return None, None, None

    pit_cap_cny = circ_mv * CIRC_MV_UNIT_TO_CNY
    valuation_available_at = available_at_utc

    return pit_cap_cny, source_row_hash, valuation_available_at


def _generate_row_for_candidate(
    sym: str,
    as_of: date,
    *,
    decision_at: datetime,
    stock_info: dict[str, dict[str, Any]],
    symbol_bars: dict[str, list[BarTuple]],
    symbol_valuation: dict[str, list[ValTuple]],
    all_trading_dates: list[date],
    market_snapshot_id: str,
    fund_snapshot_id: str,
    val_file_sha256: str,
    inventory_id: str,
    contract_id: str,
    alloc_id: str,
    policy: Any,
) -> dict[str, Any]:
    """Generate a single pack row for one (symbol, as_of) pair.

    Shared between builder and verifier for exact parity.
    """
    market_code: str | None = None
    if sym.endswith(".SH"):
        market_code = "SSE"
    elif sym.endswith(".SZ"):
        market_code = "SZSE"

    bar_list = symbol_bars.get(sym, [])

    bar_idx = _bisect_find_bar(bar_list, as_of)
    has_bar = bar_idx >= 0
    bar_on_date = bar_list[bar_idx] if has_bar else None

    is_suspended: bool | None = None
    is_st: bool | None = None
    if bar_on_date is not None:
        is_suspended = bar_on_date[2]
        is_st = bar_on_date[3]

    # Listed market trading days
    info = stock_info[sym]
    ld = _parse_date_col(info.get("list_date"))
    listed_days: int | None = None
    if ld is not None:
        start_idx = bisect.bisect_left(all_trading_dates, ld)
        end_idx = bisect.bisect_right(all_trading_dates, as_of)
        listed_days = end_idx - start_idx

    # Liquidity window: exact last 20 SSE open days ending on as_of
    as_of_idx = _bisect_find(all_trading_dates, as_of)
    lookback_dates: list[date] = []
    if as_of_idx >= 0:
        start_lb = max(0, as_of_idx - 19)
        lookback_dates = all_trading_dates[start_lb : as_of_idx + 1]

    liquidity_obs: list[LayerTwoLiquidityObservation] = []
    for obs_date in lookback_dates:
        obs_available_at = _make_market_close(obs_date)
        obs_idx = _bisect_find_bar(bar_list, obs_date)
        if obs_idx < 0:
            liquidity_obs.append(
                LayerTwoLiquidityObservation(
                    observation_date=obs_date,
                    tradability=None,
                    amount_cny=None,
                    available_at=obs_available_at,
                )
            )
        else:
            obs_bar = bar_list[obs_idx]
            _, obs_amount, obs_susp, _ = obs_bar
            if obs_susp:
                liquidity_obs.append(
                    LayerTwoLiquidityObservation(
                        observation_date=obs_date,
                        tradability="known_full_day_suspension",
                        amount_cny=0.0,
                        available_at=obs_available_at,
                    )
                )
            elif obs_amount is not None:
                # Finding #8: invalid/nonfinite/negative amount → unknown
                if not isinstance(obs_amount, (int, float)) or not math.isfinite(obs_amount) or obs_amount < 0:
                    liquidity_obs.append(
                        LayerTwoLiquidityObservation(
                            observation_date=obs_date,
                            tradability=None,
                            amount_cny=None,
                            available_at=obs_available_at,
                        )
                    )
                else:
                    liquidity_obs.append(
                        LayerTwoLiquidityObservation(
                            observation_date=obs_date,
                            tradability="tradable",
                            amount_cny=obs_amount,
                            available_at=obs_available_at,
                        )
                    )
            else:
                liquidity_obs.append(
                    LayerTwoLiquidityObservation(
                        observation_date=obs_date,
                        tradability=None,
                        amount_cny=None,
                        available_at=obs_available_at,
                    )
                )

    # PIT valuation
    pit_cap_cny, valuation_source_row_hash, valuation_available_at = _resolve_valuation_for_candidate(
        sym,
        as_of,
        decision_at=decision_at,
        symbol_valuation=symbol_valuation,
    )

    # Finding #2: security_status_available_at = market close (15:00), NOT decision_at
    security_status_available_at = _make_market_close(as_of) if has_bar else None

    # Finding #2: pit_free_float_market_cap_available_at = actual stored available_at
    pit_available_at = valuation_available_at if pit_cap_cny is not None else None

    candidate_input = LayerTwoCandidateInput(
        symbol=sym,
        market=market_code,
        is_ordinary_a_share=True,
        is_bse=False,
        is_st_or_delist_risk=is_st,
        is_suspended_on_decision_date=is_suspended,
        listed_market_trading_days=listed_days,
        security_status_as_of=as_of if has_bar else None,
        security_status_available_at=security_status_available_at,
        planned_buy_notional_cny=float(BOUND_PLANNED_BUY_NOTIONAL_CNY),
        liquidity_observations=liquidity_obs,
        pit_free_float_market_cap_cny=pit_cap_cny,
        pit_free_float_market_cap_as_of=as_of if pit_cap_cny is not None else None,
        pit_free_float_market_cap_available_at=pit_available_at,
    )

    evaluation = evaluate_layer_two_candidate(
        candidate_input,
        as_of=as_of,
        decision_at=decision_at,
        policy=policy,
    )

    # Finding #4: expanded source_input_hash envelope
    src_hash = compute_source_input_hash(
        candidate_input,
        market_snapshot_id=market_snapshot_id,
        raw_request_id=BOUND_RAW_COLLECTION_REQUEST_ID,
        valuation_source_row_hash=valuation_source_row_hash,
        inventory_id=inventory_id,
        fundamental_snapshot_id=fund_snapshot_id,
        valuation_file_sha256=val_file_sha256,
        two_layer_contract_id=contract_id,
        allocation_protocol_id=alloc_id,
    )

    return {
        "symbol": sym,
        "as_of": as_of.isoformat(),
        "decision_at": decision_at.isoformat(),
        "eligible_for_new_entry": evaluation.eligible_for_new_entry,
        "unknown_critical_input": evaluation.unknown_critical_input,
        "market_scope_pass": evaluation.market_scope_pass,
        "tradability_pass": evaluation.tradability_pass,
        "listing_history_pass": evaluation.listing_history_pass,
        "st_delist_pass": evaluation.st_delist_pass,
        "liquidity_structure_pass": evaluation.liquidity_structure_pass,
        "liquidity_tradable_count_pass": evaluation.liquidity_tradable_count_pass,
        "liquidity_median_pass": evaluation.liquidity_median_pass,
        "liquidity_capacity_pass": evaluation.liquidity_capacity_pass,
        "size_cap_pass": evaluation.size_cap_pass,
        "median_daily_amount_cny": evaluation.median_daily_amount_cny,
        "average_daily_amount_cny": evaluation.average_daily_amount_cny,
        "tradable_days_in_lookback": evaluation.tradable_days_in_lookback,
        "pit_free_float_market_cap_cny": pit_cap_cny,
        "size_multiplier": evaluation.size_multiplier,
        "adjusted_planned_notional_cny": evaluation.adjusted_planned_notional_cny,
        "reason_codes": ",".join(evaluation.reason_codes),
        "source_input_hash": src_hash,
    }


def _generate_expected_rows(
    *,
    trading_dates: list[date],
    ordinary_a_symbols: list[str],
    symbol_list_date: dict[str, date | None],
    symbol_delist_date: dict[str, date | None],
    stock_info: dict[str, dict[str, Any]],
    symbol_bars: dict[str, list[BarTuple]],
    symbol_valuation: dict[str, list[ValTuple]],
    all_trading_dates: list[date],
    market_snapshot_id: str,
    fund_snapshot_id: str,
    val_file_sha256: str,
    inventory_id: str,
    contract_id: str,
    alloc_id: str,
    policy: Any,
) -> Iterator[dict[str, Any]]:
    """Yield expected rows in deterministic order: as_of ASC, symbol ASC."""
    for as_of in trading_dates:
        decision_at = make_decision_at(as_of)
        candidates_on_date: list[str] = []
        for sym in ordinary_a_symbols:
            ld = symbol_list_date[sym]
            dd = symbol_delist_date[sym]
            if ld is None or ld > as_of:
                continue
            if dd is not None and dd <= as_of:
                continue
            candidates_on_date.append(sym)

        for sym in candidates_on_date:
            yield _generate_row_for_candidate(
                sym,
                as_of,
                decision_at=decision_at,
                stock_info=stock_info,
                symbol_bars=symbol_bars,
                symbol_valuation=symbol_valuation,
                all_trading_dates=all_trading_dates,
                market_snapshot_id=market_snapshot_id,
                fund_snapshot_id=fund_snapshot_id,
                val_file_sha256=val_file_sha256,
                inventory_id=inventory_id,
                contract_id=contract_id,
                alloc_id=alloc_id,
                policy=policy,
            )


# ---------------------------------------------------------------------------
# Production streaming verification helper (Finding #1 from review)
# ---------------------------------------------------------------------------


def _verify_expected_rows_streaming(
    *,
    parquet_path: Path,
    expected_iter: Iterator[dict[str, Any]],
    expected_schema: pa.Schema,
    expected_row_count: int,
    expected_year_counts: dict[int, int],
) -> bool:
    """Streaming sequential row verification (production helper).

    Reads stored parquet via iter_batches, compares each row against the
    expected_iter in deterministic order (as_of ASC, symbol ASC). Verifies:
    - Schema column names match expected_schema
    - Every field matches exactly
    - Row count and per-year counts match
    - No extra, missing, duplicate or reordered keys

    Raises ValueError on any mismatch. Returns True on success.
    """
    pf = pq.ParquetFile(str(parquet_path))

    # Schema check: exact equality of field order, names, and types
    stored_schema = pf.schema_arrow
    if not stored_schema.equals(expected_schema):
        raise ValueError(f"schema mismatch: stored={stored_schema}, expected={expected_schema}")

    stored_row_idx = 0
    year_counts: dict[int, int] = {2022: 0, 2023: 0, 2024: 0}

    for batch in pf.iter_batches():
        col_names = batch.schema.names
        for row_idx in range(batch.num_rows):
            stored_row: dict[str, Any] = {col: batch.column(col)[row_idx].as_py() for col in col_names}

            try:
                expected_row = next(expected_iter)
            except StopIteration:
                raise ValueError(
                    f"stored parquet has extra row at index {stored_row_idx}: "
                    f"({stored_row.get('symbol')}, {stored_row.get('as_of')})"
                ) from None

            for col_name in expected_schema.names:
                stored_val = stored_row.get(col_name)
                expected_val = expected_row.get(col_name)
                if stored_val != expected_val:
                    raise ValueError(
                        f"column '{col_name}' mismatch at row {stored_row_idx} "
                        f"({stored_row.get('symbol')}, {stored_row.get('as_of')}): "
                        f"stored={stored_val!r}, expected={expected_val!r}"
                    )

            as_of_str = stored_row.get("as_of", "")
            if isinstance(as_of_str, str) and len(as_of_str) >= 4:
                year = int(as_of_str[:4])
                if year in year_counts:
                    year_counts[year] += 1

            stored_row_idx += 1

    # Detect missing rows efficiently: check for at least one remaining
    try:
        next(expected_iter)
    except StopIteration:
        pass
    else:
        raise ValueError(f"stored parquet is missing expected rows after index {stored_row_idx}")

    if stored_row_idx != expected_row_count:
        raise ValueError(f"row count mismatch: expected={expected_row_count}, actual={stored_row_idx}")

    for year, expected_count in expected_year_counts.items():
        actual_count = year_counts.get(year, 0)
        if actual_count != expected_count:
            raise ValueError(f"year_{year} count mismatch: expected={expected_count}, actual={actual_count}")

    return True


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_candidate_eligibility_pack(
    *,
    repo_root: Path,
    output_dir: Path,
    progress_callback: Callable[[int, int, date], None] | None = None,
) -> CandidateEligibilityPackManifest:
    """Materialize the full candidate eligibility Parquet pack.

    Writes eligibility_verdicts.parquet and manifest.json atomically into
    output_dir. Refuses if output_dir already exists.
    """
    root = repo_root.resolve()
    out = _validate_safe_path(output_dir, repo_root=root, field_name="output_dir")

    if out.exists():
        raise FileExistsError(f"output directory already exists: {out}")

    pack_module_sha = _compute_pack_module_sha256()
    e10a_module_sha = _compute_e10a_module_sha256(root)
    _require_bound_e10a_sha(e10a_module_sha)

    inventory_path = root / BOUND_INVENTORY_PATH
    if not inventory_path.is_file():
        raise ValueError(f"inventory not found at {BOUND_INVENTORY_PATH}")
    inventory = verify_inventory(inventory_path, repo_root=root)
    if inventory.inventory_id is None:
        raise ValueError("inventory_id is None")
    _require_bound_inventory_id(inventory.inventory_id)

    market_slot: BoundSlot | None = None
    fund_slot: BoundSlot | None = None
    val_slot: BoundSlot | None = None
    for slot in inventory.slots:
        if not isinstance(slot, BoundSlot):
            continue
        if slot.kind == "sealed_market_snapshot":
            market_slot = slot
        elif slot.kind == "pit_fundamental_overlay":
            fund_slot = slot
        elif slot.kind == "pit_daily_valuation":
            val_slot = slot

    if market_slot is None or fund_slot is None or val_slot is None:
        raise ValueError("inventory missing required bound slots")

    market_dir = root / market_slot.repo_relative_path
    fund_dir = root / fund_slot.repo_relative_path

    raw_dir = (root / BOUND_RAW_COLLECTION_DIR).resolve()
    verify_raw_collection_strict(raw_dir, repo_root=root)

    contract_path = root / BOUND_TWO_LAYER_CONTRACT_PATH
    if not contract_path.is_file():
        raise ValueError(f"two-layer contract missing: {BOUND_TWO_LAYER_CONTRACT_PATH}")
    contract_sha = _sha256_file(contract_path)
    if contract_sha != BOUND_TWO_LAYER_CONTRACT_FILE_SHA256:
        raise ValueError("two-layer contract file SHA-256 drifted")
    _contract_id, _contract_rel, policy = bind_two_layer_eligibility_policy(repo_root=root)
    if _contract_id != BOUND_TWO_LAYER_CONTRACT_ID:
        raise ValueError("two-layer contract_id drifted")

    alloc_path = root / BOUND_ALLOCATION_PROTOCOL_PATH
    if not alloc_path.is_file():
        raise ValueError(f"allocation protocol missing: {BOUND_ALLOCATION_PROTOCOL_PATH}")
    alloc_sha = _sha256_file(alloc_path)
    if alloc_sha != BOUND_ALLOCATION_PROTOCOL_FILE_SHA256:
        raise ValueError("allocation protocol file SHA-256 drifted")
    alloc_protocol = load_layer_two_allocation_protocol(alloc_path)
    alloc_result = verify_layer_two_allocation_protocol(alloc_protocol)
    if alloc_result.protocol_id != BOUND_ALLOCATION_PROTOCOL_ID:
        raise ValueError(
            f"allocation protocol_id mismatch: expected {BOUND_ALLOCATION_PROTOCOL_ID}, got {alloc_result.protocol_id}"
        )

    all_trading_dates = _load_trade_cal_dates(raw_dir)
    trading_dates = [d for d in all_trading_dates if COVERAGE_START <= d <= COVERAGE_END]
    if not trading_dates:
        raise ValueError("no trading dates in coverage window")
    trading_dates.sort()

    stock_info = _load_stock_basic(raw_dir)
    symbol_bars = _load_daily_bars_index(market_dir)
    symbol_valuation = _load_daily_valuation_index(fund_dir)

    ordinary_a_symbols: list[str] = []
    for ts_code, info in stock_info.items():
        exchange = info.get("exchange")
        if not is_ordinary_a_share_from_stock_basic(ts_code, exchange):
            continue
        ordinary_a_symbols.append(ts_code)
    ordinary_a_symbols.sort()

    symbol_list_date: dict[str, date | None] = {}
    symbol_delist_date: dict[str, date | None] = {}
    for sym in ordinary_a_symbols:
        info = stock_info[sym]
        symbol_list_date[sym] = _parse_date_col(info.get("list_date"))
        symbol_delist_date[sym] = _parse_date_col(info.get("delist_date"))

    parent_dir = out.parent
    parent_dir.mkdir(parents=True, exist_ok=True)
    temp_dir_obj = tempfile.mkdtemp(dir=parent_dir, prefix=".pack_build_")
    temp_dir = Path(temp_dir_obj)
    try:
        parquet_tmp = temp_dir / "eligibility_verdicts.parquet"
        writer = pq.ParquetWriter(str(parquet_tmp), _OUTPUT_PARQUET_SCHEMA)

        # Finding #8: writer.close() in try/finally
        try:
            total_rows = 0
            year_counts: dict[int, int] = {2022: 0, 2023: 0, 2024: 0}
            total_dates = len(trading_dates)

            for date_idx, as_of in enumerate(trading_dates):
                if progress_callback is not None:
                    progress_callback(date_idx, total_dates, as_of)

                decision_at = make_decision_at(as_of)

                candidates_on_date: list[str] = []
                for sym in ordinary_a_symbols:
                    ld = symbol_list_date[sym]
                    dd = symbol_delist_date[sym]
                    if ld is not None and ld > as_of:
                        continue
                    if ld is None:
                        continue
                    if dd is not None and dd <= as_of:
                        continue
                    candidates_on_date.append(sym)

                batch_rows: list[dict[str, Any]] = []

                for sym in candidates_on_date:
                    row_dict = _generate_row_for_candidate(
                        sym,
                        as_of,
                        decision_at=decision_at,
                        stock_info=stock_info,
                        symbol_bars=symbol_bars,
                        symbol_valuation=symbol_valuation,
                        all_trading_dates=all_trading_dates,
                        market_snapshot_id=market_slot.snapshot_id,
                        fund_snapshot_id=fund_slot.snapshot_id,
                        val_file_sha256=val_slot.file_sha256,
                        inventory_id=BOUND_INVENTORY_ID,
                        contract_id=BOUND_TWO_LAYER_CONTRACT_ID,
                        alloc_id=BOUND_ALLOCATION_PROTOCOL_ID,
                        policy=policy,
                    )
                    batch_rows.append(row_dict)

                if batch_rows:
                    arrays: dict[str, list[Any]] = {col.name: [] for col in _OUTPUT_PARQUET_SCHEMA}
                    for r in batch_rows:
                        for col_name in arrays:
                            arrays[col_name].append(r[col_name])

                    batch = pa.record_batch(
                        [pa.array(arrays[col.name], type=col.type) for col in _OUTPUT_PARQUET_SCHEMA],
                        schema=_OUTPUT_PARQUET_SCHEMA,
                    )
                    writer.write_batch(batch)
                    total_rows += len(batch_rows)
                    year_counts[as_of.year] = year_counts.get(as_of.year, 0) + len(batch_rows)
        finally:
            writer.close()

        if total_rows == 0:
            raise ValueError("pack produced zero rows; cannot write empty pack")

        # Finding #1: compute canonical hash via streaming iter_batches
        parquet_sha = _sha256_file(parquet_tmp)
        pf = pq.ParquetFile(str(parquet_tmp))
        table_hash = compute_canonical_table_hash_streaming(pf)

        trading_date_set_hash = compute_trading_date_set_hash(trading_dates)

        source_binding = PackSourceBinding(
            inventory_path=BOUND_INVENTORY_PATH,
            inventory_id=BOUND_INVENTORY_ID,
            market_snapshot_id=market_slot.snapshot_id,
            market_manifest_sha256=market_slot.file_sha256,
            fundamental_snapshot_id=fund_slot.snapshot_id,
            fundamental_manifest_sha256=fund_slot.file_sha256,
            valuation_file_sha256=val_slot.file_sha256,
            raw_collection_dir=BOUND_RAW_COLLECTION_DIR,
            raw_collection_request_id=BOUND_RAW_COLLECTION_REQUEST_ID,
            raw_collection_manifest_sha256=BOUND_RAW_COLLECTION_MANIFEST_SHA256,
            raw_quality_report_sha256=BOUND_RAW_QUALITY_REPORT_SHA256,
            raw_dataset_hash_trade_cal=BOUND_RAW_DATASET_HASH_TRADE_CAL,
            raw_dataset_hash_stock_basic=BOUND_RAW_DATASET_HASH_STOCK_BASIC,
            raw_dataset_hash_namechange=BOUND_RAW_DATASET_HASH_NAMECHANGE,
            two_layer_contract_id=BOUND_TWO_LAYER_CONTRACT_ID,
            two_layer_contract_path=BOUND_TWO_LAYER_CONTRACT_PATH,
            two_layer_contract_file_sha256=BOUND_TWO_LAYER_CONTRACT_FILE_SHA256,
            allocation_protocol_id=BOUND_ALLOCATION_PROTOCOL_ID,
            allocation_protocol_path=BOUND_ALLOCATION_PROTOCOL_PATH,
            allocation_protocol_file_sha256=BOUND_ALLOCATION_PROTOCOL_FILE_SHA256,
            planned_buy_notional_cny=BOUND_PLANNED_BUY_NOTIONAL_CNY,
            e10a_engine_version=LAYER_TWO_CANDIDATE_ELIGIBILITY_ENGINE_VERSION,
            e10a_module_sha256=e10a_module_sha,
        )

        coverage = PackCoverageInfo(
            start=COVERAGE_START,
            end=COVERAGE_END,
            trading_date_count=len(trading_dates),
            trading_date_set_sha256=trading_date_set_hash,
        )

        row_counts = PackRowCounts(
            total=total_rows,
            year_2022=year_counts.get(2022, 0),
            year_2023=year_counts.get(2023, 0),
            year_2024=year_counts.get(2024, 0),
        )

        integrity = PackIntegrity(
            parquet_file_sha256=parquet_sha,
            canonical_table_sha256=table_hash,
            symbol_date_key_unique=True,
            row_count=total_rows,
        )

        readiness = PackReadinessFlags(
            research_only=True,
            ready_for_scoring=False,
            ready_for_trading=False,
            ready_for_portfolio_construction=False,
            not_alpha_evidence=True,
            not_authorization=True,
        )

        manifest = CandidateEligibilityPackManifest(
            schema_version=PACK_SCHEMA_VERSION,
            pack_version=PACK_ENGINE_VERSION,
            source_binding=source_binding,
            coverage=coverage,
            row_counts=row_counts,
            integrity=integrity,
            readiness=readiness,
            pack_module_sha256=pack_module_sha,
            e10a_module_sha256=e10a_module_sha,
        )
        sealed = seal_manifest(manifest)

        manifest_text = (
            json.dumps(
                sealed.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        manifest_tmp = temp_dir / "manifest.json"
        manifest_tmp.write_text(manifest_text, encoding="utf-8")

        temp_dir.rename(out)

    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return sealed


# ---------------------------------------------------------------------------
# Verifier (Finding #1: fully streaming, no DataFrame)
# ---------------------------------------------------------------------------


def verify_candidate_eligibility_pack(
    pack_dir: Path,
    *,
    repo_root: Path,
) -> CandidateEligibilityPackManifest:
    """Full recomputation verification of an existing pack (streaming)."""
    root = repo_root.resolve()
    pack = _validate_safe_path(pack_dir, repo_root=root, field_name="pack_dir")

    manifest_path = pack / "manifest.json"
    parquet_path = pack / "eligibility_verdicts.parquet"

    if not manifest_path.is_file():
        raise ValueError("manifest.json missing in pack dir")
    if not parquet_path.is_file():
        raise ValueError("eligibility_verdicts.parquet missing in pack dir")

    manifest_payload = json.loads(manifest_path.read_text("utf-8"))
    manifest = CandidateEligibilityPackManifest.model_validate(manifest_payload)

    if manifest.pack_id is None:
        raise ValueError("pack_id is missing (not sealed)")
    expected_id = compute_pack_id(manifest)
    if manifest.pack_id != expected_id:
        raise ValueError("pack_id does not match canonical content hash")

    current_pack_module_sha = _compute_pack_module_sha256()
    if current_pack_module_sha != manifest.pack_module_sha256:
        raise ValueError(
            f"pack module SHA-256 mismatch: manifest={manifest.pack_module_sha256}, current={current_pack_module_sha}"
        )
    current_e10a_sha = _compute_e10a_module_sha256(root)
    if current_e10a_sha != manifest.e10a_module_sha256:
        raise ValueError(
            f"E10a module SHA-256 mismatch: manifest={manifest.e10a_module_sha256}, current={current_e10a_sha}"
        )
    _require_bound_e10a_sha(current_e10a_sha)

    actual_parquet_sha = _sha256_file(parquet_path)
    if actual_parquet_sha != manifest.integrity.parquet_file_sha256:
        raise ValueError(
            f"Parquet file SHA-256 mismatch: manifest={manifest.integrity.parquet_file_sha256}, "
            f"actual={actual_parquet_sha}"
        )

    pf = pq.ParquetFile(str(parquet_path))
    actual_table_hash = compute_canonical_table_hash_streaming(pf)
    if actual_table_hash != manifest.integrity.canonical_table_sha256:
        raise ValueError("canonical table hash mismatch")

    # Full source verification
    inventory_path = root / BOUND_INVENTORY_PATH
    if not inventory_path.is_file():
        raise ValueError("inventory not found on disk")
    inventory = verify_inventory(inventory_path, repo_root=root)
    if inventory.inventory_id is None:
        raise ValueError("inventory_id is None")
    _require_bound_inventory_id(inventory.inventory_id)

    raw_dir = (root / BOUND_RAW_COLLECTION_DIR).resolve()
    if not raw_dir.is_dir():
        raise ValueError("raw collection dir missing")
    verify_raw_collection_strict(raw_dir, repo_root=root)

    contract_file = root / BOUND_TWO_LAYER_CONTRACT_PATH
    if not contract_file.is_file():
        raise ValueError("two-layer contract file missing")
    actual_contract_sha = _sha256_file(contract_file)
    if actual_contract_sha != manifest.source_binding.two_layer_contract_file_sha256:
        raise ValueError("two-layer contract file SHA on disk does not match source binding")

    alloc_file = root / BOUND_ALLOCATION_PROTOCOL_PATH
    if not alloc_file.is_file():
        raise ValueError("allocation protocol file missing")
    actual_alloc_sha = _sha256_file(alloc_file)
    if actual_alloc_sha != manifest.source_binding.allocation_protocol_file_sha256:
        raise ValueError("allocation protocol file SHA on disk does not match source binding")

    # Extract bound slots
    market_slot_v: BoundSlot | None = None
    fund_slot_v: BoundSlot | None = None
    val_slot_v: BoundSlot | None = None
    for slot in inventory.slots:
        if not isinstance(slot, BoundSlot):
            continue
        if slot.kind == "sealed_market_snapshot":
            market_slot_v = slot
        elif slot.kind == "pit_fundamental_overlay":
            fund_slot_v = slot
        elif slot.kind == "pit_daily_valuation":
            val_slot_v = slot

    if market_slot_v is None or fund_slot_v is None or val_slot_v is None:
        raise ValueError("inventory missing required bound slots for verification")

    if inventory.inventory_id is None:
        raise ValueError("inventory_id is None after verification")

    # Finding #4: construct expected source binding and require model equality
    expected_binding = PackSourceBinding(
        inventory_path=BOUND_INVENTORY_PATH,
        inventory_id=inventory.inventory_id,
        market_snapshot_id=market_slot_v.snapshot_id,
        market_manifest_sha256=market_slot_v.file_sha256,
        fundamental_snapshot_id=fund_slot_v.snapshot_id,
        fundamental_manifest_sha256=fund_slot_v.file_sha256,
        valuation_file_sha256=val_slot_v.file_sha256,
        raw_collection_dir=BOUND_RAW_COLLECTION_DIR,
        raw_collection_request_id=BOUND_RAW_COLLECTION_REQUEST_ID,
        raw_collection_manifest_sha256=BOUND_RAW_COLLECTION_MANIFEST_SHA256,
        raw_quality_report_sha256=BOUND_RAW_QUALITY_REPORT_SHA256,
        raw_dataset_hash_trade_cal=BOUND_RAW_DATASET_HASH_TRADE_CAL,
        raw_dataset_hash_stock_basic=BOUND_RAW_DATASET_HASH_STOCK_BASIC,
        raw_dataset_hash_namechange=BOUND_RAW_DATASET_HASH_NAMECHANGE,
        two_layer_contract_id=BOUND_TWO_LAYER_CONTRACT_ID,
        two_layer_contract_path=BOUND_TWO_LAYER_CONTRACT_PATH,
        two_layer_contract_file_sha256=BOUND_TWO_LAYER_CONTRACT_FILE_SHA256,
        allocation_protocol_id=BOUND_ALLOCATION_PROTOCOL_ID,
        allocation_protocol_path=BOUND_ALLOCATION_PROTOCOL_PATH,
        allocation_protocol_file_sha256=BOUND_ALLOCATION_PROTOCOL_FILE_SHA256,
        planned_buy_notional_cny=BOUND_PLANNED_BUY_NOTIONAL_CNY,
        e10a_engine_version=LAYER_TWO_CANDIDATE_ELIGIBILITY_ENGINE_VERSION,
        e10a_module_sha256=current_e10a_sha,
    )
    if manifest.source_binding != expected_binding:
        raise ValueError("source_binding does not match expected binding constructed from verified sources")

    # Verify coverage
    all_dates = _load_trade_cal_dates(raw_dir)
    window_dates = [d for d in all_dates if COVERAGE_START <= d <= COVERAGE_END]
    window_dates.sort()
    actual_date_hash = compute_trading_date_set_hash(window_dates)
    if actual_date_hash != manifest.coverage.trading_date_set_sha256:
        raise ValueError("trading date set hash mismatch")
    if len(window_dates) != manifest.coverage.trading_date_count:
        raise ValueError("trading date count mismatch")

    # Load sources for recomputation
    _, _, policy_v = bind_two_layer_eligibility_policy(repo_root=root)
    stock_info_v = _load_stock_basic(raw_dir)
    market_dir_v = root / market_slot_v.repo_relative_path
    fund_dir_v = root / fund_slot_v.repo_relative_path
    symbol_bars_v = _load_daily_bars_index(market_dir_v)
    symbol_valuation_v = _load_daily_valuation_index(fund_dir_v)

    ordinary_a_v: list[str] = []
    for ts_code, info in stock_info_v.items():
        exchange = info.get("exchange")
        if is_ordinary_a_share_from_stock_basic(ts_code, exchange):
            ordinary_a_v.append(ts_code)
    ordinary_a_v.sort()

    sym_ld: dict[str, date | None] = {}
    sym_dd: dict[str, date | None] = {}
    for sym in ordinary_a_v:
        info = stock_info_v[sym]
        sym_ld[sym] = _parse_date_col(info.get("list_date"))
        sym_dd[sym] = _parse_date_col(info.get("delist_date"))

    expected_iter = _generate_expected_rows(
        trading_dates=window_dates,
        ordinary_a_symbols=ordinary_a_v,
        symbol_list_date=sym_ld,
        symbol_delist_date=sym_dd,
        stock_info=stock_info_v,
        symbol_bars=symbol_bars_v,
        symbol_valuation=symbol_valuation_v,
        all_trading_dates=all_dates,
        market_snapshot_id=market_slot_v.snapshot_id,
        fund_snapshot_id=fund_slot_v.snapshot_id,
        val_file_sha256=val_slot_v.file_sha256,
        inventory_id=inventory.inventory_id,
        contract_id=BOUND_TWO_LAYER_CONTRACT_ID,
        alloc_id=BOUND_ALLOCATION_PROTOCOL_ID,
        policy=policy_v,
    )

    result = _verify_expected_rows_streaming(
        parquet_path=parquet_path,
        expected_iter=expected_iter,
        expected_schema=_OUTPUT_PARQUET_SCHEMA,
        expected_row_count=manifest.integrity.row_count,
        expected_year_counts={
            2022: manifest.row_counts.year_2022,
            2023: manifest.row_counts.year_2023,
            2024: manifest.row_counts.year_2024,
        },
    )
    assert result is True

    return manifest


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "BOUND_ALLOCATION_PROTOCOL_FILE_SHA256",
    "BOUND_ALLOCATION_PROTOCOL_ID",
    "BOUND_ALLOCATION_PROTOCOL_PATH",
    "BOUND_E10A_MODULE_SHA256",
    "BOUND_INVENTORY_ID",
    "BOUND_INVENTORY_PATH",
    "BOUND_PLANNED_BUY_NOTIONAL_CNY",
    "BOUND_RAW_COLLECTION_DIR",
    "BOUND_RAW_COLLECTION_MANIFEST_SHA256",
    "BOUND_RAW_COLLECTION_REQUEST_ID",
    "BOUND_RAW_DATASET_HASH_NAMECHANGE",
    "BOUND_RAW_DATASET_HASH_STOCK_BASIC",
    "BOUND_RAW_DATASET_HASH_TRADE_CAL",
    "BOUND_RAW_QUALITY_REPORT_SHA256",
    "BOUND_TWO_LAYER_CONTRACT_FILE_SHA256",
    "BOUND_TWO_LAYER_CONTRACT_ID",
    "BOUND_TWO_LAYER_CONTRACT_PATH",
    "BarTuple",
    "CIRC_MV_UNIT_TO_CNY",
    "COVERAGE_END",
    "COVERAGE_START",
    "CandidateEligibilityPackManifest",
    "PACK_ENGINE_VERSION",
    "PACK_SCHEMA_VERSION",
    "PackCoverageInfo",
    "PackIntegrity",
    "PackReadinessFlags",
    "PackRowCounts",
    "PackSourceBinding",
    "ValTuple",
    "_bisect_find_val",
    "_generate_expected_rows",
    "_require_bound_e10a_sha",
    "_require_bound_inventory_id",
    "_verify_expected_rows_streaming",
    "build_candidate_eligibility_pack",
    "canonical_manifest_bytes",
    "canonical_manifest_payload",
    "compute_canonical_table_hash",
    "compute_canonical_table_hash_streaming",
    "compute_pack_id",
    "compute_source_input_hash",
    "compute_trading_date_set_hash",
    "is_ordinary_a_share_from_stock_basic",
    "make_decision_at",
    "seal_manifest",
    "verify_candidate_eligibility_pack",
    "verify_raw_collection_strict",
]
