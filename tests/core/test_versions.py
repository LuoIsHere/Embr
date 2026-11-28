"""core/versions: build versions in embr.sqlite."""

import dataclasses
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from embr.core.history import FlashRecord, History, Operation, RecordResult
from embr.core.storage.database import open_database
from embr.core.storage.ids import new_id
from embr.core.versions import (
    Origin,
    Version,
    VersionDraft,
    VersionEntry,
    VersionParams,
    VersionStore,
    base_name,
    default_name,
    is_renamed,
)

FW = "01JC00000000000000000000F1"
OTHER_FW = "01JC00000000000000000000F2"
BOOT = VersionEntry("bootloader.bin", "bootloader", 0x0, "b" * 64, 20000, True)
APP = VersionEntry("app.bin", "app", 0x10000, "a" * 64, 180000, True, note="main")


def _draft(**changes: Any) -> VersionDraft:
    base = VersionDraft(
        firmware_id=FW,
        created_at="2026-11-26T10:05:09",
        origin=Origin.MANUAL,
        digest="0123456789abcdef",
        params=VersionParams(flash_mode="dio", flash_size="4MB", idf_version="v5.3"),
        entries=(BOOT, APP),
        chip="esp32s3",
        app_version="1.0",
        git_commit="c" * 40,
        git_describe="v1.0-3-gccccccc",
        git_dirty=False,
        build_path="build",
    )
    return dataclasses.replace(base, **changes)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = open_database(tmp_path / "embr.sqlite", tmp_path / "backup")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def store(connection: sqlite3.Connection) -> VersionStore:
    return VersionStore(connection)


def _insert(store: VersionStore, connection: sqlite3.Connection, **changes: Any) -> Version:
    with connection:
        return store.insert(_draft(**changes))


def test_sqlite_supports_returning() -> None:
    assert sqlite3.sqlite_version_info >= (3, 35)


def test_insert_and_read_back(store: VersionStore, connection: sqlite3.Connection) -> None:
    created = _insert(store, connection, note="demo", pinned=True)
    assert created.seq == 1
    assert created.name == "v1.0-3-gccccccc"
    assert store.get(created.id) == created
    assert created.entries == (BOOT, APP)
    assert created.origin is Origin.MANUAL and created.pinned and created.note == "demo"
    assert store.get(new_id()) is None


def test_versions_are_listed_newest_first_per_firmware(
    store: VersionStore, connection: sqlite3.Connection
) -> None:
    first = _insert(store, connection)
    other = _insert(store, connection, firmware_id=OTHER_FW)
    second = _insert(store, connection, entries=(APP,))
    assert [v.id for v in store.for_firmware(FW)] == [second.id, first.id]
    assert [v.entries for v in store.for_firmware(FW)] == [(APP,), (BOOT, APP)]
    assert store.latest(FW) == second
    assert [v.seq for v in store.for_firmware(OTHER_FW)] == [other.seq] == [1]
    assert store.latest("none") is None


def test_numbers_are_never_given_out_again(
    store: VersionStore, connection: sqlite3.Connection
) -> None:
    one = _insert(store, connection)
    two = _insert(store, connection)
    assert (one.seq, two.seq) == (1, 2)
    with connection:
        store.delete([two.id])
    assert _insert(store, connection).seq == 3
    with connection:
        store.delete([v.id for v in store.for_firmware(FW)])
    assert _insert(store, connection).seq == 4


def test_deleting_a_firmware_drops_its_versions_and_counter(
    store: VersionStore, connection: sqlite3.Connection
) -> None:
    _insert(store, connection)
    _insert(store, connection, entries=(APP,))
    kept = _insert(store, connection, firmware_id=OTHER_FW)
    with connection:
        shas = store.delete_firmware(FW)
    assert shas == {BOOT.sha256, APP.sha256}
    assert store.for_firmware(FW) == []
    assert connection.execute("SELECT COUNT(*) FROM version_entries").fetchone()[0] == 2
    assert store.get(kept.id) == kept
    assert _insert(store, connection).seq == 1  # a new firmware with that id starts again


