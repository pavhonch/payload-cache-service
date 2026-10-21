import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, TimeoutError

from payload_cache.config import Settings
from payload_cache.database import build_engine
from payload_cache.schemas import HealthResponse

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(config)
        app.state.engine = engine
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="Payload Cache Service", version="0.1.0", lifespan=lifespan)

    @app.get("/health", response_model=HealthResponse, summary="Check database connectivity")
    def health() -> HealthResponse:
        try:
            with app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except (OperationalError, TimeoutError) as exc:
            logger.warning("Database health check failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database is unavailable",
            ) from exc
        return HealthResponse()

    return app
