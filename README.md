# Payload Cache Service

A FastAPI service for generating JSON payload files with persistent transformation caching.
PostgreSQL stores transformed strings and an index of previously processed requests.

The current checkpoint includes the transformation cache, database schema and migration,
input contract, and database health endpoint. Payload endpoints and the CLI follow in
the next checkpoints.

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
