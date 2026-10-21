import asyncio
import hashlib
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, insert, select
from sqlalchemy.exc import IntegrityError

from payload_cache.app import create_app
from payload_cache.config import Settings
from payload_cache.database import build_engine
from payload_cache.models import RequestPayload, Transformation

pytestmark = pytest.mark.integration


@pytest.fixture
def connection(database_settings: Settings) -> Iterator[Connection]:
    engine = build_engine(database_settings)
    try:
        with engine.connect() as connection, connection.begin() as transaction:
            yield connection
            transaction.rollback()
    finally:
        engine.dispose()


def test_models_match_migrated_schema(
    database_settings: Settings, migration_config: Config
) -> None:
    command.check(migration_config)


def test_migration_can_be_reapplied(database_settings: Settings, migration_config: Config) -> None:
    command.downgrade(migration_config, "base")
    command.upgrade(migration_config, "head")
    command.check(migration_config)


def test_cache_preserves_unicode_and_nul(connection: Connection) -> None:
    source = "Привіт\x00\U0001f680".encode()
    result = source.decode().upper().encode()
    source_hash = hashlib.sha256(source).digest()
    connection.execute(
        insert(Transformation).values(
            transformer_version="test-v1",
            source_hash=source_hash,
            source=source,
            result=result,
        )
    )
    row = connection.execute(
        select(Transformation.source, Transformation.result).where(
            Transformation.transformer_version == "test-v1",
            Transformation.source_hash == source_hash,
        )
    ).one()
    assert row.source == source
    assert row.result == result


def test_cache_key_includes_transformer_version(connection: Connection) -> None:
    source_hash = hashlib.sha256(b"a").digest()
    for version, result in [("test-upper", b"A"), ("test-lower", b"a")]:
        connection.execute(
            insert(Transformation).values(
                transformer_version=version,
                source_hash=source_hash,
                source=b"a",
                result=result,
            )
        )
    results = connection.scalars(
        select(Transformation.result).where(Transformation.source_hash == source_hash)
    ).all()
    assert set(results) == {b"A", b"a"}


def test_invalid_cache_digest_is_rejected(connection: Connection) -> None:
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            insert(Transformation).values(
                transformer_version="test-v1", source_hash=b"short", source=b"a", result=b"A"
            )
        )


@pytest.mark.parametrize("payload_id", ["a" * 63, "A" * 64, "z" * 64])
def test_invalid_payload_id_is_rejected(connection: Connection, payload_id: str) -> None:
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            insert(RequestPayload).values(request_hash=b"x" * 32, payload_id=payload_id)
        )


def test_health_checks_live_database(database_settings: Settings, tmp_path: Path) -> None:
    settings = database_settings.model_copy(update={"data_dir": tmp_path / "payloads"})
    app = create_app(settings)

    async def request_health() -> httpx.Response:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            return await client.get("/health")

    response = asyncio.run(request_health())
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
