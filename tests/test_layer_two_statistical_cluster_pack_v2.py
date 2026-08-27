from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from app.research.layer_two_statistical_cluster_pack_v2 import (
    CORRELATION_THRESHOLD,
    LOOKBACK_RETURNS,
    _components,
    _monthly_anchors,
)


def test_monthly_anchors_are_first_available_market_dates() -> None:
    calendar = [date(2021, 12, 31)]
    current = date(2022, 1, 1)
    while current <= date(2024, 12, 31):
        if current.weekday() < 5:
            calendar.append(current)
        current += timedelta(days=1)

    anchors = _monthly_anchors(calendar)

    assert len(anchors) == 36
    assert anchors[0] == date(2022, 1, 3)
    assert anchors[-1] == date(2024, 12, 2)
    assert all(
        anchor.month != previous.month
        for previous, anchor in zip(anchors, anchors[1:], strict=False)
    )


def test_connected_components_allow_chain_linkage_without_pair_table() -> None:
    base = np.linspace(-1.0, 1.0, LOOKBACK_RETURNS)
    a = (base - base.mean()) / base.std(ddof=1)
    b_raw = base + 0.25 * np.sin(np.arange(LOOKBACK_RETURNS))
    b = (b_raw - b_raw.mean()) / b_raw.std(ddof=1)
    c_raw = b_raw + 0.25 * np.cos(np.arange(LOOKBACK_RETURNS))
    c = (c_raw - c_raw.mean()) / c_raw.std(ddof=1)
    opposite = -a
    matrix = np.column_stack([a, b, c, opposite])

    components = _components(["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"], matrix)

    assert CORRELATION_THRESHOLD == 0.65
    assert components == [["000001.SZ", "000002.SZ", "000003.SZ"], ["000004.SZ"]]


def test_component_result_is_deterministic() -> None:
    first = np.linspace(-2.0, 2.0, LOOKBACK_RETURNS)
    first = (first - first.mean()) / first.std(ddof=1)
    second = first.copy()
    matrix = np.column_stack([first, second])

    left = _components(["000001.SZ", "000002.SZ"], matrix)
    right = _components(["000001.SZ", "000002.SZ"], matrix)

    assert left == right == [["000001.SZ", "000002.SZ"]]
