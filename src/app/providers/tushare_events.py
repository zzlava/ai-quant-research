from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Any, cast
from zoneinfo import ZoneInfo

import polars as pl

from app.errors import DataQualityError
from app.models.events import EVENT_TABLE_NAMES
from app.providers.tushare_normalize import require_ts_code
from app.storage.fundamental_io import require_no_conflicting_duplicates, source_row_hash

EVENT_AVAILABILITY_POLICY = (
    "Tushare event ann_date at 23:59 Asia/Shanghai; date-only publication is first usable "
    "at the next decision after that timestamp"
)

FORECAST_NUMERIC = (
    "p_change_min",
    "p_change_max",
    "net_profit_min",
    "net_profit_max",
    "last_parent_net",
)
EXPRESS_NUMERIC = (
    "revenue",
    "operate_profit",
    "total_profit",
    "n_income",
    "total_assets",
    "total_hldr_eqy_exc_min_int",
    "diluted_eps",
    "diluted_roe",
    "yoy_net_profit",
    "bps",
    "yoy_sales",
    "yoy_op",
    "yoy_tp",
    "yoy_dedu_np",
    "yoy_eps",
    "yoy_roe",
    "growth_assets",
    "yoy_equity",
    "growth_bps",
    "or_last_year",
    "op_last_year",
    "tp_last_year",
    "np_last_year",
    "eps_last_year",
    "open_net_assets",
    "open_bps",
)

EVENT_COLUMNS: dict[str, tuple[str, ...]] = {
    "earnings_forecast_events": (
        "symbol",
        "report_period",
        "ann_date",
        "available_at",
        "forecast_type",
        *FORECAST_NUMERIC,
        "first_ann_date",
        "summary",
        "change_reason",
        "source_row_hash",
    ),
    "earnings_express_events": (
        "symbol",
        "report_period",
        "ann_date",
        "available_at",
        *EXPRESS_NUMERIC,
        "summary",
        "source_row_hash",
    ),
    "holder_count_events": (
        "symbol",
        "end_date",
        "ann_date",
        "available_at",
        "holder_num",
        "source_row_hash",
    ),
    "share_unlock_events": (
        "symbol",
        "float_date",
        "ann_date",
        "available_at",
        "float_share",
        "float_ratio",
        "holder_name",
        "share_type",
        "source_row_hash",
    ),
    "audit_opinion_events": (
        "symbol",
        "report_period",
        "ann_date",
        "available_at",
        "audit_result",
        "audit_fees",
        "audit_agency",
        "audit_sign",
        "source_row_hash",
    ),
}

SOURCE_TO_TABLE = {
    "forecast": "earnings_forecast_events",
    "express": "earnings_express_events",
    "stk_holdernumber": "holder_count_events",
    "share_float": "share_unlock_events",
    "fina_audit": "audit_opinion_events",
}

LOGICAL_KEYS: dict[str, tuple[str, ...]] = {
    "earnings_forecast_events": ("symbol", "report_period", "ann_date"),
    "earnings_express_events": ("symbol", "report_period", "ann_date"),
    "holder_count_events": ("symbol", "end_date", "ann_date"),
    "share_unlock_events": (
        "symbol",
        "float_date",
        "ann_date",
        "holder_name",
        "share_type",
        "float_share",
        "float_ratio",
    ),
    "audit_opinion_events": ("symbol", "report_period", "ann_date"),
}


