from __future__ import annotations

import os
from typing import Any, Protocol

import polars as pl

from app.errors import (
    BigQuantFetchError,
    MissingBigQuantCredentialsError,
    sanitize_error_message,
)

ACCESS_KEY_ENV = "AIQ_BIGQUANT_ACCESS_KEY"
SECRET_KEY_ENV = "AIQ_BIGQUANT_SECRET_KEY"


class BigQuantQueryClient(Protocol):
    """Small, replaceable BigQuant query surface. Implementations must not log credentials."""

    def query(self, sql: str, *, filters: dict[str, list[str]]) -> pl.DataFrame: ...


def read_bigquant_credentials() -> tuple[str, str]:
    access_key = os.environ.get(ACCESS_KEY_ENV, "").strip()
    secret_key = os.environ.get(SECRET_KEY_ENV, "").strip()
    if not access_key or not secret_key:
        raise MissingBigQuantCredentialsError(
            "BigQuant access credentials are not configured; "
            f"set both {ACCESS_KEY_ENV} and {SECRET_KEY_ENV} locally"
        )
    return access_key, secret_key


class LiveBigQuantClient:
    """Lazy BigQuant DAI wrapper used only for a separately labelled public reconstruction."""

    def __init__(self, access_key: str, secret_key: str) -> None:
        if not access_key.strip() or not secret_key.strip():
            raise MissingBigQuantCredentialsError("BigQuant access credentials are not configured")
        self._access_key = access_key
        self._secret_key = secret_key
        self._dai: Any = None

    def __repr__(self) -> str:
        return "LiveBigQuantClient(credentials=<redacted>)"

    def query(self, sql: str, *, filters: dict[str, list[str]]) -> pl.DataFrame:
        dai = self._dai
        if dai is None:
            try:
                from bigquantdai import dai as imported_dai
            except ImportError as exc:
                raise BigQuantFetchError(
                    "BigQuant SDK is not installed; install the project extra with 'pip install -e .[bigquant]'"
                ) from exc
            try:
                imported_dai.login(self._access_key, self._secret_key)
            except Exception as exc:  # noqa: BLE001
                raise BigQuantFetchError(
                    f"BigQuant authentication failed: {sanitize_error_message(exc)}"
                ) from None
            dai = imported_dai
            self._dai = dai
        try:
            result = dai.query(sql, filters=filters)
            frame = result.df()
        except Exception as exc:  # noqa: BLE001
            raise BigQuantFetchError(f"BigQuant query failed: {sanitize_error_message(exc)}") from None
        if frame is None:
            return pl.DataFrame()
        if isinstance(frame, pl.DataFrame):
            return frame
        try:
            return pl.from_pandas(frame)
        except Exception as exc:  # noqa: BLE001
            raise BigQuantFetchError(
                f"BigQuant query returned an unreadable table: {sanitize_error_message(exc)}"
            ) from None
