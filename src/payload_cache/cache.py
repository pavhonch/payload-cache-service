import hashlib
import re
from collections.abc import Iterable
from itertools import batched

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, TimeoutError
from sqlalchemy.orm import Session, sessionmaker

from payload_cache.errors import (
    CacheIntegrityError,
    CacheUnavailable,
    InvalidTransformation,
    PayloadTooLarge,
)
from payload_cache.models import Transformation
from payload_cache.schemas import MAX_OUTPUT_BYTES
from payload_cache.transformer import Transformer


class TransformCache:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        transformer: Transformer,
        *,
        lock_timeout_ms: int = 5000,
    ) -> None:
        version = transformer.version
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", version) is None:
            raise ValueError("Transformer version must be 1-64 ASCII letters, digits, dots, - or _")
        if lock_timeout_ms <= 0:
            raise ValueError("Lock timeout must be positive")
        self._sessions = sessions
        self._transformer = transformer
        self._version = version
        self._lock_timeout_ms = lock_timeout_ms

    @property
    def version(self) -> str:
        return self._version

    def transform_many(self, values: Iterable[str]) -> dict[str, str]:
        digests = {
            value: hashlib.sha256(value.encode("utf-8")).digest() for value in dict.fromkeys(values)
        }
        if not digests:
            return {}
        try:
            results = self._get_many(digests)
            for value, digest in digests.items():
                if value not in results:
                    results[value] = self._get_or_transform(value, digest)
            return results
        except (OperationalError, TimeoutError) as exc:
            raise CacheUnavailable("Transformation cache is temporarily unavailable") from exc

    def _get_many(self, digests: dict[str, bytes]) -> dict[str, str]:
        sources = {digest: value for value, digest in digests.items()}
        results: dict[str, str] = {}
        with self._sessions() as session:
            for chunk in batched(sources, 500, strict=False):
                rows = session.scalars(
                    select(Transformation).where(
                        Transformation.transformer_version == self._version,
                        Transformation.source_hash.in_(chunk),
                    )
                )
                for row in rows:
                    source = sources[row.source_hash]
                    results[source] = self._read_result(row, source)
        return results

    def _get_or_transform(self, value: str, digest: bytes) -> str:
        with self._sessions.begin() as session:
            session.execute(
                select(func.set_config("lock_timeout", str(self._lock_timeout_ms), True))
            )
            lock_digest = hashlib.sha256(self._version.encode("ascii") + b"\x00" + digest).digest()
            lock_key = int.from_bytes(lock_digest[:8], "big", signed=True)
            session.execute(select(func.pg_advisory_xact_lock(lock_key)))

            # Recheck after acquiring the lock; another transaction may have filled the cache.
            row = session.get(Transformation, (self._version, digest))
            if row is not None:
                result = self._read_result(row, value)
            else:
                result = self._transformer.transform(value)
                encoded = self._encode_result(result)
                session.add(
                    Transformation(
                        transformer_version=self._version,
                        source_hash=digest,
                        source=value.encode("utf-8"),
                        result=encoded,
                    )
                )
        return result

    @staticmethod
    def _encode_result(result: str) -> bytes:
        if not isinstance(result, str):
            raise InvalidTransformation("Transformer must return a string")
        try:
            encoded = result.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InvalidTransformation("Transformer returned invalid Unicode") from exc
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise PayloadTooLarge("Transformed string exceeds the output size limit")
        return encoded

    @staticmethod
    def _read_result(row: Transformation, source: str) -> str:
        if row.source != source.encode("utf-8"):
            raise CacheIntegrityError("Cached source does not match its digest")
        if len(row.result) > MAX_OUTPUT_BYTES:
            raise CacheIntegrityError("Cached result exceeds the output size limit")
        try:
            return row.result.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CacheIntegrityError("Cached result is not valid UTF-8") from exc
