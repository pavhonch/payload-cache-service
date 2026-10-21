import asyncio
import os
import re
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.app import create_app
from payload_cache.config import Settings
from payload_cache.models import RequestPayload, Transformation
from tests.helpers import CountingTransformer

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_sample_payload(
    api_client: httpx.AsyncClient, api_transformer: CountingTransformer, payload_directory: Path
) -> None:
    response = await api_client.post(
        "/payload",
        json={
            "list_1": ["first string", "second string", "third string"],
            "list_2": ["other string", "another string", "last string"],
        },
    )
    assert response.status_code == 200
    payload_id = response.json()["id"]
    assert re.fullmatch(r"[0-9a-f]{64}", payload_id)
    assert response.json()["message"] == "Payload is ready"
    read = await api_client.get(f"/payload/{payload_id}")
    assert read.status_code == 200
    expected = (
        "FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING, THIRD STRING, LAST STRING"
    )
    assert read.json() == {"output": expected}
    assert read.headers["content-type"] == "application/json"
    assert len(api_transformer.calls) == 6
    assert len(list(payload_directory.glob("*.json"))) == 1


async def test_repeated_request_uses_the_request_index(
    api_client: httpx.AsyncClient,
    api_transformer: CountingTransformer,
    cache_sessions: sessionmaker[Session],
    cache_version: str,
) -> None:
    first = await api_client.post("/payload", json={"list_1": ["a"], "list_2": ["b"]})
    assert first.status_code == 200
    with cache_sessions.begin() as session:
        session.execute(
            delete(Transformation).where(Transformation.transformer_version == cache_version)
        )
    repeated = await api_client.post(
        "/payload",
        content=b'{ "list_2": ["b"], "list_1": ["a"] }',
        headers={"content-type": "application/json"},
    )
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert api_transformer.calls == ["a", "b"]


async def test_duplicate_strings_and_partial_overlap(
    api_client: httpx.AsyncClient, api_transformer: CountingTransformer
) -> None:
    first = await api_client.post(
        "/payload", json={"list_1": ["a", "b", "a"], "list_2": ["b", "c", "a"]}
    )
    read = await api_client.get(f"/payload/{first.json()['id']}")
    assert read.json() == {"output": "A, B, B, C, A, A"}
    assert api_transformer.calls == ["a", "b", "c"]
    second = await api_client.post("/payload", json={"list_1": ["a"], "list_2": ["d"]})
    assert second.status_code == 200
    assert api_transformer.calls == ["a", "b", "c", "d"]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ((["a"], ["b"]), (["A"], ["B"])),
        ((["a, b"], ["c"]), (["a"], ["b, c"])),
    ],
)
async def test_identical_output_reuses_the_file(
    api_client: httpx.AsyncClient,
    payload_directory: Path,
    first: tuple[list[str], list[str]],
    second: tuple[list[str], list[str]],
) -> None:
    responses = [
        await api_client.post("/payload", json={"list_1": one, "list_2": two})
        for one, two in [first, second]
    ]
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert len(list(payload_directory.glob("*.json"))) == 1


async def test_empty_lists_do_not_call_transformer(
    api_client: httpx.AsyncClient, api_transformer: CountingTransformer
) -> None:
    response = await api_client.post("/payload", json={"list_1": [], "list_2": []})
    assert response.status_code == 200
    read = await api_client.get(f"/payload/{response.json()['id']}")
    assert read.json() == {"output": ""}
    assert api_transformer.calls == []


async def test_concurrent_identical_requests(
    api_client: httpx.AsyncClient, api_transformer: CountingTransformer, payload_directory: Path
) -> None:
    responses = await asyncio.gather(
        *[api_client.post("/payload", json={"list_1": ["a"], "list_2": ["b"]}) for _ in range(8)]
    )
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    assert sorted(api_transformer.calls) == ["a", "b"]
    assert len(list(payload_directory.iterdir())) == 1


