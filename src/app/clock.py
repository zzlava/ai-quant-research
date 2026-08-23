from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.models.config import DataConfig, SessionConfig


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", maxsplit=1)
    return time(int(hour), int(minute))


def available_at_utc(bar_date: date, session: SessionConfig) -> datetime:
    """When a session close becomes knowable, stored as naive UTC."""
    local = datetime.combine(bar_date, parse_hhmm(session.session_close), tzinfo=ZoneInfo(session.timezone))
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def decision_at_utc(as_of: date, data: DataConfig) -> datetime:
    """A-share research decision time: as_of session close in the configured timezone."""
    local = datetime.combine(
        as_of,
        parse_hhmm(data.decision_time),
        tzinfo=ZoneInfo(data.decision_timezone),
    )
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def session_for(symbol: str, data: DataConfig) -> SessionConfig:
    if symbol not in data.sessions:
        raise KeyError(f"no session contract for '{symbol}' in strategy data.sessions")
    return data.sessions[symbol]
