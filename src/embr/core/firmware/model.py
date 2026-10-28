"""Firmware and entry model (design doc §3.1, §3.2). References a project only by id."""

from dataclasses import dataclass, field
from enum import StrEnum

from embr.core.firmware.kinds import EntryKind
from embr.core.storage.documents import Document

FIRMWARE_SCHEMA_VERSION = 1


class Source(StrEnum):
    MANUAL = "manual"
    ARCHIVED = "archived"
    IDF_BUILD = "idf_build"
    PIO_BUILD = "pio_build"

    @property
    def tracking(self) -> bool:
        """True for firmware that follows a build directory."""
        return self in (Source.IDF_BUILD, Source.PIO_BUILD)


class AddressSource(StrEnum):
    AUTO = "auto"
    """Address proposed by image_sniff."""
    MANUAL = "manual"
    """Address typed by the user; never overwritten by detection."""
    PARTITION = "partition"
    """Address taken from the partition table (placement by partition name)."""


@dataclass
class Entry:
    id: str
    name: str
    """File name; never edited (archived entries keep the recorded original name)."""
    file: str
    """"path:<absolute path>", "archive:<sha256>" or "blank:" (0xFF of `size` bytes)."""
    sha256: str
    size: int
    kind: EntryKind
    address: int | None
    """Current address. For placement by partition it is resolved from the table."""
    auto_address: int | None
    """Detected address, kept after manual edits so clearing the field can restore it."""
    address_source: AddressSource
    partition: str | None = None
    """Partition name when placed by partition, else None."""
    enabled: bool = True
    note: str = ""
    from_dump: bool = False
    auto_added: bool = False
    """Blank otadata added automatically."""


@dataclass
class FlashParams:
    mode: str = "keep"
    freq: str = "keep"
    size: str = "keep"


@dataclass
class BuildRef:
    path: str
    """IDF: build directory. PIO: project root."""
    env: str | None = None
    """PIO environment name."""


@dataclass
class EntryOverride:
    """User changes to a derived entry of a tracking firmware."""

    address: int | None = None
    enabled: bool | None = None
    note: str | None = None


@dataclass
class Firmware:
    id: str
    name: str
    project_id: str | None
    device: str
    """Group name inside the project; "" means ungrouped."""
    source: Source
    chip: str | None = None
    notes: str = ""
    tags: list[str] = field(default_factory=lambda: list[str]())
    flash: FlashParams = field(default_factory=FlashParams)
    before: str = "default-reset"
    after: str = "hard-reset"
    erase_all: bool = False
    entries: list[Entry] = field(default_factory=lambda: list[Entry]())
    """Fixed firmware: all entries. Tracking firmware: derived entries of the last resolve
    (not stored; rebuilt by the resolver)."""
    build_ref: BuildRef | None = None
    framework_pkg: str = ""
    archived_from: str = ""
    user_entries: list[Entry] = field(default_factory=lambda: list[Entry]())
    entry_overrides: dict[str, EntryOverride] = field(
        default_factory=lambda: dict[str, EntryOverride]()
    )
    """Tracking firmware: keyed by "<kind>:<file name in the build output>"."""
    last_resolved: Document | None = None
    created_at: str = ""
    updated_at: str = ""
    read_only: bool = False
    """Loaded from a file written by a newer Embr; saving is refused."""


# parameter names for editable_params() / set_param()
PARAMS = (
    "chip",
    "flash_mode",
    "flash_freq",
    "flash_size",
    "before",
    "after",
    "erase_all",
    "tags",
    "notes",
)


class ReadOnlyFieldError(ValueError):
    """The parameter is derived from the build output for this firmware."""


def editable_params(firmware: Firmware) -> frozenset[str]:
    """Fixed (manual, archived): all PARAMS. IDF tracking: erase_all, tags, notes.
    PIO tracking: before, after, erase_all, tags, notes."""
    raise NotImplementedError


def set_param(firmware: Firmware, name: str, value: object) -> None:
    """Change one parameter. ReadOnlyFieldError if not editable for this firmware;
    ValueError for an invalid value (unknown chip, flash setting the chip does not support,
    unknown reset mode, wrong type)."""
    raise NotImplementedError
