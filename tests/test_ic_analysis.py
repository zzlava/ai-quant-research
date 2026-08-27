from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from app.demo.generator import generate_demo_market
from app.providers.demo_provider import DemoProvider
from app.research.ic import (
    _summary,
    _target_overlap_lag,
    analyze_ic,
    write_ic_report,
)
from app.research.quantile_portfolios import (
    SPREAD_DEFINITION,
    QuantileDayObservation,
    quantile_day_observation,
    summarize_quantile_observations,
    validate_quantile_count,
)
from app.storage.memory import InMemoryStore
from tests.helpers import load_test_config


def test_ic_analysis_uses_as_of_scores_and_writes_a_reproducible_report(tmp_path) -> None:
    bundle = generate_demo_market(seed=7, n_stocks=12, start=date(2023, 1, 3), end=date(2024, 3, 29))
    store = InMemoryStore.from_provider(DemoProvider(bundle=bundle))
    start = bundle.calendar[60]
    end = bundle.calendar[80]

    progress: list[tuple[int, int, date]] = []
    report = analyze_ic(
        store=store,
        config=load_test_config(),
        start=start,
        end=end,
        horizons=[1, 5],
        rolling_window_days=10,
        rolling_step_days=5,
        progress=lambda done, total, day: progress.append((done, total, day)),
    )

    assert report.data_snapshot_id == store.snapshot().snapshot_id
    assert report.horizons == [1, 5]
    assert len(report.summaries) == 22
    assert report.diagnostic_only is True
    assert report.tradable_long_short is False
    assert report.ready_for_scoring is False
    assert report.ready_for_trading is False
    assert report.quantile_count == 5
    assert report.spread_definition == SPREAD_DEFINITION
    assert len(report.quantile_summaries) == 22
    assert report.annual_periods[0].label == "2023"
    assert report.annual_quantile_periods[0].label == "2023"
    assert report.rolling_periods[0].start == start
    assert report.rolling_periods[0].end == bundle.calendar[69]
    assert report.rolling_quantile_periods[0].start == start
    assert any(item.observations > 0 for item in report.summaries)
    assert any(item.scoring_days > 0 for item in report.quantile_summaries)
    assert progress[0] == (1, 21, start)
    assert progress[-1] == (21, 21, end)
    populated = next(item for item in report.summaries if item.observations >= 2)
    assert populated.t_stat is not None
    assert populated.icir is not None
    assert populated.hac_t_stat is not None
    assert populated.hac_lag == populated.horizon_days - 1
    q_populated = next(item for item in report.quantile_summaries if item.scoring_days >= 2)
    assert q_populated.mean_spread is not None
    assert q_populated.spread_ir is not None
    assert q_populated.hac_t_stat is not None
    assert q_populated.hac_lag == q_populated.horizon_days - 1
    assert q_populated.average_names is not None
    assert q_populated.minimum_names is not None
    output = tmp_path / "ic.json"
    write_ic_report(report, output)
    text = output.read_text(encoding="utf-8")
    assert '"data_snapshot_id"' in text
    assert '"t_stat"' in text
    assert '"icir"' in text
    assert '"hac_t_stat"' in text
    assert '"hac_lag"' in text
    assert '"quantile_summaries"' in text
    assert '"diagnostic_only": true' in text
    assert '"tradable_long_short": false' in text
    assert '"ready_for_scoring": false' in text
    assert '"ready_for_trading": false' in text
    assert f'"spread_definition": "{SPREAD_DEFINITION}"' in text
    write_ic_report(report, output)
    assert output.read_text(encoding="utf-8") == text


def test_ic_analysis_rejects_partial_rolling_configuration() -> None:
    bundle = generate_demo_market(seed=7, n_stocks=12, start=date(2023, 1, 3), end=date(2024, 3, 29))
    store = InMemoryStore.from_provider(DemoProvider(bundle=bundle))

    try:
        analyze_ic(
            store=store,
            config=load_test_config(),
            start=bundle.calendar[60],
            end=bundle.calendar[80],
            horizons=[5],
            rolling_window_days=10,
        )
    except ValueError as exc:
        assert "configured together" in str(exc)
    else:
        raise AssertionError("partial rolling configuration must fail")


