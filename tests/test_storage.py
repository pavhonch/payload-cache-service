import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import NoReturn

import pytest

from payload_cache.errors import (
    PayloadIntegrityError,
    PayloadNotFound,
    PayloadTooLarge,
    StorageError,
)
from payload_cache.schemas import MAX_OUTPUT_BYTES
from payload_cache.storage import PayloadStore


@pytest.fixture
def store(tmp_path: Path) -> PayloadStore:
    result = PayloadStore(tmp_path / "payloads")
    result.prepare()
    return result


def test_payload_is_canonical_and_reused(store: PayloadStore) -> None:
    output = "Привіт\x00, A"
    payload_id = store.publish(output)
    path = store.directory / f"{payload_id}.json"
    expected = '{"output":"Привіт\\u0000, A"}'.encode()
    assert path.read_bytes() == expected
    assert payload_id == hashlib.sha256(expected).hexdigest()
    assert store.publish(output) == payload_id
    assert store.read(payload_id).output == output
    assert [file.name for file in store.directory.iterdir()] == [path.name]


def test_empty_output_is_a_valid_payload(store: PayloadStore) -> None:
    assert store.read(store.publish("")).output == ""


def test_concurrent_publishers_reuse_one_complete_file(store: PayloadStore) -> None:
    output = "A, Привіт " * 10000
    with ThreadPoolExecutor(max_workers=8) as executor:
        identifiers = list(executor.map(store.publish, [output] * 16))
    assert len(set(identifiers)) == 1
    assert store.read(identifiers[0]).output == output
    assert len(list(store.directory.iterdir())) == 1


def test_file_is_not_visible_before_publication(
    store: PayloadStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = Event()
    release = Event()
    real_link = os.link
    payload_id = hashlib.sha256(b'{"output":"A"}').hexdigest()

    def delayed_link(source: Path, target: Path) -> None:
        ready.set()
        if not release.wait(10):
            raise RuntimeError("Timed out waiting to publish the test payload")
        real_link(source, target)

    monkeypatch.setattr(os, "link", delayed_link)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(store.publish, "A")
        try:
            assert ready.wait(5)
            with pytest.raises(PayloadNotFound):
                store.read(payload_id)
        finally:
            release.set()
        assert future.result(timeout=5) == payload_id
    assert store.read(payload_id).output == "A"


@pytest.mark.parametrize("operation", ["fsync", "link"])
def test_failed_publication_leaves_no_partial_files(
    store: PayloadStore, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    def fail(*args: object, **kwargs: object) -> NoReturn:
        raise OSError("Simulated disk failure")

    monkeypatch.setattr(os, operation, fail)
    with pytest.raises(StorageError):
        store.publish("A")
    assert list(store.directory.iterdir()) == []


def test_corrupt_payload_is_not_returned_or_overwritten(store: PayloadStore) -> None:
    payload_id = store.publish("A")
    path = store.directory / f"{payload_id}.json"
    path.write_bytes(b"incomplete")
    with pytest.raises(PayloadIntegrityError):
        store.read(payload_id)
    with pytest.raises(PayloadIntegrityError):
        store.publish("A")
    assert path.read_bytes() == b"incomplete"


def test_directory_sync_failure_can_be_retried(
    store: PayloadStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> NoReturn:
        raise OSError("Simulated directory sync failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_sync_directory", fail)
        with pytest.raises(StorageError):
            store.publish("A")
    published = list(store.directory.glob("*.json"))
    assert len(published) == 1
    assert store.publish("A") == published[0].stem
    assert store.read(published[0].stem).output == "A"


@pytest.mark.parametrize(
    "content",
    [b"invalid", b'{"output":1}', b'{"output":"a","extra":true}', b'{"output":"\\ud800"}'],
)
def test_invalid_payload_with_a_matching_digest_is_rejected(
    store: PayloadStore, content: bytes
) -> None:
    payload_id = hashlib.sha256(content).hexdigest()
    (store.directory / f"{payload_id}.json").write_bytes(content)
    with pytest.raises(PayloadIntegrityError):
        store.read(payload_id)


def test_missing_payload_returns_not_found(store: PayloadStore) -> None:
    with pytest.raises(PayloadNotFound):
        store.read("a" * 64)


def test_read_permission_error_is_not_not_found(
    store: PayloadStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload_id = store.publish("A")

    def deny_open(path: Path, mode: str) -> NoReturn:
        raise PermissionError("Simulated permission failure")

    monkeypatch.setattr(Path, "open", deny_open)
    with pytest.raises(StorageError):
        store.read(payload_id)


@pytest.mark.parametrize("payload_id", ["../secret", "A" * 64, "z" * 64, "a" * 63])
def test_invalid_identifiers_cannot_be_used_as_paths(store: PayloadStore, payload_id: str) -> None:
    with pytest.raises(ValueError):
        store.read(payload_id)


def test_serialized_size_limit_includes_json_escaping(store: PayloadStore) -> None:
    with pytest.raises(PayloadTooLarge):
        store.publish("\x00" * (MAX_OUTPUT_BYTES // 6))
    assert list(store.directory.iterdir()) == []


def test_payload_at_the_size_limit_can_be_read(store: PayloadStore) -> None:
    overhead = len(b'{"output":""}')
    output = "a" * (MAX_OUTPUT_BYTES - overhead)
    assert store.read(store.publish(output)).output == output


def test_directory_must_be_writable_and_not_a_file(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"not a directory")
    with pytest.raises(StorageError):
        PayloadStore(path).prepare()
