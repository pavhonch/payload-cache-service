from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.cache import TransformCache
from payload_cache.errors import CacheUnavailable, PayloadTooLarge
from payload_cache.payloads import PayloadService
from payload_cache.schemas import PayloadInput
from payload_cache.storage import PayloadStore
from tests.helpers import CountingTransformer

pytestmark = pytest.mark.integration


def test_file_published_before_failed_index_write_is_reused(
    cache_sessions: sessionmaker[Session],
    cache_version: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transformer = CountingTransformer(cache_version)
    store = PayloadStore(tmp_path / "payloads")
    store.prepare()
    service = PayloadService(cache_sessions, TransformCache(cache_sessions, transformer), store)
    payload = PayloadInput(list_1=["a"], list_2=["b"])

    def fail(request_hash: bytes, payload_id: str) -> None:
        raise CacheUnavailable("Simulated index failure")

    with monkeypatch.context() as patch:
        patch.setattr(service, "_remember_request", fail)
        with pytest.raises(CacheUnavailable):
            service.create(payload)
    published = list(store.directory.glob("*.json"))
    assert len(published) == 1
    assert store.read(published[0].stem).output == "A, B"
    assert service.create(payload) == published[0].stem
    assert transformer.calls == ["a", "b"]


def test_repeated_large_parts_are_limited_before_joining(
    cache_sessions: sessionmaker[Session], cache_version: str, tmp_path: Path
) -> None:
    transformer = CountingTransformer(cache_version, lambda value: "a" * (1024 * 1024))
    store = PayloadStore(tmp_path / "payloads")
    store.prepare()
    service = PayloadService(cache_sessions, TransformCache(cache_sessions, transformer), store)
    with pytest.raises(PayloadTooLarge):
        service.create(PayloadInput(list_1=["x"] * 1000, list_2=["x"] * 1000))
    assert list(store.directory.iterdir()) == []
    valid_id = service.create(PayloadInput(list_1=["x"], list_2=["x"]))
    assert store.read(valid_id).output == "a" * (1024 * 1024) + ", " + "a" * (1024 * 1024)
    assert transformer.calls == ["x"]