def test_ic_analysis_rejects_invalid_quantile_count() -> None:
    bundle = generate_demo_market(seed=7, n_stocks=12, start=date(2023, 1, 3), end=date(2024, 3, 29))
    store = InMemoryStore.from_provider(DemoProvider(bundle=bundle))
    with pytest.raises(ValueError, match="2 to 10"):
        analyze_ic(
            store=store,
            config=load_test_config(),
            start=bundle.calendar[60],
            end=bundle.calendar[80],
            horizons=[1],
            quantiles=1,
        )
    with pytest.raises(ValueError, match="2 to 10"):
        validate_quantile_count(11)


def test_ic_analysis_can_follow_the_strategy_signal_schedule() -> None:
    bundle = generate_demo_market(
        seed=7,
        n_stocks=12,
        start=date(2023, 1, 3),
        end=date(2024, 3, 29),
    )
    store = InMemoryStore.from_provider(DemoProvider(bundle=bundle))
    start = bundle.calendar[60]
    end = bundle.calendar[80]
    config = load_test_config().model_copy(deep=True)
    config.trade.signal_interval_days = 5
    config.trade.signal_anchor_date = start
    progress: list[tuple[int, int, date]] = []
    report = analyze_ic(
        store=store,
        config=config,
        start=start,
        end=end,
        horizons=[1],
        scheduled_only=True,
        progress=lambda done, total, day: progress.append((done, total, day)),
    )
    assert report.decision_schedule == "strategy_signal_schedule"
    assert [item[2] for item in progress] == [
        bundle.calendar[index] for index in range(60, 81, 5)
    ]
    for item in report.summaries:
        if item.observations >= 2:
            assert item.hac_lag == 0
    for item in report.quantile_summaries:
        if item.scoring_days >= 2:
            assert item.hac_lag == 0


def test_target_overlap_lag_rules() -> None:
    assert _target_overlap_lag(
        horizon_days=5, scheduled_only=False, signal_interval_days=1
    ) == 4
    assert _target_overlap_lag(
        horizon_days=5, scheduled_only=True, signal_interval_days=5
    ) == 0
    assert _target_overlap_lag(
        horizon_days=10, scheduled_only=True, signal_interval_days=5
    ) == 1
    assert _target_overlap_lag(
        horizon_days=20, scheduled_only=True, signal_interval_days=20
    ) == 0


def test_summary_hac_differs_from_naive_on_autocorrelated_series() -> None:
    # Strong positive autocorrelation: overlapping-style persistence.
    values = [0.02 * math.sin(index / 3.0) + 0.01 for index in range(40)]
    summary = _summary(
        5,
        "final_score",
        values,
        scheduled_only=False,
        signal_interval_days=1,
    )
    assert summary.hac_lag == 4
    assert summary.t_stat is not None
    assert summary.hac_t_stat is not None
    assert summary.icir is not None
    assert summary.std_spearman_ic is not None
    assert summary.mean_spearman_ic is not None
    assert summary.icir == pytest.approx(summary.mean_spearman_ic / summary.std_spearman_ic)
    assert abs(summary.hac_t_stat) < abs(summary.t_stat)


def test_summary_caps_hac_lag_to_observations_minus_one() -> None:
    summary = _summary(
        20,
        "final_score",
        [0.1, -0.05, 0.02],
        scheduled_only=False,
        signal_interval_days=1,
    )
    assert summary.observations == 3
    assert summary.hac_lag == 2
    assert summary.t_stat is not None
    assert summary.hac_t_stat is not None


