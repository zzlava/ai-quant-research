from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta

import polars as pl

from app.models.fundamentals import FundamentalSnapshot
from app.models.market import Instrument
from app.models.snapshot import DataSnapshot
from app.storage.hashing import sha256_text
from app.storage.protocol import MarketStore


class FundamentalOverlayStore:
    """Read-only, hashed fundamental overlay on top of a verified market store."""

    def __init__(
        self,
        base: MarketStore,
        snapshot: FundamentalSnapshot,
        tables: dict[str, pl.DataFrame],
    ) -> None:
        market_snapshot_id = base.snapshot().snapshot_id
        bound_snapshot_id = snapshot.base_market_snapshot_id
        if bound_snapshot_id is not None and bound_snapshot_id != market_snapshot_id:
            raise ValueError(
                "fundamental overlay was collected for a different market snapshot: "
                f"expected {bound_snapshot_id}, got {market_snapshot_id}"
            )
        self._base = base
        self._fundamental_snapshot = snapshot
        self._reports = tables["fundamental_reports"]
        self._valuation = tables["daily_valuation"]
        partitions = self._valuation.sort(["date", "symbol"]).partition_by(
            "date", as_dict=True, include_key=True, maintain_order=True
        )
        self._valuation_by_date = {
            key[0]: value for key, value in partitions.items() if isinstance(key[0], date)
        }
        self._valuation_dates = sorted(self._valuation_by_date)

    @property
    def fundamental_snapshot_id(self) -> str:
        return self._fundamental_snapshot.snapshot_id

    @property
    def base_market_snapshot_id(self) -> str:
        """Exact raw market snapshot bound by this overlay."""
        return self._base.snapshot().snapshot_id

    def __getattr__(self, name: str) -> object:
        return getattr(self._base, name)

    def get_fundamental_reports(self, available_by: datetime) -> pl.DataFrame:
        return self._reports.filter(pl.col("available_at") <= available_by)

    def get_daily_valuation(self, available_by: datetime) -> pl.DataFrame:
        # The feature contract only needs the latest value within a 10-day
        # age limit. Keep a conservative 31-day window so an all-market IC run
        # does not repeatedly scan millions of historical valuation rows.
        cutoff = available_by.date()
        stop = bisect_right(self._valuation_dates, cutoff)
        earliest = cutoff - timedelta(days=31)
        selected = [
            self._valuation_by_date[day]
            for day in self._valuation_dates[:stop]
            if day >= earliest
        ]
        if not selected:
            return self._valuation.clear()
        return pl.concat(selected, how="vertical_relaxed").filter(
            pl.col("available_at") <= available_by
        )

    def get_instruments(self) -> list[Instrument]:
        return self._base.get_instruments()

    def get_calendar(self, start: date, end: date) -> list[date]:
        return self._base.get_calendar(start, end)

    def get_daily_bars(
        self, as_of: date, symbol: str | None = None, start: date | None = None
    ) -> pl.DataFrame:
        return self._base.get_daily_bars(as_of=as_of, symbol=symbol, start=start)

    def get_index_bars(
        self, as_of: date, symbol: str | None = None, start: date | None = None
    ) -> pl.DataFrame:
        return self._base.get_index_bars(as_of=as_of, symbol=symbol, start=start)

    def get_global_bars(
        self, as_of: date, symbol: str | None = None, start: date | None = None
    ) -> pl.DataFrame:
        return self._base.get_global_bars(as_of=as_of, symbol=symbol, start=start)

    def get_universe_members(
        self,
        universe_id: str,
        as_of: date,
        available_by: datetime,
        *,
        expected_constituents: int | None = None,
        require_available_cross_section: bool = False,
    ) -> set[str]:
        return self._base.get_universe_members(
            universe_id,
            as_of,
            available_by,
            expected_constituents=expected_constituents,
            require_available_cross_section=require_available_cross_section,
        )

    def next_trading_day(self, after: date) -> date | None:
        return self._base.next_trading_day(after)

    def trading_days_after(self, after: date, n: int) -> list[date]:
        return self._base.trading_days_after(after, n)

    def snapshot(self) -> DataSnapshot:
        market = self._base.snapshot()
        composite = sha256_text(
            f"market_snapshot_id={market.snapshot_id}\n"
            f"fundamental_snapshot_id={self._fundamental_snapshot.snapshot_id}\n"
        )
        hashes = dict(market.table_hashes)
        hashes.update(
            {
                f"fundamental.{name}": value
                for name, value in self._fundamental_snapshot.table_hashes.items()
            }
        )
        counts = dict(market.row_counts)
        counts.update(
            {
                f"fundamental.{name}": value
                for name, value in self._fundamental_snapshot.row_counts.items()
            }
        )
        return market.model_copy(
            update={
                "snapshot_id": composite,
                "content_hash": composite,
                "table_hashes": hashes,
                "row_counts": counts,
                "source_name": f"{market.source_name}+{self._fundamental_snapshot.source_name}",
                "source_version": (
                    f"market={market.source_version or '-'};"
                    f"fundamental={self._fundamental_snapshot.source_version or '-'}"
                ),
            }
        )
