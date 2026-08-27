"""Synthetic numerical tests for E11b-0a pure alpha diagnostic math kernels."""

from __future__ import annotations

import ast
import math
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.research.layer_two_alpha_diagnostic_engine import (
    BOUND_E11A_PROTOCOL_ID,
    BOUND_E11A_PROTOCOL_PATH,
    FROZEN_FACTOR_FAMILY_IDS,
    LAYER_TWO_ALPHA_DIAGNOSTIC_ENGINE_VERSION,
    LOW_VOL_REQUIRED_BARS,
    MOMENTUM_INDEX_T_MINUS_21,
    MOMENTUM_INDEX_T_MINUS_242,
    MOMENTUM_REQUIRED_BARS,
    QUALITY_SEALED_RULES,
    VALUE_SEALED_KEYS,
    FamilyCompositeEntry,
    HolmFactorResult,
    HolmStepDownResult,
    MarketObservation,
    SymbolCloseObservation,
    average_rank_percentiles,
    coverage_gate,
    defensive_low_vol,
    exact_forward_return,
    holm_step_down_four_factors,
    medium_momentum_12_1,
    multi_component_family_composite,
    newey_west_bartlett_inference,
    paired_spearman,
    quality_family_composite,
    quintile_top_minus_bottom_spread,
    size_band,
    standard_normal_cdf,
    value_family_composite,
    within_cluster_percentiles,
)

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "app" / "research" / "layer_two_alpha_diagnostic_engine.py"

SYM_A = "000001.SZ"
SYM_B = "000002.SZ"
SYM_C = "000003.SZ"
SYM_D = "000004.SZ"
SYM_E = "000005.SZ"
SYM_Z = "000099.SZ"


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _bars(dates: list[date], closes: list[float], *, verified: bool = True) -> list[MarketObservation]:
    assert len(dates) == len(closes)
    return [
        MarketObservation(date=day, adj_close=close, verified=verified)
        for day, close in zip(dates, closes, strict=True)
    ]


def test_engine_version_and_e11a_bindings() -> None:
    assert LAYER_TWO_ALPHA_DIAGNOSTIC_ENGINE_VERSION == "layer-two-alpha-diagnostic-engine-v0a"
    assert BOUND_E11A_PROTOCOL_PATH == "config/research/layer-two-alpha-development-protocol-v1.json"
    assert BOUND_E11A_PROTOCOL_ID == "fa91f0e260beb59a7f639dd3650a3842c817e470e9c3614abf2583dd691d2f86"
    assert FROZEN_FACTOR_FAMILY_IDS == (
        "quality",
        "value",
        "medium_momentum_12_1",
        "defensive_low_vol",
    )


def test_average_rank_percentiles_ties_and_n_equals_1() -> None:
    values = {SYM_D: 4.0, SYM_A: 1.0, SYM_C: 2.0, SYM_B: 2.0}
    got = average_rank_percentiles(values)
    assert got == {SYM_A: 0.0, SYM_B: 50.0, SYM_C: 50.0, SYM_D: 100.0}
    inverted = average_rank_percentiles(values, invert=True)
    assert inverted == {SYM_A: 100.0, SYM_B: 50.0, SYM_C: 50.0, SYM_D: 0.0}

    singleton = average_rank_percentiles({SYM_A: 7.0})
    assert singleton == {SYM_A: None}

    with_missing = average_rank_percentiles({SYM_Z: None, SYM_A: 10.0, SYM_B: 20.0})
    assert with_missing[SYM_Z] is None
    assert with_missing[SYM_A] == 0.0
    assert with_missing[SYM_B] == 100.0


def test_average_rank_percentiles_rejects_bool_nan_inf_and_is_order_deterministic() -> None:
    with pytest.raises(ValueError, match="bool"):
        average_rank_percentiles({SYM_A: True})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="finite"):
        average_rank_percentiles({SYM_A: float("nan")})
    with pytest.raises(ValueError, match="finite"):
        average_rank_percentiles({SYM_A: float("inf")})

    forward = average_rank_percentiles({SYM_B: 2.0, SYM_A: 1.0, SYM_C: 3.0})
    reverse = average_rank_percentiles({SYM_C: 3.0, SYM_A: 1.0, SYM_B: 2.0})
    assert forward == reverse == {SYM_A: 0.0, SYM_B: 50.0, SYM_C: 100.0}


