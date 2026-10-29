"""Resolve tracking firmware against its build output (design doc §3.4).

Source-agnostic: an importer (import_idf; import_pio later) describes the build as a
DerivedBuild. resolve() classifies its files, applies the user's entry_overrides and
user_entries, cross-checks chips and ESP-IDF versions, and compares the result with
last_resolved. accept() stores a resolution as the new last_resolved.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from embr.core.firmware.filecache import FileCache
from embr.core.firmware.kinds import EntryKind
from embr.core.firmware.model import Firmware, FlashParams
from embr.core.storage.documents import Document


class ResolveState(StrEnum):
    OK = "ok"
    """Same as last_resolved."""
    UPDATED = "updated"
    """Only file contents changed: followed without confirmation (accept() is applied)."""
    NEEDS_CONFIRMATION = "needs_confirmation"
    """Anything else changed since last_resolved; not flashable until confirmed."""
    TARGET_SWITCHED = "target_switched"
    """The build output agrees with itself but not with the configured target: rebuild."""
    INCOMPLETE = "incomplete"
    """A file is missing or unparsable, or chips / IDF versions disagree inside the build."""
    MISSING = "missing"
    """The build directory or its description file is gone."""
    BUILDING = "building"
    """Set by the app when a source file changed moments ago; never returned by resolve()."""

    @property
    def flashable(self) -> bool:
        """OK and UPDATED."""
        return self in (ResolveState.OK, ResolveState.UPDATED)


class ChangeKind(StrEnum):
    """Change kinds in the order the change list shows them."""

    CHIP = "chip"
    IDF_VERSION = "idf_version"
    REVISION_RANGE = "revision_range"
    """Chip revision range in a bootloader or app image header."""
    FLASH = "flash"
    RESET = "reset"
    PARTITION_TABLE = "partition_table"
    """Content of the partition table file."""
    ENTRY_ADDED = "entry_added"
    ENTRY_REMOVED = "entry_removed"
    ADDRESS = "address"
    """Derived address changed (also when a manual address overrides it)."""
    OVERRIDE_DROPPED = "override_dropped"
    """An entry_overrides key matches no file any more; dropped on confirmation."""
    MANUAL_ADDRESS = "manual_address"
    """Manual address of a derived entry, listed on a chip or IDF change for review (on a
    chip change it is dropped on confirmation)."""
    ATTACHMENT = "attachment"
    """User entry placed by address, listed on a chip change for review; or one placed by
    partition whose address followed the new table."""
    CONTENT = "content"
    """Same file, new content. The only kind in an UPDATED resolution."""
    GROUP_HINT = "group_hint"
    """Chip changed: the device group may need adjusting."""


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    subject: str
    """Entry name, or "" for firmware-wide changes."""
    before: str
    after: str
    message: str
    """One line for the change list, e.g. "Target chip: esp32s3 -> esp32p4"."""


class EntryMark(StrEnum):
    CHANGED = "changed"
    """Content changed since last_resolved."""
    NEW = "new"
    """File not in last_resolved."""
    MISSING = "missing"
    """In last_resolved (or flash_files) but the file is gone."""


@dataclass(frozen=True)
class DerivedFile:
    address: int
    path: Path
    """Absolute."""


@dataclass(frozen=True)
class DerivedBuild:
    """What an importer read from the build output, before any file is classified."""

    files: tuple[DerivedFile, ...]
    declared_chips: tuple[str, ...]
    """Chips the build output declares (IDF: flasher_args chip and project_description
    target); empty when it declares none (PIO)."""
    configured_target: str | None
    """Target in the project configuration; None when unknown (the check is skipped)."""
    idf_version: str | None
    """ESP-IDF version from the build metadata; None skips the version checks (PIO)."""
    flash: FlashParams
    before: str | None
    after: str | None
    """Reset modes from the build output; None keeps the firmware's own (PIO)."""
    stub: bool
    inputs: tuple[Path, ...]
    """Files whose stat decides whether the build must be resolved again."""


@dataclass(frozen=True)
class Resolution:
    state: ResolveState
    firmware: Firmware
    """A copy of the firmware as it would be flashed: chip, flash, reset and stub from the
    build; entries = derived entries (id = entry_key(), overrides applied, partition
    placement resolved) followed by user_entries. For MISSING: entries rebuilt from
    last_resolved for display."""
    changes: tuple[Change, ...]
    """Against last_resolved, ordered by ChangeKind. Empty for OK."""
    problems: tuple[str, ...]
    """Why the state is INCOMPLETE, TARGET_SWITCHED or MISSING, showing the values of
    every side (e.g. "sdkconfig: esp32p4, build output: esp32s3")."""
    marks: Mapping[str, EntryMark]
    """Entry id -> mark, for the entries table."""
    snapshot: Document
    """What accept() stores as last_resolved (chip, IDF version, flash, reset, partition
    table hash, per derived entry: key, name, address, sha256, size, kind and image
    revision range, and the time)."""
    idf_version: str | None
    configured_target: str | None


def entry_key(kind: EntryKind, file_name: str) -> str:
    """Key of a derived entry in entry_overrides and its entry id: "<kind>:<file name>"."""
    raise NotImplementedError


def resolve(
    firmware: Firmware, build: DerivedBuild, cache: FileCache, *, archive_dir: Path, now: str
) -> Resolution:
    """Resolve `firmware` (a tracking firmware) against `build`. Never raises for a broken
    build; the state says what is wrong. In order of precedence:
    1. a build file missing, or a bootloader / app / partition table that cannot be parsed
       -> INCOMPLETE
    2. chips disagree among declared_chips and the bootloader and app image headers, or
       the IDF versions of the build metadata, bootloader descriptor and app descriptor
       disagree (compared on the first 31 characters; a missing descriptor is skipped)
       -> INCOMPLETE
    3. configured_target set and different from the build's chip -> TARGET_SWITCHED
    4. no last_resolved -> OK (first resolve; the importer accepts it)
    5. only CONTENT changes -> UPDATED; any other change -> NEEDS_CONFIRMATION; else OK
    A derived entry whose override sets a manual address while the derived address changed
    is NEEDS_CONFIRMATION (manual address kept). User entries are checked by name, path and
    hash like fixed entries; a changed or missing user entry is NEEDS_CONFIRMATION or
    INCOMPLETE respectively. `now` is stored in the snapshot."""
    raise NotImplementedError


def missing(firmware: Firmware, problem: str) -> Resolution:
    """MISSING resolution showing the last_resolved snapshot (no entries without one)."""
    raise NotImplementedError


def accept(firmware: Firmware, resolution: Resolution) -> None:
    """Store resolution.snapshot as last_resolved and apply what confirmation implies:
    on a chip change, the chip and flash parameters follow the build and the manual
    addresses of derived entries are dropped; overrides whose key matches no derived entry
    are dropped. User entries are kept. Changed user entries take their new hash and size.
    ValueError for states other than OK, UPDATED and NEEDS_CONFIRMATION."""
    raise NotImplementedError


def digest(firmware: Firmware) -> str:
    """Short hash of what a flash would write: chip, flash parameters, reset modes, stub,
    erase_all and (address, sha256) of every enabled entry. Equal digests mean the same
    flash job. Works for fixed firmware too."""
    raise NotImplementedError
