"""core/firmware/image_sniff: classification, merged images, partition tables, otadata."""

import struct
from pathlib import Path

import pytest
from embr_testkit import FIXTURES, IDF, idf_app_path, idf_expect

from embr.core import chipinfo
from embr.core.firmware.image_header import read_image_header
from embr.core.firmware.image_sniff import (
    TYPE_PARTITION_TABLE,
    PartitionTable,
    SniffResult,
    app_address,
    blank_otadata,
    find_merged,
    parse_partition_table,
    sniff,
)
from embr.core.firmware.kinds import EntryKind

SYNTH = FIXTURES / "synthetic"
OTA = IDF / "esp32s3_ota_custom/build"
PIO_S3 = FIXTURES / "pio/arduino_esp32s3_otadata"
CHIP_SAMPLES = ["esp32_hello", "esp32s3_hello", "esp32p4_rev1_hello"]


def _sniff(path: Path, table: PartitionTable | None = None) -> SniffResult:
    return sniff(path.read_bytes(), path.name, table)


def _table(path: Path) -> PartitionTable:
    table = parse_partition_table(path.read_bytes())
    assert table is not None
    return table


def _bootloader(sample: str) -> Path:
    return IDF / sample / "build/bootloader/bootloader.bin"


def _table_file(sample: str) -> Path:
    return IDF / sample / "build/partition_table/partition-table.bin"


# --- ESP images --------------------------------------------------------------------------


@pytest.mark.parametrize("sample", CHIP_SAMPLES)
def test_classifies_chip_samples(sample: str) -> None:
    expect = idf_expect(sample)
    chip = expect["chip"]

    bootloader = _sniff(_bootloader(sample))
    assert (bootloader.kind, bootloader.chip) == (EntryKind.BOOTLOADER, chip)
    assert bootloader.address == int(expect["bootloader_offset"], 16)
    assert bootloader.header is not None

    app = _sniff(idf_app_path(sample))
    assert (app.kind, app.chip, app.address) == (EntryKind.APP, chip, 0x10000)

    table = _sniff(_table_file(sample))
    assert (table.kind, table.address) == (EntryKind.PARTITION_TABLE, 0x8000)
    assert table.partition_table is not None
    # a default table has no partition_table entry, so 0x8000 is a guess
    assert table.confirm


def test_classifies_otadata() -> None:
    initial = OTA / "ota_data_initial.bin"
    with_table = _sniff(initial, _table(OTA / "partition_table/partition-table.bin"))
    assert (with_table.kind, with_table.address) == (EntryKind.OTADATA, 0x17000)
    without_table = _sniff(initial)
    assert without_table.kind == EntryKind.OTADATA
    assert without_table.confirm

    # boot_app0.bin is not blank; it is recognised by name and placed by the table
    boot_app0 = PIO_S3 / "pio_core/packages/framework-arduinoespressif32/tools/partitions"
    table = _table(PIO_S3 / ".pio/build/esp32-s3-devkitc-1/partitions.bin")
    result = _sniff(boot_app0 / "boot_app0.bin", table)
    assert (result.kind, result.address) == (EntryKind.OTADATA, 0xD000)


def test_app_without_descriptor_is_app() -> None:
    result = _sniff(SYNTH / "variants/app_no_desc.bin")
    assert (result.kind, result.chip, result.address) == (EntryKind.APP, "esp32s3", 0x10000)


def test_arduino_bootloaders_without_descriptor() -> None:
    cases = [
        ("pio/arduino_esp32_16mb/.pio/build/esp32dev", "esp32", 0x1000),
        ("pio/arduino_esp32s3_otadata/.pio/build/esp32-s3-devkitc-1", "esp32s3", 0x0),
    ]
    for build, chip, address in cases:
        path = FIXTURES / build / "bootloader.bin"
        header = read_image_header(path.read_bytes())
        assert header is not None and not header.has_bootloader_desc
        result = _sniff(path)
        assert (result.kind, result.chip, result.address) == (EntryKind.BOOTLOADER, chip, address)


def test_conflicting_signals_are_unknown() -> None:
    # a bootloader (all segments in RAM) whose first segment claims to be an app descriptor
    data = bytearray(_bootloader("esp32s3_hello").read_bytes())
    data[0x20:0x24] = struct.pack("<I", 0xABCD5432)
    header = read_image_header(bytes(data))
    assert header is not None and header.has_app_desc
    assert sniff(bytes(data), "bootloader.bin").kind == EntryKind.UNKNOWN


@pytest.mark.parametrize("sample", CHIP_SAMPLES)
def test_segment_addresses_and_flash_map(sample: str) -> None:
    chip = idf_expect(sample)["chip"]
    ranges = chipinfo.flash_map_ranges(chip)
    assert ranges and all(start < end for start, end in ranges)

    def in_flash(address: int) -> bool:
        return any(start <= address < end for start, end in ranges)

    app = read_image_header(idf_app_path(sample).read_bytes())
    bootloader = read_image_header(_bootloader(sample).read_bytes())
    assert app is not None and bootloader is not None
    assert len(app.segment_addresses) == app.segment_count
    assert len(bootloader.segment_addresses) == bootloader.segment_count
    assert any(in_flash(a) for a in app.segment_addresses)
    assert not any(in_flash(a) for a in bootloader.segment_addresses)


