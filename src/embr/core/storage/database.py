"""embr.sqlite: flash records and the archive index. Version in PRAGMA user_version."""

import sqlite3
from pathlib import Path

DB_SCHEMA_VERSION = 1
"""Version 1 tables: flash_records, record_entries (design doc §3.3), archive_files,
archive_names (§7.6)."""


class NewerSchemaError(Exception):
    """Data written by a newer Embr; it is opened read-only or not at all, never changed."""


def open_database(path: Path, backup_root: Path) -> sqlite3.Connection:
    """Open (creating if missing) and migrate step by step to DB_SCHEMA_VERSION.
    Before migrating an existing database, copy it to backup_root/<timestamp>/.
    Raises NewerSchemaError if user_version > DB_SCHEMA_VERSION (file left untouched)."""
    raise NotImplementedError


def schema_version(connection: sqlite3.Connection) -> int:
    """Current PRAGMA user_version."""
    raise NotImplementedError