def normalize_event_sources(raw: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    missing = sorted(set(SOURCE_TO_TABLE) - set(raw))
    if missing:
        raise DataQualityError(f"event source is missing required endpoints: {missing}")
    return {
        "earnings_forecast_events": normalize_earnings_forecast(raw["forecast"]),
        "earnings_express_events": normalize_earnings_express(raw["express"]),
        "holder_count_events": normalize_holder_count(raw["stk_holdernumber"]),
        "share_unlock_events": normalize_share_unlock(raw["share_float"]),
        "audit_opinion_events": normalize_audit_opinion(raw["fina_audit"]),
    }


def normalize_earnings_forecast(raw: pl.DataFrame) -> pl.DataFrame:
    required = (
        "ts_code",
        "ann_date",
        "end_date",
        "type",
        *FORECAST_NUMERIC,
        "first_ann_date",
        "summary",
        "change_reason",
    )
    _require_source(raw, required, "forecast")
    rows: list[dict[str, object]] = []
    hash_columns = (
        "symbol",
        "report_period",
        "ann_date",
        "forecast_type",
        *FORECAST_NUMERIC,
        "first_ann_date",
        "summary",
        "change_reason",
    )
    for line, item in enumerate(raw.iter_rows(named=True), start=1):
        ann_date = _parse_ymd(item.get("ann_date"), "ann_date", line)
        first_ann = _optional_date(item.get("first_ann_date"), "first_ann_date", line)
        if first_ann is not None and first_ann > ann_date:
            raise DataQualityError(f"forecast first_ann_date exceeds ann_date at source row {line}")
        row: dict[str, object] = {
            "symbol": require_ts_code(str(item.get("ts_code") or ""), kind="stock"),
            "report_period": _parse_ymd(item.get("end_date"), "end_date", line),
            "ann_date": ann_date,
            "available_at": _available_at(ann_date),
            "forecast_type": _required_text(item.get("type"), "type", line),
            "first_ann_date": first_ann,
            "summary": _optional_text(item.get("summary")),
            "change_reason": _optional_text(item.get("change_reason")),
        }
        for name in FORECAST_NUMERIC:
            row[name] = _optional_number(item.get(name), name, line)
        row["source_row_hash"] = source_row_hash(row, hash_columns)
        rows.append(row)
    frame = _cast_frame(
        rows,
        date_columns=("report_period", "ann_date", "first_ann_date"),
        numeric_columns=FORECAST_NUMERIC,
    ).select(EVENT_COLUMNS["earnings_forecast_events"])
    return require_no_conflicting_duplicates(
        frame,
        key=["symbol", "report_period", "ann_date"],
        table="earnings_forecast_events",
    )


def normalize_earnings_express(raw: pl.DataFrame) -> pl.DataFrame:
    required = ("ts_code", "ann_date", "end_date", *EXPRESS_NUMERIC)
    _require_source(raw, required, "express")
    if "perf_summary" not in raw.columns and "summary" not in raw.columns:
        raise DataQualityError(
            "express missing required summary column: expected official perf_summary"
        )
    rows: list[dict[str, object]] = []
    hash_columns = (
        "symbol",
        "report_period",
        "ann_date",
        *EXPRESS_NUMERIC,
        "summary",
    )
    for line, item in enumerate(raw.iter_rows(named=True), start=1):
        ann_date = _parse_ymd(item.get("ann_date"), "ann_date", line)
        row: dict[str, object] = {
            "symbol": require_ts_code(str(item.get("ts_code") or ""), kind="stock"),
            "report_period": _parse_ymd(item.get("end_date"), "end_date", line),
            "ann_date": ann_date,
            "available_at": _available_at(ann_date),
            "summary": _express_summary(item, line),
        }
        for name in EXPRESS_NUMERIC:
            row[name] = _optional_number(item.get(name), name, line)
        row["source_row_hash"] = source_row_hash(row, hash_columns)
        rows.append(row)
    frame = _cast_frame(
        rows,
        date_columns=("report_period", "ann_date"),
        numeric_columns=EXPRESS_NUMERIC,
    ).select(EVENT_COLUMNS["earnings_express_events"])
    return require_no_conflicting_duplicates(
        frame,
        key=["symbol", "report_period", "ann_date"],
        table="earnings_express_events",
    )


def _express_summary(item: dict[str, Any], line: int) -> str | None:
    official = _optional_text(item.get("perf_summary"))
    legacy = _optional_text(item.get("summary"))
    if official is not None and legacy is not None and official != legacy:
        raise DataQualityError(
            f"express perf_summary conflicts with legacy summary at source row {line}"
        )
    return official if official is not None else legacy


def normalize_holder_count(raw: pl.DataFrame) -> pl.DataFrame:
    _require_source(raw, ("ts_code", "ann_date", "end_date", "holder_num"), "stk_holdernumber")
    rows: list[dict[str, object]] = []
    hash_columns = ("symbol", "end_date", "ann_date", "holder_num")
    for line, item in enumerate(raw.iter_rows(named=True), start=1):
        # Tushare documents holder_num as int, but historical source rows can
        # contain a blank value. Preserve those rows in the raw export and
        # quality report; they cannot form a canonical holder-count event.
        if _is_missing_source_value(item.get("holder_num")):
            continue
        ann_date = _parse_ymd(item.get("ann_date"), "ann_date", line)
        holder_num = _required_int(item.get("holder_num"), "holder_num", line)
        if holder_num <= 0:
            raise DataQualityError(f"holder_num must be positive at source row {line}")
        row: dict[str, object] = {
            "symbol": require_ts_code(str(item.get("ts_code") or ""), kind="stock"),
            "end_date": _parse_ymd(item.get("end_date"), "end_date", line),
            "ann_date": ann_date,
            "available_at": _available_at(ann_date),
            "holder_num": holder_num,
        }
        row["source_row_hash"] = source_row_hash(row, hash_columns)
        rows.append(row)
    if not rows:
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "end_date": pl.Date,
                "ann_date": pl.Date,
                "available_at": pl.Datetime("us"),
                "holder_num": pl.Int64,
                "source_row_hash": pl.String,
            }
        ).select(EVENT_COLUMNS["holder_count_events"])
    frame = _cast_frame(
        rows,
        date_columns=("end_date", "ann_date"),
        integer_columns=("holder_num",),
    ).select(EVENT_COLUMNS["holder_count_events"])
    return require_no_conflicting_duplicates(
        frame,
        key=["symbol", "end_date", "ann_date"],
        table="holder_count_events",
    )


