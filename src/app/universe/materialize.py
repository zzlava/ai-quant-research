from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from app.clock import decision_at_utc
from app.errors import DataQualityError
from app.models.config import StrategyConfig
from app.providers._frames import UNIVERSE_MEMBERSHIP_SCHEMA
from app.providers.tushare_normalize import require_ts_code
from app.storage.quality import parse_available_at_utc, validate_universe_membership
from app.universe.membership import _optional_float, _require_date, _require_text

SNAPSHOT_REQUIRED = ("universe_id", "effective_from", "symbol", "available_at")
SNAPSHOT_COLUMNS = (*SNAPSHOT_REQUIRED, "weight")
CALENDAR_REQUIRED = ("date",)


@dataclass(frozen=True)
class SnapshotCrossSection:
    universe_id: str
    effective_from: date
    available_at: datetime
    members: tuple[tuple[str, float | None], ...]


@dataclass(frozen=True)
class MaterializeResult:
    frame: pl.DataFrame
    path: Path
    universe_id: str
    trading_days: int
    members_per_day: str
    row_count: int
    snapshot_count: int


def read_trade_calendar_file(path: Path) -> list[date]:
    source = Path(path)
    if not source.is_file():
        raise DataQualityError(f"trade calendar file not found: {source.name}")
    try:
        raw = pl.read_csv(source, try_parse_dates=True)
    except Exception as exc:
        raise DataQualityError("trade calendar file is not a readable CSV") from exc
    missing = [col for col in CALENDAR_REQUIRED if col not in raw.columns]
    if missing:
        raise DataQualityError(f"trade calendar missing required columns: {missing}")
    extra = [col for col in raw.columns if col not in CALENDAR_REQUIRED]
    if extra:
        raise DataQualityError(f"trade calendar has unknown columns: {extra}")
    if raw.is_empty():
        raise DataQualityError("trade calendar has no rows")
    days: list[date] = []
    seen: set[date] = set()
    for index, item in enumerate(raw.iter_rows(named=True), start=2):
        day = _require_date(item.get("date"), index)
        if day in seen:
            raise DataQualityError(f"trade calendar has duplicate date {day.isoformat()}")
        seen.add(day)
        days.append(day)
    return sorted(days)


def read_universe_snapshots_file(path: Path) -> list[SnapshotCrossSection]:
    source = Path(path)
    if not source.is_file():
        raise DataQualityError(f"universe snapshot file not found: {source.name}")
    try:
        raw = pl.read_csv(
            source,
            try_parse_dates=True,
            schema_overrides={"available_at": pl.String, "weight": pl.Utf8},
        )
    except Exception as exc:
        raise DataQualityError("universe snapshot file is not a readable CSV") from exc
    missing = [col for col in SNAPSHOT_REQUIRED if col not in raw.columns]
    if missing:
        raise DataQualityError(f"universe snapshot missing required columns: {missing}")
    extra = [col for col in raw.columns if col not in SNAPSHOT_COLUMNS]
    if extra:
        raise DataQualityError(f"universe snapshot has unknown columns: {extra}")
    if raw.is_empty():
        raise DataQualityError("universe snapshot file has no rows")

    grouped: dict[tuple[str, date], list[tuple[str, datetime, float | None]]] = {}
    seen: set[tuple[str, date, str]] = set()
    for index, item in enumerate(raw.iter_rows(named=True), start=2):
        universe_id = _require_text(item.get("universe_id"), "universe_id", index)
        effective_from = _require_date(item.get("effective_from"), index)
        symbol = require_ts_code(str(item.get("symbol") or ""), kind="stock")
        available_at = parse_available_at_utc(item.get("available_at"), name="universe snapshot")
        weight = _optional_float(item.get("weight"), index)
        key = (universe_id, effective_from, symbol)
        if key in seen:
            raise DataQualityError(
                f"universe snapshot duplicate primary key "
                f"(universe_id, effective_from, symbol)={key}"
            )
        seen.add(key)
        grouped.setdefault((universe_id, effective_from), []).append((symbol, available_at, weight))

    snapshots: list[SnapshotCrossSection] = []
    for (universe_id, effective_from), rows in grouped.items():
        stamps = {stamp for _symbol, stamp, _weight in rows}
        if len(stamps) != 1:
            raise DataQualityError(
                f"universe snapshot {universe_id} effective_from={effective_from.isoformat()} "
                "has mixed available_at values; a complete cross-section must share one timestamp"
            )
        snapshots.append(
            SnapshotCrossSection(
                universe_id=universe_id,
                effective_from=effective_from,
                available_at=next(iter(stamps)),
                members=tuple((symbol, weight) for symbol, _stamp, weight in rows),
            )
        )
    return sorted(snapshots, key=lambda item: (item.universe_id, item.effective_from))


