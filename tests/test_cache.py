import hashlib
from typing import cast

import pytest
from sqlalchemy import event, insert, text
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.cache import TransformCache
from payload_cache.config import Settings
from payload_cache.database import build_engine, build_session_factory
from payload_cache.errors import (
    CacheIntegrityError,
    CacheUnavailable,
    InvalidTransformation,
    PayloadTooLarge,
    TransformerUnavailable,
)
from payload_cache.models import Transformation
from payload_cache.schemas import MAX_OUTPUT_BYTES
from tests.helpers import CountingTransformer

pytestmark = pytest.mark.integration


def test_duplicates_and_partial_overlap(
    cache_sessions: sessionmaker[Session], cache_version: str
) -> None:
    transformer = CountingTransformer(cache_version)
    cache = TransformCache(cache_sessions, transformer)
    assert cache.transform_many(["a", "b", "a", "b", "c", "a"]) == {"a": "A", "b": "B", "c": "C"}
    assert transformer.calls == ["a", "b", "c"]
    assert cache.transform_many(["a", "b", "c"]) == {"a": "A", "b": "B", "c": "C"}
    assert transformer.calls == ["a", "b", "c"]
    assert cache.transform_many(["a", "d"]) == {"a": "A", "d": "D"}
    assert transformer.calls == ["a", "b", "c", "d"]


def test_empty_and_unicode_results_are_cached(
    cache_sessions: sessionmaker[Session], cache_version: str
) -> None:
    transformer = CountingTransformer(cache_version)
    cache = TransformCache(cache_sessions, transformer)
    values = ["", " Привіт\x00Straße "]
    expected = {value: value.upper() for value in values}
    assert cache.transform_many(values) == expected
    assert cache.transform_many(values) == expected
    assert transformer.calls == values


def test_cache_keys_preserve_case_and_whitespace(
    cache_sessions: sessionmaker[Session], cache_version: str
) -> None:
    transformer = CountingTransformer(cache_version)
    cache = TransformCache(cache_sessions, transformer)
    values = ["a", "A", " a", "a "]
    expected = {value: value.upper() for value in values}
    assert cache.transform_many(values) == expected
    assert cache.transform_many(values) == expected
    assert transformer.calls == values


def test_cache_survives_a_new_engine(
    cache_sessions: sessionmaker[Session], cache_settings: Settings, cache_version: str
) -> None:
    first = CountingTransformer(cache_version)
    assert TransformCache(cache_sessions, first).transform_many(["a"]) == {"a": "A"}
    second = CountingTransformer(cache_version)
    engine = build_engine(cache_settings)
    try:
        cache = TransformCache(build_session_factory(engine), second)
        assert cache.transform_many(["a"]) == {"a": "A"}
    finally:
        engine.dispose()
    assert first.calls == ["a"]
    assert second.calls == []


def test_new_version_does_not_reuse_old_results(
    cache_sessions: sessionmaker[Session], cache_version: str
) -> None:
    first = CountingTransformer(cache_version)
    second = CountingTransformer(f"{cache_version}-next", str.lower)
    assert TransformCache(cache_sessions, first).transform_many(["MiXeD"]) == {"MiXeD": "MIXED"}
    assert TransformCache(cache_sessions, second).transform_many(["MiXeD"]) == {"MiXeD": "mixed"}
    assert first.calls == ["MiXeD"]
    assert second.calls == ["MiXeD"]


def test_failure_preserves_successful_results(
    cache_sessions: sessionmaker[Session], cache_version: str
) -> None:
    transformer = CountingTransformer(cache_version)
    transformer.failures.add("b")
    cache = TransformCache(cache_sessions, transformer)
    with pytest.raises(TransformerUnavailable):
        cache.transform_many(["a", "b", "c"])
    transformer.failures.clear()
    assert cache.transform_many(["a", "b", "c"]) == {"a": "A", "b": "B", "c": "C"}
    assert transformer.calls == ["a", "b", "b", "c"]


def test_lost_connection_before_commit_does_not_report_success(
    cache_sessions: sessionmaker[Session], cache_version: str
) -> None:
    transformer = CountingTransformer(cache_version)
    cache = TransformCache(cache_sessions, transformer)

    def disconnect_before_commit(session: Session) -> None:
        if session.new:
            pid: int = session.connection().execute(text("SELECT pg_backend_pid()")).scalar_one()
            with cache_sessions() as control:
                assert control.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})

    event.listen(cache_sessions, "before_commit", disconnect_before_commit)
    try:
        with pytest.raises(CacheUnavailable):
            cache.transform_many(["a"])
    finally:
        event.remove(cache_sessions, "before_commit", disconnect_before_commit)
    assert transformer.calls == ["a"]
    assert cache.transform_many(["a"]) == {"a": "A"}
    assert transformer.calls == ["a", "a"]


def test_all_cached_values_are_returned_for_a_large_request(
    cache_sessions: sessionmaker[Session], cache_version: str
) -> None:
    values = [f"value-{index}" for index in range(2000)]
    with cache_sessions.begin() as session:
        session.execute(
            insert(Transformation),
            [
                {
                    "transformer_version": cache_version,
                    "source_hash": hashlib.sha256(value.encode()).digest(),
                    "source": value.encode(),
                    "result": value.upper().encode(),
                }
                for value in values
            ],
        )
    transformer = CountingTransformer(cache_version)
    assert TransformCache(cache_sessions, transformer).transform_many(values) == {
        value: value.upper() for value in values
    }
    assert transformer.calls == []


@pytest.mark.parametrize(("source", "result"), [(b"wrong source", b"A"), (b"a", b"\xff")])
def test_corrupt_cache_entries_are_rejected(
    cache_sessions: sessionmaker[Session], cache_version: str, source: bytes, result: bytes
) -> None:
    with cache_sessions.begin() as session:
        session.add(
            Transformation(
                transformer_version=cache_version,
                source_hash=hashlib.sha256(b"a").digest(),
                source=source,
                result=result,
            )
        )
    transformer = CountingTransformer(cache_version)
    with pytest.raises(CacheIntegrityError):
        TransformCache(cache_sessions, transformer).transform_many(["a"])
    assert transformer.calls == []


@pytest.mark.parametrize("result", [123, "\ud800"])
def test_invalid_transformer_output_is_not_cached(
    cache_sessions: sessionmaker[Session], cache_version: str, result: object
) -> None:
    class BadTransformer:
        version = cache_version

        def transform(self, value: str) -> str:
            return cast(str, result)

    with pytest.raises(InvalidTransformation):
        TransformCache(cache_sessions, BadTransformer()).transform_many(["a"])
    healthy = CountingTransformer(cache_version)
    assert TransformCache(cache_sessions, healthy).transform_many(["a"]) == {"a": "A"}
    assert healthy.calls == ["a"]


def test_oversized_transformation_is_not_cached(
    cache_sessions: sessionmaker[Session], cache_version: str
) -> None:
    class LargeTransformer:
        version = cache_version

        def transform(self, value: str) -> str:
            return "a" * (MAX_OUTPUT_BYTES + 1)

    with pytest.raises(PayloadTooLarge):
        TransformCache(cache_sessions, LargeTransformer()).transform_many(["a"])
    healthy = CountingTransformer(cache_version)
    assert TransformCache(cache_sessions, healthy).transform_many(["a"]) == {"a": "A"}
    assert healthy.calls == ["a"]
