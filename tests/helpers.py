from collections.abc import Callable
from threading import Lock

from payload_cache.errors import TransformerUnavailable


class CountingTransformer:
    def __init__(self, version: str, operation: Callable[[str], str] = str.upper) -> None:
        self.version = version
        self.calls: list[str] = []
        self.failures: set[str] = set()
        self._lock = Lock()
        self._operation = operation

    def transform(self, value: str) -> str:
        with self._lock:
            self.calls.append(value)
            if value in self.failures:
                raise TransformerUnavailable("Test transformer is unavailable")
        return self._operation(value)