def test_quality_sealed_five_components() -> None:
    # All five sealed components; roe/roic/grossprofit_margin/ocf_to_or high,
    # debt_to_assets low.  With monotone values the composites are 0/50/100.
    quality = quality_family_composite(
        {
            "roe": {SYM_C: 3.0, SYM_A: 1.0, SYM_B: 2.0},
            "roic": {SYM_A: 1.0, SYM_B: 2.0, SYM_C: 3.0},
            "grossprofit_margin": {SYM_A: 1.0, SYM_B: 2.0, SYM_C: 3.0},
            "debt_to_assets": {SYM_A: 30.0, SYM_B: 20.0, SYM_C: 10.0},
            "ocf_to_or": {SYM_A: 1.0, SYM_B: 2.0, SYM_C: 3.0},
        },
    )
    assert quality.entries[SYM_A].raw_composite == 0.0
    assert quality.entries[SYM_B].raw_composite == 50.0
    assert quality.entries[SYM_C].raw_composite == 100.0
    assert quality.entries[SYM_A].final_percentile == 0.0
    assert quality.entries[SYM_C].final_percentile == 100.0


def test_value_sealed_three_metrics() -> None:
    # pe_ttm/pb/ps_ttm all positive_only_inverted; SYM_A all-negative → excluded
    value = value_family_composite(
        {
            "pe_ttm": {SYM_A: -1.0, SYM_B: 10.0, SYM_C: 5.0},
            "pb": {SYM_B: 2.0, SYM_C: 3.0, SYM_A: 1.0},
            "ps_ttm": {SYM_A: -5.0, SYM_B: 15.0, SYM_C: 8.0},
        },
    )
    assert value.entries[SYM_A].raw_composite is None
    assert value.entries[SYM_A].final_percentile is None
    assert value.entries[SYM_B].raw_composite == pytest.approx(50.0 / 3)
    assert value.entries[SYM_C].raw_composite == pytest.approx(200.0 / 3)
    assert value.entries[SYM_B].final_percentile == 0.0
    assert value.entries[SYM_C].final_percentile == 100.0


def test_multi_component_min_known_gate() -> None:
    result = multi_component_family_composite(
        {
            "m1": {SYM_A: 1.0, SYM_B: 2.0, SYM_C: 3.0},
            "m2": {SYM_A: None, SYM_B: 3.0, SYM_C: 4.0},
            "m3": {SYM_A: None, SYM_B: 4.0, SYM_C: 5.0},
        },
        rules={"m1": "high", "m2": "high", "m3": "high"},
        min_known_components=3,
    )
    assert result.entries[SYM_A].raw_composite is None
    assert result.entries[SYM_A].known_component_count == 1
    assert result.entries[SYM_B].known_component_count == 3
    assert result.entries[SYM_B].raw_composite is not None
    assert result.entries[SYM_C].known_component_count == 3
    assert result.entries[SYM_C].raw_composite is not None


def test_momentum_exact_indices() -> None:
    dates = _weekdays(date(2022, 1, 3), MOMENTUM_REQUIRED_BARS)
    closes = [100.0 + i * 0.1 for i in range(MOMENTUM_REQUIRED_BARS)]
    expected = closes[MOMENTUM_INDEX_T_MINUS_21] / closes[MOMENTUM_INDEX_T_MINUS_242] - 1.0
    assert MOMENTUM_INDEX_T_MINUS_21 == 221
    assert MOMENTUM_INDEX_T_MINUS_242 == 0
    got = medium_momentum_12_1(_bars(dates, closes), expected_dates=dates)
    assert got == pytest.approx(expected)


def test_low_vol_ddof_and_sign() -> None:
    dates = _weekdays(date(2022, 1, 3), LOW_VOL_REQUIRED_BARS)
    closes = [100.0]
    for i in range(60):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 0.99))
    returns = [closes[i + 1] / closes[i] - 1.0 for i in range(60)]
    mean = sum(returns) / 60
    var = sum((r - mean) ** 2 for r in returns) / 59  # ddof=1
    expected = -math.sqrt(var) * math.sqrt(242.0)
    got = defensive_low_vol(_bars(dates, closes), expected_dates=dates)
    assert got == pytest.approx(expected)
    assert got is not None and got < 0.0


