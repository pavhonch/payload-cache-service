import hashlib
import json
from itertools import chain

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError, TimeoutError
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.cache import TransformCache
from payload_cache.errors import (
    CacheIntegrityError,
    CacheUnavailable,
    PayloadNotFound,
    PayloadTooLarge,
)
from payload_cache.models import RequestPayload
from payload_cache.schemas import MAX_OUTPUT_BYTES, PayloadInput, PayloadOutput
from payload_cache.storage import PayloadStore

OUTPUT_FORMAT_VERSION = "comma-json-v1"


class PayloadService:
    def __init__(
        self, sessions: sessionmaker[Session], cache: TransformCache, store: PayloadStore
    ) -> None:
        self._sessions = sessions
        self._cache = cache
        self._store = store

    def create(self, payload: PayloadInput) -> str:
        request_hash = hashlib.sha256(
            json.dumps(
                [self._cache.version, OUTPUT_FORMAT_VERSION, payload.list_1, payload.list_2],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).digest()
        try:
            existing = self._find_request(request_hash)
            if existing is not None:
                try:
                    self._store.read(existing)
                except PayloadNotFound:
                    pass
                else:
                    return existing
            values = list(chain.from_iterable(zip(payload.list_1, payload.list_2, strict=True)))
            transformed = self._cache.transform_many(values)
            parts: list[str] = []
            output_size = 0
            for value in values:
                part = transformed[value]
                output_size += len(part.encode("utf-8")) + (2 if parts else 0)
                if output_size > MAX_OUTPUT_BYTES:
                    raise PayloadTooLarge("Combined output exceeds the output size limit")
                parts.append(part)
            payload_id = self._store.publish(", ".join(parts))
            self._remember_request(request_hash, payload_id)
            return payload_id
        except (OperationalError, TimeoutError) as exc:
            raise CacheUnavailable("Payload index is temporarily unavailable") from exc

    def read(self, payload_id: str) -> PayloadOutput:
        return self._store.read(payload_id)

    def _find_request(self, request_hash: bytes) -> str | None:
        with self._sessions() as session:
            return session.scalar(
                select(RequestPayload.payload_id).where(RequestPayload.request_hash == request_hash)
            )

    def _remember_request(self, request_hash: bytes, payload_id: str) -> None:
        statement = (
            insert(RequestPayload)
            .values(request_hash=request_hash, payload_id=payload_id)
            .on_conflict_do_nothing(index_elements=[RequestPayload.request_hash])
            .returning(RequestPayload.payload_id)
        )
        with self._sessions.begin() as session:
            stored_id = session.scalar(statement)
            if stored_id is None:
                stored_id = session.scalar(
                    select(RequestPayload.payload_id).where(
                        RequestPayload.request_hash == request_hash
                    )
                )
            if stored_id != payload_id:
                raise CacheIntegrityError("Request index refers to an inconsistent payload")
