# Service contract

These choices resolve ambiguities in the assessment. They are implementation
assumptions, not additional requirements from the task author.

## API

`POST /payload` accepts `list_1` and `list_2`, both arrays of strings of equal length.
Unknown fields and non-string elements are rejected. Whitespace, case, duplicates,
empty strings, and valid Unicode are preserved. Two empty lists are valid.

Each string is transformed with Python's `str.upper()`. The output alternates
between the lists and joins the transformed strings with `, `.

```json
{"list_1":["first string","second string"],"list_2":["other string","another string"]}
```

The create-or-reuse operation returns `200`:

```json
{"id":"<64 lowercase hexadecimal characters>","message":"Payload is ready"}
```

`GET /payload/{id}` reads a stored JSON file and returns:

```json
{"output":"FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING"}
```

GET performs no transformations. Malformed IDs fail validation; absent files return
`404`. Storage permission errors and corrupt files are internal errors, not `404`.

## Limits

- HTTP request body: 1 MiB, enforced before JSON parsing, including streamed bodies.
- Each list: at most 1,000 elements.
- Each input string: at most 10,000 Unicode characters.
- Each transformed result and the serialized payload: at most 4 MiB in UTF-8.

Invalid Unicode scalar sequences are rejected. NUL characters are accepted;
cache source and result values use UTF-8 bytes in PostgreSQL `BYTEA` columns.

## Identity and caching

The transformer is deterministic within its version. Cache entries use the exact
input string and transformer version. Unique strings are looked up in batches;
only missing results require transformation.

The request index hashes a canonical representation of the ordered lists, the
transformer version, and the output format version. JSON object key ordering and
insignificant JSON whitespace do not affect request identity.

The payload ID is SHA-256 of the stored JSON bytes. Serialization uses UTF-8,
`ensure_ascii=False`, compact separators, one `output` field, and no final newline.
Different inputs producing identical output share one payload ID, including
ambiguous comma-separated outputs. Hash collision resistance is an assumption.

Changing transformation behavior requires a new transformer version; changing
output formatting requires a new format version. Old files remain immutable.

## Concurrency and failures

For a missing cache entry, acquire a PostgreSQL transaction-level advisory lock,
then recheck the entry in a separate statement under `READ COMMITTED`. Hold the
lock through transformation and commit. Each operation holds at most one key
lock, and each thread owns its session. Lock keys are stable across processes.

Successful transformations are committed individually, even if a later value
fails. No final payload is published until all transformations succeed.

The coordinator prevents duplicate calls after successful persistence across
processes sharing the database. A crash or lost database connection after an
external call but before commit can cause a repeated call. Exactly-once external
execution is not guaranteed. Locks, connections, and external calls have bounded
waits; there is no automatic retry of a failed transformation.

## Files

Payloads live at `<data_dir>/<id>.json`. Publish a complete temporary file from
the same filesystem before committing the request index. Use atomic replacement
and synchronize file data; the Linux deployment also synchronizes the directory.
Concurrent publishers of identical content produce the same complete file.

A crash between publication and indexing leaves a valid reusable file. A repeated
POST repairs a missing file from its input and cached transformations. GET cannot
recover lost data from an ID alone. All API workers must share the payload directory.
Backups need both PostgreSQL and the payload volume. Cache eviction and payload
garbage collection are outside this task.

## CLI

`cache-cli` uses Pydantic Settings for argument parsing and validation:

```text
cache-cli [-H URL|--host URL] [-r N|--repeat N]
          (-i FILE|-i -|--input FILE|--input -|-j JSON|--json JSON)
          [-o FILE|-o -|--output FILE|--output -] [-h|--help]
```

`-h` is reserved for help because the task assigns it to both host and help.
Host defaults to `http://localhost:8000`; repeat defaults to `1` and must be
positive. Output defaults to stdout. Input is required and read exactly once.
Input files accept UTF-8 with or without BOM. Each iteration performs POST then
GET; output is one JSON line with `id` and `output` per iteration. Diagnostics use
stderr. Host URLs preserve an optional base path and reject credentials, queries,
and fragments. CLI options do not read environment variables or dotenv files.

Output files are replaced only after every iteration succeeds. Input and output
must not be the same file. Stdout can contain successful earlier iterations when
a later one fails. Exit codes: `0` success, `1` runtime/protocol/I/O error, `2`
invalid arguments or input. HTTP responses are validated before use.
