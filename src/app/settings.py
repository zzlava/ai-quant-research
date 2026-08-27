from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIQ_", extra="ignore")

    data_dir: Path = Path("data")
    config_dir: Path = Path("config")
    database_url: str = "sqlite:///data/app.db"
    public_reconstruction_dir: Path | None = None
    fundamental_dir: Path | None = None
    ownership_dir: Path | None = None
    event_dir: Path | None = None

    @property
    def parquet_dir(self) -> Path:
        return self.data_dir / "parquet"

    @property
    def scores_dir(self) -> Path:
        return self.data_dir / "scores"

    @property
    def strategies_dir(self) -> Path:
        return self.config_dir / "strategies"


def get_settings() -> Settings:
    return Settings()
