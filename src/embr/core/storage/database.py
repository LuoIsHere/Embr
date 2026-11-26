"""embr.sqlite: flash records, the archive index and build versions. Version in PRAGMA
user_version."""

import shutil
import sqlite3
from pathlib import Path

from embr.core.storage.documents import backup_folder

DB_SCHEMA_VERSION = 3
"""Version 1 tables: flash_records, record_entries, archive_files,
archive_names. Version 2: flash_records.operation (flash / erase / read_flash). Version 3:
versions, version_entries, version_counters; flash_records.origin (gui / cli) and
flash_records.version_id."""


class NewerSchemaError(Exception):
    """Data written by a newer Embr; it is opened read-only or not at all, never changed."""


class DamagedDatabaseError(Exception):
    """The database file is not a readable SQLite database, or fails its integrity check. The file
    is left untouched."""


def open_database(path: Path, backup_root: Path) -> sqlite3.Connection:
    """Open (creating if missing) and migrate step by step to DB_SCHEMA_VERSION.
    Before migrating an existing database, copy it to backup_root/<timestamp>/.
    Raises NewerSchemaError if user_version > DB_SCHEMA_VERSION, DamagedDatabaseError if
    an existing file is damaged (the file is left untouched in both cases)."""
    existing = path.is_file() and path.stat().st_size > 0
    version = 0
    if existing:
        # as_uri() escapes "#", "%" and spaces; a raw "file:" string would cut the path
        probe = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            version = schema_version(probe)
            result = probe.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise DamagedDatabaseError(f"{path}: {exc}") from exc
        finally:
            probe.close()
        if result is None or result[0] != "ok":
            raise DamagedDatabaseError(f"{path}: {result[0] if result else 'check failed'}")
    if version > DB_SCHEMA_VERSION:
        raise NewerSchemaError(
            f"{path} has schema version {version}, newer than {DB_SCHEMA_VERSION}"
        )
    if existing and version < DB_SCHEMA_VERSION:
        shutil.copy2(path, backup_folder(backup_root) / path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The GUI and the command line may use the same file at once: a writer waits up to 5 s
    # for the other process's transaction instead of failing at once. The rollback journal
    # is kept (no WAL), because WAL is unsafe when the folder is on a network share.
    connection = sqlite3.connect(path, timeout=5)
    connection.execute("PRAGMA foreign_keys = ON")
    while version < DB_SCHEMA_VERSION:
        _migrate(connection, version)
        version += 1
    return connection


def _migrate(connection: sqlite3.Connection, version: int) -> None:
    """One migration step and its user_version in one transaction: a step stopped half way
    leaves the database at `version`, never with new tables and the old version."""
    try:
        connection.executescript(
            f"BEGIN; {_MIGRATIONS[version]} PRAGMA user_version = {version + 1}; COMMIT;"
        )
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


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
    # build versions: immutable copies of a tracked firmware's flashable content; their
    # files live in the archive (archive_files only, no archive_names rows). seq numbers
    # come from version_counters, never from max(seq) + 1, so a deleted number is not
    # given out again. Records made before this step all came from the GUI.
    2: """
    CREATE TABLE versions (
        id TEXT PRIMARY KEY,
        firmware_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        origin TEXT NOT NULL,
        digest TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        pinned INTEGER NOT NULL DEFAULT 0,
        app_version TEXT,
        git_commit TEXT,
        git_describe TEXT,
        git_dirty INTEGER,
        chip TEXT,
        params TEXT NOT NULL,
        build_path TEXT NOT NULL DEFAULT '',
        UNIQUE (firmware_id, seq)
    );
    CREATE TABLE version_entries (
        version_id TEXT NOT NULL REFERENCES versions (id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        address INTEGER,
        sha256 TEXT NOT NULL,
        size INTEGER NOT NULL,
        enabled INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (version_id, position)
    );
    CREATE TABLE version_counters (
        firmware_id TEXT PRIMARY KEY,
        last_seq INTEGER NOT NULL
    );
    ALTER TABLE flash_records ADD COLUMN origin TEXT NOT NULL DEFAULT 'gui';
    ALTER TABLE flash_records ADD COLUMN version_id TEXT;
    CREATE INDEX flash_records_version ON flash_records (version_id);
    """,
}
