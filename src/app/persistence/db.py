from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.persistence.models import Base
from app.settings import get_settings


def get_engine(url: str | None = None) -> Engine:
    settings = get_settings()
    db_url = url or settings.database_url
    if db_url.startswith("sqlite"):
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    return create_engine(db_url, future=True)


def init_db(engine: Engine | None = None) -> Engine:
    eng = engine or get_engine()
    Base.metadata.create_all(eng)
    return eng
