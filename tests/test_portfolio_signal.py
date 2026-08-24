from __future__ import annotations

from datetime import date

import pytest

from app.models.scores import ScoreBreakdown, ScoreResult
from app.research.portfolio_signal import analyze_portfolio_signal, write_portfolio_signal_report
from tests.helpers import fill_quiet_bars, store_from_rows, weekdays, zero_cost_config


def test_portfolio_signal_uses_next_open_and_reports_top_tail(tmp_path) -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    rows = []
    for symbol, final_close in (("AAA", 12.0), ("BBB", 11.0), ("CCC", 9.0), ("DDD", 8.0)):
        rows.extend(
            fill_quiet_bars(
                symbol,
                calendar,
                {
                    calendar[1]: {"open": 10.0, "close": 10.0},
                    calendar[3]: {"open": final_close, "close": final_close},
                },
            )
        )
    store = store_from_rows(calendar, rows)
    config = zero_cost_config()

    def scores(as_of: date) -> list[ScoreResult]:
        del as_of
        return [
            _score("AAA", 90.0),
            _score("BBB", 80.0),
            _score("CCC", 20.0),
            _score("DDD", 10.0),
        ]

    report = analyze_portfolio_signal(
        store=store,
        config=config,
        start=calendar[0],
        end=calendar[0],
        horizons=[2],
        top_k=2,
        quantiles=2,
        score_fn=scores,
    )

    summary = report.summaries[0]
    assert summary.scoring_days == 1
    assert summary.mean_top_k_gross_return == pytest.approx(0.15)
    assert summary.mean_top_k_estimated_net_return == pytest.approx(0.15)
    assert summary.mean_long_short_spread == pytest.approx(0.3)
    output = tmp_path / "portfolio.json"
    write_portfolio_signal_report(report, output)
    assert '"entry_rule":"next_trading_day_adjusted_open"' in output.read_text(encoding="utf-8").replace(" ", "")


def _score(symbol: str, value: float) -> ScoreResult:
    return ScoreResult(
        symbol=symbol,
        score_date=date(2024, 1, 2),
        strategy_name="test",
        strategy_version="1",
        strategy_config_hash="test",
        final_score=value,
        breakdown=ScoreBreakdown(
            market_score=50.0,
            global_score=50.0,
            sector_score=50.0,
            alpha_score=value,
            crowding_risk=0.0,
            execution_risk=0.0,
            final_score=value,
        ),
    )
