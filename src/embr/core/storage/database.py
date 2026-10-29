"""embr.sqlite: flash records and the archive index. Version in PRAGMA user_version."""

import shutil
import sqlite3
from pathlib import Path

from embr.core.storage.documents import backup_folder

DB_SCHEMA_VERSION = 2
"""Version 1 tables: flash_records, record_entries (design doc §3.3), archive_files,
archive_names (§7.6). Version 2: flash_records.operation (flash / erase / read_flash)."""


class NewerSchemaError(Exception):
    """Data written by a newer Embr; it is opened read-only or not at all, never changed."""


def open_database(path: Path, backup_root: Path) -> sqlite3.Connection:
    """Open (creating if missing) and migrate step by step to DB_SCHEMA_VERSION.
    Before migrating an existing database, copy it to backup_root/<timestamp>/.
    Raises NewerSchemaError if user_version > DB_SCHEMA_VERSION (file left untouched)."""
    existing = path.is_file() and path.stat().st_size > 0
    version = 0
    if existing:
        probe = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            version = schema_version(probe)
        finally:
            probe.close()
    if version > DB_SCHEMA_VERSION:
        raise NewerSchemaError(
            f"{path} has schema version {version}, newer than {DB_SCHEMA_VERSION}"
        )
    if existing and version < DB_SCHEMA_VERSION:
        shutil.copy2(path, backup_folder(backup_root) / path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    while version < DB_SCHEMA_VERSION:
        connection.executescript(_MIGRATIONS[version])
        version += 1
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    return connection


def schema_version(connection: sqlite3.Connection) -> int:
    """Current PRAGMA user_version."""
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


_MIGRATIONS: dict[int, str] = {
    0: """
    CREATE TABLE flash_records (
        id TEXT PRIMARY KEY,
        project_id TEXT, project_name TEXT,
        firmware_id TEXT, firmware_name TEXT, device TEXT,
        timestamp TEXT NOT NULL,
        port TEXT, baud INTEGER, connection_type TEXT,
        reset_before TEXT, reset_after TEXT,
        chip_detected TEXT, chip_revision TEXT, mac TEXT, flash_size_detected TEXT,
        result TEXT NOT NULL, refuse_reason TEXT,
        duration_ms INTEGER, esptool_version TEXT, command TEXT, log_file TEXT
    );
    CREATE INDEX flash_records_mac ON flash_records (mac);
    CREATE INDEX flash_records_firmware ON flash_records (firmware_id);
    CREATE INDEX flash_records_project ON flash_records (project_id);
    CREATE TABLE record_entries (
        record_id TEXT NOT NULL REFERENCES flash_records (id),
        position INTEGER NOT NULL,
        name TEXT, address INTEGER, sha256 TEXT, size INTEGER, kind TEXT,
        skipped INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (record_id, position)
    );
    CREATE TABLE archive_files (
        sha256 TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        first_archived TEXT NOT NULL
    );
    CREATE TABLE archive_names (
        id TEXT PRIMARY KEY,
        sha256 TEXT NOT NULL REFERENCES archive_files (sha256),
        original_name TEXT NOT NULL, original_path TEXT, note TEXT,
        archived_at TEXT NOT NULL, source_firmware TEXT
    );
    """,
    # records made before erase and read_flash were recorded are all flash records
    1: """
    ALTER TABLE flash_records ADD COLUMN operation TEXT NOT NULL DEFAULT 'flash';
    CREATE INDEX flash_records_operation ON flash_records (operation);
    """,
}
