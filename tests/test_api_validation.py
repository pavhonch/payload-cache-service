from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from pydantic import SecretStr

from payload_cache.app import create_app
from payload_cache.config import Settings
from payload_cache.schemas import MAX_REQUEST_BYTES
from payload_cache.storage import PayloadStore
from tests.helpers import CountingTransformer

pytestmark = pytest.mark.anyio


@pytest.fixture
async def offline_client(tmp_path: Path, anyio_backend: str) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_url=SecretStr("postgresql+psycopg://unused@127.0.0.1:1/unused"),
        data_dir=tmp_path / "payloads",
    )
    app = create_app(settings, CountingTransformer("test-http"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        yield client


@pytest.mark.parametrize(
    "content",
    [
        b'{"list_1":[],"list_2":["a"]}',
        b'{"list_1":[1],"list_2":["a"]}',
        b'{"list_1":[],"list_2":[],"extra":true}',
        b'{"list_1":["\\ud800"],"list_2":["a"]}',
        b'{"list_1":[],"list_2":[],"\\ud800":true}',
        b"invalid JSON",
    ],
)
async def test_invalid_input_is_rejected_before_database_access(
    offline_client: httpx.AsyncClient, content: bytes
) -> None:
    response = await offline_client.post(
        "/payload", content=content, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert "detail" in response.json()


async def test_oversized_body_is_rejected_before_parsing(offline_client: httpx.AsyncClient) -> None:
    response = await offline_client.post(
        "/payload",
        content=b" " * (MAX_REQUEST_BYTES + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


async def test_streamed_body_is_limited_without_content_length(
    offline_client: httpx.AsyncClient,
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b" " * (MAX_REQUEST_BYTES // 2)
        yield b" " * (MAX_REQUEST_BYTES // 2 + 1)

    response = await offline_client.post(
        "/payload", content=chunks(), headers={"content-type": "application/json"}
    )
    assert "content-length" not in response.request.headers
    assert response.status_code == 413


@pytest.mark.parametrize("payload_id", ["invalid", "A" * 64, "a" * 63, "a" * 64 + "\n"])
async def test_malformed_id_is_rejected(offline_client: httpx.AsyncClient, payload_id: str) -> None:
    assert (await offline_client.get(f"/payload/{quote(payload_id, safe='')}")).status_code == 422


async def test_missing_payload_returns_404(offline_client: httpx.AsyncClient) -> None:
    response = await offline_client.get(f"/payload/{'a' * 64}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Payload not found"}


async def test_get_does_not_require_database(
    offline_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    store = PayloadStore(tmp_path / "payloads")
    payload_id = store.publish("A, B")
    response = await offline_client.get(f"/payload/{payload_id}")
    assert response.status_code == 200
    assert response.json() == {"output": "A, B"}
