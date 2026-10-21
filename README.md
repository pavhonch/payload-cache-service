# Payload Cache Service

A FastAPI service for generating JSON payload files with persistent transformation caching.
PostgreSQL stores transformed strings and an index of previously processed requests.

The transformer uppercases strings. The service interleaves the two transformed
lists, joins them with `, `, and stores the result as an immutable JSON file.

## Run with Docker

```sh
docker compose up --build -d --wait
```

This starts PostgreSQL, runs Alembic migrations in a separate container, then starts the API at
`http://localhost:8000`. Interactive API documentation is at `/docs`.
The application runs as a non-root user. Named volumes preserve PostgreSQL data
and payload files across container restarts and `docker compose down`.

```sh
docker compose logs app migrate
docker compose down
```

The Compose credentials and localhost-only ports are for local evaluation. Set
`CACHE_HTTP_PORT` or `CACHE_POSTGRES_PORT` when the default host ports (8000 and
5433) are occupied. Compose uses its internal PostgreSQL address regardless of
the database URL used for local Python development.

The image contains the CLI too. This example needs only Docker:

```sh
docker compose exec app cache-cli -H http://127.0.0.1:8000 -j '{"list_1":["hello"],"list_2":["world"]}' -r 3
```

For an existing deployment, run migrations as a single release step before
starting or scaling API workers. They are deliberately separate from the API
startup. A custom payload volume must be writable by UID/GID `10001:10001` and
shared by all workers.

## Development

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), and Docker with Compose.

```sh
uv sync --locked
docker compose up -d --wait postgres
uv run alembic upgrade head
uv run uvicorn payload_cache.app:create_app --factory --reload
```

The API is available at `http://localhost:8000`, with OpenAPI documentation at `/docs`.
`GET /health` checks PostgreSQL connectivity and returns `503` if it is unavailable.

Configuration uses environment variables prefixed with `CACHE_`; see `.env.example`.
The application does not load `.env` files automatically. The example database
credentials are for local development.

## API

Create or reuse a payload:

```sh
curl -H "Content-Type: application/json" --data-binary "@examples/payload.json" http://localhost:8000/payload
```

Use `curl.exe` in Windows PowerShell. POST returns `200` with an `id` and the message
`Payload is ready`. Read the result using that ID:

```sh
curl "http://localhost:8000/payload/<id>"
```

```json
{"output":"FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING, THIRD STRING, LAST STRING"}
```

A repeated request uses its stored request index. New requests reuse cached
transformations for every matching string; missing values are computed under
PostgreSQL locks shared by all workers. Identical output shares one file and ID,
even when produced by different inputs. GET only reads the file and does not
require a database connection.

Files are published atomically using hard links, supported by NTFS and typical
Linux filesystems such as ext4. The payload directory must support hard links.
Every worker must use the same directory. The default location is `data/payloads`.

Invalid input returns `422`, oversized input or output `413`, missing payloads
`404`, and known dependency failures `503`. Unexpected failures return `500`.

## CLI

The CLI uses Pydantic Settings to parse and validate arguments. Each iteration
creates a payload, reads it back, and writes one JSON line containing `id` and
`output`. Use `-H` for the server; `-h` is reserved for help because the assignment
uses that short option for both.

```sh
uv run cache-cli --help
uv run cache-cli -H http://localhost:8000 -i examples/payload.json -r 3
uv run cache-cli -i examples/payload.json -o results.jsonl
```

Inline JSON and stdin in a POSIX shell:

```sh
uv run cache-cli -j '{"list_1":["hello"],"list_2":["world"]}'
cat examples/payload.json | uv run cache-cli -i -
```

PowerShell:

```powershell
Get-Content -Raw -Encoding utf8 examples/payload.json | uv run cache-cli -i -
```

Choose exactly one of `--input` and `--json`. Files accept UTF-8 with or without a
BOM. Output defaults to stdout; diagnostics go to stderr. An output file is
replaced only after all iterations succeed. It must differ from the input file.
When writing to stdout, completed iterations remain visible if a later one fails.

The host may include a base path, such as `https://example.com/cache`. Credentials,
queries, and fragments are rejected. Requests have a 10-second network inactivity
timeout and redirects are treated as errors. CLI options do not read environment
variables or dotenv files. Exit codes are `0` for success, `1` for runtime errors,
and `2` for invalid arguments or input.

## Checks

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not integration"
```

Integration tests own and may recreate the schema in a separate database whose name
must end with `_test`. Do not point them at a database containing other work.
Create it once:

```sh
docker compose exec -T postgres createdb -U cache cache_test
```

Then run all tests in PowerShell:

```powershell
$env:CACHE_TEST_DATABASE_URL = "postgresql+psycopg://cache:cache@127.0.0.1:5433/cache_test"
uv run pytest
```

Or in a POSIX shell:

```sh
CACHE_TEST_DATABASE_URL=postgresql+psycopg://cache:cache@127.0.0.1:5433/cache_test uv run pytest
```

The behavior and storage guarantees are described in [the service contract](docs/contract.md).

Cache integration tests verify call counts, reuse across engine instances and processes,
independent keys, lock timeouts, and a lost database connection before commit. Successful
transformations remain cached when a later transformation fails.

API and storage tests cover duplicate payloads, concurrent publication, interrupted
writes, missing files, corrupt files, streamed request limits, and Unicode handling.
CLI tests cover argument validation, stdin and files, repeated POST/GET requests,
response validation, atomic output, failures, and a complete run against the API
with PostgreSQL.

GitHub Actions runs formatting, lint, strict type checking, all tests against
PostgreSQL, and a Docker build with a deployed API/CLI smoke check. To run the
same smoke check locally against a running service:

```sh
uv run python scripts/smoke.py http://127.0.0.1:8000
```

## Scope and tradeoffs

PostgreSQL is required; SQLite is not supported. Transaction advisory locks
coordinate cache misses across workers, at the cost of holding a connection while
the transformer runs. A future external transformer must implement its own I/O
timeout and use a new version whenever its behavior changes.

A crash after transformation but before commit can cause the external call to
be repeated. Files and database rows cannot share one transaction: publish the
file first, then index it, so an interrupted request can leave a reusable file.
Repeated POST requests can reconstruct missing files from cached transformations.
GET cannot recover a missing file from its hash alone.

Authentication, cache eviction, and payload cleanup are outside this assessment.
Backups must include both the database and payload volume.
