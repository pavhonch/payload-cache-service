import io
import json
import os
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import anyio
import httpx
import pytest
from pydantic import ValidationError
from pydantic_settings import CliApp

from payload_cache import cli
from payload_cache.cli_settings import CliSettings
from payload_cache.schemas import MAX_OUTPUT_BYTES, MAX_REQUEST_BYTES
from tests.helpers import CountingTransformer

INPUT = '{"list_1":["hello"],"list_2":["світ"]}'
PAYLOAD_ID = "a" * 64


def settings(*args: str) -> CliSettings:
    return CliApp.run(CliSettings, cli_args=list(args))


def success(request: httpx.Request) -> httpx.Response:
    if request.method == "POST":
        return httpx.Response(200, json={"id": PAYLOAD_ID, "message": "Payload is ready"})
    return httpx.Response(200, json={"output": "HELLO, СВІТ"})


def test_short_options_and_defaults() -> None:
    options = settings("-H", "https://example.com/api/", "-r", "2", "-j", INPUT, "-o", "out.jsonl")
    assert str(options.host) == "https://example.com/api/"
    assert options.repeat == 2
    assert options.json_input == INPUT
    assert options.output_file == "out.jsonl"
    defaults = settings("--input", "-")
    assert defaults.repeat == 1
    assert defaults.output_file == "-"


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["-i", "-", "-j", INPUT],
        ["-j", INPUT, "-r", "0"],
        ["-j", INPUT, "-r", "-1"],
        ["-j", INPUT, "-r", "1.5"],
        ["-j", INPUT, "-i", ""],
        ["-j", INPUT, "-o", ""],
        ["-j", INPUT, "-o", "bad\x00path"],
    ],
)
def test_invalid_options(args: list[str]) -> None:
    with pytest.raises(ValidationError):
        settings(*args)


@pytest.mark.parametrize(
    "host",
    [
        "ftp://example.com",
        "example.com",
        "http://user:password@example.com",
        "http://example.com/?token=secret",
        "http://example.com/#fragment",
        "http://example.com/?",
    ],
)
def test_invalid_hosts(host: str) -> None:
    with pytest.raises(ValidationError):
        settings("-j", INPUT, "--host", host)


def test_environment_and_dotenv_do_not_supply_cli_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("REPEAT=999\nHOST=https://wrong.example", encoding="utf-8")
    monkeypatch.setenv("REPEAT", "999")
    monkeypatch.setenv("HOST", "https://wrong.example")
    monkeypatch.setenv("JSON", INPUT)
    with pytest.raises(ValidationError):
        settings()
    options = settings("-j", INPUT)
    assert options.repeat == 1
    assert str(options.host) == "http://localhost:8000/"


def test_repeat_reuses_input_preserves_base_path_and_emits_json_lines() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return success(request)

    source, output = io.StringIO(INPUT), io.StringIO()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        cli.execute(
            settings("-i", "-", "-r", "3", "-H", "http://example.com/api"), client, source, output
        )
    assert [request.method for request in requests] == ["POST", "GET"] * 3
    assert {str(request.url) for request in requests} == {
        "http://example.com/api/payload",
        f"http://example.com/api/payload/{PAYLOAD_ID}",
    }
    assert all(json.loads(request.content) == json.loads(INPUT) for request in requests[::2])
    assert all(request.headers["content-type"] == "application/json" for request in requests[::2])
    assert [json.loads(line) for line in output.getvalue().splitlines()] == [
        {"id": PAYLOAD_ID, "output": "HELLO, СВІТ"}
    ] * 3


def test_bom_input_file_and_atomic_output(tmp_path: Path) -> None:
    source, target = tmp_path / "input.json", tmp_path / "output.jsonl"
    source.write_text(INPUT, encoding="utf-8-sig")
    target.write_text("old result", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert target.read_text(encoding="utf-8") == "old result"
        return success(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        cli.execute(
            settings("-i", str(source), "-o", str(target)), client, io.StringIO(), io.StringIO()
        )
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "id": PAYLOAD_ID,
        "output": "HELLO, СВІТ",
    }
    assert list(tmp_path.glob(".cache-cli-*")) == []


@pytest.mark.parametrize("same_file", ["direct", "relative", "hardlink"])
def test_rejects_input_output_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_file: str
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "input.json"
    source.write_text(INPUT, encoding="utf-8")
    target = source
    if same_file == "relative":
        target = Path("input.json")
    elif same_file == "hardlink":
        target = tmp_path / "alias.json"
        os.link(source, target)
    with (
        httpx.Client(transport=httpx.MockTransport(success)) as client,
        pytest.raises(cli.InputError, match="same file"),
    ):
        cli.execute(
            settings("-i", str(source), "-o", str(target)), client, io.StringIO(), io.StringIO()
        )
    assert source.read_text(encoding="utf-8") == INPUT


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{",
        "{}",
        '{"list_1":[1],"list_2":["a"]}',
        '{"list_1":["\\ud800"],"list_2":["a"]}',
        " " * (MAX_REQUEST_BYTES + 1),
    ],
    ids=["empty", "syntax", "missing-fields", "non-string", "surrogate", "oversized"],
)
def test_invalid_input_makes_no_http_requests(raw: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid input must be rejected before making a request")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(cli.InputError),
    ):
        cli.execute(settings("-i", "-"), client, io.StringIO(raw), io.StringIO())