def test_summary_scheduled_non_overlapping_horizon_uses_lag_zero() -> None:
    values = [0.01, -0.02, 0.015, -0.005, 0.008]
    summary = _summary(
        5,
        "value_score",
        values,
        scheduled_only=True,
        signal_interval_days=5,
    )
    assert summary.hac_lag == 0
    assert summary.t_stat is not None
    assert summary.hac_t_stat is not None
    # lag=0 Newey-West uses /n gamma0, so it differs from the sample-std naive t.
    assert summary.hac_t_stat != pytest.approx(summary.t_stat)


def test_summary_boundary_empty_single_and_zero_variance() -> None:
    empty = _summary(5, "final_score", [])
    assert empty.observations == 0
    assert empty.mean_spearman_ic is None
    assert empty.std_spearman_ic is None
    assert empty.t_stat is None
    assert empty.icir is None
    assert empty.hac_t_stat is None
    assert empty.hac_lag is None

    single = _summary(5, "final_score", [0.12])
    assert single.observations == 1
    assert single.mean_spearman_ic == pytest.approx(0.12)
    assert single.std_spearman_ic is None
    assert single.t_stat is None
    assert single.icir is None
    assert single.hac_t_stat is None
    assert single.hac_lag is None

    zero_var = _summary(5, "final_score", [0.05, 0.05, 0.05])
    assert zero_var.std_spearman_ic is None
    assert zero_var.t_stat is None
    assert zero_var.icir is None
    assert zero_var.hac_lag == 2
    assert zero_var.hac_t_stat is None


def test_summary_rejects_non_finite_observations() -> None:
    with pytest.raises(ValueError, match="finite"):
        _summary(5, "final_score", [0.1, float("nan")])
    with pytest.raises(ValueError, match="finite"):
        _summary(5, "final_score", [0.1, float("inf")])


def test_annual_and_rolling_summaries_respect_overlap_lag() -> None:
    bundle = generate_demo_market(seed=7, n_stocks=12, start=date(2023, 1, 3), end=date(2024, 3, 29))
    store = InMemoryStore.from_provider(DemoProvider(bundle=bundle))
    start = bundle.calendar[60]
    end = bundle.calendar[80]
    report = analyze_ic(
        store=store,
        config=load_test_config(),
        start=start,
        end=end,
        horizons=[5],
        rolling_window_days=10,
        rolling_step_days=5,
    )
    for period in [*report.annual_periods, *report.rolling_periods]:
        for item in period.summaries:
            if item.observations >= 2:
                assert item.hac_lag == min(4, item.observations - 1)
    for period in [*report.annual_quantile_periods, *report.rolling_quantile_periods]:
        for item in period.summaries:
            if item.scoring_days >= 2:
                assert item.hac_lag == min(4, item.scoring_days - 1)


def test_quantile_monotonic_cross_section_has_positive_spread() -> None:
    day = date(2023, 6, 1)
    pairs = [(float(index), float(index) / 100.0) for index in range(10)]
    observed = quantile_day_observation(pairs, quantile_count=5, decision_day=day)
    assert observed is not None
    assert observed.spread > 0
    assert observed.highest_quantile_return > observed.lowest_quantile_return


def test_quantile_day_observation_is_order_insensitive() -> None:
    day = date(2023, 6, 1)
    pairs = [(1.0, 0.01), (5.0, 0.05), (3.0, 0.03), (2.0, 0.02), (4.0, 0.04)]
    forward = list(reversed(pairs))
    shuffled = [pairs[2], pairs[0], pairs[4], pairs[1], pairs[3]]
    baseline = quantile_day_observation(pairs, quantile_count=5, decision_day=day)
    assert baseline is not None
    for candidate in (forward, shuffled):
        observed = quantile_day_observation(candidate, quantile_count=5, decision_day=day)
        assert observed is not None
        assert observed.spread == pytest.approx(baseline.spread)
        assert observed.highest_quantile_return == pytest.approx(baseline.highest_quantile_return)
        assert observed.lowest_quantile_return == pytest.approx(baseline.lowest_quantile_return)
        assert observed.names == baseline.names


