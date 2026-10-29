"""core/history: flash records in embr.sqlite (design doc §3.3)."""

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from embr.core.flasher.protocol import ErrorEvent, ResultEvent, ResultStatus
from embr.core.history import (
    FlashRecord,
    History,
    Operation,
    RecordEntry,
    RecordResult,
    normalize_mac,
    result_from_event,
)
from embr.core.storage.database import open_database
from embr.core.storage.ids import new_id

MAC = "aa:bb:cc:dd:ee:ff"
OTHER_MAC = "11:22:33:44:55:66"
PROJECT = "01JC00000000000000000000P1"
FIRMWARE = "01JC00000000000000000000F1"

BOOTLOADER = RecordEntry("bootloader.bin", 0x1000, "1" * 64, 26000, "bootloader")
APP = RecordEntry("app.bin", 0x10000, "2" * 64, 180000, "app", skipped=True)


def _record(**changes: Any) -> FlashRecord:
    """A complete flash record with a fresh id; `changes` override fields."""
    base = FlashRecord(
        id=new_id(),
        operation=Operation.FLASH,
        timestamp="2026-10-29T10:00:00",
        result=RecordResult.OK,
        project_id=PROJECT,
        project_name="Weather station",
        firmware_id=FIRMWARE,
        firmware_name="Release 1.2",
        device="Outdoor unit",
        port="COM3",
        baud=921600,
        connection_type="uart_bridge",
        reset_before="default-reset",
        reset_after="hard-reset",
        chip_detected="esp32",
        chip_revision="v3.0",
        mac=MAC,
        flash_size_detected="4MB",
        entries=(BOOTLOADER, APP),
        duration_ms=8123,
        esptool_version="5.0.0",
        command="esptool --chip esp32 -p COM3 -b 921600 write-flash 0x1000 bootloader.bin",
        log_file="2026-10-29.log",
    )
    return dataclasses.replace(base, **changes)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "embr.sqlite"


@pytest.fixture
def history(db_path: Path, tmp_path: Path) -> Iterator[History]:
    connection = open_database(db_path, tmp_path / "backup")
    try:
        yield History(connection)
    finally:
        connection.close()


def test_record_snapshot_reads_back_after_reopen(db_path: Path, tmp_path: Path) -> None:
    # the record names a project and firmware that exist nowhere: it is a self-contained
    # snapshot, so it stays complete whatever happens to them later
    record = _record(reset_before="no-reset", reset_after="watchdog-reset")
    connection = open_database(db_path, tmp_path / "backup")
    try:
        History(connection).add(record)
    finally:
        connection.close()
    connection = open_database(db_path, tmp_path / "backup")
    try:
        assert History(connection).get(record.id) == record
    finally:
        connection.close()


def test_query_filters_newest_first_with_limit(history: History) -> None:
    other_project = new_id()
    r1 = _record(timestamp="2026-10-29T09:00:00")
    r2 = _record(timestamp="2026-10-29T10:00:00", firmware_id=new_id(), mac=OTHER_MAC)
    r3 = _record(
        timestamp="2026-10-29T11:00:00",
        operation=Operation.ERASE,
        project_id=None,
        firmware_id=None,
        entries=(),
    )
    r4 = _record(timestamp="2026-10-29T12:00:00", project_id=other_project)
    for record in (r2, r4, r1, r3):  # insertion order does not matter
        history.add(record)

    def ids(records: list[FlashRecord]) -> list[str]:
        return [r.id for r in records]

    assert ids(history.query()) == [r4.id, r3.id, r2.id, r1.id]
    assert ids(history.query(project_id=PROJECT)) == [r2.id, r1.id]
    assert ids(history.query(firmware_id=FIRMWARE)) == [r4.id, r1.id]
    assert ids(history.query(mac=MAC)) == [r4.id, r3.id, r1.id]
    assert ids(history.query(operation=Operation.ERASE)) == [r3.id]
    assert ids(history.query(project_id=PROJECT, mac=MAC)) == [r1.id]
    assert ids(history.query(operation=Operation.READ_FLASH)) == []
    assert ids(history.query(limit=2)) == [r4.id, r3.id]


@pytest.mark.parametrize("spelling", ["AA-BB-CC-DD-EE-FF", "aabbccddeeff", "AA:bb:CC:dd:EE:ff"])
def test_mac_any_spelling_is_stored_and_matched_normalized(history: History, spelling: str) -> None:
    assert normalize_mac(spelling) == MAC
    record = _record(mac="AA-BB-CC-DD-EE-FF")
    history.add(record)
    history.add(_record(mac=OTHER_MAC))
    assert history.get(record.id).mac == MAC
    assert [r.id for r in history.query(mac=spelling)] == [record.id]


@pytest.mark.parametrize(
    "record",
    [
        _record(
            operation=Operation.ERASE,
            project_id=None,
            project_name="",
            firmware_id=None,
            firmware_name="",
            device="",
            entries=(),
            command="esptool --chip esp32 -p COM3 erase-flash",
        ),
        _record(
            operation=Operation.READ_FLASH,
            firmware_id="01JC00000000000000000000D1",  # the dump firmware made from it
            firmware_name="Dump 2026-10-29",
            entries=(RecordEntry("dump.bin", 0x0, "3" * 64, 0x400000, "merged"),),
            command="esptool --chip esp32 -p COM3 read-flash 0x0 0x400000 dump.bin",
        ),
    ],
    ids=["erase", "read_flash"],
)
def test_erase_and_read_flash_records(history: History, record: FlashRecord) -> None:
    history.add(record)
    assert history.get(record.id) == record
    assert [r.id for r in history.query(operation=record.operation)] == [record.id]


def test_duplicate_id_is_refused(history: History) -> None:
    record = _record()
    history.add(record)
    with pytest.raises(ValueError):
        history.add(_record(id=record.id, firmware_name="Other"))
    assert history.get(record.id) == record


def test_get_missing_raises_key_error(history: History) -> None:
    with pytest.raises(KeyError):
        history.get(new_id())


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (ResultEvent(ResultStatus.OK, ""), RecordResult.OK),
        (ResultEvent(ResultStatus.REFUSED, "chip mismatch"), RecordResult.REFUSED),
        (ResultEvent(ResultStatus.CANCELLED, ""), RecordResult.CANCELLED),
        (ErrorEvent("connection lost", "Traceback ..."), RecordResult.FAILED),
    ],
)
def test_result_from_event(event: ResultEvent | ErrorEvent, expected: RecordResult) -> None:
    assert result_from_event(event) is expected


@pytest.mark.parametrize(
    "text", ["", "aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:ff:00", "gg:bb:cc:dd:ee:ff", "aabbccddeef"]
)
def test_normalize_mac_rejects_bad_input(text: str) -> None:
    with pytest.raises(ValueError):
        normalize_mac(text)
