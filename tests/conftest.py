import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.app import create_app
from payload_cache.config import Settings
from payload_cache.database import build_engine, build_session_factory
from payload_cache.models import RequestPayload, Transformation
from tests.helpers import CountingTransformer


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
                connection.execute(delete(RequestPayload))
                connection.execute(
                    delete(Transformation).where(
                        Transformation.transformer_version.in_(
                            [cache_version, f"{cache_version}-next"]
                        )
                    )
                )
        finally:
            engine.dispose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def api_transformer(cache_version: str) -> CountingTransformer:
    return CountingTransformer(cache_version)


@pytest.fixture
def payload_directory(tmp_path: Path) -> Path:
    return tmp_path / "payloads"


@pytest.fixture
def api_app(
    cache_settings: Settings,
    cache_sessions: sessionmaker[Session],
    api_transformer: CountingTransformer,
    payload_directory: Path,
) -> FastAPI:
    settings = cache_settings.model_copy(update={"data_dir": payload_directory})
    return create_app(settings, api_transformer)


@pytest.fixture
async def api_client(api_app: FastAPI, anyio_backend: str) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        api_app.router.lifespan_context(api_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        yield client