def test_quantile_ties_are_not_split_and_all_equal_is_skipped() -> None:
    day = date(2023, 6, 1)
    # Identical mid-factor values must share one average rank / one bucket.
    tied = [
        (1.0, -0.10),
        (2.0, -0.05),
        (2.0, 0.50),
        (2.0, 0.60),
        (3.0, 0.10),
        (4.0, 0.20),
        (5.0, 0.30),
        (6.0, 0.40),
        (7.0, 0.45),
        (8.0, 0.50),
    ]
    observed = quantile_day_observation(tied, quantile_count=5, decision_day=day)
    assert observed is not None
    # With average ranks, the three identical 2.0 scores stay together; spread
    # must not invent opposite-end membership for the same factor value.
    assert observed.spread == pytest.approx(
        observed.highest_quantile_return - observed.lowest_quantile_return
    )

    assert (
        quantile_day_observation(
            [(1.0, 0.01), (1.0, 0.99), (1.0, -0.5), (1.0, 0.2), (1.0, 0.3)],
            quantile_count=5,
            decision_day=day,
        )
        is None
    )


def test_quantile_insufficient_cross_section_is_skipped_not_fabricated() -> None:
    day = date(2023, 6, 1)
    assert (
        quantile_day_observation(
            [(1.0, 0.1), (2.0, 0.2), (3.0, 0.3)],
            quantile_count=5,
            decision_day=day,
        )
        is None
    )
    summary = summarize_quantile_observations(
        horizon=5,
        factor="final_score",
        quantile_count=5,
        observations=[],
        skipped_insufficient_cross_section=7,
        scheduled_only=False,
        signal_interval_days=1,
    )
    assert summary.scoring_days == 0
    assert summary.mean_spread is None
    assert summary.t_stat is None
    assert summary.skipped_insufficient_cross_section == 7


def test_quantile_unknown_values_must_be_excluded_by_caller() -> None:
    # analyze_ic never fills missing factors/prices with 0; unit path mirrors that
    # by only accepting already-filtered finite pairs.
    day = date(2023, 6, 1)
    filtered = [(1.0, 0.01), (2.0, 0.02), (3.0, 0.03), (4.0, 0.04), (5.0, 0.05)]
    observed = quantile_day_observation(filtered, quantile_count=5, decision_day=day)
    assert observed is not None
    assert observed.names == 5
    # A zero factor is a real observation only when the caller explicitly saw 0.
    with_zero = [(0.0, 0.0), (1.0, 0.1), (2.0, 0.2), (3.0, 0.3), (4.0, 0.4)]
    zero_obs = quantile_day_observation(with_zero, quantile_count=5, decision_day=day)
    assert zero_obs is not None
    assert zero_obs.names == 5


def _synthetic_quantile_observations(
    spreads: list[float],
    *,
    start: date = date(2023, 1, 1),
) -> list[QuantileDayObservation]:
    """Build consistent day observations with unique decision_day values."""
    return [
        QuantileDayObservation(
            decision_day=start + timedelta(days=index),
            names=20,
            highest_quantile_return=0.01 + value,
            lowest_quantile_return=0.01,
            spread=value,
        )
        for index, value in enumerate(spreads)
    ]


def test_quantile_spread_summary_hac_and_spread_ir() -> None:
    spreads = [0.02 * math.sin(index / 3.0) + 0.01 for index in range(40)]
    observations = _synthetic_quantile_observations(spreads)
    summary = summarize_quantile_observations(
        horizon=5,
        factor="final_score",
        quantile_count=5,
        observations=observations,
        skipped_insufficient_cross_section=0,
        scheduled_only=False,
        signal_interval_days=1,
    )
    assert summary.hac_lag == 4
    assert summary.mean_spread is not None
    assert summary.std_spread is not None
    assert summary.spread_ir == pytest.approx(summary.mean_spread / summary.std_spread)
    assert summary.t_stat is not None
    assert summary.hac_t_stat is not None
    assert abs(summary.hac_t_stat) < abs(summary.t_stat)

    scheduled = summarize_quantile_observations(
        horizon=5,
        factor="final_score",
        quantile_count=5,
        observations=observations[:5],
        skipped_insufficient_cross_section=2,
        scheduled_only=True,
        signal_interval_days=5,
    )
    assert scheduled.hac_lag == 0
    assert scheduled.skipped_insufficient_cross_section == 2
    assert scheduled.average_names == pytest.approx(20.0)
    assert scheduled.minimum_names == 20


