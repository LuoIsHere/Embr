"""Classify bin files, detect merged images and parse partition tables (design doc §5,
stage one). Offline only; never opens a serial port. Does not depend on the firmware
model or on the importers."""

import hashlib
import struct
from collections.abc import Iterable
from dataclasses import dataclass

from embr.core import chipinfo
from embr.core.firmware.image_header import ImageHeader, read_image_header
from embr.core.firmware.kinds import EntryKind

PARTITION_TABLE_DEFAULT_OFFSET = 0x8000
PARTITION_TABLE_MAX_SIZE = 0xC00
APP_DEFAULT_OFFSET = 0x10000
OTADATA_SIZE = 0x2000

# partition types and subtypes used here (ESP-IDF partition table format)
TYPE_APP = 0x00
TYPE_DATA = 0x01
TYPE_BOOTLOADER = 0x02
TYPE_PARTITION_TABLE = 0x03
SUBTYPE_FACTORY = 0x00
SUBTYPE_OTA_0 = 0x10
SUBTYPE_OTADATA = 0x00
SUBTYPE_FAT = 0x81
SUBTYPE_SPIFFS = 0x82
SUBTYPE_LITTLEFS = 0x83

_ENTRY_SIZE = 32
_TABLE_MAGIC = b"\xaa\x50"
_MD5_MAGIC = b"\xeb\xeb"
_OTADATA_NAMES = ("ota_data_initial.bin", "boot_app0.bin")
_OTADATA_DEFAULTS = {"boot_app0.bin": 0xE000}  # PIO Arduino; IDF uses 0xD000
_MERGED_SCAN_LIMIT = 0x10000


@dataclass(frozen=True)
class Partition:
    name: str
    type: int
    subtype: int
    offset: int
    size: int
    flags: int


@dataclass(frozen=True)
class PartitionTable:
    partitions: tuple[Partition, ...]
    has_md5: bool
    md5_ok: bool | None
    """None without an MD5 entry."""

    def find(self, name: str) -> Partition | None:
        """Partition by name."""
        return next((p for p in self.partitions if p.name == name), None)

    def first(self, type: int, subtype: int | None = None) -> Partition | None:
        """First partition of a type (and subtype, if given), in table order."""
        return next(
            (p for p in self.partitions if p.type == type and subtype in (None, p.subtype)),
            None,
        )

    @property
    def end(self) -> int:
        """Highest offset + size of all partitions."""
        return max((p.offset + p.size for p in self.partitions), default=0)


@dataclass(frozen=True)
class SniffResult:
    kind: EntryKind
    chip: str | None
    """Chip from the image header (bootloader header for merged images), else None."""
    address: int | None
    """Default address; None when there is no sensible default."""
    confirm: bool
    """True when the default is a guess the user should confirm (e.g. partition table
    without a partition_table entry, otadata without a table)."""
    header: ImageHeader | None
    partition_table: PartitionTable | None
    """Set for partition tables and for merged images (the table found inside)."""


def parse_partition_table(data: bytes) -> PartitionTable | None:
    """Parse the ESP-IDF binary format: 32-byte entries starting with magic 0x50AA,
    an optional 0xEBEB MD5 entry, at most 0xC00 bytes. None if `data` is not a table."""
    if data[:2] != _TABLE_MAGIC:
        return None
    partitions: list[Partition] = []
    has_md5 = False
    md5_ok: bool | None = None
    for pos in range(0, min(len(data), PARTITION_TABLE_MAX_SIZE), _ENTRY_SIZE):
        entry = data[pos : pos + _ENTRY_SIZE]
        if len(entry) < _ENTRY_SIZE or entry == b"\xff" * _ENTRY_SIZE:
            break
        if entry[:2] == _TABLE_MAGIC:
            offset, size = struct.unpack_from("<II", entry, 4)
            (flags,) = struct.unpack_from("<I", entry, 28)
            name = entry[12:28].split(b"\0", 1)[0].decode("ascii", "replace")
            partitions.append(Partition(name, entry[2], entry[3], offset, size, flags))
        elif entry[:2] == _MD5_MAGIC:
            has_md5 = True
            md5_ok = hashlib.md5(data[:pos]).digest() == entry[16:32]
            break
        else:
            return None
    if not partitions:
        return None
    return PartitionTable(tuple(partitions), has_md5, md5_ok)


def find_merged(data: bytes) -> tuple[int, str] | None:
    """(start offset, chip) if `data` is a merged image: for a candidate bootloader offset
    BL (from chipinfo) and start T in {0, BL}, file offset BL - T holds a bootloader image
    whose chip's bootloader offset is BL, and a partition table is found at file offset
    0x8000 - T or, failing that, at a 0x1000-aligned offset in the first 64 KiB. None
    otherwise."""
    found = _find_merged(data)
    return (found[0], found[1]) if found else None


