"""Verified, explicitly non-PIT membership overlay for public reconstructions.

The normal six-table snapshot keeps its strict ``available_at`` contract.  This
module deliberately does not add public third-party memberships to that table:
their historical decision-time availability has not been proven.  Instead it
verifies the separate BigQuant collection pack and exposes a date-keyed overlay
that is usable only by the ``public_reconstruction`` research scope.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import polars as pl

from app.errors import DataQualityError, PreflightError
from app.models.market import Instrument
from app.models.snapshot import DataSnapshot
from app.providers.tushare_normalize import require_ts_code
from app.research_scope import PUBLIC_RECONSTRUCTION_CLASSIFICATION
from app.storage.protocol import MarketStore

_MANIFEST_FILE = "collection_manifest.json"
_QUALITY_FILE = "quality_report.json"
_CANDIDATE_FILE = "candidate_membership.csv"
_RAW_COLUMNS = ("date", "instrument", "member_code", "member_name", "weight")
_CANDIDATE_COLUMNS = (
    "source_date",
    "index_code",
    "symbol",
    "weight",
    "source_member_name",
    "retrieved_at",
)


@dataclass(frozen=True)
class PublicReconstructionPack:
    """A verified public reconstruction collection, never a PIT assertion."""

    directory: Path
    collection_id: str
    source_name: str
    index_code: str
    retrieved_at: str
    coverage_start: date
    coverage_end: date
    expected_constituents: int
    memberships: pl.DataFrame


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataQualityError(f"public reconstruction {label} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise DataQualityError(f"public reconstruction {label} must be a JSON object")
    return value


def _required_text(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DataQualityError(f"public reconstruction {label} is missing")
    return text


def _utc_timestamp(value: object, label: str) -> str:
    """Canonicalize an ISO UTC timestamp without treating it as available_at."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _required_text(value, label).replace("z", "Z")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise DataQualityError(f"public reconstruction {label} is not an ISO timestamp") from exc
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None:
        raise DataQualityError(f"public reconstruction {label} must include UTC offset")
    if offset.total_seconds() != 0:
        raise DataQualityError(f"public reconstruction {label} must be UTC")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _within(root: Path, relative: str, label: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise DataQualityError(f"public reconstruction {label} escapes collection directory") from exc
    if not candidate.is_file():
        raise DataQualityError(f"public reconstruction {label} is missing")
    return candidate


def _as_date(value: object, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"public reconstruction {label} has invalid date") from exc


def _as_weight(value: object, label: str) -> float:
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"public reconstruction {label} has invalid weight") from exc
    if not number >= 0.0 or not number < float("inf"):
        raise DataQualityError(f"public reconstruction {label} has invalid weight")
    return number


def _read_candidate(path: Path, *, index_code: str, retrieved_at: str) -> pl.DataFrame:
    try:
        raw = pl.read_csv(path, try_parse_dates=True)
    except Exception as exc:  # noqa: BLE001
        raise DataQualityError("public reconstruction candidate_membership is not readable CSV") from exc
    missing = [name for name in _CANDIDATE_COLUMNS if name not in raw.columns]
    extra = [name for name in raw.columns if name not in _CANDIDATE_COLUMNS]
    if missing or extra:
        raise DataQualityError(
            "public reconstruction candidate_membership schema mismatch: "
            f"missing={missing}, extra={extra}"
        )
    rows: list[dict[str, object]] = []
    seen: set[tuple[date, str]] = set()
    for line, item in enumerate(raw.iter_rows(named=True), start=2):
        source_date = _as_date(item.get("source_date"), f"candidate source_date at line {line}")
        actual_index = _required_text(item.get("index_code"), f"candidate index_code at line {line}")
        if actual_index != index_code:
            raise DataQualityError(f"public reconstruction candidate has unexpected index_code at line {line}")
        symbol = require_ts_code(str(item.get("symbol") or ""), kind="stock")
        key = (source_date, symbol)
        if key in seen:
            raise DataQualityError(f"public reconstruction candidate has duplicate (source_date, symbol)={key}")
        seen.add(key)
        stamp = _utc_timestamp(item.get("retrieved_at"), f"candidate retrieved_at at line {line}")
        if stamp != retrieved_at:
            raise DataQualityError("public reconstruction candidate retrieved_at does not match collection manifest")
        rows.append(
            {
                "source_date": source_date,
                "symbol": symbol,
                "weight": _as_weight(item.get("weight"), f"candidate weight at line {line}"),
                "source_member_name": str(item.get("source_member_name") or "").strip(),
            }
        )
    if not rows:
        raise DataQualityError("public reconstruction candidate_membership has no rows")
    return pl.DataFrame(rows).with_columns(
        [pl.col("source_date").cast(pl.Date), pl.col("weight").cast(pl.Float64)]
    ).sort(["source_date", "symbol"])