def test_market_window_defects_raise_or_unknown() -> None:
    dates = _weekdays(date(2022, 1, 3), MOMENTUM_REQUIRED_BARS)
    closes = [100.0 + i for i in range(MOMENTUM_REQUIRED_BARS)]
    bars = _bars(dates, closes)

    bad_verified = list(bars)
    bad_verified[10] = MarketObservation(date=dates[10], adj_close=closes[10], verified=False)
    assert medium_momentum_12_1(bad_verified, expected_dates=dates) is None

    bad_close = list(bars)
    bad_close[5] = MarketObservation(date=dates[5], adj_close=0.0, verified=True)
    assert medium_momentum_12_1(bad_close, expected_dates=dates) is None

    dup_dates = list(dates)
    dup_dates[1] = dup_dates[0]
    with pytest.raises(ValueError, match="unique|exactly equal|order"):
        medium_momentum_12_1(_bars(dup_dates, closes), expected_dates=dates)

    shuffled = list(dates)
    shuffled[0], shuffled[1] = shuffled[1], shuffled[0]
    with pytest.raises(ValueError, match="exactly equal|order"):
        medium_momentum_12_1(_bars(shuffled, closes), expected_dates=dates)

    extra_dates = dates + [_weekdays(dates[-1] + timedelta(days=1), 1)[0]]
    with pytest.raises(ValueError, match="exactly"):
        medium_momentum_12_1(
            _bars(extra_dates, closes + [closes[-1] + 1.0]),
            expected_dates=dates,
        )

    with pytest.raises(ValueError, match="exactly"):
        medium_momentum_12_1(bars[:-1], expected_dates=dates)

    with pytest.raises(ValidationError):
        MarketObservation(date=dates[0], adj_close=True, verified=True)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        MarketObservation(date=dates[0], adj_close=1.0, verified=1)  # type: ignore[arg-type]


def test_exact_forward_return_endpoint_no_shift() -> None:
    symbol = "000001.SZ"
    t = date(2022, 6, 1)
    endpoint = date(2022, 8, 1)
    t_obs = SymbolCloseObservation(symbol=symbol, date=t, adj_close=10.0, verified=True)
    end_obs = SymbolCloseObservation(symbol=symbol, date=endpoint, adj_close=11.0, verified=True)
    assert exact_forward_return(
        symbol=symbol,
        decision_date=t,
        horizon_market_days=40,
        expected_endpoint_date=endpoint,
        observation_t=t_obs,
        observation_endpoint=end_obs,
    ) == pytest.approx(0.1)

    with pytest.raises(ValueError, match="expected_endpoint"):
        exact_forward_return(
            symbol=symbol,
            decision_date=t,
            horizon_market_days=40,
            expected_endpoint_date=endpoint,
            observation_t=t_obs,
            observation_endpoint=SymbolCloseObservation(
                symbol=symbol, date=date(2022, 8, 2), adj_close=11.0, verified=True
            ),
        )

    assert (
        exact_forward_return(
            symbol=symbol,
            decision_date=t,
            horizon_market_days=40,
            expected_endpoint_date=endpoint,
            observation_t=t_obs,
            observation_endpoint=None,
        )
        is None
    )

    assert (
        exact_forward_return(
            symbol=symbol,
            decision_date=t,
            horizon_market_days=40,
            expected_endpoint_date=endpoint,
            observation_t=t_obs,
            observation_endpoint=SymbolCloseObservation(symbol=symbol, date=endpoint, adj_close=11.0, verified=False),
        )
        is None
    )

    with pytest.raises(ValueError, match="same-symbol|symbol"):
        exact_forward_return(
            symbol=symbol,
            decision_date=t,
            horizon_market_days=40,
            expected_endpoint_date=endpoint,
            observation_t=t_obs,
            observation_endpoint=SymbolCloseObservation(
                symbol="000002.SZ", date=endpoint, adj_close=11.0, verified=True
            ),
        )


