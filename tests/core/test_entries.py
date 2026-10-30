"""core/firmware/entries: adding files, address edits, partition placement, layout checks."""

import hashlib
import os
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from embr_testkit import FIXTURES, IDF

from embr.core.firmware.entries import (
    AddressError,
    Problem,
    ProblemLevel,
    attachment_entry,
    blank_otadata_entry,
    check_layout,
    copy_entry,
    enabled_partition_table,
    entries_outside,
    entry_from_file,
    in_folder,
    resolve_partitions,
    set_address,
)
from embr.core.firmware.image_sniff import PartitionTable, parse_partition_table
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import AddressSource, Entry, Firmware, Source
from embr.core.storage.ids import new_id

OTA = IDF / "esp32s3_ota_custom/build"
HELLO = IDF / "esp32s3_hello/build"
ATTACH = FIXTURES / "synthetic/attachments"


def _table(path: Path) -> PartitionTable:
    table = parse_partition_table(path.read_bytes())
    assert table is not None
    return table


OTA_TABLE_FILE = OTA / "partition_table/partition-table.bin"


def _firmware(*entries: Entry) -> Firmware:
    return Firmware(
        id=new_id(),
        name="F",
        project_id=None,
        device="",
        source=Source.MANUAL,
        chip="esp32s3",
        entries=list(entries),
    )


def _read(entry: Entry) -> bytes:
    return Path(entry.file.removeprefix("path:")).read_bytes()


def _refusals(problems: list[Problem]) -> list[Problem]:
    return [p for p in problems if p.level == ProblemLevel.REFUSE]


@pytest.mark.parametrize(
    ("path", "with_table", "kind", "address"),
    [
        (IDF / "esp32_hello/build/bootloader/bootloader.bin", False, EntryKind.BOOTLOADER, 0x1000),
        (OTA / "ota_custom.bin", True, EntryKind.APP, 0x20000),
        (OTA / "ota_data_initial.bin", True, EntryKind.OTADATA, 0x17000),
    ],
)
def test_entry_from_file(path: Path, with_table: bool, kind: EntryKind, address: int) -> None:
    data = path.read_bytes()
    entry = entry_from_file(path, _table(OTA_TABLE_FILE) if with_table else None)
    assert (entry.kind, entry.address, entry.auto_address) == (kind, address, address)
    assert entry.address_source == AddressSource.AUTO
    assert entry.name == path.name
    assert entry.file == f"path:{path}"
    assert (entry.sha256, entry.size) == (hashlib.sha256(data).hexdigest(), len(data))
    assert entry.enabled


def test_set_address_manual_clear_and_detected_value() -> None:
    entry = entry_from_file(HELLO / "hello_esp32s3.bin")
    set_address(entry, "0x20000")
    assert (entry.address, entry.address_source, entry.auto_address) == (
        0x20000,
        AddressSource.MANUAL,
        0x10000,
    )
    set_address(entry, "")
    assert (entry.address, entry.address_source) == (0x10000, AddressSource.AUTO)
    set_address(entry, "30000")
    assert (entry.address, entry.address_source) == (0x30000, AddressSource.MANUAL)
    set_address(entry, "10000")  # the detected value counts as automatic
    assert (entry.address, entry.address_source) == (0x10000, AddressSource.AUTO)


def test_set_address_rejects_non_hex() -> None:
    entry = entry_from_file(HELLO / "hello_esp32s3.bin")
    set_address(entry, "0x30000")
    before = asdict(entry)
    for text in ("xyz", "0x1G000"):
        with pytest.raises(AddressError):
            set_address(entry, text)
        assert asdict(entry) == before


def test_clearing_partition_placed_address_restores_partition() -> None:
    entry = attachment_entry(ATTACH / "assets_fits.bin", partition="assets")
    assert entry.address_source == AddressSource.PARTITION
    fw = _firmware(entry)
    table = _table(OTA_TABLE_FILE)
    assert _refusals(resolve_partitions(fw, table)) == []
    set_address(entry, "0x300000")
    assert entry.address_source == AddressSource.MANUAL
    set_address(entry, "")
    assert entry.address_source == AddressSource.PARTITION
    resolve_partitions(fw, table)
    assert entry.address == 0x1A000


def test_copy_entry_is_independent() -> None:
    original = entry_from_file(HELLO / "hello_esp32s3.bin")
    copy = copy_entry(original)
    assert copy.id != original.id
    assert asdict(replace(copy, id=original.id)) == asdict(original)
    set_address(copy, "0x20000")
    copy.note = "copy only"
    copy.enabled = False
    assert (original.address, original.note, original.enabled) == (0x10000, "", True)


