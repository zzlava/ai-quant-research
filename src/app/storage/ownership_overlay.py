from __future__ import annotations

from datetime import date, datetime

import polars as pl

from app.models.market import Instrument
from app.models.ownership import OwnershipSnapshot
from app.models.snapshot import DataSnapshot
from app.storage.hashing import sha256_text
from app.storage.protocol import MarketStore


class OwnershipOverlayStore:
    """Read-only ownership overlay bound to exact market and fundamental data."""

    def __init__(
        self,
        base: MarketStore,
        snapshot: OwnershipSnapshot,
        table: pl.DataFrame,
    ) -> None:
        base_market_snapshot_id = getattr(base, "base_market_snapshot_id", None)
        fundamental_snapshot_id = getattr(base, "fundamental_snapshot_id", None)
        if base_market_snapshot_id != snapshot.base_market_snapshot_id:
            raise ValueError(
                "ownership overlay was collected for a different market snapshot: "
                f"expected {snapshot.base_market_snapshot_id}, got {base_market_snapshot_id}"
            )
        if fundamental_snapshot_id != snapshot.fundamental_snapshot_id:
            raise ValueError(
                "ownership overlay was collected for a different fundamental snapshot: "
                f"expected {snapshot.fundamental_snapshot_id}, got {fundamental_snapshot_id}"
            )
        self._base = base
        self._ownership_snapshot = snapshot
        self._holders = table

    @property
    def ownership_snapshot_id(self) -> str:
        return self._ownership_snapshot.snapshot_id

    @property
    def fundamental_snapshot_id(self) -> str:
        return self._ownership_snapshot.fundamental_snapshot_id

    @property
    def base_market_snapshot_id(self) -> str:
        return self._ownership_snapshot.base_market_snapshot_id

    def __getattr__(self, name: str) -> object:
        return getattr(self._base, name)

    def get_top10_float_holders(self, available_by: datetime) -> pl.DataFrame:
        return self._holders.filter(pl.col("available_at") <= available_by)

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
        base = self._base.snapshot()
        composite = sha256_text(
            f"base_snapshot_id={base.snapshot_id}\n"
            f"ownership_snapshot_id={self._ownership_snapshot.snapshot_id}\n"
        )
        hashes = dict(base.table_hashes)
        hashes["ownership.top10_float_holders"] = self._ownership_snapshot.table_hash
        counts = dict(base.row_counts)
        counts["ownership.top10_float_holders"] = self._ownership_snapshot.row_count
        return base.model_copy(
            update={
                "snapshot_id": composite,
                "content_hash": composite,
                "table_hashes": hashes,
                "row_counts": counts,
                "source_name": f"{base.source_name}+{self._ownership_snapshot.source_name}",
                "source_version": (
                    f"base={base.source_version or '-'};"
                    f"ownership={self._ownership_snapshot.source_version or '-'}"
                ),
            }
        )