def _membership_from_raw(path: Path, *, index_code: str) -> pl.DataFrame:
    try:
        raw = pl.read_csv(path, try_parse_dates=True)
    except Exception as exc:  # noqa: BLE001
        raise DataQualityError("public reconstruction raw response is not readable CSV") from exc
    missing = [name for name in _RAW_COLUMNS if name not in raw.columns]
    if missing:
        raise DataQualityError(f"public reconstruction raw response missing columns: {missing}")
    rows: list[dict[str, object]] = []
    seen: set[tuple[date, str]] = set()
    for line, item in enumerate(raw.iter_rows(named=True), start=2):
        source_date = _as_date(item.get("date"), f"raw date at line {line}")
        actual_index = _required_text(item.get("instrument"), f"raw instrument at line {line}")
        if actual_index != index_code:
            raise DataQualityError(f"public reconstruction raw response has unexpected instrument at line {line}")
        symbol = require_ts_code(str(item.get("member_code") or ""), kind="stock")
        key = (source_date, symbol)
        if key in seen:
            raise DataQualityError(f"public reconstruction raw response has duplicate (date, member_code)={key}")
        seen.add(key)
        rows.append(
            {
                "source_date": source_date,
                "symbol": symbol,
                "weight": _as_weight(item.get("weight"), f"raw weight at line {line}"),
                "source_member_name": str(item.get("member_name") or "").strip(),
            }
        )
    if not rows:
        raise DataQualityError("public reconstruction raw response has no rows")
    return pl.DataFrame(rows).with_columns(
        [pl.col("source_date").cast(pl.Date), pl.col("weight").cast(pl.Float64)]
    ).sort(["source_date", "symbol"])


def _assert_candidate_matches_raw(candidate: pl.DataFrame, raw: pl.DataFrame) -> None:
    if candidate.height != raw.height:
        raise DataQualityError("public reconstruction candidate row count does not match hashed raw response")
    left = candidate.select(["source_date", "symbol", "weight", "source_member_name"])
    if left.rows() != raw.select(left.columns).rows():
        raise DataQualityError("public reconstruction candidate does not match hashed raw response")


def _assert_complete(frame: pl.DataFrame, *, expected_constituents: int, quality: dict[str, Any]) -> None:
    if quality.get("schema") != "aiq.public_reconstruction_quality.v1":
        raise DataQualityError("public reconstruction quality report has unexpected schema")
    if quality.get("eligible_for_public_reconstruction") is not True:
        raise DataQualityError("public reconstruction quality report is not eligible")
    if int(quality.get("expected_constituents", 0)) != expected_constituents:
        raise DataQualityError("public reconstruction quality report expected_constituents mismatch")
    if int(quality.get("incomplete_dates", -1)) != 0 or list(quality.get("row_validation_errors", [])):
        raise DataQualityError("public reconstruction quality report records incomplete or invalid rows")
    grouped = frame.group_by("source_date").agg(pl.len().alias("count"))
    bad = grouped.filter(pl.col("count") != expected_constituents)
    if bad.height:
        first = bad.sort("source_date").head(1).to_dicts()[0]
        raise DataQualityError(
            "public reconstruction must have a complete daily cross-section; "
            f"source_date={first['source_date']} count={first['count']}"
        )
    if int(quality.get("source_dates", -1)) != grouped.height:
        raise DataQualityError("public reconstruction quality report source_dates mismatch")
    if int(quality.get("complete_dates", -1)) != grouped.height:
        raise DataQualityError("public reconstruction quality report complete_dates mismatch")