def test_quantile_summary_is_order_insensitive_and_rejects_duplicate_days() -> None:
    spreads = [0.02 * math.sin(index / 3.0) + 0.01 for index in range(12)]
    ordered = _synthetic_quantile_observations(spreads)
    shuffled = [ordered[i] for i in (5, 0, 11, 2, 8, 1, 9, 3, 10, 4, 7, 6)]
    kwargs = {
        "horizon": 5,
        "factor": "final_score",
        "quantile_count": 5,
        "skipped_insufficient_cross_section": 0,
        "scheduled_only": False,
        "signal_interval_days": 1,
    }
    baseline = summarize_quantile_observations(observations=ordered, **kwargs)
    reordered = summarize_quantile_observations(observations=shuffled, **kwargs)
    assert reordered.scoring_days == baseline.scoring_days
    assert reordered.mean_spread == pytest.approx(baseline.mean_spread)
    assert reordered.std_spread == pytest.approx(baseline.std_spread)
    assert reordered.t_stat == pytest.approx(baseline.t_stat)
    assert reordered.spread_ir == pytest.approx(baseline.spread_ir)
    assert reordered.hac_t_stat == pytest.approx(baseline.hac_t_stat)
    assert reordered.hac_lag == baseline.hac_lag
    assert reordered.average_names == pytest.approx(baseline.average_names)
    assert reordered.minimum_names == baseline.minimum_names

    duplicate = [
        *ordered[:2],
        QuantileDayObservation(
            decision_day=ordered[0].decision_day,
            names=20,
            highest_quantile_return=0.03,
            lowest_quantile_return=0.01,
            spread=0.02,
        ),
    ]
    with pytest.raises(ValueError, match="duplicate decision_day"):
        summarize_quantile_observations(observations=duplicate, **kwargs)


def test_quantile_day_observation_rejects_inconsistent_spread_and_illegal_names() -> None:
    with pytest.raises(ValueError, match="spread must equal"):
        QuantileDayObservation(
            decision_day=date(2023, 6, 1),
            names=20,
            highest_quantile_return=0.05,
            lowest_quantile_return=0.01,
            spread=0.02,
        )
    for illegal_names in (0, -1, True):
        with pytest.raises(ValueError, match="positive integer"):
            QuantileDayObservation(
                decision_day=date(2023, 6, 2),
                names=illegal_names,
                highest_quantile_return=0.02,
                lowest_quantile_return=0.01,
                spread=0.01,
            )
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            QuantileDayObservation(
                decision_day=date(2023, 6, 3),
                names=5,
                highest_quantile_return=bad,
                lowest_quantile_return=0.0,
                spread=bad,
            )


def test_quantile_day_observation_rejects_non_finite_pairs() -> None:
    day = date(2023, 6, 4)
    finite = [(1.0, 0.01), (2.0, 0.02), (3.0, 0.03), (4.0, 0.04), (5.0, 0.05)]
    with pytest.raises(ValueError, match="finite"):
        quantile_day_observation(
            [*finite[:-1], (float("nan"), 0.06)],
            quantile_count=5,
            decision_day=day,
        )
    with pytest.raises(ValueError, match="finite"):
        quantile_day_observation(
            [*finite[:-1], (6.0, float("inf"))],
            quantile_count=5,
            decision_day=day,
        )
