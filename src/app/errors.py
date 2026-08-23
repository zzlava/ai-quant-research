from __future__ import annotations


class MissingBenchmarkError(ValueError):
    """Required index/global series is missing or too short at the decision time."""


class DataQualityError(ValueError):
    """Market data failed schema, OHLC, or snapshot contract checks."""
