"""Entry operations: adding files, addresses, placement, layout checks (design doc §3.2,
§5 stage one)."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from embr.core.firmware.image_sniff import PartitionTable
from embr.core.firmware.model import Entry, Firmware

ReadFile = Callable[[Entry], bytes]
"""Returns an entry's bytes, e.g. files.read_entry with the archive folder bound."""


class AddressError(ValueError):
    """Text that is not a hexadecimal address; the entry is left unchanged."""


class ProblemLevel(StrEnum):
    WARN = "warn"
    REFUSE = "refuse"


@dataclass(frozen=True)
class Problem:
    level: ProblemLevel
    message: str
    entry_ids: tuple[str, ...]
    """Entries the problem is about."""


def entry_from_file(path: Path, table: PartitionTable | None = None) -> Entry:
    """New enabled entry for a file: name = file name, file = "path:<absolute>", sha256 and
    size of the content, kind and address from image_sniff (address_source AUTO)."""
    raise NotImplementedError


def attachment_entry(
    path: Path, *, address: int | None = None, partition: str | None = None
) -> Entry:
    """New ATTACHMENT entry placed by address (address_source MANUAL) or by partition name
    (address_source PARTITION, address resolved later). Exactly one of the two is given."""
    raise NotImplementedError


def blank_otadata_entry(offset: int, size: int) -> Entry:
    """Enabled OTADATA entry of 0xFF bytes: file "blank:", auto_added True."""
    raise NotImplementedError


def set_address(entry: Entry, text: str) -> None:
    """Apply text typed into the address field. Empty text restores auto_address (and the
    AUTO or PARTITION source). A hex value ("0x10000" or "10000") sets a MANUAL address,
    except that the detected value itself counts as AUTO. Anything else raises AddressError
    and changes nothing."""
    raise NotImplementedError


def copy_entry(entry: Entry) -> Entry:
    """Independent copy with a new id, for adding to another firmware."""
    raise NotImplementedError


def sorted_entries(entries: list[Entry]) -> list[Entry]:
    """By address; entries without an address last, in their original order."""
    raise NotImplementedError


def enabled_partition_table(
    firmware: Firmware, read: ReadFile
) -> tuple[PartitionTable | None, list[Problem]]:
    """The table of the enabled PARTITION_TABLE entry, read through `read`. More than one
    enabled partition table is a REFUSE problem (and no table is returned)."""
    raise NotImplementedError


def resolve_partitions(firmware: Firmware, table: PartitionTable | None) -> list[Problem]:
    """Set the address of every entry placed by partition to the partition's offset.
    REFUSE when the partition is missing (or there is no table) or the file is larger than the
    partition. Entries placed by address are not touched."""
    raise NotImplementedError


def check_layout(firmware: Firmware, flash_size: int | None) -> list[Problem]:
    """Problems among enabled entries only: overlapping address ranges (REFUSE), an entry
    beyond `flash_size` bytes (REFUSE, skipped when None), an entry without an address
    (REFUSE)."""
    raise NotImplementedError
