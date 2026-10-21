import json
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    example = Path(__file__).resolve().parents[1] / "examples" / "payload.json"
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / "results.jsonl"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "payload_cache.cli",
                "--host",
                host,
                "--input",
                str(example),
                "--repeat",
                "3",
                "--output",
                str(output),
            ],
            check=True,
        )
        results = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(results) == 3 and all(result == results[0] for result in results)
    assert results[0]["output"] == (
        "FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING, THIRD STRING, LAST STRING"
    )
    response = httpx.get(f"{host.rstrip('/')}/health", timeout=10)
    response.raise_for_status()
    assert response.json() == {"status": "ok"}
    print(f"API and CLI smoke check passed; payload ID: {results[0]['id']}")


if __name__ == "__main__":
    main()
