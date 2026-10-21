from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CACHE_", extra="forbid")

    database_url: SecretStr = SecretStr("postgresql+psycopg://cache:cache@127.0.0.1:5433/cache")
    data_dir: Path = Path("data/payloads")
    pool_size: int = Field(default=5, ge=1, le=50)
    pool_timeout_seconds: float = Field(default=5, gt=0, le=60)
    lock_timeout_ms: int = Field(default=5000, ge=1, le=60000)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError as exc:
            raise ValueError("Expected a PostgreSQL connection URL") from exc
        if url.drivername != "postgresql+psycopg" or not url.database:
            raise ValueError("Use postgresql+psycopg:// with a database name")
        return value
