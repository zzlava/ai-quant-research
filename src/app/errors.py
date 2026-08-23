from __future__ import annotations

import re

from pydantic import ValidationError


class MissingBenchmarkError(ValueError):
    """Required index/global series is missing or too short at the decision time."""


class DataQualityError(ValueError):
    """Market data failed schema, OHLC, or snapshot contract checks."""


class SnapshotError(ValueError):
    """Market snapshot is missing, incomplete, or does not match its manifest."""


class MissingTushareTokenError(ValueError):
    """Tushare token is not configured. Do not include the secret in the message."""


class TushareFetchError(ValueError):
    """Tushare raw data could not be fetched or normalized."""


class PreflightError(ValueError):
    """Research window failed warm-up or point-in-time membership preflight."""


_HOME_RE = re.compile(r"(?:/Users|/home)/[^\s:/]+")
_WIN_HOME_RE = re.compile(r"[A-Za-z]:\\Users\\[^\s\\]+")
_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)(?:[\w.\-]+[/\\])+[\w.\-]+")
_SECRET_RE = re.compile(r"(?i)(token|secret|password|api[_-]?key|cookie)\s*[:=]\s*\S+")
_ENV_RE = re.compile(r"(?i)\b(AIQ_[A-Z0-9_]+|TUSHARE_TOKEN|AKSHARE_\w+)\s*[:=]\s*\S+")


def sanitize_error_message(exc: BaseException) -> str:
    message = str(exc) or exc.__class__.__name__
    message = _SECRET_RE.sub(r"\1=<redacted>", message)
    message = _ENV_RE.sub(r"\1=<redacted>", message)
    message = _HOME_RE.sub("<home>", message)
    message = _WIN_HOME_RE.sub("<home>", message)
    message = _ABS_PATH_RE.sub("<path>", message)
    return message


def is_client_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        MissingBenchmarkError
        | DataQualityError
        | SnapshotError
        | MissingTushareTokenError
        | TushareFetchError
        | PreflightError
        | FileNotFoundError
        | KeyError
        | ValueError
        | ValidationError,
    )
