import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from pydantic import ValidationError

from payload_cache.errors import (
    PayloadIntegrityError,
    PayloadNotFound,
    PayloadTooLarge,
    StorageError,
)
from payload_cache.schemas import MAX_OUTPUT_BYTES, PayloadOutput

logger = logging.getLogger(__name__)


class PayloadStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def prepare(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=self.directory):
                pass
        except OSError as exc:
            raise StorageError("Payload directory is not writable") from exc

    def read(self, payload_id: str) -> PayloadOutput:
        path = self._path(payload_id)
        try:
            with path.open("rb") as file:
                data = file.read(MAX_OUTPUT_BYTES + 1)
        except FileNotFoundError as exc:
            raise PayloadNotFound("Payload not found") from exc
        except OSError as exc:
            raise StorageError("Payload could not be read") from exc
        if len(data) > MAX_OUTPUT_BYTES or hashlib.sha256(data).hexdigest() != payload_id:
            raise PayloadIntegrityError("Payload content does not match its identifier")
        try:
            return PayloadOutput.model_validate_json(data)
        except ValidationError as exc:
            raise PayloadIntegrityError("Stored payload is invalid") from exc

    def publish(self, output: str) -> str:
        data = json.dumps(
            {"output": output}, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(data) > MAX_OUTPUT_BYTES:
            raise PayloadTooLarge("Serialized payload exceeds the output size limit")
        payload_id = hashlib.sha256(data).hexdigest()
        if self._reuse(payload_id, output):
            return payload_id

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.directory, prefix=".payload-", suffix=".tmp", delete=False
            ) as file:
                temporary_path = Path(file.name)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            try:
                # A hard link publishes a complete immutable file without replacing another writer.
                os.link(temporary_path, self._path(payload_id))
            except FileExistsError:
                if not self._reuse(payload_id, output):
                    raise StorageError("Concurrent payload publication disappeared") from None
            temporary_path.unlink()
            temporary_path = None
            self._sync_directory()
        except OSError as exc:
            raise StorageError("Payload could not be saved") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove temporary payload file")
        return payload_id

    def _reuse(self, payload_id: str, output: str) -> bool:
        try:
            existing = self.read(payload_id)
        except PayloadNotFound:
            return False
        if existing.output != output:
            raise PayloadIntegrityError("Payload identifier refers to different content")
        try:
            self._sync_directory()
        except OSError as exc:
            raise StorageError("Payload publication could not be synchronized") from exc
        return True

    def _path(self, payload_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", payload_id) is None:
            raise ValueError("Invalid payload identifier")
        return self.directory / f"{payload_id}.json"

    def _sync_directory(self) -> None:
        if os.name == "posix":
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
