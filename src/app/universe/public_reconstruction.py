"""Collection and quality checks for a clearly non-PIT public CSI300 reconstruction.

This module intentionally never writes ``available_at``, ``effective_from``, a
PIT manifest, or a six-table market snapshot.  A historical query made today
cannot establish what was available to a decision maker on the historical date.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isfinite
from pathlib import Path
from typing import Any, cast

import polars as pl

from app.errors import BigQuantFetchError, DataQualityError
from app.providers.bigquant_client import BigQuantQueryClient
from app.providers.tushare_normalize import require_ts_code

BIGQUANT_WEIGHT_TABLE = "cn_stock_index_weight"
BIGQUANT_SOURCE_URL = "https://bigquant.com/data/datasources/cn_stock_index_weight"
RAW_REQUIRED_COLUMNS = ("date", "instrument", "member_code", "member_name", "weight")
CANDIDATE_COLUMNS = (
    "source_date",
    "index_code",
    "symbol",
    "weight",
    "source_member_name",
    "retrieved_at",
)


@dataclass(frozen=True)
class PublicReconstructionResult:
    output_dir: Path
    raw_response_path: Path
    candidate_membership_path: Path | None
    collection_manifest_path: Path
    quality_report_path: Path
    raw_rows: int
    source_dates: int
    complete_dates: int
    incomplete_dates: int
    eligible_for_public_reconstruction: bool


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_source_date(value: object, *, row_number: int) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"BigQuant source date is invalid at row {row_number}") from exc


def _finite_weight(value: object, *, row_number: int) -> float:
    try:
        weight = float(cast(str | float | int, value))
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"BigQuant weight is invalid at row {row_number}") from exc
    if not isfinite(weight) or weight < 0:
        raise DataQualityError(f"BigQuant weight is invalid at row {row_number}")
    return weight


def _query_weights(
    client: BigQuantQueryClient,
    *,
    index_code: str,
    start: date,
    end: date,
) -> pl.DataFrame:
    sql = (
        "SELECT date, instrument, name, member_code, member_name, weight "
        f"FROM {BIGQUANT_WEIGHT_TABLE} "
        f"WHERE instrument = '{index_code}' "
        f"AND date >= '{start.isoformat()}' AND date <= '{end.isoformat()}' "
        "ORDER BY date, member_code"
    )
    try:
        return client.query(sql, filters={"date": [start.isoformat(), end.isoformat()]})
    except BigQuantFetchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BigQuantFetchError("BigQuant component-weight query failed") from exc


def _prepare_output_dir(path: Path) -> Path:
    output = Path(path)
    if output.exists():
        raise DataQualityError(
            f"public reconstruction output directory already exists: {output.name}; refusing to overwrite it"
        )
    output.mkdir(parents=True)
    (output / "source_documents").mkdir()
    return output


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collect_bigquant_public_reconstruction(
    *,
    client: BigQuantQueryClient,
    start: date,
    end: date,
    output_dir: Path,
    index_code: str = "000300.SH",
    expected_constituents: int = 300,
    retrieved_at: datetime | None = None,
) -> PublicReconstructionResult:
    """Save a public third-party candidate data pack with a separate quality report.

    The returned candidate is intentionally not in the project's PIT membership
    schema.  ``retrieved_at`` records collection time only and is never treated
    as historical ``available_at``.
    """
    if end < start:
        raise DataQualityError("end date must be on or after start date")
    if expected_constituents <= 0:
        raise DataQualityError("expected_constituents must be positive")
    if index_code != "000300.SH":
        raise DataQualityError("public reconstruction currently supports only index_code=000300.SH")

    raw = _query_weights(client, index_code=index_code, start=start, end=end)
    missing = [column for column in RAW_REQUIRED_COLUMNS if column not in raw.columns]
    if missing:
        raise DataQualityError(f"BigQuant response missing required columns: {missing}")
    if raw.is_empty():
        raise DataQualityError("BigQuant response has no rows for the requested window")

    output = _prepare_output_dir(output_dir)
    stamp = (retrieved_at or datetime.now(UTC)).astimezone(UTC)
    raw_path = output / "source_documents" / "bigquant_cn_stock_index_weight.csv"
    raw.write_csv(raw_path)

    rows: list[dict[str, object]] = []
    errors: list[str] = []
    seen: set[tuple[date, str]] = set()
    for row_number, item in enumerate(raw.iter_rows(named=True), start=2):
        try:
            source_date = _parse_source_date(item.get("date"), row_number=row_number)
            instrument = str(item.get("instrument") or "").strip()
            if instrument != index_code:
                raise DataQualityError(
                    f"BigQuant response has unexpected instrument '{instrument}' at row {row_number}"
                )
            symbol = require_ts_code(str(item.get("member_code") or ""), kind="stock")
            key = (source_date, symbol)
            if key in seen:
                raise DataQualityError(
                    f"BigQuant response duplicate component (date, symbol)=({source_date.isoformat()}, {symbol})"
                )
            seen.add(key)
            weight = _finite_weight(item.get("weight"), row_number=row_number)
        except DataQualityError as exc:
            errors.append(str(exc))
            continue
        rows.append(
            {
                "source_date": source_date,
                "index_code": index_code,
                "symbol": symbol,
                "weight": weight,
                "source_member_name": str(item.get("member_name") or "").strip() or None,
                "retrieved_at": stamp.isoformat().replace("+00:00", "Z"),
            }
        )

    date_summary: list[dict[str, object]] = []
    if rows:
        candidate = pl.DataFrame(rows).select(list(CANDIDATE_COLUMNS)).sort(["source_date", "symbol"])
        summary = candidate.group_by("source_date").agg(
            pl.len().alias("constituent_count"), pl.col("weight").sum().alias("weight_sum")
        )
        for item in summary.sort("source_date").iter_rows(named=True):
            count = int(item["constituent_count"])
            weight_sum = float(item["weight_sum"])
            if abs(weight_sum - 100.0) < 0.01:
                weight_sum_scale = "near_100"
            elif abs(weight_sum - 1.0) < 0.0001:
                weight_sum_scale = "near_1"
            else:
                weight_sum_scale = "unrecognized"
            date_summary.append(
                {
                    "source_date": item["source_date"].isoformat(),
                    "constituent_count": count,
                    "weight_sum": weight_sum,
                    "weight_sum_scale": weight_sum_scale,
                    "complete": count == expected_constituents,
                }
            )
    else:
        candidate = pl.DataFrame(schema={column: pl.String for column in CANDIDATE_COLUMNS})

    incomplete = [item for item in date_summary if not bool(item["complete"])]
    if errors:
        candidate_path: Path | None = None
    else:
        candidate_path = output / "candidate_membership.csv"
        candidate.write_csv(candidate_path)

    quality = {
        "schema": "aiq.public_reconstruction_quality.v1",
        "intended_use": (
            "public third-party reconstruction research only; "
            "not licensed or point-in-time historical membership"
        ),
        "raw_row_count": raw.height,
        "source_dates": len(date_summary),
        "expected_constituents": expected_constituents,
        "complete_dates": len(date_summary) - len(incomplete),
        "incomplete_dates": len(incomplete),
        "row_validation_errors": errors,
        "dates": date_summary,
        "eligible_for_public_reconstruction": not errors and not incomplete,
        "not_proven": [
            "historical available_at",
            "historical decision-time availability",
            "licensed point-in-time provenance",
        ],
    }
    quality_path = output / "quality_report.json"
    _write_json(quality_path, quality)

    manifest = {
        "schema": "aiq.public_reconstruction_collection.v1",
        "classification": "public_reconstructed_not_licensed_pit",
        "source_name": "BigQuant cn_stock_index_weight",
        "source_url": BIGQUANT_SOURCE_URL,
        "source_table": BIGQUANT_WEIGHT_TABLE,
        "query_index_code": index_code,
        "requested_coverage": {"start": start.isoformat(), "end": end.isoformat()},
        "retrieved_at": stamp.isoformat().replace("+00:00", "Z"),
        "raw_response": {
            "path": str(raw_path.relative_to(output)),
            "sha256": _sha256(raw_path),
            "row_count": raw.height,
        },
        "candidate_membership": str(candidate_path.relative_to(output)) if candidate_path else None,
        "quality_report": str(quality_path.relative_to(output)),
        "availability_boundary": (
            "retrieved_at is the collection time only. It must not be copied to available_at, "
            "effective_from, or a PIT membership manifest."
        ),
    }
    manifest_path = output / "collection_manifest.json"
    _write_json(manifest_path, manifest)

    return PublicReconstructionResult(
        output_dir=output,
        raw_response_path=raw_path,
        candidate_membership_path=candidate_path,
        collection_manifest_path=manifest_path,
        quality_report_path=quality_path,
        raw_rows=raw.height,
        source_dates=len(date_summary),
        complete_dates=len(date_summary) - len(incomplete),
        incomplete_dates=len(incomplete),
        eligible_for_public_reconstruction=not errors and not incomplete,
    )
