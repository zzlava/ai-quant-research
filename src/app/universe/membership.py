from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from app.clock import decision_at_utc, parse_hhmm
from app.errors import DataQualityError, TushareFetchError
from app.models.config import StrategyConfig, UniverseConfig
from app.providers._frames import UNIVERSE_MEMBERSHIP_SCHEMA
from app.providers.tushare_normalize import require_ts_code
from app.storage.quality import normalize_available_at, parse_available_at_utc

MEMBERSHIP_REQUIRED = ("universe_id", "as_of_date", "symbol", "available_at")
MEMBERSHIP_COLUMNS = (*MEMBERSHIP_REQUIRED, "weight")


def default_cn_available_at(as_of: date) -> datetime:
    """A-share session close (15:00 Asia/Shanghai) as naive UTC."""
    local = datetime.combine(as_of, parse_hhmm("15:00"), tzinfo=ZoneInfo("Asia/Shanghai"))
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def build_manual_static_membership(
    symbols: list[str],
    calendar: list[date],
    *,
    universe_id: str,
    available_at_for: Callable[[date], datetime] | None = None,
    weight: float | None = None,
) -> pl.DataFrame:
    """Materialize a full daily cross-section for a controlled research pool."""
    if not universe_id.strip():
        raise DataQualityError("universe_id must be non-empty")
    if not symbols:
        raise DataQualityError("manual_static membership requires at least one symbol")
    if not calendar:
        raise DataQualityError("manual_static membership requires a trading calendar")
    avail_fn = available_at_for or default_cn_available_at
    rows: list[dict[str, object]] = []
    for as_of in calendar:
        available_at = avail_fn(as_of)
        for symbol in symbols:
            rows.append(
                {
                    "universe_id": universe_id,
                    "as_of_date": as_of,
                    "symbol": symbol,
                    "available_at": available_at,
                    "weight": weight,
                }
            )
    return pl.DataFrame(rows).with_columns(
        [
            pl.col("universe_id").cast(pl.String),
            pl.col("as_of_date").cast(pl.Date),
            pl.col("symbol").cast(pl.String),
            pl.col("available_at").cast(pl.Datetime("us")),
            pl.col("weight").cast(pl.Float64),
        ]
    )


def read_universe_membership_file(path: Path) -> pl.DataFrame:
    """Parse an offline daily membership CSV. Does not invent missing dates."""
    source = Path(path)
    if not source.is_file():
        raise DataQualityError(f"universe membership file not found: {source.name}")
    try:
        raw = pl.read_csv(
            source,
            try_parse_dates=True,
            schema_overrides={"available_at": pl.String, "weight": pl.Utf8},
        )
    except Exception as exc:
        raise DataQualityError("universe_membership file is not a readable CSV") from exc
    missing = [col for col in MEMBERSHIP_REQUIRED if col not in raw.columns]
    if missing:
        raise DataQualityError(f"universe_membership missing required columns: {missing}")
    extra = [col for col in raw.columns if col not in MEMBERSHIP_COLUMNS]
    if extra:
        raise DataQualityError(f"universe_membership has unknown columns: {extra}")
    if raw.is_empty():
        raise DataQualityError("universe_membership has no rows")

    rows: list[dict[str, object]] = []
    seen: set[tuple[str, date, str]] = set()
    for index, item in enumerate(raw.iter_rows(named=True), start=2):
        universe_id = _require_text(item.get("universe_id"), "universe_id", index)
        as_of = _require_date(item.get("as_of_date"), index)
        symbol = require_ts_code(str(item.get("symbol") or ""), kind="stock")
        available_at = parse_available_at_utc(item.get("available_at"), name="universe_membership")
        weight = _optional_float(item.get("weight"), index)
        key = (universe_id, as_of, symbol)
        if key in seen:
            raise DataQualityError(
                f"universe_membership duplicate primary key "
                f"(universe_id, as_of_date, symbol)={key}"
            )
        seen.add(key)
        rows.append(
            {
                "universe_id": universe_id,
                "as_of_date": as_of,
                "symbol": symbol,
                "available_at": available_at,
                "weight": weight,
            }
        )
    frame = pl.DataFrame(rows).with_columns(
        [
            pl.col("as_of_date").cast(pl.Date),
            pl.col("available_at").cast(pl.Datetime("us")),
            pl.col("weight").cast(pl.Float64),
        ]
    )
    return normalize_available_at(frame, "universe_membership").select(list(UNIVERSE_MEMBERSHIP_SCHEMA))