def test_delete_returns_the_files_and_cascades(
    store: VersionStore, connection: sqlite3.Connection
) -> None:
    version = _insert(store, connection)
    with connection:
        assert store.delete([version.id, new_id()]) == {BOOT.sha256, APP.sha256}
    assert connection.execute("SELECT COUNT(*) FROM version_entries").fetchone()[0] == 0
    with connection:
        assert store.delete([]) == set()


def test_insert_is_undone_with_the_callers_transaction(
    store: VersionStore, connection: sqlite3.Connection
) -> None:
    with pytest.raises(RuntimeError), connection:
        store.insert(_draft())
        raise RuntimeError("archive rows failed")
    assert store.for_firmware(FW) == []
    assert _insert(store, connection).seq == 1  # the counter went back too


def test_two_connections_get_different_numbers(tmp_path: Path) -> None:
    path = tmp_path / "embr.sqlite"
    first = open_database(path, tmp_path / "backup")
    second = open_database(path, tmp_path / "backup")
    try:
        seqs: list[int] = []
        for db in (first, second, first, second):
            with db:
                seqs.append(VersionStore(db).insert(_draft()).seq)
    finally:
        first.close()
        second.close()
    assert seqs == [1, 2, 3, 4]


# --- names -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("describe", "app_version", "expected"),
    [
        ("v1.0-dirty", "1.0", "v1.0-dirty"),
        (None, "1.0", "1.0"),
        ("", "", "2026-11-26 10:05"),
        (None, None, "2026-11-26 10:05"),
    ],
)
def test_base_name(describe: str | None, app_version: str | None, expected: str) -> None:
    assert base_name(describe, app_version, "2026-11-26T10:05:09") == expected


def test_default_name_adds_the_time_only_when_taken() -> None:
    assert default_name("abc1234-dirty", "2026-11-26T10:05:09", ["other"]) == "abc1234-dirty"
    assert (
        default_name("abc1234-dirty", "2026-11-26T10:05:09", ["abc1234-dirty"])
        == "abc1234-dirty (2026-11-26 10:05)"
    )


def test_equal_default_names_get_the_creation_time(
    store: VersionStore, connection: sqlite3.Connection
) -> None:
    first = _insert(store, connection, git_describe="abc1234-dirty")
    second = _insert(
        store, connection, git_describe="abc1234-dirty", created_at="2026-11-26T11:30:00"
    )
    other_fw = _insert(store, connection, git_describe="abc1234-dirty", firmware_id=OTHER_FW)
    assert first.name == "abc1234-dirty"
    assert second.name == "abc1234-dirty (2026-11-26 11:30)"
    assert other_fw.name == "abc1234-dirty"  # names are compared within one firmware
    assert not is_renamed(first) and not is_renamed(second)


def test_an_unused_app_version_is_kept_but_not_named_after(
    store: VersionStore, connection: sqlite3.Connection
) -> None:
    arduino = _insert(
        store,
        connection,
        git_describe=None,
        app_version="esp-idf: v4.4.7 38eeba213a",
        params=VersionParams(use_app_version=False),
    )
    assert arduino.name == "2026-11-26 10:05"
    assert arduino.app_version == "esp-idf: v4.4.7 38eeba213a"
    assert arduino.shown_app_version is None
    assert store.get(arduino.id) == arduino
    assert not is_renamed(arduino)


def test_rename_and_restore_the_default(
    store: VersionStore, connection: sqlite3.Connection
) -> None:
    first = _insert(store, connection, git_describe="abc1234-dirty")
    second = _insert(
        store, connection, git_describe="abc1234-dirty", created_at="2026-11-26T11:30:00"
    )
    renamed = store.rename(second.id, "  Customer demo  ")
    stored = store.get(second.id)
    assert stored is not None
    assert renamed.name == "Customer demo" == stored.name
    assert is_renamed(renamed)
    # restoring checks against the other versions, so the time is added again
    assert store.rename(second.id, " ").name == "abc1234-dirty (2026-11-26 11:30)"
    # the first version gets its plain name back once nobody else holds it
    store.rename(second.id, "Customer demo")
    store.rename(first.id, "Something")
    assert store.rename(first.id, "").name == "abc1234-dirty"
    with pytest.raises(KeyError):
        store.rename(new_id(), "x")


