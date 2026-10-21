import json
import os
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO

import httpx
from pydantic import BaseModel, ValidationError
from pydantic_settings import CliApp, SettingsError

from payload_cache.cli_settings import CliSettings
from payload_cache.schemas import (
    MAX_OUTPUT_BYTES,
    MAX_REQUEST_BYTES,
    PayloadConfirmation,
    PayloadInput,
    PayloadOutput,
)


class InputError(ValueError):
    pass


class ProtocolError(ValueError):
    pass


def _validation_message(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'input'}: {item['msg']}"
        for item in error.errors(include_input=False, include_context=False, include_url=False)
    )


def _request_body(settings: CliSettings, stdin: IO[str]) -> bytes:
    if settings.json_input is not None:
        raw = settings.json_input
    elif settings.input_file == "-":
        raw = stdin.read(MAX_REQUEST_BYTES + 1)
    else:
        assert settings.input_file is not None
        with Path(settings.input_file).open(encoding="utf-8-sig", newline="") as source:
            raw = source.read(MAX_REQUEST_BYTES + 1)
    raw = raw.removeprefix("\ufeff")
    if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise InputError("Input exceeds the 1 MiB request limit")
    try:
        payload = PayloadInput.model_validate_json(raw)
    except ValidationError as exc:
        raise InputError(_validation_message(exc)) from exc
    body = json.dumps(payload.model_dump(), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(body) > MAX_REQUEST_BYTES:
        raise InputError("Serialized input exceeds the 1 MiB request limit")
    return body


def _check_output_path(settings: CliSettings) -> None:
    if settings.input_file is None or settings.input_file == "-" or settings.output_file == "-":
        return
    source, destination = Path(settings.input_file), Path(settings.output_file)
    if source.resolve() == destination.resolve() or (
        destination.exists() and source.samefile(destination)
    ):
        raise InputError("Input and output must not be the same file")


@contextmanager
def _output(destination: str, stdout: IO[str]) -> Iterator[IO[str]]:
    if destination == "-":
        yield stdout
        return
    path = Path(destination)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=".cache-cli-",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            yield target.file
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _response[ResponseModel: BaseModel](
    client: httpx.Client,
    method: str,
    url: str,
    model: type[ResponseModel],
    body: bytes | None = None,
) -> ResponseModel:
    with client.stream(
        method,
        url,
        content=body,
        headers={"Content-Type": "application/json"} if body is not None else None,
    ) as response:
        if response.status_code != 200:
            raise ProtocolError(f"{method} returned HTTP {response.status_code}")
        data = bytearray()
        for chunk in response.iter_bytes():
            if len(data) + len(chunk) > MAX_OUTPUT_BYTES:
                raise ProtocolError(f"{method} response exceeds the 4 MiB limit")
            data.extend(chunk)
    try:
        return model.model_validate_json(data)
    except ValidationError as exc:
        raise ProtocolError(f"{method} returned an invalid payload response") from exc


def execute(settings: CliSettings, client: httpx.Client, stdin: IO[str], stdout: IO[str]) -> None:
    _check_output_path(settings)
    try:
        body = _request_body(settings, stdin)
    except UnicodeError as exc:
        raise InputError("Input must be valid UTF-8") from exc
    url = str(settings.host).rstrip("/") + "/payload"
    with _output(settings.output_file, stdout) as output:
        for _ in range(settings.repeat):
            created = _response(client, "POST", url, PayloadConfirmation, body)
            payload = _response(client, "GET", f"{url}/{created.id}", PayloadOutput)
            output.write(
                json.dumps({"id": created.id, "output": payload.output}, ensure_ascii=False)
            )
            output.write("\n")
            output.flush()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        settings = CliApp.run(CliSettings, cli_args=list(argv) if argv is not None else None)
    except (ValidationError, SettingsError) as exc:
        message = _validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
        print(f"cache-cli: {message}", file=sys.stderr)
        return 2
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            execute(settings, client, sys.stdin, sys.stdout)
    except InputError as exc:
        print(f"cache-cli: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, httpx.HTTPError, ProtocolError) as exc:
        print(f"cache-cli: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cache-cli: interrupted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