def normalize_share_unlock(raw: pl.DataFrame) -> pl.DataFrame:
    required = (
        "ts_code",
        "ann_date",
        "float_date",
        "float_share",
        "float_ratio",
        "holder_name",
        "share_type",
    )
    _require_source(raw, required, "share_float")
    rows: list[dict[str, object]] = []
    hash_columns = (
        "symbol",
        "float_date",
        "ann_date",
        "float_share",
        "float_ratio",
        "holder_name",
        "share_type",
    )
    for line, item in enumerate(raw.iter_rows(named=True), start=1):
        ann_date = _parse_ymd(item.get("ann_date"), "ann_date", line)
        float_share = _required_number(item.get("float_share"), "float_share", line)
        if float_share < 0:
            raise DataQualityError(f"float_share cannot be negative at source row {line}")
        float_ratio = _optional_number(item.get("float_ratio"), "float_ratio", line)
        if float_ratio is not None and float_ratio < 0:
            raise DataQualityError(f"float_ratio cannot be negative at source row {line}")
        row: dict[str, object] = {
            "symbol": require_ts_code(str(item.get("ts_code") or ""), kind="stock"),
            "float_date": _parse_ymd(item.get("float_date"), "float_date", line),
            "ann_date": ann_date,
            "available_at": _available_at(ann_date),
            "float_share": float_share,
            "float_ratio": float_ratio,
            "holder_name": _required_text(item.get("holder_name"), "holder_name", line),
            "share_type": _required_text(item.get("share_type"), "share_type", line),
        }
        row["source_row_hash"] = source_row_hash(row, hash_columns)
        rows.append(row)
    frame = _cast_frame(
        rows,
        date_columns=("float_date", "ann_date"),
        numeric_columns=("float_share", "float_ratio"),
    ).select(EVENT_COLUMNS["share_unlock_events"])
    return require_no_conflicting_duplicates(
        frame,
        # Tushare can publish multiple unlock tranches for the same holder,
        # type, announcement date, and unlock date. It provides no tranche ID;
        # the announced amount is therefore part of the source-grain identity.
        # Preserve both instead of silently choosing one amount.
        key=[
            "symbol",
            "float_date",
            "ann_date",
            "holder_name",
            "share_type",
            "float_share",
            "float_ratio",
        ],
        table="share_unlock_events",
    )


