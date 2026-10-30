"""Firmware and entry model (design doc §3.1, §3.2). References a project only by id."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from embr.core import chipinfo
from embr.core.firmware.kinds import EntryKind
from embr.core.flasher.plan import AFTER_MODES, BEFORE_MODES
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
    chip: str | None = None
    """MCU chosen by hand for a file that names no chip in an image header (partition
    table, data images, non-ESP files); ignored when the header names one (design doc §12
    item 13). See app.chips for the MCU an entry is flashed to."""


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
    """Tracking firmware: the chip of the build (derived). Fixed firmware: None; their chips
    come from the entries (app.chips.firmware_chips)."""
    notes: str = ""
    tags: list[str] = field(default_factory=lambda: list[str]())
    flash: FlashParams = field(default_factory=FlashParams)
    before: str = "default-reset"
    after: str = "hard-reset"
    erase_all: bool = False
    stub: bool = True
    """Load the flasher stub. IDF tracking: extra_esptool_args.stub (derived, not stored);
    always True for other firmware."""
    entries: list[Entry] = field(default_factory=lambda: list[Entry]())
    """Fixed firmware: all entries. Tracking firmware: derived entries of the last resolve
    (not stored; rebuilt by the resolver)."""
    build_ref: BuildRef | None = None
    folder: str = ""
    """Fixed firmware: the folder of the first file added; later "path:" entries must lie
    in it or below it (design doc §12 item 8). "" until a file is added."""
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
    if firmware.source == Source.IDF_BUILD:
        return _IDF_PARAMS
    if firmware.source == Source.PIO_BUILD:
        return _PIO_PARAMS
    return _FIXED_PARAMS


def set_param(firmware: Firmware, name: str, value: object) -> None:
    """Change one parameter. ReadOnlyFieldError if not editable for this firmware;
    ValueError for an invalid value (unknown chip, flash setting the chip does not support,
    unknown reset mode, wrong type)."""
    if name not in PARAMS:
        raise ValueError(f"unknown parameter {name!r}")
    if name not in editable_params(firmware):
        raise ReadOnlyFieldError(f"{name} is derived from the build output")
    if name in ("erase_all",):
        if not isinstance(value, bool):
            raise ValueError("erase_all must be true or false")
        firmware.erase_all = value
    elif name == "tags":
        if not isinstance(value, list) or not all(
            isinstance(t, str) for t in cast(list[object], value)
        ):
            raise ValueError("tags must be a list of strings")
        firmware.tags = [t for t in cast(list[str], value)]
    elif not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    elif name == "notes":
        firmware.notes = value
    elif name == "chip":
        try:
            chipinfo.get_chip(value)
        except chipinfo.UnknownChipError:
            raise ValueError(f"unknown chip {value!r}") from None
        firmware.chip = value
    elif name == "before":
        if value not in BEFORE_MODES:
            raise ValueError(f"unknown reset mode {value!r}")
        firmware.before = value
    elif name == "after":
        if value not in AFTER_MODES:
            raise ValueError(f"unknown reset mode {value!r}")
        firmware.after = value
    elif name == "flash_mode":
        if value not in ("keep", *chipinfo.FLASH_MODES):
            raise ValueError(f"unknown flash mode {value!r}")
        firmware.flash.mode = value
    elif name == "flash_freq":
        if value != "keep" and value not in _choices(firmware.chip, "freq"):
            raise ValueError(f"flash frequency {value!r} is not supported")
        firmware.flash.freq = value
    else:  # flash_size
        if value not in ("keep", "detect") and value not in _choices(firmware.chip, "size"):
            raise ValueError(f"flash size {value!r} is not supported")
        firmware.flash.size = value


_FIXED_PARAMS = frozenset(PARAMS)


_IDF_PARAMS = frozenset({"erase_all", "tags", "notes"})


_PIO_PARAMS = frozenset({"before", "after", "erase_all", "tags", "notes"})


def _choices(chip: str | None, what: str) -> set[str]:
    """Flash sizes / frequencies of the chip, or of any chip when the chip is not set."""
    chips = [chipinfo.get_chip(chip)] if chip else list(chipinfo.supported_chips())
    return {v for c in chips for v in (c.flash_sizes if what == "size" else c.flash_freqs)}
