from __future__ import annotations

from datetime import date

import pytest

from app.models.backtest import TradeFill
from app.models.market import Instrument
from app.research.portfolio_robustness import _cost_stress_config, _sector_attribution, _symbol_attribution
from tests.test_portfolio_construction import _fixed_horizon_config


def _trade(symbol: str, *, pnl: float, gross_pnl: float, entry: date, exit_: date) -> TradeFill:
    return TradeFill(
        symbol=symbol,
        entry_date=entry,
        exit_date=exit_,
        entry_price=10.01,
        exit_price=9.98,
        shares=100,
        pnl=pnl,
        return_pct=pnl / 1_006.0,
        holding_days=20,
        exit_reason="timeout",
        buy_commission=5.0,
        sell_commission=5.0,
        stamp_tax=1.0,
        entry_raw_price=10.0,
        exit_raw_price=10.0,
        buy_slippage=1.0,
        sell_slippage=2.0,
        gross_pnl=gross_pnl,
    )


def test_symbol_and_sector_attribution_are_cost_explicit() -> None:
    start = date(2024, 1, 2)
    end = date(2024, 1, 30)
    instruments = {
        "AAA": Instrument(symbol="AAA", name="A", sector="bank", listing_date=date(2010, 1, 1)),
        "BBB": Instrument(symbol="BBB", name="B", sector="bank", listing_date=date(2010, 1, 1)),
    }
    symbols = _symbol_attribution(
        [
            _trade("AAA", pnl=-114.0, gross_pnl=-100.0, entry=start, exit_=end),
            _trade("BBB", pnl=86.0, gross_pnl=100.0, entry=start, exit_=end),
        ],
        instruments,
        80_000,
    )

    assert [item.symbol for item in symbols] == ["AAA", "BBB"]
    assert symbols[0].trading_costs == pytest.approx(14.0)
    assert symbols[0].gross_pnl == pytest.approx(-100.0)
    assert symbols[0].net_pnl == pytest.approx(-114.0)
    sectors = _sector_attribution(symbols, 80_000)
    assert len(sectors) == 1
    assert sectors[0].sector == "bank"
    assert sectors[0].entry_notional_share == pytest.approx(1.0)
    assert sectors[0].net_pnl == pytest.approx(-28.0)


def test_cost_stress_changes_only_declared_cost_rates() -> None:
    config = _fixed_horizon_config()
    stressed = _cost_stress_config(
        config,
        scenario_id="severe",
        commission_multiplier=4.0,
        slippage_multiplier=5.0,
    )

    assert stressed.portfolio == config.portfolio
    assert stressed.trade == config.trade
    assert stressed.fundamental_ranking == config.fundamental_ranking
    assert stressed.costs.commission_rate == pytest.approx(config.costs.commission_rate * 4)
    assert stressed.costs.slippage_bps == pytest.approx(config.costs.slippage_bps * 5)
    assert stressed.costs.stamp_tax_schedule == config.costs.stamp_tax_schedule
