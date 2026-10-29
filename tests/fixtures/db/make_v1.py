"""Generate tests/fixtures/db/embr_v1.sqlite: an embr.sqlite at schema version 1 holding one
flash record with one entry, written by the v1 open_database itself.

Run once from the repository root, while open_database still creates version 1:

    uv run python tests/fixtures/db/make_v1.py

The committed file is the reference for the v1 -> v2 migration tests; after the schema
moves on this script refuses to run instead of producing a newer file.
"""

import hashlib
import sqlite3
import sys
import tempfile
from pathlib import Path

import tomli_w

from embr.core.storage.database import open_database, schema_version

OUT = Path(__file__).resolve().parent
DB_FILE = "embr_v1.sqlite"

# neutral sample values; null columns are left out
RECORD: dict[str, str | int] = {
    "id": "01JC0000000000000000000001",
    "project_id": "01JC0000000000000000000002",
    "project_name": "Sample project",
    "firmware_id": "01JC0000000000000000000003",
    "firmware_name": "Sample firmware",
    "device": "Sample board",
    "timestamp": "2026-10-01T12:00:00",
    "port": "COM3",
    "baud": 460800,
    "connection_type": "uart_bridge",
    "reset_before": "default-reset",
    "reset_after": "hard-reset",
    "chip_detected": "esp32",
    "chip_revision": "v3.0",
    "mac": "aa:bb:cc:dd:ee:ff",
    "flash_size_detected": "4MB",
    "result": "ok",
    "duration_ms": 12345,
    "esptool_version": "5.0.0",
    "command": "esptool --chip esp32 -p COM3 -b 460800 write-flash 0x10000 app.bin",
    "log_file": "2026-10-01.log",
}
ENTRY: dict[str, str | int | bool] = {
    "name": "app.bin",
    "address": 0x10000,
    "sha256": hashlib.sha256(b"embr fixture").hexdigest(),
    "size": 1024,
    "kind": "app",
    "skipped": False,
}


def _insert(connection: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(row.values()))


def main() -> int:
    path = OUT / DB_FILE
    if path.exists():
        print(f"{path} exists; delete it first to regenerate", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as backup:
        connection = open_database(path, Path(backup))
        try:
            version = schema_version(connection)
            if version != 1:
                connection.close()
                path.unlink()
                print(f"open_database creates version {version}, not 1", file=sys.stderr)
                return 1
            with connection:
                _insert(connection, "flash_records", dict(RECORD))
                entry = {**ENTRY, "skipped": int(ENTRY["skipped"])}
                _insert(
                    connection,
                    "record_entries",
                    {"record_id": RECORD["id"], "position": 0, **entry},
                )
        finally:
            connection.close()
    data = path.read_bytes()
    source = {
        "schema": 1,
        "kind": "embr-sqlite",
        "schema_version": 1,
        "made_with": "embr.core.storage.database.open_database at DB_SCHEMA_VERSION 1",
        "rebuild": "uv run python tests/fixtures/db/make_v1.py (only while the schema is 1)",
        "content": "one flash_records row and its one record_entries row, inserted with plain SQL",
        "purpose": "real v1 database for the v1 -> v2 migration tests",
        "file": {"name": DB_FILE, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
        "record": RECORD,
        "entry": ENTRY,
    }
    (OUT / "SOURCE.toml").write_text(tomli_w.dumps(source), encoding="utf-8")
    print(f"wrote {path} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
