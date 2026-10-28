"""Classify bin files, detect merged images and parse partition tables (design doc §5,
stage one). Offline only; never opens a serial port. Does not depend on the firmware
model or on the importers."""

from collections.abc import Iterable
from dataclasses import dataclass

from embr.core.firmware.image_header import ImageHeader
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
        raise NotImplementedError

    def first(self, type: int, subtype: int | None = None) -> Partition | None:
        """First partition of a type (and subtype, if given), in table order."""
        raise NotImplementedError

    @property
    def end(self) -> int:
        """Highest offset + size of all partitions."""
        raise NotImplementedError


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
    raise NotImplementedError


def find_merged(data: bytes) -> tuple[int, str] | None:
    """(start offset, chip) if `data` is a merged image: for a candidate bootloader offset
    BL (from chipinfo) and start T in {0, BL}, file offset BL - T holds an image whose chip's
    bootloader offset is BL, and a partition table is found at file offset 0x8000 - T or, failing
    that, at a 0x1000-aligned offset in the first 64 KiB. None otherwise."""
    raise NotImplementedError


def app_address(table: PartitionTable | None) -> int:
    """factory partition, else the first OTA slot, else APP_DEFAULT_OFFSET."""
    raise NotImplementedError


def sniff(data: bytes, file_name: str, table: PartitionTable | None = None) -> SniffResult:
    """Classify a file and propose its address. `table` is the firmware's enabled
    partition table, if any. Order: merged image, ESP image, partition table, otadata,
    file system, else UNKNOWN. ESP images: a bootloader descriptor means bootloader, an app
    descriptor means app; without a descriptor, all segments in RAM means bootloader and a
    segment mapped to flash means app; conflicting signals give UNKNOWN. otadata: name
    ota_data_initial.bin / boot_app0.bin, or 8 KiB of 0xFF. Never raises for bad input."""
    raise NotImplementedError


def blank_otadata(
    table: PartitionTable | None, enabled_kinds: Iterable[EntryKind]
) -> Partition | None:
    """The otadata partition to fill with a blank (0xFF) entry: returned when the table has
    an otadata partition and no enabled entry is otadata; None otherwise."""
    raise NotImplementedError