def load_public_reconstruction_pack(
    directory: Path,
    *,
    expected_constituents: int,
    index_code: str = "000300.SH",
) -> PublicReconstructionPack:
    """Verify a collection pack before it can enter a non-PIT simulation.

    This function intentionally verifies raw bytes and candidate rows separately;
    a syntactically valid edited candidate CSV must not silently alter a replay.
    """
    root = Path(directory).resolve()
    if not root.is_dir():
        raise PreflightError("public reconstruction directory is missing; set AIQ_PUBLIC_RECONSTRUCTION_DIR")
    manifest_path = root / _MANIFEST_FILE
    quality_path = root / _QUALITY_FILE
    candidate_path = root / _CANDIDATE_FILE
    if not manifest_path.is_file() or not quality_path.is_file() or not candidate_path.is_file():
        raise DataQualityError("public reconstruction collection directory is incomplete")
    manifest = _read_json(manifest_path, "collection manifest")
    if manifest.get("schema") != "aiq.public_reconstruction_collection.v1":
        raise DataQualityError("public reconstruction collection manifest has unexpected schema")
    if manifest.get("classification") != PUBLIC_RECONSTRUCTION_CLASSIFICATION:
        raise DataQualityError("public reconstruction collection is not explicitly classified non-PIT")
    actual_index = _required_text(manifest.get("query_index_code"), "query_index_code")
    if actual_index != index_code:
        raise DataQualityError(f"public reconstruction index_code '{actual_index}' does not match '{index_code}'")
    retrieved_at = _utc_timestamp(manifest.get("retrieved_at"), "retrieved_at")
    raw_info = manifest.get("raw_response")
    if not isinstance(raw_info, dict):
        raise DataQualityError("public reconstruction collection manifest has no raw_response")
    raw_path = _within(root, _required_text(raw_info.get("path"), "raw_response.path"), "raw response")
    expected_sha = _required_text(raw_info.get("sha256"), "raw_response.sha256")
    if _sha256(raw_path) != expected_sha:
        raise DataQualityError("public reconstruction raw response SHA-256 does not match collection manifest")
    candidate = _read_candidate(candidate_path, index_code=index_code, retrieved_at=retrieved_at)
    raw = _membership_from_raw(raw_path, index_code=index_code)
    _assert_candidate_matches_raw(candidate, raw)
    quality = _read_json(quality_path, "quality report")
    _assert_complete(candidate, expected_constituents=expected_constituents, quality=quality)
    coverage = manifest.get("requested_coverage")
    if not isinstance(coverage, dict):
        raise DataQualityError("public reconstruction collection manifest has no requested_coverage")
    coverage_start = _as_date(coverage.get("start"), "requested_coverage.start")
    coverage_end = _as_date(coverage.get("end"), "requested_coverage.end")
    actual_start = candidate["source_date"].min()
    actual_end = candidate["source_date"].max()
    if not isinstance(actual_start, date) or not isinstance(actual_end, date):
        raise DataQualityError("public reconstruction has no source-date coverage")
    if actual_start < coverage_start or actual_end > coverage_end:
        raise DataQualityError("public reconstruction candidate lies outside requested coverage")
    collection_id = hashlib.sha256(
        "\n".join(_sha256(path) for path in (manifest_path, quality_path, candidate_path, raw_path)).encode("utf-8")
    ).hexdigest()
    return PublicReconstructionPack(
        directory=root,
        collection_id=collection_id,
        source_name=_required_text(manifest.get("source_name"), "source_name"),
        index_code=index_code,
        retrieved_at=retrieved_at,
        coverage_start=actual_start,
        coverage_end=actual_end,
        expected_constituents=expected_constituents,
        memberships=candidate.select(["source_date", "symbol", "weight"]).sort(["source_date", "symbol"]),
    )