@pytest.mark.parametrize("failure", ["status", "invalid_id", "invalid_output", "json", "timeout"])
def test_failed_iteration_preserves_output_file_and_cleans_temporary_file(
    tmp_path: Path, failure: str
) -> None:
    destination = tmp_path / "result.jsonl"
    destination.write_text("previous result", encoding="utf-8")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return success(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("Timed out", request=request)
        if failure == "status":
            return httpx.Response(503)
        if failure == "invalid_id":
            return httpx.Response(200, json={"id": "../secret", "message": "Payload is ready"})
        if failure == "json":
            return httpx.Response(200, content=b"not json")
        if request.method == "POST":
            return success(request)
        return httpx.Response(200, json={"output": 42})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises((cli.ProtocolError, httpx.ReadTimeout)),
    ):
        cli.execute(
            settings("-j", INPUT, "-r", "2", "-o", str(destination)),
            client,
            io.StringIO(),
            io.StringIO(),
        )
    assert destination.read_text(encoding="utf-8") == "previous result"
    assert list(tmp_path.glob(".cache-cli-*")) == []


class LargeResponse(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        for _ in range(MAX_OUTPUT_BYTES // 1024 + 1):
            yield b" " * 1024


def test_streamed_response_limit() -> None:
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=LargeResponse())
            )
        ) as client,
        pytest.raises(cli.ProtocolError, match="4 MiB"),
    ):
        cli.execute(settings("-j", INPUT), client, io.StringIO(), io.StringIO())


def test_main_exit_codes_and_diagnostics(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["-j", "{}"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Field required" in captured.err
    assert cli.main(["-j", INPUT, "-r", "0"]) == 2
    assert capsys.readouterr().out == ""
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    monkeypatch.setattr("payload_cache.cli.httpx.Client", lambda **kwargs: client)
    assert cli.main(["-j", INPUT]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "HTTP 500" in captured.err


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main([flag])
    assert stopped.value.code == 0
    assert "--host, -H" in capsys.readouterr().out


def test_main_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    client = httpx.Client(transport=httpx.MockTransport(success))
    monkeypatch.setattr("payload_cache.cli.httpx.Client", lambda **kwargs: client)
    assert cli.main(["-j", INPUT]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"id": PAYLOAD_ID, "output": "HELLO, СВІТ"}


def test_input_file_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing.json"
    assert cli.main(["-i", str(missing)]) == 1
    assert capsys.readouterr().out == ""
    invalid = tmp_path / "invalid.json"
    invalid.write_bytes(b"\xff")
    assert cli.main(["-i", str(invalid)]) == 2
    captured = capsys.readouterr()
    assert "UTF-8" in captured.err
    assert captured.out == ""


def test_stdout_keeps_completed_iterations_when_a_later_request_fails() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return success(request) if calls <= 2 else httpx.Response(503)

    output = io.StringIO()
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(cli.ProtocolError),
    ):
        cli.execute(settings("-j", INPUT, "-r", "2"), client, io.StringIO(), output)
    assert len(output.getvalue().splitlines()) == 1


def test_failed_output_replace_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "output.jsonl"
    destination.write_text("original", encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        raise PermissionError("Output is locked")

    monkeypatch.setattr("payload_cache.cli.os.replace", fail_replace)
    with (
        httpx.Client(transport=httpx.MockTransport(success)) as client,
        pytest.raises(PermissionError),
    ):
        cli.execute(
            settings("-j", INPUT, "-o", str(destination)), client, io.StringIO(), io.StringIO()
        )
    assert destination.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".cache-cli-*")) == []


@pytest.mark.integration
@pytest.mark.anyio
async def test_cli_against_api_and_postgres(
    api_client: httpx.AsyncClient, api_transformer: CountingTransformer
) -> None:
    async def send(request: httpx.Request) -> httpx.Response:
        return await api_client.send(request)

    def handler(request: httpx.Request) -> httpx.Response:
        response = anyio.from_thread.run(send, request)
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    output = io.StringIO()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        await anyio.to_thread.run_sync(
            partial(
                cli.execute,
                settings("-j", INPUT, "-r", "3", "-H", "http://testserver"),
                client,
                io.StringIO(),
                output,
            )
        )
    results = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(results) == 3
    assert all(result == results[0] for result in results)
    assert results[0]["output"] == "HELLO, СВІТ"
    assert len(api_transformer.calls) == 2