def test_paired_spearman_and_all_equal() -> None:
    assert paired_spearman([(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]) == pytest.approx(1.0)
    assert paired_spearman([(1.0, 3.0), (2.0, 2.0), (3.0, 1.0)]) == pytest.approx(-1.0)
    assert paired_spearman([(1.0, None), (2.0, 2.0), (3.0, 3.0), (None, 9.0)]) == pytest.approx(1.0)
    assert paired_spearman([(1.0, 1.0), (1.0, 2.0), (1.0, 3.0)]) is None
    assert paired_spearman([(1.0, 5.0), (2.0, 5.0), (3.0, 5.0)]) is None
    assert paired_spearman([(1.0, 1.0)]) is None


def test_quintile_tie_behavior_and_extremes() -> None:
    pairs = [(float(i), float(i) * 10.0) for i in range(5)]
    assert quintile_top_minus_bottom_spread(pairs) == pytest.approx(40.0)

    tied = [(1.0, 10.0), (1.0, 11.0), (1.0, 12.0), (2.0, 20.0), (3.0, 30.0)]
    assert quintile_top_minus_bottom_spread(tied) is None

    assert quintile_top_minus_bottom_spread([(1.0, 1.0)] * 5) is None
    assert quintile_top_minus_bottom_spread([]) is None


def test_coverage_gate_boundaries() -> None:
    assert coverage_gate(known_count=500, eligible_count=500) is True
    assert coverage_gate(known_count=500, eligible_count=833) is True
    assert coverage_gate(known_count=500, eligible_count=834) is False
    assert coverage_gate(known_count=499, eligible_count=500) is False
    assert coverage_gate(known_count=600, eligible_count=1000) is True
    assert coverage_gate(known_count=599, eligible_count=1000) is False
    with pytest.raises(ValueError, match="positive"):
        coverage_gate(known_count=0, eligible_count=0)
    with pytest.raises(ValueError, match="bool"):
        coverage_gate(known_count=True, eligible_count=1000)  # type: ignore[arg-type]


def test_newey_west_numeric_fixture_and_undefined_tails() -> None:
    series = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = newey_west_bartlett_inference(series, lag=1)
    assert result.defined is True
    assert result.mean == pytest.approx(3.0)
    assert result.long_run_variance == pytest.approx(2.8)
    assert result.variance_of_mean == pytest.approx(0.56)
    expected_stat = 3.0 / math.sqrt(0.56)
    assert result.statistic == pytest.approx(expected_stat)
    phi = standard_normal_cdf(expected_stat)
    assert result.positive_p_value == pytest.approx(1.0 - phi)
    assert result.negative_p_value == pytest.approx(phi)
    assert result.positive_p_value is not None and result.positive_p_value < 0.001
    assert result.negative_p_value is not None and result.negative_p_value > 0.999

    undefined = newey_west_bartlett_inference([1.0, 2.0], lag=2)
    assert undefined.defined is False
    assert undefined.statistic is None
    assert undefined.positive_p_value is None
    assert undefined.negative_p_value is None

    constant = newey_west_bartlett_inference([2.0, 2.0, 2.0, 2.0], lag=1)
    assert constant.defined is False
    assert constant.statistic is None

    neg = newey_west_bartlett_inference([-1.0, -2.0, -3.0, -4.0, -5.0], lag=1)
    assert neg.defined is True
    assert neg.statistic is not None and neg.statistic < 0.0
    assert neg.positive_p_value is not None and neg.positive_p_value > 0.999
    assert neg.negative_p_value is not None and neg.negative_p_value < 0.001

    with pytest.raises(ValueError, match="finite|bool"):
        newey_west_bartlett_inference([1.0, float("nan")], lag=0)
    with pytest.raises(ValueError, match="bool"):
        newey_west_bartlett_inference([1.0, True], lag=0)  # type: ignore[list-item]


def test_holm_equal_p_tie_order_first_failure_and_missing() -> None:
    equal = holm_step_down_four_factors(
        {
            "defensive_low_vol": 0.01,
            "medium_momentum_12_1": 0.01,
            "value": 0.01,
            "quality": 0.01,
        }
    )
    assert [item.factor_id for item in equal.results] == list(FROZEN_FACTOR_FAMILY_IDS)
    assert [item.sorted_position for item in equal.results] == [1, 2, 3, 4]
    assert equal.results[0].threshold == pytest.approx(0.05 / 4)
    assert equal.results[1].threshold == pytest.approx(0.05 / 3)
    assert equal.results[2].threshold == pytest.approx(0.05 / 2)
    assert equal.results[3].threshold == pytest.approx(0.05 / 1)
    assert all(item.rejected for item in equal.results)
    assert equal.alpha == 0.05

    stopped = holm_step_down_four_factors(
        {
            "quality": 0.01,
            "value": 0.03,
            "medium_momentum_12_1": 0.02,
            "defensive_low_vol": 0.04,
        }
    )
    by_id = {item.factor_id: item for item in stopped.results}
    assert [item.factor_id for item in stopped.results] == [
        "quality",
        "medium_momentum_12_1",
        "value",
        "defensive_low_vol",
    ]
    assert by_id["quality"].rejected is True
    assert by_id["medium_momentum_12_1"].rejected is False
    assert by_id["value"].rejected is False
    assert by_id["defensive_low_vol"].rejected is False

    missing = holm_step_down_four_factors(
        {
            "quality": None,
            "value": 0.001,
            "medium_momentum_12_1": 0.001,
            "defensive_low_vol": 0.001,
        }
    )
    by_id = {item.factor_id: item for item in missing.results}
    assert by_id["quality"].raw_p_value is None
    assert by_id["quality"].effective_p_value == 1.0
    assert by_id["quality"].rejected is False
    assert by_id["value"].rejected is True
    assert by_id["medium_momentum_12_1"].rejected is True
    assert by_id["defensive_low_vol"].rejected is True

    with pytest.raises(ValueError, match="exactly the four"):
        holm_step_down_four_factors({"quality": 0.1, "value": 0.1, "medium_momentum_12_1": 0.1})


def test_size_band_boundaries() -> None:
    assert size_band(None) == "unknown"
    assert size_band(0.0) == "below_lowest"
    assert size_band(2_999_999_999.0) == "below_lowest"
    assert size_band(3_000_000_000.0) == "3bn_5bn"
    assert size_band(4_999_999_999.0) == "3bn_5bn"
    assert size_band(5_000_000_000.0) == "5bn_10bn"
    assert size_band(9_999_999_999.0) == "5bn_10bn"
    assert size_band(10_000_000_000.0) == "10bn_plus"
    assert size_band(1e12) == "10bn_plus"
    with pytest.raises(ValueError, match="bool"):
        size_band(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        size_band(float("nan"))
    with pytest.raises(ValueError, match="finite"):
        size_band(float("-inf"))


def test_within_cluster_percentiles_singleton_and_unassigned() -> None:
    values = {SYM_A: 1.0, SYM_B: 2.0, SYM_C: 3.0, SYM_D: 4.0, SYM_E: None}
    clusters = {SYM_A: "c1", SYM_B: "c1", SYM_C: "solo", SYM_D: None, SYM_E: "c1"}
    got = within_cluster_percentiles(values, clusters)
    assert got[SYM_A] == 0.0
    assert got[SYM_B] == 100.0
    assert got[SYM_C] is None  # singleton
    assert got[SYM_D] is None  # unassigned
    assert got[SYM_E] is None  # unknown value in non-singleton

    values_rev = {SYM_E: None, SYM_D: 4.0, SYM_C: 3.0, SYM_B: 2.0, SYM_A: 1.0}
    clusters_rev = {SYM_E: "c1", SYM_D: None, SYM_C: "solo", SYM_B: "c1", SYM_A: "c1"}
    assert within_cluster_percentiles(values_rev, clusters_rev) == got

    with pytest.raises(ValueError, match="same symbol set"):
        within_cluster_percentiles({SYM_A: 1.0}, {SYM_A: "c1", SYM_B: "c1"})


def test_module_ast_forbids_scoring_backtest_strategy_pipeline_broker() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
    forbidden_prefixes = (
        "app.scoring",
        "app.backtest",
        "app.strategies",
        "app.pipeline",
        "app.broker",
    )
    for module in imported:
        assert not any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_prefixes)
    for token in ("broker", "ready_for_scoring", "ready_for_trading"):
        if token == "broker":
            assert "broker" not in source


# --- Adversarial protocol-seal tests ---


def test_quality_sealed_protocol_rejects_wrong_keys() -> None:
    base = {k: {SYM_A: 1.0, SYM_B: 2.0} for k in QUALITY_SEALED_RULES}
    quality_family_composite(base)

    missing = {k: v for k, v in base.items() if k != "roe"}
    with pytest.raises(ValueError, match="sealed"):
        quality_family_composite(missing)

    extra = {**base, "extra_metric": {SYM_A: 1.0, SYM_B: 2.0}}
    with pytest.raises(ValueError, match="sealed"):
        quality_family_composite(extra)

    wrong = {("wrong" if k == "roe" else k): v for k, v in base.items()}
    with pytest.raises(ValueError, match="sealed"):
        quality_family_composite(wrong)


def test_value_sealed_protocol_rejects_wrong_keys() -> None:
    base = {k: {SYM_A: 1.0, SYM_B: 2.0} for k in VALUE_SEALED_KEYS}
    value_family_composite(base)

    missing = {k: v for k, v in base.items() if k != "pb"}
    with pytest.raises(ValueError, match="sealed"):
        value_family_composite(missing)

    extra = {**base, "extra": {SYM_A: 1.0, SYM_B: 2.0}}
    with pytest.raises(ValueError, match="sealed"):
        value_family_composite(extra)


def test_holm_alpha_sealed_at_005() -> None:
    valid_results = [
        HolmFactorResult(
            factor_id="quality",
            raw_p_value=0.01,
            effective_p_value=0.01,
            sorted_position=1,
            threshold=0.0125,
            rejected=True,
        ),
        HolmFactorResult(
            factor_id="value",
            raw_p_value=0.01,
            effective_p_value=0.01,
            sorted_position=2,
            threshold=0.05 / 3,
            rejected=True,
        ),
        HolmFactorResult(
            factor_id="medium_momentum_12_1",
            raw_p_value=0.01,
            effective_p_value=0.01,
            sorted_position=3,
            threshold=0.025,
            rejected=True,
        ),
        HolmFactorResult(
            factor_id="defensive_low_vol",
            raw_p_value=0.01,
            effective_p_value=0.01,
            sorted_position=4,
            threshold=0.05,
            rejected=True,
        ),
    ]
    ok = HolmStepDownResult(alpha=0.05, results=valid_results)
    assert ok.alpha == 0.05

    with pytest.raises(ValidationError, match="alpha"):
        HolmStepDownResult(alpha=0.10, results=valid_results)

    with pytest.raises(ValidationError):
        HolmStepDownResult(alpha=True, results=valid_results)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        HolmStepDownResult(alpha=float("nan"), results=valid_results)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        HolmStepDownResult(alpha=float("inf"), results=valid_results)  # type: ignore[arg-type]


def test_size_band_negative_cap_raises() -> None:
    with pytest.raises(ValueError, match="negative"):
        size_band(-1.0)
    with pytest.raises(ValueError, match="negative"):
        size_band(-0.01)
    with pytest.raises(ValueError, match="negative"):
        size_band(-1e12)


def test_symbol_validation_rejects_invalid_formats() -> None:
    invalid_symbols = [
        "",
        " ",
        "A",
        "B",
        "abc",
        "000001",
        "000001.BJ",
        "000001.sz",
        "AAPL",
        " 000001.SZ",
        "000001.SZ ",
        "00001.SZ",
        "0000001.SZ",
        "000001.SH.extra",
        "random_text",
    ]
    for bad in invalid_symbols:
        with pytest.raises(ValueError, match="A-share"):
            average_rank_percentiles({bad: 1.0})

    for bad in invalid_symbols:
        with pytest.raises((ValueError, ValidationError)):
            SymbolCloseObservation(symbol=bad, date=date(2022, 1, 1), adj_close=10.0, verified=True)

    for bad in invalid_symbols:
        with pytest.raises(ValueError, match="A-share|symbol"):
            exact_forward_return(
                symbol=bad,
                decision_date=date(2022, 1, 1),
                horizon_market_days=20,
                expected_endpoint_date=date(2022, 2, 1),
                observation_t={
                    "symbol": "000001.SZ",
                    "date": date(2022, 1, 1),
                    "adj_close": 10.0,
                    "verified": True,
                },
                observation_endpoint=None,
            )

    valid_symbols = ["000001.SZ", "600000.SH", "300001.SZ", "688001.SH"]
    for good in valid_symbols:
        result = average_rank_percentiles({good: 1.0})
        assert result[good] is None  # singleton → unknown


def test_family_composite_entry_rejects_invalid_symbol() -> None:
    with pytest.raises(ValidationError):
        FamilyCompositeEntry(symbol="INVALID", known_component_count=3, raw_composite=50.0, final_percentile=50.0)
    ok = FamilyCompositeEntry(symbol="000001.SZ", known_component_count=3, raw_composite=50.0, final_percentile=50.0)
    assert ok.symbol == "000001.SZ"


# --- P1 immutability / frozen adversarial tests ---


def test_quality_sealed_rules_immutable() -> None:
    """QUALITY_SEALED_RULES must not be modifiable in-place."""
    expected = {
        "roe": "high",
        "roic": "high",
        "grossprofit_margin": "high",
        "debt_to_assets": "low",
        "ocf_to_or": "high",
    }
    assert dict(QUALITY_SEALED_RULES) == expected

    with pytest.raises(TypeError):
        QUALITY_SEALED_RULES["roe"] = "low"  # type: ignore[index]
    with pytest.raises(TypeError):
        QUALITY_SEALED_RULES["new_key"] = "high"  # type: ignore[index]
    with pytest.raises(TypeError):
        del QUALITY_SEALED_RULES["roe"]  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        QUALITY_SEALED_RULES.clear()  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        QUALITY_SEALED_RULES.pop("roe")  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        QUALITY_SEALED_RULES.update({"roe": "low"})  # type: ignore[attr-defined]

    assert dict(QUALITY_SEALED_RULES) == expected


def test_strict_models_frozen_post_construction() -> None:
    """Frozen models must reject attribute assignment after construction."""
    obs = MarketObservation(date=date(2022, 1, 3), adj_close=100.0, verified=True)
    with pytest.raises(ValidationError):
        obs.adj_close = 999.0  # type: ignore[misc]
    with pytest.raises(ValidationError):
        obs.verified = False  # type: ignore[misc]
    assert obs.adj_close == 100.0
    assert obs.verified is True

    sym_obs = SymbolCloseObservation(symbol=SYM_A, date=date(2022, 1, 3), adj_close=10.0, verified=True)
    with pytest.raises(ValidationError):
        sym_obs.adj_close = 999.0  # type: ignore[misc]
    assert sym_obs.adj_close == 10.0

    entry = FamilyCompositeEntry(symbol=SYM_A, known_component_count=3, raw_composite=50.0, final_percentile=50.0)
    with pytest.raises(ValidationError):
        entry.raw_composite = 0.0  # type: ignore[misc]
    assert entry.raw_composite == 50.0


def test_holm_step_down_result_frozen_and_immutable_results() -> None:
    """HolmStepDownResult must be frozen; results must be tuple (no append/extend)."""
    result = holm_step_down_four_factors(
        {
            "quality": 0.01,
            "value": 0.01,
            "medium_momentum_12_1": 0.01,
            "defensive_low_vol": 0.01,
        }
    )

    with pytest.raises(ValidationError):
        result.alpha = 0.10  # type: ignore[misc]
    assert result.alpha == 0.05

    with pytest.raises(ValidationError):
        result.results = ()  # type: ignore[misc,assignment]

    assert isinstance(result.results, tuple)
    assert len(result.results) == 4
    with pytest.raises(AttributeError):
        result.results.append(result.results[0])  # type: ignore[attr-defined]

    for factor_result in result.results:
        with pytest.raises(ValidationError):
            factor_result.rejected = not factor_result.rejected  # type: ignore[misc]
        with pytest.raises(ValidationError):
            factor_result.raw_p_value = 0.99  # type: ignore[misc]
