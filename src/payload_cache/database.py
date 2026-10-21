from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.config import Settings


def build_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url.get_secret_value(),
        isolation_level="READ COMMITTED",
        pool_pre_ping=True,
        hide_parameters=True,
        pool_size=settings.pool_size,
        max_overflow=0,
        pool_timeout=settings.pool_timeout_seconds,
        connect_args={
            "connect_timeout": 5,
            "options": f"-c lock_timeout={settings.lock_timeout_ms}",
        },
    )


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
