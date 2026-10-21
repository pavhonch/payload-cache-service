import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, TimeoutError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from payload_cache.cache import TransformCache
from payload_cache.config import Settings
from payload_cache.database import build_engine, build_session_factory
from payload_cache.errors import (
    CacheIntegrityError,
    CacheUnavailable,
    InvalidTransformation,
    PayloadNotFound,
    PayloadTooLarge,
    StorageError,
    TransformerUnavailable,
)
from payload_cache.middleware import RequestSizeLimit
from payload_cache.payloads import PayloadService
from payload_cache.schemas import (
    HealthResponse,
    PayloadConfirmation,
    PayloadId,
    PayloadInput,
    PayloadOutput,
)
from payload_cache.storage import PayloadStore
from payload_cache.transformer import Transformer, UppercaseTransformer

logger = logging.getLogger(__name__)


async def handle_error(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, PayloadNotFound):
        return JSONResponse(status_code=404, content={"detail": "Payload not found"})
    if isinstance(exc, PayloadTooLarge):
        return JSONResponse(
            status_code=413, content={"detail": "Payload exceeds the output size limit"}
        )
    if isinstance(exc, (CacheUnavailable, TransformerUnavailable)):
        logger.warning("Payload dependency unavailable: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503, content={"detail": "Service is temporarily unavailable"}
        )
    logger.error("Payload request failed", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


async def handle_validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    details = [
        {"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()
    ]
    return Response(
        json.dumps({"detail": details}, ensure_ascii=True),
        status_code=422,
        media_type="application/json",
    )


def create_app(settings: Settings | None = None, transformer: Transformer | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(config)
        app.state.engine = engine
        try:
            store = PayloadStore(config.data_dir)
            await run_in_threadpool(store.prepare)
            sessions = build_session_factory(engine)
            cache = TransformCache(
                sessions,
                transformer if transformer is not None else UppercaseTransformer(),
                lock_timeout_ms=config.lock_timeout_ms,
            )
            app.state.payloads = PayloadService(sessions, cache, store)
            yield
        finally:
            await run_in_threadpool(engine.dispose)

    app = FastAPI(title="Payload Cache Service", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestSizeLimit)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    for error_type in (
        PayloadNotFound,
        PayloadTooLarge,
        CacheUnavailable,
        TransformerUnavailable,
        CacheIntegrityError,
        InvalidTransformation,
        StorageError,
        Exception,
    ):
        app.add_exception_handler(error_type, handle_error)

    @app.post(
        "/payload",
        response_model=PayloadConfirmation,
        summary="Create or reuse a payload",
        responses={
            413: {"description": "Request or generated payload exceeds the size limit"},
            503: {"description": "A required dependency is temporarily unavailable"},
        },
    )
    def create_payload(payload: PayloadInput, request: Request) -> PayloadConfirmation:
        service: PayloadService = request.app.state.payloads
        return PayloadConfirmation(id=service.create(payload))

    @app.get(
        "/payload/{payload_id}",
        response_model=PayloadOutput,
        summary="Read a generated payload",
        responses={404: {"description": "Payload not found"}},
    )
    def read_payload(payload_id: PayloadId, request: Request) -> PayloadOutput:
        service: PayloadService = request.app.state.payloads
        return service.read(payload_id)

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
