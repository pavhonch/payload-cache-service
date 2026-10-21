import pytest
from pydantic import SecretStr, ValidationError

from payload_cache.config import Settings


def test_settings_read_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CACHE_POOL_SIZE", "3")
    monkeypatch.setenv("CACHE_DATABASE_URL", "postgresql+psycopg://cache:secret@localhost/cache")
    settings = Settings()
    assert settings.pool_size == 3
    assert "secret" not in repr(settings)


@pytest.mark.parametrize("url", ["sqlite:///cache.db", "postgresql://localhost/cache", "invalid"])
def test_database_driver_is_validated(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=SecretStr(url))


@pytest.mark.parametrize("pool_size", [0, 51])
def test_invalid_pool_size_is_rejected(pool_size: int) -> None:
    with pytest.raises(ValidationError):
        Settings(pool_size=pool_size)
