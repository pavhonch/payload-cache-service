from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.sharedctypes import Synchronized
from multiprocessing.synchronize import Barrier
from multiprocessing.synchronize import Event as ProcessEvent
from threading import Event

import pytest
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.cache import TransformCache
from payload_cache.config import Settings
from payload_cache.database import build_engine, build_session_factory
from payload_cache.errors import CacheUnavailable
from tests.helpers import CountingTransformer

pytestmark = pytest.mark.integration


def wait_for_advisory_waiter(sessions: sessionmaker[Session], application_name: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with sessions() as session:
            waiting = session.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                    "WHERE l.locktype = 'advisory' AND NOT l.granted "
                    "AND a.application_name = :name)"
                ),
                {"name": application_name},
            )
        if waiting:
            return
        Event().wait(0.01)
    raise AssertionError("The second worker did not wait for the cache key lock")


def test_lock_is_held_until_commit(
    cache_sessions: sessionmaker[Session], cache_version: str
) -> None:
    committing = Event()
    release = Event()
    transformer = CountingTransformer(cache_version)
    first_cache = TransformCache(cache_sessions, transformer)
    second_cache = TransformCache(cache_sessions, transformer)

    def pause_commit(session: Session) -> None:
        if session.new:
            committing.set()
            if not release.wait(15):
                raise RuntimeError("Timed out waiting to release the test commit")

    event.listen(cache_sessions, "before_commit", pause_commit)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(first_cache.transform_many, ["shared"])
            try:
                assert committing.wait(5)
                second = executor.submit(second_cache.transform_many, ["shared"])
                wait_for_advisory_waiter(cache_sessions, cache_version)
                assert transformer.calls == ["shared"]
            finally:
                release.set()
            assert first.result(timeout=5) == {"shared": "SHARED"}
            assert second.result(timeout=5) == {"shared": "SHARED"}
    finally:
        event.remove(cache_sessions, "before_commit", pause_commit)
    assert transformer.calls == ["shared"]


def test_different_keys_can_transform_concurrently(
    cache_sessions: sessionmaker[Session], cache_version: str
) -> None:
    entered = Event()
    release = Event()

    class BlockingTransformer(CountingTransformer):
        def transform(self, value: str) -> str:
            result = super().transform(value)
            if value == "a":
                entered.set()
                if not release.wait(15):
                    raise RuntimeError("Timed out waiting to release the test transformer")
            return result

    transformer = BlockingTransformer(cache_version)
    cache = TransformCache(cache_sessions, transformer)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(cache.transform_many, ["a"])
        try:
            assert entered.wait(5)
            second = executor.submit(cache.transform_many, ["b"])
            assert second.result(timeout=5) == {"b": "B"}
        finally:
            release.set()
        assert first.result(timeout=5) == {"a": "A"}
    assert transformer.calls == ["a", "b"]


def test_lock_timeout_rolls_back_and_allows_a_retry(
    cache_sessions: sessionmaker[Session], cache_version: str
) -> None:
    committing = Event()
    release = Event()
    transformer = CountingTransformer(cache_version)
    first_cache = TransformCache(cache_sessions, transformer)
    impatient_cache = TransformCache(cache_sessions, transformer, lock_timeout_ms=75)

    def pause_commit(session: Session) -> None:
        if session.new:
            committing.set()
            if not release.wait(15):
                raise RuntimeError("Timed out waiting to release the test commit")

    event.listen(cache_sessions, "before_commit", pause_commit)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(first_cache.transform_many, ["shared"])
            try:
                assert committing.wait(5)
                with pytest.raises(CacheUnavailable):
                    impatient_cache.transform_many(["shared"])
                assert transformer.calls == ["shared"]
            finally:
                release.set()
            assert first.result(timeout=5) == {"shared": "SHARED"}
    finally:
        event.remove(cache_sessions, "before_commit", pause_commit)
    assert impatient_cache.transform_many(["shared"]) == {"shared": "SHARED"}
    assert transformer.calls == ["shared"]


class ProcessTransformer:
    def __init__(
        self,
        version: str,
        calls: Synchronized[int],
        entered: ProcessEvent,
        release: ProcessEvent,
    ) -> None:
        self.version = version
        self.calls = calls
        self.entered = entered
        self.release = release

    def transform(self, value: str) -> str:
        with self.calls.get_lock():
            self.calls.value += 1
        self.entered.set()
        if not self.release.wait(15):
            raise RuntimeError("Timed out waiting to release the process transformer")
        return value.upper()


def transform_in_process(
    database_url: str,
    version: str,
    calls: Synchronized[int],
    entered: ProcessEvent,
    release: ProcessEvent,
    ready: Barrier,
) -> None:
    settings = Settings(database_url=SecretStr(database_url), pool_size=1)
    engine = build_engine(settings)
    try:
        cache = TransformCache(
            build_session_factory(engine),
            ProcessTransformer(version, calls, entered, release),
            lock_timeout_ms=10000,
        )
        ready.wait(timeout=15)
        assert cache.transform_many(["shared"]) == {"shared": "SHARED"}
    finally:
        engine.dispose()


@pytest.mark.timeout(45)
def test_separate_processes_share_one_transformation(
    cache_sessions: sessionmaker[Session], cache_settings: Settings, cache_version: str
) -> None:
    context = multiprocessing.get_context("spawn")
    calls = context.Value("i", 0)
    entered = context.Event()
    release = context.Event()
    ready = context.Barrier(3)
    workers = [
        context.Process(
            target=transform_in_process,
            args=(
                cache_settings.database_url.get_secret_value(),
                cache_version,
                calls,
                entered,
                release,
                ready,
            ),
        )
        for _ in range(2)
    ]
    try:
        for worker in workers:
            worker.start()
        ready.wait(timeout=15)
        assert entered.wait(5)
        wait_for_advisory_waiter(cache_sessions, cache_version)
        assert calls.value == 1
        release.set()
        for worker in workers:
            worker.join(timeout=5)
            assert worker.exitcode == 0
        assert calls.value == 1
    finally:
        release.set()
        ready.abort()
        for worker in workers:
            if worker.pid is not None:
                worker.join(timeout=5)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
                worker.close()
