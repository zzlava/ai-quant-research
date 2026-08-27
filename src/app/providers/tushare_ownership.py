from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Any, cast
from zoneinfo import ZoneInfo

import polars as pl

from app.errors import DataQualityError
from app.providers.tushare_normalize import require_ts_code
from app.storage.fundamental_io import source_row_hash

OWNERSHIP_AVAILABILITY_POLICY = (
    "Tushare top10_floatholders ann_date at 23:59 Asia/Shanghai; "
    "conservative date-only publication clock"
)
OWNERSHIP_FIELDS = (
    "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio,"
    "hold_float_ratio,hold_change,holder_type"
)


def normalize_top10_float_holders(raw: pl.DataFrame) -> pl.DataFrame:
    if raw.is_empty():
        raise DataQualityError("top10_floatholders returned no rows")
    required = (
        "ts_code",
        "ann_date",
        "end_date",
        "holder_name",
        "hold_float_ratio",
        "holder_type",
    )
    missing = [name for name in required if name not in raw.columns]
    if missing:
        raise DataQualityError(f"top10_floatholders missing required columns: {missing}")
    rows: list[dict[str, object]] = []
    for line, item in enumerate(raw.iter_rows(named=True), start=1):
        holder_name = str(item.get("holder_name") or "").strip()
        holder_type = str(item.get("holder_type") or "").strip()
        if not holder_name:
            raise DataQualityError(f"holder_name is blank at source row {line}")
        # An absent classification is preserved as unknown. The feature layer
        # rejects the whole ten-holder group instead of inventing a type.
        report_period = _parse_ymd(item.get("end_date"), "end_date", line)
        ann_date = _parse_ymd(item.get("ann_date"), "ann_date", line)
        hold_amount = _optional_number(item.get("hold_amount"), "hold_amount", line)
        hold_ratio = _optional_number(item.get("hold_ratio"), "hold_ratio", line)
        ratio = _optional_number(item.get("hold_float_ratio"), "hold_float_ratio", line)
        hold_change = _optional_number(item.get("hold_change"), "hold_change", line)
        row: dict[str, object] = {
            "symbol": require_ts_code(str(item.get("ts_code") or ""), kind="stock"),
            "report_period": report_period,
            "ann_date": ann_date,
            "available_at": _available_at(ann_date),
            "holder_name": holder_name,
            "holder_type": holder_type,
            "hold_amount": hold_amount,
            "hold_ratio": hold_ratio,
            "hold_float_ratio": ratio,
            "hold_change": hold_change,
        }
        row["source_row_hash"] = source_row_hash(
            row,
            (
                "symbol",
                "report_period",
                "ann_date",
                "holder_name",
                "holder_type",
                "hold_amount",
                "hold_ratio",
                "hold_float_ratio",
                "hold_change",
            ),
        )
        rows.append(row)
    frame = pl.DataFrame(rows).with_columns(
        pl.col("report_period").cast(pl.Date),
        pl.col("ann_date").cast(pl.Date),
        pl.col("available_at").cast(pl.Datetime("us")),
        pl.col("hold_amount").cast(pl.Float64),
        pl.col("hold_ratio").cast(pl.Float64),
        pl.col("hold_float_ratio").cast(pl.Float64),
        pl.col("hold_change").cast(pl.Float64),
    )
    # Byte-equivalent source rows may repeat in an API response. Conflicting
    # variants for one named holder are deliberately retained: the feature
    # layer will invalidate that entire disclosure group instead of choosing a
    # value that the source does not identify as authoritative.
    canonical = frame.unique(subset=["source_row_hash"], keep="first")
    validate_top10_float_holders(canonical)
    return canonical


def validate_top10_float_holders(frame: pl.DataFrame) -> None:
    required = {
        "symbol",
        "report_period",
        "ann_date",
        "available_at",
        "holder_name",
        "holder_type",
        "hold_amount",
        "hold_ratio",
        "hold_float_ratio",
        "hold_change",
        "source_row_hash",
    }
    missing = sorted(required - set(frame.columns))
    if frame.is_empty() or missing:
        raise DataQualityError(
            "top10_float_holders canonical table is empty or incomplete: "
            f"missing={missing}"
        )
    if frame.select(pl.col("source_row_hash").str.contains(r"^[0-9a-f]{64}$").all()).item() is not True:
        raise DataQualityError("top10_float_holders has invalid source_row_hash")
    for field in ("hold_amount", "hold_ratio", "hold_float_ratio", "hold_change"):
        if frame.select(
            (pl.col(field).is_null() | pl.col(field).is_finite()).all()
        ).item() is not True:
            raise DataQualityError(f"top10_float_holders has non-finite {field}")
    for line, row in enumerate(frame.iter_rows(named=True), start=1):
        symbol = require_ts_code(str(row.get("symbol") or ""), kind="stock")
        report_period = row.get("report_period")
        ann_date = row.get("ann_date")
        available_at = row.get("available_at")
        holder_name = str(row.get("holder_name") or "").strip()
        if not isinstance(report_period, date) or not isinstance(ann_date, date):
            raise DataQualityError(f"top10_float_holders has invalid dates at row {line}")
        if ann_date < report_period:
            raise DataQualityError(
                f"top10_float_holders announcement predates report period at row {line}"
            )
        if available_at != _available_at(ann_date):
            raise DataQualityError(
                f"top10_float_holders available_at violates the policy at row {line}"
            )
        if not holder_name:
            raise DataQualityError(f"top10_float_holders holder_name is blank at row {line}")
        expected_hash = source_row_hash(
            {
                "symbol": symbol,
                "report_period": report_period,
                "ann_date": ann_date,
                "holder_name": holder_name,
                "holder_type": str(row.get("holder_type") or "").strip(),
                "hold_amount": (
                    float(cast(Any, row["hold_amount"]))
                    if row.get("hold_amount") is not None
                    else None
                ),
                "hold_ratio": (
                    float(cast(Any, row["hold_ratio"]))
                    if row.get("hold_ratio") is not None
                    else None
                ),
                "hold_float_ratio": (
                    float(cast(Any, row["hold_float_ratio"]))
                    if row.get("hold_float_ratio") is not None
                    else None
                ),
                "hold_change": (
                    float(cast(Any, row["hold_change"]))
                    if row.get("hold_change") is not None
                    else None
                ),
            },
            (
                "symbol",
                "report_period",
                "ann_date",
                "holder_name",
                "holder_type",
                "hold_amount",
                "hold_ratio",
                "hold_float_ratio",
                "hold_change",
            ),
        )
        if row.get("source_row_hash") != expected_hash:
            raise DataQualityError(
                f"top10_float_holders source_row_hash mismatch at row {line}"
            )
    duplicates = frame.group_by("source_row_hash").len().filter(pl.col("len") != 1)
    if duplicates.height:
        raise DataQualityError("top10_float_holders has duplicate exact source rows")


def _parse_ymd(value: object, name: str, line: int) -> date:
    text = str(value or "").strip().replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise DataQualityError(f"{name} is invalid at source row {line}") from exc


def _optional_number(value: object, name: str, line: int) -> float | None:
    if value is None or str(value).strip() in {"", "None", "nan", "NaN", "--"}:
        return None
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"{name} is not numeric at source row {line}") from exc
    if not math.isfinite(number):
        raise DataQualityError(f"{name} is not finite at source row {line}")
    return number


def _available_at(value: date) -> datetime:
    local = datetime.combine(value, time(23, 59), tzinfo=ZoneInfo("Asia/Shanghai"))
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