def membership_symbols(frame: pl.DataFrame) -> list[str]:
    if frame.is_empty() or "symbol" not in frame.columns:
        return []
    return list(dict.fromkeys(str(code) for code in frame["symbol"].to_list()))


def assert_membership_within_window(frame: pl.DataFrame, start: date, end: date) -> None:
    if end < start:
        raise DataQualityError("end date must be on or after start date")
    if frame.is_empty() or "as_of_date" not in frame.columns:
        raise DataQualityError("universe_membership has no rows")
    extras = sorted(
        {
            day
            for day in frame["as_of_date"].to_list()
            if isinstance(day, date) and (day < start or day > end)
        }
    )
    if extras:
        raise DataQualityError(
            f"universe_membership has dates outside the requested window "
            f"{start.isoformat()}..{end.isoformat()}, first={extras[0]}"
        )


def assert_membership_covers_calendar(
    frame: pl.DataFrame,
    calendar: list[date],
    *,
    universe_id: str | None = None,
    expected_constituents: int | None = None,
    name: str = "universe_membership",
) -> None:
    if frame.is_empty() or "as_of_date" not in frame.columns:
        raise DataQualityError(f"{name} has no rows")
    ids = {str(value) for value in frame["universe_id"].to_list()}
    if universe_id is not None and ids != {universe_id}:
        raise DataQualityError(f"{name} universe_id {sorted(ids)} does not match '{universe_id}'")
    cal = set(calendar)
    present = {day for day in frame["as_of_date"].to_list() if isinstance(day, date)}
    extra_days = sorted(present - cal)
    if extra_days:
        raise DataQualityError(f"{name} has dates outside snapshot coverage, first={extra_days[0]}")
    for uid in sorted(ids):
        uid_days = {
            day
            for day, value in zip(frame["as_of_date"].to_list(), frame["universe_id"].to_list(), strict=True)
            if str(value) == uid and isinstance(day, date)
        }
        missing_days = sorted(cal - uid_days)
        if missing_days:
            raise DataQualityError(
                f"{name} missing complete cross-section for universe_id={uid} "
                f"on {missing_days[0]}; refusing to reuse another day's members"
            )
        if expected_constituents is None:
            continue
        counts = (
            frame.filter(pl.col("universe_id") == uid)
            .group_by("as_of_date")
            .len()
            .filter(pl.col("len") != expected_constituents)
        )
        if counts.height:
            sample = counts.sort("as_of_date").head(1).to_dicts()[0]
            raise DataQualityError(
                f"{name} expected_constituents={expected_constituents} "
                f"but as_of_date={sample['as_of_date']} has {sample['len']} members"
            )


def membership_lookup_options(universe: UniverseConfig) -> dict[str, int | bool | None]:
    return {
        "expected_constituents": universe.expected_constituents,
        "require_available_cross_section": universe.mode == "historical_membership",
    }


def resolve_fetch_universe(
    config: StrategyConfig,
    *,
    symbols_file: Path | None,
    membership_file: Path | None,
    start: date | None = None,
    end: date | None = None,
) -> tuple[list[str], pl.DataFrame | None]:
    """Validate CLI inputs against universe.mode before any network or token read."""
    if symbols_file is not None and membership_file is not None:
        raise TushareFetchError("--symbols-file and --universe-membership-file are mutually exclusive")
    mode = config.universe.mode
    if mode == "historical_membership":
        if membership_file is None:
            raise TushareFetchError(
                "historical_membership requires --universe-membership-file; "
                "refusing to invent historical constituents"
            )
        if not membership_file.is_file():
            raise TushareFetchError("universe membership file not found")
        frame = read_universe_membership_file(membership_file)
        ids = {str(value) for value in frame["universe_id"].to_list()}
        if ids != {config.universe.id}:
            raise DataQualityError(
                f"universe_membership universe_id {sorted(ids)} "
                f"does not match config universe.id '{config.universe.id}'"
            )
        if start is not None and end is not None:
            assert_membership_within_window(frame, start, end)
        stocks = membership_symbols(frame)
        if not stocks:
            raise TushareFetchError("universe membership file has no symbols")
        return stocks, frame
    if mode == "public_reconstruction":
        raise TushareFetchError(
            "public_reconstruction must use a separately collected base price snapshot; "
            "do not pass it to fetch-tushare as a PIT membership file"
        )
    if membership_file is not None:
        raise TushareFetchError("manual_static requires --symbols-file, not --universe-membership-file")
    if symbols_file is None:
        raise TushareFetchError(
            "manual_static requires --symbols-file. "
            "--index-universe is disabled because end-date constituents look ahead"
        )
    if not symbols_file.is_file():
        raise TushareFetchError("symbols file not found")
    from app.providers.tushare_fetch import read_symbols_file

    return read_symbols_file(symbols_file), None


