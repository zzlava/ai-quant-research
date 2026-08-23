from __future__ import annotations

import shutil
import uuid
from datetime import date
from pathlib import Path

from app.errors import DataQualityError, TushareFetchError
from app.models.config import StrategyConfig
from app.models.snapshot import TABLE_NAMES, DataSnapshot
from app.providers.tushare_client import TushareQueryClient
from app.providers.tushare_normalize import require_ts_code
from app.providers.tushare_provider import TushareProvider
from app.storage.import_market import import_market_data


def read_symbols_file(path: Path) -> list[str]:
    codes: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        codes.append(require_ts_code(line, kind="stock"))
    if not codes:
        raise TushareFetchError("symbols file is empty")
    return list(dict.fromkeys(codes))


def write_normalized_tables(tables: dict, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for name in TABLE_NAMES:
        if name not in tables:
            raise DataQualityError(f"normalized Tushare output missing {name}")
        tables[name].write_parquet(dest / f"{name}.parquet")
    return dest


def fetch_tushare_and_import(
    *,
    start: date,
    end: date,
    config: StrategyConfig,
    dest_dir: Path,
    stocks: list[str],
    source_version: str | None = None,
    client: TushareQueryClient | None = None,
    source_name: str = "tushare",
) -> DataSnapshot:
    if not stocks:
        raise TushareFetchError(
            "stock universe is empty; pass --symbols-file. "
            "--index-universe is disabled because end-date constituents look ahead"
        )
    provider = TushareProvider(client=client)
    tables = provider.fetch(start, end, config=config, stocks=stocks)
    parent = Path(dest_dir).parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f".tushare-norm-{uuid.uuid4().hex}"
    try:
        write_normalized_tables(tables, tmp)
        return import_market_data(
            tmp,
            dest_dir,
            source_name=source_name,
            adjustment=config.data.adjustment,
            source_version=source_version,
            market_index=config.data.market_index,
            global_symbol=config.data.global_symbol,
        )
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
