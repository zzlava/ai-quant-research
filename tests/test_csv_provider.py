from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.demo.generator import generate_demo_market
from app.providers._frames import bars_to_frame, global_to_frame, instruments_to_frame
from app.providers.csv_provider import CsvProvider


def test_csv_provider_reads_offline_files(tmp_path: Path) -> None:
    bundle = generate_demo_market(
        seed=42, n_stocks=4, start=date(2023, 6, 1), end=date(2024, 3, 29)
    )
    bars_to_frame(bundle.daily_bars).write_csv(tmp_path / "daily_bars.csv")
    bars_to_frame(bundle.index_bars).write_csv(tmp_path / "index_bars.csv")
    global_to_frame(bundle.global_bars).write_csv(tmp_path / "global_bars.csv")
    instruments_to_frame(bundle.instruments).write_csv(tmp_path / "instruments.csv")
    pl.DataFrame({"date": bundle.calendar}).write_csv(tmp_path / "calendar.csv")

    provider = CsvProvider(tmp_path)
    assert provider.get_calendar(date(2024, 1, 2), date(2024, 1, 31))
    daily = provider.get_daily_bars("STK0001", date(2024, 1, 2), date(2024, 1, 31))
    assert daily.height > 0
    assert set(provider.get_sector(item.symbol) for item in provider.get_instruments()[:4])