def materialize_daily_membership(
    snapshots: list[SnapshotCrossSection],
    calendar: list[date],
    config: StrategyConfig,
    start: date,
    end: date,
) -> pl.DataFrame:
    if config.universe.mode != "historical_membership":
        raise DataQualityError(
            "build-universe-membership requires universe.mode=historical_membership; "
            f"got {config.universe.mode}"
        )
    if end < start:
        raise DataQualityError("end date must be on or after start date")
    if not snapshots:
        raise DataQualityError("universe snapshot file has no rows")
    if not calendar:
        raise DataQualityError("trade calendar has no rows")
    cal_start, cal_end = calendar[0], calendar[-1]
    if start < cal_start or end > cal_end:
        raise DataQualityError(
            f"request window {start.isoformat()}..{end.isoformat()} "
            f"is not covered by the trade calendar {cal_start.isoformat()}..{cal_end.isoformat()}"
        )
    ids = {item.universe_id for item in snapshots}
    if ids != {config.universe.id}:
        raise DataQualityError(
            f"universe snapshot universe_id {sorted(ids)} "
            f"does not match config universe.id '{config.universe.id}'"
        )
    expected = config.universe.expected_constituents
    if expected is not None:
        for item in snapshots:
            if len(item.members) != expected:
                raise DataQualityError(
                    f"universe snapshot expected_constituents={expected} "
                    f"but effective_from={item.effective_from.isoformat()} has {len(item.members)} members"
                )

    days = [day for day in calendar if start <= day <= end]
    if not days:
        raise DataQualityError("request window contains no trade-calendar dates")

    rows: list[dict[str, object]] = []
    for day in days:
        cutoff = decision_at_utc(day, config.data)
        usable = [
            item
            for item in snapshots
            if item.effective_from <= day and item.available_at <= cutoff
        ]
        if not usable:
            raise DataQualityError(
                f"universe membership cannot be built for {day.isoformat()}; "
                "no effective and known complete snapshot"
            )
        chosen = max(usable, key=lambda item: item.effective_from)
        if expected is not None and len(chosen.members) != expected:
            raise DataQualityError(
                f"universe_membership expected_constituents={expected} "
                f"but as_of_date={day.isoformat()} has {len(chosen.members)} members"
            )
        for symbol, weight in chosen.members:
            rows.append(
                {
                    "universe_id": chosen.universe_id,
                    "as_of_date": day,
                    "symbol": symbol,
                    "available_at": chosen.available_at,
                    "weight": weight,
                }
            )
    frame = pl.DataFrame(rows).with_columns(
        [
            pl.col("universe_id").cast(pl.String),
            pl.col("as_of_date").cast(pl.Date),
            pl.col("symbol").cast(pl.String),
            pl.col("available_at").cast(pl.Datetime("us")),
            pl.col("weight").cast(pl.Float64),
        ]
    ).select(list(UNIVERSE_MEMBERSHIP_SCHEMA))
    instruments = pl.DataFrame(
        {
            "symbol": list(dict.fromkeys(frame["symbol"].to_list())),
            "is_index": False,
            "is_global": False,
        }
    )
    validate_universe_membership(
        frame,
        days,
        instruments,
        universe_id=config.universe.id,
        expected_constituents=expected,
    )
    return frame


def format_available_at_utc(value: datetime) -> str:
    """Lossless naive-UTC ISO-8601 with Z. Preserves microseconds."""
    dt = value
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def write_universe_membership_csv(frame: pl.DataFrame, dest: Path, *, overwrite: bool = False) -> Path:
    dest = Path(dest)
    if dest.exists() and not overwrite:
        raise DataQualityError(f"output file already exists: {dest.name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.{uuid.uuid4().hex}.tmp"
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["universe_id", "as_of_date", "symbol", "available_at", "weight"])
            for row in frame.iter_rows(named=True):
                available = row["available_at"]
                if isinstance(available, datetime):
                    stamp = format_available_at_utc(available)
                else:
                    stamp = str(available)
                weight = row["weight"]
                writer.writerow(
                    [
                        row["universe_id"],
                        row["as_of_date"].isoformat() if isinstance(row["as_of_date"], date) else row["as_of_date"],
                        row["symbol"],
                        stamp,
                        "" if weight is None else weight,
                    ]
                )
        tmp.replace(dest)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    return dest


def build_universe_membership(
    *,
    snapshots_file: Path,
    calendar_file: Path,
    config: StrategyConfig,
    start: date,
    end: date,
    output: Path,
    overwrite: bool = False,
) -> MaterializeResult:
    snapshots = read_universe_snapshots_file(snapshots_file)
    calendar = read_trade_calendar_file(calendar_file)
    frame = materialize_daily_membership(snapshots, calendar, config, start, end)
    path = write_universe_membership_csv(frame, output, overwrite=overwrite)
    counts = frame.group_by("as_of_date").len().sort("as_of_date")["len"].to_list()
    unique = sorted({int(value) for value in counts})
    members_per_day = str(unique[0]) if len(unique) == 1 else ",".join(str(value) for value in unique)
    return MaterializeResult(
        frame=frame,
        path=path,
        universe_id=config.universe.id,
        trading_days=int(frame["as_of_date"].n_unique()),
        members_per_day=members_per_day,
        row_count=frame.height,
        snapshot_count=len(snapshots),
    )
