"""Official CSI 1 Bond history contract, collection, and materialization."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.request import Request, urlopen

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.repo_file_safety import resolve_repo_regular_file

DEFAULT_CONTRACT_PATH = Path("config/research/csi-1-bond-defensive-leg-contract-v1.json")
DEFAULT_STAGING_DIR = Path("data/raw/csi-1-bond-2005-2024-v1")
DEFAULT_SNAPSHOT_DIR = Path("data/research/csi-1-bond-2005-2024-v1")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceEvidence(_StrictModel):
    url: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CalendarBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_rows: Literal[4858]
    coverage_start: Literal["2005-01-04"]
    coverage_end: Literal["2024-12-31"]


class DefensiveLegContract(_StrictModel):
    schema_version: Literal["1"]
    contract_version: Literal["csi-1-bond-defensive-leg-contract-v1"]
    contract_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_as_of: date
    index_code: Literal["H11010"]
    index_name_cn: Literal["中证1债指数"]
    index_name_en: Literal["CSI 1 Bond Index"]
    role: Literal["short_duration_defensive_research_proxy"]
    return_definition: Literal["full_price_plus_coupon_reinvestment"]
    remaining_maturity_rule: Literal["one_month_or_more_and_less_than_one_year"]
    eligible_credit_floor: Literal["BBB_or_above"]
    credit_risk_is_nonzero: Literal[True]
    duration_risk_is_nonzero: Literal[True]
    assumed_fixed_carry_forbidden: Literal[True]
    official_sources: dict[str, SourceEvidence]
    calendar_binding: CalendarBinding
    staging_dir: Literal["data/raw/csi-1-bond-2005-2024-v1"]
    snapshot_dir: Literal["data/research/csi-1-bond-2005-2024-v1"]
    exact_live_tracking_product_found_on_official_index_page: Literal[False]
    ready_for_index_level_historical_replay: Literal[True]
    ready_for_live_product_mapping: Literal[False]
    ready_for_orders: Literal[False]
    ready_for_trading: Literal[False]

    @model_validator(mode="after")
    def _fail_closed(self) -> DefensiveLegContract:
        if self.sealed_as_of != date(2026, 8, 27):
            raise ValueError("defensive-leg contract date drifted")
        if set(self.official_sources) != {"history", "methodology", "factsheet", "base_info"}:
            raise ValueError("defensive-leg official source set drifted")
        return self


class BytesClient(Protocol):
    def fetch(self, url: str) -> bytes: ...


class OfficialCSIBytesClient:
    def fetch(self, url: str) -> bytes:
        allowed = (
            "https://www.csindex.com.cn/csindex-home/",
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/",
        )
        if not url.startswith(allowed):
            raise ValueError("defensive-leg source URL is outside official CSI hosts")
        request = Request(url, headers={"User-Agent": "ai-quant-research/0.1"})
        with urlopen(request, timeout=60) as response:  # noqa: S310
            payload = response.read(16 * 1024 * 1024 + 1)
        if len(payload) > 16 * 1024 * 1024:
            raise ValueError("official CSI response exceeds sealed size limit")
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


def _contract_id(contract: DefensiveLegContract) -> str:
    payload = contract.model_dump(mode="json", exclude={"contract_id"})
    return _json_hash(payload)


def verify_defensive_leg_contract(
    *, repo_root: Path, path: Path = DEFAULT_CONTRACT_PATH
) -> DefensiveLegContract:
    root = Path(repo_root).resolve(strict=True)
    resolved = resolve_repo_regular_file(path, repo_root=root, field_name="contract_path")
    try:
        contract = DefensiveLegContract.model_validate_json(resolved.read_text())
    except Exception as exc:
        raise ValueError("defensive-leg contract is missing or invalid") from exc
    if contract.contract_id != _contract_id(contract):
        raise ValueError("defensive-leg contract self-hash mismatch")
    calendar = resolve_repo_regular_file(
        Path(contract.calendar_binding.path), repo_root=root, field_name="calendar_binding.path"
    )
    if _sha256_file(calendar) != contract.calendar_binding.sha256:
        raise ValueError("defensive-leg bound calendar hash mismatch")
    return contract


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


def _atomic_parquet(path: Path, frame: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    try:
        frame.write_parquet(temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _parse_history(payload: bytes, *, calendar: pl.DataFrame) -> pl.DataFrame:
    try:
        envelope = json.loads(payload)
    except Exception as exc:
        raise ValueError("official CSI defensive history is invalid JSON") from exc
    if envelope.get("code") != "200" or envelope.get("success") is not True:
        raise ValueError("official CSI defensive history request did not succeed")
    rows = envelope.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("official CSI defensive history has no rows")
    expected_dates = calendar.get_column("date").to_list()
    by_date: dict[date, float] = {}
    extra_dates: set[date] = set()
    expected_set = set(expected_dates)
    for row in rows:
        if not isinstance(row, dict) or row.get("indexCode") != "H11010":
            raise ValueError("official CSI defensive history contains wrong index identity")
        try:
            trade_date = datetime.strptime(str(row["tradeDate"]), "%Y%m%d").date()
            close = float(row["close"])
        except Exception as exc:
            raise ValueError("official CSI defensive history row is invalid") from exc
        if not close > 0.0:
            raise ValueError("official CSI defensive history close must be positive")
        if trade_date in expected_set:
            if trade_date in by_date:
                raise ValueError("official CSI defensive history has duplicate calendar date")
            by_date[trade_date] = close
        else:
            extra_dates.add(trade_date)
    if extra_dates != {date(2005, 1, 1)}:
        raise ValueError("official CSI defensive history unexpected off-calendar dates")
    if set(by_date) != expected_set:
        raise ValueError("official CSI defensive history does not completely cover equity calendar")
    return pl.DataFrame(
        {
            "date": expected_dates,
            "close": [by_date[item] for item in expected_dates],
            "available_at": [
                datetime.combine(item, time(7, 0), tzinfo=UTC) for item in expected_dates
            ],
        },
        schema={"date": pl.Date, "close": pl.Float64, "available_at": pl.Datetime("us", "UTC")},
    )


def materialize_official_defensive_leg_history(
    *,
    repo_root: Path,
    client: BytesClient | None = None,
    contract_path: Path = DEFAULT_CONTRACT_PATH,
) -> dict[str, Any]:
    root = Path(repo_root).resolve(strict=True)
    contract = verify_defensive_leg_contract(repo_root=root, path=contract_path)
    calendar_path = root / contract.calendar_binding.path
    calendar = pl.read_parquet(calendar_path)
    if calendar.columns != ["date"] or calendar.height != contract.calendar_binding.expected_rows:
        raise ValueError("defensive-leg bound calendar schema or row count drifted")
    source = contract.official_sources["history"]
    raw_path = root / contract.staging_dir / "raw" / "H11010-20050101-20241231.json"
    if raw_path.exists():
        payload = raw_path.read_bytes()
    else:
        payload = (client or OfficialCSIBytesClient()).fetch(source.url)
        if _sha256_bytes(payload) != source.sha256:
            raise ValueError("official CSI defensive history hash mismatch")
        _atomic_bytes(raw_path, payload)
    if _sha256_bytes(payload) != source.sha256:
        raise ValueError("sealed defensive history raw bytes drifted")
    frame = _parse_history(payload, calendar=calendar)
    snapshot_dir = root / contract.snapshot_dir
    table_path = snapshot_dir / "total_return_index.parquet"
    if table_path.exists():
        existing = pl.read_parquet(table_path)
        if not existing.equals(frame):
            raise ValueError("existing defensive snapshot differs from sealed source")
    else:
        _atomic_parquet(table_path, frame)
    source_manifest_payload = {
        "schema_version": "1",
        "source_name": "official_csi_1_bond_history_v1",
        "contract_id": contract.contract_id,
        "index_code": contract.index_code,
        "history_url": source.url,
        "history_sha256": source.sha256,
        "collection_time_is_not_historical_available_at": True,
        "availability_policy": "trade-date 15:00 Asia/Shanghai encoded as 07:00Z; T+1 action only",
        "no_interpolation_or_forward_fill": True,
        "ready_for_orders": False,
        "ready_for_trading": False,
    }
    source_manifest = {
        **source_manifest_payload,
        "source_manifest_id": _json_hash(source_manifest_payload),
    }
    source_manifest_path = root / contract.staging_dir / "source_manifest.json"
    if source_manifest_path.exists():
        if json.loads(source_manifest_path.read_text()) != source_manifest:
            raise ValueError("existing defensive source manifest drifted")
    else:
        _atomic_json(source_manifest_path, source_manifest)
    manifest_payload = {
        "schema_version": "1",
        "snapshot_version": "csi-1-bond-history-snapshot-v1",
        "contract_id": contract.contract_id,
        "source_manifest_sha256": _sha256_file(source_manifest_path),
        "raw_history_sha256": _sha256_file(raw_path),
        "table_sha256": _sha256_file(table_path),
        "rows": frame.height,
        "coverage_start": str(frame.get_column("date").min()),
        "coverage_end": str(frame.get_column("date").max()),
        "return_definition": contract.return_definition,
        "ready_for_index_level_historical_replay": True,
        "ready_for_live_product_mapping": False,
        "ready_for_orders": False,
        "ready_for_trading": False,
    }
    manifest = {**manifest_payload, "snapshot_id": _json_hash(manifest_payload)}
    manifest_path = snapshot_dir / "manifest.json"
    if manifest_path.exists():
        if json.loads(manifest_path.read_text()) != manifest:
            raise ValueError("existing defensive snapshot manifest drifted")
    else:
        _atomic_json(manifest_path, manifest)
    return verify_official_defensive_leg_history(repo_root=root, contract_path=contract_path)


def verify_official_defensive_leg_history(
    *, repo_root: Path, contract_path: Path = DEFAULT_CONTRACT_PATH
) -> dict[str, Any]:
    root = Path(repo_root).resolve(strict=True)
    contract = verify_defensive_leg_contract(repo_root=root, path=contract_path)
    raw_path = root / contract.staging_dir / "raw" / "H11010-20050101-20241231.json"
    source_manifest_path = root / contract.staging_dir / "source_manifest.json"
    table_path = root / contract.snapshot_dir / "total_return_index.parquet"
    manifest_path = root / contract.snapshot_dir / "manifest.json"
    for path in (raw_path, source_manifest_path, table_path, manifest_path):
        if not path.is_file() or path.is_symlink():
            raise ValueError("defensive-leg artifact is missing or unsafe")
    if _sha256_file(raw_path) != contract.official_sources["history"].sha256:
        raise ValueError("defensive-leg raw history hash mismatch")
    manifest = json.loads(manifest_path.read_text())
    payload = {key: value for key, value in manifest.items() if key != "snapshot_id"}
    if manifest.get("snapshot_id") != _json_hash(payload):
        raise ValueError("defensive-leg snapshot self-hash mismatch")
    if manifest.get("contract_id") != contract.contract_id:
        raise ValueError("defensive-leg snapshot contract binding mismatch")
    if manifest.get("source_manifest_sha256") != _sha256_file(source_manifest_path):
        raise ValueError("defensive-leg source manifest hash mismatch")
    if manifest.get("table_sha256") != _sha256_file(table_path):
        raise ValueError("defensive-leg table hash mismatch")
    calendar = pl.read_parquet(root / contract.calendar_binding.path)
    frame = _parse_history(raw_path.read_bytes(), calendar=calendar)
    if not pl.read_parquet(table_path).equals(frame):
        raise ValueError("defensive-leg table is not a full recomputation of sealed raw bytes")
    return manifest


__all__ = [
    "DEFAULT_CONTRACT_PATH",
    "DEFAULT_SNAPSHOT_DIR",
    "DEFAULT_STAGING_DIR",
    "DefensiveLegContract",
    "materialize_official_defensive_leg_history",
    "verify_defensive_leg_contract",
    "verify_official_defensive_leg_history",
]