class PublicReconstructionStore:
    """Read-only market store with an explicitly non-PIT member overlay."""

    def __init__(self, base: MarketStore, pack: PublicReconstructionPack, *, universe_id: str) -> None:
        self._base = base
        self._pack = pack
        self._universe_id = universe_id
        known = {item.symbol for item in base.get_instruments() if not item.is_index and not item.is_global}
        missing = sorted({str(value) for value in pack.memberships["symbol"].to_list()} - known)
        if missing:
            raise DataQualityError(
                "market snapshot does not contain every public reconstruction member, e.g. "
                f"{missing[:3]}"
            )

    @property
    def public_reconstruction_id(self) -> str:
        return self._pack.collection_id

    def get_instruments(self) -> list[Instrument]:
        return self._base.get_instruments()

    def get_calendar(self, start: date, end: date) -> list[date]:
        return self._base.get_calendar(start, end)

    def get_daily_bars(self, as_of: date, symbol: str | None = None, start: date | None = None) -> pl.DataFrame:
        return self._base.get_daily_bars(as_of=as_of, symbol=symbol, start=start)

    def get_index_bars(self, as_of: date, symbol: str | None = None, start: date | None = None) -> pl.DataFrame:
        return self._base.get_index_bars(as_of=as_of, symbol=symbol, start=start)

    def get_global_bars(self, as_of: date, symbol: str | None = None, start: date | None = None) -> pl.DataFrame:
        return self._base.get_global_bars(as_of=as_of, symbol=symbol, start=start)

    def get_universe_members(
        self,
        universe_id: str,
        as_of: date,
        available_by: datetime,
        *,
        expected_constituents: int | None = None,
        require_available_cross_section: bool = False,
    ) -> set[str]:
        del available_by
        if universe_id != self._universe_id:
            raise DataQualityError(
                f"public reconstruction universe_id '{universe_id}' does not match '{self._universe_id}'"
            )
        if require_available_cross_section:
            raise DataQualityError("public reconstruction cannot satisfy a historical available_at requirement")
        rows = self._pack.memberships.filter(pl.col("source_date") == as_of)
        if rows.is_empty():
            raise DataQualityError(
                "public reconstruction missing complete source-date cross-section for "
                f"universe_id={universe_id} as_of_date={as_of}; refusing to reuse another day's members"
            )
        expected = expected_constituents if expected_constituents is not None else self._pack.expected_constituents
        if rows.height != expected:
            raise DataQualityError(
                f"public reconstruction expected_constituents={expected} but source_date={as_of} "
                f"has {rows.height} members"
            )
        return {str(value) for value in rows["symbol"].to_list()}

    def next_trading_day(self, after: date) -> date | None:
        return self._base.next_trading_day(after)

    def trading_days_after(self, after: date, n: int) -> list[date]:
        return self._base.trading_days_after(after, n)

    def snapshot(self) -> DataSnapshot:
        return self._base.snapshot()


def export_public_reconstruction_symbols(pack: PublicReconstructionPack, output: Path) -> Path:
    """Write the unique pack members for a base-market-data download.

    The resulting file is explicitly a download convenience, not a historical
    CSI300 membership file.  Refusing to overwrite makes the selected raw-data
    universe auditable as well.
    """
    target = Path(output)
    if target.exists():
        raise DataQualityError(f"public reconstruction symbols output already exists: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    symbols = sorted({str(value) for value in pack.memberships["symbol"].to_list()})
    target.write_text("\n".join(symbols) + "\n", encoding="utf-8")
    return target
