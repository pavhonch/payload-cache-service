import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.config import Settings
from payload_cache.database import build_engine, build_session_factory
from payload_cache.models import Transformation


@pytest.fixture
def migration_config() -> Config:
    return Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))


@pytest.fixture
def database_settings(monkeypatch: pytest.MonkeyPatch, migration_config: Config) -> Settings:
    raw_url = os.environ.get("CACHE_TEST_DATABASE_URL")
    if raw_url is None:
        pytest.skip("Set CACHE_TEST_DATABASE_URL to a dedicated PostgreSQL database")
    database = make_url(raw_url).database
    if not database or not database.endswith("_test"):
        pytest.fail("The integration database name must end with _test")
    monkeypatch.setenv("CACHE_DATABASE_URL", raw_url)
    settings = Settings(database_url=SecretStr(raw_url))
    command.upgrade(migration_config, "head")
    return settings


@pytest.fixture
def cache_version() -> str:
    return f"test-{uuid.uuid4().hex}"


@pytest.fixture
def cache_settings(database_settings: Settings, cache_version: str) -> Settings:
    url = make_url(database_settings.database_url.get_secret_value()).update_query_dict(
        {"application_name": cache_version}
    )
    return Settings(
        database_url=SecretStr(url.render_as_string(hide_password=False)),
        pool_size=5,
        lock_timeout_ms=10000,
    )


@pytest.fixture
def cache_sessions(cache_settings: Settings, cache_version: str) -> Iterator[sessionmaker[Session]]:
    engine = build_engine(cache_settings)
    try:
        yield build_session_factory(engine)
    finally:
        try:
            with engine.begin() as connection:
                connection.execute(
                    delete(Transformation).where(
                        Transformation.transformer_version.in_(
                            [cache_version, f"{cache_version}-next"]
                        )
                    )
                )
        finally:
            engine.dispose()
