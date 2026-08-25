from __future__ import annotations

import os
from typing import Any, Protocol

import polars as pl

from app.errors import MissingTushareTokenError, TushareFetchError, sanitize_error_message

TOKEN_ENV = "AIQ_TUSHARE_TOKEN"


class TushareQueryClient(Protocol):
    """Offline-replaceable Tushare query surface. Implementations must not log secrets."""

    def query(self, api_name: str, **params: Any) -> pl.DataFrame: ...


def read_tushare_token() -> str:
    raw = os.environ.get(TOKEN_ENV)
    if raw is None or not str(raw).strip():
        raise MissingTushareTokenError("Tushare token is not configured")
    return str(raw).strip()


class LiveTushareClient:
    """Lazy official tushare.pro_api wrapper. Network happens only on query()."""

    # Tushare's single-code endpoints can be rate limited independently.  The
    # provider uses this marker so test doubles remain fast while real fetches
    # remain below the service ceiling.
    requires_single_code_rate_limit = True

    def __init__(self, token: str) -> None:
        if not token.strip():
            raise MissingTushareTokenError("Tushare token is not configured")
        self._token = token
        self._pro: Any = None

    def __repr__(self) -> str:
        return "LiveTushareClient(token=<redacted>)"

    def query(self, api_name: str, **params: Any) -> pl.DataFrame:
        pro = self._pro
        if pro is None:
            try:
                import tushare as ts
            except ImportError as exc:
                raise TushareFetchError("tushare package is not installed") from exc
            pro = ts.pro_api(self._token)
            self._pro = pro
        fn = getattr(pro, api_name, None)
        if fn is None:
            raise TushareFetchError(f"tushare API '{api_name}' is not available")
        try:
            frame = fn(**params)
        except Exception as exc:
            raise TushareFetchError(
                f"tushare {api_name} query failed: {sanitize_error_message(exc)}"
            ) from None
        if frame is None:
            return pl.DataFrame()
        if isinstance(frame, str):
            raise TushareFetchError(f"tushare {api_name} query failed: {sanitize_error_message(ValueError(frame))}")
        if isinstance(frame, pl.DataFrame):
            return frame
        try:
            return pl.from_pandas(frame)
        except Exception:
            # Some Tushare pages arrive with object columns whose early rows
            # look integral but later rows contain decimals. Fall back to a
            # full-row schema scan instead of trusting a partial inference.
            try:
                records = (
                    frame.astype(object)
                    .where(frame.notna(), None)
                    .to_dict(orient="records")
                )
                return pl.from_dicts(records, infer_schema_length=None)
            except Exception as exc:
                raise TushareFetchError(
                    f"tushare {api_name} query failed: {sanitize_error_message(exc)}"
                ) from None