def normalize_audit_opinion(raw: pl.DataFrame) -> pl.DataFrame:
    required = (
        "ts_code",
        "ann_date",
        "end_date",
        "audit_result",
        "audit_fees",
        "audit_agency",
        "audit_sign",
    )
    _require_source(raw, required, "fina_audit")
    rows: list[dict[str, object]] = []
    hash_columns = (
        "symbol",
        "report_period",
        "ann_date",
        "audit_result",
        "audit_fees",
        "audit_agency",
        "audit_sign",
    )
    for line, item in enumerate(raw.iter_rows(named=True), start=1):
        ann_date = _parse_ymd(item.get("ann_date"), "ann_date", line)
        row: dict[str, object] = {
            "symbol": require_ts_code(str(item.get("ts_code") or ""), kind="stock"),
            "report_period": _parse_ymd(item.get("end_date"), "end_date", line),
            "ann_date": ann_date,
            "available_at": _available_at(ann_date),
            "audit_result": _required_text(item.get("audit_result"), "audit_result", line),
            "audit_fees": _optional_number(item.get("audit_fees"), "audit_fees", line),
            "audit_agency": _optional_text(item.get("audit_agency")),
            "audit_sign": _optional_text(item.get("audit_sign")),
        }
        row["source_row_hash"] = source_row_hash(row, hash_columns)
        rows.append(row)
    frame = _cast_frame(
        rows,
        date_columns=("report_period", "ann_date"),
        numeric_columns=("audit_fees",),
    ).select(EVENT_COLUMNS["audit_opinion_events"])
    return require_no_conflicting_duplicates(
        frame,
        key=["symbol", "report_period", "ann_date"],
        table="audit_opinion_events",
    )