def bind_membership_to_tables(
    tables: dict[str, pl.DataFrame],
    *,
    config: StrategyConfig,
    membership: pl.DataFrame | None,
    stocks: list[str],
) -> dict[str, pl.DataFrame]:
    calendar = [day for day in tables["calendar"]["date"].to_list() if isinstance(day, date)]
    if membership is None:
        def available_at_for(day: date) -> datetime:
            return decision_at_utc(day, config.data)

        bound = build_manual_static_membership(
            stocks,
            calendar,
            universe_id=config.universe.id,
            available_at_for=available_at_for,
        )
    else:
        bound = membership
    from app.storage.quality import validate_universe_membership

    validate_universe_membership(
        bound,
        calendar,
        tables["instruments"],
        universe_id=config.universe.id,
        expected_constituents=config.universe.expected_constituents,
    )
    out = dict(tables)
    out["universe_membership"] = bound.select(list(UNIVERSE_MEMBERSHIP_SCHEMA))
    return out


def members_available_on(
    frame: pl.DataFrame,
    *,
    universe_id: str,
    as_of: date,
    available_by: datetime,
    expected_constituents: int | None = None,
    require_available_cross_section: bool = False,
) -> set[str]:
    if frame.is_empty() or "as_of_date" not in frame.columns:
        raise DataQualityError(
            f"universe_membership missing complete cross-section for "
            f"universe_id={universe_id} as_of_date={as_of}; "
            "refusing to reuse another day's members"
        )
    day_rows = frame.filter((pl.col("universe_id") == universe_id) & (pl.col("as_of_date") == as_of))
    if day_rows.is_empty():
        raise DataQualityError(
            f"universe_membership missing complete cross-section for "
            f"universe_id={universe_id} as_of_date={as_of}; "
            "refusing to reuse another day's members"
        )
    if expected_constituents is not None and day_rows.height != expected_constituents:
        raise DataQualityError(
            f"universe_membership expected_constituents={expected_constituents} "
            f"but as_of_date={as_of} has {day_rows.height} members"
        )
    late = day_rows.filter(pl.col("available_at") > available_by)
    if require_available_cross_section and late.height:
        raise DataQualityError(
            f"universe_membership as_of_date={as_of} has {late.height} members "
            f"not yet available at decision time; refusing to score a partial universe"
        )
    usable = day_rows.filter(pl.col("available_at") <= available_by)
    if usable.is_empty():
        return set()
    return {str(code) for code in usable["symbol"].to_list()}


def _require_text(value: object, name: str, line: int) -> str:
    text = "" if value is None else str(value).strip()
    if not text or text in {"null", "None", "NA"}:
        raise DataQualityError(f"universe_membership {name} is empty at line {line}")
    return text


def _require_date(value: object, line: int) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = "" if value is None else str(value).strip()
    if not text:
        raise DataQualityError(f"universe_membership as_of_date is empty at line {line}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise DataQualityError(f"universe_membership as_of_date is invalid at line {line}") from exc


def _optional_float(value: object, line: int) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        raise DataQualityError(f"universe_membership weight is not a finite float at line {line}")
    text = str(value).strip()
    if not text or text in {"null", "None", "NA"}:
        return None
    try:
        parsed = float(text)
    except ValueError as exc:
        raise DataQualityError(f"universe_membership weight is not a finite float at line {line}") from exc
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        raise DataQualityError(f"universe_membership weight is not a finite float at line {line}")
    return parsed
