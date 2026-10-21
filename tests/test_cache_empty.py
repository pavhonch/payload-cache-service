from sqlalchemy import create_engine

from payload_cache.cache import TransformCache
from payload_cache.database import build_session_factory
from tests.helpers import CountingTransformer


def test_empty_input_does_not_connect_to_database() -> None:
    engine = create_engine("postgresql+psycopg://unused@127.0.0.1:1/unused")
    transformer = CountingTransformer("test-v1")
    try:
        cache = TransformCache(build_session_factory(engine), transformer)
        assert cache.transform_many([]) == {}
        assert transformer.calls == []
    finally:
        engine.dispose()
