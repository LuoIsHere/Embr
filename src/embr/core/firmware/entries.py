"""Entry operations: adding files, addresses, placement, layout checks (design doc §3.2,
§5 stage one)."""

import dataclasses
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from embr.core.firmware.image_sniff import PartitionTable, parse_partition_table, sniff
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import AddressSource, Entry, Firmware
from embr.core.storage.ids import new_id

ReadFile = Callable[[Entry], bytes]
"""Returns an entry's bytes, e.g. files.read_entry with the archive folder bound."""


class AddressError(ValueError):
    """Text that is not a hexadecimal address; the entry is left unchanged."""


class FolderError(ValueError):
    """Files outside the folder of a fixed firmware (design doc §12 item 8); nothing was
    changed."""

    def __init__(self, folder: str, outside: list[Path]) -> None:
        names = ", ".join(p.name for p in outside)
        super().__init__(f"{names}: not in the firmware's folder {folder}")
        self.folder = folder
        self.outside = outside


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
    data = path.read_bytes()
    result = sniff(data, path.name, table)
    return Entry(
        id=new_id(),
        name=path.name,
        file=f"path:{path.resolve()}",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        kind=result.kind,
        address=result.address,
        auto_address=result.address,
        address_source=AddressSource.AUTO,
    )


def attachment_entry(
    path: Path, *, address: int | None = None, partition: str | None = None
) -> Entry:
    """New ATTACHMENT entry placed by address (address_source MANUAL) or by partition name
    (address_source PARTITION, address resolved later). Exactly one of the two is given."""
    if (address is None) == (partition is None):
        raise ValueError("give exactly one of address and partition")
    data = path.read_bytes()
    return Entry(
        id=new_id(),
        name=path.name,
        file=f"path:{path.resolve()}",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        kind=EntryKind.ATTACHMENT,
        address=address,
        auto_address=address,
        address_source=AddressSource.MANUAL if partition is None else AddressSource.PARTITION,
        partition=partition,
    )


def blank_otadata_entry(offset: int, size: int) -> Entry:
    """Enabled OTADATA entry of 0xFF bytes: file "blank:", auto_added True."""
    return Entry(
        id=new_id(),
        name="otadata (blank)",
        file="blank:",
        sha256=hashlib.sha256(b"\xff" * size).hexdigest(),
        size=size,
        kind=EntryKind.OTADATA,
        address=offset,
        auto_address=offset,
        address_source=AddressSource.AUTO,
        auto_added=True,
    )


def set_address(entry: Entry, text: str) -> None:
    """Apply text typed into the address field. Empty text restores auto_address (and the
    AUTO or PARTITION source). A hex value ("0x10000" or "10000") sets a MANUAL address,
    except that the detected value itself counts as AUTO. Anything else raises AddressError
    and changes nothing."""
    text = text.strip()
    detected = AddressSource.PARTITION if entry.partition is not None else AddressSource.AUTO
    if not text:
        entry.address = entry.auto_address
        entry.address_source = detected
        return
    if not _HEX.fullmatch(text):
        raise AddressError(f"{text!r} is not a hexadecimal address")
    value = int(text, 16)
    entry.address = value
    entry.address_source = detected if value == entry.auto_address else AddressSource.MANUAL


def copy_entry(entry: Entry) -> Entry:
    """Independent copy with a new id, for adding to another firmware."""
    return dataclasses.replace(entry, id=new_id())


def in_folder(folder: str, path: Path) -> bool:
    """`path` lies in `folder` or in one of its subfolders. Both are resolved (absolute,
    `..` removed) and compared ignoring case, as Windows does."""
    raise NotImplementedError


def entries_outside(firmware: Firmware) -> list[Entry]:
    """Entries with a "path:" file outside firmware.folder ("archive:" and "blank:" entries
    never are); none while the folder is "". Firmware made before the folder rule may have
    such entries; they still flash."""
    raise NotImplementedError


def sorted_entries(entries: list[Entry]) -> list[Entry]:
    """By address; entries without an address last, in their original order."""
    return sorted(entries, key=lambda e: (e.address is None, e.address or 0))


def enabled_partition_table(
    firmware: Firmware, read: ReadFile
) -> tuple[PartitionTable | None, list[Problem]]:
    """The table of the enabled PARTITION_TABLE entry, read through `read`. More than one
    enabled partition table is a REFUSE problem (and no table is returned)."""
    tables = [e for e in firmware.entries if e.enabled and e.kind == EntryKind.PARTITION_TABLE]
    if len(tables) > 1:
        ids = tuple(e.id for e in tables)
        return None, [Problem(ProblemLevel.REFUSE, "more than one partition table is enabled", ids)]
    if not tables:
        return None, []
    table = parse_partition_table(read(tables[0]))
    if table is None:
        return None, [
            Problem(
                ProblemLevel.REFUSE, f"{tables[0].name} is not a partition table", (tables[0].id,)
            )
        ]
    return table, []


def resolve_partitions(firmware: Firmware, table: PartitionTable | None) -> list[Problem]:
    """Set the address of every entry placed by partition to the partition's offset.
    REFUSE when the partition is missing (or there is no table) or the file is larger than the
    partition. Entries placed by address are not touched."""
    problems: list[Problem] = []
    for entry in firmware.entries:
        if entry.partition is None or entry.address_source == AddressSource.MANUAL:
            continue
        part = table.find(entry.partition) if table is not None else None
        if part is None:
            problems.append(
                Problem(
                    ProblemLevel.REFUSE,
                    f"{entry.name}: partition '{entry.partition}' not found",
                    (entry.id,),
                )
            )
            continue
        entry.address = entry.auto_address = part.offset
        if entry.size > part.size:
            problems.append(
                Problem(
                    ProblemLevel.REFUSE,
                    f"{entry.name} ({entry.size:#x} bytes) does not fit "
                    f"partition '{part.name}' ({part.size:#x})",
                    (entry.id,),
                )
            )
    return problems


def check_layout(firmware: Firmware, flash_size: int | None) -> list[Problem]:
    """Problems among enabled entries only: overlapping address ranges (REFUSE), an entry
    beyond `flash_size` bytes (REFUSE, skipped when None), an entry without an address
    (REFUSE)."""
    enabled = [e for e in firmware.entries if e.enabled]
    problems = [
        Problem(ProblemLevel.REFUSE, f"{e.name} has no address", (e.id,))
        for e in enabled
        if e.address is None
    ]
    placed = sorted((e for e in enabled if e.address is not None), key=lambda e: e.address or 0)
    for i, a in enumerate(placed):
        a_end = (a.address or 0) + a.size
        for b in placed[i + 1 :]:
            if (b.address or 0) >= a_end:
                break
            problems.append(
                Problem(ProblemLevel.REFUSE, f"{a.name} and {b.name} overlap", (a.id, b.id))
            )
        if flash_size is not None and a_end > flash_size:
            problems.append(
                Problem(
                    ProblemLevel.REFUSE,
                    f"{a.name} ends at {a_end:#x}, beyond the flash size",
                    (a.id,),
                )
            )
    return problems


_HEX = re.compile(r"(0x)?[0-9a-f]+", re.IGNORECASE)
