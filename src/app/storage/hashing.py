from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from app.models.snapshot import RAW_PLUS_ADJUSTED_PRICE_BASIS, SCHEMA_VERSION, TABLE_NAMES, DataSnapshot

HASH_COLUMNS: dict[str, tuple[str, ...]] = {
    "daily_bars": (
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turnover_rate",
        "is_st",
        "is_suspended",
        "price_limit_pct",
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
        "adj_factor",
        "pre_close",
        "up_limit",
        "down_limit",
    ),
    "index_bars": (
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turnover_rate",
        "is_st",
        "is_suspended",
        "price_limit_pct",
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
        "adj_factor",
        "pre_close",
        "up_limit",
        "down_limit",
    ),
    "global_bars": ("symbol", "date", "close", "available_at"),
    "instruments": (
        "symbol",
        "name",
        "sector",
        "listing_date",
        "is_index",
        "is_global",
        "market",
        "timezone",
        "session_close",
    ),
    "calendar": ("date",),
    "universe_membership": ("universe_id", "as_of_date", "symbol", "available_at", "weight"),
    "fundamental_reports": (
        "symbol",
        "report_period",
        "ann_date",
        "available_at",
        "update_flag",
        "roe",
        "roic",
        "grossprofit_margin",
        "debt_to_assets",
        "ocf_to_or",
        "q_sales_yoy",
        "q_netprofit_yoy",
        "dt_netprofit_yoy",
        "source_row_hash",
    ),
    "daily_valuation": (
        "symbol",
        "date",
        "available_at",
        "turnover_rate",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "total_mv",
        "circ_mv",
        "source_row_hash",
    ),
    "top10_float_holders": (
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
    ),
    "earnings_forecast_events": (
        "symbol",
        "report_period",
        "ann_date",
        "available_at",
        "forecast_type",
        "p_change_min",
        "p_change_max",
        "net_profit_min",
        "net_profit_max",
        "last_parent_net",
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

SORT_KEYS: dict[str, tuple[str, ...]] = {
    "daily_bars": ("symbol", "date"),
    "index_bars": ("symbol", "date"),
    "global_bars": ("symbol", "date"),
    "instruments": ("symbol",),
    "calendar": ("date",),
    "universe_membership": ("universe_id", "as_of_date", "symbol"),
    "fundamental_reports": ("symbol", "report_period", "ann_date", "update_flag", "source_row_hash"),
    "daily_valuation": ("symbol", "date"),
    "top10_float_holders": (
        "symbol",
        "report_period",
        "ann_date",
        "holder_name",
        "source_row_hash",
    ),
    "earnings_forecast_events": (
        "symbol",
        "report_period",
        "ann_date",
        "source_row_hash",
    ),
    "earnings_express_events": (
        "symbol",
        "report_period",
        "ann_date",
        "source_row_hash",
    ),
    "holder_count_events": ("symbol", "end_date", "ann_date", "source_row_hash"),
    "share_unlock_events": (
        "symbol",
        "float_date",
        "ann_date",
        "holder_name",
        "share_type",
        "source_row_hash",
    ),
    "audit_opinion_events": (
        "symbol",
        "report_period",
        "ann_date",
        "source_row_hash",
    ),
}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonicalize_table(frame: pl.DataFrame, table: str) -> str:
    columns = HASH_COLUMNS[table]
    keys = SORT_KEYS[table]
    present = [col for col in columns if col in frame.columns]
    if frame.is_empty() or not present:
        return ""
    work = frame.select(present)
    sort_cols = [col for col in keys if col in work.columns]
    if sort_cols:
        work = work.sort(sort_cols)
    lines: list[str] = []
    for row in work.iter_rows(named=True):
        parts = [_encode_field(_canon_cell(row.get(col), col)) for col in columns]
        lines.append("".join(parts))
    return "\n".join(lines) + "\n"


def hash_table(frame: pl.DataFrame, table: str) -> str:
    columns = HASH_COLUMNS[table]
    keys = SORT_KEYS[table]
    present = [col for col in columns if col in frame.columns]
    if frame.is_empty() or not present:
        return sha256_text("")
    work = frame.select(present)
    sort_cols = [col for col in keys if col in work.columns]
    if sort_cols:
        work = work.sort(sort_cols)
    digest = hashlib.sha256()
    for row in work.iter_rows(named=True):
        digest.update(
            "".join(
                _encode_field(_canon_cell(row.get(col), col)) for col in columns
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def combine_hashes(
    table_hashes: dict[str, str],
    adjustment: str,
    price_basis: str,
    schema_version: str = SCHEMA_VERSION,
) -> str:
    parts = [
        f"schema_version={schema_version}",
        f"adjustment={adjustment}",
        f"price_basis={price_basis}",
    ]
    for name in TABLE_NAMES:
        parts.append(f"{name}={table_hashes.get(name, sha256_text(''))}")
    return sha256_text("\n".join(parts) + "\n")


def build_snapshot(
    tables: dict[str, pl.DataFrame],
    *,
    adjustment: str,
    price_basis: str = RAW_PLUS_ADJUSTED_PRICE_BASIS,
    source_name: str,
    fetched_at: datetime | None = None,
    market_index: str | None = None,
    global_symbol: str | None = None,
    source_version: str | None = None,
) -> DataSnapshot:
    table_hashes = {name: hash_table(tables.get(name, pl.DataFrame()), name) for name in TABLE_NAMES}
    content_hash = combine_hashes(table_hashes, adjustment, price_basis)
    calendar = tables.get("calendar")
    coverage_start, coverage_end = _coverage(calendar)
    fetched = fetched_at or datetime.now(UTC).replace(tzinfo=None)
    return DataSnapshot(
        snapshot_id=content_hash,
        schema_version=SCHEMA_VERSION,
        table_hashes=table_hashes,
        content_hash=content_hash,
        source_name=source_name,
        fetched_at=fetched.strftime("%Y-%m-%dT%H:%M:%S"),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        adjustment=adjustment,
        price_basis=price_basis,
        row_counts={
            name: int(tables[name].height) if name in tables and tables[name] is not None else 0
            for name in TABLE_NAMES
        },
        market_index=market_index,
        global_symbol=global_symbol,
        source_version=source_version,
    )


def _coverage(calendar: pl.DataFrame | None) -> tuple[date | None, date | None]:
    if calendar is None or calendar.is_empty() or "date" not in calendar.columns:
        return None, None
    values = [v for v in calendar["date"].to_list() if isinstance(v, date)]
    if not values:
        return None, None
    return min(values), max(values)


def _canon_cell(value: Any, column: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if column == "available_at" or isinstance(value, datetime):
        return _canon_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int | float):
        return float(value).hex()
    return str(value)


def _encode_field(text: str) -> str:
    return f"{len(text.encode('utf-8'))}:{text}"


def _canon_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone(UTC).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    text = str(value).replace("Z", "")
    if "+" in text[10:]:
        text = text.split("+", 1)[0]
    if "." in text:
        text = text.split(".", 1)[0]
    return text