# --- merged images -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file", "start", "chip"),
    [
        (SYNTH / "merged/esp32_offset_0x0.bin", 0x0, "esp32"),
        (SYNTH / "merged/esp32_offset_0x1000.bin", 0x1000, "esp32"),
        (SYNTH / "merged/esp32p4_offset_0x2000.bin", 0x2000, "esp32p4"),
        (IDF / "esp32s3_hello_v61/build/merged-binary.bin", 0x0, "esp32s3"),
    ],
)
def test_merged_images(file: Path, start: int, chip: str) -> None:
    data = file.read_bytes()
    assert find_merged(data) == (start, chip)
    result = sniff(data, file.name)
    assert (result.kind, result.chip, result.address) == (EntryKind.MERGED, chip, start)
    assert result.partition_table is not None
    assert result.partition_table.find("factory") is not None


def test_padded_and_single_images() -> None:
    # same as merge-bin --pad-to-size 4MB: 0xFF up to the flash size
    data = (SYNTH / "merged/esp32p4_offset_0x2000.bin").read_bytes()
    data += b"\xff" * (0x400000 - 0x2000 - len(data))
    assert find_merged(data) == (0x2000, "esp32p4")
    # single images are not merged images
    assert find_merged(_bootloader("esp32s3_hello").read_bytes()) is None
    assert find_merged(idf_app_path("esp32_hello").read_bytes()) is None


@pytest.mark.parametrize("sample", ["esp32s3_hello", "esp32_hello"])
def test_app_with_table_bytes_at_merged_offset_is_app(sample: str) -> None:
    # a plain app whose bytes at 0x4000 happen to be a partition table has no bootloader
    data = bytearray(idf_app_path(sample).read_bytes())
    table = _table_file(sample).read_bytes()
    data[0x4000 : 0x4000 + len(table)] = table
    assert sniff(bytes(data), "app.bin").kind == EntryKind.APP


# --- partition tables --------------------------------------------------------------------


def test_default_partition_table() -> None:
    table = _table(_table_file("esp32s3_hello"))
    assert [p.name for p in table.partitions] == ["nvs", "phy_init", "factory"]
    assert (table.has_md5, table.md5_ok) == (True, True)
    factory = table.find("factory")
    assert factory is not None and (factory.offset, factory.size) == (0x10000, 0x100000)
    assert table.end == 0x110000
    assert table.find("missing") is None


def test_partition_table_without_md5() -> None:
    table = _table(SYNTH / "variants/partition_table_no_md5.bin")
    assert (table.has_md5, table.md5_ok) == (False, None)
    assert table.partitions == _table(_table_file("esp32s3_hello")).partitions


def test_custom_partition_table_with_partition_table_entry() -> None:
    path = OTA / "partition_table/partition-table.bin"
    table = _table(path)
    assert len(table.partitions) == 8
    own = table.first(TYPE_PARTITION_TABLE)
    assert own is not None and own.offset == 0x10000
    result = _sniff(path)
    assert (result.kind, result.address, result.confirm) == (
        EntryKind.PARTITION_TABLE,
        0x10000,
        False,
    )


def test_app_address() -> None:
    assert app_address(_table(_table_file("esp32_hello"))) == 0x10000  # factory
    assert app_address(_table(OTA / "partition_table/partition-table.bin")) == 0x20000  # ota_0
    assert app_address(None) == 0x10000
    app = _sniff(OTA / "ota_custom.bin", _table(OTA / "partition_table/partition-table.bin"))
    assert app.address == 0x20000


def test_blank_otadata() -> None:
    table = _table(OTA / "partition_table/partition-table.bin")
    base = [EntryKind.BOOTLOADER, EntryKind.PARTITION_TABLE, EntryKind.APP]
    otadata = blank_otadata(table, base)
    assert otadata is not None and (otadata.offset, otadata.size) == (0x17000, 0x2000)
    assert blank_otadata(table, [*base, EntryKind.OTADATA]) is None
    assert blank_otadata(_table(_table_file("esp32s3_hello")), base) is None
    assert blank_otadata(None, base) is None


# --- negative samples --------------------------------------------------------------------


def test_negative_samples_are_unknown() -> None:
    for path in sorted((SYNTH / "negative").iterdir()):
        result = _sniff(path)
        assert (result.kind, result.chip) == (EntryKind.UNKNOWN, None), path.name


def test_bad_input_and_non_tables() -> None:
    truncated = _bootloader("esp32_hello").read_bytes()[:40]
    for data in (b"", b"\xe9", truncated):
        assert sniff(data, "x.bin").kind == EntryKind.UNKNOWN
        assert find_merged(data) is None
        assert parse_partition_table(data) is None
    assert parse_partition_table(idf_app_path("esp32s3_hello").read_bytes()) is None
    assert parse_partition_table(bytes([0xFF]) * 0xC00) is None