def test_note_and_pin(store: VersionStore, connection: sqlite3.Connection) -> None:
    version = _insert(store, connection)
    store.set_note(version.id, "for the customer")
    store.set_pinned(version.id, True)
    stored = store.get(version.id)
    assert stored is not None
    assert (stored.note, stored.pinned) == ("for the customer", True)
    assert not is_renamed(stored)  # a note is not a new name


# --- params ------------------------------------------------------------------------------


def test_params_read_with_defaults_for_missing_or_odd_keys(
    store: VersionStore, connection: sqlite3.Connection
) -> None:
    version = _insert(store, connection)
    for text, expected in [
        ('{"flash_mode": "qio", "future": 1}', VersionParams(flash_mode="qio")),
        ('{"stub": "yes", "idf_version": 5}', VersionParams()),
        ("not json", VersionParams()),
        ("[1, 2]", VersionParams()),
    ]:
        with connection:
            connection.execute("UPDATE versions SET params = ? WHERE id = ?", (text, version.id))
        stored = store.get(version.id)
        assert stored is not None
        assert stored.params == expected, text
    assert VersionParams().use_app_version  # missing reads as used


# --- references and flashes --------------------------------------------------------------


def test_referenced_files(store: VersionStore, connection: sqlite3.Connection) -> None:
    assert store.referenced() == set()
    _insert(store, connection, entries=(APP,))
    _insert(store, connection, firmware_id=OTHER_FW)
    assert store.referenced() == {APP.sha256, BOOT.sha256}
    assert store.referenced(besides=OTHER_FW) == {APP.sha256}
    assert store.referenced(besides=FW) == {APP.sha256, BOOT.sha256}


def _flash(version_id: str | None, mac: str | None, **changes: Any) -> FlashRecord:
    record = FlashRecord(
        id=new_id(),
        operation=Operation.FLASH,
        timestamp="2026-11-26T12:00:00",
        result=RecordResult.OK,
        firmware_id=FW,
        mac=mac,
        version_id=version_id,
    )
    return dataclasses.replace(record, **changes)


def test_flash_counts_list_successful_flashes(
    store: VersionStore, connection: sqlite3.Connection
) -> None:
    flashed = _insert(store, connection)
    never = _insert(store, connection)
    foreign = _insert(store, connection, firmware_id=OTHER_FW)
    history = History(connection)
    history.add(_flash(flashed.id, "aa:bb:cc:dd:ee:ff", timestamp="2026-11-26T13:00:00"))
    history.add(_flash(flashed.id, "11:22:33:44:55:66"))
    history.add(_flash(flashed.id, "11:22:33:44:55:77", result=RecordResult.FAILED))
    history.add(_flash(flashed.id, None, result=RecordResult.REFUSED))
    history.add(_flash(foreign.id, "aa:bb:cc:dd:ee:ff"))
    history.add(_flash(None, "aa:bb:cc:dd:ee:ff"))

    counts = store.flash_counts(FW)
    assert counts == {
        flashed.id: [
            ("11:22:33:44:55:66", "2026-11-26T12:00:00"),
            ("aa:bb:cc:dd:ee:ff", "2026-11-26T13:00:00"),
        ]
    }
    assert never.id not in counts


def test_store_version_counts_its_changes(
    store: VersionStore, connection: sqlite3.Connection
) -> None:
    start = store.version
    version = _insert(store, connection)
    store.set_note(version.id, "x")
    store.set_pinned(version.id, True)
    store.rename(version.id, "y")
    assert store.version == start + 4