def test_enabled_partition_table_refuses_two() -> None:
    first = entry_from_file(OTA_TABLE_FILE)
    second = entry_from_file(HELLO / "partition_table/partition-table.bin")
    fw = _firmware(first, second)
    table, problems = enabled_partition_table(fw, _read)
    assert table is None
    assert _refusals(problems)

    second.enabled = False
    table, problems = enabled_partition_table(fw, _read)
    assert table is not None and table.find("assets") is not None
    assert _refusals(problems) == []


def test_resolve_partitions_places_by_partition_only() -> None:
    by_partition = attachment_entry(ATTACH / "assets_fits.bin", partition="assets")
    by_address = attachment_entry(ATTACH / "calib_at_address.bin", address=0x3FF000)
    assert by_address.address_source == AddressSource.MANUAL
    fw = _firmware(by_partition, by_address)
    assert _refusals(resolve_partitions(fw, _table(OTA_TABLE_FILE))) == []
    assert by_partition.address == 0x1A000
    assert by_address.address == 0x3FF000


def test_resolve_partitions_refuses() -> None:
    table = _table(OTA_TABLE_FILE)
    cases = [
        ("assets_too_big.bin", "assets", table),  # file larger than the partition
        ("assets_fits.bin", "no_such_partition", table),
        ("assets_fits.bin", "assets", None),
    ]
    for file, partition, case_table in cases:
        entry = attachment_entry(ATTACH / file, partition=partition)
        refusals = _refusals(resolve_partitions(_firmware(entry), case_table))
        assert refusals, (file, partition)
        assert entry.id in refusals[0].entry_ids


def test_check_layout_overlap_and_disabled_entries() -> None:
    app = entry_from_file(HELLO / "hello_esp32s3.bin")
    overlapping = attachment_entry(ATTACH / "calib_at_address.bin", address=0x10800)
    fw = _firmware(entry_from_file(HELLO / "bootloader/bootloader.bin"), app, overlapping)

    refusals = _refusals(check_layout(fw, None))
    assert len(refusals) == 1
    assert set(refusals[0].entry_ids) == {app.id, overlapping.id}

    overlapping.enabled = False
    assert check_layout(fw, None) == []


def test_check_layout_flash_size_and_missing_address() -> None:
    # 0x1000 bytes at 0x3FF000 end exactly at 4 MiB
    calib = attachment_entry(ATTACH / "calib_at_address.bin", address=0x3FF000)
    fw = _firmware(calib)
    assert check_layout(fw, 0x400000) == []
    beyond = _refusals(check_layout(fw, 0x200000))
    assert beyond and calib.id in beyond[0].entry_ids

    unresolved = attachment_entry(ATTACH / "assets_fits.bin", partition="assets")
    fw.entries.append(unresolved)
    missing = _refusals(check_layout(fw, 0x400000))
    assert missing and unresolved.id in missing[0].entry_ids


# --- one folder per fixed firmware (design doc §12 item 8) -------------------------------

IN_FOLDER: dict[str, tuple[Callable[[Path], str], Callable[[Path], Path], bool]] = {
    "file": (str, lambda r: r / "a.bin", True),
    "subfolder": (str, lambda r: r / "sub" / "a.bin", True),
    "case": (lambda r: str(r).upper(), lambda r: r / "a.bin", True),
    "trailing_separator": (lambda r: str(r) + os.sep, lambda r: r / "a.bin", True),
    "dotdot_in_folder": (lambda r: str(r / "sub" / ".."), lambda r: r / "a.bin", True),
    "dotdot_out_of_it": (str, lambda r: r / "sub" / ".." / ".." / "a.bin", False),
    "sibling_with_same_prefix": (str, lambda r: r.parent / f"{r.name}2" / "a.bin", False),
}


@pytest.mark.parametrize("case", list(IN_FOLDER))
def test_in_folder(tmp_path: Path, case: str) -> None:
    folder, path, inside = IN_FOLDER[case]
    root = tmp_path / "root"
    assert in_folder(folder(root), path(root)) is inside


def test_entries_outside_lists_only_path_entries_outside_the_folder(tmp_path: Path) -> None:
    def entry(file: str) -> Entry:
        return replace(entry_from_file(HELLO / "hello_esp32s3.bin"), file=file)

    inside = entry(f"path:{tmp_path / 'root' / 'sub' / 'a.bin'}")
    outside = entry(f"path:{tmp_path / 'other' / 'a.bin'}")
    archived = entry(f"archive:{'0' * 64}")
    fw = _firmware(inside, outside, archived, blank_otadata_entry(0xD000, 0x2000))
    assert entries_outside(fw) == []  # no folder: firmware made before the rule
    fw.folder = str(tmp_path / "root")
    assert entries_outside(fw) == [outside]