async def test_missing_file_is_rebuilt_from_cached_transformations(
    api_client: httpx.AsyncClient, api_transformer: CountingTransformer, payload_directory: Path
) -> None:
    payload = {"list_1": ["a"], "list_2": ["b"]}
    first = await api_client.post("/payload", json=payload)
    payload_id = first.json()["id"]
    (payload_directory / f"{payload_id}.json").unlink()
    assert (await api_client.get(f"/payload/{payload_id}")).status_code == 404
    repeated = await api_client.post("/payload", json=payload)
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert api_transformer.calls == ["a", "b"]


async def test_transformer_failure_keeps_successful_cached_values(
    api_client: httpx.AsyncClient, api_transformer: CountingTransformer, payload_directory: Path
) -> None:
    api_transformer.failures.add("b")
    payload = {"list_1": ["a"], "list_2": ["b"]}
    failed = await api_client.post("/payload", json=payload)
    assert failed.status_code == 503
    assert list(payload_directory.iterdir()) == []
    api_transformer.failures.clear()
    assert (await api_client.post("/payload", json=payload)).status_code == 200
    assert api_transformer.calls == ["a", "b", "b"]


async def test_corrupt_file_returns_an_internal_error(
    api_client: httpx.AsyncClient, api_transformer: CountingTransformer, payload_directory: Path
) -> None:
    payload = {"list_1": ["a"], "list_2": ["b"]}
    created = await api_client.post("/payload", json=payload)
    payload_id = created.json()["id"]
    (payload_directory / f"{payload_id}.json").write_bytes(b"broken")
    for response in [
        await api_client.get(f"/payload/{payload_id}"),
        await api_client.post("/payload", json=payload),
    ]:
        assert response.status_code == 500
        assert response.json() == {"detail": "Internal server error"}
    assert api_transformer.calls == ["a", "b"]


async def test_new_app_reuses_existing_payload(
    api_client: httpx.AsyncClient,
    cache_settings: Settings,
    cache_version: str,
    payload_directory: Path,
) -> None:
    payload = {"list_1": ["a"], "list_2": ["b"]}
    first = await api_client.post("/payload", json=payload)
    transformer = CountingTransformer(cache_version)
    app = create_app(cache_settings.model_copy(update={"data_dir": payload_directory}), transformer)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        repeated = await client.post("/payload", json=payload)
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert transformer.calls == []


async def test_changed_transformer_version_bypasses_old_request_index(
    api_client: httpx.AsyncClient,
    cache_settings: Settings,
    cache_version: str,
    payload_directory: Path,
) -> None:
    payload = {"list_1": ["MiXeD"], "list_2": ["TeXt"]}
    first = await api_client.post("/payload", json=payload)
    transformer = CountingTransformer(f"{cache_version}-next", str.lower)
    app = create_app(cache_settings.model_copy(update={"data_dir": payload_directory}), transformer)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        second = await client.post("/payload", json=payload)
        read = await client.get(f"/payload/{second.json()['id']}")
    assert second.status_code == 200
    assert second.json()["id"] != first.json()["id"]
    assert read.json() == {"output": "mixed, text"}
    assert transformer.calls == ["MiXeD", "TeXt"]


async def test_unexpected_transformer_error_is_not_a_dependency_error(
    api_client: httpx.AsyncClient,
    api_transformer: CountingTransformer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(value: str) -> str:
        raise RuntimeError("Private debugging detail")

    monkeypatch.setattr(api_transformer, "_operation", broken)
    response = await api_client.post("/payload", json={"list_1": ["a"], "list_2": ["b"]})
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}


async def test_failed_publication_does_not_create_an_index_entry(
    api_client: httpx.AsyncClient,
    api_transformer: CountingTransformer,
    cache_sessions: sessionmaker[Session],
    payload_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("Private disk detail")

    payload = {"list_1": ["a"], "list_2": ["b"]}
    with monkeypatch.context() as patch:
        patch.setattr(os, "link", fail)
        response = await api_client.post("/payload", json=payload)
        assert response.status_code == 500
    assert list(payload_directory.iterdir()) == []
    with cache_sessions() as session:
        assert session.scalar(select(func.count()).select_from(RequestPayload)) == 0
    assert (await api_client.post("/payload", json=payload)).status_code == 200
    assert api_transformer.calls == ["a", "b"]
