"""core/storage: data layout, single-instance lock, ids, embr.sqlite and TOML documents."""

import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest
import tomli_w
from embr_testkit import REPO

from embr.core.storage.database import (
    DB_SCHEMA_VERSION,
    NewerSchemaError,
    open_database,
    schema_version,
)
from embr.core.storage.documents import Document, load_document, save_document
from embr.core.storage.ids import is_id, new_id, unique_name
from embr.core.storage.lock import InstanceLock
from embr.core.storage.paths import AppPaths, clear_temp, is_writable, prepare

CROCKFORD_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


def _backup_files(backup_root: Path) -> list[Path]:
    return [p for p in backup_root.rglob("*") if p.is_file()] if backup_root.exists() else []


# --- paths -------------------------------------------------------------------------------


def test_is_writable_creates_missing_folder(tmp_path: Path) -> None:
    assert is_writable(tmp_path)
    root = tmp_path / "portable"
    assert is_writable(root)
    assert root.is_dir()


def test_prepare_creates_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # prepare() redirects tempfile; restore it (and the variables it may read) afterwards
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    for var in ("TMP", "TEMP", "TMPDIR"):
        if var in os.environ:
            monkeypatch.setenv(var, os.environ[var])
        else:
            monkeypatch.delenv(var, raising=False)
    paths = AppPaths(tmp_path)
    prepare(paths)
    for folder in (paths.projects, paths.firmwares, paths.archive, paths.backup):
        assert folder.is_dir()
    assert paths.logs.is_dir()
    assert paths.temp.is_dir()
    assert Path(tempfile.gettempdir()).resolve() == paths.temp.resolve()


def test_clear_temp_keeps_lock_data_and_logs(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path)
    for folder in (paths.data, paths.logs, paths.temp / "nested"):
        folder.mkdir(parents=True)
    paths.lock_file.write_bytes(b"")
    (paths.temp / "leftover.bin").write_bytes(b"x")
    (paths.temp / "nested" / "deep.txt").write_text("x")
    (paths.data / "keep.toml").write_text("a = 1")
    (paths.logs / "keep.log").write_text("log")

    clear_temp(paths)

    assert sorted(paths.temp.iterdir()) == [paths.lock_file]
    assert (paths.data / "keep.toml").read_text() == "a = 1"
    assert (paths.logs / "keep.log").read_text() == "log"


# --- lock --------------------------------------------------------------------------------

_LOCK_PROBE = (
    "import sys; from pathlib import Path; from embr.core.storage.lock import InstanceLock; "
    "print(InstanceLock(Path(sys.argv[1])).acquire())"
)


def _other_process_acquires(path: Path) -> bool:
    """Try the lock from a separate process (locks are per process); a hang fails the test."""
    proc = subprocess.run(
        [sys.executable, "-c", _LOCK_PROBE, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO,
        check=True,
    )
    return proc.stdout.strip() == "True"


def test_instance_lock_is_exclusive_until_released(tmp_path: Path) -> None:
    path = tmp_path / "embr.lock"
    lock = InstanceLock(path)
    assert lock.acquire()
    try:
        assert lock.held
        assert not _other_process_acquires(path)
        # a second copy of Embr in another folder uses another file
        assert _other_process_acquires(tmp_path / "other.lock")
    finally:
        lock.release()
    assert not lock.held
    lock.release()  # safe to call twice
    assert _other_process_acquires(path)


# --- ids ---------------------------------------------------------------------------------


def test_new_id_format_and_order() -> None:
    ids = [new_id() for _ in range(200)]
    assert all(CROCKFORD_ULID.fullmatch(i) for i in ids)
    assert all(is_id(i) for i in ids)
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert not is_id("")
    assert not is_id("not-a-ulid")
    assert not is_id("U" * 26)


def test_unique_name_suffixes() -> None:
    assert unique_name("App", []) == "App"
    assert unique_name("App", ["App"]) == "App (2)"
    assert unique_name("App", ["App", "App (2)", "App (4)"]) == "App (3)"
    assert unique_name("app", ["App"]) == "app"


# --- database ----------------------------------------------------------------------------


def _make_db(path: Path, version: int) -> bytes:
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {version}")
    connection.commit()
    connection.close()
    return path.read_bytes()


def test_open_database_creates_current_schema(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "embr.sqlite", tmp_path / "backup")
    try:
        assert schema_version(connection) == DB_SCHEMA_VERSION
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {str(row[0]) for row in rows}
    finally:
        connection.close()
    assert {"flash_records", "record_entries", "archive_files", "archive_names"} <= tables
    assert _backup_files(tmp_path / "backup") == []


def test_open_database_migrates_old_file_with_backup(tmp_path: Path) -> None:
    path = tmp_path / "embr.sqlite"
    original = _make_db(path, 0)
    connection = open_database(path, tmp_path / "backup")
    try:
        assert schema_version(connection) == DB_SCHEMA_VERSION
    finally:
        connection.close()
    backups = _backup_files(tmp_path / "backup")
    assert [b.read_bytes() for b in backups] == [original]


def test_open_database_refuses_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "embr.sqlite"
    original = _make_db(path, DB_SCHEMA_VERSION + 1)
    with pytest.raises(NewerSchemaError):
        open_database(path, tmp_path / "backup")
    assert path.read_bytes() == original


# --- documents ---------------------------------------------------------------------------

SAMPLE: Document = {"name": "Demo", "tags": ["a", "b"], "nested": {"value": 3, "on": True}}


def _without_version(data: Document) -> Document:
    return {k: v for k, v in data.items() if k != "schema_version"}


def test_document_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "doc.toml"
    save_document(path, SAMPLE, current=2)
    assert tomllib.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2
    assert list(tmp_path.iterdir()) == [path]  # no temporary file left behind
    loaded = load_document(path, current=2, migrations={}, backup_root=tmp_path / "backup")
    assert _without_version(loaded.data) == SAMPLE
    assert not loaded.read_only
    assert loaded.migrated_from is None


def test_document_migrates_step_by_step_with_backup(tmp_path: Path) -> None:
    path = tmp_path / "doc.toml"
    path.write_text(tomli_w.dumps({"schema_version": 1, "old": "x"}), encoding="utf-8")
    original = path.read_bytes()

    def v1_to_v2(doc: Document) -> Document:
        return {k: v for k, v in doc.items() if k != "old"} | {"mid": doc["old"]}

    def v2_to_v3(doc: Document) -> Document:
        return doc | {"added": True}

    backup = tmp_path / "backup"
    loaded = load_document(
        path, current=3, migrations={1: v1_to_v2, 2: v2_to_v3}, backup_root=backup
    )
    assert _without_version(loaded.data) == {"mid": "x", "added": True}
    assert loaded.migrated_from == 1
    assert not loaded.read_only
    assert [b.read_bytes() for b in _backup_files(backup)] == [original]
    on_disk = tomllib.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 3
    assert on_disk["mid"] == "x"


def test_newer_document_is_read_only_and_untouched(tmp_path: Path) -> None:
    path = tmp_path / "doc.toml"
    path.write_text(tomli_w.dumps({"schema_version": 9, "future": 1}), encoding="utf-8")
    original = path.read_bytes()
    loaded = load_document(path, current=1, migrations={}, backup_root=tmp_path / "backup")
    assert loaded.read_only
    assert loaded.data["future"] == 1
    assert path.read_bytes() == original