def app_address(table: PartitionTable | None) -> int:
    """factory partition, else the first OTA slot, else APP_DEFAULT_OFFSET."""
    if table is None:
        return APP_DEFAULT_OFFSET
    factory = table.first(TYPE_APP, SUBTYPE_FACTORY)
    if factory is not None:
        return factory.offset
    ota = next(
        (p for p in table.partitions if p.type == TYPE_APP and 0x10 <= p.subtype <= 0x1F), None
    )
    return ota.offset if ota is not None else APP_DEFAULT_OFFSET


def sniff(data: bytes, file_name: str, table: PartitionTable | None = None) -> SniffResult:
    """Classify a file and propose its address. `table` is the firmware's enabled
    partition table, if any. Order: merged image, ESP image, partition table, otadata,
    file system, else UNKNOWN. ESP images: a bootloader descriptor means bootloader, an app
    descriptor means app; without a descriptor, all segments in RAM means bootloader and a
    segment mapped to flash means app; conflicting signals give UNKNOWN. otadata: name
    ota_data_initial.bin / boot_app0.bin, or 8 KiB of 0xFF. Never raises for bad input."""
    merged = _find_merged(data)
    if merged is not None:
        start, chip, header, inner = merged
        return SniffResult(EntryKind.MERGED, chip, start, False, header, inner)
    header = read_image_header(data)
    if header is not None:
        kind = _image_kind(header)
        address: int | None = None
        if kind == EntryKind.BOOTLOADER and header.chip is not None:
            address = chipinfo.get_chip(header.chip).bootloader_offset
        elif kind == EntryKind.APP:
            address = app_address(table)
        return SniffResult(kind, header.chip, address, False, header, None)
    own = parse_partition_table(data)
    if own is not None:
        entry = own.first(TYPE_PARTITION_TABLE)
        offset = entry.offset if entry is not None else PARTITION_TABLE_DEFAULT_OFFSET
        return SniffResult(EntryKind.PARTITION_TABLE, None, offset, entry is None, None, own)
    if file_name.lower() in _OTADATA_NAMES or (
        len(data) == OTADATA_SIZE and data == b"\xff" * OTADATA_SIZE
    ):
        part = table.first(TYPE_DATA, SUBTYPE_OTADATA) if table else None
        if part is not None:
            return SniffResult(EntryKind.OTADATA, None, part.offset, False, None, None)
        guess = _OTADATA_DEFAULTS.get(file_name.lower(), 0xD000)
        return SniffResult(EntryKind.OTADATA, None, guess, True, None, None)
    if b"littlefs" in data[:0x1000]:
        part = _fs_partition(table)
        return SniffResult(EntryKind.FS, None, part.offset if part else None, True, None, None)
    return SniffResult(EntryKind.UNKNOWN, None, None, False, None, None)


def blank_otadata(
    table: PartitionTable | None, enabled_kinds: Iterable[EntryKind]
) -> Partition | None:
    """The otadata partition to fill with a blank (0xFF) entry: returned when the table has
    an otadata partition and no enabled entry is otadata; None otherwise."""
    if table is None or EntryKind.OTADATA in set(enabled_kinds):
        return None
    return table.first(TYPE_DATA, SUBTYPE_OTADATA)


def _image_kind(header: ImageHeader) -> EntryKind:
    ranges = chipinfo.flash_map_ranges(header.chip) if header.chip else ()
    in_flash = any(lo <= a < hi for a in header.segment_addresses for lo, hi in ranges)
    if header.has_bootloader_desc and header.has_app_desc:
        return EntryKind.UNKNOWN
    if header.has_bootloader_desc:
        return EntryKind.UNKNOWN if in_flash else EntryKind.BOOTLOADER
    if header.has_app_desc:
        return EntryKind.APP if in_flash else EntryKind.UNKNOWN
    return EntryKind.APP if in_flash else EntryKind.BOOTLOADER


def _find_merged(data: bytes) -> tuple[int, str, ImageHeader, PartitionTable] | None:
    for bl in chipinfo.bootloader_offsets():
        for start in sorted({0, bl}):
            pos = bl - start
            if pos >= len(data) or data[pos] != 0xE9:
                continue
            header = read_image_header(data[pos:])
            if header is None or header.chip is None:
                continue
            if chipinfo.get_chip(header.chip).bootloader_offset != bl:
                continue
            # with start == bl the image is at file offset 0, so a plain app gets this far
            if _image_kind(header) != EntryKind.BOOTLOADER:
                continue
            table = _table_inside(data, start)
            if table is not None:
                return start, header.chip, header, table
    return None


def _table_inside(data: bytes, start: int) -> PartitionTable | None:
    expected = PARTITION_TABLE_DEFAULT_OFFSET - start
    candidates = [expected] if expected >= 0 else []
    candidates += range(0x1000, min(len(data), _MERGED_SCAN_LIMIT), 0x1000)
    for pos in candidates:
        table = parse_partition_table(data[pos : pos + PARTITION_TABLE_MAX_SIZE])
        if table is not None:
            return table
    return None


def _fs_partition(table: PartitionTable | None) -> Partition | None:
    if table is None:
        return None
    for subtype in (SUBTYPE_LITTLEFS, SUBTYPE_SPIFFS, SUBTYPE_FAT):
        part = table.first(TYPE_DATA, subtype)
        if part is not None:
            return part
    return None