def validate_event_tables(tables: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    if set(tables) != set(EVENT_TABLE_NAMES):
        raise DataQualityError(
            f"event overlay tables must contain exactly {list(EVENT_TABLE_NAMES)}"
        )
    validated: dict[str, pl.DataFrame] = {}
    for table in EVENT_TABLE_NAMES:
        frame = tables[table]
        expected_columns = EVENT_COLUMNS[table]
        if tuple(frame.columns) != expected_columns:
            raise DataQualityError(
                f"{table} columns do not match the executable schema; "
                f"expected {list(expected_columns)}"
            )
        if frame.is_empty():
            raise DataQualityError(f"{table} contains no rows")
        if frame.select(LOGICAL_KEYS[table]).is_duplicated().any():
            raise DataQualityError(f"{table} contains duplicate logical keys")
        for line, item in enumerate(frame.iter_rows(named=True), start=1):
            _validate_event_row_semantics(table, item, line)
            ann_date = item.get("ann_date")
            if not isinstance(ann_date, date):
                raise DataQualityError(f"{table} ann_date is invalid at row {line}")
            if item.get("available_at") != _available_at(ann_date):
                raise DataQualityError(
                    f"{table} available_at violates the date-only policy at row {line}"
                )
            expected_hash = source_row_hash(item, _hash_columns(table))
            if item.get("source_row_hash") != expected_hash:
                raise DataQualityError(f"{table} source_row_hash mismatch at row {line}")
        validated[table] = frame
    return validated


def _validate_event_row_semantics(
    table: str,
    item: dict[str, Any],
    line: int,
) -> None:
    require_ts_code(str(item.get("symbol") or ""), kind="stock")
    date_columns = {
        "earnings_forecast_events": ("report_period", "ann_date"),
        "earnings_express_events": ("report_period", "ann_date"),
        "holder_count_events": ("end_date", "ann_date"),
        "share_unlock_events": ("float_date", "ann_date"),
        "audit_opinion_events": ("report_period", "ann_date"),
    }[table]
    for name in date_columns:
        if not isinstance(item.get(name), date):
            raise DataQualityError(f"{table} {name} is invalid at row {line}")
    numeric_columns = {
        "earnings_forecast_events": FORECAST_NUMERIC,
        "earnings_express_events": EXPRESS_NUMERIC,
        "holder_count_events": (),
        "share_unlock_events": ("float_share", "float_ratio"),
        "audit_opinion_events": ("audit_fees",),
    }[table]
    for name in numeric_columns:
        value = item.get(name)
        if value is not None and (
            not isinstance(value, int | float) or not math.isfinite(float(value))
        ):
            raise DataQualityError(f"{table} {name} is invalid at row {line}")
    if table == "earnings_forecast_events":
        first_ann = item.get("first_ann_date")
        ann_date = item["ann_date"]
        if first_ann is not None and (
            not isinstance(first_ann, date) or first_ann > ann_date
        ):
            raise DataQualityError(
                f"earnings_forecast_events first_ann_date is invalid at row {line}"
            )
        _required_text(item.get("forecast_type"), "forecast_type", line)
    elif table == "holder_count_events":
        holder_num = item.get("holder_num")
        if not isinstance(holder_num, int) or holder_num <= 0:
            raise DataQualityError(f"holder_count_events holder_num is invalid at row {line}")
    elif table == "share_unlock_events":
        float_share = item.get("float_share")
        float_ratio = item.get("float_ratio")
        if not isinstance(float_share, int | float) or float_share < 0:
            raise DataQualityError(f"share_unlock_events float_share is invalid at row {line}")
        if float_ratio is not None and (
            not isinstance(float_ratio, int | float) or float_ratio < 0
        ):
            raise DataQualityError(f"share_unlock_events float_ratio is invalid at row {line}")
        _required_text(item.get("holder_name"), "holder_name", line)
        _required_text(item.get("share_type"), "share_type", line)
    elif table == "audit_opinion_events":
        _required_text(item.get("audit_result"), "audit_result", line)


def _hash_columns(table: str) -> tuple[str, ...]:
    return tuple(
        name
        for name in EVENT_COLUMNS[table]
        if name not in {"available_at", "source_row_hash"}
    )


def _require_source(raw: pl.DataFrame, required: tuple[str, ...], table: str) -> None:
    if raw.is_empty():
        raise DataQualityError(f"{table} returned no rows")
    missing = sorted(set(required) - set(raw.columns))
    if missing:
        raise DataQualityError(f"{table} missing required columns: {missing}")


def _cast_frame(
    rows: list[dict[str, object]],
    *,
    date_columns: tuple[str, ...],
    numeric_columns: tuple[str, ...] = (),
    integer_columns: tuple[str, ...] = (),
) -> pl.DataFrame:
    return pl.DataFrame(rows, infer_schema_length=None).with_columns(
        [
            *[pl.col(name).cast(pl.Date) for name in date_columns],
            pl.col("available_at").cast(pl.Datetime("us")),
            *[pl.col(name).cast(pl.Float64) for name in numeric_columns],
            *[pl.col(name).cast(pl.Int64) for name in integer_columns],
        ]
    )


def _parse_ymd(value: object, name: str, line: int) -> date:
    text = str(value or "").strip().replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise DataQualityError(f"{name} is invalid at source row {line}") from exc


def _optional_date(value: object, name: str, line: int) -> date | None:
    if value is None or str(value).strip() in {"", "nan", "None", "null"}:
        return None
    return _parse_ymd(value, name, line)


def _available_at(value: date) -> datetime:
    local = datetime.combine(value, time(23, 59), tzinfo=ZoneInfo("Asia/Shanghai"))
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _required_text(value: object, name: str, line: int) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        raise DataQualityError(f"{name} is blank at source row {line}")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.lower() in {"nan", "none", "null"} else text


def _optional_number(value: object, name: str, line: int) -> float | None:
    if _is_missing_source_value(value):
        return None
    return _required_number(value, name, line)


def _required_number(value: object, name: str, line: int) -> float:
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"{name} is not numeric at source row {line}") from exc
    if not math.isfinite(number):
        raise DataQualityError(f"{name} is not finite at source row {line}")
    return number


def _required_int(value: object, name: str, line: int) -> int:
    number = _required_number(value, name, line)
    if not number.is_integer():
        raise DataQualityError(f"{name} is not an integer at source row {line}")
    return int(number)


def _is_missing_source_value(value: object) -> bool:
    return value is None or str(value).strip().lower() in {"", "nan", "none", "null"}
